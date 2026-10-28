"""Planned restarts and shutdowns: the notice clients see, and the rules for
new jobs while one is pending.

`scripts/server.py` on the host drives them through the CLI below: schedule,
wait, drain, then restart or stop. The notice lives in Redis, so every API
process sees it. It expires on its own if the script dies, and a notice whose
time has passed is removed when the API starts.

    python -m app.maintenance schedule restart 5m [--grace 5m] [--reason TEXT]
    python -m app.maintenance schedule shutdown 10m [--back-in 2h] [--reason TEXT]
    python -m app.maintenance status
    python -m app.maintenance active-jobs
    python -m app.maintenance cancel
"""

import json
import math
import sys
import time
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel
from redis import Redis

Action = Literal["restart", "shutdown"]
ACTIONS: tuple[Action, ...] = ("restart", "shutdown")

NOTICE_KEY = "maintenance:notice"
# New jobs are refused this close to the planned time, whatever their estimate.
FINAL_WINDOW_SECONDS = 60
# How long running jobs may take to finish after the planned time.
DEFAULT_GRACE_SECONDS = 5 * 60
# The notice outlives the planned time by this much past the grace period,
# then expires, so a crashed script cannot leave it behind forever.
NOTICE_SLACK_SECONDS = 10 * 60
# Expected time for the stack to come back after a restart, for Retry-After.
RESTART_SECONDS = 60


class NoticeResponse(BaseModel):
    """A pending restart or shutdown, as clients see it. Clients should count
    down from `at` themselves; message is a ready-made English text."""

    action: Action
    at: int
    seconds_remaining: int
    message: str
    reason: str | None = None
    # Shutdowns only: when the server is expected back, if known.
    back_at: int | None = None


@dataclass(frozen=True)
class Notice:
    action: Action
    at: float
    grace_seconds: float
    reason: str | None = None
    back_at: float | None = None

    def seconds_remaining(self, now: float | None = None) -> int:
        return max(0, math.ceil(self.at - (time.time() if now is None else now)))

    def retry_after(self, now: float | None = None) -> int | None:
        """Seconds until the server should accept jobs again, or None for a
        shutdown without a known return time."""
        now = time.time() if now is None else now
        earliest = self.seconds_remaining(now) + int(self.grace_seconds) + RESTART_SECONDS
        if self.action == "restart":
            return earliest
        if self.back_at is None:
            return None
        return max(earliest, math.ceil(self.back_at - now))

    def as_response(self, now: float | None = None) -> NoticeResponse:
        remaining = self.seconds_remaining(now)
        return NoticeResponse(
            action=self.action,
            at=int(self.at),
            seconds_remaining=remaining,
            message=message(self.action, remaining),
            reason=self.reason,
            back_at=int(self.back_at) if self.back_at is not None else None,
        )


def message(action: Action, seconds_remaining: int) -> str:
    if seconds_remaining <= 0:
        return "Server is restarting" if action == "restart" else "Server is shutting down"
    if seconds_remaining < 60:
        when = f"{seconds_remaining} s"
    else:
        # Nearest minute: 64 s reads "1 m", a fresh 5-minute notice at 299 s "5 m".
        when = f"{math.floor(seconds_remaining / 60 + 0.5)} m"
    return f"Server restart in {when}" if action == "restart" else f"Server shutting down in {when}"


def schedule(
    redis: Redis,
    action: Action,
    delay_seconds: float,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    reason: str | None = None,
    back_in_seconds: float | None = None,
    now: float | None = None,
) -> Notice:
    """Announce a restart or shutdown in delay_seconds. Replaces any earlier
    notice. back_in_seconds, for shutdowns, counts from now."""
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    now = time.time() if now is None else now
    back_at = now + back_in_seconds if action == "shutdown" and back_in_seconds else None
    notice = Notice(action, now + delay_seconds, grace_seconds, reason or None, back_at)
    mapping = {"action": action, "at": notice.at, "grace_seconds": grace_seconds}
    if notice.reason:
        mapping["reason"] = notice.reason
    if back_at is not None:
        mapping["back_at"] = back_at
    with redis.pipeline() as pipe:
        pipe.delete(NOTICE_KEY)
        pipe.hset(NOTICE_KEY, mapping=mapping)
        pipe.expire(NOTICE_KEY, math.ceil(delay_seconds + grace_seconds + NOTICE_SLACK_SECONDS))
        pipe.execute()
    return notice


def current(redis: Redis) -> Notice | None:
    data = redis.hgetall(NOTICE_KEY)
    if not data or data.get("action") not in ACTIONS:
        return None
    try:
        return Notice(
            data["action"],
            float(data["at"]),
            float(data.get("grace_seconds") or DEFAULT_GRACE_SECONDS),
            data.get("reason") or None,
            float(data["back_at"]) if data.get("back_at") else None,
        )
    except (KeyError, ValueError):
        return None


def cancel(redis: Redis) -> bool:
    return bool(redis.delete(NOTICE_KEY))


def clear_past(redis: Redis, now: float | None = None) -> bool:
    """Remove a notice whose time has passed. Called at API startup: the
    restart or shutdown it announced has happened."""
    notice = current(redis)
    if notice is not None and notice.seconds_remaining(now) == 0:
        return cancel(redis)
    return False


def refusal(redis: Redis, estimated_seconds: float, now: float | None = None) -> Notice | None:
    """The pending notice that a new job of estimated_seconds would not
    finish before, or None if the job may start."""
    notice = current(redis)
    if notice is None:
        return None
    remaining = notice.seconds_remaining(now)
    if remaining <= FINAL_WINDOW_SECONDS or estimated_seconds > remaining:
        return notice
    return None


def processing_jobs(redis: Redis) -> list[str]:
    """Jobs a restart or shutdown would interrupt. Queued jobs survive both."""
    from app.worker.tasks import job_key

    jobs = []
    for key in redis.scan_iter(match=job_key("*")):
        if redis.hget(key, "status") == "processing":
            jobs.append(key.removeprefix(job_key("")))
    return jobs


def main(arguments: list[str]) -> int:
    import argparse

    from app.config import duration_seconds, settings

    parser = argparse.ArgumentParser(prog="python -m app.maintenance")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("schedule", help="announce a restart or shutdown")
    plan.add_argument("action", choices=ACTIONS)
    plan.add_argument("delay", help="time until it happens, for example 5m or 30s")
    plan.add_argument("--grace", default=f"{DEFAULT_GRACE_SECONDS}s", help="time running jobs may take to finish")
    plan.add_argument("--reason", help="optional text shown to clients")
    plan.add_argument("--back-in", help="shutdowns: when the server is expected back, counted from now")
    commands.add_parser("status", help="print the pending notice as JSON")
    commands.add_parser("active-jobs", help="print the number of processing jobs")
    commands.add_parser("cancel", help="remove the notice")
    args = parser.parse_args(arguments)

    redis = Redis.from_url(settings.service.redis_url, decode_responses=True, **settings.service.redis_options())
    if args.command == "schedule":
        try:
            delay = duration_seconds(args.delay, "delay", allow_zero=True)
            grace = duration_seconds(args.grace, "--grace", allow_zero=True)
            back_in = duration_seconds(args.back_in, "--back-in") if args.back_in else None
        except ValueError as error:
            parser.error(str(error))
        if back_in is not None and args.action != "shutdown":
            parser.error("--back-in is only for shutdowns")
        notice = schedule(redis, args.action, delay, grace, args.reason, back_in)
        print(notice.as_response().model_dump_json())
    elif args.command == "status":
        notice = current(redis)
        print(notice.as_response().model_dump_json() if notice else json.dumps(None))
    elif args.command == "active-jobs":
        print(len(processing_jobs(redis)))
    elif args.command == "cancel":
        print("cancelled" if cancel(redis) else "nothing was scheduled")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
