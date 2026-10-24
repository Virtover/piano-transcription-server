"""`billing`: minutes sold, given free, and used per day or month, negative
balances, the voided-purchases sync, and the time jobs ran remotely."""

import time

from app import google_play
from app.billing import LEDGER_SINCE_KEY
from app.admin.common import AdminError, ago, billing_enabled, table, when
from app.config import duration_seconds, settings
from app.worker import tasks

PERIOD_FORMATS = {"day": "%Y-%m-%d", "month": "%Y-%m"}
# Column order. Used and voided minutes are shown as positive numbers;
# changes by hand go both ways and keep their sign.
COLUMNS = (("purchase", "Sold"), ("free", "Free"), ("used", "Used"), ("voided", "Voided"), ("admin", "By hand"))
SIGNED = {"admin"}
NEGATIVE_SHOWN = 10


def run(args) -> None:
    try:
        since = time.time() - duration_seconds(args.since, "--since")
    except ValueError as error:
        raise AdminError(str(error)) from error
    period = PERIOD_FORMATS[args.per]
    billing = tasks.billing
    print(f"Since {when(since)}, per {args.per} (UTC)\n")

    if billing_enabled():
        minutes(billing, since, period)
        negative_balances(billing)
        voided_sync(billing)
    else:
        print("Billing is off (BILLING_PROVIDER=none): no minutes are sold or used.\n")
    job_time(billing, since, period, args.remote_price_per_hour)


def minutes(billing, since: float, period: str) -> None:
    rows = billing.read(
        "SELECT strftime(?, at, 'unixepoch') AS period, kind, SUM(minutes) AS minutes "
        "FROM minute_ledger WHERE at >= ? GROUP BY period, kind ORDER BY period",
        (period, int(since)),
    )
    totals: dict[str, dict[str, int]] = {}
    for row in rows:
        totals.setdefault(row["period"], {})[row["kind"]] = row["minutes"]
    print("Minutes")
    if not totals:
        print("  No balance changes in this time.")
    else:
        body = [[name, *[shown(kind, values.get(kind, 0)) for kind, _ in COLUMNS]] for name, values in totals.items()]
        body.append(["Total", *[shown(kind, sum(values.get(kind, 0) for values in totals.values())) for kind, _ in COLUMNS]])
        print(table(["Period", *[title for _, title in COLUMNS]], body))
    recorded_since = billing.get_state(LEDGER_SINCE_KEY)
    if recorded_since and int(recorded_since) > since:
        print(f"  Balance changes are recorded from {when(int(recorded_since))}; earlier ones are not in this report.")
    print()


def shown(kind: str, minutes: int) -> str:
    return f"{minutes:+d}" if kind in SIGNED and minutes else str(abs(minutes))


def users(count: int) -> str:
    return f"{count} user" if count == 1 else f"{count} users"


def negative_balances(billing) -> None:
    rows = billing.read("SELECT user_id, email, minutes FROM users WHERE minutes < 0 ORDER BY minutes")
    if not rows:
        print("Negative balances: none\n")
        return
    owed = -sum(row["minutes"] for row in rows)
    print(f"Negative balances: {users(len(rows))}, owing {owed} minutes after refunds or chargebacks")
    print(table(["User", "Minutes"], [[row["email"] or row["user_id"], row["minutes"]] for row in rows[:NEGATIVE_SHOWN]]))
    if len(rows) > NEGATIVE_SHOWN:
        print(f"  and {len(rows) - NEGATIVE_SHOWN} more")
    print()


def voided_sync(billing) -> None:
    if settings.billing.provider != "google_play":
        return
    synced_until = billing.get_state(google_play.VOIDED_SYNCED_UNTIL)
    if synced_until is None:
        print("Voided purchases: never synced; the API syncs them when it starts and then regularly\n")
    else:
        synced_at = int(synced_until) / 1000
        print(f"Voided purchases: last synced {ago(synced_at)} ({when(synced_at)})\n")


def job_time(billing, since: float, period: str, price_per_hour: float | None) -> None:
    rows = billing.read(
        "SELECT strftime(?, created_at, 'unixepoch') AS period, backend, COUNT(*) AS jobs, "
        "SUM(finished_at - started_at) AS seconds FROM job_diagnostics "
        "WHERE created_at >= ? AND status = 'completed' AND started_at IS NOT NULL AND finished_at IS NOT NULL "
        "GROUP BY period, backend ORDER BY period",
        (period, int(since)),
    )
    retention = settings.billing.job_diagnostics_retention
    print("Completed jobs and their run time")
    if not rows:
        print("  None in this time.")
    else:
        body = []
        remote_seconds = 0.0
        for row in rows:
            hours = (row["seconds"] or 0) / 3600
            line = [row["period"], row["backend"] or "-", row["jobs"], f"{hours:.2f}"]
            if price_per_hour is not None:
                line.append(f"{hours * price_per_hour:.2f}" if row["backend"] == "remote" else "-")
            body.append(line)
            if row["backend"] == "remote":
                remote_seconds += row["seconds"] or 0
        headers = ["Period", "Backend", "Jobs", "Hours"]
        if price_per_hour is not None:
            headers.append("Remote cost")
        print(table(headers, body))
        if price_per_hour is not None:
            print(f"  Estimated remote cost: {remote_seconds / 3600 * price_per_hour:.2f} at {price_per_hour:g} per hour.")
    print(
        "  Run time is from start to finish, including the download, so it overstates the time a remote GPU is billed. "
        f"Job records are kept for {retention / 86400:g} days (BILLING_JOB_DIAGNOSTICS_RETENTION)."
    )
