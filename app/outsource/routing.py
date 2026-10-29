"""Deciding whether a queued job runs locally or remotely.

Overload is the expected local wait, not VRAM: the worker thread count already
caps GPU concurrency, so overload shows up as jobs waiting. Each backend keeps a
Redis set of its active (queued or processing) job IDs, so counting is O(1).
"""

import math
import os
import socket
import time
from typing import Literal

from redis import Redis
from redis.exceptions import WatchError

from app.config import settings


Backend = Literal["local", "remote"]

# Each local worker process publishes its own heartbeat, a hash that expires
# unless renewed. A sorted set of worker IDs by last heartbeat finds them
# without scanning every key.
WORKERS_KEY = "workers:local"
HEARTBEAT_PREFIX = "workers:local:heartbeat:"
HEARTBEAT_TTL = 60
ACTIVE_KEYS: dict[str, str] = {
    "local": "jobs:active:local",
    "remote": "jobs:active:remote",
}
REMOTE_RUNNING_KEY = "jobs:running:remote"
# Used for the wait estimate until the worker has finished a job.
DEFAULT_JOB_SECONDS = 60.0


def worker_id() -> str:
    """This process's worker ID: its host, which names the container, and
    its process ID."""
    return f"{socket.gethostname()}:{os.getpid()}"


def heartbeat_key(worker: str) -> str:
    return f"{HEARTBEAT_PREFIX}{worker}"


def publish_heartbeat(
    redis: Redis,
    capacity: int,
    average_job_seconds: float | None,
    worker: str | None = None,
) -> None:
    """Called periodically by each GPU worker. Expires if the worker stops."""
    worker = worker or worker_id()
    now = time.time()
    with redis.pipeline() as pipe:
        pipe.hset(
            heartbeat_key(worker),
            mapping={
                "capacity": capacity,
                "average_job_seconds": average_job_seconds or "",
                "updated_at": now,
            },
        )
        pipe.expire(heartbeat_key(worker), HEARTBEAT_TTL)
        pipe.zadd(WORKERS_KEY, {worker: now})
        # Forget workers that stopped without clearing their heartbeat.
        pipe.zremrangebyscore(WORKERS_KEY, "-inf", now - HEARTBEAT_TTL)
        pipe.execute()


def clear_heartbeat(redis: Redis, worker: str | None = None) -> None:
    worker = worker or worker_id()
    with redis.pipeline() as pipe:
        pipe.delete(heartbeat_key(worker))
        pipe.zrem(WORKERS_KEY, worker)
        pipe.execute()


def local_workers(redis: Redis) -> list[dict]:
    """The local workers with a live heartbeat, oldest first. Only reads, so
    the admin web page's read-only Redis user may call it."""
    workers = redis.zrangebyscore(WORKERS_KEY, time.time() - HEARTBEAT_TTL, "+inf")
    if not workers:
        return []
    with redis.pipeline() as pipe:
        for worker in workers:
            pipe.hgetall(heartbeat_key(worker))
        heartbeats = pipe.execute()
    alive = []
    for worker, heartbeat in zip(workers, heartbeats):
        if not heartbeat:
            continue
        try:
            capacity = int(heartbeat.get("capacity") or 0)
        except (TypeError, ValueError):
            capacity = 0
        try:
            average = float(heartbeat["average_job_seconds"]) if heartbeat.get("average_job_seconds") else None
        except (TypeError, ValueError):
            average = None
        try:
            updated_at = float(heartbeat.get("updated_at") or 0)
        except (TypeError, ValueError):
            updated_at = 0.0
        alive.append({"id": worker, "capacity": capacity, "average_job_seconds": average, "updated_at": updated_at})
    return alive


def local_capacity(redis: Redis) -> tuple[int, float]:
    """(job capacity, average job seconds) of all local workers together.
    Capacity is 0 without a heartbeat. The average weighs each worker by its
    capacity, because it runs that share of the jobs."""
    workers = local_workers(redis)
    capacity = sum(worker["capacity"] for worker in workers)
    timed = [worker for worker in workers if worker["average_job_seconds"] and worker["capacity"] > 0]
    weight = sum(worker["capacity"] for worker in timed)
    if not weight:
        return capacity, DEFAULT_JOB_SECONDS
    return capacity, sum(worker["average_job_seconds"] * worker["capacity"] for worker in timed) / weight


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
    limit = remote_job_limit(redis)
    while True:
        try:
            with redis.pipeline() as pipe:
                pipe.watch(key)
                if pipe.scard(key) >= limit:
                    return False
                pipe.multi()
                pipe.sadd(key, job_id)
                pipe.execute()
                return True
        except WatchError:
            continue


def choose_backend(redis: Redis, job_id: str) -> Backend:
    """Pick the backend for a newly queued job and count it as active there."""
    mode = settings.outsource.routing
    if outsourcing_enabled() and mode.startswith("remote_first"):
        if mode == "remote_first_fallback" or reserve_remote_slot(redis, job_id):
            redis.sadd(ACTIVE_KEYS["remote"], job_id)
            return "remote"
        redis.sadd(ACTIVE_KEYS["local"], job_id)
        return "local"
    if outsourcing_enabled() and estimated_local_wait(redis) > settings.outsource.local_first_max_wait:
        if reserve_remote_slot(redis, job_id):
            return "remote"
    redis.sadd(ACTIVE_KEYS["local"], job_id)
    return "local"


def wait_for_remote_slot(redis: Redis, job_id: str, cancelled) -> bool:
    """Wait until this remote job owns a provider-concurrency slot."""
    while True:
        if cancelled():
            return False
        with redis.pipeline() as pipe:
            pipe.watch(REMOTE_RUNNING_KEY)
            if pipe.sismember(REMOTE_RUNNING_KEY, job_id):
                return True
            if pipe.scard(REMOTE_RUNNING_KEY) >= _remote_limit(redis):
                pipe.unwatch()
            else:
                pipe.multi()
                pipe.sadd(REMOTE_RUNNING_KEY, job_id)
                try:
                    pipe.execute()
                except WatchError:
                    continue
                return True
        time.sleep(0.5)


def release_remote_slot(redis: Redis, job_id: str) -> None:
    redis.srem(REMOTE_RUNNING_KEY, job_id)


def _remote_limit(redis: Redis) -> int:
    from app.outsource.capacity import remote_job_limit

    return remote_job_limit(redis)


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
    for job_id in redis.sscan_iter(REMOTE_RUNNING_KEY):
        if redis.hget(job_key(job_id), "status") != "processing":
            removed += redis.srem(REMOTE_RUNNING_KEY, job_id)
    return removed
