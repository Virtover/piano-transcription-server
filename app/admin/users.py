"""`user`: one account's balance and history, credits and debits by hand,
and account deletion. `users find`: accounts by part of their email."""

from app import controls
from app.admin.common import (
    AdminError,
    ask_reason,
    billing_enabled,
    check_reason,
    ensure_audit_log,
    find_user,
    finish,
    print_json,
    proceed,
    say,
    shorten,
    span,
    table,
    when,
)
from app.billing import period_seconds
from app.config import settings
from app.worker import tasks

RECENT = 10
FOUND = 20
SEARCH_LENGTH = 3


# --- Data ---------------------------------------------------------------------

def account(who: str) -> dict:
    """A user's balance, free minutes, block, purchases (without tokens),
    latest balance changes, reservations, and recent jobs."""
    user = find_user(who)
    user_id = user["user_id"]
    billing = tasks.billing
    reserved = [
        dict(row)
        for row in billing.read(
            "SELECT job_id, reserved_minutes FROM transcription_reservations WHERE user_id = ? AND status = 'reserved'",
            (user_id,),
        )
    ]
    held = sum(row["reserved_minutes"] for row in reserved)
    free = None
    if (settings.billing.free_minutes or 0) > 0:
        last = user["free_grant_at"] or None
        free = {"last_grant_at": last, "next_grant_at": last + period_seconds() if last else None}
    block = controls.blocked(tasks.redis, user_id)
    return {
        "user_id": user_id,
        "email": user["email"],
        "minutes": user["minutes"],
        "reserved_minutes": held,
        "available_minutes": user["minutes"] - held,
        "free_minutes": free,
        "blocked": {"reason": block.value or None, "expires_in": block.expires_in} if block else None,
        "purchases": [
            dict(row)
            for row in billing.read(
                "SELECT product_id, order_id, minutes, voided_at FROM purchases WHERE user_id = ? "
                "ORDER BY voided_at NULLS FIRST, order_id DESC",
                (user_id,),
            )
        ],
        "balance_changes": [
            dict(row)
            for row in billing.read(
                "SELECT at, kind, minutes, note FROM minute_ledger WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, RECENT),
            )
        ],
        "reservations": reserved,
        "jobs": [
            dict(row)
            for row in billing.read(
                "SELECT * FROM job_diagnostics WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, RECENT),
            )
        ],
    }


def find(text: str, limit: int = FOUND) -> list[dict]:
    """Accounts whose email address or user ID contains text, for when only
    part of it is known."""
    if not billing_enabled():
        raise AdminError("Billing is off (BILLING_PROVIDER=none), so there are no user accounts")
    text = text.strip().lower()
    if len(text) < SEARCH_LENGTH:
        raise AdminError(f"Give at least {SEARCH_LENGTH} characters to search for")
    # The text is matched as written: % and _ are not wildcards.
    pattern = "%" + text.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
    rows = tasks.billing.read(
        "SELECT user_id, email, minutes FROM users "
        "WHERE lower(email) LIKE ? ESCAPE '!' OR lower(user_id) LIKE ? ESCAPE '!' "
        "ORDER BY email, user_id LIMIT ?",
        (pattern, pattern, limit),
    )
    return [dict(row) for row in rows]


def credit_plan(who: str, minutes: int) -> dict:
    """The balance before and after adding minutes, or taking them away
    when negative."""
    user = find_user(who)
    if minutes == 0:
        raise AdminError("Give a number of minutes other than 0; a negative number takes minutes away")
    return {
        "user_id": user["user_id"],
        "email": user["email"],
        "minutes": minutes,
        "balance": user["minutes"],
        "balance_after": user["minutes"] + minutes,
    }


def credit(who: str, minutes: int, actor: str, reason: str | None = None) -> dict:
    """Add minutes, or take them away when negative. The balance change and
    its audit entry are written together, or not at all."""
    reason = check_reason(reason, required=True)
    plan = credit_plan(who, minutes)
    balance = tasks.billing.adjust_minutes(plan["user_id"], minutes, reason, actor)
    return {**plan, "balance_after": balance}


def delete_plan(who: str) -> dict:
    user = find_user(who)
    return {"user_id": user["user_id"], "email": user["email"], "minutes": user["minutes"]}


def delete(who: str, actor: str, reason: str | None = None) -> dict:
    """Delete the account and its data, as DELETE /api/account does, and
    any block on it. The audit entry is written with the billing data, by a
    hash of the account's ID."""
    reason = check_reason(reason, required=True)
    plan = delete_plan(who)
    ensure_audit_log()
    tasks.delete_account(plan["user_id"], actor, reason)
    controls.unblock(tasks.redis, plan["user_id"])
    return plan


# --- Command line -------------------------------------------------------------

def run_show(args) -> None:
    data = account(args.user)
    if args.json:
        print_json(data)
        return
    print(f"User {data['user_id']}")
    print(f"  email: {data['email'] or '-'}")
    print(
        f"  balance: {data['minutes']} minutes, {data['reserved_minutes']} reserved by running jobs, "
        f"{data['available_minutes']} available"
    )
    if data["minutes"] < 0:
        print("  The balance is negative: a refunded purchase's minutes were already used.")
    free = data["free_minutes"]
    if free is not None:
        next_grant = when(free["next_grant_at"]) if free["next_grant_at"] else "the next request"
        print(f"  last free minutes: {when(free['last_grant_at'])}, next from {next_grant}")
    block = data["blocked"]
    if block is not None:
        until = f" for {span(block['expires_in'])} more" if block["expires_in"] else ""
        print(f"  BLOCKED{until}: {block['reason'] or 'no reason given'}")

    purchases = data["purchases"]
    print(f"\nPurchases ({len(purchases)}):")
    if purchases:
        print(
            table(
                ["Product", "Order", "Minutes", "Voided"],
                [[row["product_id"], row["order_id"], row["minutes"], when(row["voided_at"])] for row in purchases],
            )
        )

    print(f"\nRecent balance changes (newest {RECENT}):")
    if data["balance_changes"]:
        print(
            table(
                ["When", "Kind", "Minutes", "Note"],
                [
                    [when(row["at"]), row["kind"], f"{row['minutes']:+d}", shorten(row["note"], 50)]
                    for row in data["balance_changes"]
                ],
            )
        )

    if data["reservations"]:
        print("\nReservations held by running jobs:")
        print(table(["Job", "Minutes"], [[row["job_id"][:8], row["reserved_minutes"]] for row in data["reservations"]]))

    print(f"\nRecent jobs (newest {RECENT}; details: jobs show <job>):")
    if data["jobs"]:
        print(
            table(
                ["Created", "Job", "Status", "Length", "Cost", "Error"],
                [
                    [
                        when(row["created_at"]),
                        row["job_id"][:8],
                        row["status"],
                        span(row["duration_seconds"]),
                        row["cost_minutes"],
                        shorten(row["error"], 50),
                    ]
                    for row in data["jobs"]
                ],
            )
        )


def run_find(args) -> None:
    rows = find(args.text)
    if args.json:
        print_json(rows)
        return
    if not rows:
        print("No user matches")
        return
    print(table(["User", "Email", "Minutes"], [[row["user_id"], row["email"], row["minutes"]] for row in rows]))
    if len(rows) == FOUND:
        print(f"\nShowing the first {FOUND}; search for more of the address.")


def run_credit(args) -> None:
    ask_reason(args)
    plan = credit_plan(args.user, args.minutes)
    label = plan["email"] or plan["user_id"]
    say(args, f"{label}: balance {plan['balance']} -> {plan['balance_after']} minutes")
    if args.minutes > 0:
        question = f"Add {args.minutes} minutes to {label}?"
    else:
        question = f"Take {-args.minutes} minutes from {label}?"
    if not proceed(args, plan, question):
        return
    result = credit(args.user, args.minutes, args.by, args.reason)
    if args.minutes > 0:
        text = f"Credited {args.minutes} minutes to {label}; balance {result['balance_after']} minutes"
    else:
        text = f"Took {-args.minutes} minutes from {label}; balance {result['balance_after']} minutes"
    finish(args, result, text, ["user", plan["user_id"], "credit", str(-args.minutes), "--reason", "<why>"])


def run_delete(args) -> None:
    ask_reason(args)
    plan = delete_plan(args.user)
    label = plan["email"] or plan["user_id"]
    say(args, f"{label}: {plan['minutes']} minutes, its jobs and their files, and its job records will be deleted.")
    say(args, "Purchases are kept without the user. This cannot be undone.")
    if not proceed(args, plan, f"Permanently delete the account of {label}?", typed=label):
        return
    delete(args.user, args.by, args.reason)
    finish(args, plan, f"Deleted the account of {label}")
