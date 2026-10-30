"""Billing database backups: when they are made, which are kept, and the
optional encrypted copies in off-site storage.

    python -m app.backups now        # back up now, prune, and copy off-site
    python -m app.backups list       # local and off-site backups
    python -m app.backups keygen     # a new key pair for OFFSITE_BACKUP_RECIPIENT
    python -m app.backups fetch latest < key.txt   # download and decrypt one

Backups are kept in steps by age (BILLING_BACKUP_KEEP_*): every backup at
first, then the oldest of each hour, day, week, and month (UTC). Keeping the
oldest of each period means a backup kept by one step is also the one the
next step keeps, so thinning never deletes a backup that is still needed.
The newest backup is always kept, so a server that was off for a long time
still has one. Off-site copies follow the same steps, decided from the
copies that exist there, so they keep their history when this disk is lost.
"""

import argparse
import logging
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from app.config import settings

logger = logging.getLogger(__name__)

BACKUP_FILE_PATTERN = re.compile(r"^billing-(\d+)\.(?:sqlite3|dump)$")
OFFSITE_PREFIX = "billing-backups/"
OFFSITE_KEY_PATTERN = re.compile(r"^billing-backups/billing-(\d+)\.(?:sqlite3|dump)\.age$")
# billing_state keys for the last off-site copy, shown by `app.admin status`.
OFFSITE_SUCCESS_KEY = "offsite_backup_succeeded_at"
OFFSITE_FAILURE_KEY = "offsite_backup_failure"
KEYGEN_COMMAND = "docker compose run --rm --no-deps api python -m app.backups keygen"

HOUR = 60 * 60
DAY = 24 * HOUR


def _week(timestamp: int) -> int:
    # Weeks start on Monday; 1 January 1970 was a Thursday.
    return (timestamp // DAY + 3) // 7


def _month(timestamp: int) -> tuple[int, int]:
    moment = datetime.fromtimestamp(timestamp, timezone.utc)
    return moment.year, moment.month


@dataclass(frozen=True)
class RetentionPolicy:
    """Ages in seconds. 0 turns a step off; max_age None means no limit."""

    keep_all: float
    hourly: float
    daily: float
    weekly: float
    monthly: float
    max_age: float | None = None

    @classmethod
    def from_settings(cls, billing=None) -> "RetentionPolicy":
        billing = billing or settings.billing
        return cls(
            keep_all=billing.backup_keep_all,
            hourly=billing.backup_keep_hourly,
            daily=billing.backup_keep_daily,
            weekly=billing.backup_keep_weekly,
            monthly=billing.backup_keep_monthly,
            max_age=billing.backup_retention,
        )

    def steps(self) -> list[tuple[float, Callable[[int], Any]]]:
        return [
            (self.hourly, lambda timestamp: timestamp // HOUR),
            (self.daily, lambda timestamp: timestamp // DAY),
            (self.weekly, _week),
            (self.monthly, _month),
        ]

    def longest(self) -> float:
        """How long the oldest backup is kept."""
        longest = max(self.keep_all, self.hourly, self.daily, self.weekly, self.monthly)
        return min(longest, self.max_age) if self.max_age else longest

    def keep(self, timestamps: Iterable[int], now: float) -> set[int]:
        ordered = sorted(set(timestamps))
        if not ordered:
            return set()
        kept = {timestamp for timestamp in ordered if now - timestamp < self.keep_all}
        for window, period in self.steps():
            oldest_per_period = {}
            for timestamp in ordered:
                if now - timestamp < window:
                    oldest_per_period.setdefault(period(timestamp), timestamp)
            kept.update(oldest_per_period.values())
        if self.max_age:
            kept = {timestamp for timestamp in kept if now - timestamp < self.max_age}
        kept.add(ordered[-1])
        return kept


def local_backups(directory: str) -> dict[int, Path]:
    """Automatic backups by timestamp. Other files are not backups."""
    backup_dir = Path(directory)
    if not backup_dir.is_dir():
        return {}
    found = {}
    for path in backup_dir.iterdir():
        match = BACKUP_FILE_PATTERN.match(path.name)
        if match and path.is_file():
            found[int(match.group(1))] = path
    return found


def prune_backups(directory: str, policy: RetentionPolicy, now: float | None = None) -> list[Path]:
    """Delete the automatic backups the policy does not keep. Only files named
    like them are touched; the age comes from the name's timestamp."""
    backups = local_backups(directory)
    kept = policy.keep(backups, time.time() if now is None else now)
    removed = []
    for timestamp, path in sorted(backups.items()):
        if timestamp not in kept:
            path.unlink()
            removed.append(path)
    return removed


def replaced_database_problem(billing, directory: str) -> str | None:
    """A billing database created after the newest backup is not the one that
    was backed up: PostgreSQL started empty, for example because
    POSTGRES_DATA_LOCATION changed or its folder was removed. Serving it would
    show every balance as zero, and new purchases would land in it."""
    from app.billing import LEDGER_SINCE_KEY

    dumps = {timestamp: path for timestamp, path in local_backups(directory).items() if path.suffix == ".dump"}
    if not dumps:
        return None
    created = billing.get_state(LEDGER_SINCE_KEY)
    newest = max(dumps)
    if created is None or int(created) <= newest:
        return None
    return (
        f"The billing database was created at {_when(int(created))}, after the newest backup "
        f"{dumps[newest].name} ({_when(newest)}), so it is not the database that was backed up. "
        "PostgreSQL probably started empty in a new place: check POSTGRES_DATA_LOCATION in .env, or restore "
        "the backup (\"Moving the billing database\" in docs/configuration.md). To start with an empty "
        "billing database on purpose, move the .dump files out of billing-data/backups."
    )


# --- Off-site ----------------------------------------------------------------

def offsite_key(timestamp: int, extension: str | None = None) -> str:
    extension = extension or (
        "dump" if settings.billing.database_url.startswith(("postgres://", "postgresql://")) else "sqlite3"
    )
    return f"{OFFSITE_PREFIX}billing-{timestamp}.{extension}.age"


class OffsiteStore:
    """Backups in S3-compatible storage, encrypted with age to the
    recipient's public key before they leave this machine."""

    def __init__(self, client: Any, bucket: str, recipient: str | None):
        self.client = client
        self.bucket = bucket
        self.recipient = recipient

    @classmethod
    def from_settings(cls, config=None) -> "OffsiteStore":
        import boto3
        from botocore.config import Config

        config = config or settings
        offsite = config.offsite
        access_key = config.secrets.offsite_backup_access_key
        secret_key = config.secrets.offsite_backup_secret_key
        if not (offsite.endpoint and offsite.bucket and access_key and secret_key):
            raise RuntimeError("Off-site backups are not configured")
        client = boto3.client(
            "s3",
            endpoint_url=offsite.endpoint,
            region_name=offsite.region,
            aws_access_key_id=access_key.get_secret_value(),
            aws_secret_access_key=secret_key.get_secret_value(),
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
        )
        return cls(client, offsite.bucket, (offsite.recipient or "").strip() or None)

    def backups(self) -> dict[int, dict]:
        """Off-site backups by timestamp, with their key and size."""
        found = {}
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=OFFSITE_PREFIX):
            for item in page.get("Contents", []):
                match = OFFSITE_KEY_PATTERN.match(item["Key"])
                if match:
                    found[int(match.group(1))] = {"key": item["Key"], "size": item.get("Size", 0)}
        return found

    def upload(self, timestamp: int, path: Path) -> None:
        import pyrage
        from pyrage import x25519

        if not self.recipient:
            raise RuntimeError("OFFSITE_BACKUP_RECIPIENT is not set")
        encrypted = pyrage.encrypt(path.read_bytes(), [x25519.Recipient.from_str(self.recipient)])
        key = offsite_key(timestamp, path.suffix.removeprefix("."))
        self.client.put_object(Bucket=self.bucket, Key=key, Body=encrypted)
        stored = self.client.head_object(Bucket=self.bucket, Key=key).get("ContentLength")
        if stored != len(encrypted):
            raise RuntimeError(f"{key} was stored with {stored} bytes instead of {len(encrypted)}")

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def download(self, key: str, identity: str) -> bytes:
        import pyrage
        from pyrage import x25519

        encrypted = self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        return pyrage.decrypt(encrypted, [x25519.Identity.from_str(identity.strip())])


def sync_offsite(store: OffsiteStore, directory: str, policy: RetentionPolicy, now: float) -> tuple[int, int]:
    """Upload the local backups the off-site history keeps, and delete the
    off-site ones it no longer keeps. Returns (uploaded, deleted)."""
    local = local_backups(directory)
    remote = store.backups()
    kept = policy.keep(set(local) | set(remote), now)
    uploaded = deleted = 0
    for timestamp in sorted(set(local) - set(remote)):
        if timestamp in kept:
            store.upload(timestamp, local[timestamp])
            uploaded += 1
    for timestamp, item in sorted(remote.items()):
        if timestamp not in kept:
            store.delete(item["key"])
            deleted += 1
    return uploaded, deleted


# --- The backup run ----------------------------------------------------------

def run_backup(billing, config=None, now: float | None = None, store_factory: Callable | None = None) -> bool:
    """Back up, prune, and copy off-site if configured. The local backup is
    made first; an off-site failure is logged and recorded, and returns
    False. A failed local backup raises."""
    config = config or settings
    now = time.time() if now is None else now
    removed = billing.prune_job_diagnostics(config.billing.job_diagnostics_retention)
    if removed:
        logger.info("Removed %d expired job diagnostics records", removed)
    directory = config.billing.backup_dir
    extension = "dump" if getattr(billing, "_postgres", False) else "sqlite3"
    billing.backup(f"{directory}/billing-{int(now)}.{extension}")
    policy = RetentionPolicy.from_settings(config.billing)
    for path in prune_backups(directory, policy, now):
        logger.info("Removed billing backup %s", path.name)
    if not config.offsite.enabled:
        return True
    try:
        store = (store_factory or OffsiteStore.from_settings)(config)
        uploaded, deleted = sync_offsite(store, directory, policy, now)
    except Exception as error:
        logger.exception("Could not copy billing backups off-site")
        billing.set_state(OFFSITE_FAILURE_KEY, f"{int(time.time())} {describe_error(error)}")
        return False
    logger.info("Off-site backups: %d uploaded, %d deleted", uploaded, deleted)
    billing.set_state(OFFSITE_SUCCESS_KEY, str(int(time.time())))
    billing.set_state(OFFSITE_FAILURE_KEY, "")
    return True


def describe_error(error: Exception) -> str:
    """A short description without secrets or provider responses."""
    response = getattr(error, "response", None)
    if isinstance(response, dict) and response.get("Error", {}).get("Code"):
        return f"{type(error).__name__}: {response['Error']['Code']}"
    if isinstance(error, RuntimeError):
        return str(error)
    return type(error).__name__


def offsite_status(billing) -> dict | None:
    """The last off-site copy, for `app.admin status`; None when off."""
    if not settings.offsite.enabled:
        return None
    succeeded = billing.get_state(OFFSITE_SUCCESS_KEY)
    failure = billing.get_state(OFFSITE_FAILURE_KEY) or ""
    failed_at, _, error = failure.partition(" ")
    return {
        "succeeded_at": int(succeeded) if succeeded else None,
        "failed_at": int(failed_at) if failed_at else None,
        "error": error or None,
    }


# --- Command line --------------------------------------------------------------

def _when(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def keygen() -> int:
    from pyrage import x25519

    identity = x25519.Identity.generate()
    print(f"OFFSITE_BACKUP_RECIPIENT={identity.to_public()}")
    print()
    print("Private key, the only way to read the off-site backups. Store it away from this server,")
    print("for example in a password manager, and do not put it in .env or secrets/:")
    print()
    print(identity)
    return 0


def list_backups(store_factory: Callable | None = None) -> int:
    local = local_backups(settings.billing.backup_dir)
    print(f"Local ({settings.billing.backup_dir}): {len(local)}")
    # Numbered newest first, as `scripts/server.py restore --from local` counts them.
    for number, (timestamp, path) in enumerate(sorted(local.items(), reverse=True), 1):
        print(f"  {number:>3}. {_when(timestamp)}  {path.name}  {path.stat().st_size:,} bytes")
    if not settings.offsite.enabled:
        print("Off-site: off (OFFSITE_BACKUP_BUCKET is not set)")
        return 0
    remote = (store_factory or OffsiteStore.from_settings)(settings).backups()
    print(f"Off-site ({settings.offsite.bucket}): {len(remote)}")
    # Numbered newest first, as `fetch <number>` counts them.
    for number, (timestamp, item) in enumerate(sorted(remote.items(), reverse=True), 1):
        print(f"  {number:>3}. {_when(timestamp)}  {item['key']}  {item['size']:,} bytes")
    return 0


def fetch(name: str, identity: str, store_factory: Callable | None = None) -> int:
    """Download and decrypt one off-site backup into BILLING_BACKUP_DIR's
    sibling folder `restore`, and check it."""
    if not settings.offsite.enabled:
        print("Off-site backups are off: OFFSITE_BACKUP_BUCKET is not set.", file=sys.stderr)
        return 1
    # Key files from age-keygen start with comment lines, and PowerShell may
    # put a byte order mark before what it pipes.
    lines = identity.replace("﻿", "").splitlines()
    keys = [line.strip() for line in lines if line.strip().startswith("AGE-SECRET-KEY-")]
    if not keys:
        print("Send the private key (AGE-SECRET-KEY-...) on standard input.", file=sys.stderr)
        return 1
    store = (store_factory or OffsiteStore.from_settings)(settings)
    remote = store.backups()
    if not remote:
        print("There are no off-site backups.", file=sys.stderr)
        return 1
    if name == "latest":
        timestamp = max(remote)
    elif name.isdigit():
        newest_first = sorted(remote, reverse=True)
        if not 1 <= int(name) <= len(newest_first):
            print(f"There are {len(newest_first)} off-site backups: choose 1 (the newest) to {len(newest_first)}. "
                  "See: python -m app.backups list", file=sys.stderr)
            return 1
        timestamp = newest_first[int(name) - 1]
    else:
        match = re.search(r"billing-(\d+)", name)
        timestamp = int(match.group(1)) if match else -1
        if timestamp not in remote:
            print(f"No off-site backup named {name}. See: python -m app.backups list", file=sys.stderr)
            return 1
    data = store.download(remote[timestamp]["key"], keys[0])
    extension = remote[timestamp]["key"].rsplit(".", 2)[-2]
    target = Path(settings.billing.backup_dir).parent / "restore" / f"billing-{timestamp}.{extension}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    if extension == "dump":
        try:
            subprocess.run(["pg_restore", "--list", str(target)], check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError) as error:
            print(f"{target} failed pg_restore validation: {error}", file=sys.stderr)
            return 1
    else:
        with closing(sqlite3.connect(f"file:{target}?mode=ro", uri=True)) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            print(f"{target} failed its integrity check: {result}", file=sys.stderr)
            return 1
    print(f"Backup from {_when(timestamp)} saved and checked: {target}")
    return 0


def copy_to(url_file: str, directory: str | None = None) -> int:
    """Restore the newest local backup into another PostgreSQL database, such
    as a managed one when moving to several machines. The URL, with password
    and TLS options, is read from a file, so it is in no command line. The
    restore is one transaction, so a database that is not empty is left as
    it was."""
    from urllib.parse import urlsplit

    from app.billing import postgres_tool_arguments, run_postgres_tool

    try:
        url = Path(url_file).read_text(encoding="utf-8").strip()
    except OSError as error:
        print(f"Cannot read {url_file}: {type(error).__name__}", file=sys.stderr)
        return 1
    if not url.startswith(("postgres://", "postgresql://")):
        print(f"{url_file} does not hold a postgresql:// URL", file=sys.stderr)
        return 1
    dumps = {stamp: path for stamp, path in local_backups(directory or settings.billing.backup_dir).items()
             if path.suffix == ".dump"}
    if not dumps:
        print("There is no backup to copy; make one with: python -m app.backups now", file=sys.stderr)
        return 1
    newest = max(dumps)
    target, environment = postgres_tool_arguments(url)
    try:
        run_postgres_tool(
            ["pg_restore", "--no-owner", "--exit-on-error", "--single-transaction", "--dbname", target,
             str(dumps[newest])],
            environment,
        )
    except (OSError, RuntimeError) as error:
        print(f"Could not copy the backup: {error}. The target database was not changed.", file=sys.stderr)
        return 1
    print(f"Copied {dumps[newest].name} ({_when(newest)}) into the database at {urlsplit(url).hostname}.")
    return 0


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.backups", description="Billing database backups.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("now", help="back up now, prune, and copy off-site if configured")
    commands.add_parser("list", help="list local and off-site backups")
    commands.add_parser("keygen", help="print a new key pair for OFFSITE_BACKUP_RECIPIENT")
    fetch_parser = commands.add_parser(
        "fetch", help="download and decrypt an off-site backup; the private key is read from standard input"
    )
    fetch_parser.add_argument("name", help="latest, a name from `list`, or its number there: 1 is the newest")
    copy_parser = commands.add_parser(
        "copy-to", help="restore the newest backup into another, empty PostgreSQL database, such as a managed one"
    )
    copy_parser.add_argument("url_file", help="a file with that database's URL, with its password and TLS options")
    args = parser.parse_args(arguments)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.command == "keygen":
        return keygen()
    if args.command == "list":
        return list_backups()
    if args.command == "fetch":
        return fetch(args.name, sys.stdin.read())
    if args.command == "copy-to":
        return copy_to(args.url_file)
    from app.billing import Billing

    if not run_backup(Billing()):
        print("The local backup is done; copying it off-site failed (see above).", file=sys.stderr)
        return 1
    print("Backup done" + (", and copied off-site." if settings.offsite.enabled else "."))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
