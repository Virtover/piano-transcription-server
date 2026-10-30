import json
import re
from pathlib import Path

import pytest

from app import config as app_config
from app import features
from app.config import validate_settings
from app.features import BY_KEY, FEATURES, all_problems, describe_features, unused_settings_warnings


ROOT = Path(__file__).resolve().parent.parent


def secret(default_settings, name, value="x" * 32):
    path = Path(default_settings.secrets_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


# --- The table ------------------------------------------------------------------------

def test_every_reference_is_to_a_known_feature_setting_and_section():
    from app.requirements import build_requirements

    sections = {section.name for section in build_requirements(app_config.settings)}
    for feature in FEATURES:
        for key in (*feature.needs, *(key for key, _ in feature.excludes), *feature.serves):
            assert key in BY_KEY, (feature.key, key)
        for name in (*feature.settings, *feature.required, *feature.turn_on, *feature.turn_off):
            assert name in app_config.known_variables(), (feature.key, name)
        assert set(feature.required) <= set(feature.settings), feature.key
        assert set(feature.requirement_sections) <= sections, feature.key
        assert (ROOT / feature.docs.split("#")[0]).is_file(), feature.docs


def test_a_basic_server_has_every_feature_off_and_no_problem(default_settings):
    assert not any(feature.enabled(default_settings) for feature in FEATURES)
    assert all_problems(default_settings, "all") == []


def test_needs_come_before_what_needs_them():
    order = [feature.key for feature in FEATURES]
    for feature in FEATURES:
        for key in feature.needs:
            assert order.index(key) < order.index(feature.key)


# --- Needs, exclusions, required settings, and secrets -----------------------------------

def test_a_feature_names_what_it_needs_and_how_to_turn_it_on(default_settings):
    default_settings.outsource.provider = "runpod"

    problems = validate_settings(default_settings, "remote")

    assert (
        "OUTSOURCE_PROVIDER=runpod needs object storage: set STORAGE_BUCKET and STORAGE_ENDPOINT, and the secret "
        "files storage_access_key and storage_secret_key"
    ) in problems
    assert "RUNPOD_ENDPOINT_ID is required when OUTSOURCE_PROVIDER=runpod" in problems
    assert f"OUTSOURCE_PROVIDER=runpod needs the secret file {default_settings.secrets_dir}/runpod_api_key" in problems
    # Optional secrets are not required.
    assert not any("runpod_read_api_key" in problem for problem in problems)


def test_a_need_met_is_quiet(default_settings):
    default_settings.outsource.provider = "runpod"
    default_settings.runpod.endpoint_id = "endpoint"
    default_settings.storage.bucket = "audio"
    default_settings.storage.endpoint = "https://storage.example"
    for name in ("runpod_api_key", "storage_access_key", "storage_secret_key"):
        secret(default_settings, name)

    assert validate_settings(default_settings, "remote") == []


def test_two_features_needing_one_report_it_once(default_settings):
    default_settings.outsource.provider = "runpod"
    default_settings.jobs.storage = "s3"
    default_settings.storage.bucket = "audio"

    problems = validate_settings(default_settings, "all")

    assert problems.count("STORAGE_ENDPOINT is required when STORAGE_BUCKET is set") == 1


def test_offsite_copies_exclude_provider_backups(default_settings):
    default_settings.offsite.bucket = "backups"
    default_settings.database.backups = "provider"

    problems = validate_settings(default_settings, "api")

    assert "OFFSITE_BACKUP_BUCKET copies the server's backups, which DATABASE_BACKUPS=provider turns off" in problems


def test_provider_backups_need_a_managed_database(default_settings):
    default_settings.database.backups = "provider"

    assert any(problem.startswith("DATABASE_BACKUPS=provider needs a managed database")
               for problem in validate_settings(default_settings, "api"))

    default_settings.database.url = "postgresql://billing@db.example.com/billing?sslmode=verify-full"
    assert validate_settings(default_settings, "api") == []


def test_each_role_checks_only_its_features(default_settings):
    default_settings.billing.provider = "google_play"

    assert validate_settings(default_settings, "worker") == []
    assert validate_settings(default_settings, "api")


def test_switches_set_by_mode_name_it(default_settings):
    default_settings.admin_web.mode = "read-write"
    default_settings.tunnel.compose_profiles = "admin"

    problems = validate_settings(default_settings, "all")

    assert "ADMIN_WEB_HOSTNAME is required when ADMIN_WEB_MODE=read-write" in problems
    assert any(problem.startswith("ADMIN_WEB_MODE=read-write needs HTTPS through a Cloudflare Tunnel")
               for problem in problems)


# --- Unused settings -----------------------------------------------------------------------

def test_settings_of_a_feature_that_is_off_are_pointed_out(default_settings):
    warnings = unused_settings_warnings(default_settings, {"RUNPOD_ENDPOINT_ID": "x", "OUTSOURCE_TIMEOUT": "5m"})

    assert warnings == [
        "RUNPOD_ENDPOINT_ID, OUTSOURCE_TIMEOUT have no effect while outsourcing to RunPod is off; to use them, "
        "set OUTSOURCE_PROVIDER=runpod"
    ]


def test_a_switch_set_to_off_is_not_unused(default_settings):
    assert unused_settings_warnings(default_settings, {"OUTSOURCE_PROVIDER": "none", "BILLING_PROVIDER": "none"}) == []


def test_object_storage_that_nothing_uses_is_pointed_out(default_settings):
    default_settings.storage.bucket = "audio"

    [warning] = unused_settings_warnings(default_settings, {"STORAGE_BUCKET": "audio"})

    assert warning == "Object storage is set up, but nothing uses it: JOB_STORAGE=s3 or OUTSOURCE_PROVIDER=runpod would"


# --- Listing ---------------------------------------------------------------------------------

def test_features_json_lists_state_settings_and_secrets(default_settings):
    default_settings.outsource.provider = "runpod"
    secret(default_settings, "runpod_api_key")

    listed = {feature["key"]: feature for feature in describe_features(default_settings)}

    outsourcing = listed["outsourcing"]
    assert outsourcing["enabled"] is True
    assert outsourcing["needs"] == ["object_storage"]
    assert outsourcing["turn_off"] == {"OUTSOURCE_PROVIDER": "none"}
    assert listed["object_storage"]["needed_by"] == ["outsourcing"]
    assert listed["object_storage"]["off"] == {
        "set": {}, "unset": ["STORAGE_BUCKET"], "delete_secret": None, "remove_profiles": [],
    }
    assert listed["metrics"]["off"]["delete_secret"] == "metrics_token"
    assert listed["tunnel"]["off"]["remove_profiles"] == ["cloudflare"]
    routing = next(item for item in outsourcing["settings"] if item["name"] == "OUTSOURCE_ROUTING")
    assert routing["choices"] == ["local_first", "remote_first_overflow", "remote_first_fallback"]
    timeout = next(item for item in outsourcing["settings"] if item["name"] == "OUTSOURCE_TIMEOUT")
    assert timeout["default"] == timeout["value"] == "15m"
    assert {item["name"]: item["present"] for item in outsourcing["secrets"]} == {
        "runpod_api_key": True, "runpod_read_api_key": False,
    }
    assert any("needs object storage" in problem for problem in outsourcing["problems"])
    # JSON for scripts: nothing that json cannot write.
    json.dumps(describe_features(default_settings), default=str)


def test_features_command(tmp_path):
    import subprocess
    import sys

    (tmp_path / ".env").write_text("JOB_STORAGE=s3\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "app.config", "features", "--json", "--env-file", str(tmp_path / ".env"),
         "--secrets-dir", str(tmp_path)],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )

    listed = {feature["key"]: feature for feature in json.loads(result.stdout)}
    assert listed["job_storage"]["enabled"] is True


# --- server.env.example and the docs follow the table -----------------------------------------

def test_env_example_has_a_block_for_every_feature():
    example = (ROOT / "server.env.example").read_text(encoding="utf-8")
    for feature in FEATURES:
        if feature.key == "provider_backups":
            # A choice of the billing database's block.
            assert "# DATABASE_BACKUPS=server" in example
            continue
        block = re.search(rf"^# --- {re.escape(feature.title)} -+$\n(.*?)(?=^# ---|\Z)", example, re.M | re.S)
        assert block, feature.title
        text = block.group(1)
        assert text.startswith("# On with:"), feature.title
        for name in feature.settings:
            if name in app_config.COMPOSE_ONLY_VARIABLES:
                # Docker Compose reads it, so it is in .env.
                assert re.search(rf"^#? ?{name}=", (ROOT / ".env.example").read_text(encoding="utf-8"), re.M), name
                continue
            assert re.search(rf"^#? ?{name}=", text, re.M), (feature.title, name)
        # A Compose profile is in .env, which says what it starts.
        for profile in feature.profiles:
            assert profile in (ROOT / ".env.example").read_text(encoding="utf-8"), (feature.title, profile)
        for item in feature.secrets:
            assert item.name in text, (feature.title, item.name)


def test_the_docs_list_every_feature():
    documentation = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    table = documentation.split("### 🧩 Optional features", 1)[1].split("\n### ", 1)[0]
    for feature in FEATURES:
        assert f"| {feature.title} |" in table, feature.title
