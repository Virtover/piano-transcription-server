import sqlite3
import time
from pathlib import Path

import pytest

from app import main
from app.main import backup_billing_database
from app.billing import prune_backups
from app.config import ConfigError, load_settings


class StopLoop(BaseException):
    pass


def backup_files(settings):
    return sorted(Path(settings.billing.backup_dir).glob("billing-*.sqlite3"))


def test_backup_is_a_readable_copy_of_the_database(api_context):
    _, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 5}
    billing.credit_purchase("user-1", "starter", "token", "order-1", "user@example.com")

    main.run_billing_backup()

    [backup] = backup_files(settings)
    connection = sqlite3.connect(backup)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT user_id, minutes FROM users").fetchall() == [("user-1", 5)]
    finally:
        connection.close()


def test_backup_removes_backups_older_than_retention(api_context):
    _, settings, _ = api_context
    settings.billing.backup_retention = "2d"
    backup_dir = Path(settings.billing.backup_dir)
    backup_dir.mkdir(parents=True)
    now = int(time.time())
    expired = backup_dir / f"billing-{now - 3 * 24 * 60 * 60}.sqlite3"
    kept = backup_dir / f"billing-{now - 24 * 60 * 60}.sqlite3"
    expired.touch()
    kept.touch()

    main.run_billing_backup()

    assert not expired.exists()
    assert kept.exists()
    assert len(backup_files(settings)) == 2


def test_prune_only_touches_automatic_backups(tmp_path):
    old = 1_000
    files = [
        tmp_path / f"billing-{old}.sqlite3",
        tmp_path / "manual-copy.sqlite3",
        tmp_path / f"billing-{old}.sqlite3.bak",
        tmp_path / "notes.txt",
    ]
    for path in files:
        path.touch()
    (tmp_path / f"billing-{old - 1}.sqlite3").mkdir()

    removed = prune_backups(str(tmp_path), 60, now=10_000)

    assert removed == [files[0]]
    assert [path.exists() for path in files] == [False, True, True, True]


def test_prune_handles_missing_directory(tmp_path):
    assert prune_backups(str(tmp_path / "missing"), 60) == []


def test_backup_loop_uses_configured_interval(api_context, monkeypatch):
    _, settings, _ = api_context
    settings.billing.backup_interval = "15m"
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        raise StopLoop

    monkeypatch.setattr(main.time, "sleep", fake_sleep)
    with pytest.raises(StopLoop):
        backup_billing_database()

    assert sleeps == [15 * 60]
    assert len(backup_files(settings)) == 1


def test_backup_loop_survives_backup_errors(api_context, monkeypatch):
    def failing_backup():
        raise OSError("disk full")

    def stop(seconds):
        raise StopLoop

    monkeypatch.setattr(main, "run_billing_backup", failing_backup)
    monkeypatch.setattr(main.time, "sleep", stop)
    with pytest.raises(StopLoop):
        backup_billing_database()


def test_backup_defaults(api_context):
    _, settings, _ = api_context

    assert settings.billing.backup_interval == 60 * 60
    assert settings.billing.backup_retention == 30 * 24 * 60 * 60


def test_invalid_backup_setting_fails_at_load(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("BILLING_BACKUP_RETENTION=forever\n")

    with pytest.raises(ConfigError, match="BILLING_BACKUP_RETENTION"):
        load_settings(str(env_file), None)


def test_backup_that_fails_its_check_leaves_no_file(api_context, monkeypatch):
    _, settings, _ = api_context
    real_connect = sqlite3.connect

    class Corrupt(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith("PRAGMA integrity_check"):
                sql = "SELECT 'page 3 is never used'"
            return super().execute(sql, *args)

    def connect(path, *args, **kwargs):
        if str(path).endswith(".partial"):
            kwargs["factory"] = Corrupt
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)

    with pytest.raises(sqlite3.DatabaseError, match="integrity check"):
        main.run_billing_backup()

    assert list(Path(settings.billing.backup_dir).iterdir()) == []
