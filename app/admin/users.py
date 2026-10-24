"""`user`: one account's balance and history, credits and debits by hand,
and account deletion."""

from app import controls
from app.admin.common import AdminError, audit, confirm, find_user, shorten, span, table, when
from app.billing import deleted_user_target, period_seconds
from app.config import settings
from app.worker import tasks

RECENT = 10


def show(args) -> None:
    user = find_user(args.user)
    user_id = user["user_id"]
    billing = tasks.billing
    reserved = billing.read(
        "SELECT * FROM transcription_reservations WHERE user_id = ? AND status = 'reserved'",
        (user_id,),
    )
    held = sum(row["reserved_minutes"] for row in reserved)

    print(f"User {user_id}")
    print(f"  email: {user['email'] or '-'}")
    print(f"  balance: {user['minutes']} minutes, {held} reserved by running jobs, {user['minutes'] - held} available")
    if user["minutes"] < 0:
        print("  The balance is negative: a refunded purchase's minutes were already used.")
    if (settings.billing.free_minutes or 0) > 0:
        next_grant = user["free_grant_at"] + period_seconds() if user["free_grant_at"] else None
        print(f"  last free minutes: {when(user['free_grant_at'])}, next from {when(next_grant) if next_grant else 'the next request'}")
    block = controls.blocked(tasks.redis, user_id)
    if block is not None:
        until = f" for {span(block.expires_in)} more" if block.expires_in else ""
        print(f"  BLOCKED{until}: {block.value or 'no reason given'}")

    purchases = billing.read(
        "SELECT product_id, order_id, minutes, voided_at FROM purchases WHERE user_id = ? ORDER BY rowid DESC",
        (user_id,),
    )
    print(f"\nPurchases ({len(purchases)}):")
    if purchases:
        print(
            table(
                ["Product", "Order", "Minutes", "Voided"],
                [[row["product_id"], row["order_id"], row["minutes"], when(row["voided_at"])] for row in purchases],
            )
        )

    ledger = billing.read(
        "SELECT * FROM minute_ledger WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, RECENT),
    )
    print(f"\nRecent balance changes (newest {RECENT}):")
    if ledger:
        print(
            table(
                ["When", "Kind", "Minutes", "Note"],
                [[when(row["at"]), row["kind"], f"{row['minutes']:+d}", shorten(row["note"], 50)] for row in ledger],
            )
        )

    if reserved:
        print("\nReservations held by running jobs:")
        print(table(["Job", "Minutes"], [[row["job_id"][:8], row["reserved_minutes"]] for row in reserved]))

    jobs = billing.read(
        "SELECT * FROM job_diagnostics WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, RECENT),
    )
    print(f"\nRecent jobs (newest {RECENT}; details: jobs show <job>):")
    if jobs:
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
                    for row in jobs
                ],
            )
        )


def credit(args) -> None:
    user = find_user(args.user)
    if args.minutes == 0:
        raise AdminError("Give a number of minutes other than 0; a negative number takes minutes away")
    label = user["email"] or user["user_id"]
    after = user["minutes"] + args.minutes
    if args.minutes < 0:
        print(f"{label}: balance {user['minutes']} -> {after} minutes")
        if not confirm(f"Take {-args.minutes} minutes from {label}?", args.yes):
            print("Nothing changed")
            return
    balance = tasks.billing.adjust_minutes(user["user_id"], args.minutes, args.reason)
    audit(args, "user.credit", user["user_id"], f"{args.minutes:+d} minutes")
    verb = "Credited" if args.minutes > 0 else "Took"
    print(f"{verb} {abs(args.minutes)} minutes {'to' if args.minutes > 0 else 'from'} {label}; balance {balance} minutes")


def delete(args) -> None:
    user = find_user(args.user)
    label = user["email"] or user["user_id"]
    print(f"{label}: {user['minutes']} minutes, its jobs and their files, and its job records will be deleted.")
    print("Purchases are kept without the user. This cannot be undone.")
    if not confirm(f"Permanently delete the account of {label}?", args.yes):
        print("Nothing changed")
        return
    tasks.delete_account(user["user_id"])
    controls.unblock(tasks.redis, user["user_id"])
    # Logged by hash: the account's ID is personal data and now gone.
    audit(args, "user.delete", deleted_user_target(user["user_id"]))
    print(f"Deleted the account of {label}")
