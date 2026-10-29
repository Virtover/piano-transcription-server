"""GET /metrics: what an autoscaler needs, in Prometheus's text format.

Workers are added on the queue, not on CPU: a GPU worker waiting for its
next job uses little CPU while users wait. The signals are, per queue (local
GPU workers, remote RunPod dispatch), the jobs waiting and how long the oldest
has waited. Everything comes from Redis, so every API replica answers the
same, except api_requests_in_flight, which is the replica's own.
"""

import threading
import time

from redis import Redis

from app.config import settings
from app.outsource import routing
from app.slots import VIDEO_CHECKS_KEY, in_use


# The queue names Dramatiq and status use, for each backend.
QUEUES = {"local": "default", "remote": "remote"}


class InFlight:
    """Requests this replica is answering now."""

    def __init__(self):
        self._count = 0
        self._lock = threading.Lock()

    def __enter__(self):
        with self._lock:
            self._count += 1

    def __exit__(self, *exc):
        with self._lock:
            self._count -= 1

    @property
    def count(self) -> int:
        return self._count


in_flight = InFlight()


def job_queues(redis: Redis, now: float | None = None) -> dict[str, dict]:
    """Per backend: jobs waiting, jobs running, and the oldest wait in
    seconds (0 when none waits)."""
    from app.worker.tasks import job_key

    now = time.time() if now is None else now
    result = {}
    for backend, key in routing.ACTIVE_KEYS.items():
        job_ids = list(redis.smembers(key))
        with redis.pipeline() as pipe:
            for job_id in job_ids:
                pipe.hmget(job_key(job_id), "status", "queued_at")
            fields = pipe.execute()
        waiting, running, oldest = 0, 0, 0.0
        for status, queued_at in fields:
            if status == "processing":
                running += 1
            elif status == "queued":
                waiting += 1
                try:
                    oldest = max(oldest, now - float(queued_at))
                except (TypeError, ValueError):
                    # Queued by a version that did not record the time.
                    pass
        result[backend] = {"waiting": waiting, "running": running, "oldest_wait_seconds": round(oldest, 1)}
    return result


def collect(redis: Redis) -> list[tuple[str, str, dict[str, str], float]]:
    """(name, help, labels, value) for every sample."""
    samples = []
    for backend, queue in job_queues(redis).items():
        labels = {"queue": QUEUES[backend], "backend": backend}
        samples += [
            ("piano_queue_jobs", "Jobs waiting in the queue.", labels, queue["waiting"]),
            ("piano_queue_oldest_job_age_seconds", "How long the oldest waiting job has waited.", labels,
             queue["oldest_wait_seconds"]),
            ("piano_jobs_running", "Jobs being transcribed.", labels, queue["running"]),
        ]
    capacity, _ = routing.local_capacity(redis)
    samples += [
        ("piano_local_workers", "Local GPU or CPU workers with a live heartbeat.", {},
         len(routing.local_workers(redis))),
        ("piano_local_worker_capacity", "Jobs the local workers run at once, together.", {}, capacity),
    ]
    if routing.outsourcing_enabled():
        from app.outsource.capacity import remote_job_limit

        samples.append(("piano_remote_job_limit", "Remote jobs allowed at once.", {}, remote_job_limit(redis)))
    samples.append(("piano_video_checks", "Video links being checked, by every API replica.", {},
                    in_use(redis, VIDEO_CHECKS_KEY)))
    if settings.limits.max_video_checks is not None:
        samples.append(("piano_video_checks_limit", "MAX_VIDEO_CHECKS.", {}, settings.limits.max_video_checks))
    samples.append(("piano_api_requests_in_flight", "Requests this API replica is answering.", {},
                    in_flight.count))
    return samples


def render(samples) -> str:
    lines = []
    described = set()
    for name, description, labels, value in samples:
        if name not in described:
            described.add(name)
            lines += [f"# HELP {name} {description}", f"# TYPE {name} gauge"]
        label_text = ",".join(f'{key}="{label}"' for key, label in labels.items())
        lines.append(f"{name}{{{label_text}}} {value:g}" if label_text else f"{name} {value:g}")
    return "\n".join(lines) + "\n"
