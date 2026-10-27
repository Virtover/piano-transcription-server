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
from typing import Annotated, Any, Callable, Literal

from dotenv import dotenv_values
from pydantic import BeforeValidator, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


logger = logging.getLogger(__name__)

SECRETS_DIR = "/run/secrets"
# Unknown variables with these prefixes are reported as possible typos.
CHECKED_PREFIXES = ("WORKER_", "BILLING_", "OUTSOURCE_", "RUNPOD_", "GOOGLE_PLAY_", "CLOUDFLARE_", "OFFSITE_")
MIN_SERVER_PASSWORD_LENGTH = 12
# Read from .env by Docker Compose itself (docker-compose.yml), not by the server.
COMPOSE_VARIABLES = {"API_BIND_ADDRESS", "POSTGRES_DATA_LOCATION", "BILLING_DATA_GROUP"}
# The Compose profile that starts the cloudflared service.
TUNNEL_PROFILE = "cloudflare"
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
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
        populate_by_name=True,
        validate_assignment=True,
    )


class ServiceSettings(BaseSettings):
    """Container wiring. Docker Compose sets these; the defaults suit local development."""

    model_config = _group_config()

    redis_url: str = "redis://localhost:6379/0"
    data_dir: str = "/data"


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


class CleanupSettings(BaseSettings):
    model_config = _group_config("CLEANUP_")

    interval: Duration = 600.0


class AuthSettings(BaseSettings):
    model_config = _group_config("GOOGLE_OAUTH_")

    client_ids: list[str] = Field(default_factory=list)


class BillingSettings(BaseSettings):
    model_config = _group_config("BILLING_")

    provider: Literal["none", "google_play"] = "none"
    products: dict[str, int] = Field(default_factory=dict)
    # PostgreSQL is the production database. database_path remains for the
    # explicit SQLite compatibility used by local tests and migration tools.
    database_url: str = "postgresql://billing@postgres:5432/billing"
    database_path: str = "/billing/billing.sqlite3"
    backup_dir: str = "/billing/backups"
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
    job_diagnostics_retention: Duration = 30 * 24 * 60 * 60.0
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
    outsource_storage_access_key: SecretStr | None = None
    outsource_storage_secret_key: SecretStr | None = None
    offsite_backup_access_key: SecretStr | None = None
    offsite_backup_secret_key: SecretStr | None = None
    # Optional. When present, every request except /health and /ready needs it.
    server_password: SecretStr | None = None
    billing_database_url: SecretStr | None = None

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
    # S3-compatible storage used to hand audio to the remote provider.
    storage_endpoint: str | None = None
    storage_bucket: str | None = None
    storage_region: str = "auto"
    # Largest audio file the remote handler downloads.
    max_audio_mb: float = Field(default=512, gt=0)


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


class RunPodSettings(BaseSettings):
    model_config = _group_config("RUNPOD_")

    endpoint_id: str | None = None
    # Override for local testing with the RunPod SDK's local API server.
    api_base: str = "https://api.runpod.ai/v2"


GROUPS: dict[str, type[BaseSettings]] = {
    "service": ServiceSettings,
    "limits": LimitsSettings,
    "worker": WorkerSettings,
    "cleanup": CleanupSettings,
    "auth": AuthSettings,
    "billing": BillingSettings,
    "google_play": GooglePlaySettings,
    "outsource": OutsourceSettings,
    "runpod": RunPodSettings,
    "offsite": OffsiteSettings,
    "tunnel": TunnelSettings,
    "secrets": SecretSettings,
}


@dataclass
class Settings:
    service: ServiceSettings
    limits: LimitsSettings
    worker: WorkerSettings
    cleanup: CleanupSettings
    auth: AuthSettings
    billing: BillingSettings
    google_play: GooglePlaySettings
    outsource: OutsourceSettings
    runpod: RunPodSettings
    offsite: OffsiteSettings
    tunnel: TunnelSettings
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
}

# Still supported, but replaced by another mechanism.
DEPRECATED: dict[str, str] = {
    "GOOGLE_PLAY_SERVICE_ACCOUNT_FILE": (
        "put the service-account JSON at secrets/google_play_service_account.json instead"
    ),
}


def read_environment(env_file: str | None) -> dict[str, str]:
    """Variables from the env file, overridden by the process environment."""
    values: dict[str, str] = {}
    if env_file and Path(env_file).is_file():
        values.update({key.upper(): value for key, value in dotenv_values(env_file).items() if value})
    values.update({key.upper(): value for key, value in os.environ.items() if value})
    return values


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


def unknown_variable_warnings(environment: dict[str, str]) -> list[str]:
    known = set(known_variables()) | set(ALIASES)
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


def load_settings(env_file: str | None = ".env", secrets_dir: str | None = SECRETS_DIR) -> Settings:
    environment = read_environment(env_file)
    overrides = resolve_aliases(environment)
    for message in unknown_variable_warnings(environment):
        logger.warning(message)

    secrets = secrets_dir if secrets_dir and Path(secrets_dir).is_dir() else None
    groups = {}
    problems = []
    for group_name, group in GROUPS.items():
        values = overrides.get(group_name, {})
        try:
            groups[group_name] = group(_env_file=env_file, _secrets_dir=secrets, **values)
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


def validate_settings(config: "Settings | None" = None, role: str = "all") -> list[str]:
    """Cross-setting checks for a process role: api, worker, remote, cleanup, or all."""
    config = config or settings
    problems = []
    password = config.secrets.server_password
    if role in ("api", "all") and password is not None:
        # Nothing slows down guessing, so the length is the only protection.
        if len(password.get_secret_value()) < MIN_SERVER_PASSWORD_LENGTH:
            problems.append(
                f"The secret file {config.secrets_dir}/server_password must hold at least "
                f"{MIN_SERVER_PASSWORD_LENGTH} characters"
            )
    if role in ("api", "remote", "all") and config.outsource.provider == "runpod":
        required = {
            "RUNPOD_ENDPOINT_ID": config.runpod.endpoint_id,
            "OUTSOURCE_STORAGE_ENDPOINT": config.outsource.storage_endpoint,
            "OUTSOURCE_STORAGE_BUCKET": config.outsource.storage_bucket,
        }
        for name, value in required.items():
            if not value:
                problems.append(f"{name} is required when OUTSOURCE_PROVIDER=runpod")
        for name in ("runpod_api_key", "outsource_storage_access_key", "outsource_storage_secret_key"):
            if getattr(config.secrets, name) is None:
                problems.append(
                    f"OUTSOURCE_PROVIDER=runpod needs the secret file {config.secrets_dir}/{name}"
                )
    if role in ("api", "all") and config.billing.provider == "google_play":
        if not config.google_play.package_name:
            problems.append("GOOGLE_PLAY_PACKAGE_NAME is required when BILLING_PROVIDER=google_play")
        if not [client_id for client_id in config.auth.client_ids if client_id.strip()]:
            problems.append("GOOGLE_OAUTH_CLIENT_IDS is required when BILLING_PROVIDER=google_play")
        account = google_play_service_account_json(config)
        if account is None:
            source = (
                f"GOOGLE_PLAY_SERVICE_ACCOUNT_FILE ({config.google_play.service_account_file}) cannot be read"
                if config.google_play.service_account_file
                else f"the secret file {config.secrets_dir}/google_play_service_account.json is missing"
            )
            problems.append(f"BILLING_PROVIDER=google_play needs a Google Play service account: {source}")
        else:
            try:
                json.loads(account)
            except ValueError:
                problems.append("The Google Play service-account secret is not valid JSON")
    if role in ("api", "all") and config.offsite.enabled:
        problems.extend(offsite_problems(config))
    if role == "all" and config.tunnel.enabled:
        # Only cloudflared reads the token, so only `check` looks for it.
        token = Path(config.secrets_dir) / TUNNEL_TOKEN_FILE
        if not token.is_file() or token.stat().st_size == 0:
            problems.append(
                f"COMPOSE_PROFILES={TUNNEL_PROFILE} needs the tunnel token in the secret file "
                f"{config.secrets_dir}/{TUNNEL_TOKEN_FILE}"
            )
    return problems


def offsite_problems(config: "Settings") -> list[str]:
    offsite = config.offsite
    problems = []
    if not offsite.endpoint:
        problems.append("OFFSITE_BACKUP_ENDPOINT is required when OFFSITE_BACKUP_BUCKET is set")
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
    for name in ("offsite_backup_access_key", "offsite_backup_secret_key"):
        if getattr(config.secrets, name) is None:
            problems.append(f"OFFSITE_BACKUP_BUCKET needs the secret file {config.secrets_dir}/{name}")
    return problems


def configuration_warnings(config: "Settings", environment: dict[str, str]) -> list[str]:
    """Choices that work but are probably mistakes."""
    warnings = []
    if config.tunnel.enabled:
        if config.secrets.server_password is None and config.billing.provider == "none":
            warnings.append(
                "The Cloudflare Tunnel makes the server reachable from the internet, and it has neither a "
                "password nor sign-in: anyone who finds the address can use it. See \"Password protection\" "
                "in docs/setup.md."
            )
        if environment.get("API_BIND_ADDRESS", "0.0.0.0") != "127.0.0.1":
            warnings.append(
                "Port 8000 is published on every network interface, so the server is also reachable without the "
                "tunnel's HTTPS. Set API_BIND_ADDRESS=127.0.0.1 unless devices on your network connect directly."
            )
    offsite, outsource = config.offsite, config.outsource
    if offsite.enabled and (offsite.endpoint, offsite.bucket) == (outsource.storage_endpoint, outsource.storage_bucket):
        warnings.append(
            "Off-site backups use the outsourcing bucket. Use a bucket and keys of their own, so the keys used "
            "for every outsourced job cannot delete the backups."
        )
    return warnings


CONTAINER_UID = 10001
# The Compose prepare service does this before every start; see app.prepare.
OWNERSHIP_FIX = "docker compose run --rm prepare"


def unwritable_directories(config: "Settings | None" = None, role: str = "all") -> list[str]:
    """Problems with directories the role writes to. The services run as a
    non-root user; volumes created by older versions belong to root."""
    config = config or settings
    directories = [config.service.data_dir]
    if role in ("api", "worker", "remote", "all"):
        directories.append(str(Path(config.billing.database_path).parent))
    if role in ("api", "all"):
        directories.append(config.billing.backup_dir)
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

def format_duration(seconds: float) -> str:
    for unit, size in sorted(DURATION_UNITS.items(), key=lambda item: -item[1]):
        if seconds >= size and (seconds / size).is_integer():
            return f"{int(seconds // size)}{unit} ({seconds:.10g} s)"
    return f"{seconds:.10g}s"


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
    check.add_argument("--env-file", default=".env", help="env file to read (default: .env)")
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
    requirements.add_argument("--env-file", default=".env", help="env file to read (default: .env)")
    requirements.add_argument("--secrets-dir", default=SECRETS_DIR)
    requirements.add_argument("--json", action="store_true", help="print JSON, for scripts and AI agents")
    args = parser.parse_args(arguments)

    logging.basicConfig(level=logging.WARNING, format="warning: %(message)s")
    if args.command == "requirements":
        return print_requirements(args)
    try:
        config = load_settings(args.env_file, args.secrets_dir)
        problems = validate_settings(config, "all")
    except ConfigError as error:
        problems = error.problems
        config = None
    if config is not None:
        print("\n".join(describe(config)))
        warnings = configuration_warnings(config, read_environment(args.env_file))
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


def print_requirements(args) -> int:
    from app.requirements import build_requirements, format_requirements, requirements_json

    try:
        config = load_settings(args.env_file, args.secrets_dir)
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
    sys.exit(main(sys.argv[1:]))

settings = load_settings()
