import hashlib
import math
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from app.config import duration_seconds as parse_duration, settings

DELETED_USER_ID = "deleted"
TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}
# Kinds of minute_ledger entries. Each change to a balance is one entry.
LEDGER_KINDS = ("purchase", "free", "used", "voided", "admin")
# billing_state key: when this database started recording minute_ledger.
LEDGER_SINCE_KEY = "minute_ledger_since"


def transcription_cost(duration_seconds: float) -> int:
    minutes = duration_seconds / 60
    return 1 if minutes < 2 else math.floor(minutes)


def cancelled_cost(full_cost: int, progress: float) -> int:
    if progress < 0.05:
        return 0
    return math.ceil(0.6 * full_cost * progress)


class Billing:
    def __init__(self, database_path: str | None = None):
        # The database is created on first use, not here: modules create a
        # Billing when imported, also in services and tests without access
        # to the billing directory.
        self.database_path = database_path or settings.billing.database_path
        self._ready = False
        self._ready_lock = threading.Lock()

    def _create_schema(self) -> None:
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
                CREATE INDEX IF NOT EXISTS minute_ledger_user_index ON minute_ledger (user_id);
                CREATE INDEX IF NOT EXISTS minute_ledger_at_index ON minute_ledger (at);
                CREATE INDEX IF NOT EXISTS job_diagnostics_user_index ON job_diagnostics (user_id);
                CREATE INDEX IF NOT EXISTS job_diagnostics_created_index ON job_diagnostics (created_at);
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

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        # Sync the WAL at every commit, so a power cut loses no committed
        # purchase or charge. Usually SQLite's default, but not on every build.
        connection.execute("PRAGMA synchronous=FULL")
        return connection

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
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
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
        if email:
            connection.execute(
                "UPDATE users SET email = ? WHERE user_id = ?",
                (email, user_id),
            )

    def grant_free_minutes(self, user_id: str, email: str | None = None) -> int:
        free_minutes = settings.billing.free_minutes or 0
        if settings.billing.provider == "none" or free_minutes <= 0:
            return 0

        now = int(time.time())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_user(connection, user_id, email)
            user = connection.execute(
                "SELECT minutes, free_grant_at FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if now - user["free_grant_at"] < period_seconds():
                return 0
            if user["minutes"] < 0:
                # Minutes owed for a refunded purchase are not paid off with
                # free minutes; the grant waits until the balance is positive.
                return 0
            grant = max(0, free_minutes - user["minutes"])
            connection.execute(
                "UPDATE users SET minutes = minutes + ?, free_grant_at = ? WHERE user_id = ?",
                (grant, now, user_id),
            )
            _log_minutes(connection, user_id, "free", grant)
            return grant

    def get_balance(self, user_id: str) -> int:
        """Available minutes. Negative after a refunded purchase whose minutes
        were already used."""
        if settings.billing.provider == "none":
            return 0
        with self._connect() as connection:
            user = connection.execute(
                """SELECT u.minutes - COALESCE(
                           (SELECT SUM(reserved_minutes)
                            FROM transcription_reservations
                            WHERE user_id = ? AND status = 'reserved'),
                           0
                       ) AS available_minutes
                    FROM users AS u
                    WHERE u.user_id = ?""",
                (user_id, user_id),
            ).fetchone()
            return int(user["available_minutes"]) if user else 0

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
        if settings.billing.provider == "none" or cost <= 0:
            return None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_user(connection, user_id, email)
            user = connection.execute(
                "SELECT minutes FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            reserved = connection.execute(
                """SELECT COALESCE(SUM(reserved_minutes), 0) AS total
                   FROM transcription_reservations WHERE user_id = ? AND status = 'reserved'""",
                (user_id,),
            ).fetchone()["total"]
            if user["minutes"] - reserved < cost:
                raise InsufficientMinutes()
            # A failed job, whose reservation was released, can be queued
            # again under its ID (python -m app.admin jobs requeue).
            reserved_now = connection.execute(
                """INSERT INTO transcription_reservations
                   (job_id, user_id, reserved_minutes, status)
                   VALUES (?, ?, ?, 'reserved')
                   ON CONFLICT (job_id) DO UPDATE SET
                       reserved_minutes = excluded.reserved_minutes,
                       charged_minutes = NULL,
                       status = 'reserved'
                   WHERE status = 'released' AND user_id = excluded.user_id""",
                (job_id, user_id, cost),
            ).rowcount
            if not reserved_now:
                raise ValueError(f"Job {job_id} already has a reservation")
            return int(user["minutes"] - reserved - cost)

    def settle_success(self, job_id: str) -> None:
        self._settle(job_id, None)

    def settle_cancellation(self, job_id: str, charged_minutes: int) -> None:
        self._settle(job_id, charged_minutes)

    def release(self, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE transcription_reservations SET status = 'released' WHERE job_id = ? AND status = 'reserved'",
                (job_id,),
            )

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
            connection.execute("BEGIN IMMEDIATE")
            reservation = connection.execute(
                "SELECT * FROM transcription_reservations WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if not reservation or reservation["status"] != "reserved":
                return
            charge = reservation["reserved_minutes"] if charged_minutes is None else min(
                max(0, charged_minutes), reservation["reserved_minutes"]
            )
            connection.execute(
                "UPDATE users SET minutes = minutes - ? WHERE user_id = ?",
                (charge, reservation["user_id"]),
            )
            connection.execute(
                """UPDATE transcription_reservations
                   SET status = 'settled', charged_minutes = ? WHERE job_id = ?""",
                (charge, job_id),
            )
            _log_minutes(connection, reservation["user_id"], "used", -charge, job_id)

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
            connection.execute("BEGIN IMMEDIATE")
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

    def void_purchase(self, token: str) -> tuple[str, int] | None:
        """Take back the minutes of a refunded or charged-back purchase, once.
        The balance can go below zero when they were already used. Returns
        (user ID, minutes taken), or None if the token is unknown, already
        voided, or belonged to a deleted account."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            purchase = connection.execute(
                "SELECT rowid, user_id, minutes, order_id FROM purchases WHERE purchase_token = ? AND voided_at IS NULL",
                (token,),
            ).fetchone()
            if purchase is None:
                return None
            connection.execute(
                "UPDATE purchases SET voided_at = ? WHERE rowid = ?",
                (int(time.time()), purchase["rowid"]),
            )
            if purchase["user_id"] == DELETED_USER_ID:
                return None
            connection.execute(
                "UPDATE users SET minutes = minutes - ? WHERE user_id = ?",
                (purchase["minutes"], purchase["user_id"]),
            )
            _log_minutes(connection, purchase["user_id"], "voided", -purchase["minutes"], purchase["order_id"])
            return purchase["user_id"], purchase["minutes"]

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

    def adjust_minutes(self, user_id: str, minutes: int, reason: str) -> int:
        """Add minutes to a user's balance by hand, or take them away when
        negative. Returns the new balance, which can be below zero."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE users SET minutes = minutes + ? WHERE user_id = ?",
                (minutes, user_id),
            ).rowcount
            if not updated:
                raise ValueError(f"No billed user with ID {user_id}")
            _log_minutes(connection, user_id, "admin", minutes, reason)
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
            connection.execute(
                "INSERT INTO admin_actions (at, actor, action, target, detail, reason) VALUES (?, ?, ?, ?, ?, ?)",
                (int(time.time()), actor, action, target, detail, reason),
            )

    def read(self, query: str, parameters: tuple = ()) -> list[sqlite3.Row]:
        """Rows of a read-only query, for the admin commands' reports."""
        with closing(self._connect()) as connection:
            return connection.execute(query, parameters).fetchall()

    def delete_user(self, user_id: str) -> bool:
        """Delete a user's account data. Purchases are kept without the user ID
        so a purchase token cannot be credited twice."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = connection.execute(
                "SELECT free_grant_at FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            connection.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            connection.execute("DELETE FROM transcription_reservations WHERE user_id = ?", (user_id,))
            connection.execute("DELETE FROM job_diagnostics WHERE user_id = ?", (user_id,))
            connection.execute(
                "UPDATE purchases SET user_id = ? WHERE user_id = ?",
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
                        "INSERT OR REPLACE INTO deleted_users (user_hash, free_grant_at) VALUES (?, ?)",
                        (_user_hash(user_id), user["free_grant_at"]),
                    )
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
                    created_at, started_at, finished_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (job_id) DO UPDATE SET
                       status = excluded.status,
                       error = excluded.error,
                       backend = excluded.backend,
                       outsourced = excluded.outsourced,
                       requeue_reason = COALESCE(excluded.requeue_reason, requeue_reason),
                       started_at = COALESCE(started_at, excluded.started_at),
                       finished_at = COALESCE(finished_at, excluded.finished_at),
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
        partial = target_path.with_name(target_path.name + ".partial")
        try:
            with closing(self._connect()) as source, closing(sqlite3.connect(partial)) as target:
                source.backup(target)
                result = target.execute("PRAGMA integrity_check").fetchone()
            if result != ("ok",):
                raise sqlite3.DatabaseError(f"The backup failed its integrity check: {result}")
            partial.replace(target_path)
        finally:
            partial.unlink(missing_ok=True)


class InsufficientMinutes(Exception):
    pass


def period_seconds() -> int:
    return int(parse_duration(settings.billing.free_minutes_period or "30d", "FREE_MINUTES_PERIOD"))


def deleted_user_target(user_id: str) -> str:
    """How the audit log names a deleted user: by a hash, like deleted_users."""
    return "deleted:" + _user_hash(user_id)[:16]


def _log_minutes(connection: sqlite3.Connection, user_id: str, kind: str, minutes: int, note: str | None = None) -> None:
    if minutes:
        connection.execute(
            "INSERT INTO minute_ledger (user_id, kind, minutes, at, note) VALUES (?, ?, ?, ?, ?)",
            (user_id, kind, minutes, int(time.time()), note),
        )


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