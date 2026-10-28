"""`limits`: temporary controls that apply at once, without a restart. The
API reads them on every submission; see app.controls. seconds, in the
functions below, makes a control expire on its own; None keeps it until it
is removed."""

from app import controls, video_sites
from app.admin.common import (
    AdminError,
    ask_reason,
    audit,
    billing_enabled,
    check_reason,
    ensure_audit_log,
    find_user,
    finish,
    parse_duration,
    print_json,
    proceed,
    span,
)
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
        "video_sites": video_sites.states(),
    }


def pause(message: str | None, seconds: float | None, actor: str, reason: str | None = None) -> dict:
    """Refuse new submissions with message; running and queued jobs continue."""
    reason = check_reason(reason)
    ensure_audit_log()
    message = message or controls.DEFAULT_PAUSE_MESSAGE
    controls.pause(tasks.redis, message, seconds)
    audit(actor, "limits.pause", detail=f"{message}{lasting(seconds)}", reason=reason)
    return {"message": message, "expires_in": seconds}


def resume(actor: str, reason: str | None = None) -> dict:
    reason = check_reason(reason)
    ensure_audit_log()
    changed = controls.resume(tasks.redis)
    audit(actor, "limits.resume", reason=reason)
    return {"changed": changed}


def links_off(message: str | None, seconds: float | None, actor: str, reason: str | None = None) -> dict:
    """Refuse video links and their cost; uploads are still accepted."""
    reason = check_reason(reason)
    ensure_audit_log()
    message = message or controls.DEFAULT_LINKS_OFF_MESSAGE
    controls.turn_links_off(tasks.redis, message, seconds)
    audit(actor, "limits.links_off", detail=lasting(seconds).strip(), reason=reason)
    return {"message": message, "expires_in": seconds}


def links_on(actor: str, reason: str | None = None) -> dict:
    reason = check_reason(reason)
    ensure_audit_log()
    changed = controls.turn_links_on(tasks.redis)
    audit(actor, "limits.links_on", reason=reason)
    return {"changed": changed}


def reset_video_sites(site: str | None, actor: str, reason: str | None = None) -> dict:
    """Accept links to site, or to every site, again before a cooldown ends,
    and start the next cooldown from the first again. site is a host such as
    youtube.com or youtu.be. changed lists the sites that were refused or
    recovering."""
    reason = check_reason(reason)
    ensure_audit_log()
    name = video_sites.canonical_site(site) if site else None
    changed = video_sites.reset(name)
    audit(actor, "limits.sites_reset", detail=name or "all sites", reason=reason)
    return {"site": name, "changed": changed}


def block(who: str, seconds: float | None, actor: str, reason: str | None = None) -> dict:
    """Stop a user from starting transcriptions. The reason is kept with the
    block, for administrators; the user never sees it."""
    reason = check_reason(reason, required=True)
    user = find_user(who)
    ensure_audit_log()
    controls.block(tasks.redis, user["user_id"], reason, seconds)
    audit(actor, "limits.block", user["user_id"], lasting(seconds).strip(), reason)
    return {"user_id": user["user_id"], "email": user["email"], "expires_in": seconds}


def unblock(who: str, actor: str, reason: str | None = None) -> dict:
    reason = check_reason(reason)
    user = find_user(who)
    ensure_audit_log()
    changed = controls.unblock(tasks.redis, user["user_id"])
    audit(actor, "limits.unblock", user["user_id"], reason=reason)
    return {"user_id": user["user_id"], "email": user["email"], "changed": changed}


def check_per_user_limit(limit: int | None) -> None:
    """AdminError if the per-user limit cannot be set to limit; None is the
    setting."""
    if not billing_enabled():
        raise AdminError("The per-user limit applies only with billing on; with BILLING_PROVIDER=none only MAX_ACTIVE_JOBS does")
    if limit is not None and limit < 1:
        raise AdminError("Give the limit as a whole number of 1 or more, or `default`")


def set_per_user_limit(limit: int | None, seconds: float | None, actor: str, reason: str | None = None) -> dict:
    """Replace MAX_ACTIVE_JOBS_PER_USER for a while, or go back to it with
    limit None. changed is False if there was nothing to reset."""
    reason = check_reason(reason)
    check_per_user_limit(limit)
    ensure_audit_log()
    if limit is None:
        changed = controls.reset_per_user_limit(tasks.redis)
        audit(actor, "limits.per_user", detail="default", reason=reason)
        return {"limit": settings.limits.max_active_jobs_per_user, "expires_in": None, "changed": changed}
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
    print(f"Video sites:     {render_video_sites(data['video_sites'], indent=17)}")
    print(f"Blocked users:   {len(data['blocked']) or 'none'}")
    for user in data["blocked"]:
        print(f"  {user['email'] or user['user_id']}{lasting(user['expires_in'])}: {user['reason'] or 'no reason given'}")


def run_pause(args) -> None:
    seconds = parse_duration(args.for_, "--for")
    plan = {"message": args.message or controls.DEFAULT_PAUSE_MESSAGE, "expires_in": seconds}
    if not proceed(args, plan, would=f"New submissions will be refused{lasting(seconds)}, with: {plan['message']}"):
        return
    result = pause(args.message, seconds, args.by, args.reason)
    finish(
        args,
        result,
        f"New submissions are paused{lasting(result['expires_in'])}; running and queued jobs continue",
        ["limits", "resume"],
    )


def run_resume(args) -> None:
    if not proceed(args, {"paused": controls.paused(tasks.redis) is not None}, would="New submissions will be accepted again"):
        return
    result = resume(args.by, args.reason)
    finish(args, result, "Submissions are accepted again" if result["changed"] else "Submissions were not paused")


def run_links(args) -> None:
    if args.state == "off":
        seconds = parse_duration(args.for_, "--for")
        plan = {"message": args.message or controls.DEFAULT_LINKS_OFF_MESSAGE, "expires_in": seconds}
        if not proceed(args, plan, would=f"Video links will be refused{lasting(seconds)}, with: {plan['message']}"):
            return
        result = links_off(args.message, seconds, args.by, args.reason)
        finish(
            args,
            result,
            f"Video links are refused{lasting(result['expires_in'])}; uploads are still accepted",
            ["limits", "links", "on"],
        )
    else:
        if not proceed(args, {"links_off": controls.links_off(tasks.redis) is not None}, would="Video links will be accepted again"):
            return
        result = links_on(args.by, args.reason)
        finish(args, result, "Video links are accepted again" if result["changed"] else "Video links were not off")


def run_sites(args) -> None:
    name = video_sites.canonical_site(args.site) if args.site else None
    if not proceed(args, {"site": name}, would=f"Links to {name or 'every video site'} will be accepted again, and the next refusal starts a new cooldown"):
        return
    result = reset_video_sites(args.site, args.by, args.reason)
    if result["changed"]:
        text = f"Links to {', '.join(result['changed'])} are accepted again; the next refusal starts a new cooldown"
    else:
        text = f"{result['site'] or 'No video site'} was not refusing links"
    finish(args, result, text)


def run_block(args) -> None:
    ask_reason(args)
    seconds = parse_duration(args.for_, "--for")
    user = find_user(args.user)
    label = user["email"] or user["user_id"]
    if not proceed(args, {"user_id": user["user_id"], "email": user["email"], "expires_in": seconds}, would=f"{label} will not be able to start transcriptions{lasting(seconds)}; running jobs continue"):
        return
    result = block(args.user, seconds, args.by, args.reason)
    finish(
        args,
        result,
        f"{label} cannot start transcriptions{lasting(result['expires_in'])}; running jobs continue",
        ["limits", "unblock", args.user],
    )


def run_unblock(args) -> None:
    user = find_user(args.user)
    label = user["email"] or user["user_id"]
    if not proceed(args, {"user_id": user["user_id"], "email": user["email"]}, would=f"{label} will be able to start transcriptions again"):
        return
    result = unblock(args.user, args.by, args.reason)
    finish(args, result, f"{label} can start transcriptions again" if result["changed"] else f"{label} was not blocked")


def run_per_user(args) -> None:
    if args.limit == "default":
        check_per_user_limit(None)
        if not proceed(args, {"limit": settings.limits.max_active_jobs_per_user, "expires_in": None}, would=f"The per-user limit will go back to {settings.limits.max_active_jobs_per_user} (MAX_ACTIVE_JOBS_PER_USER)"):
            return
        result = set_per_user_limit(None, None, args.by, args.reason)
        text = (
            f"The per-user limit is back to {result['limit']}"
            if result["changed"]
            else f"The per-user limit was already {result['limit']}"
        )
        finish(args, result, text)
        return
    if not args.limit.isdigit():
        raise AdminError("Give the limit as a whole number of 1 or more, or `default`")
    limit = int(args.limit)
    check_per_user_limit(limit)
    seconds = parse_duration(args.for_, "--for")
    if not proceed(args, {"limit": limit, "expires_in": seconds}, would=f"Each user will be able to have {limit} transcriptions in progress{lasting(seconds)}"):
        return
    result = set_per_user_limit(limit, seconds, args.by, args.reason)
    finish(
        args,
        result,
        f"Each user can have {result['limit']} transcriptions in progress{lasting(result['expires_in'])}; "
        "running jobs are not stopped",
        ["limits", "per-user", "default"],
    )
