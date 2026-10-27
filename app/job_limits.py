"""Limits on jobs waiting or running at once: in total, which bounds disk use
by uploads and queued work, and per user, so one account cannot take all of
it. Each limit is a Redis set of job IDs. Finished jobs are removed lazily,
the next time the set is counted, so no code path has to remember to."""

from redis import Redis

from app import controls
from app.config import settings


ALL_JOBS_KEY = "jobs:limit:all"
# A job holds its place from before its upload or video check until it ends.
ACTIVE_STATUSES = {"preparing", "uploading", "queued", "processing"}


class TooManyJobs(Exception):
    """A limit is reached. The message is written for the user."""


def user_jobs_key(user_id: str) -> str:
    return f"jobs:limit:user:{user_id}"


def _take(redis: Redis, key: str, job_id: str, limit: int, job_key) -> bool:
    """Add job_id to the set unless it already holds `limit` active jobs."""
    for other in redis.smembers(key):
        if other != job_id and redis.hget(job_key(other), "status") not in ACTIVE_STATUSES:
            redis.srem(key, other)
    with redis.pipeline() as pipe:
        pipe.sadd(key, job_id)
        pipe.scard(key)
        _, count = pipe.execute()
    if limit is not None and count > limit:
        # Two requests racing for the last place may both be refused, never both let in.
        redis.srem(key, job_id)
        return False
    return True


def acquire(redis: Redis, job_id: str, user_id: str, job_key) -> None:
    """Count a new job against the limits, or raise TooManyJobs. With
    billing off every client is the same anonymous user, so only the total
    applies."""
    limits = settings.limits
    if not _take(redis, ALL_JOBS_KEY, job_id, limits.max_active_jobs, job_key):
        raise TooManyJobs("The server is busy; try again when some transcriptions have finished")
    if settings.billing.provider == "none":
        return
    # An administrator can change the per-user limit for a while.
    per_user = controls.per_user_job_limit(redis)
    if not _take(redis, user_jobs_key(user_id), job_id, per_user, job_key):
        redis.srem(ALL_JOBS_KEY, job_id)
        raise TooManyJobs(
            f"You can have at most {per_user} transcriptions in progress; "
            "wait for one to finish"
        )


def release(redis: Redis, job_id: str, user_id: str) -> None:
    """Give the place back right away, for a job that was never queued."""
    with redis.pipeline() as pipe:
        pipe.srem(ALL_JOBS_KEY, job_id)
        pipe.srem(user_jobs_key(user_id), job_id)
        pipe.execute()


def forget_user(redis: Redis, user_id: str) -> None:
    redis.delete(user_jobs_key(user_id))
