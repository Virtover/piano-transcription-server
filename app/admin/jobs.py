"""`jobs`: list jobs, show one in full, cancel a stuck one, and queue a
failed one again. Jobs are read from their diagnostics records, which outlive
the Redis job, and from Redis while they are active."""

import json
import time
from pathlib import Path

from fastapi import HTTPException

from app import job_limits
from app.admin.common import (
    AdminError,
    audit,
    billing_enabled,
    confirm,
    find_job,
    find_user,
    shorten,
    span,
    table,
    user_label,
    when,
)
from app.auth import AuthenticatedUser
from app.billing import cancelled_cost
from app.config import duration_seconds, settings
from app.outsource import routing
from app.worker import tasks

# Fields shown by `show` only as a summary: they are long.
SUMMARISED_FIELDS = {"metadata"}


def list_jobs(args) -> None:
    conditions, parameters = [], []
    status = "failed" if args.failed else args.status
    if status:
        conditions.append("j.status = ?")
        parameters.append(status)
    if args.backend:
        conditions.append("j.backend = ?")
        parameters.append(args.backend)
    if args.user:
        conditions.append("j.user_id = ?")
        parameters.append(find_user(args.user)["user_id"] if billing_enabled() else args.user)
    if args.since:
        try:
            seconds = duration_seconds(args.since, "--since")
        except ValueError as error:
            raise AdminError(str(error)) from error
        conditions.append("j.created_at >= ?")
        parameters.append(int(time.time() - seconds))
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = tasks.billing.read(
        f"""SELECT j.*, u.email FROM job_diagnostics AS j LEFT JOIN users AS u ON u.user_id = j.user_id
            {where} ORDER BY j.created_at DESC LIMIT ?""",
        (*parameters, args.limit),
    )
    if not rows:
        print("No jobs match")
        return
    print(
        table(
            ["Created", "Job", "User", "Status", "Backend", "Length", "Error"],
            [
                [
                    when(row["created_at"]),
                    row["job_id"][:8],
                    shorten(user_label(row["user_id"], row["email"]), 32),
                    row["status"],
                    row["backend"],
                    span(row["duration_seconds"]),
                    shorten(row["error"], 60),
                ]
                for row in rows
            ],
        )
    )
    if len(rows) == args.limit:
        print(f"\nShowing the newest {args.limit}; use --limit for more.")


def show(args) -> None:
    job_id = find_job(args.job)
    job = tasks.redis.hgetall(tasks.job_key(job_id))
    records = tasks.billing.read(
        "SELECT j.*, u.email FROM job_diagnostics AS j LEFT JOIN users AS u ON u.user_id = j.user_id WHERE job_id = ?",
        (job_id,),
    )
    reservations = tasks.billing.read("SELECT * FROM transcription_reservations WHERE job_id = ?", (job_id,))

    print(f"Job {job_id}")
    if job:
        print("\nIn Redis (live):")
        for field in sorted(job):
            value = job[field]
            if field in SUMMARISED_FIELDS:
                value = f"{len(value)} characters of JSON"
            print(f"  {field}: {value}")
    else:
        print("\nNot in Redis: it ended more than an hour ago, or was never queued.")

    if records:
        record = records[0]
        print("\nDiagnostics record:")
        for field in ("user_id", "email", "source", "source_url", "status", "backend", "requeue_reason"):
            print(f"  {field}: {record[field] if record[field] is not None else '-'}")
        print(f"  outsourced: {'yes' if record['outsourced'] else 'no'}")
        print(f"  length: {span(record['duration_seconds'])}, cost: {record['cost_minutes'] or '-'} minutes")
        for field in ("created_at", "started_at", "finished_at", "updated_at"):
            print(f"  {field}: {when(record[field])}")
        if record["started_at"] and record["finished_at"]:
            print(f"  ran for: {span(record['finished_at'] - record['started_at'])}")
        # The full error, which users never see.
        print(f"  error: {record['error'] or '-'}")

    if reservations:
        reservation = reservations[0]
        print(
            f"\nReservation: {reservation['status']}, {reservation['reserved_minutes']} minutes reserved, "
            f"{reservation['charged_minutes'] if reservation['charged_minutes'] is not None else '-'} charged"
        )

    folder = Path(settings.service.data_dir) / "jobs" / job_id
    if folder.is_dir():
        files = sorted(path.name for path in folder.iterdir())
        print(f"\nFiles: {', '.join(files) or 'none'}")


def cancel(args) -> None:
    job_id = find_job(args.job)
    job = tasks.redis.hgetall(tasks.job_key(job_id))
    status = job.get("status")
    if status not in {"queued", "processing"}:
        if status in {"preparing", "uploading"}:
            raise AdminError(f"The job is {status}; it is not queued yet, and its placeholder expires on its own")
        raise AdminError(f"Only queued or processing jobs can be cancelled; this one is {status or 'gone from Redis'}")

    progress = float(job.get("progress") or 0)
    if not job.get("user_id") or not billing_enabled():
        charge_text = "nothing is charged (billing is off)"
    elif args.refund:
        charge_text = "nothing is charged (--refund)"
    else:
        charge = cancelled_cost(int(job.get("full_cost") or 0), progress)
        charge_text = f"{charge} of {job.get('full_cost')} minutes are charged, as when the user cancels"
    print(f"Job {job_id}: {status} on {job.get('backend', '-')}, {progress:.0%} done, user {job.get('user_id', '-')}")
    print(f"Cancelling it: {charge_text}.")
    if not confirm("Cancel this job?", args.yes):
        print("Nothing changed")
        return
    before = tasks.cancel_job(job_id, charge=not args.refund)
    if before not in {"queued", "processing"}:
        raise AdminError(f"The job changed meanwhile and is now {before}; nothing was done")
    audit(args, "job.cancel", job_id, "refund" if args.refund else "cancellation price")
    print("Cancelled")


def requeue(args) -> None:
    """Queue a failed link job again under its ID, so the user's app finds
    it, and charge it again like a new submission."""
    from app.api.routes import transcriptions as transcription_routes

    job_id = find_job(args.job)
    key = tasks.job_key(job_id)
    job = tasks.redis.hgetall(key)
    if not job:
        raise AdminError("The job is gone from Redis (jobs are kept for an hour after they end); ask the user to submit it again")
    if job.get("status") != "failed":
        raise AdminError(f"Only failed jobs can be queued again; this one is {job.get('status')}")
    if job.get("source") == "upload":
        raise AdminError("The uploaded file was deleted when the job failed; ask the user to upload it again")
    backend = args.backend
    if backend == "remote" and not routing.outsourcing_enabled():
        raise AdminError("Outsourcing is off (OUTSOURCE_PROVIDER=none), so the job cannot run remotely")

    user_id = job.get("user_id") or "anonymous"
    user = None
    if billing_enabled() and user_id != "anonymous":
        account = find_user(user_id)
        user = AuthenticatedUser(user_id, account["email"] or "")
    print(f"Job {job_id}: failed with: {job.get('error_detail') or job.get('error')}")
    where = f"the {backend} backend" if backend else "the backend routing chooses"
    cost = "it is charged again like a new submission" if user else "nothing is charged"
    print(f"Queueing it again on {where}; {cost}.")
    if not confirm("Queue this job again?", args.yes):
        print("Nothing changed")
        return

    # Back to how a new submission starts: a placeholder that holds a place.
    tasks.redis.hset(key, "status", "preparing")
    tasks.redis.hdel(key, "error", "error_detail", "result")
    try:
        job_limits.acquire(tasks.redis, job_id, user_id, tasks.job_key)
        reason = f"requeued by {args.by}" + (f": {args.reason}" if args.reason else "")
        tasks.billing.reopen_job_record(job_id, reason)
        response = transcription_routes.submit_link(job_id, job["source_url"], user, backend)
    except (HTTPException, job_limits.TooManyJobs, ValueError) as error:
        restore(job_id, job, user_id)
        raise AdminError(f"Could not queue the job: {refusal_text(error)}") from error
    except BaseException:
        restore(job_id, job, user_id)
        raise
    audit(args, "job.requeue", job_id, f"backend {tasks.redis.hget(key, 'backend')}")
    balance = f"; the user has {response.minutes} minutes left" if response.minutes is not None else ""
    print(f"Queued on {tasks.redis.hget(key, 'backend')}{balance}")


def restore(job_id: str, job: dict[str, str], user_id: str) -> None:
    key = tasks.job_key(job_id)
    with tasks.redis.pipeline() as pipe:
        pipe.delete(key)
        pipe.hset(key, mapping=job)
        pipe.expire(key, tasks.JOB_TTL)
        pipe.execute()
    job_limits.release(tasks.redis, job_id, user_id)
    tasks.record_job(job_id)


def refusal_text(error: Exception) -> str:
    if isinstance(error, HTTPException):
        detail = error.detail
        if isinstance(detail, dict):
            detail = detail.get("message", json.dumps(detail))
        return f"{detail} (HTTP {error.status_code})"
    return str(error)
