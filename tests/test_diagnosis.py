"""check --json, validate, and session (app/diagnosis.py): the problems as
data for scripts/setup.py, values checked one at a time, and one container
answering a whole setup."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import config as config_module
from app import diagnosis, online_check
from app.online_check import Result


ROOT = Path(__file__).resolve().parent.parent


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def files(tmp_path):
    """An empty .env and server.env, and an empty secrets folder."""
    write(tmp_path / ".env", "")
    write(tmp_path / "server.env", "")
    (tmp_path / "secrets").mkdir()
    return tmp_path


def report(root, **options):
    return diagnosis.report([str(root / ".env"), str(root / "server.env")], str(root / "secrets"), **options)


# --- Names in a message -----------------------------------------------------------

def test_a_message_names_its_settings_and_secret_files_in_order():
    text = "OUTSOURCE_PROVIDER=runpod needs the secret file /run/secrets/runpod_api_key and STORAGE_BUCKET"

    assert diagnosis.mentioned_names(text) == ["OUTSOURCE_PROVIDER", "runpod_api_key", "STORAGE_BUCKET"]


def test_a_secret_inside_a_longer_name_is_not_named():
    assert diagnosis.mentioned_names("needs cluster_storage_access_key") == ["cluster_storage_access_key"]
    assert diagnosis.mentioned_names("Rewrite secrets/storage_access_key.") == ["storage_access_key"]
    assert diagnosis.mentioned_names("the tunnel token in secrets/cloudflare/tunnel_token") == ["cloudflare/tunnel_token"]


def test_words_in_capitals_that_are_no_setting_are_not_named():
    assert diagnosis.mentioned_names("HTTP 401 from the API: check RUNPOD_ENDPOINT_ID") == ["RUNPOD_ENDPOINT_ID"]


# --- check --json -----------------------------------------------------------------

def test_a_value_that_cannot_be_read_names_its_setting(files):
    write(files / "server.env", "WORKER_BATCH_TIMEOUT=soon\n")

    result = report(files)

    assert result["loaded"] is False and result["ok"] is False
    assert result["errors"][0]["names"] == ["WORKER_BATCH_TIMEOUT"]
    assert "30s" in result["errors"][0]["text"]


def test_a_typo_in_a_name_comes_with_the_rename(files):
    write(files / "server.env", "STORAGE_BUCKT=audio\n")

    warning = next(item for item in report(files)["warnings"] if "STORAGE_BUCKT" in item["text"])

    assert warning["fix"] == {"action": "rename", "name": "STORAGE_BUCKT", "to": "STORAGE_BUCKET"}


def test_a_deprecated_name_comes_with_its_converted_value(files):
    write(files / "server.env", "CLEANUP_INTERVAL_SECONDS=60\n")

    warning = next(item for item in report(files)["warnings"] if item.get("fix"))

    assert warning["fix"] == {"action": "rename", "name": "CLEANUP_INTERVAL_SECONDS", "to": "CLEANUP_INTERVAL",
                              "value": "60s"}


def test_both_names_set_is_an_error_whose_fix_removes_the_old(files):
    write(files / "server.env", "CLEANUP_INTERVAL_SECONDS=60\nCLEANUP_INTERVAL=2m\n")

    errors = report(files)["errors"]

    assert errors[0]["fix"] == {"action": "remove", "name": "CLEANUP_INTERVAL_SECONDS"}
    assert len(errors) == 1


def test_a_secret_file_under_an_old_name_comes_with_the_rename(files):
    write(files / "secrets" / "outsource_storage_access_key", "key")

    warning = next(item for item in report(files)["warnings"] if item.get("fix"))

    assert warning["fix"] == {"action": "rename_secret", "name": "outsource_storage_access_key",
                              "to": "storage_access_key"}


def test_a_feature_problem_says_its_feature(files):
    write(files / "server.env", "STORAGE_BUCKET=audio\nSTORAGE_ENDPOINT=https://storage.example\n")

    errors = report(files)["errors"]

    assert {error["feature"] for error in errors} == {"object_storage"}
    assert {name for error in errors for name in error["names"]} >= {"storage_access_key", "storage_secret_key"}


def test_online_results_say_their_feature_and_what_their_hint_names(files, monkeypatch):
    seen = []

    def checks(config, features=None):
        seen.append(features)
        return [Result("Storage", "fail", "Upload: InvalidAccessKeyId", "Rewrite secrets/storage_access_key.",
                       feature="object_storage")]

    result = report(files, online=True, features=["object_storage"], run_checks=checks)

    assert seen == [["object_storage"]]
    assert result["ok"] is False
    assert result["online"] == [{"check": "Storage", "status": "fail", "detail": "Upload: InvalidAccessKeyId",
                                 "hint": "Rewrite secrets/storage_access_key.", "feature": "object_storage",
                                 "names": ["storage_access_key"]}]


def test_online_checks_wait_for_the_errors_to_be_fixed(files):
    write(files / "server.env", "WORKER_BATCH_TIMEOUT=soon\n")

    result = report(files, online=True, run_checks=lambda config, features=None: pytest.fail("ran the checks"))

    assert result["online"] is None


def test_the_command_prints_json_and_fails_with_errors(files, capsys):
    write(files / "server.env", "WORKER_BATCH_TIMEOUT=soon\n")
    arguments = ["check", "--json", "--env-file", str(files / "server.env"), "--secrets-dir", str(files / "secrets")]

    assert config_module.main(arguments) == 1
    assert json.loads(capsys.readouterr().out)["errors"][0]["names"] == ["WORKER_BATCH_TIMEOUT"]


def test_check_names_the_problems_of_settings_it_cannot_read_instead_of_failing_to_start(files):
    """app.config reads the settings when it is imported; as the command it
    must still start, to say what is wrong."""
    environment = {**os.environ, "WORKER_BATCH_TIMEOUT": "soon"}
    result = subprocess.run([sys.executable, "-m", "app.config", "check", "--env-file", str(files / ".env")],
                            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=120)

    assert result.returncode == 1
    assert "WORKER_BATCH_TIMEOUT" in result.stderr and "Traceback" not in result.stderr


# --- validate ---------------------------------------------------------------------

def test_a_value_is_checked_as_the_server_reads_it(files):
    env = [str(files / "server.env")]

    assert diagnosis.validate_value("WORKER_BATCH_TIMEOUT", "30s", env)["ok"] is True
    refused = diagnosis.validate_value("WORKER_BATCH_TIMEOUT", "30", env)
    assert refused["ok"] is False and "30s" in refused["problems"][0]
    assert not refused["problems"][0].startswith("WORKER_BATCH_TIMEOUT")
    assert diagnosis.validate_value("CLOUDFLARE_TUNNEL_HOSTNAME", "https://piano.example.com", env)["ok"] is False
    assert "WORKER_BATCH_TIMEOUT" not in os.environ


def test_another_setting_already_wrong_is_not_blamed_on_the_value(files):
    write(files / "server.env", "WORKER_BATCH_SIZE=many\n")

    assert diagnosis.validate_value("WORKER_BATCH_TIMEOUT", "30s", [str(files / "server.env")])["ok"] is True


def test_unknown_and_deprecated_names_are_refused(files):
    env = [str(files / "server.env")]

    assert diagnosis.validate_value("STORAGE_BUCKT", "x", env)["ok"] is False
    assert "CLEANUP_INTERVAL" in diagnosis.validate_value("CLEANUP_INTERVAL_SECONDS", "60", env)["problems"][0]
    # Docker Compose's own: it says when one is wrong.
    assert diagnosis.validate_value("API_BIND_ADDRESS", "127.0.0.1", env) == {
        "name": "API_BIND_ADDRESS", "ok": True, "problems": [], "file": ".env"}


# --- describe --all ---------------------------------------------------------------

def test_every_setting_people_choose_is_offered_with_its_choices(files, capsys):
    arguments = ["describe", "--all", "--json", "--env-file", str(files / "server.env"),
                 "--secrets-dir", str(files / "secrets")]

    assert config_module.main(arguments) == 0
    facts = {item["name"]: item for item in json.loads(capsys.readouterr().out)}

    assert facts["OUTSOURCE_ROUTING"]["choices"] == ["local_first", "remote_first_overflow", "remote_first_fallback"]
    assert facts["storage_access_key"]["kind"] == "secret"
    assert facts["cloudflare/tunnel_token"]["file"] == "secrets/cloudflare/tunnel_token"
    # Wiring and the files scripts write are not offered.
    assert "REDIS_URL" not in facts and "postgres_password" not in facts
    assert "cluster_storage_access_key" not in facts


# --- session ----------------------------------------------------------------------

def ask(*requests):
    return io.StringIO("".join(json.dumps({"args": list(arguments)}) + "\n" for arguments in requests))


def test_a_session_reads_the_files_as_they_are_at_each_command(files, monkeypatch):
    monkeypatch.setattr(config_module, "settings", config_module.settings)
    write(files / "server.env", "WORKER_BATCH_TIMEOUT=soon\n")
    answers = io.StringIO()

    class Requests(io.StringIO):
        """The second command after the file is fixed."""

        def __iter__(self):
            yield json.dumps({"args": ["check", "--json", "--secrets-dir", str(files / "secrets")]})
            write(files / "server.env", "WORKER_BATCH_TIMEOUT=30s\n")
            yield json.dumps({"args": ["check", "--json", "--secrets-dir", str(files / "secrets")]})

    assert diagnosis.serve(str(files), config_module.main, Requests(), answers) == 0
    first, second = [json.loads(line) for line in answers.getvalue().splitlines()]

    assert first["returncode"] == 1 and json.loads(first["stdout"])["loaded"] is False
    assert json.loads(second["stdout"])["loaded"] is True


def test_a_session_forgets_what_compose_gave_it_from_the_files(files, monkeypatch):
    write(files / "server.env", "WORKER_BATCH_TIMEOUT=10m\nDATABASE_URL=postgresql://wired\n")
    monkeypatch.setenv("WORKER_BATCH_TIMEOUT", "10m")
    # Wiring that docker-compose.yml sets over the file stays.
    monkeypatch.setenv("DATABASE_URL", "postgresql://from-compose")

    diagnosis.forget_environment([str(files / "server.env")])

    assert "WORKER_BATCH_TIMEOUT" not in os.environ
    assert os.environ["DATABASE_URL"] == "postgresql://from-compose"


def test_a_session_answers_failures_and_bad_requests_and_goes_on(files, monkeypatch):
    monkeypatch.setattr(config_module, "settings", config_module.settings)
    answers = io.StringIO()
    requests = io.StringIO("not json\n" + json.dumps({"args": ["no-such-command"]}) + "\n"
                           + json.dumps({"args": ["validate", "WORKER_BATCH_TIMEOUT", "1m", "--json"]}) + "\n")

    diagnosis.serve(str(files), config_module.main, requests, answers)
    bad, unknown, valid = [json.loads(line) for line in answers.getvalue().splitlines()]

    assert bad["returncode"] == 2
    assert unknown["returncode"] == 2 and "invalid choice" in unknown["stderr"]
    assert valid["returncode"] == 0 and json.loads(valid["stdout"])["ok"] is True


# --- Online checks by feature -----------------------------------------------------

def test_a_feature_is_checked_with_what_it_needs_and_nothing_else(default_settings, monkeypatch):
    default_settings.storage.bucket = "audio"
    default_settings.outsource.provider = "runpod"
    monkeypatch.setattr(online_check, "check_storage", lambda config: Result("Storage", "ok", "fine"))
    monkeypatch.setattr(online_check, "check_runpod", lambda config: [Result("RunPod", "ok", "fine")])
    monkeypatch.setattr(online_check, "check_redis", lambda config: pytest.fail("checked Redis"))

    results = online_check.run_checks(default_settings, ["outsourcing"])

    assert [(result.name, result.feature) for result in results] == [("Storage", "object_storage"),
                                                                      ("RunPod", "outsourcing")]


def test_a_feature_with_nothing_outside_the_server_says_so(default_settings):
    results = online_check.run_checks(default_settings, ["ensemble"])

    assert [(result.status, result.feature) for result in results] == [("skip", "ensemble")]
    assert "nothing outside the server" in results[0].detail


def test_every_feature_marked_online_has_a_check(default_settings, monkeypatch):
    """features.py's online flag, which tells setup to offer a test, matches
    what run_checks tests."""
    from app.features import FEATURES

    names = [name for name in dir(online_check) if name.startswith("check_")]
    for name in names:
        monkeypatch.setattr(online_check, name, lambda config, *rest, _name=name: Result(_name, "ok", "fine"))
    # Everything on.
    settings = default_settings
    settings.storage.bucket, settings.jobs.storage, settings.outsource.provider = "audio", "s3", "runpod"
    settings.billing.provider, settings.database.backups = "google_play", "provider"
    settings.tunnel.compose_profiles = "cloudflare,admin"
    settings.admin_web.mode = "read-only"
    settings.offsite.bucket, settings.error_report.url = "backups", "https://errors.example"

    for feature in FEATURES:
        results = online_check.run_checks(settings, [feature.key])
        tested = any(result.feature == feature.key and result.status != "skip" for result in results)
        assert tested == feature.online, feature.key


def test_a_managed_database_is_signed_in_to_with_tls(default_settings):
    class Connection:
        def __init__(self, encrypted):
            self.pgconn = type("PG", (), {"ssl_in_use": encrypted})()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, query):
            pass

    default_settings.database.url = "postgresql://billing:secret@db.example.com/billing"
    seen = []

    def connect(url, connect_timeout):
        seen.append(url)
        return Connection(True)

    assert online_check.check_database(default_settings, connect).status == "ok"
    assert online_check.check_database(default_settings, lambda url, connect_timeout: Connection(False)).status == "warn"

    def refuse(url, connect_timeout):
        raise OSError(f"cannot connect to {url}")

    failed = online_check.check_database(default_settings, refuse)
    assert failed.status == "fail" and "secret" not in failed.detail and "db.example.com" in failed.detail


def test_a_secret_file_that_is_no_utf8_is_named_instead_of_a_traceback(files):
    (files / "secrets" / "metrics_token").write_bytes("token".encode("utf-16"))

    with pytest.raises(config_module.ConfigError) as raised:
        config_module.load_settings([str(files / "server.env")], str(files / "secrets"))

    assert raised.value.problems[0].startswith("The secret file metrics_token cannot be read: it must be UTF-8 text")
    assert report(files)["errors"][0]["names"] == ["metrics_token"]


def test_a_session_ends_at_once_when_it_cannot_read_the_files(files, monkeypatch):
    """Then setup asks each question in a container Compose gives the files."""
    monkeypatch.setattr(os, "access", lambda path, mode: not str(path).endswith("server.env"))
    answers = io.StringIO()

    assert diagnosis.serve(str(files), config_module.main, ask(["features", "--json"]), answers) == 3
    assert answers.getvalue() == ""


def test_an_unknown_feature_is_named_with_the_known_ones(files, capsys):
    arguments = ["check", "--online", "--feature", "storage", "--env-file", str(files / "server.env")]

    assert config_module.main(arguments) == 2
    assert "unknown feature 'storage'" in capsys.readouterr().err
