"""The remote job limit for OUTSOURCE_MAX_CONCURRENCY=auto.

auto = the provider's max workers x the requests one worker takes at once.

The remote worker reads max workers from the provider (RunPod's endpoint
settings) at startup and every REFRESH_INTERVAL. Requests per worker is the
worker's batch size, which the handler reports with every result, because it
depends on the GPU RunPod assigns. Both are kept in Redis, so the API never
calls the provider and all processes use the same limit.
"""

import logging
import time

from redis import Redis

from app.config import settings


logger = logging.getLogger(__name__)

CAPACITY_KEY = "outsource:remote:capacity"
REFRESH_INTERVAL = 10 * 60
# Requests per worker until a worker has reported: a 16 GB GPU fits about 12.
DEFAULT_JOBS_PER_WORKER = 12
# The limit when max workers cannot be read: the default of
# OUTSOURCE_MAX_CONCURRENCY.
FALLBACK_LIMIT = 4
# Upper bound for remote-worker threads with auto: a 24 GB GPU fits about 20.
MAX_JOBS_PER_WORKER = 24
MAX_THREADS = 64


def _read(redis: Redis) -> tuple[int | None, int]:
    info = redis.hgetall(CAPACITY_KEY)
    try:
        workers = int(info["workers_max"]) if info.get("workers_max") else None
    except ValueError:
        workers = None
    try:
        per_worker = int(info.get("jobs_per_worker") or DEFAULT_JOBS_PER_WORKER)
    except ValueError:
        per_worker = DEFAULT_JOBS_PER_WORKER
    return workers, max(1, per_worker)


def remote_job_limit(redis: Redis) -> int:
    """The most jobs assigned to the remote backend at once."""
    configured = settings.outsource.max_concurrency
    if configured:
        return configured
    workers, per_worker = _read(redis)
    if not workers:
        return FALLBACK_LIMIT
    return workers * per_worker


def refresh_max_workers(redis: Redis, provider) -> int | None:
    """Read the provider's max workers into Redis. Keeps the last known value
    if the provider cannot be asked."""
    workers = provider.max_workers()
    if workers:
        redis.hset(CAPACITY_KEY, mapping={"workers_max": workers, "updated_at": time.time()})
    elif not redis.hget(CAPACITY_KEY, "workers_max"):
        logger.warning(
            "OUTSOURCE_MAX_CONCURRENCY=auto: max workers unknown, allowing %d remote jobs",
            FALLBACK_LIMIT,
        )
    return workers


def record_jobs_per_worker(redis: Redis, jobs: object) -> None:
    """Store the requests per worker reported by a remote handler."""
    if isinstance(jobs, int) and not isinstance(jobs, bool) and jobs > 0:
        redis.hset(CAPACITY_KEY, "jobs_per_worker", min(jobs, MAX_JOBS_PER_WORKER))


def remote_worker_threads(redis: Redis) -> int:
    """remote-worker threads: enough for the remote job limit. With auto, the
    limit can grow after startup (a larger GPU, or more max workers), so this
    leaves room for the largest likely value."""
    configured = settings.outsource.max_concurrency
    if configured:
        return configured
    workers, _ = _read(redis)
    if not workers:
        return FALLBACK_LIMIT
    return min(MAX_THREADS, workers * MAX_JOBS_PER_WORKER)
