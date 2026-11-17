import hashlib
import math
import os
import sqlite3
import subprocess
import threading
import time
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from app.config import duration_seconds as parse_duration, settings

DELETED_USER_ID = "deleted"
TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}
# Kinds of minute_ledger entries. Each change to a balance is one entry.
# "free" renews the monthly minutes and "expired" takes back the ones left
# unused at the end of the period.
LEDGER_KINDS = ("purchase", "free", "used", "voided", "admin", "expired", "subscription", "welcome")
# A user's two balances, the minute_ledger pools: monthly minutes, renewed
# every FREE_MINUTES_INTERVAL and spent first, and paid minutes, which never
# expire.
MONTHLY = "monthly"
PAID = "paid"
# billing_state key: when this database started recording minute_ledger.
LEDGER_SINCE_KEY = "minute_ledger_since"
SCHEMA_VERSION = 1
CONNECT_TIMEOUT_SECONDS = 10


class _PostgresConnection:
    """Small DB-API adapter retaining qmark SQL for legacy SQLite callers."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, query, parameters=()):
        return self._connection.execute(query.replace("?", "%s"), parameters)

    def executemany(self, query, parameters):
        return self._connection.executemany(query.replace("?", "%s"), parameters)

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, *args):
        return self._connection.__exit__(*args)

    def close(self):
        return self._connection.close()

    def __getattr__(self, name):
        return getattr(self._connection, name)


def transcription_cost(duration_seconds: float) -> int:
    minutes = duration_seconds / 60
    return 1 if minutes < 2 else math.floor(minutes)


def cancelled_cost(full_cost: int, progress: float) -> int:
    if progress < 0.05:
        return 0
    return math.ceil(0.6 * full_cost * progress)


class Billing:
    def __init__(self, database_path: str | None = None, create_schema: bool = True):
        # The database is created on first use, not here: modules create a
        # Billing when imported, also in services and tests without access
        # to the billing directory.
        self.database_path = database_path or settings.database.path
        self.database_url = None if database_path else (
            settings.secrets.database_url.get_secret_value()
            if settings.secrets.database_url else settings.database.url
        )
        self._postgres = bool(self.database_url and not database_path)
        # The admin web page's roles may not create or change tables; the
        # API keeps the schema.
        self._ready = not create_schema
        self._ready_lock = threading.Lock()

    def _create_schema(self) -> None:
        if self._postgres:
            with self._connect_raw() as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended('billing-schema', 0))"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS billing_schema (
                        version INTEGER PRIMARY KEY,
                        applied_at BIGINT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS users (
                        user_id TEXT PRIMARY KEY, email TEXT UNIQUE, minutes INTEGER NOT NULL DEFAULT 0,
                        free_grant_at BIGINT NOT NULL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS purchases (
                        product_id TEXT NOT NULL, purchase_token TEXT NOT NULL, user_id TEXT NOT NULL,
                        minutes INTEGER NOT NULL, order_id TEXT, voided_at BIGINT,
                        PRIMARY KEY (product_id, purchase_token)
                    );
                    CREATE TABLE IF NOT EXISTS transcription_reservations (
                        job_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, reserved_minutes INTEGER NOT NULL CHECK (reserved_minutes > 0),
                        charged_minutes INTEGER, status TEXT NOT NULL CHECK (status IN ('reserved','released','settled'))
                    );
                    CREATE TABLE IF NOT EXISTS deleted_users (user_hash TEXT PRIMARY KEY, free_grant_at BIGINT NOT NULL);
                    CREATE TABLE IF NOT EXISTS job_diagnostics (
                        job_id TEXT PRIMARY KEY, user_id TEXT, source TEXT, source_url TEXT,
                        duration_seconds DOUBLE PRECISION, cost_minutes INTEGER, status TEXT NOT NULL, error TEXT,
                        backend TEXT, outsourced INTEGER NOT NULL DEFAULT 0, requeue_reason TEXT,
                        created_at BIGINT NOT NULL, started_at BIGINT, finished_at BIGINT, updated_at BIGINT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS billing_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS minute_ledger (
                        id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL,
                        minutes INTEGER NOT NULL, at BIGINT NOT NULL, note TEXT
                    );
                    CREATE TABLE IF NOT EXISTS admin_actions (
                        id BIGSERIAL PRIMARY KEY, at BIGINT NOT NULL, actor TEXT NOT NULL,
                        action TEXT NOT NULL, target TEXT, detail TEXT, reason TEXT
                    );
                    CREATE TABLE IF NOT EXISTS admin_web_caps (
                        id BIGSERIAL PRIMARY KEY, at BIGINT NOT NULL, actor TEXT NOT NULL,
                        mode TEXT NOT NULL CHECK (mode IN ('read-only', 'disabled')), reason TEXT
                    );
                    CREATE TABLE IF NOT EXISTS announcements (
                        id TEXT PRIMARY KEY, content TEXT NOT NULL, starts_at BIGINT NOT NULL, ends_at BIGINT,
                        created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL, created_by TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS minute_ledger_user_index ON minute_ledger (user_id);
                    CREATE INDEX IF NOT EXISTS minute_ledger_at_index ON minute_ledger (at);
                    CREATE INDEX IF NOT EXISTS job_diagnostics_user_index ON job_diagnostics (user_id);
                    CREATE INDEX IF NOT EXISTS job_diagnostics_created_index ON job_diagnostics (created_at);
                    -- Added with piano arrangements; NULL in older rows means a transcription.
                    ALTER TABLE job_diagnostics ADD COLUMN IF NOT EXISTS mode TEXT;
                    -- Added with monthly minutes. A reservation holds monthly_minutes of
                    -- its cost from the monthly balance; monthly_ended is 1 once their
                    -- period has ended, so that those it does not use expire.
                    ALTER TABLE users ADD COLUMN IF NOT EXISTS monthly_minutes INTEGER NOT NULL DEFAULT 0
                        CHECK (monthly_minutes >= 0);
                    ALTER TABLE transcription_reservations ADD COLUMN IF NOT EXISTS monthly_minutes INTEGER NOT NULL DEFAULT 0
                        CHECK (monthly_minutes >= 0);
                    ALTER TABLE transcription_reservations ADD COLUMN IF NOT EXISTS monthly_ended INTEGER NOT NULL DEFAULT 0;
                    ALTER TABLE minute_ledger ADD COLUMN IF NOT EXISTS pool TEXT NOT NULL DEFAULT 'paid'
                        CHECK (pool IN ('paid', 'monthly'));
                    -- Added with developer accounts: 1 when an administrator made the
                    -- account a developer's, whose jobs are never charged.
                    ALTER TABLE users ADD COLUMN IF NOT EXISTS developer INTEGER NOT NULL DEFAULT 0;

                    -- Added with subscriptions. A subscription is known by a
                    -- hash of its purchase token (id), so that periods name it
                    -- without the token, which the admin web page never reads.
                    CREATE TABLE IF NOT EXISTS subscriptions (
                        id TEXT PRIMARY KEY, purchase_token TEXT NOT NULL UNIQUE, user_id TEXT NOT NULL,
                        product_id TEXT NOT NULL, state TEXT NOT NULL, expires_at BIGINT NOT NULL,
                        auto_renewing INTEGER NOT NULL DEFAULT 0, latest_order_id TEXT, replaced_by TEXT,
                        revoked_at BIGINT, checked_at BIGINT NOT NULL, created_at BIGINT NOT NULL,
                        acknowledged INTEGER NOT NULL DEFAULT 1
                    );
                    -- One row per paid order: the minutes it gave (NULL for
                    -- unlimited) for its period, and those left unused when it ended.
                    CREATE TABLE IF NOT EXISTS subscription_periods (
                        order_id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL, user_id TEXT NOT NULL,
                        product_id TEXT NOT NULL, minutes INTEGER CHECK (minutes > 0), starts_at BIGINT NOT NULL,
                        ends_at BIGINT NOT NULL, unused_minutes INTEGER, voided_at BIGINT
                    );
                    -- Jobs of an unlimited subscription, to tell heavy use
                    -- and to charge them when its order is refunded. 0 minutes
                    -- once the job was released.
                    CREATE TABLE IF NOT EXISTS unlimited_usage (
                        job_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, order_id TEXT NOT NULL,
                        minutes INTEGER NOT NULL CHECK (minutes >= 0), at BIGINT NOT NULL
                    );
                    -- Accounts given WELCOME_MINUTES, by hash, kept after the
                    -- account is deleted so that a new one is not given them again.
                    CREATE TABLE IF NOT EXISTS welcome_claims (user_hash TEXT PRIMARY KEY, at BIGINT NOT NULL);
                    CREATE INDEX IF NOT EXISTS subscriptions_user_index ON subscriptions (user_id);
                    CREATE INDEX IF NOT EXISTS subscriptions_expires_index ON subscriptions (expires_at);
                    CREATE INDEX IF NOT EXISTS subscription_periods_user_index ON subscription_periods (user_id, ends_at);
                    CREATE INDEX IF NOT EXISTS subscription_periods_subscription_index ON subscription_periods (subscription_id);
                    CREATE INDEX IF NOT EXISTS unlimited_usage_user_index ON unlimited_usage (user_id, at);
                    CREATE INDEX IF NOT EXISTS unlimited_usage_order_index ON unlimited_usage (order_id);
                    INSERT INTO billing_state (key, value) VALUES ('minute_ledger_since', '{ledger_since}')
                    ON CONFLICT (key) DO NOTHING
                    ;
                    INSERT INTO billing_schema (version, applied_at)
                    VALUES ({schema_version}, {applied_at})
                    ON CONFLICT (version) DO NOTHING
                    """.format(
                        ledger_since=int(time.time()),
                        schema_version=SCHEMA_VERSION,
                        applied_at=int(time.time()),
                    ),
                )
                _sync_admin_web_roles(connection)
            return
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._open()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    email TEXT UNIQUE,
                    minutes INTEGER NOT NULL DEFAULT 0,
                    free_grant_at INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS purchases (
                    product_id TEXT NOT NULL,
                    purchase_token TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    minutes INTEGER NOT NULL,
                    PRIMARY KEY (product_id, purchase_token)
                );
                CREATE TABLE IF NOT EXISTS transcription_reservations (
                    job_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    reserved_minutes INTEGER NOT NULL,
                    charged_minutes INTEGER,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deleted_users (
                    user_hash TEXT PRIMARY KEY,
                    free_grant_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS job_diagnostics (
                    job_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    source TEXT,
                    source_url TEXT,
                    duration_seconds REAL,
                    cost_minutes INTEGER,
                    status TEXT NOT NULL,
                    error TEXT,
                    backend TEXT,
                    outsourced INTEGER NOT NULL DEFAULT 0,
                    requeue_reason TEXT,
                    created_at INTEGER NOT NULL,
                    started_at INTEGER,
                    finished_at INTEGER,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS billing_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS minute_ledger (
                    id INTEGER PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    minutes INTEGER NOT NULL,
                    at INTEGER NOT NULL,
                    note TEXT
                );
                CREATE TABLE IF NOT EXISTS admin_actions (
                    id INTEGER PRIMARY KEY,
                    at INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT,
                    detail TEXT,
                    reason TEXT
                );
                CREATE TABLE IF NOT EXISTS admin_web_caps (
                    id INTEGER PRIMARY KEY,
                    at INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK (mode IN ('read-only', 'disabled')),
                    reason TEXT
                );
                CREATE TABLE IF NOT EXISTS announcements (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    starts_at INTEGER NOT NULL,
                    ends_at INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    created_by TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS minute_ledger_user_index ON minute_ledger (user_id);
                CREATE INDEX IF NOT EXISTS minute_ledger_at_index ON minute_ledger (at);
                CREATE INDEX IF NOT EXISTS job_diagnostics_user_index ON job_diagnostics (user_id);
                CREATE INDEX IF NOT EXISTS job_diagnostics_created_index ON job_diagnostics (created_at);
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id TEXT PRIMARY KEY,
                    purchase_token TEXT NOT NULL UNIQUE,
                    user_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    auto_renewing INTEGER NOT NULL DEFAULT 0,
                    latest_order_id TEXT,
                    replaced_by TEXT,
                    revoked_at INTEGER,
                    checked_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    acknowledged INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS subscription_periods (
                    order_id TEXT PRIMARY KEY,
                    subscription_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    minutes INTEGER CHECK (minutes > 0),
                    starts_at INTEGER NOT NULL,
                    ends_at INTEGER NOT NULL,
                    unused_minutes INTEGER,
                    voided_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS unlimited_usage (
                    job_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    minutes INTEGER NOT NULL CHECK (minutes >= 0),
                    at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS welcome_claims (
                    user_hash TEXT PRIMARY KEY,
                    at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS subscriptions_user_index ON subscriptions (user_id);
                CREATE INDEX IF NOT EXISTS subscriptions_expires_index ON subscriptions (expires_at);
                CREATE INDEX IF NOT EXISTS subscription_periods_user_index ON subscription_periods (user_id, ends_at);
                CREATE INDEX IF NOT EXISTS subscription_periods_subscription_index ON subscription_periods (subscription_id);
                CREATE INDEX IF NOT EXISTS unlimited_usage_user_index ON unlimited_usage (user_id, at);
                CREATE INDEX IF NOT EXISTS unlimited_usage_order_index ON unlimited_usage (order_id);
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
            if "email" not in columns:
                connection.execute("ALTER TABLE users ADD COLUMN email TEXT")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email_index ON users (email)")
            connection.execute(
                "INSERT OR IGNORE INTO billing_state (key, value) VALUES (?, ?)",
                (LEDGER_SINCE_KEY, str(int(time.time()))),
            )
            purchase_columns = {row[1] for row in connection.execute("PRAGMA table_info(purchases)")}
            if "order_id" not in purchase_columns:
                connection.execute("ALTER TABLE purchases ADD COLUMN order_id TEXT")
            if "voided_at" not in purchase_columns:
                connection.execute("ALTER TABLE purchases ADD COLUMN voided_at INTEGER")
            job_columns = {row[1] for row in connection.execute("PRAGMA table_info(job_diagnostics)")}
            if "mode" not in job_columns:
                connection.execute("ALTER TABLE job_diagnostics ADD COLUMN mode TEXT")
            if "monthly_minutes" not in columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN monthly_minutes INTEGER NOT NULL DEFAULT 0 CHECK (monthly_minutes >= 0)"
                )
            if "developer" not in columns:
                connection.execute("ALTER TABLE users ADD COLUMN developer INTEGER NOT NULL DEFAULT 0")
            reservation_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(transcription_reservations)")
            }
            if "monthly_minutes" not in reservation_columns:
                connection.execute(
                    "ALTER TABLE transcription_reservations ADD COLUMN monthly_minutes INTEGER NOT NULL DEFAULT 0 "
                    "CHECK (monthly_minutes >= 0)"
                )
            if "monthly_ended" not in reservation_columns:
                connection.execute(
                    "ALTER TABLE transcription_reservations ADD COLUMN monthly_ended INTEGER NOT NULL DEFAULT 0"
                )
            ledger_columns = {row[1] for row in connection.execute("PRAGMA table_info(minute_ledger)")}
            if "pool" not in ledger_columns:
                connection.execute(
                    "ALTER TABLE minute_ledger ADD COLUMN pool TEXT NOT NULL DEFAULT 'paid' "
                    "CHECK (pool IN ('paid', 'monthly'))"
                )

    def _connect_raw(self):
        import psycopg
        from psycopg.rows import dict_row
        # A database that cannot be reached fails the request instead of
        # holding its thread until the operating system gives up.
        connection = psycopg.connect(self._postgres_url(), connect_timeout=CONNECT_TIMEOUT_SECONDS)
        connection.row_factory = dict_row
        connection.autocommit = False
        return connection

    def _postgres_url(self) -> str:
        # Compose deliberately keeps the password out of the URL and exposes
        # it as a Docker secret. A complete database_url secret still
        # takes precedence in __init__.
        url = self.database_url
        password_file = Path(settings.secrets_dir) / "postgres_password"
        if url and urlsplit(url).password is None and password_file.is_file():
            parts = urlsplit(url)
            password = password_file.read_text(encoding="utf-8").strip()
            netloc = f"{parts.username}:{quote(password, safe='')}@{parts.hostname}"
            if parts.port:
                netloc += f":{parts.port}"
            url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
        return url

    def _postgres_tool_arguments(self) -> tuple[str, dict[str, str]]:
        return postgres_tool_arguments(self._postgres_url())

    def _open(self):
        if self._postgres:
            return _PostgresConnection(self._connect_raw())
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        # Sync the WAL at every commit, so a power cut loses no committed
        # purchase or charge. Usually SQLite's default, but not on every build.
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _begin_write(self, connection) -> None:
        if self._postgres:
            # Serialize balance-changing transactions and make reserve/settle/
            # purchase safe across API and worker processes.
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('billing-write', 0))")
        else:
            connection.execute("BEGIN IMMEDIATE")

    def check(self) -> None:
        """Raise if the database cannot be opened and read."""
        with closing(self._connect()) as connection:
            connection.execute("SELECT 1 FROM users LIMIT 1").fetchall()

    def _connect(self) -> sqlite3.Connection:
        if not self._ready:
            with self._ready_lock:
                if not self._ready:
                    self._create_schema()
                    self._ready = True
        return self._open()

    @staticmethod
    def _ensure_user(connection: sqlite3.Connection, user_id: str, email: str | None = None) -> None:
        created = connection.execute(
            "INSERT INTO users (user_id) VALUES (?) ON CONFLICT (user_id) DO NOTHING",
            (user_id,),
        ).rowcount
        if created:
            # A recreated account keeps the free-grant time of the deleted one,
            # so deleting an account cannot be used to reset free minutes.
            _purge_expired_deleted_users(connection)
            deleted = connection.execute(
                "SELECT free_grant_at FROM deleted_users WHERE user_hash = ?",
                (_user_hash(user_id),),
            ).fetchone()
            if deleted:
                connection.execute(
                    "UPDATE users SET free_grant_at = ? WHERE user_id = ?",
                    (deleted["free_grant_at"], user_id),
                )
                connection.execute(
                    "DELETE FROM deleted_users WHERE user_hash = ?",
                    (_user_hash(user_id),),
                )
            _give_welcome_minutes(connection, user_id)
        if email:
            connection.execute(
                "UPDATE users SET email = ? WHERE user_id = ?",
                (email, user_id),
            )

    def renew_monthly_minutes(self, user_id: str, email: str | None = None) -> int:
        """Renew the user's monthly minutes if their period has ended: the
        unused ones expire and FREE_MINUTES are given. Returns the minutes
        given. It is one transaction, so a crash leaves either the old
        period or the new one."""
        if settings.billing.provider == "none":
            return 0
        if not _free_minutes_enabled() and not _welcome_minutes_enabled():
            # Then this only lets left-over ones expire. Job status polls
            # call it, so it takes the write lock only when there are some.
            with closing(self._connect()) as connection:
                user = connection.execute(
                    "SELECT monthly_minutes FROM users WHERE user_id = ?", (user_id,)
                ).fetchone()
            if user is None or user["monthly_minutes"] <= 0:
                return 0
        with self._connect() as connection:
            self._begin_write(connection)
            if _free_minutes_enabled() or _welcome_minutes_enabled():
                self._ensure_user(connection, user_id, email)
            return _renew_monthly(connection, user_id)

    def balances(self, user_id: str) -> dict[str, int]:
        """Available minutes, less what running jobs hold: in all, monthly,
        and paid. Paid minutes, and with them the total, are negative after
        a refunded purchase whose minutes were already used."""
        empty = {"minutes": 0, "monthly_minutes": 0, "paid_minutes": 0}
        if settings.billing.provider == "none":
            return empty
        with closing(self._connect()) as connection:
            user = connection.execute(
                "SELECT minutes, monthly_minutes FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if user is None:
                return empty
            held = _held(connection, user_id)
        monthly = int(user["monthly_minutes"]) - held["monthly"]
        paid = int(user["minutes"]) - (held["total"] - held["monthly"])
        return {"minutes": monthly + paid, "monthly_minutes": monthly, "paid_minutes": paid}

    def get_balance(self, user_id: str) -> int:
        """Available minutes, monthly and paid. Negative after a refunded
        purchase whose minutes were already used."""
        return self.balances(user_id)["minutes"]

    def free_minutes_status(self, user_id: str) -> dict[str, int | None]:
        if settings.billing.provider == "none" or (settings.billing.free_minutes or 0) <= 0:
            return {
                "free_minutes_seconds_until_next_grant": None,
                "free_minutes_next_grant_at": None,
            }
        with self._connect() as connection:
            user = connection.execute(
                "SELECT free_grant_at FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        last_grant = int(user["free_grant_at"]) if user else 0
        next_grant_at = last_grant + period_seconds()
        return {
            "free_minutes_seconds_until_next_grant": max(0, next_grant_at - int(time.time())),
            "free_minutes_next_grant_at": next_grant_at,
        }

    def reserve(self, user_id: str, job_id: str, cost: int, email: str | None = None) -> int | None:
        """Hold the job's cost until it is settled or released: from the
        monthly minutes first, the rest from the paid ones. Returns the
        minutes still available."""
        if settings.billing.provider == "none" or cost <= 0:
            return None
        with self._connect() as connection:
            self._begin_write(connection)
            self._ensure_user(connection, user_id, email)
            # In the same transaction, so a job is never paid with minutes
            # of a period that has just ended.
            _renew_monthly(connection, user_id)
            user = connection.execute(
                "SELECT minutes, monthly_minutes, developer FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            held = _held(connection, user_id)
            available = user["minutes"] + user["monthly_minutes"] - held["total"]
            if user["developer"]:
                # A developer's jobs hold and cost nothing: without a
                # reservation, settling and releasing them change nothing.
                return int(available)
            period = _current_period(connection, user_id)
            if period is not None and period["minutes"] is None:
                # An unlimited subscription: nothing is held or charged, and
                # nothing refused. The job is counted, to tell heavy use, and
                # charged after all if the order is refunded.
                _use_unlimited(connection, user_id, job_id, cost, period["order_id"])
                return int(available)
            if available < cost:
                raise InsufficientMinutes()
            from_monthly = min(cost, max(0, user["monthly_minutes"] - held["monthly"]))
            # A failed job, whose reservation was released, can be queued
            # again under its ID (python -m app.admin jobs requeue).
            reserved_now = connection.execute(
                """INSERT INTO transcription_reservations
                   (job_id, user_id, reserved_minutes, monthly_minutes, monthly_ended, status)
                   VALUES (?, ?, ?, ?, 0, 'reserved')
                   ON CONFLICT (job_id) DO UPDATE SET
                       reserved_minutes = excluded.reserved_minutes,
                       monthly_minutes = excluded.monthly_minutes,
                       monthly_ended = 0,
                       charged_minutes = NULL,
                       status = 'reserved'
                   WHERE transcription_reservations.status = 'released'
                     AND transcription_reservations.user_id = excluded.user_id""",
                (job_id, user_id, cost, from_monthly),
            ).rowcount
            if not reserved_now:
                raise ValueError(f"Job {job_id} already has a reservation")
            return int(available - cost)

    def settle_success(self, job_id: str) -> None:
        self._settle(job_id, None)

    def settle_cancellation(self, job_id: str, charged_minutes: int) -> None:
        self._settle(job_id, charged_minutes)

    def release(self, job_id: str) -> None:
        with self._connect() as connection:
            self._begin_write(connection)
            # A job of an unlimited subscription no longer counts.
            connection.execute("UPDATE unlimited_usage SET minutes = 0 WHERE job_id = ?", (job_id,))
            reservation = connection.execute(
                "SELECT * FROM transcription_reservations WHERE job_id = ? AND status = 'reserved'",
                (job_id,),
            ).fetchone()
            if not reservation:
                return
            connection.execute(
                "UPDATE transcription_reservations SET status = 'released' WHERE job_id = ?",
                (job_id,),
            )
            _expire_stale_hold(connection, reservation, 0)

    def job_seen(self, job_id: str) -> bool:
        """Whether a job with this ID was ever queued: it has a diagnostics
        record or a reservation that was not given back, which outlive the
        Redis job. A submission that failed before queuing has neither."""
        with closing(self._connect()) as connection:
            return connection.execute(
                """SELECT 1 FROM transcription_reservations WHERE job_id = ? AND status != 'released'
                   UNION ALL SELECT 1 FROM job_diagnostics WHERE job_id = ? LIMIT 1""",
                (job_id, job_id),
            ).fetchone() is not None

    def reserved_job_ids(self) -> list[str]:
        with self._connect() as connection:
            reservations = connection.execute(
                "SELECT job_id FROM transcription_reservations WHERE status = 'reserved'",
            ).fetchall()
        return [reservation["job_id"] for reservation in reservations]

    def _settle(self, job_id: str, charged_minutes: int | None) -> None:
        with self._connect() as connection:
            self._begin_write(connection)
            if charged_minutes is not None:
                # A cancelled job of an unlimited subscription counts as far
                # as it ran.
                connection.execute(
                    "UPDATE unlimited_usage SET minutes = MIN(minutes, ?) WHERE job_id = ?"
                    if not self._postgres else
                    "UPDATE unlimited_usage SET minutes = LEAST(minutes, ?) WHERE job_id = ?",
                    (max(0, charged_minutes), job_id),
                )
            reservation = connection.execute(
                "SELECT * FROM transcription_reservations WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if not reservation or reservation["status"] != "reserved":
                return
            charge = reservation["reserved_minutes"] if charged_minutes is None else min(
                max(0, charged_minutes), reservation["reserved_minutes"]
            )
            user = connection.execute(
                "SELECT monthly_minutes FROM users WHERE user_id = ?",
                (reservation["user_id"],),
            ).fetchone()
            # Monthly minutes first, never more than the reservation holds
            # of them or the balance has.
            from_monthly = min(charge, reservation["monthly_minutes"] or 0, user["monthly_minutes"] if user else 0)
            from_paid = charge - from_monthly
            connection.execute(
                "UPDATE users SET minutes = minutes - ?, monthly_minutes = monthly_minutes - ? WHERE user_id = ?",
                (from_paid, from_monthly, reservation["user_id"]),
            )
            connection.execute(
                """UPDATE transcription_reservations
                   SET status = 'settled', charged_minutes = ? WHERE job_id = ?""",
                (charge, job_id),
            )
            _log_minutes(connection, reservation["user_id"], "used", -from_monthly, job_id, MONTHLY)
            _log_minutes(connection, reservation["user_id"], "used", -from_paid, job_id)
            _expire_stale_hold(connection, reservation, from_monthly)

    def credit_purchase(
        self,
        user_id: str,
        product_id: str,
        token: str,
        order_id: str,
        email: str | None = None,
    ) -> int:
        minutes = settings.billing.products[product_id]
        with self._connect() as connection:
            self._begin_write(connection)
            existing = connection.execute(
                "SELECT 1 FROM purchases WHERE product_id = ? AND purchase_token = ?",
                (product_id, token),
            ).fetchone()
            if existing:
                return 0
            self._ensure_user(connection, user_id, email)
            connection.execute(
                "INSERT INTO purchases (product_id, purchase_token, order_id, user_id, minutes) VALUES (?, ?, ?, ?, ?)",
                (product_id, token, order_id, user_id, minutes),
            )
            connection.execute(
                "UPDATE users SET minutes = minutes + ? WHERE user_id = ?",
                (minutes, user_id),
            )
            _log_minutes(connection, user_id, "purchase", minutes, order_id)
            return minutes

    def void_purchase(self, token: str, order_id: str | None = None) -> tuple[str, int] | None:
        """Take back the minutes of a refunded or charged-back purchase, once:
        a product's, or a subscription order's (_void_subscription_order).
        The balance can go below zero when they were already used. Returns
        (user ID, minutes taken), or None if the token is unknown, already
        voided, or belonged to a deleted account."""
        with self._connect() as connection:
            self._begin_write(connection)
            purchase = connection.execute(
                "SELECT product_id, purchase_token, user_id, minutes, order_id FROM purchases WHERE purchase_token = ? AND voided_at IS NULL",
                (token,),
            ).fetchone()
            if purchase is None:
                return _void_subscription_order(connection, token, order_id)
            connection.execute(
                "UPDATE purchases SET voided_at = ? WHERE product_id = ? AND purchase_token = ?",
                (int(time.time()), purchase["product_id"], purchase["purchase_token"]),
            )
            if purchase["user_id"] == DELETED_USER_ID:
                return None
            connection.execute(
                "UPDATE users SET minutes = minutes - ? WHERE user_id = ?",
                (purchase["minutes"], purchase["user_id"]),
            )
            _log_minutes(connection, purchase["user_id"], "voided", -purchase["minutes"], purchase["order_id"])
            return purchase["user_id"], purchase["minutes"]

    # --- Subscriptions ---------------------------------------------------------

    def record_subscription(self, user_id: str | None, info: "SubscriptionInfo", email: str | None = None) -> dict:
        """Save what Google Play reports of a subscription, and start the
        period of a paid order not seen before: its minutes replace the
        monthly ones, which expire. One transaction, so a crash leaves the
        order either given or not, and a repeat gives it once. user_id None
        refreshes the subscription for the account that has it. Raises
        SubscriptionOwned when another account has it."""
        now = int(time.time())
        subscription_id = subscription_key(info.token)
        with self._connect() as connection:
            self._begin_write(connection)
            existing = connection.execute(
                "SELECT user_id FROM subscriptions WHERE id = ?", (subscription_id,)
            ).fetchone()
            owner = existing["user_id"] if existing else None
            if user_id is None:
                if owner is None or owner == DELETED_USER_ID:
                    return {"status": "unknown", "minutes": 0, "user_id": None}
                user_id = owner
            elif info.account_tag and info.account_tag != account_tag(user_id):
                # Bought while another account of this app was signed in.
                raise SubscriptionOwned()
            elif owner is not None and owner != user_id:
                if owner != DELETED_USER_ID or info.account_tag != account_tag(user_id):
                    raise SubscriptionOwned()
                # The same person, whose account was deleted and made again:
                # the subscription, and its current period, come back.
                connection.execute(
                    "UPDATE subscriptions SET user_id = ? WHERE id = ?", (user_id, subscription_id)
                )
                connection.execute(
                    "UPDATE subscription_periods SET user_id = ? WHERE subscription_id = ? AND ends_at > ?",
                    (user_id, subscription_id, now),
                )
            self._ensure_user(connection, user_id, email)
            connection.execute(
                """INSERT INTO subscriptions
                   (id, purchase_token, user_id, product_id, state, expires_at, auto_renewing,
                    latest_order_id, checked_at, created_at, acknowledged)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (id) DO UPDATE SET
                       product_id = excluded.product_id, state = excluded.state,
                       expires_at = excluded.expires_at, auto_renewing = excluded.auto_renewing,
                       latest_order_id = COALESCE(excluded.latest_order_id, subscriptions.latest_order_id),
                       checked_at = excluded.checked_at, acknowledged = excluded.acknowledged""",
                (subscription_id, info.token, user_id, info.product_id, info.state, info.expires_at,
                 int(info.auto_renewing), info.latest_order_id, now, now,
                 # A paid one waits to be acknowledged; others need nothing.
                 int(info.acknowledged or not info.paid)),
            )
            subscription = connection.execute(
                "SELECT revoked_at, replaced_by FROM subscriptions WHERE id = ?", (subscription_id,)
            ).fetchone()
            plan = settings.billing.subscriptions.get(info.product_id)
            known = info.latest_order_id is not None and connection.execute(
                "SELECT 1 FROM subscription_periods WHERE order_id = ?", (info.latest_order_id,)
            ).fetchone() is not None
            given = None
            if (
                info.paid
                and info.expires_at > now
                and subscription["revoked_at"] is None
                and subscription["replaced_by"] is None
                and plan is not None
                and info.latest_order_id is not None
                and not known
            ):
                if info.linked_token:
                    # An upgrade or downgrade: the subscription it replaces ends now.
                    linked = subscription_key(info.linked_token)
                    connection.execute(
                        "UPDATE subscriptions SET replaced_by = ? WHERE id = ? AND user_id = ?",
                        (subscription_id, linked, user_id),
                    )
                    connection.execute(
                        "UPDATE subscription_periods SET ends_at = ? "
                        "WHERE subscription_id = ? AND user_id = ? AND ends_at > ? AND voided_at IS NULL",
                        (now, linked, user_id, now),
                    )
                given = _start_subscription_period(
                    connection, user_id, subscription_id, info.product_id, info.latest_order_id,
                    now, info.expires_at, plan.minutes,
                )
            elif known:
                # A known order: its period follows Google's expiry, kept
                # through a grace period, ended at once when not entitled.
                if info.entitled and subscription["revoked_at"] is None:
                    connection.execute(
                        "UPDATE subscription_periods SET ends_at = ? "
                        "WHERE order_id = ? AND subscription_id = ? AND voided_at IS NULL",
                        (info.expires_at, info.latest_order_id, subscription_id),
                    )
                else:
                    ends_at = min(info.expires_at, now)
                    connection.execute(
                        "UPDATE subscription_periods SET ends_at = ? "
                        "WHERE subscription_id = ? AND voided_at IS NULL AND ends_at > ?",
                        (ends_at, subscription_id, ends_at),
                    )
            return {"status": "started" if given is not None else "recorded",
                    "minutes": given or 0, "user_id": user_id}

    def subscriptions_to_check(self, now: int | None = None, limit: int = 100) -> list[str]:
        """Purchase tokens to read from Google Play again: those whose
        period ends soon or has ended, until they end for good, each read
        again more rarely the longer ago it expired; and those not yet
        acknowledged, as after a crash between saving and acknowledging,
        which Google would refund within three days."""
        now = int(time.time()) if now is None else now
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT purchase_token, expires_at, checked_at, acknowledged FROM subscriptions
                   WHERE replaced_by IS NULL AND revoked_at IS NULL AND user_id != ? AND (
                       acknowledged = 0
                       OR (state != 'EXPIRED' AND expires_at <= ? AND expires_at > ?))
                   ORDER BY checked_at LIMIT ?""",
                (DELETED_USER_ID, now + SUBSCRIPTION_CHECK_AHEAD, now - SUBSCRIPTION_CHECK_FOR, limit * 4),
            ).fetchall()
        due = []
        for row in rows:
            if not row["acknowledged"]:
                if now - int(row["checked_at"]) >= SUBSCRIPTION_CHECK_MIN_WAIT:
                    due.append(row["purchase_token"])
                continue
            overdue = max(0, now - int(row["expires_at"]))
            wait = min(SUBSCRIPTION_CHECK_MAX_WAIT, max(SUBSCRIPTION_CHECK_MIN_WAIT, overdue // 10))
            if now - int(row["checked_at"]) >= wait:
                due.append(row["purchase_token"])
        return due[:limit]

    def subscription_status(self, user_id: str) -> dict:
        """The user's subscription for the apps: the plan whose period runs
        now, whether it renews, and for an unlimited one, whether its use in
        the last 24 hours is heavy (see unlimited_heavy_use)."""
        empty = {"subscription": None, "unlimited": False, "unlimited_heavy_use": False}
        if settings.billing.provider == "none":
            return empty
        now = int(time.time())
        with closing(self._connect()) as connection:
            period = _current_period(connection, user_id, now)
            if period is None:
                return empty
            subscription = connection.execute(
                "SELECT state, auto_renewing FROM subscriptions WHERE id = ?", (period["subscription_id"],)
            ).fetchone()
            used = _unlimited_used(connection, user_id, now) if period["minutes"] is None else 0
        plan = settings.billing.subscriptions.get(period["product_id"])
        unlimited = period["minutes"] is None
        return {
            "subscription": {
                "product_id": period["product_id"],
                "name": plan.name if plan else period["product_id"],
                "minutes": period["minutes"],
                "level": settings.billing.subscription_levels().get(period["product_id"], 0),
                "period_ends_at": int(period["ends_at"]),
                "auto_renewing": bool(subscription and subscription["auto_renewing"]),
                "state": subscription["state"] if subscription else None,
            },
            "unlimited": unlimited,
            "unlimited_heavy_use": unlimited and used >= settings.billing.unlimited_heavy_use_minutes,
        }

    def end_subscription(self, token: str) -> None:
        """Google no longer knows the subscription: it expired long ago."""
        with self._connect() as connection:
            connection.execute(
                "UPDATE subscriptions SET state = 'EXPIRED', checked_at = ? WHERE purchase_token = ?",
                (int(time.time()), token),
            )

    def mark_subscription_acknowledged(self, token: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE subscriptions SET acknowledged = 1 WHERE purchase_token = ?", (token,))

    def mark_subscription_checked(self, token: str) -> None:
        """A failed read waits like a successful one, so one subscription
        Google refuses does not hold back the others."""
        with self._connect() as connection:
            connection.execute(
                "UPDATE subscriptions SET checked_at = ? WHERE purchase_token = ?",
                (int(time.time()), token),
            )

    def unlimited_heavy_use(self, user_id: str) -> bool:
        """Whether the user is on an unlimited plan and started at least
        BILLING_UNLIMITED_HEAVY_USE_MINUTES of jobs in the last 24 hours.
        Such jobs are never refused: they run one at a time
        (BILLING_UNLIMITED_HEAVY_USE_JOBS) and never on RunPod, so that one
        account cannot take every worker or run up the provider's bill."""
        if settings.billing.provider == "none":
            return False
        now = int(time.time())
        with closing(self._connect()) as connection:
            period = _current_period(connection, user_id, now)
            if period is None or period["minutes"] is not None:
                return False
            return _unlimited_used(connection, user_id, now) >= settings.billing.unlimited_heavy_use_minutes

    def credit_all(self, minutes: int, reason: str, actor: str, grant_id: str) -> dict:
        """Add minutes to every account's paid balance, never expiring, with
        one audit entry, all in one transaction. grant_id makes it once
        only: a repeat, as after a lost answer, changes nothing and returns
        the first result."""
        if minutes <= 0:
            raise ValueError("Give a positive number of minutes")
        target = f"grant:{grant_id}"
        with self._connect() as connection:
            self._begin_write(connection)
            done = connection.execute(
                "SELECT detail FROM admin_actions WHERE action = 'users.credit_all' AND target = ?", (target,)
            ).fetchone()
            if done is not None:
                return {"grant_id": grant_id, "repeated": True, **_credit_all_detail(done["detail"])}
            now = int(time.time())
            # Tagged with the grant, so that it can be undone account by account.
            connection.execute(
                "INSERT INTO minute_ledger (user_id, kind, minutes, at, note, pool) "
                "SELECT user_id, 'admin', ?, ?, ?, 'paid' FROM users WHERE user_id != ?",
                (minutes, now, f"{grant_note(grant_id)} {reason}", DELETED_USER_ID),
            )
            accounts = connection.execute(
                "UPDATE users SET minutes = minutes + ? WHERE user_id != ?", (minutes, DELETED_USER_ID)
            ).rowcount
            _log_admin_action(
                connection, actor, "users.credit_all", target, f"+{minutes} minutes to {accounts} accounts", reason
            )
        return {"grant_id": grant_id, "repeated": False, "minutes": minutes, "accounts": accounts,
                "total": minutes * accounts}

    def credit_all_grants(self, limit: int = 10) -> list[dict]:
        """The latest grants to every account, newest first, and whether
        each was undone."""
        rows = self.read(
            """SELECT given.at, given.actor, given.target, given.detail, given.reason,
                      undone.at AS undone_at, undone.detail AS undone_detail
               FROM admin_actions AS given
               LEFT JOIN admin_actions AS undone
                 ON undone.action = 'users.credit_all_undo' AND undone.target = given.target
               WHERE given.action = 'users.credit_all'
               ORDER BY given.id DESC LIMIT ?""",
            (limit,),
        )
        return [
            {**dict(row), "grant_id": row["target"].removeprefix("grant:"), **_credit_all_detail(row["detail"])}
            for row in rows
        ]

    def undo_credit_all_plan(self, grant_id: str) -> dict:
        """What undoing a grant would take back, changing nothing."""
        with closing(self._connect()) as connection:
            return _undo_amounts(connection, grant_id)

    def undo_credit_all(self, grant_id: str, reason: str, actor: str) -> dict:
        """Take a grant's minutes back from each account that got them: no
        more than the paid minutes it has left, so no balance goes below
        zero. Once only, in one transaction with its audit entry."""
        target = f"grant:{grant_id}"
        with self._connect() as connection:
            self._begin_write(connection)
            plan = _undo_amounts(connection, grant_id)
            if plan["undone"]:
                return {**plan, "repeated": True}
            for account in plan["take"]:
                connection.execute(
                    "UPDATE users SET minutes = minutes - ? WHERE user_id = ?",
                    (account["minutes"], account["user_id"]),
                )
                _log_minutes(connection, account["user_id"], "admin", -account["minutes"], f"undo {grant_note(grant_id)}")
            _log_admin_action(
                connection, actor, "users.credit_all_undo", target,
                f"-{plan['taken']} minutes from {len(plan['take'])} accounts; {plan['kept']} already used",
                reason,
            )
        return {**plan, "repeated": False}

    def account_count(self) -> int:
        with closing(self._connect()) as connection:
            return int(connection.execute(
                "SELECT COUNT(*) AS n FROM users WHERE user_id != ?", (DELETED_USER_ID,)
            ).fetchone()["n"])

    def get_state(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM billing_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO billing_state (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def user_id_by_email(self, email: str) -> str | None:
        with self._connect() as connection:
            user = connection.execute(
                "SELECT user_id FROM users WHERE email = ?",
                (email.strip().lower(),),
            ).fetchone()
        return user["user_id"] if user else None

    def is_developer(self, user_id: str | None) -> bool:
        """Whether an administrator made the account a developer's: its jobs
        are never charged, and the API shows it the developer extras."""
        if settings.billing.provider == "none" or not user_id:
            return False
        with closing(self._connect()) as connection:
            user = connection.execute(
                "SELECT developer FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return bool(user and user["developer"])

    def set_developer(self, user_id: str, developer: bool, actor: str, reason: str | None = None) -> bool:
        """Make the account a developer's, or end that, with its audit entry
        in the same transaction. Returns whether anything changed."""
        with self._connect() as connection:
            self._begin_write(connection)
            changed = connection.execute(
                "UPDATE users SET developer = ? WHERE user_id = ? AND developer != ?",
                (int(developer), user_id, int(developer)),
            ).rowcount
            if changed:
                _log_admin_action(
                    connection, actor, "user.developer", user_id, "on" if developer else "off", reason
                )
            return bool(changed)

    def adjust_minutes(self, user_id: str, minutes: int, reason: str, actor: str | None = None) -> int:
        """Add minutes to a user's balance by hand, or take them away when
        negative. Returns the new balance, which can be below zero. With an
        actor, the audit entry is written in the same transaction, so the
        change is never made without it."""
        with self._connect() as connection:
            self._begin_write(connection)
            updated = connection.execute(
                "UPDATE users SET minutes = minutes + ? WHERE user_id = ?",
                (minutes, user_id),
            ).rowcount
            if not updated:
                raise ValueError(f"No billed user with ID {user_id}")
            _log_minutes(connection, user_id, "admin", minutes, reason)
            if actor is not None:
                _log_admin_action(connection, actor, "user.credit", user_id, f"{minutes:+d} minutes", reason)
            return connection.execute("SELECT minutes FROM users WHERE user_id = ?", (user_id,)).fetchone()["minutes"]

    def log_admin_action(
        self,
        actor: str,
        action: str,
        target: str | None = None,
        detail: str | None = None,
        reason: str | None = None,
    ) -> None:
        with self._connect() as connection:
            _log_admin_action(connection, actor, action, target, detail, reason)

    def admin_web_caps(self) -> list[dict]:
        """Lowerings of the admin web page's access, oldest first; see
        app.admin.web."""
        return [dict(row) for row in self.read("SELECT * FROM admin_web_caps ORDER BY id")]

    def lower_admin_web(self, mode: str, actor: str, reason: str) -> None:
        """Lower the admin web page's access to mode, with its audit entry
        in the same transaction. The page's roles may add these rows, never
        change or delete them."""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO admin_web_caps (at, actor, mode, reason) VALUES (?, ?, ?, ?)",
                (int(time.time()), actor, mode, reason),
            )
            _log_admin_action(connection, actor, "web.lower", None, mode, reason)

    def restore_admin_web(self, actor: str, reason: str) -> int:
        """Remove every lowering, from the command line only. Returns how
        many there were."""
        with self._connect() as connection:
            self._begin_write(connection)
            removed = connection.execute("DELETE FROM admin_web_caps").rowcount
            _log_admin_action(connection, actor, "web.restore", None, f"{removed} lowering(s) removed", reason)
            return removed

    def announcements(self, ended: bool = False, now: float | None = None) -> list[dict]:
        """Announcement rows, newest first; see app.announcements. Without
        ended, only those that have not ended, for the API."""
        query = "SELECT * FROM announcements"
        parameters: tuple = ()
        if not ended:
            query += " WHERE ends_at IS NULL OR ends_at > ?"
            parameters = (int(time.time() if now is None else now),)
        return [dict(row) for row in self.read(query + " ORDER BY starts_at DESC, id", parameters)]

    def save_announcement(
        self, row: dict, actor: str, detail: str, reason: str | None, replace: bool, max_stored: int
    ) -> bool:
        """Add the announcement row, from app.announcements.to_row, with its
        audit entry in the same transaction. replace allows overwriting one
        with the same ID; without it, that ID fails with ValueError. Returns
        whether one was replaced."""
        now = int(time.time())
        with self._connect() as connection:
            self._begin_write(connection)
            existing = connection.execute("SELECT created_at, created_by FROM announcements WHERE id = ?", (row["id"],)).fetchone()
            if existing is not None and not replace:
                raise ValueError(f"An announcement {row['id']} exists; replace it, or choose another ID")
            if existing is None:
                count = connection.execute("SELECT COUNT(*) AS count FROM announcements").fetchone()["count"]
                if count >= max_stored:
                    raise ValueError(f"There are {count} announcements, the most kept; replace an ended one instead")
                connection.execute(
                    "INSERT INTO announcements (id, content, starts_at, ends_at, created_at, updated_at, created_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (row["id"], row["content"], row["starts_at"], row["ends_at"], now, now, actor),
                )
            else:
                connection.execute(
                    "UPDATE announcements SET content = ?, starts_at = ?, ends_at = ?, updated_at = ? WHERE id = ?",
                    (row["content"], row["starts_at"], row["ends_at"], now, row["id"]),
                )
            _log_admin_action(connection, actor, "announce.replace" if existing else "announce.add", row["id"], detail, reason)
            return existing is not None

    def end_announcement(self, announcement_id: str, actor: str, reason: str | None) -> bool:
        """End an announcement now, with its audit entry. It is kept, ended,
        so it can be seen and replaced later. Returns False if it had already
        ended or does not exist."""
        now = int(time.time())
        with self._connect() as connection:
            self._begin_write(connection)
            ended = connection.execute(
                "UPDATE announcements SET ends_at = ?, updated_at = ? WHERE id = ? AND (ends_at IS NULL OR ends_at > ?)",
                # An announcement that has not started ends before it starts.
                (now, now, announcement_id, now),
            ).rowcount
            if ended:
                connection.execute(
                    "UPDATE announcements SET starts_at = ends_at - 1 WHERE id = ? AND starts_at >= ends_at", (announcement_id,)
                )
                _log_admin_action(connection, actor, "announce.end", announcement_id, None, reason)
            return bool(ended)

    def read(self, query: str, parameters: tuple = ()) -> list[sqlite3.Row]:
        """Rows of a query, for the admin commands' reports. It runs in a
        read-only transaction, so the database refuses any change."""
        with closing(self._connect()) as connection:
            if self._postgres:
                connection.execute("SET TRANSACTION READ ONLY")
            else:
                connection.execute("PRAGMA query_only = ON")
            return connection.execute(query, parameters).fetchall()

    def delete_user(self, user_id: str, actor: str | None = None, reason: str | None = None) -> bool:
        """Delete a user's account data. Purchases are kept without the user ID
        so a purchase token cannot be credited twice. With an actor, the
        audit entry is written in the same transaction."""
        with self._connect() as connection:
            self._begin_write(connection)
            user = connection.execute(
                "SELECT free_grant_at FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            connection.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            connection.execute("DELETE FROM transcription_reservations WHERE user_id = ?", (user_id,))
            connection.execute("DELETE FROM job_diagnostics WHERE user_id = ?", (user_id,))
            # Kept without the user, like purchases, so that an order is never
            # given twice; the same person signing up again gets it back.
            for table in ("purchases", "subscriptions", "subscription_periods", "unlimited_usage"):
                connection.execute(
                    f"UPDATE {table} SET user_id = ? WHERE user_id = ?",
                    (DELETED_USER_ID, user_id),
                )
            # Totals stay right; whose minutes they were is forgotten.
            connection.execute(
                "UPDATE minute_ledger SET user_id = ? WHERE user_id = ?",
                (DELETED_USER_ID, user_id),
            )
            connection.execute(
                "UPDATE admin_actions SET target = ? WHERE target = ?",
                (deleted_user_target(user_id), user_id),
            )
            _purge_expired_deleted_users(connection)
            if user and user["free_grant_at"] and _free_minutes_enabled():
                if user["free_grant_at"] + period_seconds() > time.time():
                    connection.execute(
                        "INSERT INTO deleted_users (user_hash, free_grant_at) VALUES (?, ?) "
                        "ON CONFLICT (user_hash) DO UPDATE SET free_grant_at = excluded.free_grant_at",
                        (_user_hash(user_id), user["free_grant_at"]),
                    )
            if actor is not None:
                # Logged by hash: the account's ID is personal data and now gone.
                _log_admin_action(connection, actor, "user.delete", deleted_user_target(user_id), None, reason)
            return user is not None

    def record_job(self, job_id: str, job: dict[str, str]) -> None:
        """Save a snapshot of a job's state, which outlives the Redis job.
        The first start and the first terminal state keep their time."""
        status = job.get("status", "unknown")
        now = int(time.time())
        try:
            duration = float(job["duration"]) if job.get("duration") else None
        except ValueError:
            duration = None
        try:
            cost = int(job["full_cost"]) if job.get("full_cost") else None
        except ValueError:
            cost = None
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO job_diagnostics
                   (job_id, user_id, source, source_url, duration_seconds, cost_minutes,
                    status, error, backend, outsourced, requeue_reason,
                    created_at, started_at, finished_at, updated_at, mode)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (job_id) DO UPDATE SET
                       mode = excluded.mode,
                       status = excluded.status,
                       error = excluded.error,
                       backend = excluded.backend,
                       outsourced = excluded.outsourced,
                       requeue_reason = COALESCE(
                           excluded.requeue_reason, job_diagnostics.requeue_reason
                       ),
                       started_at = COALESCE(
                           job_diagnostics.started_at, excluded.started_at
                       ),
                       finished_at = COALESCE(
                           job_diagnostics.finished_at, excluded.finished_at
                       ),
                       updated_at = excluded.updated_at""",
                (
                    job_id,
                    job.get("user_id"),
                    job.get("source") or "url",
                    job.get("source_url"),
                    duration,
                    cost,
                    status,
                    job.get("error_detail") or job.get("error"),
                    job.get("backend"),
                    1 if job.get("outsourced") == "1" else 0,
                    job.get("requeue_reason"),
                    now,
                    now if status == "processing" else None,
                    now if status in TERMINAL_JOB_STATUSES else None,
                    now,
                    "arrangement" if job.get("mode") == "arrangement" else "transcription",
                ),
            )

    def reopen_job_record(self, job_id: str, reason: str) -> None:
        """Forget a job's start, end, and error before it is queued again."""
        with self._connect() as connection:
            connection.execute(
                """UPDATE job_diagnostics
                   SET started_at = NULL, finished_at = NULL, error = NULL, requeue_reason = ?
                   WHERE job_id = ?""",
                (reason, job_id),
            )

    def prune_job_diagnostics(self, max_age_seconds: float, now: float | None = None) -> int:
        cutoff = (time.time() if now is None else now) - max_age_seconds
        with self._connect() as connection:
            return connection.execute(
                "DELETE FROM job_diagnostics WHERE created_at < ?",
                (int(cutoff),),
            ).rowcount

    def backup(self, destination: str) -> None:
        """Copy the database to destination. The copy is written under a
        temporary name and checked first, so a file at destination is always
        a complete, intact backup."""
        target_path = Path(destination)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if self._postgres:
            partial = target_path.with_name(target_path.name + ".partial")
            url, environment = self._postgres_tool_arguments()
            try:
                run_postgres_tool(
                    ["pg_dump", "--format=custom", "--no-owner", "--file", str(partial), url], environment
                )
                if partial.stat().st_size == 0:
                    raise RuntimeError("pg_dump produced an empty backup")
                run_postgres_tool(["pg_restore", "--list", str(partial)], environment)
                _replace_durably(partial, target_path)
            finally:
                partial.unlink(missing_ok=True)
            return
        partial = target_path.with_name(target_path.name + ".partial")
        try:
            with closing(self._connect()) as source, closing(sqlite3.connect(partial)) as target:
                source.backup(target)
                result = target.execute("PRAGMA integrity_check").fetchone()
            if result != ("ok",):
                raise sqlite3.DatabaseError(f"The backup failed its integrity check: {result}")
            _replace_durably(partial, target_path)
        finally:
            partial.unlink(missing_ok=True)


class InsufficientMinutes(Exception):
    pass


def postgres_tool_arguments(url: str) -> tuple[str, dict[str, str]]:
    """The URL without its password, and an environment with it. Command
    arguments are visible to every process in the container and appear in
    the error of a failed command, which is logged."""
    parts = urlsplit(url)
    environment = dict(os.environ)
    if parts.password is None:
        return urlunsplit(parts), environment
    environment["PGPASSWORD"] = unquote(parts.password)
    netloc = f"{parts.username}@{parts.hostname}" if parts.username else parts.hostname
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment)), environment


def run_postgres_tool(command: list[str], environment: dict[str, str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, env=environment)
    if result.returncode != 0:
        # Only the tool's name and its own message: the command holds the URL.
        message = (result.stderr or "").strip().splitlines()
        raise RuntimeError(
            f"{command[0]} failed with exit status {result.returncode}" + (f": {message[-1]}" if message else "")
        )


def _replace_durably(partial: Path, target: Path) -> None:
    """Rename a finished file into place so that a power cut leaves either
    no file or the complete one, never a renamed but unwritten file."""
    # Writable, because Windows cannot sync a file opened only for reading.
    with partial.open("r+b") as file:
        os.fsync(file.fileno())
    partial.replace(target)
    try:
        directory = os.open(target.parent, os.O_RDONLY)
    except OSError:
        # Directories cannot be opened on Windows, where tests run.
        return
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def period_seconds() -> int:
    return int(parse_duration(settings.billing.free_minutes_period or "30d", "FREE_MINUTES_INTERVAL"))


def deleted_user_target(user_id: str) -> str:
    """How the audit log names a deleted user: by a hash, like deleted_users."""
    return "deleted:" + _user_hash(user_id)[:16]


# The admin web page's PostgreSQL role for each mode, and its password file
# in the secrets folder, which scripts/server.py creates. See app/admin_web.
ADMIN_WEB_ROLES = {"read-only": "admin-read-only", "read-write": "admin-read-write"}
ADMIN_WEB_SECRETS = "admin-web"
# What each mode's role may write, beyond reading. Read-only adds only audit
# entries and lowerings of its own access. Read-write may also cancel jobs and
# change balances by hand, and add or end announcements: never delete
# anything, create users, or touch purchases, which stay with the API and the
# command line.
ADMIN_WEB_WRITES = {
    "read-only": {"admin_actions": "INSERT", "admin_web_caps": "INSERT"},
    "read-write": {
        "admin_actions": "INSERT",
        "admin_web_caps": "INSERT",
        "announcements": "INSERT, UPDATE (content, starts_at, ends_at, updated_at)",
        # monthly_minutes only to settle the jobs it cancels; developer to
        # make an account a developer's, or end that.
        "users": "UPDATE (minutes, monthly_minutes, developer)",
        "minute_ledger": "INSERT",
        "transcription_reservations": "UPDATE (status, charged_minutes)",
        # To stop counting the jobs of an unlimited subscription it cancels.
        "unlimited_usage": "UPDATE (minutes)",
        "job_diagnostics": "INSERT, UPDATE",
    },
}
# Tables whose rows take their ID from a sequence.
SEQUENCES = {"admin_actions", "admin_web_caps", "minute_ledger"}
# Purchase tokens are never shown, so the page cannot read them.
PURCHASE_COLUMNS_SHOWN = ("product_id", "user_id", "minutes", "order_id", "voided_at")
SUBSCRIPTION_COLUMNS_SHOWN = (
    "id", "user_id", "product_id", "state", "expires_at", "auto_renewing", "latest_order_id",
    "replaced_by", "revoked_at", "checked_at", "created_at",
)
TOKEN_TABLES = {"purchases": PURCHASE_COLUMNS_SHOWN, "subscriptions": SUBSCRIPTION_COLUMNS_SHOWN}


def admin_web_password_file(mode: str) -> Path:
    return Path(settings.secrets_dir) / ADMIN_WEB_SECRETS / f"postgres-{mode}"


def _sync_admin_web_roles(connection) -> None:
    """Create or update the admin web page's roles whose password file
    exists, with exactly their privileges: read every table except purchase
    tokens, and write only what ADMIN_WEB_WRITES lists for their mode.
    Nothing else, so the database refuses any other change whatever the
    page's code does. Runs with the schema, as the owner."""
    from psycopg import sql

    tables = [row["tablename"] for row in connection.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall()]
    for mode, role in ADMIN_WEB_ROLES.items():
        password_file = admin_web_password_file(mode)
        password = password_file.read_text(encoding="utf-8").strip() if password_file.is_file() else ""
        if not password:
            continue
        name = sql.Identifier(role)
        if connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is None:
            connection.execute(sql.SQL("CREATE ROLE {}").format(name))
        connection.execute(
            sql.SQL(
                "ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT "
                "CONNECTION LIMIT 20 PASSWORD {}"
            ).format(name, sql.Literal(password))
        )
        # Column privileges go with the table's, so this starts from nothing.
        connection.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(name))
        connection.execute(sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {}").format(name))
        for table in tables:
            if table in TOKEN_TABLES:
                columns = sql.SQL(", ").join(sql.Identifier(column) for column in TOKEN_TABLES[table])
                connection.execute(
                    sql.SQL("GRANT SELECT ({}) ON {} TO {}").format(columns, sql.Identifier(table), name)
                )
            else:
                connection.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(sql.Identifier(table), name))
        for table, privileges in ADMIN_WEB_WRITES[mode].items():
            # Fixed strings from ADMIN_WEB_WRITES, never input.
            connection.execute(sql.SQL("GRANT " + privileges + " ON {} TO {}").format(sql.Identifier(table), name))
            if table in SEQUENCES:
                connection.execute(
                    sql.SQL("GRANT USAGE ON SEQUENCE {} TO {}").format(sql.Identifier(f"{table}_id_seq"), name)
                )


def _log_admin_action(
    connection: sqlite3.Connection,
    actor: str,
    action: str,
    target: str | None,
    detail: str | None,
    reason: str | None,
) -> None:
    connection.execute(
        "INSERT INTO admin_actions (at, actor, action, target, detail, reason) VALUES (?, ?, ?, ?, ?, ?)",
        (int(time.time()), actor, action, target, detail, reason),
    )


def _log_minutes(
    connection: sqlite3.Connection,
    user_id: str,
    kind: str,
    minutes: int,
    note: str | None = None,
    pool: str = PAID,
) -> None:
    if minutes:
        connection.execute(
            "INSERT INTO minute_ledger (user_id, kind, minutes, at, note, pool) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, kind, minutes, int(time.time()), note, pool),
        )


def _held(connection: sqlite3.Connection, user_id: str) -> dict[str, int]:
    """Minutes the user's running jobs hold: in all, and of those, the
    monthly ones."""
    row = connection.execute(
        """SELECT COALESCE(SUM(reserved_minutes), 0) AS total, COALESCE(SUM(monthly_minutes), 0) AS monthly
           FROM transcription_reservations WHERE user_id = ? AND status = 'reserved'""",
        (user_id,),
    ).fetchone()
    return {"total": int(row["total"]), "monthly": int(row["monthly"])}


def _renew_monthly(connection: sqlite3.Connection, user_id: str) -> int:
    """Start a new monthly period if the user's has ended, in the caller's
    write transaction. Monthly minutes that running jobs hold stay until the
    jobs end (_expire_stale_hold); the others expire. Returns the minutes
    given. A subscription's period is renewed by its payments instead
    (Billing.record_subscription); when it ends unrenewed, free periods
    start again from its end."""
    user = connection.execute(
        "SELECT minutes, monthly_minutes, free_grant_at FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    now = int(time.time())
    if user is None or _current_period(connection, user_id, now) is not None:
        return 0
    # A subscription period that ended, whose unused minutes have not yet
    # expired: free periods start again from its end.
    ended = connection.execute(
        """SELECT order_id, ends_at FROM subscription_periods
           WHERE user_id = ? AND voided_at IS NULL AND unused_minutes IS NULL AND ends_at <= ?
           ORDER BY ends_at DESC LIMIT 1""",
        (user_id, now),
    ).fetchone()
    after_subscription = ended is not None
    subscription_end = int(ended["ends_at"]) if ended else 0
    if not after_subscription and now - user["free_grant_at"] < period_seconds():
        return 0
    kept = min(user["monthly_minutes"], _held(connection, user_id)["monthly"])
    expired = user["monthly_minutes"] - kept
    grant = (settings.billing.free_minutes or 0) if _free_minutes_enabled() else 0
    if user["minutes"] < 0:
        # Minutes owed for a refunded purchase are not paid off with
        # monthly minutes; the renewal waits until the debt is paid. A
        # subscription's minutes still expire with its period.
        if not after_subscription:
            return 0
        grant = 0
    if not expired and not grant and not after_subscription:
        return 0
    # Periods follow a fixed schedule from the first, which starts at sign-up
    # or when a subscription ends: the new one starts where the last full
    # period ended, however late the user comes back.
    start = subscription_end if after_subscription else user["free_grant_at"]
    period = period_seconds()
    period_start = now if start <= 0 else start + (now - start) // period * period
    if after_subscription:
        connection.execute(
            "UPDATE subscription_periods SET unused_minutes = ? WHERE order_id = ?",
            (expired, ended["order_id"]),
        )
    connection.execute(
        "UPDATE users SET monthly_minutes = ?, free_grant_at = ? WHERE user_id = ?",
        (kept + grant, period_start, user_id),
    )
    connection.execute(
        "UPDATE transcription_reservations SET monthly_ended = 1 "
        "WHERE user_id = ? AND status = 'reserved' AND monthly_minutes > 0",
        (user_id,),
    )
    _log_minutes(connection, user_id, "expired", -expired, None, MONTHLY)
    _log_minutes(connection, user_id, "free", grant, None, MONTHLY)
    return grant


def _expire_stale_hold(connection: sqlite3.Connection, reservation, used: int) -> None:
    """When a job that held monthly minutes of an ended period ends, the
    ones it did not use expire, as that period's others did at renewal."""
    held = reservation["monthly_minutes"] or 0
    if held <= used or not reservation["monthly_ended"]:
        return
    user = connection.execute(
        "SELECT monthly_minutes FROM users WHERE user_id = ?",
        (reservation["user_id"],),
    ).fetchone()
    if user is None:
        return
    expired = min(held - used, user["monthly_minutes"])
    if expired <= 0:
        return
    connection.execute(
        "UPDATE users SET monthly_minutes = monthly_minutes - ? WHERE user_id = ?",
        (expired, reservation["user_id"]),
    )
    _log_minutes(connection, reservation["user_id"], "expired", -expired, reservation["job_id"], MONTHLY)


def _free_minutes_enabled() -> bool:
    return settings.billing.provider != "none" and (settings.billing.free_minutes or 0) > 0


def _user_hash(user_id: str) -> str:
    return hashlib.sha256(user_id.encode()).hexdigest()


def _purge_expired_deleted_users(connection: sqlite3.Connection) -> None:
    if not _free_minutes_enabled():
        connection.execute("DELETE FROM deleted_users")
        return
    connection.execute(
        "DELETE FROM deleted_users WHERE free_grant_at <= ?",
        (int(time.time()) - period_seconds(),),
    )


# --- Subscriptions -------------------------------------------------------------

# Google Play's subscription states, short (SUBSCRIPTION_STATE_ removed). An
# entitled one gives its plan; a paid one may also start a period, which a
# grace period, whose payment has not come, may not.
PAID_STATES = {"ACTIVE", "CANCELED"}
ENTITLED_STATES = PAID_STATES | {"IN_GRACE_PERIOD"}
# Subscriptions read again from Google Play: from this long before their
# period ends, for this long after, at most this often and at least this often.
SUBSCRIPTION_CHECK_AHEAD = 10 * 60
SUBSCRIPTION_CHECK_FOR = 90 * 24 * 60 * 60
SUBSCRIPTION_CHECK_MIN_WAIT = 60
SUBSCRIPTION_CHECK_MAX_WAIT = 6 * 60 * 60
UNLIMITED_WINDOW = 24 * 60 * 60


@dataclass(frozen=True)
class SubscriptionInfo:
    """What Google Play reports of a subscription purchase."""

    token: str
    product_id: str
    state: str
    expires_at: int
    auto_renewing: bool = False
    latest_order_id: str | None = None
    linked_token: str | None = None
    # The obfuscated account ID the app set at purchase, if any.
    account_tag: str | None = None
    acknowledged: bool = True

    @property
    def paid(self) -> bool:
        return self.state in PAID_STATES

    @property
    def entitled(self) -> bool:
        return self.state in ENTITLED_STATES


class SubscriptionOwned(Exception):
    """The subscription belongs to another account."""


def subscription_key(token: str) -> str:
    """How tables other than subscriptions name a subscription: never by its
    purchase token, which would let whoever reads them claim it."""
    return hashlib.sha256(("subscription:" + token).encode()).hexdigest()[:32]


def account_tag(user_id: str) -> str:
    """The obfuscated account ID the apps give Google Play at purchase:
    the SHA-256 of the user ID, in hex."""
    return hashlib.sha256(user_id.encode()).hexdigest()


def _current_period(connection, user_id: str, now: int | None = None):
    """The user's subscription period that runs now, if any."""
    now = int(time.time()) if now is None else now
    return connection.execute(
        """SELECT * FROM subscription_periods
           WHERE user_id = ? AND voided_at IS NULL AND starts_at <= ? AND ends_at > ?
           ORDER BY starts_at DESC LIMIT 1""",
        (user_id, now, now),
    ).fetchone()


def _start_subscription_period(
    connection, user_id: str, subscription_id: str, product_id: str, order_id: str,
    starts_at: int, ends_at: int, minutes: int | None,
) -> int:
    """Start a paid order's period: the monthly minutes that running jobs
    do not hold expire, and the plan's are given in their place. Returns
    the minutes given."""
    user = connection.execute(
        "SELECT monthly_minutes FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    kept = min(user["monthly_minutes"], _held(connection, user_id)["monthly"])
    expired = user["monthly_minutes"] - kept
    # The period these minutes came from, running or just ended, as when
    # an upgrade ended it: it ends now, and they are its unused ones.
    previous = connection.execute(
        """SELECT order_id, ends_at FROM subscription_periods
           WHERE user_id = ? AND voided_at IS NULL AND unused_minutes IS NULL
           ORDER BY starts_at DESC LIMIT 1""",
        (user_id,),
    ).fetchone()
    if previous is not None:
        connection.execute(
            "UPDATE subscription_periods SET ends_at = ?, unused_minutes = ? WHERE order_id = ?",
            (min(int(previous["ends_at"]), starts_at), expired, previous["order_id"]),
        )
    grant = minutes or 0
    connection.execute(
        "UPDATE users SET monthly_minutes = ?, free_grant_at = ? WHERE user_id = ?",
        (kept + grant, starts_at, user_id),
    )
    connection.execute(
        "UPDATE transcription_reservations SET monthly_ended = 1 "
        "WHERE user_id = ? AND status = 'reserved' AND monthly_minutes > 0",
        (user_id,),
    )
    connection.execute(
        """INSERT INTO subscription_periods
           (order_id, subscription_id, user_id, product_id, minutes, starts_at, ends_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (order_id, subscription_id, user_id, product_id, minutes, starts_at, ends_at),
    )
    _log_minutes(connection, user_id, "expired", -expired, order_id, MONTHLY)
    _log_minutes(connection, user_id, "subscription", grant, order_id, MONTHLY)
    return grant


def _unlimited_used(connection, user_id: str, now: int) -> int:
    row = connection.execute(
        "SELECT COALESCE(SUM(minutes), 0) AS used FROM unlimited_usage WHERE user_id = ? AND at > ?",
        (user_id, now - UNLIMITED_WINDOW),
    ).fetchone()
    return int(row["used"])


def _use_unlimited(connection, user_id: str, job_id: str, cost: int, order_id: str) -> None:
    """Count a job of an unlimited subscription. Never refused for use."""
    now = int(time.time())
    counted = connection.execute(
        "SELECT user_id, minutes FROM unlimited_usage WHERE job_id = ?", (job_id,)
    ).fetchone()
    if counted is not None and (counted["minutes"] > 0 or counted["user_id"] != user_id):
        raise ValueError(f"Job {job_id} already has a reservation")
    counted = connection.execute(
        """INSERT INTO unlimited_usage (job_id, user_id, order_id, minutes, at) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (job_id) DO UPDATE SET order_id = excluded.order_id, minutes = excluded.minutes,
               at = excluded.at
           WHERE unlimited_usage.minutes = 0 AND unlimited_usage.user_id = excluded.user_id""",
        (job_id, user_id, order_id, cost, now),
    ).rowcount
    if not counted:
        raise ValueError(f"Job {job_id} already has a reservation")


def _void_subscription_order(connection, token: str, order_id: str | None) -> tuple[str, int] | None:
    """Take back what a refunded subscription order gave, once: the minutes
    of its period, those left from the monthly balance and the rest from
    the paid one, which can go below zero; for an unlimited one, the jobs
    it ran. The order's period ends, and with the latest order, the
    subscription."""
    subscription_id = subscription_key(token)
    if order_id:
        period = connection.execute(
            "SELECT * FROM subscription_periods WHERE order_id = ? AND subscription_id = ? AND voided_at IS NULL",
            (order_id, subscription_id),
        ).fetchone()
    else:
        period = connection.execute(
            "SELECT * FROM subscription_periods WHERE subscription_id = ? AND voided_at IS NULL "
            "ORDER BY starts_at DESC LIMIT 1",
            (subscription_id,),
        ).fetchone()
    subscription = connection.execute(
        "SELECT latest_order_id FROM subscriptions WHERE id = ?", (subscription_id,)
    ).fetchone()
    now = int(time.time())
    if subscription is not None and (
        order_id is None or order_id == subscription["latest_order_id"]
        or (period is not None and period["order_id"] == subscription["latest_order_id"])
    ):
        connection.execute(
            "UPDATE subscriptions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (now, subscription_id),
        )
    if period is None:
        return None
    running = int(period["ends_at"]) > now
    connection.execute(
        "UPDATE subscription_periods SET voided_at = ?, ends_at = ? WHERE order_id = ?",
        (now, min(int(period["ends_at"]), now), period["order_id"]),
    )
    user_id = period["user_id"]
    if user_id == DELETED_USER_ID:
        return None
    user = connection.execute(
        "SELECT monthly_minutes FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    if user is None:
        return None
    from_monthly = 0
    if period["minutes"] is None:
        from_paid = int(connection.execute(
            "SELECT COALESCE(SUM(minutes), 0) AS used FROM unlimited_usage WHERE order_id = ?",
            (period["order_id"],),
        ).fetchone()["used"])
    elif running:
        left = max(0, user["monthly_minutes"] - _held(connection, user_id)["monthly"])
        from_monthly = min(period["minutes"], left)
        from_paid = period["minutes"] - from_monthly
    else:
        from_paid = max(0, period["minutes"] - (period["unused_minutes"] or 0))
    connection.execute(
        "UPDATE users SET minutes = minutes - ?, monthly_minutes = monthly_minutes - ? WHERE user_id = ?",
        (from_paid, from_monthly, user_id),
    )
    _log_minutes(connection, user_id, "voided", -from_monthly, period["order_id"], MONTHLY)
    _log_minutes(connection, user_id, "voided", -from_paid, period["order_id"])
    return user_id, from_monthly + from_paid


def _credit_all_detail(detail: str | None) -> dict:
    parts = (detail or "").split()
    try:
        minutes, accounts = int(parts[0]), int(parts[3])
    except (IndexError, ValueError):
        return {}
    return {"minutes": minutes, "accounts": accounts, "total": minutes * accounts}


# --- Welcome minutes -------------------------------------------------------------

def _welcome_minutes_enabled() -> bool:
    return settings.billing.provider != "none" and settings.billing.welcome_minutes > 0


def _give_welcome_minutes(connection, user_id: str) -> None:
    """Give a new account WELCOME_MINUTES, once per person: an account
    deleted and made again is not given them again."""
    if not _welcome_minutes_enabled():
        return
    claimed = connection.execute(
        "INSERT INTO welcome_claims (user_hash, at) VALUES (?, ?) ON CONFLICT (user_hash) DO NOTHING",
        (_user_hash(user_id), int(time.time())),
    ).rowcount
    if not claimed:
        return
    minutes = settings.billing.welcome_minutes
    connection.execute("UPDATE users SET minutes = minutes + ? WHERE user_id = ?", (minutes, user_id))
    _log_minutes(connection, user_id, "welcome", minutes)


def grant_note(grant_id: str) -> str:
    """How the ledger names a grant to every account."""
    return f"[grant:{grant_id}]"


def _undo_amounts(connection, grant_id: str) -> dict:
    """For undoing a grant: each account's minutes to take back, at most
    the grant and the paid minutes it has free of running jobs."""
    target = f"grant:{grant_id}"
    if connection.execute(
        "SELECT 1 FROM admin_actions WHERE action = 'users.credit_all' AND target = ?", (target,)
    ).fetchone() is None:
        raise ValueError(f"No grant {grant_id} to every account")
    undone = connection.execute(
        "SELECT 1 FROM admin_actions WHERE action = 'users.credit_all_undo' AND target = ?", (target,)
    ).fetchone() is not None
    given = connection.execute(
        "SELECT user_id, minutes FROM minute_ledger WHERE kind = 'admin' AND minutes > 0 AND user_id != ? "
        "AND note LIKE ?",
        (DELETED_USER_ID, grant_note(grant_id) + " %"),
    ).fetchall()
    take = []
    granted = 0
    for row in given:
        granted += int(row["minutes"])
        user = connection.execute("SELECT minutes FROM users WHERE user_id = ?", (row["user_id"],)).fetchone()
        if user is None:
            continue
        held = _held(connection, row["user_id"])
        free_paid = int(user["minutes"]) - (held["total"] - held["monthly"])
        minutes = min(int(row["minutes"]), max(0, free_paid))
        if minutes:
            take.append({"user_id": row["user_id"], "minutes": minutes})
    taken = sum(account["minutes"] for account in take)
    return {"grant_id": grant_id, "undone": undone, "accounts": len(given), "granted": granted,
            "take": take, "taken": taken, "kept": granted - taken}
