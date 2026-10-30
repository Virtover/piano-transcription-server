"""`billing`: minutes sold, given free, and used per day or month, negative
balances, the voided-purchases sync, and the time jobs ran remotely."""

import time

from app import google_play
from app.billing import LEDGER_KINDS, LEDGER_SINCE_KEY
from app.admin.common import ago, billing_enabled, parse_duration, print_json, table, when
from app.config import settings
from app.worker import tasks

PERIOD_FORMATS = {"day": "%Y-%m-%d", "month": "%Y-%m"}
# Column order. Used and voided minutes are shown as positive numbers;
# changes by hand go both ways and keep their sign.
COLUMNS = (("purchase", "Sold"), ("free", "Free"), ("used", "Used"), ("voided", "Voided"), ("admin", "By hand"))
SIGNED = {"admin"}
NEGATIVE_SHOWN = 10


# --- Data ---------------------------------------------------------------------

def billing_report(since: float = 30 * 86400, per: str = "day", remote_price_per_hour: float | None = None) -> dict:
    """The report for the last `since` seconds, grouped per day or month in
    UTC. Ledger minutes keep their sign: used and voided are negative.
    minutes, negative_balances, and voided_synced_at are None without billing;
    voided_synced_at is also None before the first sync."""
    start = time.time() - since
    period = PERIOD_FORMATS[per]
    billing = tasks.billing
    report = {
        "since": start,
        "per": per,
        "billing": billing_enabled(),
        "minutes": None,
        "negative_balances": None,
        "voided_synced_at": None,
        "jobs": job_time(billing, start, period, remote_price_per_hour),
    }
    if billing_enabled():
        report["minutes"] = minutes(billing, start, period)
        report["negative_balances"] = negative_balances(billing)
        if settings.billing.provider == "google_play":
            synced_until = billing.get_state(google_play.VOIDED_SYNCED_UNTIL)
            report["voided_synced_at"] = int(synced_until) / 1000 if synced_until else None
    return report


def minutes(billing, start: float, period: str) -> dict:
    expression = (
        "to_char(to_timestamp(at) AT TIME ZONE 'UTC', 'YYYY-MM-DD')"
        if period == "%Y-%m-%d" else
        "to_char(to_timestamp(at) AT TIME ZONE 'UTC', 'YYYY-MM')"
    ) if getattr(billing, "_postgres", False) else "strftime(?, at, 'unixepoch')"
    parameters = (int(start),) if getattr(billing, "_postgres", False) else (period, int(start))
    rows = billing.read(
        f"SELECT {expression} AS period, kind, SUM(minutes) AS minutes "
        "FROM minute_ledger WHERE at >= ? GROUP BY period, kind ORDER BY period",
        parameters,
    )
    periods: dict[str, dict] = {}
    for row in rows:
        periods.setdefault(row["period"], {"period": row["period"], **dict.fromkeys(LEDGER_KINDS, 0)})[row["kind"]] = row["minutes"]
    recorded_since = billing.get_state(LEDGER_SINCE_KEY)
    return {
        "periods": list(periods.values()),
        "total": {kind: sum(values[kind] for values in periods.values()) for kind in LEDGER_KINDS},
        # When balance changes started being recorded; none before is counted.
        "recorded_since": int(recorded_since) if recorded_since else None,
    }


def negative_balances(billing) -> dict:
    rows = billing.read("SELECT user_id, email, minutes FROM users WHERE minutes < 0 ORDER BY minutes")
    return {
        "count": len(rows),
        "owed_minutes": -sum(row["minutes"] for row in rows),
        "users": [dict(row) for row in rows[:NEGATIVE_SHOWN]],
    }


def job_time(billing, start: float, period: str, price_per_hour: float | None) -> dict:
    expression = (
        "to_char(to_timestamp(created_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD')"
        if period == "%Y-%m-%d" else
        "to_char(to_timestamp(created_at) AT TIME ZONE 'UTC', 'YYYY-MM')"
    ) if getattr(billing, "_postgres", False) else "strftime(?, created_at, 'unixepoch')"
    parameters = (int(start),) if getattr(billing, "_postgres", False) else (period, int(start))
    rows = billing.read(
        f"SELECT {expression} AS period, backend, COUNT(*) AS jobs, "
        "SUM(finished_at - started_at) AS seconds FROM job_diagnostics "
        "WHERE created_at >= ? AND status = 'completed' AND started_at IS NOT NULL AND finished_at IS NOT NULL "
        "GROUP BY period, backend ORDER BY period",
        parameters,
    )
    periods = []
    remote_seconds = 0.0
    for row in rows:
        seconds = row["seconds"] or 0
        cost = None
        if row["backend"] == "remote":
            remote_seconds += seconds
            if price_per_hour is not None:
                cost = seconds / 3600 * price_per_hour
        periods.append({"period": row["period"], "backend": row["backend"], "jobs": row["jobs"], "seconds": seconds, "remote_cost": cost})
    return {
        "periods": periods,
        "remote_seconds": remote_seconds,
        "remote_price_per_hour": price_per_hour,
        "remote_cost": remote_seconds / 3600 * price_per_hour if price_per_hour is not None else None,
        # How far back job records go.
        "records_kept_seconds": settings.jobs.diagnostics_retention,
    }


# --- Command line -------------------------------------------------------------

def shown(kind: str, minutes: int) -> str:
    return f"{minutes:+d}" if kind in SIGNED and minutes else str(abs(minutes))


def users(count: int) -> str:
    return f"{count} user" if count == 1 else f"{count} users"


def run(args) -> None:
    report = billing_report(parse_duration(args.since, "--since"), args.per, args.remote_price_per_hour)
    if args.json:
        print_json(report)
        return
    print(f"Since {when(report['since'])}, per {report['per']} (UTC)\n")
    if not report["billing"]:
        print("Billing is off (BILLING_PROVIDER=none): no minutes are sold or used.\n")
    else:
        render_minutes(report)
        render_negative(report["negative_balances"])
        if settings.billing.provider == "google_play":
            synced_at = report["voided_synced_at"]
            if synced_at is None:
                print("Voided purchases: never synced; the API syncs them when it starts and then regularly\n")
            else:
                print(f"Voided purchases: last synced {ago(synced_at)} ({when(synced_at)})\n")
    render_jobs(report["jobs"])


def render_minutes(report: dict) -> None:
    data = report["minutes"]
    print("Minutes")
    if not data["periods"]:
        print("  No balance changes in this time.")
    else:
        body = [[values["period"], *[shown(kind, values[kind]) for kind, _ in COLUMNS]] for values in data["periods"]]
        body.append(["Total", *[shown(kind, data["total"][kind]) for kind, _ in COLUMNS]])
        print(table(["Period", *[title for _, title in COLUMNS]], body))
    if data["recorded_since"] and data["recorded_since"] > report["since"]:
        print(f"  Balance changes are recorded from {when(data['recorded_since'])}; earlier ones are not in this report.")
    print()


def render_negative(data: dict) -> None:
    if not data["count"]:
        print("Negative balances: none\n")
        return
    print(f"Negative balances: {users(data['count'])}, owing {data['owed_minutes']} minutes after refunds or chargebacks")
    print(table(["User", "Minutes"], [[row["email"] or row["user_id"], row["minutes"]] for row in data["users"]]))
    if data["count"] > len(data["users"]):
        print(f"  and {data['count'] - len(data['users'])} more")
    print()


def render_jobs(data: dict) -> None:
    price = data["remote_price_per_hour"]
    print("Completed jobs and their run time")
    if not data["periods"]:
        print("  None in this time.")
    else:
        body = []
        for row in data["periods"]:
            line = [row["period"], row["backend"] or "-", row["jobs"], f"{row['seconds'] / 3600:.2f}"]
            if price is not None:
                line.append(f"{row['remote_cost']:.2f}" if row["remote_cost"] is not None else "-")
            body.append(line)
        headers = ["Period", "Backend", "Jobs", "Hours"]
        if price is not None:
            headers.append("Remote cost")
        print(table(headers, body))
        if price is not None:
            print(f"  Estimated remote cost: {data['remote_cost']:.2f} at {price:g} per hour.")
    print(
        "  Run time is from start to finish, including the download, so it overstates the time a remote GPU is billed. "
        f"Job records are kept for {data['records_kept_seconds'] / 86400:g} days (JOB_DIAGNOSTICS_RETENTION)."
    )
