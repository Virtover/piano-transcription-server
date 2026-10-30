"""The server's optional features in one table: how each is turned on, what it
needs, what it cannot be on with, its settings, and its secrets.

`python -m app.config check` validates from it, `features --json` lists it
for scripts/setup.py and agents, and tests keep docs/configuration.md and
.env.example in step with it. A rule is written here once.

Without any feature, the server runs as the Compose stack starts it: no
sign-in, no billing, local job files, and the server's own backups.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args

from app import config as app_config
from app.config import Settings


@dataclass(frozen=True)
class Secret:
    """A file in the secrets folder."""

    name: str
    description: str
    # value: typed in; file: copied from a downloaded file; generate: random,
    # unless one is typed in.
    kind: str = "value"
    optional: bool = False
    # Checked by the feature's own problems, as for a secret with an
    # alternative, instead of by its presence.
    checked: bool = True


@dataclass(frozen=True)
class Feature:
    key: str
    title: str
    summary: str
    # How it is turned on, as a user writes it, and the condition the
    # messages name: "X is required when <when>", "<switch> needs ...".
    switch: Callable[[Settings], str] | str
    enabled: Callable[[Settings], bool]
    docs: str
    when: Callable[[Settings], str] | str | None = None
    # .env values that turn it on and off; COMPOSE_PROFILES entries it needs.
    turn_on: dict[str, str] = field(default_factory=dict)
    turn_off: dict[str, str] = field(default_factory=dict)
    profiles: tuple[str, ...] = ()
    # Its settings, as variable names, in the order a setup asks for them,
    # and those it cannot work without.
    settings: tuple[str, ...] = ()
    required: tuple[str, ...] = ()
    secrets: tuple[Secret, ...] = ()
    # Features that must be on, and those that cannot be on, with why.
    needs: tuple[str, ...] = ()
    excludes: tuple[tuple[str, str], ...] = ()
    # Process roles that check it; "all" is `check`.
    roles: tuple[str, ...] = ("api", "all")
    # Sections of `python -m app.config requirements` for its external services.
    requirement_sections: tuple[str, ...] = ()
    # Further problems while it is on, and while it is off.
    problems: Callable[[Settings, str], list[str]] | None = None
    off_problems: Callable[[Settings, str], list[str]] | None = None
    # How a message tells to turn it on; by default with its switch.
    how: str | None = None
    # Features one of which must use it, or it only costs.
    serves: tuple[str, ...] = ()
    # The secret file whose presence turns it on, for features without a setting.
    switch_secret: str | None = None

    def switch_text(self, config: Settings) -> str:
        return self.switch(config) if callable(self.switch) else self.switch

    def when_text(self, config: Settings) -> str:
        if self.when is None:
            return self.switch_text(config)
        return self.when(config) if callable(self.when) else self.when

    def how_text(self) -> str:
        return self.how or f"set {self.switch}"


def in_sentence(title: str) -> str:
    """A title inside a sentence: "Object storage" becomes "object storage",
    "HTTPS through a Cloudflare Tunnel" stays."""
    return title[0].lower() + title[1:] if title[1:2].islower() else title


# --- Feature-specific checks ----------------------------------------------------

def _password_problems(config: Settings, role: str) -> list[str]:
    # Nothing slows down guessing, so the length is the only protection.
    if len(config.secrets.server_password.get_secret_value()) < app_config.MIN_SERVER_PASSWORD_LENGTH:
        return [
            f"The secret file {config.secrets_dir}/server_password must hold at least "
            f"{app_config.MIN_SERVER_PASSWORD_LENGTH} characters"
        ]
    return []


def _billing_problems(config: Settings, role: str) -> list[str]:
    account = app_config.google_play_service_account_json(config)
    if account is None:
        source = (
            f"GOOGLE_PLAY_SERVICE_ACCOUNT_FILE ({config.google_play.service_account_file}) cannot be read"
            if config.google_play.service_account_file
            else f"the secret file {config.secrets_dir}/google_play_service_account.json is missing"
        )
        return [f"BILLING_PROVIDER=google_play needs a Google Play service account: {source}"]
    try:
        json.loads(account)
    except ValueError:
        return ["The Google Play service-account secret is not valid JSON"]
    return []


def _provider_backups_problems(config: Settings, role: str) -> list[str]:
    url = app_config.effective_database_url(config)
    if app_config.runs_elsewhere(url):
        return []
    return [
        "DATABASE_BACKUPS=provider needs a managed database, whose provider backs it up; the billing database "
        "here is the stack's own, which nothing would back up"
    ]


def _offsite_problems(config: Settings, role: str) -> list[str]:
    return app_config.offsite_problems(config)


def _admin_web_problems(config: Settings, role: str) -> list[str]:
    return app_config.admin_web_problems(config, check_profile=role == "all")


def _admin_web_off_problems(config: Settings, role: str) -> list[str]:
    if role != "all":
        return []
    return app_config.admin_web_problems(config, check_profile=True)


def _admin_switch(config: Settings) -> str:
    return f"ADMIN_WEB_MODE={config.admin_web.mode}"


def _tunnel_token_present(config: Settings) -> bool:
    token = Path(config.secrets_dir) / app_config.TUNNEL_TOKEN_FILE
    return token.is_file() and token.stat().st_size > 0


# --- The table ----------------------------------------------------------------------

STORAGE_SECRETS = (
    Secret("storage_access_key", "Access key ID of the storage token"),
    Secret("storage_secret_key", "Secret access key of the storage token"),
)

FEATURES: tuple[Feature, ...] = (
    Feature(
        key="password",
        title="Password protection",
        summary="Every request except /health and /ready needs a password, for a private server without sign-in.",
        switch="the secret file server_password",
        enabled=lambda config: config.secrets.server_password is not None,
        docs="docs/setup.md#-password-protection",
        secrets=(Secret("server_password", "The password, at least 12 characters", kind="generate"),),
        switch_secret="server_password",
        problems=_password_problems,
    ),
    Feature(
        key="tunnel",
        title="HTTPS through a Cloudflare Tunnel",
        summary="An HTTPS address for the server on the internet, without opening a port.",
        switch=f"COMPOSE_PROFILES={app_config.TUNNEL_PROFILE}",
        enabled=lambda config: config.tunnel.enabled,
        docs="docs/cloudflare-tunnel.md",
        profiles=(app_config.TUNNEL_PROFILE,),
        settings=("CLOUDFLARE_TUNNEL_HOSTNAME",),
        secrets=(Secret(app_config.TUNNEL_TOKEN_FILE, "The tunnel's token", checked=False),),
        # Only cloudflared reads the token, so only `check` looks for it.
        roles=("all",),
        requirement_sections=("Cloudflare Tunnel",),
        problems=lambda config, role: [] if _tunnel_token_present(config) else [
            f"COMPOSE_PROFILES={app_config.TUNNEL_PROFILE} needs the tunnel token in the secret file "
            f"{config.secrets_dir}/{app_config.TUNNEL_TOKEN_FILE}"
        ],
        how=f"add {app_config.TUNNEL_PROFILE} to COMPOSE_PROFILES",
    ),
    Feature(
        key="admin_web",
        title="Admin web page",
        summary="The admin commands as a web page, on a hostname of its own behind Cloudflare Access.",
        switch=_admin_switch,
        enabled=lambda config: config.admin_web.enabled,
        docs="docs/administration.md#-admin-web-page",
        turn_on={"ADMIN_WEB_MODE": "read-only"},
        turn_off={"ADMIN_WEB_MODE": "disabled"},
        profiles=(app_config.ADMIN_WEB_PROFILE,),
        settings=("ADMIN_WEB_MODE", "ADMIN_WEB_HOSTNAME", "ADMIN_WEB_EMAILS", "CLOUDFLARE_ACCESS_TEAM_DOMAIN",
                  "CLOUDFLARE_ACCESS_AUD"),
        needs=("tunnel",),
        roles=("admin", "all"),
        requirement_sections=("Admin web page",),
        problems=_admin_web_problems,
        off_problems=_admin_web_off_problems,
        how=f"set ADMIN_WEB_MODE to read-only or read-write, and add {app_config.ADMIN_WEB_PROFILE} to COMPOSE_PROFILES",
    ),
    Feature(
        key="billing",
        title="Billing with Google Play and Google sign-in",
        summary="Users sign in with Google and buy transcription minutes in the Android app.",
        switch="BILLING_PROVIDER=google_play",
        enabled=lambda config: config.billing.provider == "google_play",
        docs="docs/google-play-billing.md",
        turn_on={"BILLING_PROVIDER": "google_play"},
        turn_off={"BILLING_PROVIDER": "none"},
        settings=("GOOGLE_PLAY_PACKAGE_NAME", "GOOGLE_OAUTH_CLIENT_IDS", "BILLING_PRODUCTS", "FREE_MINUTES",
                  "FREE_MINUTES_PERIOD", "GOOGLE_PLAY_VOIDED_PURCHASES_INTERVAL"),
        required=("GOOGLE_PLAY_PACKAGE_NAME", "GOOGLE_OAUTH_CLIENT_IDS"),
        secrets=(Secret("google_play_service_account.json", "The service account's JSON key", kind="file",
                        checked=False),),
        requirement_sections=("Google sign-in", "Google Play"),
        problems=_billing_problems,
    ),
    Feature(
        key="provider_backups",
        title="Backups by the database's provider",
        summary="A managed PostgreSQL's provider backs the billing database up, instead of the server.",
        switch="DATABASE_BACKUPS=provider",
        enabled=lambda config: config.database.backups == "provider",
        docs="docs/architecture.md#managed-redis-and-postgresql",
        turn_on={"DATABASE_BACKUPS": "provider"},
        turn_off={"DATABASE_BACKUPS": "server"},
        requirement_sections=("PostgreSQL",),
        problems=_provider_backups_problems,
    ),
    Feature(
        key="offsite",
        title="Off-site backups",
        summary="Encrypted copies of the server's billing backups in a bucket away from this machine.",
        switch="OFFSITE_BACKUP_BUCKET",
        when="OFFSITE_BACKUP_BUCKET is set",
        enabled=lambda config: config.offsite.enabled,
        docs="docs/configuration.md#off-site-copies",
        settings=("OFFSITE_BACKUP_BUCKET", "OFFSITE_BACKUP_ENDPOINT", "OFFSITE_BACKUP_REGION",
                  "OFFSITE_BACKUP_RECIPIENT"),
        required=("OFFSITE_BACKUP_ENDPOINT",),
        secrets=(
            Secret("offsite_backup_access_key", "Access key ID for the backup bucket"),
            Secret("offsite_backup_secret_key", "Secret access key for the backup bucket"),
        ),
        excludes=(("provider_backups", "copies the server's backups, which {other} turns off"),),
        requirement_sections=("Off-site backups",),
        problems=_offsite_problems,
        how="set OFFSITE_BACKUP_BUCKET",
    ),
    Feature(
        key="object_storage",
        title="Object storage",
        summary="An S3-compatible bucket, which outsourcing and JOB_STORAGE=s3 use.",
        switch="STORAGE_BUCKET",
        when="STORAGE_BUCKET is set",
        enabled=lambda config: bool(config.storage.bucket),
        docs="docs/configuration.md#object-storage",
        settings=("STORAGE_BUCKET", "STORAGE_ENDPOINT", "STORAGE_REGION"),
        required=("STORAGE_ENDPOINT",),
        secrets=STORAGE_SECRETS,
        roles=("api", "worker", "remote", "all"),
        requirement_sections=("Storage",),
        how="set STORAGE_BUCKET and STORAGE_ENDPOINT, and the secret files storage_access_key and storage_secret_key",
        serves=("job_storage", "outsourcing"),
    ),
    Feature(
        key="job_storage",
        title="Job files in object storage",
        summary="Uploads and results in the bucket, for API replicas and workers on several machines.",
        switch="JOB_STORAGE=s3",
        enabled=lambda config: config.jobs.storage == "s3",
        docs="docs/architecture.md#job-storage",
        turn_on={"JOB_STORAGE": "s3"},
        turn_off={"JOB_STORAGE": "local"},
        needs=("object_storage",),
        roles=("api", "worker", "remote", "all"),
        requirement_sections=("Storage",),
    ),
    Feature(
        key="outsourcing",
        title="Outsourcing to RunPod",
        summary="Jobs run on RunPod Serverless GPUs when the local GPU is busy.",
        switch="OUTSOURCE_PROVIDER=runpod",
        enabled=lambda config: config.outsource.provider == "runpod",
        docs="docs/runpod.md",
        turn_on={"OUTSOURCE_PROVIDER": "runpod"},
        turn_off={"OUTSOURCE_PROVIDER": "none"},
        settings=("RUNPOD_ENDPOINT_ID", "OUTSOURCE_ROUTING", "OUTSOURCE_LOCAL_FIRST_MAX_WAIT",
                  "OUTSOURCE_MAX_CONCURRENCY", "OUTSOURCE_TIMEOUT", "OUTSOURCE_FALLBACK_LOCAL"),
        required=("RUNPOD_ENDPOINT_ID",),
        secrets=(
            Secret("runpod_api_key", "A RunPod API key restricted to the endpoint"),
            Secret("runpod_read_api_key", "A read-only RunPod key, for OUTSOURCE_MAX_CONCURRENCY=auto",
                   optional=True),
        ),
        needs=("object_storage",),
        roles=("api", "remote", "all"),
        requirement_sections=("Storage", "Image registry", "GPU endpoint"),
    ),
    Feature(
        key="error_reports",
        title="Error reports",
        summary="Errors of the API and workers go to a website's error monitor, with secrets removed.",
        switch="ERROR_REPORT_URL",
        when="ERROR_REPORT_URL is set",
        enabled=lambda config: bool(config.error_report.url),
        docs="docs/configuration.md#error-reports",
        settings=("ERROR_REPORT_URL",),
        secrets=(Secret("error_report_token", "The token the website expects", kind="generate"),),
        # The cleanup service has no secrets, so it sends no reports.
        roles=("api", "worker", "remote", "all"),
        how="set ERROR_REPORT_URL",
    ),
    Feature(
        key="metrics",
        title="Metrics for scaling",
        summary="GET /metrics reports queue depth and waits, for an autoscaler.",
        switch="the secret file metrics_token",
        enabled=lambda config: config.secrets.metrics_token is not None,
        docs="docs/architecture.md#-metrics-for-scaling",
        secrets=(Secret("metrics_token", "The token a scraper sends", kind="generate"),),
        switch_secret="metrics_token",
    ),
)

BY_KEY = {feature.key: feature for feature in FEATURES}


# --- Checks -----------------------------------------------------------------------

def setting_value(config: Settings, name: str) -> Any:
    group, field_name = app_config.known_variables()[name]
    return getattr(getattr(config, group), field_name)


def _is_set(value: Any) -> bool:
    if isinstance(value, (list, tuple, set)):
        return any(str(item).strip() for item in value)
    return value is not None and value != ""


def secret_present(config: Settings, name: str) -> bool:
    path = Path(config.secrets_dir) / name
    return path.is_file() and path.stat().st_size > 0


def feature_problems(feature: Feature, config: Settings, role: str = "all") -> list[str]:
    """What keeps a feature that is on from working, for one process role."""
    if role not in feature.roles:
        return []
    if not feature.enabled(config):
        return feature.off_problems(config, role) if feature.off_problems else []
    problems = []
    when = feature.when_text(config)
    subject = feature.switch_text(config)
    for key in feature.needs:
        needed = BY_KEY[key]
        if not needed.enabled(config):
            problems.append(f"{subject} needs {in_sentence(needed.title)}: {needed.how_text()}")
    for key, reason in feature.excludes:
        other = BY_KEY[key]
        if other.enabled(config):
            problems.append(f"{subject} {reason.format(other=other.switch_text(config))}")
    for name in feature.required:
        if not _is_set(setting_value(config, name)):
            problems.append(f"{name} is required when {when}")
    for secret in feature.secrets:
        if secret.checked and not secret.optional and not secret_present(config, secret.name):
            problems.append(f"{subject} needs the secret file {config.secrets_dir}/{secret.name}")
    if feature.problems:
        problems.extend(feature.problems(config, role))
    return problems


def all_problems(config: Settings, role: str) -> list[str]:
    problems = []
    for feature in FEATURES:
        for problem in feature_problems(feature, config, role):
            if problem not in problems:
                problems.append(problem)
    return problems


def unused_settings_warnings(config: Settings, environment: dict[str, str]) -> list[str]:
    """Settings of a feature that is off, which do nothing, and a feature on
    that nothing uses."""
    warnings = []
    for feature in FEATURES:
        if feature.enabled(config):
            if feature.serves and not any(BY_KEY[key].enabled(config) for key in feature.serves):
                users = " or ".join(BY_KEY[key].switch_text(config) for key in feature.serves)
                warnings.append(f"{feature.title} is set up, but nothing uses it: {users} would")
            continue
        switches = set(feature.turn_on)
        unused = [name for name in feature.settings if name in environment and name not in switches]
        if unused:
            warnings.append(
                f"{', '.join(unused)} {'has' if len(unused) == 1 else 'have'} no effect while "
                f"{in_sentence(feature.title)} is off; to use {'it' if len(unused) == 1 else 'them'}, "
                f"{feature.how_text()}"
            )
    return warnings


# --- Listing ------------------------------------------------------------------------

def _setting(config: Settings, name: str, required: bool) -> dict:
    group, field_name = app_config.known_variables()[name]
    group_class = app_config.GROUPS[group]
    info = group_class.model_fields[field_name]
    choices = [str(choice) for choice in get_args(info.annotation)] if "Literal" in str(info.annotation) else None
    value = getattr(getattr(config, group), field_name)
    default = info.get_default(call_default_factory=True)
    if app_config.is_duration(group_class, field_name):
        value = app_config.duration_text(value) if value is not None else None
        default = app_config.duration_text(default) if default is not None else None
    return {
        "name": name,
        "value": value,
        "default": default,
        "choices": choices or None,
        "required": required,
    }


def turn_off_actions(feature: Feature) -> dict:
    """What turns a feature off: values to set, settings to remove, a secret
    file to delete, and profiles to take from COMPOSE_PROFILES."""
    switch_setting = feature.switch if isinstance(feature.switch, str) else None
    return {
        "set": dict(feature.turn_off),
        "unset": [switch_setting] if switch_setting in app_config.known_variables() and not feature.turn_off else [],
        "delete_secret": feature.switch_secret,
        "remove_profiles": list(feature.profiles),
    }


def describe_features(config: Settings) -> list[dict]:
    """Every feature with its state, for `features --json`."""
    listed = []
    for feature in FEATURES:
        listed.append({
            "key": feature.key,
            "title": feature.title,
            "summary": feature.summary,
            "enabled": feature.enabled(config),
            "switch": feature.switch_text(config),
            "how": feature.how_text(),
            "turn_on": feature.turn_on,
            "turn_off": feature.turn_off,
            "profiles": list(feature.profiles),
            "needs": list(feature.needs),
            "needed_by": [other.key for other in FEATURES if feature.key in other.needs and other.enabled(config)],
            "excludes": [key for key, _ in feature.excludes],
            "off": turn_off_actions(feature),
            # A setup must also get the setting that turns the feature on.
            "settings": [
                _setting(config, name, name in feature.required or name == feature.switch)
                for name in feature.settings
            ],
            "secrets": [
                {
                    "name": secret.name,
                    "description": secret.description,
                    "kind": secret.kind,
                    "optional": secret.optional,
                    "present": secret_present(config, secret.name),
                }
                for secret in feature.secrets
            ],
            "problems": feature_problems(feature, config, "all"),
            "docs": feature.docs,
            "requirement_sections": list(feature.requirement_sections),
        })
    return listed


def summary_lines(config: Settings) -> list[str]:
    """The features' states, for `check`."""
    lines = ["Features (python scripts/setup.py sets them up):"]
    for feature in FEATURES:
        if feature.enabled(config):
            lines.append(f"  on   {feature.title} ({feature.switch_text(config)})")
        else:
            lines.append(f"  off  {feature.title}")
    return lines
