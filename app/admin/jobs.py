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
    parse_duration,
    print_json,
    row_dict,
    shorten,
    span,
    table,
    user_label,
    when,
)
from app.auth import AuthenticatedUser
from app.billing import cancelled_cost
from app.config import settings
from app.outsource import routing
from app.worker import tasks

STATUSES = ("queued", "processing", "completed", "failed", "cancelled")
# Fields shown by `show` only as a summary: they are long.
SUMMARISED_FIELDS = {"metadata"}


# --- Data ---------------------------------------------------------------------

def list_jobs(
    status: str | None = None,
    backend: str | None = None,
    user: str | None = None,
    since: float | None = None,
    limit: int = 30,
) -> list[dict]:
    """Diagnostics records, newest first. user is an email address or user
    ID; since is in seconds."""
    conditions, parameters = [], []
    if status:
        conditions.append("j.status = ?")
        parameters.append(status)
    if backend:
        conditions.append("j.backend = ?")
        parameters.append(backend)
    if user:
        conditions.append("j.user_id = ?")
        parameters.append(find_user(user)["user_id"] if billing_enabled() else user)
    if since:
        conditions.append("j.created_at >= ?")
        parameters.append(int(time.time() - since))
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = tasks.billing.read(
        f"""SELECT j.*, u.email FROM job_diagnostics AS j LEFT JOIN users AS u ON u.user_id = j.user_id
            {where} ORDER BY j.created_at DESC LIMIT ?""",
        (*parameters, limit),
    )
    return [dict(row) for row in rows]


def job_details(job: str) -> dict:
    """Everything known about a job, named by its ID or first characters:
    its live Redis fields (None once expired), its diagnostics record with
    the full error, its reservation, and the files in its folder."""
    job_id = find_job(job)
    live = tasks.redis.hgetall(tasks.job_key(job_id))
    records = tasks.billing.read(
        "SELECT j.*, u.email FROM job_diagnostics AS j LEFT JOIN users AS u ON u.user_id = j.user_id WHERE job_id = ?",
        (job_id,),
    )
    reservations = tasks.billing.read("SELECT * FROM transcription_reservations WHERE job_id = ?", (job_id,))
    folder = Path(settings.service.data_dir) / "jobs" / job_id
    return {
        "job_id": job_id,
        "live": live or None,
        "record": row_dict(records[0]) if records else None,
        "reservation": row_dict(reservations[0]) if reservations else None,
        "files": sorted(path.name for path in folder.iterdir()) if folder.is_dir() else None,
    }


def cancel_plan(job: str, refund: bool = False) -> dict:
    """What cancelling the job would do, or AdminError if it cannot be
    cancelled. charge is None when nothing is charged."""
    job_id = find_job(job)
    fields = tasks.redis.hgetall(tasks.job_key(job_id))
    status = fields.get("status")
    if status not in {"queued", "processing"}:
        if status in {"preparing", "uploading"}:
            raise AdminError(f"The job is {status}; it is not queued yet, and its placeholder expires on its own")
        raise AdminError(f"Only queued or processing jobs can be cancelled; this one is {status or 'gone from Redis'}")
    progress = float(fields.get("progress") or 0)
    full_cost = int(fields.get("full_cost") or 0)
    billed = bool(fields.get("user_id")) and billing_enabled()
    return {
        "job_id": job_id,
        "status": status,
        "backend": fields.get("backend"),
        "progress": progress,
        "user_id": fields.get("user_id"),
        "billed": billed,
        "refund": refund,
        "full_cost": full_cost,
        "charge": cancelled_cost(full_cost, progress) if billed and not refund else None,
    }


def cancel(job: str, refund: bool, actor: str, reason: str | None = None) -> dict:
    """Cancel a queued or processing job, as its owner does, or charge
    nothing with refund. Returns the plan that was carried out."""
    plan = cancel_plan(job, refund)
    before = tasks.cancel_job(plan["job_id"], charge=not refund)
    if before not in {"queued", "processing"}:
        raise AdminError(f"The job changed meanwhile and is now {before}; nothing was done")
    audit(actor, "job.cancel", plan["job_id"], "refund" if refund else "cancellation price", reason)
    return plan


def requeue_plan(job: str, backend: routing.Backend | None = None) -> dict:
    """What queueing a failed link job again would do, or AdminError if it
    cannot run again. backend None lets routing choose."""
    job_id = find_job(job)
    fields = tasks.redis.hgetall(tasks.job_key(job_id))
    if not fields:
        raise AdminError("The job is gone from Redis (jobs are kept for an hour after they end); ask the user to submit it again")
    if fields.get("status") != "failed":
        raise AdminError(f"Only failed jobs can be queued again; this one is {fields.get('status')}")
    if fields.get("source") == "upload":
        raise AdminError("The uploaded file was deleted when the job failed; ask the user to upload it again")
    if backend == "remote" and not routing.outsourcing_enabled():
        raise AdminError("Outsourcing is off (OUTSOURCE_PROVIDER=none), so the job cannot run remotely")
    user_id = fields.get("user_id") or "anonymous"
    billed = billing_enabled() and user_id != "anonymous"
    return {
        "job_id": job_id,
        "user_id": user_id,
        "source_url": fields.get("source_url"),
        "error": fields.get("error_detail") or fields.get("error"),
        "backend": backend,
        # Charged again like a new submission: the failure gave the minutes back.
        "charged": billed,
    }


def requeue(job: str, backend: routing.Backend | None, actor: str, reason: str | None = None) -> dict:
    """Queue a failed link job again under its ID, so the user's app finds
    it, through the same check, reservation, and routing as a new link.
    On failure the job is left as it was."""
    from app.api.routes import transcriptions as transcription_routes

    plan = requeue_plan(job, backend)
    job_id, user_id = plan["job_id"], plan["user_id"]
    key = tasks.job_key(job_id)
    fields = tasks.redis.hgetall(key)
    user = None
    if plan["charged"]:
        user = AuthenticatedUser(user_id, find_user(user_id)["email"] or "")

    # Back to how a new submission starts: a placeholder that holds a place.
    tasks.redis.hset(key, "status", "preparing")
    tasks.redis.hdel(key, "error", "error_detail", "result")
    try:
        job_limits.acquire(tasks.redis, job_id, user_id, tasks.job_key)
        tasks.billing.reopen_job_record(job_id, f"requeued by {actor}" + (f": {reason}" if reason else ""))
        response = transcription_routes.submit_link(job_id, fields["source_url"], user, backend)
    except (HTTPException, job_limits.TooManyJobs, ValueError) as error:
        restore(job_id, fields, user_id)
        raise AdminError(f"Could not queue the job: {refusal_text(error)}") from error
    except BaseException:
        restore(job_id, fields, user_id)
        raise
    chosen = tasks.redis.hget(key, "backend")
    audit(actor, "job.requeue", job_id, f"backend {chosen}", reason)
    return {**plan, "backend": chosen, "minutes": response.minutes}


def restore(job_id: str, fields: dict[str, str], user_id: str) -> None:
    key = tasks.job_key(job_id)
    with tasks.redis.pipeline() as pipe:
        pipe.delete(key)
        pipe.hset(key, mapping=fields)
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


# --- Command line -------------------------------------------------------------

def run_list(args) -> None:
    rows = list_jobs(
        "failed" if args.failed else args.status,
        args.backend,
        args.user,
        parse_duration(args.since, "--since"),
        args.limit,
    )
    if args.json:
        print_json(rows)
        return
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


def run_show(args) -> None:
    details = job_details(args.job)
    if args.json:
        print_json(details)
        return
    print(f"Job {details['job_id']}")
    live = details["live"]
    if live:
        print("\nIn Redis (live):")
        for field in sorted(live):
            value = live[field]
            if field in SUMMARISED_FIELDS:
                value = f"{len(value)} characters of JSON"
            print(f"  {field}: {value}")
    else:
        print("\nNot in Redis: it ended more than an hour ago, or was never queued.")

    record = details["record"]
    if record:
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

    reservation = details["reservation"]
    if reservation:
        charged = reservation["charged_minutes"]
        print(
            f"\nReservation: {reservation['status']}, {reservation['reserved_minutes']} minutes reserved, "
            f"{charged if charged is not None else '-'} charged"
        )
    if details["files"] is not None:
        print(f"\nFiles: {', '.join(details['files']) or 'none'}")


def run_cancel(args) -> None:
    plan = cancel_plan(args.job, args.refund)
    if not plan["billed"]:
        charge_text = "nothing is charged (billing is off)"
    elif args.refund:
        charge_text = "nothing is charged (--refund)"
    else:
        charge_text = f"{plan['charge']} of {plan['full_cost']} minutes are charged, as when the user cancels"
    print(
        f"Job {plan['job_id']}: {plan['status']} on {plan['backend'] or '-'}, "
        f"{plan['progress']:.0%} done, user {plan['user_id'] or '-'}"
    )
    print(f"Cancelling it: {charge_text}.")
    if not confirm("Cancel this job?", args.yes):
        print("Nothing changed")
        return
    cancel(args.job, args.refund, args.by, args.reason)
    print("Cancelled")


def run_requeue(args) -> None:
    plan = requeue_plan(args.job, args.backend)
    print(f"Job {plan['job_id']}: failed with: {plan['error']}")
    where = f"the {plan['backend']} backend" if plan["backend"] else "the backend routing chooses"
    cost = "it is charged again like a new submission" if plan["charged"] else "nothing is charged"
    print(f"Queueing it again on {where}; {cost}.")
    if not confirm("Queue this job again?", args.yes):
        print("Nothing changed")
        return
    result = requeue(args.job, args.backend, args.by, args.reason)
    balance = f"; the user has {result['minutes']} minutes left" if result["minutes"] is not None else ""
    print(f"Queued on {result['backend']}{balance}")
