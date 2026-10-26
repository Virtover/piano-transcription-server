"""`limits`: temporary controls that apply at once, without a restart. The
API reads them on every submission; see app.controls. seconds, in the
functions below, makes a control expire on its own; None keeps it until it
is removed."""

from app import controls, video_sites
from app.admin.common import AdminError, audit, billing_enabled, find_user, parse_duration, print_json, span
from app.admin.status import render_video_sites
from app.config import settings
from app.worker import tasks


# --- Data ---------------------------------------------------------------------

def current() -> dict:
    """The active controls. expires_in is in seconds, None until removed."""
    redis = tasks.redis
    paused = controls.paused(redis)
    links_off = controls.links_off(redis)
    per_user = controls.per_user_limit_override(redis)
    blocked = []
    for user_id, control in sorted(controls.blocked_users(redis).items()):
        emails = tasks.billing.read("SELECT email FROM users WHERE user_id = ?", (user_id,))
        blocked.append(
            {
                "user_id": user_id,
                "email": emails[0]["email"] if emails else None,
                "reason": control.value or None,
                "expires_in": control.expires_in,
            }
        )
    return {
        "paused": {"message": paused.value, "expires_in": paused.expires_in} if paused else None,
        "links_off": {"message": links_off.value, "expires_in": links_off.expires_in} if links_off else None,
        "per_user_limit": {"limit": int(per_user.value), "expires_in": per_user.expires_in} if per_user else None,
        "configured_per_user_limit": settings.limits.max_active_jobs_per_user,
        "blocked": blocked,
        "video_sites": video_sites.state(),
    }


def pause(message: str | None, seconds: float | None, actor: str, reason: str | None = None) -> dict:
    """Refuse new submissions with message; running and queued jobs continue."""
    message = message or controls.DEFAULT_PAUSE_MESSAGE
    controls.pause(tasks.redis, message, seconds)
    audit(actor, "limits.pause", detail=f"{message}{lasting(seconds)}", reason=reason)
    return {"message": message, "expires_in": seconds}


def resume(actor: str, reason: str | None = None) -> dict:
    changed = controls.resume(tasks.redis)
    audit(actor, "limits.resume", reason=reason)
    return {"changed": changed}


def links_off(message: str | None, seconds: float | None, actor: str, reason: str | None = None) -> dict:
    """Refuse video links and their cost; uploads are still accepted."""
    message = message or controls.DEFAULT_LINKS_OFF_MESSAGE
    controls.turn_links_off(tasks.redis, message, seconds)
    audit(actor, "limits.links_off", detail=lasting(seconds).strip(), reason=reason)
    return {"message": message, "expires_in": seconds}


def links_on(actor: str, reason: str | None = None) -> dict:
    changed = controls.turn_links_on(tasks.redis)
    audit(actor, "limits.links_on", reason=reason)
    return {"changed": changed}


def reset_video_sites(actor: str, reason: str | None = None) -> dict:
    """Accept links again after a video site refused this server, before its
    cooldown ends, and start the next cooldown from VIDEO_SITE_COOLDOWN."""
    changed = video_sites.reset()
    audit(actor, "limits.sites_reset", reason=reason)
    return {"changed": changed}


def block(who: str, seconds: float | None, actor: str, reason: str | None = None) -> dict:
    """Stop a user from starting transcriptions. The reason is kept with the
    block, for administrators; the user never sees it."""
    user = find_user(who)
    controls.block(tasks.redis, user["user_id"], reason, seconds)
    audit(actor, "limits.block", user["user_id"], lasting(seconds).strip(), reason)
    return {"user_id": user["user_id"], "email": user["email"], "expires_in": seconds}


def unblock(who: str, actor: str, reason: str | None = None) -> dict:
    user = find_user(who)
    changed = controls.unblock(tasks.redis, user["user_id"])
    audit(actor, "limits.unblock", user["user_id"], reason=reason)
    return {"user_id": user["user_id"], "email": user["email"], "changed": changed}


def set_per_user_limit(limit: int | None, seconds: float | None, actor: str, reason: str | None = None) -> dict:
    """Replace MAX_ACTIVE_JOBS_PER_USER for a while, or go back to it with
    limit None. changed is False if there was nothing to reset."""
    if not billing_enabled():
        raise AdminError("The per-user limit applies only with billing on; with BILLING_PROVIDER=none only MAX_ACTIVE_JOBS does")
    if limit is None:
        changed = controls.reset_per_user_limit(tasks.redis)
        audit(actor, "limits.per_user", detail="default", reason=reason)
        return {"limit": settings.limits.max_active_jobs_per_user, "expires_in": None, "changed": changed}
    if limit < 1:
        raise AdminError("Give the limit as a whole number of 1 or more, or `default`")
    controls.set_per_user_limit(tasks.redis, limit, seconds)
    audit(actor, "limits.per_user", detail=f"{limit}{lasting(seconds)}", reason=reason)
    return {"limit": limit, "expires_in": seconds, "changed": True}


# --- Command line -------------------------------------------------------------

def lasting(seconds: float | None) -> str:
    return f" for {span(seconds)}" if seconds else " until removed"


def run_show(args) -> None:
    data = current()
    if args.json:
        print_json(data)
        return
    paused, links = data["paused"], data["links_off"]
    print(f"Submissions:     {'paused' + lasting(paused['expires_in']) + ': ' + paused['message'] if paused else 'accepted'}")
    print(f"Video links:     {'off' + lasting(links['expires_in']) + ': ' + links['message'] if links else 'accepted'}")
    configured = data["configured_per_user_limit"]
    per_user = data["per_user_limit"]
    if per_user:
        print(f"Per-user limit:  {per_user['limit']}{lasting(per_user['expires_in'])} (MAX_ACTIVE_JOBS_PER_USER is {configured})")
    else:
        print(f"Per-user limit:  {configured} (MAX_ACTIVE_JOBS_PER_USER)")
    print(f"Video sites:     {render_video_sites(data['video_sites'])}")
    print(f"Blocked users:   {len(data['blocked']) or 'none'}")
    for user in data["blocked"]:
        print(f"  {user['email'] or user['user_id']}{lasting(user['expires_in'])}: {user['reason'] or 'no reason given'}")


def run_pause(args) -> None:
    result = pause(args.message, parse_duration(args.for_, "--for"), args.by, args.reason)
    print(f"New submissions are paused{lasting(result['expires_in'])}; running and queued jobs continue")


def run_resume(args) -> None:
    result = resume(args.by, args.reason)
    print("Submissions are accepted again" if result["changed"] else "Submissions were not paused")


def run_links(args) -> None:
    if args.state == "off":
        result = links_off(args.message, parse_duration(args.for_, "--for"), args.by, args.reason)
        print(f"Video links are refused{lasting(result['expires_in'])}; uploads are still accepted")
    else:
        result = links_on(args.by, args.reason)
        print("Video links are accepted again" if result["changed"] else "Video links were not off")


def run_sites(args) -> None:
    result = reset_video_sites(args.by, args.reason)
    print(
        "Video links are accepted again; the next refusal starts a new cooldown"
        if result["changed"]
        else "No video site was refusing this server"
    )


def run_block(args) -> None:
    result = block(args.user, parse_duration(args.for_, "--for"), args.by, args.reason)
    print(f"{result['email'] or result['user_id']} cannot start transcriptions{lasting(result['expires_in'])}; running jobs continue")


def run_unblock(args) -> None:
    result = unblock(args.user, args.by, args.reason)
    label = result["email"] or result["user_id"]
    print(f"{label} can start transcriptions again" if result["changed"] else f"{label} was not blocked")


def run_per_user(args) -> None:
    if args.limit == "default":
        result = set_per_user_limit(None, None, args.by, args.reason)
        print(
            f"The per-user limit is back to {result['limit']}"
            if result["changed"]
            else f"The per-user limit was already {result['limit']}"
        )
        return
    if not args.limit.isdigit():
        raise AdminError("Give the limit as a whole number of 1 or more, or `default`")
    result = set_per_user_limit(int(args.limit), parse_duration(args.for_, "--for"), args.by, args.reason)
    print(f"Each user can have {result['limit']} transcriptions in progress{lasting(result['expires_in'])}; running jobs are not stopped")
