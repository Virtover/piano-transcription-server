"""Temporary controls an administrator sets with `python -m app.admin limits`,
without editing .env or restarting: pausing new submissions, blocking a user,
a different per-user job limit, and turning video links off. Each is a Redis
key that the API reads on every submission, and each can expire on its own.
Running jobs are never touched."""

from dataclasses import dataclass

from redis import Redis

from app.config import settings

PAUSE_KEY = "controls:pause"
LINKS_OFF_KEY = "controls:links_off"
PER_USER_LIMIT_KEY = "controls:per_user_limit"
BLOCKED_PREFIX = "controls:blocked:"

DEFAULT_PAUSE_MESSAGE = "New transcriptions are paused; try again later"
DEFAULT_LINKS_OFF_MESSAGE = "Video links are not accepted at the moment; upload the file instead"
# The reason for a block is for administrators; users see only this.
BLOCKED_MESSAGE = "This account cannot start transcriptions"


@dataclass(frozen=True)
class Control:
    value: str
    # Seconds until it expires on its own, or None if it lasts until removed.
    expires_in: int | None = None


def _set(redis: Redis, key: str, value: str, seconds: float | None) -> None:
    if seconds:
        redis.set(key, value, ex=max(1, round(seconds)))
    else:
        redis.set(key, value)


def _get(redis: Redis, key: str) -> Control | None:
    with redis.pipeline() as pipe:
        pipe.get(key)
        pipe.ttl(key)
        value, ttl = pipe.execute()
    if value is None:
        return None
    return Control(value, ttl if ttl and ttl > 0 else None)


def pause(redis: Redis, message: str | None = None, seconds: float | None = None) -> None:
    _set(redis, PAUSE_KEY, message or DEFAULT_PAUSE_MESSAGE, seconds)


def resume(redis: Redis) -> bool:
    return bool(redis.delete(PAUSE_KEY))


def paused(redis: Redis) -> Control | None:
    return _get(redis, PAUSE_KEY)


def turn_links_off(redis: Redis, message: str | None = None, seconds: float | None = None) -> None:
    _set(redis, LINKS_OFF_KEY, message or DEFAULT_LINKS_OFF_MESSAGE, seconds)


def turn_links_on(redis: Redis) -> bool:
    return bool(redis.delete(LINKS_OFF_KEY))


def links_off(redis: Redis) -> Control | None:
    return _get(redis, LINKS_OFF_KEY)


def block(redis: Redis, user_id: str, reason: str | None = None, seconds: float | None = None) -> None:
    _set(redis, BLOCKED_PREFIX + user_id, reason or "", seconds)


def unblock(redis: Redis, user_id: str) -> bool:
    return bool(redis.delete(BLOCKED_PREFIX + user_id))


def blocked(redis: Redis, user_id: str) -> Control | None:
    return _get(redis, BLOCKED_PREFIX + user_id)


def blocked_users(redis: Redis) -> dict[str, Control]:
    users = {}
    for key in redis.scan_iter(match=BLOCKED_PREFIX + "*"):
        control = _get(redis, key)
        if control is not None:
            users[key.removeprefix(BLOCKED_PREFIX)] = control
    return users


def set_per_user_limit(redis: Redis, limit: int, seconds: float | None = None) -> None:
    _set(redis, PER_USER_LIMIT_KEY, str(limit), seconds)


def reset_per_user_limit(redis: Redis) -> bool:
    return bool(redis.delete(PER_USER_LIMIT_KEY))


def per_user_limit_override(redis: Redis) -> Control | None:
    control = _get(redis, PER_USER_LIMIT_KEY)
    return control if control is not None and control.value.isdigit() else None


def per_user_job_limit(redis: Redis) -> int:
    """MAX_ACTIVE_JOBS_PER_USER, or the administrator's value while one is set."""
    control = per_user_limit_override(redis)
    return int(control.value) if control is not None else settings.limits.max_active_jobs_per_user


def refusal(redis: Redis, user_id: str, link: bool, submission: bool = True) -> tuple[int, str, int | None] | None:
    """(HTTP status, message for the user, seconds until it ends if known)
    if a control stops this request: a blocked user, paused submissions, or
    a video link while links are off. Asking a cost is not a submission, so
    a pause does not stop it."""
    if user_id != "anonymous" and blocked(redis, user_id) is not None:
        return 403, BLOCKED_MESSAGE, None
    if submission and (control := paused(redis)) is not None:
        return 503, control.value, control.expires_in
    if link and (control := links_off(redis)) is not None:
        return 503, control.value, control.expires_in
    return None
