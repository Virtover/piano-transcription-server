import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from app import config


ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("kube_script", ROOT / "scripts" / "kube.py")
kube_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kube_script)


class Cluster:
    """kubectl and docker, answering from what a test sets, and recording
    every command and what was passed in."""

    def __init__(self, context="my-cluster"):
        self.context = context
        self.commands = []
        self.inputs = []
        self.answers = {}

    def __call__(self, command, input=None, capture=True):
        self.commands.append(command)
        if input is not None:
            self.inputs.append(input)
        text = " ".join(command)
        if text == "kubectl config current-context":
            return subprocess.CompletedProcess(command, 0 if self.context else 1, self.context or "", "")
        for fragment, (code, stdout) in self.answers.items():
            if fragment in text:
                return subprocess.CompletedProcess(command, code, stdout, "")
        if "--dry-run=client" in command:
            # One key per --from-file, as kubectl makes them.
            keys = [part.split("=")[1] for part in command if part.startswith("--from-file=")]
            return subprocess.CompletedProcess(command, 0, json.dumps({"kind": "Secret", "data": {
                key: "c2VjcmV0" for key in keys}}), "")
        if command[:2] == ["kubectl", "-n"] and "get" in command and "deployments" in command:
            items = [{"metadata": {"name": "api"}, "spec": {"replicas": 2, "template": {"spec": {"containers": [
                {"image": "piano-transcription-api:1"}]}}}, "status": {"readyReplicas": 2}}]
            return subprocess.CompletedProcess(command, 0, json.dumps({"items": items}), "")
        return subprocess.CompletedProcess(command, 0, "", "")

    def ran(self, *words) -> bool:
        return any(all(word in command for word in words) for command in self.commands)


class Script:
    def __init__(self, answers=(), hidden=()):
        self.answers, self.hidden, self.said = list(answers), list(hidden), []

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
def project(tmp_path):
    """A copy of the manifests and Redis's users in a temporary project."""
    import shutil

    manifests = tmp_path / "deploy" / "kubernetes"
    # Without the settings.env that kube.py wrote for this machine, which
    # is not committed.
    shutil.copytree(ROOT / "deploy" / "kubernetes", manifests, ignore=shutil.ignore_patterns("settings.env"))
    (tmp_path / "redis").mkdir()
    shutil.copyfile(ROOT / "redis" / "users.acl", tmp_path / "redis" / "users.acl")
    shutil.copyfile(ROOT / "server.env.example", tmp_path / "server.env")
    (tmp_path / "secrets").mkdir()
    return tmp_path, manifests, tmp_path / "secrets" / "kubernetes"


def make(project, target, answers=(), hidden=(), context="my-cluster"):
    root, manifests, _ = project
    cluster = Cluster(context)
    script = Script(answers, hidden)
    kube = kube_script.Kube(
        target=kube_script.Target(local=target == "local", manifests=manifests / "local" if target == "local" else manifests),
        console=kube_script.Console(ask=script.ask, ask_hidden=script.ask_hidden, say=script.say),
        run=cluster, root=root, clock=lambda: "20261030-120000",
    )
    return kube, cluster, script


def write_cluster_secrets(project):
    root, _, cluster_secrets = project
    for path in (cluster_secrets / "database_url", cluster_secrets / "redis_app_password",
                 root / "secrets" / "storage_access_key", root / "secrets" / "storage_secret_key",
                 root / "secrets" / "metrics_token", root / "secrets" / "cloudflare" / "tunnel_token"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("s3cret-value", encoding="utf-8")


# --- The test cluster ------------------------------------------------------------------------------

def test_local_up_runs_everything_after_one_question(project):
    kube, cluster, script = make(project, "local", answers=["y"], context="docker-desktop")
    committed = kube.target.text()

    assert kube.deploy(first=True) == 0

    # In order: images, namespace, Secrets, apply, rollout, bucket, check.
    order = [" ".join(command) for command in cluster.commands if command[0] in ("docker", "kubectl")]
    build = next(i for i, c in enumerate(order) if c.startswith("docker build -f Dockerfile.api"))
    apply = next(i for i, c in enumerate(order) if "apply -k" in c)
    secret = next(i for i, c in enumerate(order) if "create secret generic piano-secrets" in c)
    assert build < secret < apply
    assert cluster.ran("rollout", "status", "deploy/api")
    assert cluster.ran("exec", "deploy/api", "python", "-c") and cluster.ran("check", "--online")
    # Built here, never pushed, always under the tag the committed file names,
    # which stays unchanged; a restart brings the new images.
    assert not cluster.ran("docker", "push")
    assert cluster.ran("docker", "build", "-t", "piano-transcription-api:local")
    assert kube.target.kustomization.read_text(encoding="utf-8") == committed
    assert set(kube.target.images().values()) == {("piano-transcription-api", "local"),
                                                  ("piano-transcription-worker", "local")}
    assert cluster.ran("rollout", "restart", "deploy/api")
    assert "http://localhost:8080" in script.text


def test_local_build_brings_back_the_committed_tag(project):
    kube, cluster, _ = make(project, "local", context="docker-desktop")
    committed = kube.target.text()
    kube.target.kustomization.write_text(kube_script.set_images(committed, None, "20261030-120000"), encoding="utf-8")

    assert kube.build_images("20261031-090000")

    assert cluster.ran("docker", "build", "-t", "piano-transcription-worker:local")
    assert kube.target.text() == committed


def test_local_settings_use_the_test_clusters_services(project):
    root, manifests, _ = project
    (root / "server.env").write_text("MAX_ACTIVE_JOBS=7\nERROR_REPORT_URL=https://monitor.example\n"
                                     "OUTSOURCE_PROVIDER=runpod\n", encoding="utf-8")
    kube, _, _ = make(project, "local", context="docker-desktop")

    kube.write_settings()

    settings = kube_script.EnvFile(manifests / "local" / "settings.env")
    assert settings.get("MAX_ACTIVE_JOBS") == "7"
    assert settings.get("REDIS_URL") == "redis://app@redis:6379/0"
    assert settings.get("OUTSOURCE_PROVIDER") == "none"
    # A test cluster reports nothing to the real error monitor.
    assert settings.get("ERROR_REPORT_URL") is None


def test_local_passwords_are_generated_once_and_kept(project):
    root, _, _ = project
    kube, _, _ = make(project, "local", context="docker-desktop")

    kube.write_local_secret_files()
    local = root / "secrets" / "kubernetes-local"
    password = (local / "postgres_password").read_text(encoding="utf-8")
    kube.write_local_secret_files()

    assert (local / "postgres_password").read_text(encoding="utf-8") == password
    assert (local / "database_url").read_text(encoding="utf-8") == f"postgresql://billing:{password}@postgres:5432/billing"
    redis_password = (local / "redis_app_password").read_text(encoding="utf-8")
    users = (local / "users.acl").read_text(encoding="utf-8")
    # The services' rules of redis/users.acl, with the password as a hash.
    assert f"#{hashlib.sha256(redis_password.encode()).hexdigest()}" in users
    assert "-config" in users and redis_password not in users and "#{app}" not in users
    storage = json.loads((local / "s3.json").read_text(encoding="utf-8"))
    assert storage["identities"][0]["credentials"][0]["accessKey"] == (
        (local / "storage_access_key").read_text(encoding="utf-8")
    )


def test_local_up_installs_keda_once_for_the_scaling(project):
    kube, cluster, script = make(project, "local", answers=["y"], context="docker-desktop")
    cluster.answers = {"get crd scaledobjects.keda.sh": (1, "")}

    assert kube.deploy(first=True) == 0

    assert cluster.ran("kubectl", "apply", "--server-side", "-f", kube_script.KEDA_MANIFEST)
    assert any("install KEDA" in line for line in script.said)


def test_the_test_cluster_scales_on_its_own_metrics():
    import yaml

    local = yaml.safe_load((ROOT / "deploy" / "kubernetes" / "local" / "kustomization.yaml").read_text(encoding="utf-8"))
    [scaling] = [patch for patch in local["patches"] if patch.get("target", {}).get("kind") == "ScaledObject"]
    operations = {operation["path"]: operation["value"] for operation in yaml.safe_load(scaling["patch"])}
    [port] = [operation["value"] for patch in local["patches"] if patch.get("target", {}).get("name") == "api"
              for operation in yaml.safe_load(patch["patch"]) if operation["path"] == "/spec/ports/0/port"]

    # The API Service's port there, not the container's: a Service answers only on its own.
    assert operations["/spec/triggers/0/metadata/url"] == f"http://api.{local['namespace']}.svc.cluster.local:{port}/metrics"
    assert operations["/spec/triggers/1/metadata/url"] == operations["/spec/triggers/0/metadata/url"]
    assert operations["/spec/triggers/0/metadata/targetValue"] == "1"


def test_the_test_cluster_needs_docker_desktop(project):
    kube, cluster, script = make(project, "local", context="my-cluster")

    assert kube.deploy(first=True) == 1
    assert "Docker Desktop's Kubernetes" in script.text
    assert not cluster.ran("apply")


# --- A real cluster ------------------------------------------------------------------------------------

def test_up_asks_for_what_is_missing_and_pushes(project):
    write_cluster_secrets(project)
    kube, cluster, script = make(project, "cluster",
                                 answers=["rediss://app@redis.example.com:6380/0", "y", "docker.io/me"])

    assert kube.deploy(first=True) == 0

    assert cluster.ran("docker", "push", "docker.io/me/piano-transcription-api:20261030-120000")
    assert 'newName: docker.io/me/piano-transcription-worker' in kube.target.kustomization.read_text(encoding="utf-8")
    settings = kube_script.EnvFile(kube.target.settings)
    assert settings.get("REDIS_URL") == "rediss://app@redis.example.com:6380/0"


def test_nothing_changes_in_the_cluster_without_a_yes(project):
    write_cluster_secrets(project)
    kube, cluster, _ = make(project, "cluster", answers=["rediss://app@redis.example.com:6380/0", "n"])

    assert kube.deploy(first=True) == 1
    assert not any(command[0] == "docker" or "apply" in command or "create" in command for command in cluster.commands)


def test_a_missing_secret_stops_before_the_cluster_changes(project):
    write_cluster_secrets(project)
    (project[2] / "database_url").unlink()
    kube, cluster, script = make(project, "cluster", answers=["rediss://app@redis.example.com:6380/0"],
                                 hidden=[""])

    assert kube.deploy(first=True) == 1
    assert "Still missing: secrets/kubernetes/database_url" in script.text
    assert not cluster.ran("apply")


def test_secrets_are_copied_from_files_and_never_shown(project):
    write_cluster_secrets(project)
    kube, cluster, script = make(project, "cluster")

    assert kube.apply_secrets()

    made = [command for command in cluster.commands if "--dry-run=client" in command]
    assert {command[command.index("generic") + 1] for command in made} == {"piano-secrets", "piano-redis", "piano-cloudflare"}
    assert all(any(part.startswith("--from-file=") for part in command) for command in made)
    # The YAML with the values went to kubectl, not to the screen.
    assert cluster.inputs and "s3cret-value" not in script.text
    assert not any("s3cret-value" in " ".join(command) for command in cluster.commands)


# --- A cluster set up by hand, as docs/kubernetes.md describes --------------------------------------

def test_secrets_made_by_hand_keep_the_keys_without_a_file_here(project):
    write_cluster_secrets(project)
    # By hand, the database's URL and CA file are in secrets/, not secrets/kubernetes.
    (project[2] / "database_url").unlink()
    kube, cluster, script = make(project, "cluster")
    cluster.answers = {
        "get secret piano-secrets -o json": (0, json.dumps({"data": {
            "database_url": "aGFuZA==", "postgres_ca.crt": "Y2E=", "metrics_token": "b2xk"}})),
    }

    assert kube.apply_secrets()

    [secret] = [json.loads(text) for text in cluster.inputs if '"database_url"' in text]
    assert secret["data"]["database_url"] == "aGFuZA==" and secret["data"]["postgres_ca.crt"] == "Y2E="
    # A file here wins over the cluster's value.
    assert secret["data"]["metrics_token"] != "b2xk"
    assert "kept from the cluster: database_url, postgres_ca.crt" in script.text
    assert "aGFuZA==" not in script.text


def test_a_key_the_cluster_has_is_not_asked_for(project):
    write_cluster_secrets(project)
    (project[2] / "database_url").unlink()
    kube, cluster, script = make(project, "cluster")
    cluster.answers = {"get secret piano-secrets -o go-template": (0, "database_url metrics_token ")}

    assert kube.ensure_secret_files()
    assert "database_url" not in script.text


def test_images_written_by_hand_in_any_order(tmp_path):
    text = (
        "namespace: piano-transcription\n"
        "images:\n"
        "  # The API.\n"
        "  - newTag: v1  # by hand\n"
        "    name: piano-api\n"
        "    newName: ghcr.io/me/piano-transcription-api\n"
        "- name: piano-worker\n"
        "  newName: ghcr.io/me/piano-transcription-worker\n"
        "  digest: sha256:0123\n"
        "configMapGenerator: []\n"
    )
    target = kube_script.Target(local=False, manifests=tmp_path)
    target.kustomization.write_text(text, encoding="utf-8")

    written = kube_script.set_images(text, "docker.io/other", "20261030-120000")
    entries = kube_script.image_entries(written.splitlines())

    assert {name: {field: value for field, (_, value) in entry.items()} for name, entry in entries.items()} == {
        "piano-api": {"name": "piano-api", "newName": "docker.io/other/piano-transcription-api",
                      "newTag": "20261030-120000"},
        # The digest would win over the tag.
        "piano-worker": {"name": "piano-worker", "newName": "docker.io/other/piano-transcription-worker",
                         "newTag": "20261030-120000"},
    }
    assert "  # The API.\n" in written and written.endswith("configMapGenerator: []\n")
    # Each field where it was, the entry's first with its "- ".
    assert '  - newTag: "20261030-120000"\n    name: piano-api\n' in written
    assert '- name: piano-worker\n  newName: docker.io/other/piano-transcription-worker\n  newTag: "20261030-120000"\n' in written
    assert kube_script.set_images(written, "docker.io/other", "20261030-120000") == written
    assert target.images()["piano-api"] == ("ghcr.io/me/piano-transcription-api", "v1")
    assert target.images()["piano-worker"] == ("ghcr.io/me/piano-transcription-worker", "")


def test_an_image_without_new_name_gets_the_projects():
    text = "images:\n  - name: piano-api\n    newTag: \"1\"\n  - name: piano-worker\n"

    written = kube_script.set_images(text, "docker.io/me", "2")

    assert written == ("images:\n  - name: piano-api\n    newTag: \"2\"\n    newName: docker.io/me/piano-transcription-api\n"
                       "  - name: piano-worker\n    newName: docker.io/me/piano-transcription-worker\n    newTag: \"2\"\n")


def test_a_missing_image_entry_stops_before_anything_is_built(project):
    kube, cluster, script = make(project, "cluster")
    text = kube.target.text()
    kube.target.kustomization.write_text(text.replace("  - name: piano-worker", "  - name: something-else"), encoding="utf-8")

    assert not kube.build_images("20261030-120000", "docker.io/me")

    assert "names no image piano-worker" in script.text
    assert not cluster.ran("docker")


def test_down_of_a_real_cluster_needs_its_name_typed(project):
    kube, cluster, _ = make(project, "cluster", answers=["yes"])

    assert kube.down() == 1
    assert not cluster.ran("delete")

    kube, cluster, _ = make(project, "cluster", answers=["piano-transcription"])
    assert kube.down() == 0
    assert cluster.ran("kubectl", "delete", "namespace", "piano-transcription")


def test_a_real_cluster_is_never_this_pc(project):
    kube, cluster, script = make(project, "cluster", context="docker-desktop")

    assert kube.status() == 1
    assert "python scripts/kube.py local up" in script.text


# --- Status --------------------------------------------------------------------------------------------

def test_status_shows_pods_scaling_queue_leader_and_problems(project):
    kube, cluster, script = make(project, "cluster")
    cluster.answers = {
        "get hpa keda-hpa-worker": (0, json.dumps({"spec": {"minReplicas": 1, "maxReplicas": 4},
                                                   "status": {"currentReplicas": 2, "desiredReplicas": 3}})),
        "app.admin status --json": (0, json.dumps({
            "api_leader": "api-7f9:abc", "local_worker": {"count": 2, "capacity": 4},
            "waiting": {"local": {"waiting": 3, "oldest_wait_seconds": 42.0}, "remote": {"waiting": 0}},
        })),
        "app.config features --json": (0, json.dumps([{"problems": ["On Kubernetes, metrics for scaling is needed"]}])),
        "get secrets": (0, json.dumps({"items": [{"metadata": {"name": "piano-secrets"}, "data": {"database_url": "x"}}]})),
    }

    assert kube.status() == 0

    text = script.text
    assert "api" in text and "2/2 ready" in text
    assert "KEDA: 2 workers, wants 3, 1 to 4" in text
    assert "api-7f9:abc" in text and "2, 4 jobs at once" in text
    assert "local 3 waiting, the oldest 42 s" in text
    assert "On Kubernetes, metrics for scaling is needed" in text
    assert "missing keys" in text and "storage_access_key" in text


# --- Kept in step ----------------------------------------------------------------------------------------

def test_the_secrets_are_the_ones_the_server_reads():
    known = set(config.SecretSettings.model_fields) | {"google_play_service_account.json", "redis_ca.crt", "postgres_ca.crt"}

    assert {file.key for file in kube_script.CLUSTER_SECRET_FILES["piano-secrets"]} <= known
    assert {file.key for file in kube_script.LOCAL_SECRET_FILES["piano-secrets"]} <= known


def test_the_local_settings_are_known_settings():
    assert set(kube_script.LOCAL_SETTINGS) | set(kube_script.LOCAL_UNSET) <= set(config.known_variables())


def test_the_command_line(project):
    kube, cluster, _ = make(project, "cluster")

    assert kube_script.main(["local", "status"], kube) == 1
    assert kube.target.local
    with pytest.raises(SystemExit):
        kube_script.main(["nonsense"], kube)


# --- server.py's and admin.py's commands -----------------------------------------------------------

DEPLOYMENTS = json.dumps({"items": [
    {"metadata": {"name": name}, "spec": {"replicas": 1, "template": {"spec": {"containers": [{"image": "x"}]}}}}
    for name in ("api", "worker", "redis", "postgres")
]})


def serving(cluster):
    """A running API with nothing planned and no running jobs."""
    cluster.answers = {
        "get deployments": (0, DEPLOYMENTS),
        "app.maintenance schedule": (0, json.dumps({"message": "Restart soon", "seconds_remaining": 0})),
        "app.maintenance active-jobs": (0, "0"),
        "app.maintenance status": (0, "null"),
    }


def test_every_server_command_runs_on_a_cluster():
    assert set(kube_script.SERVER_SCRIPT.actions()) <= set(kube_script.COMMANDS + kube_script.SERVER_COMMANDS)


def test_restart_is_server_scripts_with_a_rolling_update(project):
    kube, cluster, script = make(project, "local", answers=["y"], context="docker-desktop")
    serving(cluster)

    assert kube_script.main(["local", "restart", "0s", "--no-build"], kube) == 0

    order = [" ".join(command) for command in cluster.commands]
    announced = next(i for i, c in enumerate(order) if "app.maintenance schedule restart" in c)
    restarted = next(i for i, c in enumerate(order) if "rollout restart" in c)
    assert announced < restarted
    # The server's pods, not the test cluster's Redis and PostgreSQL.
    assert order[restarted].endswith("rollout restart deploy/api deploy/worker")
    assert cluster.ran("apply", "-k") and cluster.ran("annotate", "scaledobject", f"{kube_script.KEDA_PAUSED}-")
    # The cluster's Redis is not the stack's: nothing saved or reloaded.
    assert not any("redis-cli" in c or "start.sh" in c for c in order)
    assert any("Restart the server in the test cluster on this PC, context docker-desktop?" in line
               for line in script.said)


def test_nothing_is_announced_without_a_yes(project):
    kube, cluster, _ = make(project, "local", answers=["n"], context="docker-desktop")
    serving(cluster)

    assert kube_script.main(["local", "restart", "5m"], kube) == 1
    assert not cluster.ran("app.maintenance")


def test_stop_pauses_keda_and_scales_the_server_to_none(project, capsys):
    kube, cluster, script = make(project, "cluster", answers=["y"])
    serving(cluster)

    assert kube_script.main(["stop", "0s"], kube) == 0

    assert cluster.ran("annotate", "scaledobject", f"{kube_script.KEDA_PAUSED}=0")
    assert cluster.ran("scale", "deploy/api", "deploy/worker", "--replicas=0")
    assert not cluster.ran("deploy/redis")
    assert "Start again with: python scripts/kube.py start" in capsys.readouterr().out


def test_the_build_runs_in_the_background_with_the_registry(project):
    kube, cluster, _ = make(project, "cluster", answers=["docker.io/me"])
    write_cluster_secrets(project)
    kube.target.settings.write_text("REDIS_URL=rediss://app@redis.example.com:6380/0\n", encoding="utf-8")
    started = []
    stack = kube_script.KubeStack(kube, popen=lambda command, **options: started.append(command))

    stack.prepare_files()
    stack.start_build()

    [command] = started
    assert command[2:] == ["build", "20261030-120000", "--registry", "docker.io/me"]


def test_admin_is_admin_scripts_in_an_api_pod(project):
    kube, cluster, _ = make(project, "local", context="docker-desktop")

    kube_script.main(["local", "admin", "jobs", "--failed"], kube)

    [command] = [command for command in cluster.commands if "app.admin" in command]
    # Standard input, as docker compose exec -T, and no terminal without one.
    assert command[:6] == ["kubectl", "-n", "piano-local", "exec", "-i", "deploy/api"]
    assert "ADMIN_COMMAND=python scripts/kube.py local admin" in command
    assert command[-5:] == ["python", "-m", "app.admin", "jobs", "--failed"]


def test_restore_refuses_and_changes_nothing(project, capsys):
    kube, cluster, _ = make(project, "cluster")

    assert kube_script.main(["restore", "latest"], kube) == 1
    assert "provider" in capsys.readouterr().out
    assert not cluster.ran("exec")


def test_infrastructure_is_the_test_clusters_own_services():
    import yaml

    documents = yaml.safe_load_all((ROOT / "deploy" / "kubernetes" / "local" / "services.yaml").read_text(encoding="utf-8"))
    deployments = {document["metadata"]["name"] for document in documents if document and document["kind"] == "Deployment"}

    assert deployments == set(kube_script.INFRASTRUCTURE)
