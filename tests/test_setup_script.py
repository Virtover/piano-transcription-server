import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import load_settings
from app.features import describe_features
from app.requirements import build_requirements, requirements_json


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "setup.py"
spec = importlib.util.spec_from_file_location("setup_script", SCRIPT)
setup_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup_script)


class Script:
    """Answers, in order, and what was said."""

    def __init__(self, answers=(), hidden=()):
        self.answers = list(answers)
        self.hidden = list(hidden)
        self.said = []

    def ask(self, prompt):
        self.said.append(prompt)
        assert self.answers, f"no answer left for: {prompt}"
        return self.answers.pop(0)

    def ask_hidden(self, prompt):
        self.said.append(prompt)
        assert self.hidden, f"no hidden answer left for: {prompt}"
        return self.hidden.pop(0)

    def say(self, text):
        self.said.append(text)

    @property
    def text(self):
        return "\n".join(self.said)


@pytest.fixture
def server(tmp_path):
    """A server folder with .env from .env.example, and the configuration
    commands answered by this code instead of a container."""
    shutil.copyfile(ROOT / ".env.example", tmp_path / ".env")
    (tmp_path / "secrets").mkdir()

    def config(*arguments, capture=True):
        settings = load_settings(str(tmp_path / ".env"), str(tmp_path / "secrets"))
        if arguments[0] == "features":
            output = json.dumps(describe_features(settings), default=str)
        elif arguments[0] == "requirements":
            output = json.dumps(requirements_json(build_requirements(settings)), default=str)
        else:
            output = ""
        return subprocess.CompletedProcess(arguments, 0, output, "")

    return tmp_path, config


def make(server, answers=(), hidden=()):
    root, config = server
    script = Script(answers, hidden)
    console = setup_script.Console(ask=script.ask, ask_hidden=script.ask_hidden, say=script.say)
    setup = setup_script.Setup(console=console, root=root, config=config,
                               descriptions=setup_script.setting_descriptions())
    return setup, script


def env(server):
    return setup_script.EnvFile(server[0] / ".env")


# --- .env editing -------------------------------------------------------------------------

def test_a_setting_replaces_its_commented_example_in_its_block(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# --- Outsourcing\n# OUTSOURCE_PROVIDER=runpod\n# RUNPOD_ENDPOINT_ID=\nOTHER=1\n", encoding="utf-8")
    file = setup_script.EnvFile(path)

    file.set("RUNPOD_ENDPOINT_ID", "abc")
    file.set("OTHER", "2")
    file.set("NEW_SETTING", "x")
    file.save()

    assert path.read_text(encoding="utf-8").splitlines() == [
        "# --- Outsourcing", "# OUTSOURCE_PROVIDER=runpod", "RUNPOD_ENDPOINT_ID=abc", "OTHER=2",
        "", setup_script.ADDED_HEADER, "NEW_SETTING=x",
    ]


def test_unsetting_comments_the_line_out(tmp_path):
    path = tmp_path / ".env"
    path.write_text("STORAGE_BUCKET=audio\n", encoding="utf-8")
    file = setup_script.EnvFile(path)

    file.unset("STORAGE_BUCKET")

    assert file.get("STORAGE_BUCKET") is None
    assert file.lines == ["# STORAGE_BUCKET=audio"]


def test_profiles_are_added_and_removed(tmp_path):
    file = setup_script.EnvFile(tmp_path / ".env")

    file.add_profile("cloudflare")
    file.add_profile("admin")
    file.add_profile("admin")
    assert file.get("COMPOSE_PROFILES") == "cloudflare,admin"

    file.remove_profile("cloudflare")
    assert file.get("COMPOSE_PROFILES") == "admin"
    file.remove_profile("admin")
    assert file.get("COMPOSE_PROFILES") is None


def test_values_that_dotenv_would_cut_are_quoted(tmp_path):
    file = setup_script.EnvFile(tmp_path / ".env")

    file.set("FREE_MINUTES_PERIOD", "30d #comment")

    assert file.lines[-1] == "FREE_MINUTES_PERIOD='30d #comment'"
    assert file.get("FREE_MINUTES_PERIOD") == "30d #comment"


def test_descriptions_come_from_the_docs():
    descriptions = setup_script.setting_descriptions()

    assert "RunPod Serverless endpoint" in descriptions["RUNPOD_ENDPOINT_ID"]
    assert "`" not in descriptions["JOB_STORAGE"] and "](" not in descriptions["JOB_STORAGE"]


# --- Setting up -------------------------------------------------------------------------------

def test_outsourcing_sets_up_object_storage_first(server):
    setup, script = make(
        server,
        answers=[
            "y",                       # set up object storage first
            "",                        # its services are ready
            "audio", "https://account.eu.r2.cloudflarestorage.com", "",   # bucket, endpoint, region
            "",                        # no online test
            "",                        # outsourcing's services are ready
            "endpoint-1", "", "", "", "", "",   # endpoint ID, then the defaults
            "",                        # no online test
        ],
        hidden=["access", "secret", "runpod-key", ""],
    )

    assert setup.set_up("outsourcing") is True

    file = env(server)
    assert file.get("STORAGE_BUCKET") == "audio"
    assert file.get("OUTSOURCE_PROVIDER") == "runpod"
    assert file.get("RUNPOD_ENDPOINT_ID") == "endpoint-1"
    # Defaults kept are not written.
    assert file.get("OUTSOURCE_TIMEOUT") is None
    secrets_dir = server[0] / "secrets"
    assert (secrets_dir / "storage_access_key").read_text(encoding="utf-8") == "access"
    assert (secrets_dir / "runpod_api_key").read_text(encoding="utf-8") == "runpod-key"
    assert not (secrets_dir / "runpod_read_api_key").exists()
    # The providers' needs, as intents, and no secret in what was said.
    assert "Bucket" in script.text and "why:" in script.text
    assert "runpod-key" not in script.text and "secret" not in [line.strip() for line in script.said]
    assert "Outsourcing to RunPod is set up." in script.text


def test_an_unfinished_setup_says_what_is_missing(server):
    setup, script = make(server, answers=["", "audio", "https://storage.example", ""], hidden=["", ""])

    assert setup.set_up("object_storage") is False

    assert "STORAGE_BUCKET needs the secret file" in script.text
    assert "python scripts/setup.py object_storage" in script.text


def test_a_required_setting_cannot_be_skipped(server):
    setup, script = make(server, answers=["", "", "audio", "https://storage.example", "", ""], hidden=["a", "b"])
    # The bucket turns the feature on, so it is asked for until given.

    assert setup.set_up("object_storage") is True
    assert script.said.count("  It is needed; give a value.") == 1


def test_stopping_at_the_external_services_changes_nothing(server):
    before = (server[0] / ".env").read_text(encoding="utf-8")
    setup, _ = make(server, answers=["q"])

    assert setup.set_up("object_storage") is False
    assert (server[0] / ".env").read_text(encoding="utf-8") == before


def test_a_generated_password(server):
    setup, script = make(server, answers=["y"])

    assert setup.set_up("password") is True

    password = (server[0] / "secrets" / "server_password").read_text(encoding="utf-8")
    assert len(password) >= 32 and password not in script.text


def test_the_admin_page_offers_only_modes_that_turn_it_on(server):
    setup, script = make(
        server,
        answers=[
            "y", "", "", "",                     # the tunnel first: its services, no hostname, no online test
            "", "read-write", "admin.example.com", '["me@example.com"]', "team.cloudflareaccess.com", "aud",
            "",                                  # no online test
        ],
        hidden=["tunnel-token"],
    )

    setup.set_up("admin_web")

    file = env(server)
    assert file.get("ADMIN_WEB_MODE") == "read-write"
    assert file.get("COMPOSE_PROFILES") == "cloudflare,admin"
    assert "One of: read-only, read-write" in script.text


def test_a_file_secret_is_copied(server, tmp_path_factory):
    download = tmp_path_factory.mktemp("downloads") / "key.json"
    download.write_text('{"type": "service_account"}', encoding="utf-8")
    setup, _ = make(server, answers=["", "com.example.app", '["id.apps.googleusercontent.com"]', "", "", "", "",
                                     str(download), ""])

    setup.set_up("billing")

    copied = server[0] / "secrets" / "google_play_service_account.json"
    assert json.loads(copied.read_text(encoding="utf-8")) == {"type": "service_account"}
    assert env(server).get("BILLING_PROVIDER") == "google_play"


def test_an_excluded_feature_is_turned_off_first(server):
    file = env(server)
    file.set("BILLING_BACKUPS", "provider")
    file.save()
    setup, script = make(server, answers=["y", "q"])

    setup.set_up("offsite")

    assert "cannot be on together with Backups by the database's provider" in script.text
    assert env(server).get("BILLING_BACKUPS") == "server"


# --- Turning off ----------------------------------------------------------------------------------

def test_a_feature_that_others_need_stays_on(server):
    file = env(server)
    file.set("STORAGE_BUCKET", "audio")
    file.set("JOB_STORAGE", "s3")
    file.save()
    setup, script = make(server)

    assert setup.turn_off("object_storage") is False
    assert "Job files in object storage needs it" in script.text


def test_turning_off_sets_the_off_value(server):
    file = env(server)
    file.set("JOB_STORAGE", "s3")
    file.save()
    setup, _ = make(server, answers=["y"])

    assert setup.turn_off("job_storage") is True
    assert env(server).get("JOB_STORAGE") == "local"


def test_turning_off_a_secret_switch_deletes_the_secret_only_when_asked(server):
    token = server[0] / "secrets" / "metrics_token"
    token.write_text("x" * 32, encoding="utf-8")
    setup, script = make(server, answers=["y", "n"])

    setup.turn_off("metrics")
    assert token.exists() and "move the file away" in script.text

    setup, _ = make(server, answers=["y", "y"])
    setup.turn_off("metrics")
    assert not token.exists()


def test_status_lists_every_feature_and_its_problems(server):
    file = env(server)
    file.set("JOB_STORAGE", "s3")
    file.save()
    setup, script = make(server)

    setup.status()

    assert "on   Job files in object storage  [job_storage]" in script.text
    assert "! JOB_STORAGE=s3 needs object storage" in script.text


def test_an_unknown_feature_lists_the_known_ones(server):
    setup, script = make(server)

    assert setup.set_up("nonsense") is False
    assert "outsourcing" in script.text


def test_an_image_older_than_the_code_is_built_first(server):
    root, config = server
    calls = []

    def old_then_new(*arguments, capture=True):
        if not calls:
            calls.append("old")
            return subprocess.CompletedProcess(arguments, 2, "", "error: argument command: invalid choice: 'features'")
        return config(*arguments, capture=capture)

    script = Script()
    setup = setup_script.Setup(
        console=setup_script.Console(ask=script.ask, ask_hidden=script.ask_hidden, say=script.say),
        root=root, config=old_then_new, build=lambda: calls.append("built"),
    )

    setup.status()

    assert calls == ["old", "built"]
    assert "Optional features" in script.text


def test_the_command_line(server):
    setup, _ = make(server)

    assert setup_script.main(["status"], setup) == 0
    with pytest.raises(SystemExit):
        setup_script.main(["off"], setup)
