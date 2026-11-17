"""What the admin web page may change in read-write mode, each through the
same data functions as `python -m app.admin`, so it is checked, charged, and
written to the audit log the same way.

Each action cleans its own arguments, describes what it would do (its plan,
which changes nothing), and applies it. Queueing a job again and deleting an
account stay on the command line: the first downloads from a video site and
writes job files, which the page's service may not, and the second cannot be
undone.
"""

import secrets
from dataclasses import dataclass
from typing import Callable

from app import announcements, cluster, controls, video_sites
from app.admin import announce, jobs, limits, nodes, users
from app.admin.common import AdminError, find_user, parse_duration
from app.config import settings
from app.worker import tasks

# The most minutes one change from the page may add or take away; more is
# done on the command line.
CREDIT_LIMIT = 600
# The most minutes giving every account minutes may give each from the page.
CREDIT_ALL_LIMIT = 120
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
    # What the administrator must type to apply it, from its arguments.
    typed: Callable[[dict], str] | None = None


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


def _credit_all(args: dict) -> dict:
    minutes = _whole(args, "minutes")
    if not 0 < minutes <= CREDIT_ALL_LIMIT:
        raise AdminError(
            f"The page gives every account 1 to {CREDIT_ALL_LIMIT} minutes at once; use the command line for more"
        )
    # Chosen at review, so applying the reviewed change twice gives once.
    return {"minutes": minutes, "grant_id": secrets.token_hex(16)}


def _grant(args: dict) -> dict:
    grant = _text(args, "grant", length=64)
    if not grant.isalnum():
        raise AdminError("grant must be a grant's ID")
    return {"grant": grant}


def _message(args: dict) -> dict:
    return {"message": _text(args, "message", required=False, length=MESSAGE_LENGTH), "for": _duration(args)}


def _user(args: dict) -> dict:
    return {"user": _text(args, "user")}


def _developer(args: dict) -> dict:
    developer = args.get("developer")
    if not isinstance(developer, bool):
        raise AdminError("developer must be true or false")
    return {"user": _text(args, "user"), "developer": developer}


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


def _node_switch(args: dict) -> dict:
    if args.get("on") not in (True, False):
        raise AdminError("on must be true or false")
    return {"node": nodes.check_node(_text(args, "node", length=31)), "on": args["on"]}


def _node_task(args: dict) -> dict:
    if args.get("allowed") not in (True, False):
        raise AdminError("allowed must be true or false")
    task = _text(args, "task", length=16)
    nodes.check_tasks([task])
    return {"node": nodes.check_node(_text(args, "node", length=31)), "task": task, "allowed": args["allowed"]}


def _node_plan(args: dict) -> dict:
    config = cluster.node_config(tasks.redis, args["node"])
    return {**args, "enabled_now": config["enabled"], "tasks_now": nodes.task_list(config["tasks"])}


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
    "users.credit_all": Action(
        "Give every account minutes",
        _credit_all,
        lambda args: users.credit_all_plan(args["minutes"]),
        lambda args, actor, reason: users.credit_all(args["minutes"], actor, reason, args["grant_id"]),
        reason_required=True,
        serious=True,
        typed=lambda args: users.credit_all_phrase(args["minutes"]),
    ),
    "users.credit_all_undo": Action(
        "Undo a grant to every account",
        _grant,
        lambda args: users.credit_all_undo_plan(args["grant"]),
        lambda args, actor, reason: users.undo_credit_all(args["grant"], actor, reason),
        reason_required=True,
        serious=True,
    ),
    "user.developer": Action(
        "Make an account a developer's",
        _developer,
        lambda args: users.developer_plan(args["user"], args["developer"]),
        lambda args, actor, reason: users.set_developer(args["user"], args["developer"], actor, reason),
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
    # A home cluster's devices (docs/cluster.md). Inviting and revoking stay
    # on the command line: they write the hub's secret files.
    "nodes.switch": Action(
        "Switch a device on or off",
        _node_switch,
        _node_plan,
        lambda args, actor, reason: nodes.switch(args["node"], args["on"], actor, reason),
    ),
    "nodes.task": Action(
        "Allow or deny a task on a device",
        _node_task,
        _node_plan,
        lambda args, actor, reason: nodes.allow(args["node"], [args["task"]], args["allowed"], actor, reason),
    ),
}
