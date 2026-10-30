from pathlib import Path

import pytest
from redis.exceptions import AuthenticationError, ResponseError

from app import config as app_config
from app import main, online_check, requirements
from app.config import configuration_warnings, runs_elsewhere, validate_settings


ROOT = Path(__file__).resolve().parent.parent


# --- Redis over TLS ------------------------------------------------------------------

def test_a_provider_ca_is_used_for_tls_only(default_settings, tmp_path):
    ca = tmp_path / "redis_ca.crt"
    ca.write_text("certificate", encoding="utf-8")
    default_settings.service.redis_ca_file = str(ca)

    default_settings.service.redis_url = "rediss://app@redis.example.com:6380/0"
    assert default_settings.service.redis_options() == {"ssl_ca_certs": str(ca)}

    # redis-py refuses TLS options for a plain connection.
    default_settings.service.redis_url = "redis://app@redis:6379/0"
    assert default_settings.service.redis_options() == {}


def test_the_ca_comes_with_the_password(default_settings, tmp_path):
    password = tmp_path / "password"
    password.write_text("s3cret", encoding="utf-8")
    default_settings.service.redis_url = "rediss://app@redis.example.com:6380/0"
    default_settings.service.redis_password_file = str(password)
    default_settings.service.redis_ca_file = "/run/secrets/redis_ca.crt"

    assert default_settings.service.redis_options() == {"ssl_ca_certs": "/run/secrets/redis_ca.crt", "password": "s3cret"}


def test_a_ca_without_tls_is_a_mistake(default_settings, tmp_path):
    ca = tmp_path / "redis_ca.crt"
    ca.write_text("certificate", encoding="utf-8")
    default_settings.service.redis_ca_file = str(ca)

    assert any("not a rediss:// URL" in problem for problem in validate_settings(default_settings, "api"))


def test_a_missing_ca_file_is_a_mistake(default_settings):
    default_settings.service.redis_url = "rediss://app@redis.example.com:6380/0"
    default_settings.service.redis_ca_file = "/run/secrets/missing.crt"

    assert any("does not exist" in problem for problem in validate_settings(default_settings, "api"))


@pytest.mark.parametrize("url, elsewhere", [
    ("redis://app@redis:6379/0", False),
    ("postgresql://billing@postgres:5432/billing", False),
    ("redis://localhost:6379/0", False),
    ("rediss://app@eu1.redis.example.com:6380/0", True),
    ("postgresql://billing@db.example.com/billing?sslmode=verify-full", True),
    ("", False),
])
def test_what_runs_outside_the_stack(url, elsewhere):
    assert runs_elsewhere(url) is elsewhere


def test_check_warns_about_unencrypted_services_elsewhere(default_settings):
    default_settings.service.redis_url = "redis://app@redis.example.com:6379/0"
    default_settings.database.url = "postgresql://billing@db.example.com/billing"

    warnings = configuration_warnings(default_settings, {})

    assert any("REDIS_URL leads to redis.example.com without TLS" in warning for warning in warnings)
    assert any("db.example.com is reached without required TLS" in warning for warning in warnings)


def test_check_is_quiet_about_encrypted_and_local_services(default_settings):
    assert not any("TLS" in warning for warning in configuration_warnings(default_settings, {}))

    default_settings.service.redis_url = "rediss://app@redis.example.com:6380/0"
    default_settings.database.url = "postgresql://billing@db.example.com/billing?sslmode=verify-full"

    assert not any("TLS" in warning for warning in configuration_warnings(default_settings, {}))


def test_the_database_url_secret_is_the_one_checked(default_settings):
    default_settings.secrets.database_url = "postgresql://billing:pw@db.example.com/billing?sslmode=require"

    assert not any("TLS" in warning for warning in configuration_warnings(default_settings, {}))


# --- The online check on managed Redis ----------------------------------------------------

class FakeClient:
    def __init__(self, url, answers):
        self.url = url
        self.answers = answers

    def ping(self):
        answer = self.answers["anonymous" if "@" not in self.url else "app"]
        if isinstance(answer, Exception):
            raise answer
        return True

    def config_get(self, name):
        raise self.answers["config"]


def managed_check(default_settings, tmp_path, config_error):
    password = tmp_path / "password"
    password.write_text("s3cret", encoding="utf-8")
    default_settings.service.redis_url = "rediss://app@redis.example.com:6380/0"
    default_settings.service.redis_password_file = str(password)
    default_settings.service.redis_ca_file = str(password)
    options = []

    def connect(url, **arguments):
        options.append(arguments)
        return FakeClient(url, {"anonymous": AuthenticationError("auth"), "app": True, "config": config_error})

    return online_check.check_redis(default_settings, connect), options


def test_managed_redis_without_config_passes(default_settings, tmp_path):
    result, options = managed_check(default_settings, tmp_path, ResponseError("ERR unknown command 'CONFIG'"))

    assert result.status == "ok"
    # Both probes, with and without the password, verify the certificate.
    assert all(arguments.get("ssl_ca_certs") for arguments in options)


def test_another_config_error_fails(default_settings, tmp_path):
    result, _ = managed_check(default_settings, tmp_path, ResponseError("ERR something else"))

    assert result.status == "fail"


# --- Backups off --------------------------------------------------------------------------

def test_backups_are_the_servers_by_default(default_settings):
    assert default_settings.database.backups == "server"


def test_the_interval_must_be_positive(default_settings):
    with pytest.raises(ValueError):
        default_settings.database.backup_interval = "0s"


def test_the_server_makes_no_backups_when_the_provider_does(default_settings, monkeypatch):
    default_settings.database.backups = "provider"
    monkeypatch.setattr(main, "run_billing_backup", lambda: pytest.fail("backed up"))
    monkeypatch.setattr(main.time, "sleep", lambda seconds: pytest.fail("waited"))

    main.backup_billing_database()


def test_check_reminds_that_the_provider_must_back_up(default_settings):
    default_settings.database.backups = "provider"

    assert any("DATABASE_BACKUPS=provider" in warning for warning in configuration_warnings(default_settings, {}))


def test_offsite_copies_need_the_backups(default_settings):
    default_settings.database.backups = "provider"

    default_settings.offsite.bucket = "backups"

    assert "OFFSITE_BACKUP_BUCKET copies the server's backups, which DATABASE_BACKUPS=provider turns off" in (
        validate_settings(default_settings, "api")
    )


# --- Requirements -------------------------------------------------------------------------

def sections(config):
    return {section.name: section for section in requirements.database_sections(config)}


def test_requirements_for_the_compose_stack_need_no_managed_service(default_settings):
    found = sections(default_settings)

    assert not found["Redis"].enabled and not found["PostgreSQL"].enabled


def test_requirements_for_managed_services(default_settings):
    default_settings.service.redis_url = "rediss://app@redis.example.com:6380/0"
    default_settings.database.url = "postgresql://billing@db.example.com/billing?sslmode=verify-full"

    found = sections(default_settings)

    assert found["Redis"].enabled and found["Redis"].note == "enabled (REDIS_URL leads to redis.example.com)"
    assert found["PostgreSQL"].enabled
    items = {item.item: item.value for item in found["Redis"].requirements}
    assert items["Eviction"] == "none (maxmemory-policy noeviction)"
    version = {item.item: item.value for item in found["PostgreSQL"].requirements}["Version"]
    assert version == f"{requirements.POSTGRES_MAJOR_VERSION} or older"


def test_without_backups_the_provider_must_keep_them(default_settings):
    default_settings.database.backups = "provider"

    items = {item.item: item.value for item in sections(default_settings)["PostgreSQL"].requirements}

    assert items["Version"] == "11 or later"
    assert items["Backups"].startswith("automatic backups with point-in-time recovery, kept 56 days")


def test_the_postgres_version_follows_the_compose_image():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert f"image: postgres:{requirements.POSTGRES_MAJOR_VERSION}-alpine" in compose
