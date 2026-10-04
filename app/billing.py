import math
import sqlite3
import time
from pathlib import Path

from app.config import settings

def transcription_cost(duration_seconds: float) -> int:
    minutes = duration_seconds / 60
    return 1 if minutes < 2 else math.floor(minutes)


def cancelled_cost(full_cost: int, progress: float) -> int:
    if progress < 0.05:
        return 0
    return math.ceil(0.6 * full_cost * progress)


class Billing:
    def __init__(self, database_path: str | None = None):
        self.database_path = database_path or settings.billing_database_path
        database_parent = Path(self.database_path).parent
        database_parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
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
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @staticmethod
    def _ensure_user(connection: sqlite3.Connection, user_id: str) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
            (user_id,),
        )

    def grant_free_minutes(self, user_id: str) -> int:
        free_minutes = settings.free_minutes or 0
        if settings.billing_provider == "none" or free_minutes <= 0:
            return 0

        now = int(time.time())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_user(connection, user_id)
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
        if settings.billing_provider == "none":
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
        if settings.billing_provider == "none" or (settings.free_minutes or 0) <= 0:
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

    def reserve(self, user_id: str, job_id: str, cost: int) -> None:
        if settings.billing_provider == "none" or cost <= 0:
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_user(connection, user_id)
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

    def credit_purchase(self, user_id: str, product_id: str, token: str) -> int:
        minutes = settings.billing_products[product_id]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM purchases WHERE product_id = ? AND purchase_token = ?",
                (product_id, token),
            ).fetchone()
            if existing:
                return 0
            self._ensure_user(connection, user_id)
            connection.execute(
                "INSERT INTO purchases VALUES (?, ?, ?, ?)",
                (product_id, token, user_id, minutes),
            )
            connection.execute(
                "UPDATE users SET minutes = minutes + ? WHERE user_id = ?",
                (minutes, user_id),
            )
            return minutes


class InsufficientMinutes(Exception):
    pass


def period_seconds() -> int:
    value = (settings.free_minutes_period or "30d").strip().lower()
    units = {"s": 1, "m": 60, "h": 60 * 60, "d": 24 * 60 * 60, "w": 7 * 24 * 60 * 60}
    try:
        amount = int(value[:-1])
        multiplier = units[value[-1]]
    except (KeyError, ValueError, IndexError) as error:
        raise ValueError("FREE_MINUTES_PERIOD must use a format such as 12h, 7d, or 1w") from error
    if amount < 1:
        raise ValueError("FREE_MINUTES_PERIOD must be positive")
    return amount * multiplier