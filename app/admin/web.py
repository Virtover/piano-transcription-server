"""`web`: the admin web page's access. ADMIN_WEB_MODE sets it when the
service starts. The page can lower it, to read-only or disabled, for every
administrator; each lowering is a row the page's database role can add but
never change or delete. Only `web restore`, on the command line, removes
them. The mode in effect is the lowest of the setting and the lowerings."""

from app.admin.common import (
    AdminError,
    ask_reason,
    check_reason,
    finish,
    print_json,
    proceed,
    say,
    when,
)
from app.config import ADMIN_WEB_MODES, settings
from app.worker import tasks


# --- Data ---------------------------------------------------------------------

def rank(mode: str) -> int:
    return ADMIN_WEB_MODES.index(mode)


def effective(configured: str, lowerings: list[dict]) -> str:
    """The mode in effect: the lowest of the setting and the lowerings."""
    return min([configured, *(row["mode"] for row in lowerings)], key=rank)


def lower_modes(current: str) -> list[str]:
    """The modes current can be lowered to, nearest first."""
    return list(reversed(ADMIN_WEB_MODES[:rank(current)]))


def state(configured: str | None = None) -> dict:
    """configured is the mode the service started with; ADMIN_WEB_MODE by
    default. An unreadable database counts as disabled."""
    configured = configured or settings.admin_web.mode
    lowerings = tasks.billing.admin_web_caps()
    return {"configured": configured, "effective": effective(configured, lowerings), "lowerings": lowerings}


def lower(mode: str, current: str, actor: str, reason: str | None) -> dict:
    """Lower the page's access from current to mode, which must be lower."""
    reason = check_reason(reason, required=True)
    if mode not in lower_modes(current):
        raise AdminError(f"The admin web page is {current}; it can only be lowered, to {' or '.join(lower_modes(current)) or 'nothing'}")
    tasks.billing.lower_admin_web(mode, actor, reason)
    return {"mode": mode, "previous": current}


def restore_plan() -> dict:
    data = state()
    if not data["lowerings"]:
        raise AdminError(f"The admin web page was not lowered; it is {data['configured']}, as ADMIN_WEB_MODE sets")
    return data


def restore(actor: str, reason: str | None) -> dict:
    """Remove every lowering: the page is back to ADMIN_WEB_MODE."""
    reason = check_reason(reason, required=True)
    plan = restore_plan()
    removed = tasks.billing.restore_admin_web(actor, reason)
    return {"removed": removed, "mode": plan["configured"], "previous": plan["effective"]}


# --- Command line -------------------------------------------------------------

def run_show(args) -> None:
    data = state()
    if args.json:
        print_json(data)
        return
    configured, current = data["configured"], data["effective"]
    print(f"Admin web page:  {current}" + ("" if current == configured else f" (ADMIN_WEB_MODE is {configured})"))
    for row in data["lowerings"]:
        print(f"  lowered to {row['mode']} at {when(row['at'])} by {row['actor']}: {row['reason'] or '-'}")
    if current != configured:
        print("Only `web restore` raises it again.")


def run_restore(args) -> None:
    ask_reason(args)
    plan = restore_plan()
    say(args, f"The admin web page is {plan['effective']}; it goes back to {plan['configured']} (ADMIN_WEB_MODE).")
    for row in plan["lowerings"]:
        say(args, f"  lowered to {row['mode']} at {when(row['at'])} by {row['actor']}: {row['reason'] or '-'}")
    if not proceed(args, plan, f"Give the admin web page {plan['configured']} access again?"):
        return
    result = restore(args.by, args.reason)
    finish(args, result, f"The admin web page is {result['mode']} again, for its next request")
