"""One-time, explicit SQLite to PostgreSQL billing migration.

Run this before switching a legacy installation to the production Compose
database. It never runs during application startup.
"""
import argparse
import sqlite3
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

TABLES = (
    "users", "purchases", "transcription_reservations", "deleted_users",
    "job_diagnostics", "billing_state", "minute_ledger", "admin_actions",
)


def migrate(source: str, target: str) -> None:
    source_path = Path(source)
    if not source_path.is_file():
        raise ValueError(f"SQLite source does not exist: {source}")
    with sqlite3.connect(f"file:{source_path}?mode=ro", uri=True) as sqlite:
        sqlite.row_factory = sqlite3.Row
        try:
            integrity = sqlite.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as error:
            raise ValueError(f"SQLite integrity check failed: {error}") from error
        if integrity != "ok":
            raise ValueError(f"SQLite integrity check failed: {integrity}")
        source_tables = {row[0] for row in sqlite.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        missing = set(TABLES) - source_tables
        if missing:
            raise ValueError(f"SQLite source is missing tables: {', '.join(sorted(missing))}")
        import psycopg
        from psycopg.rows import dict_row
        password_file = Path("/run/secrets/postgres_password")
        if urlsplit(target).password is None and password_file.is_file():
            parts = urlsplit(target)
            password = password_file.read_text(encoding="utf-8").strip()
            netloc = f"{parts.username}:{quote(password, safe='')}@{parts.hostname}"
            if parts.port:
                netloc += f":{parts.port}"
            target = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
        with psycopg.connect(target, row_factory=dict_row) as pg:
            # The target schema is created by Billing, never by an implicit
            # startup migration.
            from app.billing import Billing
            target_billing = Billing.__new__(Billing)
            target_billing.database_url = target
            target_billing._postgres = True
            target_billing._create_schema()
            for table in TABLES:
                columns = [row[1] for row in sqlite.execute(f"PRAGMA table_info({table})")]
                rows = sqlite.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
                if not rows:
                    continue
                quoted = ", ".join(f'"{column}"' for column in columns)
                placeholders = ", ".join(["%s"] * len(columns))
                with pg.cursor() as cursor:
                    cursor.executemany(
                        f"INSERT INTO {table} ({quoted}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                        [tuple(row[column] for column in columns) for row in rows],
                    )
            pg.execute(
                """SELECT setval(
                    pg_get_serial_sequence('minute_ledger', 'id'),
                    COALESCE((SELECT MAX(id) FROM minute_ledger), 1),
                    (SELECT MAX(id) IS NOT NULL FROM minute_ledger)
                )"""
            )
            pg.commit()
            for table in TABLES:
                sqlite_count = sqlite.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                pg_count = pg.execute(f"SELECT COUNT(*) FROM {table}").fetchone()["count"]
                if sqlite_count != pg_count:
                    raise RuntimeError(f"Validation failed for {table}: {sqlite_count} != {pg_count}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="existing billing.sqlite3")
    parser.add_argument("target", help="PostgreSQL URL (or use a secret-file URL)")
    args = parser.parse_args(argv)
    migrate(args.source, args.target)
    print("Billing migration completed and validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
