"""`status`: what the server is doing now, from Redis and the data folder."""

import json
import platform
import shutil
import time
from pathlib import Path

from app import backups, controls, job_limits, leader, maintenance, metrics, video_sites
from app.admin import web
from app.admin.common import ago, size, span
from app.config import settings
from app.outsource import routing
from app.slots import VIDEO_CHECKS_KEY, in_use
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


def in_docker_desktop_vm() -> bool:
    """Docker Desktop runs containers in a virtual machine (WSL 2 on Windows,
    LinuxKit on macOS) whose disk can grow up to its limit, not to what the
    host's drive has free."""
    release = platform.release().lower()
    return "microsoft" in release or "linuxkit" in release


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

    workers = routing.local_workers(redis)
    capacity, average = routing.local_capacity(redis)

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
            "virtual_disk": in_docker_desktop_vm(),
        }

    local_backups = backups.local_backups(settings.billing.backup_dir)
    backup_state = {
        "count": len(local_backups),
        "newest_at": max(local_backups) if local_backups else None,
        "offsite": backups.offsite_status(tasks.billing),
    }

    notice = maintenance.current(redis)
    paused = controls.paused(redis)
    links_off = controls.links_off(redis)
    per_user = controls.per_user_limit_override(redis)
    return {
        "jobs": jobs,
        "places": {"used": places, "limit": settings.limits.max_active_jobs},
        "backends": {"local": redis.scard(routing.ACTIVE_KEYS["local"]), "remote": remote["in_use"]},
        # All local workers together, or None without a heartbeat.
        "local_worker": {
            "count": len(workers),
            "capacity": capacity,
            "average_job_seconds": average if any(worker["average_job_seconds"] for worker in workers) else None,
            "updated_at": max(worker["updated_at"] for worker in workers),
        } if workers else None,
        "local_workers": workers,
        "api_leader": leader.current_leader(redis),
        "waiting": metrics.job_queues(redis),
        "video_checks": {"in_use": in_use(redis, VIDEO_CHECKS_KEY), "limit": settings.limits.max_video_checks},
        "remote": remote,
        "queues": queues,
        "disk": disk,
        "backups": backup_state,
        "maintenance": notice.as_response().model_dump() if notice else None,
        "video_sites": video_sites.states(),
        "controls": {
            "paused": paused.value if paused else None,
            "links_off": links_off.value if links_off else None,
            "per_user_limit": int(per_user.value) if per_user else None,
            "blocked_users": len(controls.blocked_users(redis)),
        },
        "admin_web": web.state(),
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
        count = f"{worker['count']} workers, " if worker["count"] > 1 else ""
        lines.append(
            f"Local worker  {count}{worker['capacity']} jobs at once, average job "
            f"{span(average) if average else 'unknown yet'}, heartbeat {ago(worker['updated_at'])}"
        )
        if worker["count"] > 1:
            for each in state["local_workers"]:
                each_average = each["average_job_seconds"]
                lines.append(
                    f"              {each['id']}: {each['capacity']} at once, average "
                    f"{span(each_average) if each_average else 'unknown yet'}, heartbeat {ago(each['updated_at'])}"
                )

    api_leader = state["api_leader"]
    lines.append(
        f"API leader    {api_leader}: runs backups and the abandoned job watcher"
        if api_leader
        else "API leader    none: no API is running, or it lost Redis"
    )

    checks = state["video_checks"]
    limit = "unlimited" if checks["limit"] is None else checks["limit"]
    lines.append(f"Video checks  {checks['in_use']} of {limit} at once, by every API (MAX_VIDEO_CHECKS)")

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
    waits = []
    for backend, queue in state["waiting"].items():
        text = f"{backend} {queue['waiting']}"
        if queue["waiting"] and queue["oldest_wait_seconds"]:
            text += f", the oldest for {span(queue['oldest_wait_seconds'])}"
        waits.append(text)
    lines.append("Waiting       " + "; ".join(waits) + " (jobs queued, not running yet)")

    disk = state["disk"]
    if disk is None:
        lines.append(f"Disk          {settings.service.data_dir} does not exist")
    else:
        lines.append(
            f"Disk          jobs use {size(disk['jobs_bytes'])}; {size(disk['free_bytes'])} free "
            f"of {size(disk['total_bytes'])} on {disk['path']}"
        )
        if disk["virtual_disk"]:
            lines.append("              Docker Desktop's virtual disk: its limit, not the host's drive, which may have less free")

    backup_state = state["backups"]
    if settings.billing.backups == "provider":
        text = "left to the database's provider (BILLING_BACKUPS=provider)"
    elif backup_state["newest_at"] is None:
        text = "none yet"
    else:
        text = f"{backup_state['count']} local, newest {ago(backup_state['newest_at'])}"
    offsite = backup_state["offsite"]
    if offsite is None:
        text += "; off-site off"
    elif offsite["failed_at"]:
        text += f"; off-site copy FAILED {ago(offsite['failed_at'])}: {offsite['error']}"
        if offsite["succeeded_at"]:
            text += f" (last success {ago(offsite['succeeded_at'])})"
    elif offsite["succeeded_at"]:
        text += f"; off-site copy {ago(offsite['succeeded_at'])}"
    else:
        text += "; off-site: no copy yet"
    lines.append("Backups       " + text)

    notice = state["maintenance"]
    if notice is None:
        lines.append("Maintenance   none planned")
    else:
        reason = f" ({notice['reason']})" if notice.get("reason") else ""
        lines.append(f"Maintenance   {notice['message']}{reason}")

    lines.append("Video sites   " + render_video_sites(state["video_sites"], indent=14))

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
    web = state["admin_web"]
    lowered = f", lowered from {web['configured']} (see: web)" if web["effective"] != web["configured"] else ""
    lines.append(f"Admin web     {web['effective']}{lowered}")
    return "\n".join(lines)


def render_video_sites(sites: list[dict], indent: int) -> str:
    """One line per site with something to show, from video_sites.states()."""
    lines = []
    for site in sites:
        refused = site["refused"]
        if refused is not None:
            left = f" for {span(refused['expires_in'])}" if refused["expires_in"] else ""
            if refused["kind"] == "limited":
                why, in_a_row = "it refused this server", site["incidents_in_a_row"]
            else:
                why, in_a_row = "it is not answering", site["timeout_incidents_in_a_row"]
            text = f"LINKS REFUSED{left}: {why} ({refused['reason']}), incident {in_a_row} in a row"
        elif site["recovering"]:
            text = "recovering: one link at a time until one is read"
        elif site["downloads"] or site["last_incident_at"] is not None:
            text = "answering"
        else:
            continue
        if site["downloads"]:
            text += f"; {site['downloads']} downloading"
        if site["last_incident_at"] is not None:
            text += f"; incidents so far: {site['incidents']}, last {ago(site['last_incident_at'])}"
        lines.append(f"{site['site']}: {text}")
    return ("\n" + " " * indent).join(lines) or "all answering"


def run(args) -> None:
    state = collect()
    if args.json:
        state["checked_at"] = time.time()
        print(json.dumps(state, indent=2))
    else:
        print(render(state))
