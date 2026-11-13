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

from app import announcements, controls, video_sites
from app.admin import announce, jobs, limits, users
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


def _version(args: dict, name: str) -> int | None:
    return None if args.get(name) is None else _whole(args, name)


def _announcement(args: dict) -> dict:
    """The announcement to publish, checked, with its times fixed now, so
    that the plan confirmed is the one applied."""
    texts = args.get("texts")
    platforms = args.get("platforms") or []
    if not isinstance(texts, dict) or not isinstance(platforms, list):
        raise AdminError("texts must be an object, and platforms a list")
    dismissible = args.get("dismissible")
    if dismissible not in (None, True, False):
        raise AdminError("dismissible must be true, false, or left out")
    start = _text(args, "start", required=False, length=16)
    announcement_id = _text(args, "id", required=False, length=64)
    if announcement_id is None:
        if args.get("replace") is True:
            raise AdminError("Replacing needs the announcement's ID")
        # From the English title, with a number added if it is taken; fixed
        # here, so the review shows the ID that is published.
        english = texts.get("en") if isinstance(texts.get("en"), dict) else {}
        title = english.get("title") if isinstance(english.get("title"), str) else ""
        announcement_id = announce.free_id(announcements.id_from_title(title))
    item = announce.build(
        announcement_id,
        texts,
        _text(args, "severity", required=False, length=16) or "info",
        _text(args, "link_url", required=False, length=announcements.URL_LENGTH),
        platforms,
        _version(args, "min_version"),
        _version(args, "max_version"),
        parse_duration(start, "start") if start else None,
        _duration(args),
        dismissible,
    )
    return {"announcement": item, "replace": args.get("replace") is True}


def _announcement_id(args: dict) -> dict:
    return {"id": _text(args, "id", length=64)}


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
    "announce.add": Action(
        "Publish an announcement",
        _announcement,
        lambda args: announce.add_plan(args["announcement"], args["replace"]),
        lambda args, actor, reason: announce.add(args["announcement"], args["replace"], actor, reason),
        serious=True,
    ),
    "announce.end": Action(
        "End an announcement",
        _announcement_id,
        lambda args: announce.end_plan(args["id"]),
        lambda args, actor, reason: announce.end(args["id"], actor, reason),
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
