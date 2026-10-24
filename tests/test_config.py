import logging
import re
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import config
from app.config import ConfigError, duration_seconds, load_settings, validate_settings

ROOT = Path(__file__).resolve().parent.parent


def load(tmp_path, text="", secrets=None):
    env_file = tmp_path / ".env"
    env_file.write_text(text, encoding="utf-8")
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(exist_ok=True)
    for name, content in (secrets or {}).items():
        (secrets_dir / name).write_text(content, encoding="utf-8")
    return load_settings(str(env_file), str(secrets_dir))


# --- Value parsing --------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "seconds"),
    [("30s", 30), ("15m", 900), ("1h", 3600), (" 2D ", 172800), ("1w", 604800), ("1.5h", 5400), ("0.5s", 0.5)],
)
def test_duration_parsing(value, seconds):
    assert duration_seconds(value, "SETTING") == seconds


@pytest.mark.parametrize("value", ["", "h", "10", "1y", "0h", "-1d", "nanm", "infh"])
def test_invalid_duration_is_rejected(value):
    with pytest.raises(ValueError, match="SETTING"):
        duration_seconds(value, "SETTING")


@pytest.mark.parametrize(("value", "parsed"), [("auto", None), ("AUTO", None), ("4", 4), (" 12 ", 12)])
def test_auto_or_positive_integer(tmp_path, value, parsed):
    assert load(tmp_path, f"WORKER_THREADS={value}\n").worker.threads == parsed


@pytest.mark.parametrize("value", ["0", "-2", "1.5", "many"])
def test_invalid_auto_integer_names_the_variable(tmp_path, value):
    with pytest.raises(ConfigError, match=r"WORKER_BATCH_SIZE: must be 'auto' or a positive integer"):
        load(tmp_path, f"WORKER_BATCH_SIZE={value}\n")


def test_errors_are_collected_across_groups(tmp_path):
    with pytest.raises(ConfigError) as error:
        load(tmp_path, "WORKER_MODEL_MEMORY_GIB=-1\nCLEANUP_INTERVAL=often\nBILLING_PROVIDER=stripe\nFREE_MINUTES=x\n")

    names = sorted(problem.split(":")[0] for problem in error.value.problems)
    assert names == ["BILLING_PROVIDER", "CLEANUP_INTERVAL", "FREE_MINUTES", "WORKER_MODEL_MEMORY_GIB"]


def test_plain_number_needs_a_unit_for_new_names(tmp_path):
    with pytest.raises(ConfigError, match="CLEANUP_INTERVAL: .*30s, 10m"):
        load(tmp_path, "CLEANUP_INTERVAL=600\n")


def test_inline_comments_and_empty_values(tmp_path):
    settings = load(tmp_path, "CLEANUP_INTERVAL=1d # once a day\nMAX_VIDEO_LENGTH_MINUTES=\n")

    assert settings.cleanup.interval == 86400
    assert settings.limits.max_video_length_minutes is None


def test_process_environment_overrides_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_BATCH_SIZE", "3")

    assert load(tmp_path, "WORKER_BATCH_SIZE=8\n").worker.batch_size == 3


def test_assignment_is_validated(default_settings):
    default_settings.worker.batch_timeout = "500ms"[:-2] + "s"
    assert default_settings.worker.batch_timeout == 500
    with pytest.raises(ValidationError):
        default_settings.worker.batch_size = 0


# --- Deprecated names -----------------------------------------------------

@pytest.mark.parametrize(
    ("line", "group", "field", "value"),
    [
        ("CLEANUP_INTERVAL_SECONDS=86400", "cleanup", "interval", 86400),
        ("CLEANUP_INTERVAL_SECONDS=86400 # 24 hours", "cleanup", "interval", 86400),
        ("CLEANUP_INTERVAL_SECONDS=2h", "cleanup", "interval", 7200),
        ("WORKER_BATCH_TIMEOUT_SECONDS=2", "worker", "batch_timeout", 2),
        ("WORKER_BATCH_TIMEOUT_SECONDS=0.5", "worker", "batch_timeout", 0.5),
    ],
)
def test_deprecated_aliases_resolve(tmp_path, caplog, line, group, field, value):
    with caplog.at_level(logging.WARNING, logger="app.config"):
        settings = load(tmp_path, line + "\n")

    assert getattr(getattr(settings, group), field) == value
    old_name = line.split("=")[0]
    warnings = [record.getMessage() for record in caplog.records if old_name in record.getMessage()]
    assert warnings == [f"{old_name} is deprecated; use {config.ALIASES[old_name].new_name} instead"]


def test_every_alias_points_to_a_known_setting():
    known = config.known_variables()
    for old_name, alias in config.ALIASES.items():
        assert alias.new_name in known
        assert old_name not in known


def test_old_and_new_name_together_fail(tmp_path):
    with pytest.raises(ConfigError, match="CLEANUP_INTERVAL_SECONDS and CLEANUP_INTERVAL are both set"):
        load(tmp_path, "CLEANUP_INTERVAL_SECONDS=600\nCLEANUP_INTERVAL=10m\n")


def test_invalid_deprecated_value_names_the_old_variable(tmp_path):
    with pytest.raises(ConfigError, match=r"CLEANUP_INTERVAL_SECONDS \(deprecated name of CLEANUP_INTERVAL\)"):
        load(tmp_path, "CLEANUP_INTERVAL_SECONDS=-5\n")


def test_current_production_style_env_still_loads(tmp_path, caplog):
    env = "\n".join([
        "REDIS_URL=redis://redis:6379/0",
        "DATA_DIR=/data",
        "BILLING_DATABASE_PATH=/billing/billing.sqlite3",
        "WORKER_THREADS=4",
        "WORKER_BATCH_SIZE=6",
        "WORKER_BATCH_TIMEOUT_SECONDS=2",
        "WORKER_MAX_CONCURRENCY=auto",
        "CLEANUP_INTERVAL_SECONDS=86400 # 24 hours",
        "MAX_VIDEO_LENGTH_MINUTES=120",
        "BILLING_PROVIDER=google_play",
        'GOOGLE_OAUTH_CLIENT_IDS=["client.apps.googleusercontent.com"]',
        'BILLING_PRODUCTS={"transcription_60":60}',
        "GOOGLE_PLAY_PACKAGE_NAME=com.example.app",
        "GOOGLE_PLAY_SERVICE_ACCOUNT_FILE=account.json",
        "FREE_MINUTES=10",
        "FREE_MINUTES_PERIOD=30d",
    ]) + "\n"
    with caplog.at_level(logging.WARNING, logger="app.config"):
        settings = load(tmp_path, env, {"account.json": '{"type": "service_account"}'})

    assert settings.cleanup.interval == 86400
    assert settings.worker.batch_timeout == 2
    assert settings.worker.threads == 4
    assert settings.billing.free_minutes_period == "30d"
    assert validate_settings(settings, "api") == []
    assert {record.getMessage().split(" ")[0] for record in caplog.records} == {
        "CLEANUP_INTERVAL_SECONDS",
        "WORKER_BATCH_TIMEOUT_SECONDS",
        "GOOGLE_PLAY_SERVICE_ACCOUNT_FILE",
    }


# --- Unknown variables ----------------------------------------------------

def test_unknown_variable_suggests_closest_name(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="app.config"):
        load(tmp_path, "WORKER_BATCHSIZE=4\nBILLING_PROVIDR=none\nGOOGLE_PLAY_PACKAGE=x\nOTHER_SETTING=1\n")

    messages = sorted(record.getMessage() for record in caplog.records)
    assert messages == [
        "Unknown setting BILLING_PROVIDR; did you mean BILLING_PROVIDER?",
        "Unknown setting GOOGLE_PLAY_PACKAGE; did you mean GOOGLE_PLAY_PACKAGE_NAME?",
        "Unknown setting WORKER_BATCHSIZE; did you mean WORKER_BATCH_SIZE?",
    ]


def test_known_and_deprecated_names_are_not_reported():
    environment = {name: "x" for name in [*config.known_variables(), *config.ALIASES]}

    assert config.unknown_variable_warnings(environment) == []


# --- Secrets --------------------------------------------------------------

def test_service_account_secret_is_read_from_file_and_masked(tmp_path):
    settings = load(tmp_path, secrets={"google_play_service_account.json": '{"private_key": "very-secret"}'})

    assert config.google_play_service_account_json(settings) == '{"private_key": "very-secret"}'
    assert "very-secret" not in repr(settings)
    assert "very-secret" not in "\n".join(config.describe(settings))


def test_deprecated_service_account_file_override(tmp_path):
    secret = '{"type": "service_account"}'
    relative = load(tmp_path, "GOOGLE_PLAY_SERVICE_ACCOUNT_FILE=legacy.json\n", {"legacy.json": secret})
    absolute_path = tmp_path / "elsewhere.json"
    absolute_path.write_text(secret)
    absolute = load(tmp_path, f"GOOGLE_PLAY_SERVICE_ACCOUNT_FILE={absolute_path}\n")

    assert config.google_play_service_account_json(relative) == secret
    assert config.google_play_service_account_json(absolute) == secret


@pytest.mark.parametrize(
    "name",
    ["GOOGLE_PLAY_SERVICE_ACCOUNT", "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", "google_play_service_account"],
)
def test_secrets_cannot_come_from_environment(tmp_path, monkeypatch, name):
    monkeypatch.setenv(name, "{}")
    settings = load(tmp_path, f"{name}={{}}\ngoogle_play_service_account.json={{}}\n")

    assert settings.secrets.google_play_service_account is None


# --- Fail fast --------------------------------------------------------------

GOOGLE_PLAY_ENV = 'BILLING_PROVIDER=google_play\nGOOGLE_PLAY_PACKAGE_NAME=com.example\nGOOGLE_OAUTH_CLIENT_IDS=["id"]\n'
ACCOUNT = {"google_play_service_account.json": '{"type": "service_account"}'}


def test_google_play_complete_configuration_is_valid(tmp_path):
    assert validate_settings(load(tmp_path, GOOGLE_PLAY_ENV, ACCOUNT), "api") == []


@pytest.mark.parametrize(
    ("env", "secrets", "message"),
    [
        ("BILLING_PROVIDER=google_play\nGOOGLE_OAUTH_CLIENT_IDS=[\"id\"]\n", ACCOUNT, "GOOGLE_PLAY_PACKAGE_NAME is required"),
        ("BILLING_PROVIDER=google_play\nGOOGLE_PLAY_PACKAGE_NAME=x\n", ACCOUNT, "GOOGLE_OAUTH_CLIENT_IDS is required"),
        (GOOGLE_PLAY_ENV, {}, "google_play_service_account.json is missing"),
        (GOOGLE_PLAY_ENV + "GOOGLE_PLAY_SERVICE_ACCOUNT_FILE=gone.json\n", {}, r"GOOGLE_PLAY_SERVICE_ACCOUNT_FILE \(gone.json\) cannot be read"),
        (GOOGLE_PLAY_ENV, {"google_play_service_account.json": "not json"}, "not valid JSON"),
    ],
)
def test_google_play_requirements(tmp_path, env, secrets, message):
    problems = validate_settings(load(tmp_path, env, secrets), "api")

    assert len(problems) == 1
    assert re.search(message, problems[0])


def test_server_password_is_read_from_file_and_masked(tmp_path):
    settings = load(tmp_path, secrets={"server_password": "correct-horse-battery\n"})

    assert settings.secrets.server_password.get_secret_value() == "correct-horse-battery"
    assert "correct-horse-battery" not in "\n".join(config.describe(settings))
    assert validate_settings(settings, "api") == []


def test_short_server_password_stops_the_api_only(tmp_path):
    settings = load(tmp_path, secrets={"server_password": "short"})

    assert validate_settings(settings, "worker") == []
    [problem] = validate_settings(settings, "api")
    assert "server_password must hold at least 12 characters" in problem


def test_worker_and_cleanup_do_not_need_google_play_secrets(tmp_path):
    settings = load(tmp_path, "BILLING_PROVIDER=google_play\n")

    assert validate_settings(settings, "worker") == []
    assert validate_settings(settings, "cleanup") == []
    assert validate_settings(settings, "api") != []


def test_api_startup_fails_on_invalid_configuration(api_context):
    from app import main

    _, settings, _ = api_context
    settings.billing.provider = "google_play"

    with pytest.raises(ConfigError, match="GOOGLE_PLAY_PACKAGE_NAME"):
        main.sync_billing_catalog()


# --- `python -m app.config check` -------------------------------------------

def run_check(*arguments):
    return subprocess.run(
        [sys.executable, "-m", "app.config", "check", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "", "SYSTEMROOT": "C:\\Windows"} if sys.platform == "win32" else {"PATH": ""},
    )


def test_check_passes_on_env_example(tmp_path):
    result = run_check("--env-file", ".env.example", "--secrets-dir", str(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "Configuration OK" in result.stdout
    assert "CLEANUP_INTERVAL = 10m (600 s)" in result.stdout
    assert "warning" not in result.stderr


def test_check_reports_errors_and_masks_secrets(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(GOOGLE_PLAY_ENV.replace("GOOGLE_PLAY_PACKAGE_NAME=com.example\n", "") + "WORKER_BATCHSIZE=2\n")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "google_play_service_account.json").write_text('{"private_key": "very-secret"}')

    result = run_check("--env-file", str(env_file), "--secrets-dir", str(secrets))

    assert result.returncode == 1
    assert "GOOGLE_PLAY_PACKAGE_NAME is required" in result.stderr
    assert "did you mean WORKER_BATCH_SIZE?" in result.stderr
    assert "very-secret" not in result.stdout + result.stderr
    assert "********" in result.stdout


def test_check_reports_invalid_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("WORKER_BATCH_SIZE=lots\n")

    result = run_check("--env-file", str(env_file), "--secrets-dir", str(tmp_path))

    assert result.returncode == 1
    assert "WORKER_BATCH_SIZE: must be 'auto' or a positive integer" in result.stderr


# --- Drift guards ---------------------------------------------------------

def example_keys() -> set[str]:
    keys = set()
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#?\s?([A-Z][A-Z0-9_]+)=", line)
        if match:
            keys.add(match.group(1))
    return keys


def test_every_env_example_key_is_a_known_setting():
    unknown = example_keys() - set(config.known_variables()) - config.COMPOSE_VARIABLES

    assert unknown == set()


def test_compose_variables_are_used_and_documented():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    documentation = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")

    for name in config.COMPOSE_VARIABLES:
        assert "${" + name in compose
        assert f"`{name}`" in documentation


def test_env_example_has_no_inline_comments_or_wiring():
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if re.match(r"^#?\s?[A-Z][A-Z0-9_]+=", line):
            assert " #" not in line, line
    assert not example_keys() & {"REDIS_URL", "DATA_DIR", "BILLING_DATABASE_PATH", "BILLING_BACKUP_DIR"}


def test_every_setting_is_documented():
    documentation = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    names = [*config.known_variables(), *config.ALIASES]
    secrets = [
        config.env_name(group, field)
        for group in config.GROUPS.values()
        for field in group.model_fields
        if config.is_secret(group, field)
    ]

    missing = [name for name in names + secrets if f"`{name}`" not in documentation]
    assert missing == []


def test_settings_import_without_gpu_libraries():
    code = (
        "import sys\n"
        "sys.modules['torch'] = None\n"
        "sys.modules['transkun'] = None\n"
        "import app.config, app.main\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


# --- Outsourcing ------------------------------------------------------------

RUNPOD_ENV = (
    "OUTSOURCE_PROVIDER=runpod\n"
    "RUNPOD_ENDPOINT_ID=abc123\n"
    "OUTSOURCE_STORAGE_ENDPOINT=https://account.eu.r2.cloudflarestorage.com\n"
    "OUTSOURCE_STORAGE_BUCKET=piano-audio\n"
)
RUNPOD_SECRETS = {
    "runpod_api_key": "rp-key",
    "outsource_storage_access_key": "access",
    "outsource_storage_secret_key": "secret",
}


def test_outsourcing_defaults(tmp_path):
    settings = load(tmp_path)

    assert settings.outsource.provider == "none"
    assert settings.outsource.max_wait == 30
    assert settings.outsource.max_concurrency == 4
    assert settings.outsource.timeout == 900
    assert settings.outsource.fallback_local is True
    assert settings.runpod.api_base == "https://api.runpod.ai/v2"
    assert validate_settings(settings, "all") == []


def test_complete_runpod_configuration_is_valid(tmp_path):
    settings = load(tmp_path, RUNPOD_ENV + "OUTSOURCE_MAX_WAIT=0s\n", RUNPOD_SECRETS)

    assert validate_settings(settings, "remote") == []
    assert settings.secrets.runpod_api_key.get_secret_value() == "rp-key"
    assert "rp-key" not in repr(settings)


@pytest.mark.parametrize(
    "missing",
    ["RUNPOD_ENDPOINT_ID", "OUTSOURCE_STORAGE_ENDPOINT", "OUTSOURCE_STORAGE_BUCKET", *RUNPOD_SECRETS],
)
def test_runpod_requirements(tmp_path, missing):
    env = "".join(line + "\n" for line in RUNPOD_ENV.splitlines() if not line.startswith(missing))
    secrets = {name: value for name, value in RUNPOD_SECRETS.items() if name != missing}

    problems = validate_settings(load(tmp_path, env, secrets), "remote")

    assert len(problems) == 1
    assert missing in problems[0]
    assert validate_settings(load(tmp_path, env, secrets), "api") == problems


def test_runpod_settings_not_needed_by_gpu_worker(tmp_path):
    assert validate_settings(load(tmp_path, "OUTSOURCE_PROVIDER=runpod\n"), "worker") == []


def test_runpod_secrets_cannot_come_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "leaked")

    settings = load(tmp_path, RUNPOD_ENV + "OUTSOURCE_STORAGE_SECRET_KEY=leaked\n")

    assert settings.secrets.runpod_api_key is None
    assert settings.secrets.outsource_storage_secret_key is None


@pytest.mark.parametrize("line", ["OUTSOURCE_PROVIDER=aws", "OUTSOURCE_MAX_CONCURRENCY=0", "OUTSOURCE_TIMEOUT=15", "OUTSOURCE_FALLBACK_LOCAL=maybe"])
def test_invalid_outsourcing_values(tmp_path, line):
    with pytest.raises(ConfigError, match=line.split("=")[0]):
        load(tmp_path, line + "\n")
