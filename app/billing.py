import hashlib
import math
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from app.config import duration_seconds as parse_duration, settings

DELETED_USER_ID = "deleted"
BACKUP_FILE_PATTERN = re.compile(r"^billing-(\d+)\.sqlite3$")
TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}


def transcription_cost(duration_seconds: float) -> int:
    minutes = duration_seconds / 60
    return 1 if minutes < 2 else math.floor(minutes)


def cancelled_cost(full_cost: int, progress: float) -> int:
    if progress < 0.05:
        return 0
    return math.ceil(0.6 * full_cost * progress)


class Billing:
    def __init__(self, database_path: str | None = None):
        self.database_path = database_path or settings.billing.database_path
        database_parent = Path(self.database_path).parent
        database_parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
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
                CREATE INDEX IF NOT EXISTS job_diagnostics_user_index ON job_diagnostics (user_id);
                CREATE INDEX IF NOT EXISTS job_diagnostics_created_index ON job_diagnostics (created_at);
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
            if "email" not in columns:
                connection.execute("ALTER TABLE users ADD COLUMN email TEXT")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email_index ON users (email)")
            purchase_columns = {row[1] for row in connection.execute("PRAGMA table_info(purchases)")}
            if "order_id" not in purchase_columns:
                connection.execute("ALTER TABLE purchases ADD COLUMN order_id TEXT")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

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
            grant = max(0, free_minutes - user["minutes"])
            connection.execute(
                "UPDATE users SET minutes = minutes + ?, free_grant_at = ? WHERE user_id = ?",
                (grant, now, user_id),
            )
            return grant

    def get_balance(self, user_id: str) -> int:
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
            return max(0, int(user["available_minutes"])) if user else 0

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
            connection.execute(
                """INSERT INTO transcription_reservations
                   (job_id, user_id, reserved_minutes, status)
                   VALUES (?, ?, ?, 'reserved')""",
                (job_id, user_id, cost),
            )
            return max(0, int(user["minutes"] - reserved - cost))

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
            return minutes

    def user_id_by_email(self, email: str) -> str | None:
        with self._connect() as connection:
            user = connection.execute(
                "SELECT user_id FROM users WHERE email = ?",
                (email.strip().lower(),),
            ).fetchone()
        return user["user_id"] if user else None

    def credit_minutes_by_email(self, email: str, minutes: int) -> int:
        normalized_email = email.strip().lower()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = connection.execute(
                "SELECT user_id FROM users WHERE email = ?",
                (normalized_email,),
            ).fetchone()
            if user is None:
                raise ValueError(f"No billed user found for email: {normalized_email}")
            connection.execute(
                "UPDATE users SET minutes = minutes + ? WHERE user_id = ?",
                (minutes, user["user_id"]),
            )
            return minutes

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
                    job.get("error"),
                    job.get("backend"),
                    1 if job.get("outsourced") == "1" else 0,
                    job.get("requeue_reason"),
                    now,
                    now if status == "processing" else None,
                    now if status in TERMINAL_JOB_STATUSES else None,
                    now,
                ),
            )

    def prune_job_diagnostics(self, max_age_seconds: float, now: float | None = None) -> int:
        cutoff = (time.time() if now is None else now) - max_age_seconds
        with self._connect() as connection:
            return connection.execute(
                "DELETE FROM job_diagnostics WHERE created_at < ?",
                (int(cutoff),),
            ).rowcount

    def backup(self, destination: str) -> None:
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)


class InsufficientMinutes(Exception):
    pass


def period_seconds() -> int:
    return int(parse_duration(settings.billing.free_minutes_period or "30d", "FREE_MINUTES_PERIOD"))


def prune_backups(directory: str, max_age_seconds: float, now: float | None = None) -> list[Path]:
    """Delete backups older than max_age_seconds. Only files named like the
    automatic backups are touched; the age comes from the name's timestamp."""
    backup_dir = Path(directory)
    if not backup_dir.is_dir():
        return []
    cutoff = (time.time() if now is None else now) - max_age_seconds
    removed = []
    for path in backup_dir.iterdir():
        match = BACKUP_FILE_PATTERN.match(path.name)
        if match and path.is_file() and int(match.group(1)) < cutoff:
            path.unlink()
            removed.append(path)
    return removed


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