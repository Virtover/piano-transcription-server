import sqlite3
import time
from pathlib import Path

import pytest

from app import main
from app.main import backup_billing_database
from app.backups import RetentionPolicy, prune_backups
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


HOUR = 60 * 60
DAY = 24 * HOUR
# 2024-01-01 00:00 UTC, a Monday.
MONDAY = 1_704_067_200
DEFAULT_POLICY = RetentionPolicy(keep_all=DAY, hourly=2 * DAY, daily=14 * DAY, weekly=56 * DAY, monthly=0)


def simulate(policy, days, interval=15 * 60):
    """Back up every interval for days, pruning after each backup."""
    kept = set()
    now = MONDAY
    while now < MONDAY + days * DAY:
        kept.add(now)
        kept = policy.keep(kept, now)
        now += interval
    return kept, now - interval


def test_history_thins_out_with_age():
    kept, now = simulate(DEFAULT_POLICY, 70)

    def between(start, end):
        return sorted(t for t in kept if start <= now - t < end)

    # Every backup of the last day.
    assert len(between(0, DAY)) == 96
    # Then the first of each hour, then of each day, then of each Monday.
    assert between(DAY, 2 * DAY) and all(t % HOUR == 0 for t in between(DAY, 2 * DAY))
    assert len(between(DAY, 2 * DAY)) == 24
    assert all(t % DAY == 0 for t in between(2 * DAY, 14 * DAY))
    assert len(between(2 * DAY, 14 * DAY)) == 12
    weekly = between(14 * DAY, 70 * DAY)
    assert weekly and all(t % DAY == 0 and (t // DAY + 3) % 7 == 0 for t in weekly)
    assert len(weekly) == 6
    assert len(kept) == 96 + 24 + 12 + 6


def test_monthly_step_keeps_the_first_backup_of_each_month():
    policy = RetentionPolicy(keep_all=DAY, hourly=0, daily=7 * DAY, weekly=0, monthly=400 * DAY)
    kept, now = simulate(policy, 100, interval=6 * HOUR)

    old = sorted(t for t in kept if now - t >= 7 * DAY)
    assert [time.strftime("%Y-%m-%d %H:%M", time.gmtime(t)) for t in old] == [
        "2024-01-01 00:00", "2024-02-01 00:00", "2024-03-01 00:00", "2024-04-01 00:00",
    ]


def test_retention_limits_every_step_but_keeps_the_newest():
    capped = RetentionPolicy(keep_all=DAY, hourly=2 * DAY, daily=14 * DAY, weekly=56 * DAY, monthly=0, max_age=5 * DAY)
    kept, now = simulate(capped, 30, interval=HOUR)
    assert max(now - t for t in kept) < 5 * DAY

    # The server was off for a year: the last backup stays.
    assert capped.keep({MONDAY}, MONDAY + 365 * DAY) == {MONDAY}
    assert DEFAULT_POLICY.keep(set(), MONDAY) == set()


def test_policy_comes_from_settings(api_context):
    _, settings, _ = api_context
    settings.billing.backup_keep_all = "6h"
    settings.billing.backup_keep_weekly = "0s"
    settings.billing.backup_retention = "30d"

    policy = RetentionPolicy.from_settings(settings.billing)

    assert policy == RetentionPolicy(6 * HOUR, 2 * DAY, 14 * DAY, 0, 0, 30 * DAY)
    assert policy.longest() == 14 * DAY


def test_backup_run_prunes_by_policy(api_context):
    _, settings, _ = api_context
    settings.billing.backup_keep_all = "1h"
    settings.billing.backup_keep_hourly = "0s"
    settings.billing.backup_keep_daily = "0s"
    settings.billing.backup_keep_weekly = "0s"
    backup_dir = Path(settings.billing.backup_dir)
    backup_dir.mkdir(parents=True)
    now = int(time.time())
    expired = backup_dir / f"billing-{now - 2 * HOUR}.sqlite3"
    kept = backup_dir / f"billing-{now - 60}.sqlite3"
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
        tmp_path / "billing-9000.sqlite3",
    ]
    for path in files:
        path.touch()
    (tmp_path / f"billing-{old - 1}.sqlite3").mkdir()
    policy = RetentionPolicy(keep_all=60, hourly=0, daily=0, weekly=0, monthly=0)

    removed = prune_backups(str(tmp_path), policy, now=10_000)

    assert removed == [files[0]]
    # The newest backup is kept even though it is old.
    assert [path.exists() for path in files] == [False, True, True, True, True]


def test_prune_handles_missing_directory(tmp_path):
    assert prune_backups(str(tmp_path / "missing"), DEFAULT_POLICY) == []


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

    assert settings.billing.backup_interval == 15 * 60
    assert RetentionPolicy.from_settings(settings.billing) == DEFAULT_POLICY
    assert settings.billing.backup_retention is None


@pytest.mark.parametrize("line", ["BILLING_BACKUP_RETENTION=forever", "BILLING_BACKUP_KEEP_DAILY=-1d"])
def test_invalid_backup_setting_fails_at_load(tmp_path, line):
    env_file = tmp_path / ".env"
    env_file.write_text(line + "\n")

    with pytest.raises(ConfigError, match=line.split("=")[0]):
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


# --- PostgreSQL ----------------------------------------------------------------

@pytest.fixture
def postgres_billing(default_settings, tmp_path):
    from app.billing import Billing

    secrets = Path(default_settings.secrets_dir)
    secrets.mkdir(parents=True)
    (secrets / "postgres_password").write_text("s3cret/pass\n", encoding="utf-8")
    billing = Billing()
    assert billing._postgres
    return billing


def test_postgres_password_is_not_a_command_argument(postgres_billing, tmp_path, monkeypatch):
    import subprocess

    calls = []

    def run(command, env=None, **kwargs):
        calls.append((command, env))
        if command[0] == "pg_dump":
            Path(command[command.index("--file") + 1]).write_bytes(b"PGDMP")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)

    postgres_billing.backup(str(tmp_path / "backups" / "billing-1.dump"))

    assert [command[0] for command, _ in calls] == ["pg_dump", "pg_restore"]
    for command, env in calls:
        assert not any("s3cret" in argument for argument in command)
        assert env["PGPASSWORD"] == "s3cret/pass"
    assert calls[0][0][-1] == "postgresql://billing@postgres:5432/billing"
    assert (tmp_path / "backups" / "billing-1.dump").read_bytes() == b"PGDMP"


def test_failed_postgres_backup_error_has_no_password(postgres_billing, tmp_path, monkeypatch):
    import subprocess

    def run(command, env=None, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "pg_dump: error: connection refused\n")

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(RuntimeError) as error:
        postgres_billing.backup(str(tmp_path / "backups" / "billing-1.dump"))

    assert str(error.value) == "pg_dump failed with exit status 1: pg_dump: error: connection refused"
    assert list((tmp_path / "backups").iterdir()) == []


def test_postgres_connections_time_out(postgres_billing, monkeypatch):
    import sys
    import types

    seen = {}

    def connect(url, **kwargs):
        seen.update(kwargs, url=url)
        raise TimeoutError

    psycopg = types.SimpleNamespace(connect=connect)
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", types.SimpleNamespace(dict_row=None))

    with pytest.raises(TimeoutError):
        postgres_billing._connect_raw()

    assert seen["connect_timeout"] > 0
    assert seen["url"] == "postgresql://billing:s3cret%2Fpass@postgres:5432/billing"
