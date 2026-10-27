import sqlite3

import pytest

from app.migrate_billing import migrate


def test_migration_rejects_missing_source(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        migrate(str(tmp_path / "missing.sqlite3"), "postgresql://unused")


def test_migration_rejects_corrupt_source(tmp_path):
    source = tmp_path / "billing.sqlite3"
    source.write_bytes(b"not sqlite")
    with pytest.raises(ValueError, match="integrity check"):
        migrate(str(source), "postgresql://unused")


def test_migration_rejects_incomplete_schema(tmp_path):
    source = tmp_path / "billing.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE users (user_id TEXT PRIMARY KEY)")
    with pytest.raises(ValueError, match="missing tables"):
        migrate(str(source), "postgresql://unused")
