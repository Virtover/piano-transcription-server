"""`user`: one account's balance and history, credits and debits by hand,
and account deletion."""

from app import controls
from app.admin.common import AdminError, audit, confirm, find_user, print_json, shorten, span, table, when
from app.billing import deleted_user_target, period_seconds
from app.config import settings
from app.worker import tasks

RECENT = 10


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
    plan = credit_plan(who, minutes)
    balance = tasks.billing.adjust_minutes(plan["user_id"], minutes, reason)
    audit(actor, "user.credit", plan["user_id"], f"{minutes:+d} minutes", reason)
    return {**plan, "balance_after": balance}


def delete_plan(who: str) -> dict:
    user = find_user(who)
    return {"user_id": user["user_id"], "email": user["email"], "minutes": user["minutes"]}


def delete(who: str, actor: str, reason: str | None = None) -> dict:
    """Delete the account and its data, as DELETE /api/account does, and
    any block on it."""
    plan = delete_plan(who)
    tasks.delete_account(plan["user_id"])
    controls.unblock(tasks.redis, plan["user_id"])
    # Logged by hash: the account's ID is personal data and now gone.
    audit(actor, "user.delete", deleted_user_target(plan["user_id"]), None, reason)
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


def run_credit(args) -> None:
    plan = credit_plan(args.user, args.minutes)
    label = plan["email"] or plan["user_id"]
    if args.minutes < 0:
        print(f"{label}: balance {plan['balance']} -> {plan['balance_after']} minutes")
        if not confirm(f"Take {-args.minutes} minutes from {label}?", args.yes):
            print("Nothing changed")
            return
    result = credit(args.user, args.minutes, args.by, args.reason)
    if args.minutes > 0:
        print(f"Credited {args.minutes} minutes to {label}; balance {result['balance_after']} minutes")
    else:
        print(f"Took {-args.minutes} minutes from {label}; balance {result['balance_after']} minutes")


def run_delete(args) -> None:
    plan = delete_plan(args.user)
    label = plan["email"] or plan["user_id"]
    print(f"{label}: {plan['minutes']} minutes, its jobs and their files, and its job records will be deleted.")
    print("Purchases are kept without the user. This cannot be undone.")
    if not confirm(f"Permanently delete the account of {label}?", args.yes):
        print("Nothing changed")
        return
    delete(args.user, args.by, args.reason)
    print(f"Deleted the account of {label}")
