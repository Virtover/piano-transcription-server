import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app import config as config_module
from app import online_check
from app.config import load_settings
from app.online_check import Result
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
    shutil.copyfile(ROOT / "server.env.example", tmp_path / "server.env")
    (tmp_path / "secrets").mkdir()

    return tmp_path, answering(tmp_path)


def answering(root):
    """python -m app.config, as the session in a container answers it, on
    root's files."""
    def config(*arguments, capture=True):
        files = ["--env-file", str(root / ".env"), "--env-file", str(root / "server.env"),
                 "--secrets-dir", str(root / "secrets")]
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                code = config_module.main([*arguments, *files])
            except SystemExit as error:
                code = error.code if isinstance(error.code, int) else 1
        return subprocess.CompletedProcess(arguments, code, output.getvalue(), errors.getvalue())

    return config


@pytest.fixture(autouse=True)
def no_services(monkeypatch):
    """The online checks answer what a test gives; none reaches a service."""
    monkeypatch.setattr(online_check, "run_checks", lambda config, features=None: [])


def make(server, answers=(), hidden=(), running=False, restart=lambda: 0):
    root, config = server
    script = Script(answers, hidden)
    console = setup_script.Console(ask=script.ask, ask_hidden=script.ask_hidden, say=script.say)
    setup = setup_script.Setup(console=console, root=root, config=config,
                               descriptions=setup_script.setting_descriptions(), running=lambda: running,
                               restart=restart)
    return setup, script


def env(server):
    """The server's settings."""
    return setup_script.EnvFile(server[0] / "server.env")


def compose_env(server):
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
    before = (server[0] / "server.env").read_text(encoding="utf-8")
    setup, script = make(server, answers=["", "audio", "https://storage.example", "", "n"], hidden=["", ""])

    assert setup.set_up("object_storage") is False

    # Turned back off: .env as it was before.
    assert (server[0] / "server.env").read_text(encoding="utf-8") == before
    assert "Object storage is off again" in script.text

    # Each missing secret file by its place on the host, not in the container.
    assert "! STORAGE_BUCKET needs the secret file secrets/storage_access_key" in " ".join(script.text.split())
    assert "/run/secrets" not in script.text
    assert "python scripts/setup.py object_storage" in script.text


def test_a_required_setting_cannot_be_skipped(server):
    setup, script = make(server, answers=["", "", "audio", "https://storage.example", "", ""], hidden=["a", "b"])
    # The bucket turns the feature on, so it is asked for until given.

    assert setup.set_up("object_storage") is True
    assert script.said.count("  It is needed; give a value.") == 1


def test_a_failed_setup_keeps_the_secrets_given_but_not_a_switch_secret(server):
    secrets_dir = server[0] / "secrets"
    setup, _ = make(server, answers=["", "audio", "https://storage.example", "", "n"], hidden=["access-key", ""])

    setup.set_up("object_storage")

    assert (secrets_dir / "storage_access_key").read_text(encoding="utf-8") == "access-key"

    # A password too short: its file alone would turn the feature on.
    setup, script = make(server, answers=["n", "n"], hidden=["short"])
    assert setup.set_up("password") is False
    assert not (secrets_dir / "server_password").exists()
    text = " ".join(script.text.split())
    assert "must hold at least 12 characters" in text
    assert "secrets/server_password is removed" in text and "other secret files" not in text


def test_reconfiguring_a_feature_that_then_fails_keeps_its_previous_settings(server):
    file = env(server)
    file.set("STORAGE_BUCKET", "audio")
    file.set("STORAGE_ENDPOINT", "https://storage.example")
    file.save()
    for name in ("storage_access_key", "storage_secret_key"):
        (server[0] / "secrets" / name).write_text("key", encoding="utf-8")
    before = (server[0] / "server.env").read_text(encoding="utf-8")
    # A new bucket, then the secret files deleted meanwhile: not ready.
    setup, script = make(server, answers=["", "other-bucket", "", "", "n", "n"], hidden=["", ""])
    original = setup.ask_secrets

    def lose_the_keys(feature, heading):
        for name in ("storage_access_key", "storage_secret_key"):
            (server[0] / "secrets" / name).unlink()
        feature = {**feature, "secrets": [{**secret, "present": False} for secret in feature["secrets"]]}
        original(feature, heading)

    setup.ask_secrets = lose_the_keys

    assert setup.set_up("object_storage") is False
    assert (server[0] / "server.env").read_text(encoding="utf-8") == before
    assert "keeps its previous settings" in script.text


def test_stopping_with_ctrl_c_turns_it_back_off(server):
    before = (server[0] / "server.env").read_text(encoding="utf-8")
    setup, _ = make(server, answers=["", "audio", "https://storage.example", ""])

    def interrupted(prompt):
        raise KeyboardInterrupt

    setup.console.ask_hidden = interrupted

    with pytest.raises(KeyboardInterrupt):
        setup.set_up("object_storage")
    assert (server[0] / "server.env").read_text(encoding="utf-8") == before


def test_stopping_at_the_external_services_changes_nothing(server):
    before = (server[0] / "server.env").read_text(encoding="utf-8")
    setup, _ = make(server, answers=["q"])

    assert setup.set_up("object_storage") is False
    assert (server[0] / "server.env").read_text(encoding="utf-8") == before


def test_a_generated_password(server):
    setup, script = make(server, answers=["y"])

    assert setup.set_up("password") is True

    password = (server[0] / "secrets" / "server_password").read_text(encoding="utf-8")
    assert len(password) >= 32 and password not in script.text


def test_the_admin_page_offers_only_modes_that_turn_it_on(server):
    setup, script = make(
        server,
        answers=[
            "y", "", "",                         # the tunnel first: its services, no hostname
            "", "read-write", "admin.example.com", '["me@example.com"]', "team.cloudflareaccess.com", "aud",
            # Both are tested once the server runs with them, so not here.
        ],
        hidden=["tunnel-token"],
    )

    setup.set_up("admin_web")

    file = env(server)
    # The page's settings in server.env; what Compose reads in .env.
    assert file.get("ADMIN_WEB_HOSTNAME") == "admin.example.com"
    assert compose_env(server).get("ADMIN_WEB_MODE") == "read-write"
    assert compose_env(server).get("COMPOSE_PROFILES") == "cloudflare,admin"
    assert file.get("COMPOSE_PROFILES") is None and file.get("ADMIN_WEB_MODE") is None
    assert "One of: read-only, read-write" in script.text


def test_a_file_secret_is_copied(server, tmp_path_factory):
    download = tmp_path_factory.mktemp("downloads") / "key.json"
    download.write_text('{"type": "service_account"}', encoding="utf-8")
    setup, _ = make(server, answers=["", "com.example.app", '["id.apps.googleusercontent.com"]', "", "", "", "", "", "", "", "",
                                     str(download), ""])

    setup.set_up("billing")

    copied = server[0] / "secrets" / "google_play_service_account.json"
    assert json.loads(copied.read_text(encoding="utf-8")) == {"type": "service_account"}
    assert env(server).get("BILLING_PROVIDER") == "google_play"


def test_an_excluded_feature_is_turned_off_first(server):
    file = env(server)
    file.set("DATABASE_BACKUPS", "provider")
    file.save()
    setup, script = make(server, answers=["y", "q"])

    setup.set_up("offsite")

    assert "cannot be on together with Backups by the database's provider" in script.text
    assert env(server).get("DATABASE_BACKUPS") == "server"


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

    line = next(line for line in script.said if "Job files in object storage" in line)
    assert "job_storage" in line and "on, 1 missing" in line
    assert any(line.strip().startswith("! JOB_STORAGE=s3 needs object storage") for line in script.said)
    ready = next(line for line in script.said if "Password protection" in line)
    assert " off " in ready and "missing" not in ready


def test_status_names_its_columns_and_explains_them(server):
    setup, script = make(server)

    setup.status()

    header = next(line for line in script.said if "Feature" in line)
    assert header.split() == ["#", "Feature", "Name", "Status", "Home", "cluster", "On", "Kubernetes"]
    assert any(line.strip().startswith("Status") and "server.env" in line for line in script.said)
    assert any(line.strip().startswith("Home cluster") and "required:" in line for line in script.said)
    assert any(line.strip().startswith("On Kubernetes") and "required:" in line for line in script.said)


def test_status_says_what_a_home_cluster_needs(server):
    setup, script = make(server)

    setup.status()

    # A home cluster needs no bucket: the hub keeps job files in its own.
    job_storage = next(line for line in script.said if "job_storage" in line and "Job files" in line)
    assert "optional" in job_storage
    provider = next(line for line in script.said if "provider_backups" in line and "provider" in line.lower())
    assert "not used" in provider


def test_status_says_what_kubernetes_needs(server):
    setup, script = make(server)

    setup.status()

    metrics = next(line for line in script.said if "Metrics for scaling" in line)
    offsite = next(line for line in script.said if "Off-site backups" in line)
    billing = next(line for line in script.said if "Billing with Google Play" in line)
    assert "required" in metrics and "optional" in billing
    # Off here, and required there: marked, with the reason below the table.
    provider = next(line for line in script.said if "Backups by the database's provider" in line)
    assert provider.endswith("required *")
    reason = next(line for line in script.said if line.strip().startswith("provider_backups "))
    assert "wiring.yaml" in reason


def test_status_explains_a_feature_kubernetes_does_not_use(server):
    file = env(server)
    file.set("OFFSITE_BACKUP_BUCKET", "backups")
    file.save()
    setup, script = make(server)

    setup.status()

    offsite = next(line for line in script.said if "Off-site backups" in line)
    assert offsite.endswith("not used *")
    after = script.said[next(i for i, line in enumerate(script.said) if line.strip().startswith("offsite ")):]
    assert "Kubernetes leaves to the provider" in " ".join(" ".join(after[:2]).split())


def test_a_missing_setting_is_named(server):
    file = env(server)
    file.set("OUTSOURCE_PROVIDER", "runpod")
    file.save()
    setup, script = make(server)

    setup.status()

    assert any("missing setting" in line and "RUNPOD_ENDPOINT_ID" in line for line in script.said)
    # Said once, as a missing setting, not again as a problem.
    assert not any("RUNPOD_ENDPOINT_ID is required" in line for line in script.said)


def test_a_secret_with_an_alternative_is_missing_only_when_the_server_says_so():
    feature = {
        "enabled": True, "settings": [], "problems": [],
        "secrets": [{"name": "google_play_service_account.json", "description": "", "optional": False,
                     "checked": False, "present": False}],
    }

    assert setup_script.Missing.of(feature).count == 0

    feature["problems"] = ["needs a Google Play service account: the secret file "
                           "/run/secrets/google_play_service_account.json is missing"]
    missing = setup_script.Missing.of(feature)
    assert [secret["name"] for secret in missing.secrets] == ["google_play_service_account.json"]
    assert missing.problems == []


# --- Colors ---------------------------------------------------------------------------------------

def test_no_colors_unless_turned_on():
    assert setup_script.Style()("text", "red") == "text"
    assert setup_script.Style(True)("text", "red", "bold") == "[31;1mtext[0m"


def test_no_colors_with_no_color_or_without_a_terminal(monkeypatch):
    class Terminal:
        def isatty(self):
            return True

    monkeypatch.setenv("NO_COLOR", "1")
    assert not setup_script.Style.for_terminal(Terminal()).enabled
    monkeypatch.delenv("NO_COLOR")
    assert not setup_script.Style.for_terminal(object()).enabled


def test_colors_do_not_count_as_width():
    said = []
    console = setup_script.Console(say=said.append, style=setup_script.Style(True))

    console.wrap("word " * 30, "  ", "dim")

    plain = [line.replace("[2m", "").replace("[0m", "") for line in said]
    assert all(len(line) <= setup_script.WIDTH for line in plain)
    assert len(said) == 2


def test_container_paths_are_shown_as_on_the_host():
    assert setup_script.host_paths("needs the secret file /run/secrets/runpod_api_key") == (
        "needs the secret file secrets/runpod_api_key"
    )


def test_an_unknown_feature_lists_the_known_ones(server):
    setup, script = make(server)

    assert setup.set_up("nonsense") is False
    assert "outsourcing" in script.text


def test_the_command_line(server):
    setup, _ = make(server)

    assert setup_script.main(["status"], setup) == 0
    with pytest.raises(SystemExit):
        setup_script.main(["off"], setup)


# --- A home cluster -----------------------------------------------------------------------

def no_tailscale(monkeypatch):
    real = shutil.which
    monkeypatch.setattr(setup_script.shutil, "which", lambda name: None if name == "tailscale" else real(name))


def with_tailscale(monkeypatch, address="100.101.102.103"):
    real = shutil.which
    monkeypatch.setattr(setup_script.shutil, "which", lambda name: f"/usr/bin/{name}" if name in ("tailscale", "docker") else real(name))
    return lambda command, **options: subprocess.CompletedProcess(command, 0, f"{address}\n" if command[:2] == ["tailscale", "ip"] else "", "")


def test_status_says_a_home_cluster_is_not_set_up(server, monkeypatch):
    no_tailscale(monkeypatch)
    setup, script = make(server)

    setup.status()

    assert "Home cluster" in script.text and "not set up" in script.text
    assert "python scripts/setup.py cluster" in script.text


def test_the_cluster_lists_what_a_hub_needs_and_how(server, monkeypatch):
    no_tailscale(monkeypatch)
    setup, script = make(server, answers=[])

    assert setup.cluster() == 0

    assert "Install Tailscale" in script.text
    assert "this machine is the hub" in script.text
    assert "python scripts/admin.py nodes invite <name>" in script.text


def test_the_cluster_offers_to_make_this_machine_the_hub(server, monkeypatch):
    root, _ = server
    run = with_tailscale(monkeypatch)
    ran = []

    def runner(command, **options):
        ran.append(command)
        return run(command)

    setup, script = make(server, answers=["y"])
    setup.run = runner

    assert setup.cluster() == 0

    [hub] = [command for command in ran if "nodes" in command]
    assert hub[-2:] == ["nodes", "hub"]
    assert "Tailscale, signed in: 100.101.102.103" in script.text


def test_a_hub_whose_address_is_not_tailscales_is_told(server, monkeypatch):
    root, _ = server
    with (root / ".env").open("a", encoding="utf-8") as stream:
        stream.write("\nHUB_ADDRESS=100.1.1.1\nCOMPOSE_FILE=docker-compose.yml:docker-compose.hub.yml\n")
    run = with_tailscale(monkeypatch)

    checks = setup_script.cluster_checks(root, None, run, platform="linux")

    by_text = {check.text: check for check in checks}
    assert by_text["this machine is the hub, on 100.1.1.1"].ok
    assert not by_text["HUB_ADDRESS 100.1.1.1 is this machine's Tailscale address"].ok
    assert "nodes hub" in by_text["HUB_ADDRESS 100.1.1.1 is this machine's Tailscale address"].fix
    assert not by_text["devices invited"].ok


def test_on_a_node_setup_shows_the_nodes_side_and_creates_no_hub_files(tmp_path, monkeypatch):
    no_tailscale(monkeypatch)
    (tmp_path / "node.env").write_text("NODE_NAME=laptop\nHUB_ADDRESS=127.0.0.9\nNODE_PROCESSOR=cpu\n", encoding="utf-8")
    monkeypatch.setattr(setup_script.NODES, "reachable", lambda host, port, timeout=5: False)
    script = Script()
    console = setup_script.Console(ask=script.ask, ask_hidden=script.ask_hidden, say=script.say)
    setup = setup_script.Setup(console=console, root=tmp_path,
                               config=lambda *arguments, **options: pytest.fail("the hub's configuration asked on a node"))

    assert setup_script.main(["status"], setup) == 1

    assert "this device is a node" in script.text
    assert "joined as the node laptop" in script.text
    assert "the hub's Redis answers at 127.0.0.9:6379" in script.text
    assert not (tmp_path / ".env").exists() and not (tmp_path / "billing-data").exists()
    assert setup_script.main(["outsourcing"], setup) == 1

# --- Values checked as they are typed -------------------------------------------------------

def test_a_value_the_server_would_refuse_is_asked_again(server):
    setup, script = make(server, answers=["30", "30s"])

    assert setup.change("WORKER_BATCH_TIMEOUT") == 0

    assert env(server).get("WORKER_BATCH_TIMEOUT") == "30s"
    assert "! the value must use a format such as 30s" in script.text


def test_any_setting_is_found_by_part_of_its_name(server):
    setup, script = make(server, answers=["batch_timeout", "2m"])

    assert setup.change() == 0

    assert env(server).get("WORKER_BATCH_TIMEOUT") == "2m"


def test_a_change_that_stops_the_server_is_undone(server):
    # Typed in rather than generated.
    setup, script = make(server, answers=["n", "y"], hidden=["short"])

    assert setup.change("server_password") == 1

    assert not (server[0] / "secrets" / "server_password").exists()
    assert "must hold at least 12 characters" in " ".join(script.text.split())


def test_an_unknown_setting_suggests_the_known_one(server):
    setup, script = make(server)

    assert setup.change("WORKER_BATCH_TIMOUT") == 1
    assert "Did you mean WORKER_BATCH_TIMEOUT" in script.text


def test_a_kubernetes_choice_is_checked_as_kube_py_checks_it(server):
    root, _ = server
    (root / "deploy" / "kubernetes").mkdir(parents=True)
    shutil.copyfile(ROOT / "deploy" / "kubernetes" / "cluster.env.example",
                    root / "deploy" / "kubernetes" / "cluster.env.example")
    setup, script = make(server, answers=["many", "3"])

    assert setup.change("KUBE_MAX_WORKERS") == 0

    assert setup_script.EnvFile(root / "deploy" / "kubernetes" / "cluster.env").get("KUBE_MAX_WORKERS") == "3"
    assert "KUBE_MAX_WORKERS must be a whole number" in script.text


# --- Testing with the real services, and fixing what fails ----------------------------------

def failing_storage_once(monkeypatch):
    """The storage test fails until the access key is written again."""
    calls = []

    def checks(config, features=None):
        calls.append(features)
        if len(calls) == 1:
            return [Result("Storage", "fail", "Upload: InvalidAccessKeyId",
                           "The access key ID is wrong. Rewrite secrets/storage_access_key.", feature="object_storage")]
        return [Result("Storage", "ok", "upload, signed download, and delete work", feature="object_storage")]

    monkeypatch.setattr(online_check, "run_checks", checks)
    return calls


def test_a_failed_test_asks_again_for_what_its_hint_names(server, monkeypatch):
    calls = failing_storage_once(monkeypatch)
    setup, script = make(server, answers=["", "audio", "https://storage.example", "", "", ""],
                         hidden=["wrong", "secret", "right"])

    assert setup.set_up("object_storage") is True

    assert calls == [["object_storage"], ["object_storage"]]
    assert (server[0] / "secrets" / "storage_access_key").read_text(encoding="utf-8") == "right"
    assert "To change: storage_access_key" in script.text
    assert "The services answer as they should." in script.text


def test_a_failed_test_can_undo_the_setup(server, monkeypatch):
    failing_storage_once(monkeypatch)
    before = (server[0] / "server.env").read_text(encoding="utf-8")
    setup, script = make(server, answers=["", "audio", "https://storage.example", "", "", "u"],
                         hidden=["wrong", "secret"])

    assert setup.set_up("object_storage") is False
    assert (server[0] / "server.env").read_text(encoding="utf-8") == before


def test_check_tests_a_feature_that_is_on(server, monkeypatch):
    calls = failing_storage_once(monkeypatch)
    file = env(server)
    file.set("STORAGE_BUCKET", "audio")
    file.set("STORAGE_ENDPOINT", "https://storage.example")
    file.save()
    for name in ("storage_access_key", "storage_secret_key"):
        (server[0] / "secrets" / name).write_text("key", encoding="utf-8")
    setup, script = make(server, answers=["t"])

    assert setup.check("object_storage") == 0
    assert len(calls) == 2


def test_a_feature_whose_service_starts_with_the_server_is_tested_after_a_restart(server, monkeypatch):
    restarts, calls = [], []

    def checks(config, features=None):
        calls.append(features)
        return [Result("Cloudflare Tunnel", "ok", "connected", feature="tunnel")]

    monkeypatch.setattr(online_check, "run_checks", checks)
    setup, script = make(server, answers=["", "", "y"], hidden=["token"], running=True,
                         restart=lambda: restarts.append(1) or 0)

    assert setup.set_up("tunnel") is True

    assert restarts == [1] and calls == [["tunnel"]]


# --- python scripts/setup.py fix ------------------------------------------------------------

def test_fix_repairs_what_editors_and_shells_leave_in_the_files(server):
    root, _ = server
    server_env = root / "server.env"
    server_env.write_bytes(b"\xef\xbb\xbf" + server_env.read_bytes()
                           + b"\nJOB_DIAGNOSTICS_RETENTON=9d\nnot a setting\nWORKER_BATCH_TIMEOUT=1m\nWORKER_BATCH_TIMEOUT=2m\n")
    secrets_dir = root / "secrets"
    # PowerShell 5.1's > writes UTF-16 with a byte order mark; Notepad adds .txt.
    (secrets_dir / "metrics_token").write_bytes("token\r\n".encode("utf-16"))
    (secrets_dir / "error_report_token.txt").write_text("report-token", encoding="utf-8")
    (secrets_dir / "server_password").mkdir()
    setup, script = make(server, answers=["y", "y", "y", "y", "n"])

    assert setup.fix() == 0

    raw = server_env.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    file = env(server)
    assert file.get("WORKER_BATCH_TIMEOUT") == "2m" and file.get("JOB_DIAGNOSTICS_RETENTION") == "9d"
    assert file.get("JOB_DIAGNOSTICS_RETENTON") is None
    assert "# not a setting" in raw.decode("utf-8")
    assert (secrets_dir / "metrics_token").read_bytes() == b"token"
    assert (secrets_dir / "error_report_token").read_text(encoding="utf-8") == "report-token"
    assert not (secrets_dir / "server_password").exists()
    # Never a secret's value on screen.
    assert "report-token" not in script.text and "token\r" not in script.text


def test_fix_asks_for_what_keeps_the_server_from_starting(server):
    file = env(server)
    file.set("WORKER_BATCH_TIMEOUT", "soon")
    file.save()
    setup, script = make(server, answers=["", "45s", "n"])

    assert setup.fix() == 0

    assert env(server).get("WORKER_BATCH_TIMEOUT") == "45s"


def test_status_says_how_to_fix_a_configuration_it_cannot_read(server):
    file = env(server)
    file.set("WORKER_BATCH_TIMEOUT", "soon")
    file.save()
    setup, script = make(server)

    assert setup.status() == 1
    assert "python scripts/setup.py fix" in script.text and "WORKER_BATCH_TIMEOUT" in script.text


def test_line_problems_skip_values_over_several_lines():
    broken, duplicates = setup_script.env_line_problems(
        ["A=1", "B='first", "still B", "end'", "oops", "A=2", "# C=3"])

    assert broken == [4]
    assert duplicates == {"A": [0, 5]}


def test_a_secret_with_one_line_end_after_it_is_fine():
    assert setup_script.decoded_secret(b"key\n") == ("key", [])
    assert setup_script.decoded_secret(b"\xef\xbb\xbfkey")[1] == ["a byte order mark (BOM)"]
    assert setup_script.decoded_secret(b"key\r\n")[1] == ["Windows line ends"]
    assert setup_script.decoded_secret(b"\xff\xfe\x00\xd8")[0] is None


# --- One container for the whole run ---------------------------------------------------------

class FakeSession:
    def __init__(self, answers):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(json.dumps(answer) + "\n" for answer in answers))

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def test_the_session_answers_and_falls_back_when_it_ends():
    stack = type("Stack", (), {})()
    started, fallback = [], []
    stack.config_session = lambda: started.append(1) or FakeSession([{"returncode": 0, "stdout": "{}", "stderr": ""}])

    def one_off(*arguments, capture=True):
        fallback.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "fallback", "")

    session = setup_script.ConfigSession(stack, one_off)

    assert session("features", "--json").stdout == "{}"
    # The fake has no second answer, as when the container stops.
    assert session("features", "--json").stdout == "fallback"
    assert session("check").stdout == "fallback"
    assert started == [1] and fallback == [("features", "--json"), ("check",)]


def test_the_session_mounts_the_checkout_read_only(tmp_path):
    ran = []
    stack = setup_script.SERVER_SCRIPT.ComposeStack([], popen=lambda command, **options: ran.append((command, options)),
                                                    root=tmp_path)

    stack.config_session()

    command, options = ran[0]
    assert command[:3] == ["docker", "compose", "run"]
    assert f"{tmp_path}:/setup:ro" in command
    assert command[-4:] == ["app.config", "session", "--root", "/setup"]
    assert options["stdin"] is subprocess.PIPE


def test_status_says_when_docker_compose_cannot_read_the_files(server):
    def refused(*arguments, capture=True):
        return subprocess.CompletedProcess(arguments, 1, "", "failed to read server.env: line 3: key cannot contain a space\n")

    setup, script = make(server)
    setup.config = refused

    assert setup.status() == 1
    assert "key cannot contain a space" in script.text and "python scripts/setup.py fix" in script.text


def test_a_wrong_value_in_the_file_is_not_kept_by_enter(server):
    file = env(server)
    file.set("WORKER_BATCH_TIMEOUT", "soon")
    file.save()
    setup, script = make(server, answers=["", "1m"])

    assert setup.change("WORKER_BATCH_TIMEOUT") == 0
    assert env(server).get("WORKER_BATCH_TIMEOUT") == "1m"


# --- The menu --------------------------------------------------------------------------------

def test_the_menu_keeps_a_result_on_screen_until_enter(server, monkeypatch):
    calls = []
    monkeypatch.setattr(online_check, "run_checks",
                        lambda config, features=None: calls.append(1) or [Result("Redis users", "ok", "refused")])
    setup, script = make(server, answers=["c", "", ""])

    assert setup.menu() == 0

    said = script.said
    result = next(index for index, line in enumerate(said) if "The services answer as they should." in line)
    # Enter, then the menu again: nothing printed between the result and the pause.
    assert "Press Enter to go back to the menu" in said[result + 1]
    assert calls == [1]
    # The legend and the commands once, at the start, not after each choice.
    assert script.text.count("Status         on or off") == 1
    assert "python scripts/setup.py <name>" not in script.text


def test_the_menu_says_what_it_did_not_understand(server):
    setup, script = make(server, answers=["x", "99", ""])

    assert setup.menu() == 0

    assert "'x' is none of these." in script.text and "'99' is none of these." in script.text
    # Asked again without drawing the features once more.
    assert script.text.count("Optional features") == 1


def test_a_feature_that_is_on_can_be_left_from_the_menu(server):
    setup, script = make(server, answers=["10", "", ""])

    assert setup.menu() == 0
    assert "Piano arrangements is on." in script.text
    assert "Press Enter to go back" not in script.text
