import io
import sqlite3
import time
from pathlib import Path

import pytest
from pyrage import x25519

from app import backups, config, main, online_check
from app.admin import status
from app.backups import OffsiteStore, RetentionPolicy, offsite_key, sync_offsite
from app.requirements import build_requirements

HOUR = 60 * 60
DAY = 24 * HOUR


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """The S3 calls the backups make, on an in-memory bucket."""

    def __init__(self, fail_on=None):
        self.objects = {}
        self.fail_on = fail_on

    def _check(self, operation):
        if operation == self.fail_on:
            raise ClientError("AccessDenied")

    def put_object(self, Bucket, Key, Body):
        self._check("put")
        self.objects[Key] = bytes(Body)

    def head_object(self, Bucket, Key):
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket, Key):
        self._check("get")
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        self._check("delete")
        self.objects.pop(Key, None)

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        client = self

        class Paginator:
            def paginate(self, Bucket, Prefix):
                client._check("list")
                keys = sorted(key for key in client.objects if key.startswith(Prefix))
                yield {"Contents": [{"Key": key, "Size": len(client.objects[key])} for key in keys[:2]]}
                yield {"Contents": [{"Key": key, "Size": len(client.objects[key])} for key in keys[2:]]}

        return Paginator()


@pytest.fixture
def identity():
    return x25519.Identity.generate()


@pytest.fixture
def offsite(api_context, identity, monkeypatch):
    """Off-site backups configured, with an in-memory bucket."""
    _, settings, billing = api_context
    settings.offsite.endpoint = "https://storage.example.com"
    settings.offsite.bucket = "backups"
    settings.offsite.recipient = str(identity.to_public())
    client = FakeS3()
    store = OffsiteStore(client, "backups", settings.offsite.recipient)
    monkeypatch.setattr(OffsiteStore, "from_settings", classmethod(lambda cls, config=None: store))
    return settings, billing, store


def make_backups(directory, timestamps):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for timestamp in timestamps:
        with sqlite3.connect(directory / f"billing-{timestamp}.sqlite3") as connection:
            connection.execute("CREATE TABLE marker (at INTEGER)")
            connection.execute("INSERT INTO marker VALUES (?)", (timestamp,))
        connection.close()


def test_backup_is_encrypted_and_copied_off_site(offsite, identity):
    settings, billing, store = offsite
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 5}
    billing.credit_purchase("user-1", "starter", "token", "order-1", "user@example.com")

    main.run_billing_backup()

    [key] = store.client.objects
    encrypted = store.client.objects[key]
    assert key.startswith("billing-backups/billing-") and key.endswith(".sqlite3.age")
    # Nothing readable leaves this machine.
    assert b"user@example.com" not in encrypted and b"SQLite format" not in encrypted
    assert store.download(key, str(identity)).startswith(b"SQLite format 3")
    assert backups.offsite_status(billing)["succeeded_at"] is not None


def test_off_site_history_follows_the_policy_and_survives_losing_this_disk(offsite):
    settings, _, store = offsite
    policy = RetentionPolicy(keep_all=HOUR, hourly=0, daily=10 * DAY, weekly=0, monthly=0)
    now = 1_704_067_200 + 20 * DAY
    old_days = [now - day * DAY for day in (12, 5, 3)]
    make_backups(settings.billing.backup_dir, old_days)
    sync_offsite(store, settings.billing.backup_dir, policy, now)
    # The 12-day-old backup is past every step; there is a newer one.
    assert set(store.backups()) == set(old_days[1:])

    # The disk is replaced: only a new backup exists here.
    for path in Path(settings.billing.backup_dir).iterdir():
        path.unlink()
    make_backups(settings.billing.backup_dir, [now])
    uploaded, deleted = sync_offsite(store, settings.billing.backup_dir, policy, now)

    assert (uploaded, deleted) == (1, 0)
    assert set(store.backups()) == {*old_days[1:], now}


def test_off_site_failure_keeps_the_local_backup_and_is_reported(offsite, capsys):
    settings, billing, store = offsite
    store.client.fail_on = "put"

    assert backups.run_backup(billing) is False

    assert len(backups.local_backups(settings.billing.backup_dir)) == 1
    state = backups.offsite_status(billing)
    assert state["error"] == "ClientError: AccessDenied" and state["succeeded_at"] is None
    rendered = status.render(status.collect())
    assert "off-site copy FAILED" in rendered and "AccessDenied" in rendered

    store.client.fail_on = None
    assert backups.run_backup(billing) is True
    assert backups.offsite_status(billing)["error"] is None
    assert "off-site copy" in status.render(status.collect())


def test_status_without_off_site_backups(api_context):
    assert "Backups       none yet; off-site off" in status.render(status.collect())


def test_fetch_downloads_decrypts_and_checks(offsite, identity, capsys):
    settings, _, store = offsite
    make_backups(settings.billing.backup_dir, [1000, 2000])
    sync_offsite(store, settings.billing.backup_dir, RetentionPolicy(DAY, 0, 0, 0, 0), 2500)

    assert backups.fetch("latest", f"# created: today\n{identity}\n") == 0

    restored = Path(settings.billing.backup_dir).parent / "restore" / "billing-2000.sqlite3"
    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT at FROM marker").fetchone() == (2000,)
    connection.close()
    assert str(restored) in capsys.readouterr().out

    # As piped by PowerShell, with a byte order mark.
    assert backups.fetch("billing-1000.sqlite3.age", f"﻿{identity}") == 0
    assert backups.fetch("billing-3000", str(identity)) == 1
    assert backups.fetch("latest", "not a key") == 1
    assert "AGE-SECRET-KEY" in capsys.readouterr().err


def test_fetch_counts_from_the_newest(offsite, identity, capsys):
    settings, _, store = offsite
    make_backups(settings.billing.backup_dir, [1000, 2000, 3000])
    sync_offsite(store, settings.billing.backup_dir, RetentionPolicy(DAY, 0, 0, 0, 0), 3500)

    assert backups.fetch("2", str(identity)) == 0
    assert "billing-2000.sqlite3" in capsys.readouterr().out
    assert backups.fetch("3", str(identity)) == 0
    assert "billing-1000.sqlite3" in capsys.readouterr().out

    assert backups.fetch("4", str(identity)) == 1
    assert backups.fetch("0", str(identity)) == 1
    assert "choose 1 (the newest) to 3" in capsys.readouterr().err


def test_fetch_with_the_wrong_key_fails(offsite):
    settings, _, store = offsite
    make_backups(settings.billing.backup_dir, [1000])
    sync_offsite(store, settings.billing.backup_dir, RetentionPolicy(DAY, 0, 0, 0, 0), 1500)

    with pytest.raises(Exception):
        backups.fetch("latest", str(x25519.Identity.generate()))


def test_keygen_prints_a_working_pair(capsys):
    assert backups.keygen() == 0
    output = capsys.readouterr().out
    recipient = output.split("OFFSITE_BACKUP_RECIPIENT=")[1].split()[0]
    secret = next(line for line in output.splitlines() if line.startswith("AGE-SECRET-KEY-"))
    assert str(x25519.Identity.from_str(secret).to_public()) == recipient


def test_now_command_backs_up_and_copies(offsite, capsys):
    settings, _, store = offsite

    assert backups.main(["now"]) == 0
    assert len(store.client.objects) == 1
    assert "copied off-site" in capsys.readouterr().out


def test_list_shows_both_places(offsite, capsys):
    settings, _, store = offsite
    make_backups(settings.billing.backup_dir, [1_704_067_200])
    sync_offsite(store, settings.billing.backup_dir, RetentionPolicy(DAY, 0, 0, 0, 0), 1_704_067_200)

    assert backups.list_backups() == 0
    output = capsys.readouterr().out
    assert "Local" in output and "Off-site (backups): 1" in output and "  1. 2024-01-01 00:00 UTC" in output


# --- Configuration --------------------------------------------------------------

def test_off_site_settings_are_validated(api_context, identity):
    _, settings, _ = api_context
    settings.offsite.bucket = "backups"

    problems = config.validate_settings(settings, "api")
    assert any("OFFSITE_BACKUP_ENDPOINT is required" in problem for problem in problems)
    assert any("OFFSITE_BACKUP_RECIPIENT is required" in problem for problem in problems)
    assert any("offsite_backup_access_key" in problem for problem in problems)

    settings.offsite.recipient = "age1notakey"
    assert any("not an age public key" in problem for problem in config.validate_settings(settings, "api"))

    settings.offsite.endpoint = "https://storage.example.com"
    settings.offsite.recipient = str(identity.to_public())
    secrets = Path(settings.secrets_dir)
    secrets.mkdir(parents=True, exist_ok=True)
    (secrets / "offsite_backup_access_key").write_text("id")
    (secrets / "offsite_backup_secret_key").write_text("secret")
    reloaded = config.load_settings(env_file=None, secrets_dir=str(secrets))
    reloaded.offsite = settings.offsite
    assert config.validate_settings(reloaded, "api") == []
    # The worker never backs up.
    settings.offsite.recipient = None
    assert config.validate_settings(settings, "worker") == []


def test_sharing_the_outsourcing_bucket_is_warned_about(api_context):
    _, settings, _ = api_context
    settings.storage.endpoint = settings.offsite.endpoint = "https://storage.example.com"
    settings.storage.bucket = settings.offsite.bucket = "shared"

    warnings = config.configuration_warnings(settings, {})

    assert any("outsourcing bucket" in warning for warning in warnings)


def test_requirements_follow_the_retention(api_context):
    _, settings, _ = api_context
    settings.billing.backup_keep_weekly = "10w"

    [section] = [section for section in build_requirements(settings) if section.name == "Off-site backups"]

    assert not section.enabled
    deletion = next(item for item in section.requirements if item.item == "Automatic deletion")
    assert "70 days" in deletion.value


# --- Online check ---------------------------------------------------------------

def test_online_check_passes_and_leaves_nothing(offsite):
    settings, _, store = offsite

    result = online_check.check_offsite(settings)

    assert result.status == "ok"
    assert store.client.objects == {}


@pytest.mark.parametrize(("fail_on", "detail"), [("put", "Upload"), ("get", "Download"), ("list", "Listing"), ("delete", "Delete")])
def test_online_check_names_the_failing_step(offsite, fail_on, detail):
    settings, _, store = offsite
    store.client.fail_on = fail_on

    result = online_check.check_offsite(settings)

    assert result.status == "fail"
    assert result.detail == f"{detail}: AccessDenied"
    assert "list, read, write, and delete" in result.hint


def test_online_checks_skip_off_site_when_off(api_context, monkeypatch):
    _, settings, _ = api_context

    results = online_check.run_checks(settings)

    assert online_check.Result("Off-site backups", "skip", "OFFSITE_BACKUP_BUCKET is not set") in results


def test_offsite_key_names():
    assert offsite_key(123) == "billing-backups/billing-123.dump.age"
    assert offsite_key(123, "sqlite3") == "billing-backups/billing-123.sqlite3.age"
    assert backups.OFFSITE_KEY_PATTERN.match(offsite_key(int(time.time())))
