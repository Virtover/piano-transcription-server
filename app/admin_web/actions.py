"""What the admin web page may change in read-write mode, each through the
same data functions as `python -m app.admin`, so it is checked, charged, and
written to the audit log the same way.

Each action cleans its own arguments, describes what it would do (its plan,
which changes nothing), and applies it. Queueing a job again and deleting an
account stay on the command line: the first downloads from a video site and
writes job files, which the page's service may not, and the second cannot be
undone.
"""

from dataclasses import dataclass
from typing import Callable

from app import controls, video_sites
from app.admin import jobs, limits, users
from app.admin.common import AdminError, find_user, parse_duration
from app.config import settings
from app.worker import tasks

# The most minutes one change from the page may add or take away; more is
# done on the command line.
CREDIT_LIMIT = 600
MESSAGE_LENGTH = 200


@dataclass(frozen=True)
class Action:
    title: str
    clean: Callable[[dict], dict]
    plan: Callable[[dict], dict]
    apply: Callable[[dict, str, str | None], dict]
    reason_required: bool = False
    # Shown with a warning: it cannot be undone, or it affects every user.
    serious: bool = False


def _text(args: dict, name: str, required: bool = True, length: int = 320) -> str | None:
    value = args.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise AdminError(f"{name} is required")
        return None
    if not isinstance(value, str) or len(value) > length:
        raise AdminError(f"{name} must be text of at most {length} characters")
    return value.strip()


def _duration(args: dict) -> float | None:
    return parse_duration(_text(args, "for", required=False, length=16), "for")


def _whole(args: dict, name: str) -> int:
    value = args.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AdminError(f"{name} must be a whole number")
    return value


def _job(args: dict) -> dict:
    return {"job": _text(args, "job", length=64), "refund": args.get("refund") is True}


def _credit(args: dict) -> dict:
    minutes = _whole(args, "minutes")
    if abs(minutes) > CREDIT_LIMIT:
        raise AdminError(f"The page changes at most {CREDIT_LIMIT} minutes at once; use the command line for more")
    return {"user": _text(args, "user"), "minutes": minutes}


def _message(args: dict) -> dict:
    return {"message": _text(args, "message", required=False, length=MESSAGE_LENGTH), "for": _duration(args)}


def _user(args: dict) -> dict:
    return {"user": _text(args, "user")}


def _block(args: dict) -> dict:
    return {"user": _text(args, "user"), "for": _duration(args)}


def _site(args: dict) -> dict:
    return {"site": _text(args, "site", required=False, length=253)}


def _per_user(args: dict) -> dict:
    limit = args.get("limit")
    if limit is not None:
        limit = _whole(args, "limit")
    limits.check_per_user_limit(limit)
    return {"limit": limit, "for": _duration(args) if limit is not None else None}


def _user_plan(args: dict) -> dict:
    user = find_user(args["user"])
    return {"user_id": user["user_id"], "email": user["email"], "blocked_now": controls.blocked(tasks.redis, user["user_id"]) is not None}


ACTIONS: dict[str, Action] = {
    "job.cancel": Action(
        "Cancel a job",
        _job,
        lambda args: jobs.cancel_plan(args["job"], args["refund"]),
        lambda args, actor, reason: jobs.cancel(args["job"], args["refund"], actor, reason),
        reason_required=True,
        serious=True,
    ),
    "user.credit": Action(
        "Change a balance",
        _credit,
        lambda args: users.credit_plan(args["user"], args["minutes"]),
        lambda args, actor, reason: users.credit(args["user"], args["minutes"], actor, reason),
        reason_required=True,
    ),
    "limits.pause": Action(
        "Pause new transcriptions",
        _message,
        lambda args: {
            "message": args["message"] or controls.DEFAULT_PAUSE_MESSAGE,
            "expires_in": args["for"],
            "paused_now": controls.paused(tasks.redis) is not None,
        },
        lambda args, actor, reason: limits.pause(args["message"], args["for"], actor, reason),
        serious=True,
    ),
    "limits.resume": Action(
        "Accept new transcriptions",
        lambda args: {},
        lambda args: {"paused_now": controls.paused(tasks.redis) is not None},
        lambda args, actor, reason: limits.resume(actor, reason),
    ),
    "limits.links_off": Action(
        "Turn video links off",
        _message,
        lambda args: {
            "message": args["message"] or controls.DEFAULT_LINKS_OFF_MESSAGE,
            "expires_in": args["for"],
            "off_now": controls.links_off(tasks.redis) is not None,
        },
        lambda args, actor, reason: limits.links_off(args["message"], args["for"], actor, reason),
        serious=True,
    ),
    "limits.links_on": Action(
        "Accept video links",
        lambda args: {},
        lambda args: {"off_now": controls.links_off(tasks.redis) is not None},
        lambda args, actor, reason: limits.links_on(actor, reason),
    ),
    "limits.sites_reset": Action(
        "Accept a video site again",
        _site,
        lambda args: {"site": video_sites.canonical_site(args["site"]) if args["site"] else "every site"},
        lambda args, actor, reason: limits.reset_video_sites(args["site"], actor, reason),
    ),
    "limits.block": Action(
        "Block a user",
        _block,
        lambda args: {**_user_plan(args), "expires_in": args["for"]},
        lambda args, actor, reason: limits.block(args["user"], args["for"], actor, reason),
        reason_required=True,
    ),
    "limits.unblock": Action(
        "Unblock a user",
        _user,
        _user_plan,
        lambda args, actor, reason: limits.unblock(args["user"], actor, reason),
    ),
    "limits.per_user": Action(
        "Change the per-user job limit",
        _per_user,
        lambda args: {
            "limit": args["limit"] if args["limit"] is not None else settings.limits.max_active_jobs_per_user,
            "expires_in": args["for"],
            "back_to_setting": args["limit"] is None,
        },
        lambda args, actor, reason: limits.set_per_user_limit(args["limit"], args["for"], actor, reason),
    ),
}
