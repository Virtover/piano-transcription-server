"""`limits`: temporary controls that apply at once, without a restart. The
API reads them on every submission; see app.controls."""

from app import controls
from app.admin.common import AdminError, audit, billing_enabled, find_user, span
from app.config import duration_seconds, settings
from app.worker import tasks


def expiry(args) -> float | None:
    if not getattr(args, "for_", None):
        return None
    try:
        return duration_seconds(args.for_, "--for")
    except ValueError as error:
        raise AdminError(str(error)) from error


def lasting(seconds: float | None) -> str:
    return f" for {span(seconds)}" if seconds else " until removed"


def show(args) -> None:
    redis = tasks.redis
    paused = controls.paused(redis)
    links_off = controls.links_off(redis)
    per_user = controls.per_user_limit_override(redis)
    print(f"Submissions:     {'paused' + lasting(paused.expires_in) + ': ' + paused.value if paused else 'accepted'}")
    print(f"Video links:     {'off' + lasting(links_off.expires_in) + ': ' + links_off.value if links_off else 'accepted'}")
    configured = settings.limits.max_active_jobs_per_user
    if per_user:
        print(f"Per-user limit:  {per_user.value}{lasting(per_user.expires_in)} (MAX_ACTIVE_JOBS_PER_USER is {configured})")
    else:
        print(f"Per-user limit:  {configured} (MAX_ACTIVE_JOBS_PER_USER)")
    blocked = controls.blocked_users(redis)
    print(f"Blocked users:   {len(blocked) or 'none'}")
    for user_id, control in sorted(blocked.items()):
        emails = tasks.billing.read("SELECT email FROM users WHERE user_id = ?", (user_id,))
        label = emails[0]["email"] if emails and emails[0]["email"] else user_id
        print(f"  {label}{lasting(control.expires_in)}: {control.value or 'no reason given'}")


def pause(args) -> None:
    seconds = expiry(args)
    controls.pause(tasks.redis, args.message, seconds)
    audit(args, "limits.pause", detail=f"{args.message or controls.DEFAULT_PAUSE_MESSAGE}{lasting(seconds)}")
    print(f"New submissions are paused{lasting(seconds)}; running and queued jobs continue")


def resume(args) -> None:
    removed = controls.resume(tasks.redis)
    audit(args, "limits.resume")
    print("Submissions are accepted again" if removed else "Submissions were not paused")


def links(args) -> None:
    if args.state == "off":
        seconds = expiry(args)
        controls.turn_links_off(tasks.redis, args.message, seconds)
        audit(args, "limits.links_off", detail=lasting(seconds).strip())
        print(f"Video links are refused{lasting(seconds)}; uploads are still accepted")
    else:
        removed = controls.turn_links_on(tasks.redis)
        audit(args, "limits.links_on")
        print("Video links are accepted again" if removed else "Video links were not off")


def block(args) -> None:
    user = find_user(args.user)
    seconds = expiry(args)
    controls.block(tasks.redis, user["user_id"], args.reason, seconds)
    audit(args, "limits.block", user["user_id"], lasting(seconds).strip())
    print(f"{user['email'] or user['user_id']} cannot start transcriptions{lasting(seconds)}; running jobs continue")


def unblock(args) -> None:
    user = find_user(args.user)
    removed = controls.unblock(tasks.redis, user["user_id"])
    audit(args, "limits.unblock", user["user_id"])
    label = user["email"] or user["user_id"]
    print(f"{label} can start transcriptions again" if removed else f"{label} was not blocked")


def per_user(args) -> None:
    if not billing_enabled():
        raise AdminError("The per-user limit applies only with billing on; with BILLING_PROVIDER=none only MAX_ACTIVE_JOBS does")
    if args.limit == "default":
        removed = controls.reset_per_user_limit(tasks.redis)
        audit(args, "limits.per_user", detail="default")
        configured = settings.limits.max_active_jobs_per_user
        print(f"The per-user limit is back to {configured}" if removed else f"The per-user limit was already {configured}")
        return
    if not args.limit.isdigit() or int(args.limit) < 1:
        raise AdminError("Give the limit as a whole number of 1 or more, or `default`")
    seconds = expiry(args)
    controls.set_per_user_limit(tasks.redis, int(args.limit), seconds)
    audit(args, "limits.per_user", detail=f"{args.limit}{lasting(seconds)}")
    print(f"Each user can have {args.limit} transcriptions in progress{lasting(seconds)}; running jobs are not stopped")
