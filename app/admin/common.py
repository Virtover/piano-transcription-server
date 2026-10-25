"""Helpers shared by the admin commands: formatting, confirmation, finding
users and jobs, and the audit log.

Each command module has data functions, which return plain dicts that JSON
can encode and raise AdminError, and `run_*` functions on top that print them
for the command line. Anything else that operates the server, such as a web
page, calls the data functions, so it behaves exactly like the commands."""

import json
import sqlite3
import time
from datetime import datetime, timezone

from app.config import duration_seconds, settings
from app.worker import tasks


class AdminError(Exception):
    """A command cannot do what was asked. The message says why."""


def when(timestamp: float | None) -> str:
    if not timestamp:
        return "-"
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def span(seconds: float | None) -> str:
    """A rough duration: 45 s, 12 m, 3.5 h, 2 d."""
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f} s"
    if seconds < 3600:
        return f"{seconds / 60:.0f} m"
    if seconds < 2 * 86400:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.0f} d"


def ago(timestamp: float | None) -> str:
    return "-" if not timestamp else f"{span(max(0.0, time.time() - timestamp))} ago"


def size(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024:
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024
    return f"{count:.1f} TB"


def table(headers: list[str], rows: list[list[object]]) -> str:
    cells = [headers, *[["-" if value is None else str(value) for value in row] for row in rows]]
    widths = [max(len(row[column]) for row in cells) for column in range(len(headers))]
    lines = ["  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip() for row in cells]
    lines.insert(1, "  ".join("-" * width for width in widths))
    return "\n".join(lines)


def shorten(text: str | None, length: int) -> str | None:
    if text is None or len(text) <= length:
        return text
    return text[: length - 1] + "…"


def parse_duration(text: str | None, name: str) -> float | None:
    if not text:
        return None
    try:
        return duration_seconds(text, name)
    except ValueError as error:
        raise AdminError(str(error)) from error


def row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def print_json(data) -> None:
    print(json.dumps(data, indent=2))


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}


def billing_enabled() -> bool:
    return settings.billing.provider != "none"


def find_user(who: str) -> dict:
    """A billed user by email address or user ID."""
    if not billing_enabled():
        raise AdminError("Billing is off (BILLING_PROVIDER=none), so there are no user accounts")
    rows = tasks.billing.read(
        "SELECT * FROM users WHERE user_id = ? OR email = ?",
        (who.strip(), who.strip().lower()),
    )
    if not rows:
        raise AdminError(f"No user with the email address or ID {who.strip()}; users appear after they sign in")
    return dict(rows[0])


def find_job(prefix: str) -> str:
    """A job ID from its first characters, as the job list shows them.
    Searches Redis, for jobs being prepared, and the diagnostics records."""
    prefix = prefix.strip().lower()
    if not prefix or not all(character in "0123456789abcdef-" for character in prefix):
        raise AdminError(f"Not a job ID: {prefix}")
    found = {key.removeprefix(tasks.job_key("")) for key in tasks.redis.scan_iter(match=tasks.job_key(prefix + "*"))}
    found.update(
        row["job_id"] for row in tasks.billing.read("SELECT job_id FROM job_diagnostics WHERE job_id LIKE ?", (prefix + "%",))
    )
    if not found:
        raise AdminError(f"No job starts with {prefix}")
    if len(found) > 1:
        raise AdminError(f"Several jobs start with {prefix}: {', '.join(sorted(found))}")
    return found.pop()


def user_label(user_id: str | None, email: str | None = None) -> str:
    return email or user_id or "-"


def audit(actor: str, action: str, target: str | None = None, detail: str | None = None, reason: str | None = None) -> None:
    tasks.billing.log_admin_action(actor, action, target, detail, reason)
