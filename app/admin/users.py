"""`user`: one account's balance and history, credits and debits by hand,
developer accounts, and account deletion. `users find`: accounts by part of
their email. `users credit-all`: minutes for every account."""

import uuid

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
from app.admin.jobs import mode_label, with_mode
from app.billing import period_seconds
from app.config import settings
from app.worker import tasks

RECENT = 10
FOUND = 20
SEARCH_LENGTH = 3
# The most minutes credit-all gives each account at once.
CREDIT_ALL_LIMIT = 10_000


# --- Data ---------------------------------------------------------------------

def account(who: str) -> dict:
    """A user's balances, paid and monthly, free minutes, block, purchases (without tokens),
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
        # Paid minutes; monthly ones, spent first, are apart.
        "minutes": user["minutes"],
        "monthly_minutes": user["monthly_minutes"],
        "reserved_minutes": held,
        "available_minutes": user["minutes"] + user["monthly_minutes"] - held,
        # Jobs of a developer's account are never charged.
        "developer": bool(user.get("developer")),
        "free_minutes": free,
        # The plan whose period runs now; its minutes are the monthly ones.
        **billing.subscription_status(user_id),
        "subscriptions": [
            dict(row)
            for row in billing.read(
                "SELECT product_id, state, expires_at, auto_renewing, latest_order_id, replaced_by, revoked_at "
                "FROM subscriptions WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, RECENT),
            )
        ],
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
                "SELECT at, kind, pool, minutes, note FROM minute_ledger WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, RECENT),
            )
        ],
        "reservations": reserved,
        "jobs": [
            with_mode(dict(row))
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
        "SELECT user_id, email, minutes, monthly_minutes, developer FROM users "
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


def credit_all_plan(minutes: int) -> dict:
    """What giving every account minutes would change. Only adding: taking
    minutes from everyone is not offered."""
    if minutes <= 0:
        raise AdminError("Give a positive number of minutes; minutes are never taken from every account at once")
    if minutes > CREDIT_ALL_LIMIT:
        raise AdminError(f"At most {CREDIT_ALL_LIMIT} minutes for each account at once")
    accounts = tasks.billing.account_count()
    if not accounts:
        raise AdminError("There are no accounts yet")
    return {"minutes": minutes, "accounts": accounts, "total": minutes * accounts}


def credit_all(minutes: int, actor: str, reason: str | None, grant_id: str) -> dict:
    """Add paid minutes, which never expire, to every account, with one
    audit entry, in one transaction. A repeat of the same grant_id, as after
    a lost answer, changes nothing."""
    reason = check_reason(reason, required=True)
    credit_all_plan(minutes)
    return tasks.billing.credit_all(minutes, reason, actor, grant_id)


def credit_all_undo_plan(grant_id: str) -> dict:
    """What undoing a grant to every account would take back. Each account
    gives back at most the paid minutes it has left; none goes below zero."""
    try:
        plan = tasks.billing.undo_credit_all_plan(grant_id)
    except ValueError as error:
        raise AdminError(str(error)) from error
    if plan["undone"]:
        raise AdminError(f"The grant {grant_id} was already undone")
    return {key: value for key, value in plan.items() if key != "take"} | {"accounts_taken_from": len(plan["take"])}


def undo_credit_all(grant_id: str, actor: str, reason: str | None) -> dict:
    reason = check_reason(reason, required=True)
    credit_all_undo_plan(grant_id)
    result = tasks.billing.undo_credit_all(grant_id, reason, actor)
    return {key: value for key, value in result.items() if key != "take"} | {"accounts_taken_from": len(result["take"])}


def grants() -> list[dict]:
    return tasks.billing.credit_all_grants()


def credit_all_phrase(minutes: int) -> str:
    """What the operator types to confirm credit-all, here and on the web page."""
    return f"give {minutes} to all"


def developer_plan(who: str, developer: bool) -> dict:
    """What making the account a developer's, or ending that, changes."""
    user = find_user(who)
    now = bool(user.get("developer"))
    if now == developer:
        state = "already a developer's" if developer else "not a developer's"
        raise AdminError(f"The account of {user['email'] or user['user_id']} is {state}")
    return {"user_id": user["user_id"], "email": user["email"], "developer_now": now, "developer": developer}


def set_developer(who: str, developer: bool, actor: str, reason: str | None = None) -> dict:
    """Make the account a developer's, whose jobs are never charged, or end
    that. The change and its audit entry are written together."""
    reason = check_reason(reason, required=True)
    plan = developer_plan(who, developer)
    tasks.billing.set_developer(plan["user_id"], developer, actor, reason)
    return plan


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
    if data["developer"]:
        print("  DEVELOPER: jobs are never charged")
    print(
        f"  balance: {data['minutes']} paid and {data['monthly_minutes']} monthly minutes, "
        f"{data['reserved_minutes']} reserved by running jobs, {data['available_minutes']} available"
    )
    if data["minutes"] < 0:
        print("  The paid balance is negative: a refunded purchase's minutes were already used.")
    subscription = data["subscription"]
    if subscription is not None:
        per_period = "unlimited" if subscription["minutes"] is None else f"{subscription['minutes']} minutes"
        renews = "renews" if subscription["auto_renewing"] else "ends"
        print(
            f"  subscription: {subscription['name']} ({per_period} a period), {renews} "
            f"{when(subscription['period_ends_at'])}, Google Play state {subscription['state'] or '-'}"
        )
        if data["unlimited"]:
            heavy = "heavy use in the last 24 hours: one job at a time, never outsourced" if data["unlimited_heavy_use"] else "normal use"
            print(f"  unlimited: {heavy}")
    free = data["free_minutes"]
    if free is not None and subscription is None:
        next_grant = when(free["next_grant_at"]) if free["next_grant_at"] else "the next request"
        print(f"  monthly minutes last renewed: {when(free['last_grant_at'])}, next from {next_grant}")
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
                ["When", "Kind", "Balance", "Minutes", "Note"],
                [
                    [when(row["at"]), row["kind"], row["pool"], f"{row['minutes']:+d}", shorten(row["note"], 50)]
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
                ["Created", "Job", "Status", "Mode", "Length", "Cost", "Error"],
                [
                    [
                        when(row["created_at"]),
                        row["job_id"][:8],
                        row["status"],
                        mode_label(row),
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
    print(table(
        ["User", "Email", "Paid", "Monthly"],
        [[row["user_id"], row["email"], row["minutes"], row["monthly_minutes"]] for row in rows],
    ))
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


def run_credit_all(args) -> None:
    ask_reason(args)
    plan = credit_all_plan(args.minutes)
    say(
        args,
        f"Every account, {plan['accounts']} now, gets {plan['minutes']} paid minutes that never expire: "
        f"{plan['total']} minutes in all. Accounts made later get none.",
    )
    question = f"Give {plan['minutes']} minutes to each of {plan['accounts']} accounts?"
    if not proceed(args, plan, question, typed=credit_all_phrase(args.minutes)):
        return
    result = credit_all(args.minutes, args.by, args.reason, uuid.uuid4().hex)
    text = f"Gave {result['minutes']} minutes to each of {result['accounts']} accounts"
    finish(args, result, text, ["users", "credit-all-undo", result["grant_id"], "--reason", "<why>"])


def run_credit_all_undo(args) -> None:
    ask_reason(args)
    plan = credit_all_undo_plan(args.grant)
    say(
        args,
        f"The grant gave {plan['granted']} minutes to {plan['accounts']} accounts. Undoing it takes back "
        f"{plan['taken']} from {plan['accounts_taken_from']} accounts; {plan['kept']} were already used and stay, "
        "so no balance goes below zero.",
    )
    if not proceed(args, plan, f"Take back {plan['taken']} minutes?"):
        return
    result = undo_credit_all(args.grant, args.by, args.reason)
    finish(args, result, f"Took back {result['taken']} minutes from {result['accounts_taken_from']} accounts")


def run_grants(args) -> None:
    rows = grants()
    if args.json:
        print_json(rows)
        return
    if not rows:
        print("No grants to every account yet")
        return
    print(table(
        ["When", "Grant", "Each", "Accounts", "By", "Undone"],
        [[when(row["at"]), row["grant_id"], row.get("minutes", "?"), row.get("accounts", "?"), row["actor"],
          when(row["undone_at"]) if row["undone_at"] else "-"] for row in rows],
    ))


def run_developer(args) -> None:
    ask_reason(args)
    developer = args.state == "on"
    plan = developer_plan(args.user, developer)
    label = plan["email"] or plan["user_id"]
    if developer:
        say(args, f"{label}: their jobs will no longer be charged, and the apps show them the developer extras.")
        question = f"Make the account of {label} a developer's?"
    else:
        say(args, f"{label}: their jobs will be charged again from their balance.")
        question = f"End the developer account of {label}?"
    if not proceed(args, plan, question):
        return
    set_developer(args.user, developer, args.by, args.reason)
    text = f"{label} is now a developer" if developer else f"{label} is no longer a developer"
    undo = ["user", plan["user_id"], "developer", "off" if developer else "on", "--reason", "<why>"]
    finish(args, plan, text, undo)


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
