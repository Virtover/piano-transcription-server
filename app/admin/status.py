"""`status`: what the server is doing now, from Redis and the data folder."""

import json
import shutil
import time
from pathlib import Path

from app import controls, job_limits, maintenance
from app.admin.common import ago, size, span
from app.config import settings
from app.outsource import routing
from app.worker import tasks

STATUS_ORDER = ("preparing", "uploading", "queued", "processing")


def folder_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            # A job finished and removed its files while this ran.
            continue
    return total


def collect() -> dict:
    redis = tasks.redis
    jobs = dict.fromkeys(STATUS_ORDER, 0)
    for key in redis.scan_iter(match=tasks.job_key("*")):
        status = redis.hget(key, "status")
        if status in jobs:
            jobs[status] += 1
    # Counted the way job_limits counts, without removing finished jobs.
    places = sum(
        1
        for job_id in redis.smembers(job_limits.ALL_JOBS_KEY)
        if redis.hget(tasks.job_key(job_id), "status") in job_limits.ACTIVE_STATUSES
    )

    heartbeat = redis.hgetall(routing.HEARTBEAT_KEY)
    worker = None
    if heartbeat:
        average = heartbeat.get("average_job_seconds")
        worker = {
            "capacity": int(heartbeat.get("capacity") or 0),
            "average_job_seconds": float(average) if average else None,
            "updated_at": float(heartbeat.get("updated_at") or 0),
        }

    remote = {"enabled": routing.outsourcing_enabled(), "in_use": redis.scard(routing.ACTIVE_KEYS["remote"])}
    if remote["enabled"]:
        from app.outsource.capacity import remote_job_limit

        remote["limit"] = remote_job_limit(redis)

    namespace = tasks.broker.namespace
    queues = {
        queue: {
            # Messages not yet finished, including ones a worker has taken.
            "messages": redis.hlen(f"{namespace}:{queue}.msgs"),
            "dead": redis.zcard(f"{namespace}:{queue}.XQ"),
        }
        for queue in (tasks.LOCAL_QUEUE, tasks.REMOTE_QUEUE)
    }

    data_dir = Path(settings.service.data_dir)
    disk = None
    if data_dir.is_dir():
        usage = shutil.disk_usage(data_dir)
        jobs_dir = data_dir / "jobs"
        disk = {
            "path": str(data_dir),
            "jobs_bytes": folder_size(jobs_dir) if jobs_dir.is_dir() else 0,
            "free_bytes": usage.free,
            "total_bytes": usage.total,
        }

    notice = maintenance.current(redis)
    paused = controls.paused(redis)
    links_off = controls.links_off(redis)
    per_user = controls.per_user_limit_override(redis)
    return {
        "jobs": jobs,
        "places": {"used": places, "limit": settings.limits.max_active_jobs},
        "backends": {"local": redis.scard(routing.ACTIVE_KEYS["local"]), "remote": remote["in_use"]},
        "local_worker": worker,
        "remote": remote,
        "queues": queues,
        "disk": disk,
        "maintenance": notice.as_response().model_dump() if notice else None,
        "controls": {
            "paused": paused.value if paused else None,
            "links_off": links_off.value if links_off else None,
            "per_user_limit": int(per_user.value) if per_user else None,
            "blocked_users": len(controls.blocked_users(redis)),
        },
    }


def render(state: dict) -> str:
    jobs = state["jobs"]
    places = state["places"]
    lines = [
        f"Jobs          {places['used']} of {places['limit']} places used (MAX_ACTIVE_JOBS)",
        "              " + ", ".join(f"{count} {status}" for status, count in jobs.items()),
        f"              {state['backends']['local']} local, {state['backends']['remote']} remote",
    ]

    worker = state["local_worker"]
    if worker is None:
        lines.append("Local worker  no heartbeat: the worker is stopped or not consuming the local queue")
    else:
        average = worker["average_job_seconds"]
        lines.append(
            f"Local worker  {worker['capacity']} jobs at once, average job "
            f"{span(average) if average else 'unknown yet'}, heartbeat {ago(worker['updated_at'])}"
        )

    remote = state["remote"]
    if remote["enabled"]:
        lines.append(f"Remote        {remote['in_use']} of {remote['limit']} slots in use")
    else:
        lines.append("Remote        off (OUTSOURCE_PROVIDER=none)")

    queue_text = []
    for name, queue in state["queues"].items():
        text = f"{name} {queue['messages']} messages"
        if queue["dead"]:
            text += f", {queue['dead']} dead-lettered"
        queue_text.append(text)
    lines.append("Queues        " + "; ".join(queue_text))

    disk = state["disk"]
    if disk is None:
        lines.append(f"Disk          {settings.service.data_dir} does not exist")
    else:
        lines.append(
            f"Disk          jobs use {size(disk['jobs_bytes'])}; {size(disk['free_bytes'])} free "
            f"of {size(disk['total_bytes'])} on {disk['path']}"
        )

    notice = state["maintenance"]
    if notice is None:
        lines.append("Maintenance   none planned")
    else:
        reason = f" ({notice['reason']})" if notice.get("reason") else ""
        lines.append(f"Maintenance   {notice['message']}{reason}")

    active = []
    controls_state = state["controls"]
    if controls_state["paused"]:
        active.append("submissions paused")
    if controls_state["links_off"]:
        active.append("links off")
    if controls_state["per_user_limit"] is not None:
        active.append(f"per-user limit {controls_state['per_user_limit']}")
    if controls_state["blocked_users"]:
        active.append(f"{controls_state['blocked_users']} blocked users")
    lines.append("Controls      " + (", ".join(active) + " (see: limits)" if active else "none"))
    return "\n".join(lines)


def run(args) -> None:
    state = collect()
    if args.json:
        state["checked_at"] = time.time()
        print(json.dumps(state, indent=2))
    else:
        print(render(state))
