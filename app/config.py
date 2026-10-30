"""Typed configuration for the API, workers, and services.

Settings are grouped by concern. Each group reads its own environment
variables (and `.env`), so the variable names stay short and stable:
`settings.worker.batch_size` is `WORKER_BATCH_SIZE`.

Secrets are files in `SECRETS_DIR` (Compose mounts `./secrets` at
`/run/secrets`); the file name is the setting name. They are typed as
`SecretStr` so they never appear in logs or reprs.

This module must stay importable without torch or GPU libraries.

Run `python -m app.config check` to validate and print the configuration.
"""

import difflib
import json
import logging
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Sequence
from typing import Annotated, Any, Callable, Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import BeforeValidator, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


logger = logging.getLogger(__name__)

SECRETS_DIR = "/run/secrets"
# Unknown variables with these prefixes are reported as possible typos.
CHECKED_PREFIXES = ("WORKER_", "BILLING_", "DATABASE_", "JOB_", "OUTSOURCE_", "STORAGE_", "RUNPOD_", "GOOGLE_PLAY_", "CLOUDFLARE_", "OFFSITE_", "ERROR_REPORT_")
MIN_SERVER_PASSWORD_LENGTH = 12
# Read from .env by Docker Compose itself (docker-compose.yml), not by the server.
COMPOSE_VARIABLES = {"API_BIND_ADDRESS", "POSTGRES_DATA_LOCATION", "BILLING_DATA_GROUP", "WORKER_GPU_BUILD"}
# The two files: .env holds what Docker Compose reads, server.env the
# server's settings, the same on every deployment. COMPOSE_PROFILES and
# ADMIN_WEB_MODE are both: Compose starts services by the one and picks the
# admin service's roles by the other, and the services read both, as they
# get .env too. Later files win, as in docker-compose.yml.
COMPOSE_ENV_FILE = ".env"
SERVER_ENV_FILE = "server.env"
ENV_FILES = (COMPOSE_ENV_FILE, SERVER_ENV_FILE)
COMPOSE_ONLY_VARIABLES = COMPOSE_VARIABLES | {"COMPOSE_PROFILES", "ADMIN_WEB_MODE"}
# The Compose profile that starts the cloudflared service.
TUNNEL_PROFILE = "cloudflare"
# The admin web page: its Compose profile, and its modes from least to most access.
ADMIN_WEB_PROFILE = "admin"
ADMIN_WEB_MODES = ("disabled", "read-only", "read-write")
# In a folder of its own, mounted into cloudflared alone. Docker creates a
# missing mount source as a directory; an empty folder is harmless, a
# directory where the token file should be is not.
TUNNEL_TOKEN_FILE = "cloudflare/tunnel_token"
HOSTNAME_PATTERN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class ConfigError(ValueError):
    """The configuration is invalid. The message names the variables."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("Invalid configuration:\n" + "\n".join(f"  - {problem}" for problem in problems))


# --- Shared value types --------------------------------------------------

DURATION_UNITS = {"s": 1, "m": 60, "h": 60 * 60, "d": 24 * 60 * 60, "w": 7 * 24 * 60 * 60}


def duration_seconds(value: str, name: str = "value", allow_zero: bool = False) -> float:
    """Parse a duration such as 30s, 10m, 1.5h, 7d, or 1w into seconds."""
    text = str(value).strip().lower()
    try:
        amount = float(text[:-1])
        multiplier = DURATION_UNITS[text[-1]]
    except (KeyError, ValueError, IndexError) as error:
        raise ValueError(f"{name} must use a format such as 30s, 10m, 12h, 7d, or 1w") from error
    if not math.isfinite(amount) or amount < 0 or (amount == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'zero or more' if allow_zero else 'positive'}")
    return amount * multiplier


def _duration(value: Any, allow_zero: bool = False) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
            raise ValueError("must be zero or more" if allow_zero else "must be positive")
        return float(value)
    return duration_seconds(value, "the value", allow_zero)


def _duration_or_zero(value: Any) -> float:
    return _duration(value, allow_zero=True)


def _optional_duration(value: Any) -> float | None:
    return None if value is None else _duration(value)


def _duration_text(value: Any) -> Any:
    if value is not None:
        duration_seconds(value, "the value")
        return str(value).strip().lower()
    return value


def _auto_or_positive_int(value: Any) -> int | None:
    """The one parser for settings that accept `auto` or a positive integer."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("", "auto"):
            return None
        try:
            value = int(text)
        except ValueError as error:
            raise ValueError("must be 'auto' or a positive integer") from error
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("must be 'auto' or a positive integer")
    return value


def _unlimited_or_positive(value: Any) -> int | float | None:
    """Parse a positive number or `unlimited`, represented internally as None."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("", "unlimited"):
            return None
        value = float(text)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("must be 'unlimited' or a positive number")
    return value


def _unlimited_or_positive_int(value: Any) -> int | None:
    parsed = _unlimited_or_positive(value)
    if parsed is None:
        return None
    if isinstance(parsed, float) and not parsed.is_integer():
        raise ValueError("must be 'unlimited' or a positive integer")
    return int(parsed)


# Seconds, parsed from s/m/h/d/w strings.
Duration = Annotated[float, BeforeValidator(_duration)]
# The same, but 0s is allowed.
DurationOrZero = Annotated[float, BeforeValidator(_duration_or_zero)]
# The same, or None when unset.
OptionalDuration = Annotated[float | None, BeforeValidator(_optional_duration)]
# The original s/m/h/d/w string, validated. Used where the API returns it.
DurationText = Annotated[str | None, BeforeValidator(_duration_text)]
# None means `auto`.
AutoInt = Annotated[int | None, BeforeValidator(_auto_or_positive_int)]
UnlimitedNumber = Annotated[float | None, BeforeValidator(_unlimited_or_positive)]
UnlimitedInt = Annotated[int | None, BeforeValidator(_unlimited_or_positive_int)]


# --- Settings groups -----------------------------------------------------

def _group_config(prefix: str = "") -> SettingsConfigDict:
    return SettingsConfigDict(
        env_prefix=prefix,
        env_file=ENV_FILES,
        env_ignore_empty=True,
        extra="ignore",
        populate_by_name=True,
        validate_assignment=True,
    )


class ServiceSettings(BaseSettings):
    """Container wiring. Docker Compose sets these; the defaults suit local development."""

    model_config = _group_config()

    redis_url: str = "redis://localhost:6379/0"
    # The password of the Redis user named in redis_url, as a file; empty
    # for a Redis without users, such as one for local development.
    redis_password_file: str = ""
    # For a rediss:// URL whose certificate is signed by the provider's own
    # CA rather than a public one, as many managed Redis services do.
    redis_ca_file: str = ""
    data_dir: str = "/data"
    # Where the services run: local (outside containers), compose, or
    # kubernetes. Set as wiring; it turns the checks of one on or off, such
    # as Compose's profiles, which Kubernetes does not have.
    deployment: Literal["local", "compose", "kubernetes"] = "local"

    def redis_tls_options(self) -> dict[str, str]:
        """Keyword arguments that verify a rediss:// server's certificate
        with redis_ca_file. A redis:// URL takes none."""
        if self.redis_ca_file and self.redis_url.startswith("rediss://"):
            return {"ssl_ca_certs": self.redis_ca_file}
        return {}

    def redis_options(self) -> dict[str, str]:
        """Keyword arguments that sign in to Redis, for Redis.from_url: the
        TLS options, and the password from redis_password_file. It stays out
        of the URL, which libraries may show in errors."""
        if not self.redis_password_file:
            return self.redis_tls_options()
        try:
            password = Path(self.redis_password_file).read_text(encoding="utf-8").strip()
        except OSError as error:
            raise RuntimeError(
                f"Cannot read the Redis password file {self.redis_password_file} ({type(error).__name__}); "
                "python scripts/server.py start creates it"
            ) from None
        return {**self.redis_tls_options(), "password": password}


class LimitsSettings(BaseSettings):
    model_config = _group_config()

    max_video_length_minutes: UnlimitedNumber = Field(default=120, gt=0)
    max_upload_size_mb: UnlimitedNumber = Field(default=200, gt=0)
    # Jobs being uploaded, checked, queued, or transcribed at once.
    max_active_jobs: UnlimitedInt = Field(default=50, ge=1)
    # The same per signed-in user; only applies with billing.
    max_active_jobs_per_user: UnlimitedInt = Field(default=5, ge=1)
    # How long a finished job's status and MIDI file can be retrieved.
    result_retention: Duration = 60 * 60.0
    # After a video site rate-limits this server, links are refused this
    # long; each incident in a row doubles it, up to the maximum.
    video_site_cooldown: Duration = 30 * 60.0
    video_site_max_cooldown: Duration = 24 * 60 * 60.0
    # After a video site times out a few times within minutes, its links are
    # refused this long, doubling up to video_site_cooldown.
    video_site_timeout_cooldown: Duration = 2 * 60.0
    # Least time between the starts of two requests that read a link from a site.
    video_site_interval: DurationOrZero = 3.0
    # Downloads from one video site at once, across the API and the workers.
    video_site_max_downloads: UnlimitedInt = Field(default=2, ge=1)
    # Video checks at once, across every API replica.
    max_video_checks: UnlimitedInt = Field(default=4, ge=1)
    source_url_allowed_hosts: list[str] = [
        "youtube.com", "youtu.be", "youtube-nocookie.com",
        "vimeo.com", "soundcloud.com", "bandcamp.com", "dailymotion.com",
        "instagram.com", "instagr.am",
        "facebook.com", "fb.watch",
        "tiktok.com",
        "x.com", "twitter.com",
        "reddit.com", "redd.it",
        "twitch.tv",
    ]


class WorkerSettings(BaseSettings):
    model_config = _group_config("WORKER_")

    threads: AutoInt = None
    # Ignore CUDA devices and run the local worker on CPU.
    enforce_cpu: bool = False
    model_memory_gib: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    memory_per_job_gib: float = Field(default=1.1, gt=0, allow_inf_nan=False)
    # System RAM reserved for the model and each additional CPU job.
    cpu_model_memory_gib: float = Field(default=2.0, gt=0, allow_inf_nan=False)
    cpu_memory_per_job_gib: float = Field(default=2.0, gt=0, allow_inf_nan=False)
    # auto: each GPU's job capacity; CPU and RAM dependent in CPU mode.
    batch_size: AutoInt = None
    batch_timeout: Duration = 2.0
    max_concurrency: AutoInt = None


class JobSettings(BaseSettings):
    model_config = _group_config("JOB_")

    # local: job files on the data volume, which one machine's services
    # share. s3: in the outsourcing bucket, for API replicas and workers on
    # several machines; see app/job_storage.py.
    storage: Literal["local", "s3"] = "local"
    # How long each job's full error is kept for administrators.
    diagnostics_retention: Duration = 30 * 24 * 60 * 60.0


class CleanupSettings(BaseSettings):
    model_config = _group_config("CLEANUP_")

    interval: Duration = 600.0


class AuthSettings(BaseSettings):
    model_config = _group_config("GOOGLE_OAUTH_")

    client_ids: list[str] = Field(default_factory=list)


class DatabaseSettings(BaseSettings):
    """The PostgreSQL database every server has, with or without billing: job
    diagnostics and the admin audit log, and with billing also balances and
    purchases. Its backups."""

    model_config = _group_config("DATABASE_")

    # path remains for the explicit SQLite compatibility used by local tests
    # and migration tools.
    url: str = "postgresql://billing@postgres:5432/billing"
    path: str = "/billing/billing.sqlite3"
    backup_dir: str = "/billing/backups"
    # Who backs the database up: the server, with pg_dump, or the provider
    # of a managed database, which then must; see app/requirements.py.
    backups: Literal["server", "provider"] = "server"
    backup_interval: Duration = 15 * 60.0
    # Backups kept, by age: every one, then one per hour, day, week, and
    # month (UTC). 0 turns a step off. See app/backups.py.
    backup_keep_all: DurationOrZero = 24 * 60 * 60.0
    backup_keep_hourly: DurationOrZero = 2 * 24 * 60 * 60.0
    backup_keep_daily: DurationOrZero = 14 * 24 * 60 * 60.0
    backup_keep_weekly: DurationOrZero = 8 * 7 * 24 * 60 * 60.0
    backup_keep_monthly: DurationOrZero = 0.0
    # Optional upper limit: older backups are deleted whatever the steps say.
    backup_retention: OptionalDuration = None


class BillingSettings(BaseSettings):
    model_config = _group_config("BILLING_")

    provider: Literal["none", "google_play"] = "none"
    products: dict[str, int] = Field(default_factory=dict)
    free_minutes: int | None = Field(default=None, ge=0, validation_alias="FREE_MINUTES")
    free_minutes_period: DurationText = Field(default=None, validation_alias="FREE_MINUTES_PERIOD")


class GooglePlaySettings(BaseSettings):
    model_config = _group_config("GOOGLE_PLAY_")

    package_name: str | None = None
    # How often refunded and charged-back purchases are read from Google Play.
    voided_purchases_interval: Duration = 60 * 60.0
    # Deprecated: a file name in SECRETS_DIR, or an absolute path.
    service_account_file: str | None = None


class SecretSettings(BaseSettings):
    """Secrets, read only from files in SECRETS_DIR, never from the
    environment or .env. The file name is the setting name."""

    model_config = _group_config()

    google_play_service_account: SecretStr | None = Field(
        default=None,
        validation_alias="google_play_service_account.json",
    )
    runpod_api_key: SecretStr | None = None
    # Optional. A Restricted key cannot use RunPod's management API, so
    # reading the endpoint's settings needs a separate key with read access.
    runpod_read_api_key: SecretStr | None = None
    storage_access_key: SecretStr | None = None
    storage_secret_key: SecretStr | None = None
    offsite_backup_access_key: SecretStr | None = None
    offsite_backup_secret_key: SecretStr | None = None
    # Optional. When present, every request except /health and /ready needs it.
    server_password: SecretStr | None = None
    database_url: SecretStr | None = None
    # Sent with every error report to ERROR_REPORT_URL.
    error_report_token: SecretStr | None = None
    # Optional. Turns on GET /metrics, for a scraper that sends it.
    metrics_token: SecretStr | None = None

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        return init_settings, file_secret_settings


class OutsourceSettings(BaseSettings):
    """Remote inference and routing when RunPod is configured."""

    model_config = _group_config("OUTSOURCE_")

    provider: Literal["none", "runpod"] = "none"
    # local_first preserves the original wait-based routing. The two
    # remote_first modes always target RunPod before deciding what local work
    # may run.
    routing: Literal["local_first", "remote_first_overflow", "remote_first_fallback"] = "local_first"
    # In local_first mode, outsource a job when its estimated local wait is
    # longer than this. Remote-first modes do not use a local wait threshold.
    local_first_max_wait: DurationOrZero = 30.0
    # Maximum remote jobs at once. `auto`: the RunPod endpoint's max workers
    # times the requests one worker takes (see app/outsource/capacity.py).
    max_concurrency: AutoInt = 4
    timeout: Duration = 15 * 60.0
    # On remote failure, put the job back on the local queue instead of failing it.
    fallback_local: bool = True
    # Largest audio file the remote handler downloads.
    max_audio_mb: float = Field(default=512, gt=0)


class StorageSettings(BaseSettings):
    """S3-compatible object storage: outsourcing hands audio to the remote
    provider through it, and JOB_STORAGE=s3 keeps job files in it."""

    model_config = _group_config("STORAGE_")

    endpoint: str | None = None
    bucket: str | None = None
    region: str = "auto"


class OffsiteSettings(BaseSettings):
    """Optional encrypted copies of the billing backups in S3-compatible
    storage, away from this machine. On when the bucket is set."""

    model_config = _group_config("OFFSITE_BACKUP_")

    endpoint: str | None = None
    bucket: str | None = None
    region: str = "auto"
    # The age public key (age1...) backups are encrypted to. The private key
    # is kept elsewhere; this server never needs it.
    recipient: str | None = None

    @field_validator("endpoint")
    @classmethod
    def _https_endpoint(cls, value: str | None) -> str | None:
        if value is not None and not value.lower().startswith("https://"):
            raise ValueError("must use https:// to protect backup credentials and data")
        return value

    @property
    def enabled(self) -> bool:
        return bool(self.bucket)


class TunnelSettings(BaseSettings):
    """Optional HTTPS access through a Cloudflare Tunnel: the cloudflared
    service in docker-compose.yml."""

    model_config = _group_config("CLOUDFLARE_TUNNEL_")

    # Read by Docker Compose; it starts cloudflared when this lists `cloudflare`.
    compose_profiles: str = Field(default="", validation_alias="COMPOSE_PROFILES")
    # The public hostname, such as piano.example.com. Only check --online
    # and the requirements use it.
    hostname: str | None = None

    @field_validator("hostname")
    @classmethod
    def _bare_hostname(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower().rstrip(".")
        if not HOSTNAME_PATTERN.match(value):
            raise ValueError("must be a hostname such as piano.example.com, without https:// or a path")
        return value

    @property
    def enabled(self) -> bool:
        profiles = {profile.strip() for profile in self.compose_profiles.split(",")}
        return bool(profiles & {TUNNEL_PROFILE, "*"})


class AdminWebSettings(BaseSettings):
    """The optional admin web page: the admin service in docker-compose.yml,
    reached through the Cloudflare Tunnel on its own hostname, behind
    Cloudflare Access. See docs/administration.md."""

    model_config = _group_config("ADMIN_WEB_")

    # disabled, read-only, or read-write. Read once when the service starts;
    # the page can only lower it, and only the command line raises it again.
    mode: Literal[ADMIN_WEB_MODES] = "disabled"
    # The page's own hostname, such as admin.example.com.
    hostname: str | None = None
    # Who may use it: the email addresses Cloudflare Access signs in.
    emails: list[str] = Field(default_factory=list)
    # The Cloudflare Access team domain, such as example.cloudflareaccess.com,
    # and the Access application's AUD tag: whose signed tokens to accept.
    access_team_domain: str | None = Field(default=None, validation_alias="CLOUDFLARE_ACCESS_TEAM_DOMAIN")
    access_aud: str | None = Field(default=None, validation_alias="CLOUDFLARE_ACCESS_AUD")

    @field_validator("hostname", "access_team_domain")
    @classmethod
    def _bare_hostname(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower().rstrip(".")
        if not HOSTNAME_PATTERN.match(value):
            raise ValueError("must be a hostname such as admin.example.com, without https:// or a path")
        return value

    @field_validator("emails")
    @classmethod
    def _lowercase(cls, value: list[str]) -> list[str]:
        return [email.strip().lower() for email in value if email.strip()]

    @property
    def enabled(self) -> bool:
        return self.mode != "disabled"


class ErrorReportSettings(BaseSettings):
    """Optional: errors of the API and the workers are sent to a website,
    such as the Piano Weave website's Error Monitor (app/error_report.py)."""

    model_config = _group_config("ERROR_REPORT_")

    # The endpoint that receives the reports, such as https://pianoweave.com/api/server-errors.
    url: str | None = None

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        parsed = urlsplit(value)
        if parsed.scheme not in ("https", "http") or not parsed.hostname:
            raise ValueError("must be a URL such as https://pianoweave.com/api/server-errors")
        # Plain HTTP would expose the token; only this machine may be reached without TLS.
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("must use https://, because the reports carry the error_report_token")
        return value


class RunPodSettings(BaseSettings):
    model_config = _group_config("RUNPOD_")

    endpoint_id: str | None = None
    # Override for local testing with the RunPod SDK's local API server.
    api_base: str = "https://api.runpod.ai/v2"


GROUPS: dict[str, type[BaseSettings]] = {
    "service": ServiceSettings,
    "limits": LimitsSettings,
    "worker": WorkerSettings,
    "jobs": JobSettings,
    "cleanup": CleanupSettings,
    "auth": AuthSettings,
    "database": DatabaseSettings,
    "billing": BillingSettings,
    "google_play": GooglePlaySettings,
    "outsource": OutsourceSettings,
    "storage": StorageSettings,
    "runpod": RunPodSettings,
    "offsite": OffsiteSettings,
    "tunnel": TunnelSettings,
    "admin_web": AdminWebSettings,
    "error_report": ErrorReportSettings,
    "secrets": SecretSettings,
}


@dataclass
class Settings:
    service: ServiceSettings
    limits: LimitsSettings
    worker: WorkerSettings
    jobs: JobSettings
    cleanup: CleanupSettings
    auth: AuthSettings
    database: DatabaseSettings
    billing: BillingSettings
    google_play: GooglePlaySettings
    outsource: OutsourceSettings
    storage: StorageSettings
    runpod: RunPodSettings
    offsite: OffsiteSettings
    tunnel: TunnelSettings
    admin_web: AdminWebSettings
    error_report: ErrorReportSettings
    secrets: SecretSettings
    secrets_dir: str = SECRETS_DIR


def is_secret(group: type[BaseSettings], field: str) -> bool:
    return group is SecretSettings


def env_name(group: type[BaseSettings], field: str) -> str:
    """The environment variable name of a setting, or the file name of a secret."""
    info = group.model_fields[field]
    alias = info.validation_alias if isinstance(info.validation_alias, str) else None
    if is_secret(group, field):
        return alias or field
    if alias:
        return alias.upper()
    return f"{group.model_config.get('env_prefix', '')}{field}".upper()


def known_variables() -> dict[str, tuple[str, str]]:
    """Environment variable name -> (group, field), secrets excluded."""
    return {
        env_name(group, field): (group_name, field)
        for group_name, group in GROUPS.items()
        for field in group.model_fields
        if not is_secret(group, field)
    }


# --- Deprecated names ----------------------------------------------------

def _seconds_or_duration(value: str) -> str:
    """Old *_SECONDS settings: a plain number is seconds."""
    text = value.strip()
    try:
        float(text)
    except ValueError:
        return text
    return f"{text}s"


@dataclass(frozen=True)
class Alias:
    new_name: str
    convert: Callable[[str], str]


# Old variable name -> replacement. Each old name logs one warning at startup.
ALIASES: dict[str, Alias] = {
    "CLEANUP_INTERVAL_SECONDS": Alias("CLEANUP_INTERVAL", _seconds_or_duration),
    "WORKER_BATCH_TIMEOUT_SECONDS": Alias("WORKER_BATCH_TIMEOUT", _seconds_or_duration),
    "OUTSOURCE_MAX_WAIT": Alias("OUTSOURCE_LOCAL_FIRST_MAX_WAIT", str),
    # The bucket also keeps job files now, not only outsourced audio.
    "OUTSOURCE_STORAGE_ENDPOINT": Alias("STORAGE_ENDPOINT", str),
    "OUTSOURCE_STORAGE_BUCKET": Alias("STORAGE_BUCKET", str),
    "OUTSOURCE_STORAGE_REGION": Alias("STORAGE_REGION", str),
    # The database is every server's, with or without billing.
    "BILLING_DATABASE_URL": Alias("DATABASE_URL", str),
    "BILLING_DATABASE_PATH": Alias("DATABASE_PATH", str),
    "BILLING_BACKUP_DIR": Alias("DATABASE_BACKUP_DIR", str),
    "BILLING_BACKUPS": Alias("DATABASE_BACKUPS", str),
    "BILLING_BACKUP_INTERVAL": Alias("DATABASE_BACKUP_INTERVAL", str),
    "BILLING_BACKUP_KEEP_ALL": Alias("DATABASE_BACKUP_KEEP_ALL", str),
    "BILLING_BACKUP_KEEP_HOURLY": Alias("DATABASE_BACKUP_KEEP_HOURLY", str),
    "BILLING_BACKUP_KEEP_DAILY": Alias("DATABASE_BACKUP_KEEP_DAILY", str),
    "BILLING_BACKUP_KEEP_WEEKLY": Alias("DATABASE_BACKUP_KEEP_WEEKLY", str),
    "BILLING_BACKUP_KEEP_MONTHLY": Alias("DATABASE_BACKUP_KEEP_MONTHLY", str),
    "BILLING_BACKUP_RETENTION": Alias("DATABASE_BACKUP_RETENTION", str),
    "BILLING_JOB_DIAGNOSTICS_RETENTION": Alias("JOB_DIAGNOSTICS_RETENTION", str),
}

# Old secret file name -> replacement, read the same way as ALIASES.
SECRET_ALIASES: dict[str, str] = {
    "outsource_storage_access_key": "storage_access_key",
    "outsource_storage_secret_key": "storage_secret_key",
    "billing_database_url": "database_url",
}

# Still supported, but replaced by another mechanism.
DEPRECATED: dict[str, str] = {
    "GOOGLE_PLAY_SERVICE_ACCOUNT_FILE": (
        "put the service-account JSON at secrets/google_play_service_account.json instead"
    ),
}


def env_file_paths(env_file: "str | Sequence[str] | None") -> list[str]:
    if env_file is None:
        return []
    return [env_file] if isinstance(env_file, str) else list(env_file)


def read_environment(env_file: "str | Sequence[str] | None") -> dict[str, str]:
    """Variables from the env files, later ones winning, overridden by the
    process environment."""
    values: dict[str, str] = {}
    for path in env_file_paths(env_file):
        if Path(path).is_file():
            values.update({key.upper(): value for key, value in dotenv_values(path).items() if value})
    values.update({key.upper(): value for key, value in os.environ.items() if value})
    return values


def misplaced_settings(env_file: "str | Sequence[str] | None") -> list[str]:
    """Lines in the wrong one of the two files, which scripts/server.py and
    scripts/setup.py move. Only the host has the files; in a container there
    is nothing to check."""
    warnings = []
    for path in env_file_paths(env_file):
        name = Path(path).name
        if not Path(path).is_file() or name not in ENV_FILES:
            continue
        keys = {key.upper() for key, value in dotenv_values(path).items() if value}
        wrong = sorted(keys - COMPOSE_ONLY_VARIABLES if name == COMPOSE_ENV_FILE else keys & COMPOSE_ONLY_VARIABLES)
        if wrong:
            other = SERVER_ENV_FILE if name == COMPOSE_ENV_FILE else COMPOSE_ENV_FILE
            warnings.append(
                f"{', '.join(wrong)} belong{'s' if len(wrong) == 1 else ''} in {other}, not {name}; "
                "python scripts/server.py start or python scripts/setup.py moves them"
            )
    return warnings


def resolve_aliases(environment: dict[str, str]) -> dict[str, dict[str, str]]:
    """Translate deprecated names into init values for the settings groups."""
    known = known_variables()
    overrides: dict[str, dict[str, str]] = {}
    problems = []
    for old_name, alias in ALIASES.items():
        if old_name not in environment:
            continue
        if alias.new_name in environment:
            problems.append(f"{old_name} and {alias.new_name} are both set; remove {old_name}")
            continue
        logger.warning("%s is deprecated; use %s instead", old_name, alias.new_name)
        group_name, field = known[alias.new_name]
        overrides.setdefault(group_name, {})[field] = alias.convert(environment[old_name])
    for name, advice in DEPRECATED.items():
        if name in environment:
            logger.warning("%s is deprecated; %s", name, advice)
    if problems:
        raise ConfigError(problems)
    return overrides


def resolve_secret_aliases(secrets_dir: str | None) -> dict[str, str]:
    """Read secrets from deprecated file names, as init values for SecretSettings."""
    values: dict[str, str] = {}
    problems = []
    if not secrets_dir:
        return values
    for old_name, new_name in SECRET_ALIASES.items():
        old_path = Path(secrets_dir) / old_name
        if not old_path.is_file():
            continue
        if (Path(secrets_dir) / new_name).is_file():
            problems.append(f"The secret files {old_name} and {new_name} both exist; remove {old_name}")
            continue
        logger.warning("The secret file %s is deprecated; rename it to %s", old_name, new_name)
        # The same as pydantic-settings reads a secret file.
        values[new_name] = old_path.read_text(encoding="utf-8").strip()
    if problems:
        raise ConfigError(problems)
    return values


def unknown_variable_warnings(environment: dict[str, str]) -> list[str]:
    # Docker Compose's own, such as WORKER_GPU_BUILD, are known too.
    known = set(known_variables()) | set(ALIASES) | COMPOSE_ONLY_VARIABLES
    messages = []
    for name in sorted(environment):
        if not name.startswith(CHECKED_PREFIXES) or name in known:
            continue
        suggestion = difflib.get_close_matches(name, sorted(known), n=1)
        hint = f"; did you mean {suggestion[0]}?" if suggestion else ""
        messages.append(f"Unknown setting {name}{hint}")
    return messages


def _format_errors(group: type[BaseSettings], error: ValidationError, overrides: dict[str, str]) -> list[str]:
    old_names = {alias.new_name: old for old, alias in ALIASES.items()}
    problems = []
    for item in error.errors():
        field = str(item["loc"][0]) if item["loc"] else ""
        if field not in group.model_fields:
            field = next((name for name in group.model_fields if env_name(group, name) == field), field)
        name = env_name(group, field) if field in group.model_fields else field
        if field in overrides and name in old_names:
            name = f"{old_names[name]} (deprecated name of {name})"
        message = item["msg"].removeprefix("Value error, ")
        problems.append(f"{name}: {message}")
    return problems


def load_settings(env_file: "str | Sequence[str] | None" = ENV_FILES, secrets_dir: str | None = SECRETS_DIR) -> Settings:
    environment = read_environment(env_file)
    overrides = resolve_aliases(environment)
    for message in unknown_variable_warnings(environment):
        logger.warning(message)

    secrets = secrets_dir if secrets_dir and Path(secrets_dir).is_dir() else None
    secret_values = resolve_secret_aliases(secrets)
    if secret_values:
        overrides.setdefault("secrets", {}).update(secret_values)
    groups = {}
    problems = []
    for group_name, group in GROUPS.items():
        values = overrides.get(group_name, {})
        try:
            groups[group_name] = group(_env_file=tuple(env_file_paths(env_file)) or None, _secrets_dir=secrets, **values)
        except ValidationError as error:
            problems.extend(_format_errors(group, error, values))
    if problems:
        raise ConfigError(problems)
    return Settings(**groups, secrets_dir=secrets_dir or SECRETS_DIR)


# --- Startup validation --------------------------------------------------

def google_play_service_account_json(config: "Settings | None" = None) -> str | None:
    """The Google Play service-account JSON, from the secret file or the
    deprecated GOOGLE_PLAY_SERVICE_ACCOUNT_FILE override."""
    config = config or settings
    google_play = config.google_play
    if google_play.service_account_file:
        path = Path(google_play.service_account_file)
        if not path.is_absolute():
            path = Path(config.secrets_dir) / path.name
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None
    if config.secrets.google_play_service_account is not None:
        return config.secrets.google_play_service_account.get_secret_value()
    return None


# Hosts of the Compose stack's own Redis and PostgreSQL, or of this machine.
LOCAL_HOSTS = {"redis", "postgres", "localhost", "127.0.0.1", "::1"}
# libpq's sslmode values that encrypt the connection.
ENCRYPTED_SSLMODES = {"require", "verify-ca", "verify-full"}


def tunnel_enabled(config: "Settings | None" = None) -> bool:
    """Whether cloudflared runs: with Compose when its profile is listed;
    with Kubernetes always, as its own Deployment."""
    config = config or settings
    return config.service.deployment == "kubernetes" or config.tunnel.enabled


def uses_compose_profiles(config: "Settings | None" = None) -> bool:
    config = config or settings
    return config.service.deployment != "kubernetes"


def runs_elsewhere(url: str | None) -> bool:
    """Whether a Redis or PostgreSQL URL leads to a server outside the stack,
    such as a managed service."""
    host = urlsplit(url or "").hostname
    return bool(host) and host not in LOCAL_HOSTS


def effective_database_url(config: "Settings | None" = None) -> str:
    """The PostgreSQL URL the billing database uses, as app.billing chooses it."""
    config = config or settings
    secret = config.secrets.database_url
    return secret.get_secret_value() if secret else config.database.url


def database_sslmode(url: str) -> str | None:
    from urllib.parse import parse_qs

    values = parse_qs(urlsplit(url).query).get("sslmode")
    return values[-1] if values else None


def validate_settings(config: "Settings | None" = None, role: str = "all") -> list[str]:
    """Cross-setting checks for a process role: api, worker, remote, cleanup, or all."""
    config = config or settings
    problems = []
    redis_password = config.service.redis_password_file
    if redis_password and (not Path(redis_password).is_file() or Path(redis_password).stat().st_size == 0):
        problems.append(
            f"The Redis password file {redis_password} is missing or empty; python scripts/server.py start "
            "creates the files in secrets/redis"
        )
    ca_file = config.service.redis_ca_file
    if ca_file and not config.service.redis_url.startswith("rediss://"):
        problems.append("REDIS_CA_FILE is set, but REDIS_URL is not a rediss:// URL: the CA file is only used for TLS")
    elif ca_file and not Path(ca_file).is_file():
        problems.append(f"The Redis CA file {ca_file} (REDIS_CA_FILE) does not exist")
    # The optional features: what each needs, excludes, and requires.
    from app.features import all_problems

    problems.extend(all_problems(config, role))
    return problems


def admin_web_problems(config: "Settings", check_profile: bool = True) -> list[str]:
    """What stops the admin web page from running safely. Anything missing
    keeps it from starting; it never runs half protected."""
    admin = config.admin_web
    profiles = {profile.strip() for profile in config.tunnel.compose_profiles.split(",")}
    # Kubernetes deploys it as a component instead.
    check_profile = check_profile and uses_compose_profiles(config)
    if not admin.enabled:
        if check_profile and ADMIN_WEB_PROFILE in profiles:
            return [f"COMPOSE_PROFILES lists {ADMIN_WEB_PROFILE}, but ADMIN_WEB_MODE=disabled; remove it from COMPOSE_PROFILES"]
        return []
    problems = []
    required = {
        "ADMIN_WEB_HOSTNAME": admin.hostname,
        "ADMIN_WEB_EMAILS": admin.emails,
        "CLOUDFLARE_ACCESS_TEAM_DOMAIN": admin.access_team_domain,
        "CLOUDFLARE_ACCESS_AUD": admin.access_aud,
    }
    for name, value in required.items():
        if not value:
            problems.append(f"{name} is required when ADMIN_WEB_MODE={admin.mode}")
    if admin.hostname and admin.hostname == config.tunnel.hostname:
        problems.append("ADMIN_WEB_HOSTNAME must differ from CLOUDFLARE_TUNNEL_HOSTNAME, the API's hostname")
    if admin.access_team_domain and not admin.access_team_domain.endswith(".cloudflareaccess.com"):
        problems.append("CLOUDFLARE_ACCESS_TEAM_DOMAIN must be your team domain, such as example.cloudflareaccess.com")
    if not config.database.url:
        problems.append("The admin web page needs the PostgreSQL billing database (DATABASE_URL)")
    if check_profile and ADMIN_WEB_PROFILE not in profiles and "*" not in profiles:
        problems.append(f"ADMIN_WEB_MODE={admin.mode} needs {ADMIN_WEB_PROFILE} in COMPOSE_PROFILES, which starts the service")
    return problems


def offsite_problems(config: "Settings") -> list[str]:
    """Beyond what the off-site feature lists in app/features.py: the key the
    backups are encrypted to."""
    offsite = config.offsite
    problems = []
    if not offsite.recipient:
        problems.append(
            "OFFSITE_BACKUP_RECIPIENT is required when OFFSITE_BACKUP_BUCKET is set: the age public key "
            "backups are encrypted to. Create a key pair with: "
            "docker compose run --rm --no-deps api python -m app.backups keygen"
        )
    else:
        try:
            from pyrage import x25519

            x25519.Recipient.from_str(offsite.recipient.strip())
        except ImportError:
            # Images without backups (the worker) do not install it.
            pass
        except Exception:
            problems.append("OFFSITE_BACKUP_RECIPIENT is not an age public key (age1...)")
    return problems


def configuration_warnings(config: "Settings", environment: dict[str, str]) -> list[str]:
    """Choices that work but are probably mistakes."""
    warnings = []
    if config.database.backups == "provider":
        warnings.append(
            "The server does not back up the billing database (DATABASE_BACKUPS=provider): its provider must. "
            "python -m app.config requirements says how."
        )
    redis_url = config.service.redis_url
    if runs_elsewhere(redis_url) and not redis_url.startswith("rediss://"):
        warnings.append(
            f"REDIS_URL leads to {urlsplit(redis_url).hostname} without TLS: job state and the password travel "
            "unencrypted. Use a rediss:// URL."
        )
    database_url = effective_database_url(config)
    if runs_elsewhere(database_url) and database_sslmode(database_url) not in ENCRYPTED_SSLMODES:
        warnings.append(
            f"The billing database at {urlsplit(database_url).hostname} is reached without required TLS. Add "
            "sslmode=verify-full and sslrootcert=<the provider's CA file> to the URL in secrets/database_url."
        )
    if tunnel_enabled(config):
        if config.secrets.server_password is None and config.billing.provider == "none":
            warnings.append(
                "The Cloudflare Tunnel makes the server reachable from the internet, and it has neither a "
                "password nor sign-in: anyone who finds the address can use it. See \"Password protection\" "
                "in docs/setup.md."
            )
        # Unset, docker-compose.yml publishes on 127.0.0.1.
        if uses_compose_profiles(config) and environment.get("API_BIND_ADDRESS", "127.0.0.1") != "127.0.0.1":
            warnings.append(
                "Port 8000 is published on every network interface, so the server is also reachable without the "
                "tunnel's HTTPS. Set API_BIND_ADDRESS=127.0.0.1 unless devices on your network connect directly."
            )
    offsite = config.offsite
    if offsite.enabled and (offsite.endpoint, offsite.bucket) == (config.storage.endpoint, config.storage.bucket):
        warnings.append(
            "Off-site backups use the object storage bucket. Use a bucket and keys of their own, so the keys "
            "every job uses cannot delete the backups."
        )
    from app.features import unused_settings_warnings

    warnings.extend(unused_settings_warnings(config, environment))
    return warnings


CONTAINER_UID = 10001
# The Compose prepare service does this before every start; see app.prepare.
OWNERSHIP_FIX = "docker compose run --rm prepare"


def unwritable_directories(config: "Settings | None" = None, role: str = "all") -> list[str]:
    """Problems with directories the role writes to. The services run as a
    non-root user; volumes created by older versions belong to root."""
    config = config or settings
    # The admin web page only reads, and gets the data folder read-only.
    directories = [] if role == "admin" else [config.service.data_dir]
    if role in ("api", "worker", "remote", "all"):
        directories.append(str(Path(config.database.path).parent))
    if role in ("api", "all"):
        directories.append(config.database.backup_dir)
    problems = []
    for directory in dict.fromkeys(directories):
        if Path(directory).is_dir() and not os.access(directory, os.W_OK):
            uid = getattr(os, "getuid", lambda: "?")()
            problems.append(
                f"{directory} is not writable by this service (user ID {uid}). "
                f"Start the stack with `docker compose up -d`, or run: {OWNERSHIP_FIX}"
            )
    return problems


def ensure_valid(role: str) -> None:
    """Fail fast at process startup."""
    problems = validate_settings(settings, role) + unwritable_directories(settings, role)
    if problems:
        raise ConfigError(problems)


# --- `python -m app.config check` -----------------------------------------

def duration_text(seconds: float) -> str:
    """A duration as a setting is written: 15m, 1d, or 90s."""
    for unit, size in sorted(DURATION_UNITS.items(), key=lambda item: -item[1]):
        if seconds >= size and (seconds / size).is_integer():
            return f"{int(seconds // size)}{unit}"
    return f"{seconds:.10g}s"


def format_duration(seconds: float) -> str:
    """A duration for people, with its seconds when it has a unit: 15m (900 s)."""
    if any(seconds >= size and (seconds / size).is_integer() for size in DURATION_UNITS.values()):
        return f"{duration_text(seconds)} ({seconds:.10g} s)"
    return duration_text(seconds)


def is_duration(group: type[BaseSettings], field: str) -> bool:
    return any(
        getattr(item, "func", None) in (_duration, _duration_or_zero, _optional_duration)
        for item in group.model_fields[field].metadata
    )


def describe(config: Settings) -> list[str]:
    lines = []
    for group_name in GROUPS:
        group = getattr(config, group_name)
        lines.append(f"[{group_name}]")
        for field in type(group).model_fields:
            value = getattr(group, field)
            if isinstance(value, SecretStr):
                shown = "********"
            elif value is None and type(group) is WorkerSettings and field == "batch_size":
                shown = "auto (each GPU's job capacity, CPU/RAM dependent; resolved at worker start)"
            elif value is None:
                shown = "auto" if field in ("threads", "max_concurrency") else "(unset)"
            elif is_duration(type(group), field):
                shown = format_duration(value)
            else:
                shown = value
            name = env_name(type(group), field)
            if is_secret(type(group), field):
                name = f"secret {config.secrets_dir}/{name}"
            lines.append(f"  {name} = {shown}")
    return lines


def main(arguments: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m app.config")
    subcommands = parser.add_subparsers(dest="command", required=True)
    check = subcommands.add_parser("check", help="validate the configuration and print it")
    check.add_argument("--env-file", action="append", help="an env file to read; repeatable (default: .env and server.env)")
    check.add_argument("--secrets-dir", default=SECRETS_DIR)
    check.add_argument(
        "--online",
        action="store_true",
        help="also test the configured services (storage, RunPod, Google Play) with the real credentials",
    )
    requirements = subcommands.add_parser(
        "requirements",
        help="print what the external services must provide, derived from the configuration",
    )
    requirements.add_argument("--env-file", action="append", help="an env file to read; repeatable (default: .env and server.env)")
    requirements.add_argument("--secrets-dir", default=SECRETS_DIR)
    requirements.add_argument("--json", action="store_true", help="print JSON, for scripts and AI agents")
    features = subcommands.add_parser(
        "features",
        help="list the optional features: how each is turned on, what it needs, and what is missing",
    )
    features.add_argument("--env-file", action="append", help="an env file to read; repeatable (default: .env and server.env)")
    features.add_argument("--secrets-dir", default=SECRETS_DIR)
    features.add_argument("--json", action="store_true", help="print JSON, for scripts/setup.py and AI agents")
    args = parser.parse_args(arguments)

    logging.basicConfig(level=logging.WARNING, format="warning: %(message)s")
    if args.command == "requirements":
        return print_requirements(args)
    if args.command == "features":
        return print_features(args)
    try:
        config = load_settings(args.env_file or ENV_FILES, args.secrets_dir)
        problems = validate_settings(config, "all")
    except ConfigError as error:
        problems = error.problems
        config = None
    if config is not None:
        from app.features import summary_lines

        print("\n".join(summary_lines(config)) + "\n")
        print("\n".join(describe(config)))
        warnings = configuration_warnings(config, read_environment(args.env_file or ENV_FILES))
        warnings += misplaced_settings(args.env_file or ENV_FILES)
        if warnings:
            print("\nWarnings:")
            for warning in warnings:
                print(f"  - {warning}")
    if problems:
        print("\nConfiguration errors:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("\nConfiguration OK")
    if args.online:
        from app.online_check import format_results, run_checks

        print("\nOnline checks:")
        results = run_checks(config)
        print("\n".join(format_results(results)))
        if any(result.status == "fail" for result in results):
            return 1
    return 0


def print_features(args) -> int:
    from app.features import describe_features, summary_lines

    try:
        config = load_settings(args.env_file or ENV_FILES, args.secrets_dir)
    except ConfigError as error:
        print("\n".join(f"  - {problem}" for problem in error.problems), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(describe_features(config), indent=2, default=str))
    else:
        print("\n".join(summary_lines(config)))
    return 0


def print_requirements(args) -> int:
    from app.requirements import build_requirements, format_requirements, requirements_json

    try:
        config = load_settings(args.env_file or ENV_FILES, args.secrets_dir)
    except ConfigError as error:
        print("Configuration errors:", file=sys.stderr)
        for problem in error.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    sections = build_requirements(config)
    if args.json:
        print(json.dumps(requirements_json(sections), indent=2))
    else:
        print("\n".join(format_requirements(sections)))
    return 0


if __name__ == "__main__":
    # Through app.config, which app.features imports: run as __main__, this
    # file would otherwise be a second module that loads the settings again.
    from app.config import main as package_main

    sys.exit(package_main(sys.argv[1:]))

settings = load_settings()
