import sqlite3
import time
from pathlib import Path

import pytest

from app import main
from app.main import backup_billing_database
from app.billing import (
    backup_interval_seconds,
    backup_retention_seconds,
    duration_seconds,
    prune_backups,
)


class StopLoop(BaseException):
    pass


def backup_files(settings):
    return sorted(Path(settings.billing_backup_dir).glob("billing-*.sqlite3"))


def test_backup_is_a_readable_copy_of_the_database(api_context):
    _, settings, billing = api_context
    settings.billing_provider = "google_play"
    settings.billing_products = {"starter": 5}
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
    settings.billing_backup_retention = "2d"
    backup_dir = Path(settings.billing_backup_dir)
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
    settings.billing_backup_interval = "15m"
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
    settings.billing_backup_interval = None
    settings.billing_backup_retention = None

    assert backup_interval_seconds() == 60 * 60
    assert backup_retention_seconds() == 30 * 24 * 60 * 60


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("30s", 30), ("15m", 900), ("1h", 3600), (" 2D ", 172800), ("1w", 604800)],
)
def test_duration_parsing(value, seconds):
    assert duration_seconds(value, "SETTING") == seconds


@pytest.mark.parametrize("value", ["", "h", "10", "1y", "0h", "-1d", "1.5h"])
def test_invalid_duration_is_rejected(value):
    with pytest.raises(ValueError, match="SETTING"):
        duration_seconds(value, "SETTING")


def test_invalid_backup_setting_fails_startup(api_context):
    client, settings, _ = api_context
    settings.billing_backup_retention = "forever"

    with pytest.raises(ValueError, match="BILLING_BACKUP_RETENTION"):
        main.sync_billing_catalog()
