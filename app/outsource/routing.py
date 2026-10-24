"""Deciding whether a queued job runs locally or remotely.

Overload is the expected local wait, not VRAM: the worker thread count already
caps GPU concurrency, so overload shows up as jobs waiting. Each backend keeps a
Redis set of its active (queued or processing) job IDs, so counting is O(1).
"""

import math
import time
from typing import Literal

from redis import Redis

from app.config import settings


Backend = Literal["local", "remote"]

HEARTBEAT_KEY = "workers:local:heartbeat"
HEARTBEAT_TTL = 60
ACTIVE_KEYS: dict[str, str] = {
    "local": "jobs:active:local",
    "remote": "jobs:active:remote",
}
# Used for the wait estimate until the worker has finished a job.
DEFAULT_JOB_SECONDS = 60.0


def publish_heartbeat(redis: Redis, capacity: int, average_job_seconds: float | None) -> None:
    """Called periodically by the GPU worker. Expires if the worker stops."""
    with redis.pipeline() as pipe:
        pipe.hset(
            HEARTBEAT_KEY,
            mapping={
                "capacity": capacity,
                "average_job_seconds": average_job_seconds or "",
                "updated_at": time.time(),
            },
        )
        pipe.expire(HEARTBEAT_KEY, HEARTBEAT_TTL)
        pipe.execute()


def clear_heartbeat(redis: Redis) -> None:
    redis.delete(HEARTBEAT_KEY)


def local_capacity(redis: Redis) -> tuple[int, float]:
    """(job capacity, average job seconds). Capacity is 0 without a heartbeat."""
    heartbeat = redis.hgetall(HEARTBEAT_KEY)
    try:
        capacity = int(heartbeat.get("capacity", 0))
    except (TypeError, ValueError):
        capacity = 0
    try:
        average = float(heartbeat.get("average_job_seconds") or DEFAULT_JOB_SECONDS)
    except (TypeError, ValueError):
        average = DEFAULT_JOB_SECONDS
    return capacity, average


def estimated_local_wait(redis: Redis) -> float:
    """Seconds a new local job would wait before it starts."""
    capacity, average = local_capacity(redis)
    if capacity < 1:
        return math.inf
    active = redis.scard(ACTIVE_KEYS["local"])
    if active < capacity:
        return 0.0
    # Jobs ahead of the new one beyond the free slots, served `capacity` at a time.
    return math.ceil((active - capacity + 1) / capacity) * average


def estimated_job_seconds(redis: Redis) -> float:
    """Rough time until a new job would finish: its local wait, if the worker
    is up, plus one average job."""
    wait = estimated_local_wait(redis)
    _, average = local_capacity(redis)
    return (0.0 if math.isinf(wait) else wait) + average


def outsourcing_enabled() -> bool:
    return settings.outsource.provider != "none"


def reserve_remote_slot(redis: Redis, job_id: str) -> bool:
    """Count the job as remote if fewer than the remote job limit are
    (OUTSOURCE_MAX_CONCURRENCY, or its `auto` value)."""
    from app.outsource.capacity import remote_job_limit

    key = ACTIVE_KEYS["remote"]
    with redis.pipeline() as pipe:
        pipe.sadd(key, job_id)
        pipe.scard(key)
        _, count = pipe.execute()
    if count > remote_job_limit(redis):
        redis.srem(key, job_id)
        return False
    return True


def choose_backend(redis: Redis, job_id: str) -> Backend:
    """Pick the backend for a newly queued job and count it as active there."""
    if outsourcing_enabled() and estimated_local_wait(redis) > settings.outsource.max_wait:
        if reserve_remote_slot(redis, job_id):
            return "remote"
    redis.sadd(ACTIVE_KEYS["local"], job_id)
    return "local"


def mark_active(redis: Redis, backend: Backend, job_id: str) -> None:
    redis.sadd(ACTIVE_KEYS[backend], job_id)


def mark_finished(redis: Redis, backend: Backend, job_id: str) -> None:
    redis.srem(ACTIVE_KEYS[backend], job_id)


def move(redis: Redis, job_id: str, source: Backend, target: Backend) -> None:
    with redis.pipeline() as pipe:
        pipe.srem(ACTIVE_KEYS[source], job_id)
        pipe.sadd(ACTIVE_KEYS[target], job_id)
        pipe.execute()


def prune_active(redis: Redis, job_key) -> int:
    """Remove IDs of jobs that are no longer queued or processing, for example
    after a crash. Run periodically by the cleanup service."""
    removed = 0
    for key in ACTIVE_KEYS.values():
        for job_id in redis.sscan_iter(key):
            if redis.hget(job_key(job_id), "status") not in {"queued", "processing", "uploading", "preparing"}:
                removed += redis.srem(key, job_id)
    return removed


def clear_active(redis: Redis) -> None:
    redis.delete(*ACTIVE_KEYS.values())
