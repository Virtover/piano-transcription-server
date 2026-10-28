"""Helpers shared by the admin commands: formatting, confirmation, finding
users and jobs, and the audit log.

Each command module has data functions, which return plain dicts that JSON
can encode and raise AdminError, and `run_*` functions on top that print them
for the command line. Anything else that operates the server, such as a web
page, calls the data functions, so it behaves exactly like the commands."""

import json
import os
import re
import shlex
import sqlite3
import sys
import time
from datetime import datetime, timezone

from app.config import duration_seconds, settings
from app.worker import tasks


# A change names a job by at least this much of its ID, as the job list
# shows it, so a short prefix cannot pick a job the operator did not mean.
CHANGE_PREFIX_LENGTH = 8
# Reasons are kept after an account is deleted, so they must not name anyone.
EMAIL_ADDRESS = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
# Set by scripts/admin.py: who runs it on the host, and how to run it again.
ACTOR_VARIABLE = "ADMIN_ACTOR"
COMMAND_VARIABLE = "ADMIN_COMMAND"


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


def default_actor() -> str:
    return os.environ.get(ACTOR_VARIABLE, "").strip() or "admin"


def command_prefix() -> str:
    return os.environ.get(COMMAND_VARIABLE, "").strip() or "python -m app.admin"


def interactive() -> bool:
    return sys.stdin.isatty()


def ask(question: str) -> str:
    """An answer typed in a terminal. Without one nothing can be asked, and
    the command stops before it changes anything."""
    if not interactive():
        raise AdminError("Standard input is not a terminal, so nothing can be asked: add --yes. Nothing was changed")
    return input(question)


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    return ask(f"{question} [y/N] ").strip().lower() in {"y", "yes"}


def confirm_typed(question: str, expected: str, assume_yes: bool) -> bool:
    """For what cannot be undone: the operator types expected, not just y."""
    if assume_yes:
        return True
    return ask(f"{question}\nType {expected} to confirm: ").strip().lower() == expected.lower()


def check_reason(reason: str | None, required: bool = False) -> str | None:
    """The reason for the audit log, or AdminError if a required one is
    missing or it contains an email address."""
    reason = (reason or "").strip() or None
    if reason is None and required:
        raise AdminError("Give a --reason; it is kept in the audit log")
    if reason and EMAIL_ADDRESS.search(reason):
        raise AdminError("Leave email addresses out of the reason: reasons are kept after an account is deleted")
    return reason


def ask_reason(args) -> None:
    """Ask for a missing reason in a terminal, before a change that needs
    one. With --yes, or without a terminal, the change refuses instead."""
    if not (args.reason or "").strip() and not getattr(args, "yes", False) and not args.dry_run and interactive():
        args.reason = input("Reason, for the audit log: ")


def say(args, text: str) -> None:
    """Text for the operator; JSON output has the data instead."""
    if not args.json:
        print(text)


def proceed(
    args, plan: dict, question: str | None = None, typed: str | None = None, would: str | None = None
) -> bool:
    """After the plan was shown: whether to make the change. A dry run and
    a no stop here, saying so; question None needs no confirmation. would
    describes the change, for a dry run of one that does not ask."""
    if args.dry_run:
        if args.json:
            print_json({"dry_run": True, "plan": plan})
        else:
            if would:
                print(would)
            print("Dry run: nothing was changed")
        return False
    if question is None:
        return True
    if args.json and not args.yes:
        raise AdminError("--json cannot ask for confirmation: add --yes, or --dry-run. Nothing was changed")
    agreed = confirm_typed(question, typed, args.yes) if typed else confirm(question, args.yes)
    if not agreed:
        print("Nothing changed")
    return agreed


def finish(args, result: dict, text: str, undo: list[str] | None = None) -> None:
    """Report a change that was made, with the command that undoes it."""
    if args.json:
        print_json({"dry_run": False, "result": result})
        return
    print(text)
    if undo:
        print(f"To undo this, run: {command_prefix()} {shlex.join(undo)}")


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
    if len(rows) > 1:
        ids = ", ".join(sorted(row["user_id"] for row in rows))
        raise AdminError(f"{who.strip()} matches several accounts ({ids}); name the user by ID")
    return dict(rows[0])


def find_job(prefix: str, change: bool = False) -> str:
    """A job ID from its first characters, as the job list shows them.
    Searches Redis, for jobs being prepared, and the diagnostics records.
    A change needs at least CHANGE_PREFIX_LENGTH characters."""
    prefix = prefix.strip().lower()
    if not prefix or not all(character in "0123456789abcdef-" for character in prefix):
        raise AdminError(f"Not a job ID: {prefix}")
    if change and len(prefix) < CHANGE_PREFIX_LENGTH:
        raise AdminError(f"Give at least {CHANGE_PREFIX_LENGTH} characters of the job ID to change a job")
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


def ensure_audit_log() -> None:
    """Before a change that cannot share a transaction with its audit entry,
    such as one in Redis: AdminError if the billing database, which holds
    the audit log, cannot be reached now."""
    try:
        tasks.billing.check()
    except Exception as error:
        raise AdminError(f"The audit log cannot be reached ({error}), so nothing was changed") from error


def audit(actor: str, action: str, target: str | None = None, detail: str | None = None, reason: str | None = None) -> None:
    try:
        tasks.billing.log_admin_action(actor, action, target, detail, reason)
    except Exception as error:
        raise AdminError(
            f"The change was made, but the audit log could not be written ({error}); record it by hand"
        ) from error
