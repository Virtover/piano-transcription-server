"""The home cluster's host scripts: scripts/nodes.py, and its parts in server.py, stacks.py, and admin.py."""

import importlib.util
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_cluster_test", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server = load("server")
admin = load("admin")
nodes = server.NODES
ClusterError = nodes.ClusterError

ADDRESS = "100.101.102.103"
DIGEST = "hub.example.ts.net/piano-transcription-worker@sha256:" + "a" * 64


class FakeClock:
    def __init__(self):
        self.time = 0.0

    def now(self):
        return self.time

    def sleep(self, seconds):
        self.time += seconds


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def hub(root, registry=None, server_env=""):
    write(root / ".env", "")
    write(root / "server.env", server_env)
    return nodes.setup_hub(root, server, ADDRESS, registry, lan=True)


def s3(root):
    return json.loads((root / "secrets/storage/s3.json").read_text())


def identity(root, name):
    return next((item for item in s3(root)["identities"] if item["name"] == name), None)


def invite_json(root, name="laptop"):
    return json.loads((root / "secrets/invites" / f"{name}.json").read_text())


# --- setup_hub ------------------------------------------------------------------------

def test_setup_hub_writes_the_compose_settings(tmp_path):
    hub(tmp_path)
    values = nodes.env_values(tmp_path / ".env")
    assert values["HUB_ADDRESS"] == ADDRESS
    assert values["COMPOSE_FILE"] == "docker-compose.yml:docker-compose.hub.yml"
    assert values["COMPOSE_PATH_SEPARATOR"] == ":"
    assert "storage" not in values.get("COMPOSE_PROFILES", "").split(",")


def test_setup_hub_rejects_an_address_that_is_not_an_ip(tmp_path):
    with pytest.raises(ClusterError):
        nodes.setup_hub(tmp_path, server, "hub.example.com", None, lan=True)


def test_setup_hub_always_creates_the_hub_keys_and_identity(tmp_path):
    hub(tmp_path)
    access = (tmp_path / "secrets/cluster_storage_access_key").read_text()
    secret = (tmp_path / "secrets/cluster_storage_secret_key").read_text()
    assert access and secret
    entry = identity(tmp_path, "hub")
    assert entry["credentials"] == [{"accessKey": access, "secretKey": secret}]
    assert "Admin" in entry["actions"]


def test_setup_hub_does_not_touch_server_env(tmp_path):
    text = "STORAGE_BUCKET=runpod\nSTORAGE_ENDPOINT=https://s3.example.com\n"
    hub(tmp_path, server_env=text)
    assert (tmp_path / "server.env").read_text() == text
    values = nodes.env_values(tmp_path / "server.env")
    for name in ("JOB_STORAGE", "RESULT_DOWNLOAD", "JOB_STORAGE_BUCKET", "JOB_STORAGE_ENDPOINT"):
        assert name not in values
    assert not (tmp_path / "secrets/job_storage_access_key").exists()
    assert not (tmp_path / "secrets/storage_access_key").exists()


def test_setup_hub_twice_keeps_the_same_keys_and_one_hub_identity(tmp_path):
    hub(tmp_path)
    first = [(tmp_path / f"secrets/{name}").read_text() for name in nodes.HUB_KEYS]
    nodes.setup_hub(tmp_path, server, ADDRESS, None, lan=True)
    assert [(tmp_path / f"secrets/{name}").read_text() for name in nodes.HUB_KEYS] == first
    assert [item["name"] for item in s3(tmp_path)["identities"]].count("hub") == 1
    assert identity(tmp_path, "hub")["credentials"][0]["accessKey"] == first[0]


def test_setup_hub_registry(tmp_path):
    hub(tmp_path, registry="Hub.Example.ts.net.")
    values = nodes.env_values(tmp_path / ".env")
    assert values["HUB_REGISTRY"] == "hub.example.ts.net"
    assert "registry" in values["COMPOSE_PROFILES"].split(",")


def test_setup_hub_rejects_a_registry_that_is_not_a_hostname(tmp_path):
    with pytest.raises(ClusterError):
        hub(tmp_path, registry="not a host")


# --- invite and revoke ---------------------------------------------------------------

def test_invite_needs_a_hub(tmp_path):
    write(tmp_path / ".env", "")
    with pytest.raises(ClusterError, match="not a hub"):
        nodes.invite(tmp_path, "laptop", ["transcribe"])


def test_invite_is_refused_before_setup_hub(tmp_path, lan=True):
    write(tmp_path / ".env", f"HUB_ADDRESS={ADDRESS}\n")
    write(tmp_path / "server.env", "")
    with pytest.raises(ClusterError, match="not a hub"):
        nodes.invite(tmp_path, "laptop", ["transcribe"])
    write(tmp_path / ".env", "")
    write(tmp_path / "secrets/storage/s3.json", '{"identities": []}')
    with pytest.raises(ClusterError, match="not a hub"):
        nodes.invite(tmp_path, "laptop", ["transcribe"])
    assert not (tmp_path / "secrets/redis/nodes/laptop").exists()


@pytest.mark.parametrize("name", ["hub", "Bad Name", "-x", "", "a" * 40, "a_b"])
def test_invite_refuses_an_invalid_name(tmp_path, name):
    hub(tmp_path)
    with pytest.raises(ClusterError):
        nodes.invite(tmp_path, name, ["transcribe"])


def test_invite_writes_the_password_and_the_invite_file(tmp_path):
    hub(tmp_path)
    path, todo = nodes.invite(tmp_path, "laptop", ["transcribe", "arrange"])
    password = (tmp_path / "secrets/redis/nodes/laptop").read_text()
    assert password
    assert path == tmp_path / "secrets/invites/laptop.json"
    invitation = invite_json(tmp_path)
    assert invitation["format"] == 1
    assert invitation["node"] == "laptop"
    assert invitation["hub_address"] == ADDRESS
    assert invitation["tasks"] == ["transcribe", "arrange"]
    assert invitation["secrets"]["redis_password"] == password
    assert todo == {"storage_changed": True, "shared_keys": []}


def test_inviting_again_needs_again_and_changes_the_password(tmp_path):
    hub(tmp_path)
    nodes.invite(tmp_path, "laptop", ["transcribe"])
    first = (tmp_path / "secrets/redis/nodes/laptop").read_text()
    with pytest.raises(ClusterError, match="invited already"):
        nodes.invite(tmp_path, "laptop", ["transcribe"])
    nodes.invite(tmp_path, "laptop", ["transcribe"], again=True)
    assert (tmp_path / "secrets/redis/nodes/laptop").read_text() != first


def test_invite_gives_the_node_its_own_limited_keys_to_the_hubs_bucket(tmp_path):
    hub(tmp_path)
    hub_access = (tmp_path / "secrets/cluster_storage_access_key").read_text()
    hub_secret = (tmp_path / "secrets/cluster_storage_secret_key").read_text()
    path, todo = nodes.invite(tmp_path, "laptop", ["transcribe"])
    entry = identity(tmp_path, "node-laptop")
    assert entry is not None and "Admin" not in entry["actions"]
    assert all(action.endswith(":piano-jobs") for action in entry["actions"])
    secrets = invite_json(tmp_path)["secrets"]
    assert secrets["cluster_storage_access_key"] == entry["credentials"][0]["accessKey"]
    assert secrets["cluster_storage_secret_key"] == entry["credentials"][0]["secretKey"]
    assert hub_access not in path.read_text() and hub_secret not in path.read_text()
    assert todo == {"storage_changed": True, "shared_keys": []}


@pytest.mark.parametrize("server_env, tasks, shared", [
    ("", ["transcribe"], False),
    ("JOB_STORAGE=s3\n", ["transcribe"], True),
    ("", ["transcribe", "dispatch"], True),
])
def test_the_cloud_buckets_keys_go_only_where_they_are_needed(tmp_path, server_env, tasks, shared):
    hub(tmp_path, server_env=server_env)
    write(tmp_path / "secrets/storage_access_key", "cloud-access")
    write(tmp_path / "secrets/storage_secret_key", "cloud-secret")
    _, todo = nodes.invite(tmp_path, "laptop", tasks)
    secrets = invite_json(tmp_path)["secrets"]
    assert ("storage_secret_key" in secrets) is shared
    assert todo["shared_keys"] == (["storage_access_key", "storage_secret_key"] if shared else [])
    # The hub's bucket keys are the node's own either way.
    assert secrets["cluster_storage_access_key"] != "cloud-access"


def test_invite_includes_the_runpod_key_only_for_dispatch(tmp_path):
    hub(tmp_path)
    write(tmp_path / "secrets/runpod_api_key", "rp-key")
    nodes.invite(tmp_path, "laptop", ["transcribe"])
    assert "runpod_api_key" not in invite_json(tmp_path)["secrets"]
    nodes.invite(tmp_path, "laptop", ["transcribe", "dispatch"], again=True)
    assert invite_json(tmp_path)["secrets"]["runpod_api_key"] == "rp-key"


def test_invite_never_holds_the_hubs_other_secrets(tmp_path):
    hub(tmp_path)
    write(tmp_path / "secrets/google_play_service_account.json", '{"private_key": "gp-secret"}')
    write(tmp_path / "secrets/postgres_password", "pg-secret")
    write(tmp_path / "secrets/cloudflare/tunnel_token", "tunnel-secret")
    write(tmp_path / "secrets/redis/operator", "operator-secret")
    write(tmp_path / "secrets/runpod_api_key", "rp-secret")
    path, _ = nodes.invite(tmp_path, "laptop", nodes.check_tasks("all"))
    text = path.read_text()
    for value in ("gp-secret", "pg-secret", "tunnel-secret", "operator-secret"):
        assert value not in text
    assert set(invite_json(tmp_path)["secrets"]) <= {"redis_password", "runpod_api_key", "error_report_token",
                                                     "cluster_storage_access_key", "cluster_storage_secret_key"}


def test_revoke_removes_the_files_and_the_node_identity_only(tmp_path):
    hub(tmp_path)
    nodes.invite(tmp_path, "laptop", ["transcribe"])
    nodes.invite(tmp_path, "desktop", ["transcribe"])
    result = nodes.revoke(tmp_path, "laptop")
    assert not (tmp_path / "secrets/redis/nodes/laptop").exists()
    assert not (tmp_path / "secrets/invites/laptop.json").exists()
    assert identity(tmp_path, "node-laptop") is None
    assert identity(tmp_path, "node-desktop") and identity(tmp_path, "hub")
    assert (tmp_path / "secrets/redis/nodes/desktop").exists()
    assert result["storage_changed"] is True and result["cloud_keys"] is False
    assert sorted(result["removed"]) == sorted([str(Path("secrets/redis/nodes/laptop")),
                                                str(Path("secrets/invites/laptop.json"))])


# --- join -----------------------------------------------------------------------------

@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(nodes, "has_nvidia_gpu", lambda: False)
    monkeypatch.setattr(nodes, "reachable", lambda host, port, timeout=5.0: True)


def make_invite(root, **changes):
    invitation = {"format": 1, "node": "laptop", "hub_address": ADDRESS, "tasks": ["transcribe"], "registry": None,
                  "secrets": {"redis_password": "pw"}}
    invitation.update(changes)
    return write(root / "laptop.json", json.dumps(invitation))


def test_join_writes_node_env_secrets_and_deletes_the_invite(tmp_path, offline):
    invitation = make_invite(tmp_path, registry="hub.example.ts.net",
                             secrets={"redis_password": "pw", "storage_access_key": "ak"})
    nodes.join(tmp_path, invitation, "cpu", False)
    values = nodes.env_values(tmp_path / "node.env")
    assert values["NODE_NAME"] == "laptop"
    assert values["HUB_ADDRESS"] == ADDRESS
    assert values["NODE_PROCESSOR"] == "cpu"
    assert values["NODE_REGISTRY"] == "hub.example.ts.net"
    assert "WORKER_GPU_BUILD" not in values
    assert (tmp_path / "secrets/node/redis_password").read_text() == "pw"
    assert (tmp_path / "secrets/node/storage_access_key").read_text() == "ak"
    assert not invitation.exists()


def test_join_legacy_sets_the_gpu_build(tmp_path, offline):
    nodes.join(tmp_path, make_invite(tmp_path), "gpu", True)
    assert nodes.env_values(tmp_path / "node.env")["WORKER_GPU_BUILD"] == "legacy"


def test_join_without_a_registry_writes_none(tmp_path, offline):
    nodes.join(tmp_path, make_invite(tmp_path), "cpu", False)
    assert "NODE_REGISTRY" not in nodes.env_values(tmp_path / "node.env")


def test_join_warns_when_the_hub_is_unreachable(tmp_path, monkeypatch):
    monkeypatch.setattr(nodes, "reachable", lambda host, port, timeout=5.0: False)
    messages = nodes.join(tmp_path, make_invite(tmp_path), "cpu", False)
    assert any("cannot reach the hub" in message for message in messages)


@pytest.mark.parametrize("changes", [
    {"format": 2},
    {"secrets": {}},
    {"secrets": {"redis_password": "pw", "postgres_password": "x"}},
    {"node": "Bad Name"},
    {"node": "hub"},
    {"registry": "not a host"},
    {"hub_address": "example.com"},
    {"tasks": ["fly"]},
])
def test_join_refuses_a_bad_invite(tmp_path, offline, changes):
    invitation = make_invite(tmp_path, **changes)
    with pytest.raises(ClusterError):
        nodes.join(tmp_path, invitation, "cpu", False)
    assert invitation.exists()
    assert not (tmp_path / "node.env").exists()


def test_read_invite_refuses_a_file_that_is_not_json(tmp_path):
    with pytest.raises(ClusterError, match="not an invite file"):
        nodes.read_invite(write(tmp_path / "x.json", "nope"))
    with pytest.raises(ClusterError, match="not an invite file"):
        nodes.read_invite(tmp_path / "missing.json")


def test_join_refuses_to_become_another_node(tmp_path, offline):
    write(tmp_path / "node.env", "NODE_NAME=desktop\n")
    with pytest.raises(ClusterError, match="desktop"):
        nodes.join(tmp_path, make_invite(tmp_path), "cpu", False)
    assert nodes.env_values(tmp_path / "node.env")["NODE_NAME"] == "desktop"


def test_join_refuses_in_a_hub_checkout(tmp_path, offline):
    write(tmp_path / ".env", "")
    (tmp_path / "billing-data").mkdir()
    with pytest.raises(ClusterError, match="runs a hub"):
        nodes.join(tmp_path, make_invite(tmp_path), "cpu", False)


def test_join_again_as_the_same_node_replaces_its_secrets(tmp_path, offline):
    nodes.join(tmp_path, make_invite(tmp_path), "cpu", False)
    nodes.join(tmp_path, make_invite(tmp_path, secrets={"redis_password": "new"}), "cpu", False)
    assert (tmp_path / "secrets/node/redis_password").read_text() == "new"


# --- env files --------------------------------------------------------------------------

def test_set_env_value_keeps_lines_replaces_and_removes(tmp_path):
    path = write(tmp_path / "node.env", "# comment\nA=1\nB=2\n")
    nodes.set_env_value(path, "C", "3")
    nodes.set_env_value(path, "A", "9")
    assert path.read_text().splitlines() == ["# comment", "B=2", "C=3", "A=9"] or \
        path.read_text().splitlines() == ["# comment", "A=9", "B=2", "C=3"]
    nodes.set_env_value(path, "B", None)
    values = nodes.env_values(path)
    assert values == {"C": "3", "A": "9"}
    assert path.read_text().splitlines()[0] == "# comment"


def test_set_env_value_creates_the_file(tmp_path):
    nodes.set_env_value(tmp_path / "node.env", "A", "1")
    assert nodes.env_values(tmp_path / "node.env") == {"A": "1"}


def test_env_values_skips_comments_and_unquotes(tmp_path):
    path = write(tmp_path / "x.env", "# c\n\nA='x y'\nB = \"z\"\nnot a line\n")
    assert nodes.env_values(path) == {"A": "x y", "B": "z"}


def test_check_tasks():
    assert nodes.check_tasks("all") == list(nodes.TASKS)
    assert nodes.check_tasks("arrange, transcribe") == ["transcribe", "arrange"]
    for bad in ("", "fly", "transcribe,fly"):
        with pytest.raises(ClusterError):
            nodes.check_tasks(bad)


# --- the RESP client ----------------------------------------------------------------------

def stream(data: bytes):
    return io.BytesIO(data)


def test_resp_read_replies():
    assert nodes.resp_read(stream(b"+OK\r\n")) == "OK"
    assert nodes.resp_read(stream(b":5\r\n")) == 5
    assert nodes.resp_read(stream(b"$3\r\nabc\r\n")) == "abc"
    assert nodes.resp_read(stream(b"$-1\r\n")) is None
    assert nodes.resp_read(stream(b"*2\r\n$1\r\na\r\n:7\r\n")) == ["a", 7]


def test_resp_read_error_and_closed_connection():
    with pytest.raises(ClusterError, match="WRONGPASS"):
        nodes.resp_read(stream(b"-WRONGPASS invalid\r\n"))
    with pytest.raises(ClusterError, match="closed"):
        nodes.resp_read(stream(b""))
    with pytest.raises(ClusterError):
        nodes.resp_read(stream(b"?x\r\n"))


class Duplex:
    def __init__(self, reply: bytes):
        self.reply = io.BytesIO(reply)
        self.written = b""

    def write(self, data):
        self.written += data

    def flush(self):
        pass

    def readline(self):
        return self.reply.readline()

    def read(self, size):
        return self.reply.read(size)


def test_resp_command_writes_an_array_of_bulk_strings():
    connection = Duplex(b"+OK\r\n")
    assert nodes.resp_command(connection, "HGET", "k", "é") == "OK"
    assert connection.written == b"*3\r\n$4\r\nHGET\r\n$1\r\nk\r\n$2\r\n\xc3\xa9\r\n"


class FakeSocket:
    def __init__(self, reply):
        self.duplex = Duplex(reply)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def makefile(self, mode):
        return self.duplex


def node_files(root):
    write(root / "node.env", f"NODE_NAME=laptop\nHUB_ADDRESS={ADDRESS}\n")
    write(root / "secrets/node/redis_password", "pw\n")


def test_hub_image_authenticates_and_returns_the_reference(tmp_path, monkeypatch):
    node_files(tmp_path)
    reply = f"+OK\r\n${len(DIGEST)}\r\n{DIGEST}\r\n".encode()
    connection = FakeSocket(reply)
    monkeypatch.setattr(nodes.socket, "create_connection", lambda address, timeout: connection)
    assert nodes.hub_image(tmp_path, "current") == DIGEST
    written = connection.duplex.written
    assert b"AUTH\r\n" in written and b"node-laptop" in written and b"worker-current" in written


def test_hub_image_none_when_the_hub_has_none(tmp_path, monkeypatch):
    node_files(tmp_path)
    monkeypatch.setattr(nodes.socket, "create_connection", lambda address, timeout: FakeSocket(b"+OK\r\n$-1\r\n"))
    assert nodes.hub_image(tmp_path, "legacy") is None


@pytest.mark.parametrize("reference", [
    "hub.example.ts.net/worker:latest",
    "hub.example.ts.net/worker@sha256:abc",
    "worker@sha256:" + "a" * 64,
    "HUB/worker@sha256:" + "a" * 64,
    "hub.example.ts.net/worker@sha256:" + "A" * 64,
])
def test_hub_image_rejects_a_reference_that_is_not_by_digest(tmp_path, monkeypatch, reference):
    node_files(tmp_path)
    reply = f"+OK\r\n${len(reference)}\r\n{reference}\r\n".encode()
    monkeypatch.setattr(nodes.socket, "create_connection", lambda address, timeout: FakeSocket(reply))
    with pytest.raises(ClusterError, match="by digest"):
        nodes.hub_image(tmp_path, "current")


# --- NodeStack and ComposeStack -------------------------------------------------------------

def node_stack(tmp_path, gpu, calls=None, stdout=""):
    calls = calls if calls is not None else []

    def run(command, **options):
        calls.append((command, options))
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    return server.NodeStack(gpu, run=run, root=tmp_path), calls


def test_node_stack_compose_with_and_without_gpu(tmp_path):
    with_gpu, _ = node_stack(tmp_path, True)
    without, _ = node_stack(tmp_path, False)
    gpu_command, cpu_command = with_gpu.compose(), without.compose()
    assert "docker-compose.node-gpu.yml" in gpu_command
    assert gpu_command[gpu_command.index("docker-compose.node-gpu.yml") - 1] == "-f"
    assert "docker-compose.node-gpu.yml" not in cpu_command
    for command in (gpu_command, cpu_command):
        assert command[:2] == ["docker", "compose"]
        assert command[command.index("-p") + 1] == "piano-node"
        assert command[command.index("--env-file") + 1] == "node.env"
        assert command[command.index("-f") + 1] == "docker-compose.node.yml"


def test_node_stack_commands(tmp_path):
    stack, calls = node_stack(tmp_path, False, stdout="abc123\n")
    base = stack.compose()
    assert stack.up() == 0 and calls[-1][0] == base + ["up", "-d", "--build"]
    assert stack.up(build=False) == 0 and calls[-1][0] == base + ["up", "-d"]
    stack.pull()
    assert calls[-1][0] == base + ["pull", "worker"]
    stack.stop(False)
    assert calls[-1][0] == base + ["stop"]
    stack.stop(True)
    assert calls[-1][0] == base + ["down"]
    assert stack.running() is True
    assert calls[-1][0] == base + ["ps", "--status", "running", "-q", "worker"]
    stack.drain(300)
    assert calls[-1][0] == base + ["exec", "-T", "worker", "python3", "-m", "app.worker.supervisor", "drain", "300s"]
    stack.status()
    assert calls[-1][0][-4:] == ["python3", "-m", "app.cluster", "status"]
    assert calls[-1][1]["cwd"] == tmp_path


def test_node_stack_not_running_without_output(tmp_path):
    stack, _ = node_stack(tmp_path, True, stdout="")
    assert stack.running() is False
    assert stack.owns_redis is False and stack.cannot_restore


def test_compose_stack_build_and_push_image(tmp_path):
    calls = []

    def run(command, **options):
        calls.append(command)
        return SimpleNamespace(returncode=0, stderr="",
                               stdout="other/repo@sha256:" + "b" * 64 + "\nhost/img@sha256:" + "c" * 64 + "\n")

    stack = server.ComposeStack([], run=run, root=tmp_path)
    assert stack.build_image("Dockerfile.worker", "host/img:current", {"GPU_BUILD": "current"}) == 0
    assert calls[-1] == ["docker", "build", "-f", "Dockerfile.worker", "--build-arg", "GPU_BUILD=current",
                         "-t", "host/img:current", "."]
    assert stack.push_image("host/img:current") == "host/img@sha256:" + "c" * 64
    assert calls[-2] == ["docker", "push", "host/img:current"]


def test_compose_stack_push_image_fails(tmp_path):
    codes = iter([1])
    stack = server.ComposeStack([], run=lambda command, **o: SimpleNamespace(returncode=next(codes), stdout=""),
                                root=tmp_path)
    assert stack.push_image("host/img:current") is None
    stack = server.ComposeStack([], run=lambda command, **o: SimpleNamespace(returncode=0, stdout="x@sha256:1\n"),
                                root=tmp_path)
    assert stack.push_image("host/img:current") is None


def test_compose_stack_restart_services(tmp_path):
    calls = []
    stack = server.ComposeStack(["storage"], run=lambda c, **o: calls.append(c) or SimpleNamespace(returncode=0),
                                root=tmp_path)
    assert stack.restart_services(["storage"]) == 0
    assert calls[0] == ["docker", "compose", "--profile", "storage", "restart", "storage"]


# --- server.py on a node ------------------------------------------------------------------------

class FakeNodeStack(server.NodeStack):
    def __init__(self, root, gpu=False, is_running=True):
        super().__init__(gpu, root=root)
        self.calls = []
        self.is_running = is_running

    def up(self, command=None, build=True, recreate=False):
        self.calls.append(("up", build, recreate) if recreate else ("up", build))
        return 0

    def pull(self):
        self.calls.append(("pull",))
        return 0

    def stop(self, down):
        self.calls.append(("stop", down))
        return 0

    def drain(self, timeout):
        self.calls.append(("drain", timeout))
        return 0

    def running(self):
        return self.is_running

    def status(self):
        self.calls.append(("status",))
        return 0

    def remove_stale_containers(self):
        self.calls.append(("remove_stale",))
        return []


def node(tmp_path, env="", **options):
    write(tmp_path / "node.env", f"NODE_NAME=laptop\nHUB_ADDRESS={ADDRESS}\nNODE_PROCESSOR=cpu\n{env}")
    write(tmp_path / "secrets/node/redis_password", "pw")
    return FakeNodeStack(tmp_path, **options)


def names(stack):
    return [call[0] for call in stack.calls]


def test_node_stop_drains_then_stops(tmp_path):
    stack = node(tmp_path)
    assert server.main(["stop", "--grace", "2m"], stack, FakeClock()) == 0
    assert stack.calls == [("drain", 120.0), ("stop", False)]


def test_node_stop_down_and_not_running_skips_the_drain(tmp_path):
    stack = node(tmp_path, is_running=False)
    assert server.main(["stop", "--down"], stack, FakeClock()) == 0
    assert stack.calls == [("stop", True)]


@pytest.mark.parametrize("action", ["restart", "update"])
def test_node_restart_and_update_drain_then_start(tmp_path, action):
    stack = node(tmp_path)
    assert server.main([action], stack, FakeClock()) == 0
    assert names(stack) == ["drain", "remove_stale", "up"]
    assert stack.calls[-1] == ("up", True)


def test_node_start_without_a_password_does_not_start(tmp_path, capsys):
    stack = node(tmp_path)
    (tmp_path / "secrets/node/redis_password").unlink()
    assert server.main(["start"], stack, FakeClock()) == 1
    assert "up" not in names(stack)
    assert "redis_password" in capsys.readouterr().out


def test_node_start_waits_for_the_worker_and_gives_up(tmp_path):
    stack = node(tmp_path, is_running=False)
    clock = FakeClock()
    assert server.main(["start"], stack, clock) == 1
    assert clock.time >= server.NODE_START_TIMEOUT


def test_node_status(tmp_path, capsys):
    stack = node(tmp_path, is_running=False)
    assert server.main(["status"], stack, FakeClock()) == 0
    assert "not running" in capsys.readouterr().out
    assert stack.calls == []
    running = node(tmp_path)
    assert server.main(["status"], running, FakeClock()) == 0
    assert running.calls == [("status",)]


@pytest.mark.parametrize("action", ["cancel", "restore"])
def test_node_refuses_the_hub_commands(tmp_path, capsys, action):
    stack = node(tmp_path)
    assert server.main([action], stack, FakeClock()) == 1
    assert "laptop" in capsys.readouterr().out
    assert stack.calls == []


def test_node_start_pulls_the_hubs_image(tmp_path, monkeypatch):
    stack = node(tmp_path, env="NODE_REGISTRY=hub.example.ts.net\n")
    monkeypatch.setattr(nodes, "hub_image", lambda root, variant: DIGEST)
    assert server.main(["start"], stack, FakeClock()) == 0
    assert nodes.env_values(tmp_path / "node.env")["NODE_WORKER_IMAGE"] == DIGEST
    assert stack.calls == [("remove_stale",), ("pull",), ("up", False)]


def test_node_start_builds_when_the_hub_has_no_image(tmp_path, monkeypatch):
    stack = node(tmp_path, env="NODE_REGISTRY=hub.example.ts.net\nNODE_WORKER_IMAGE=old\n")
    asked = []
    monkeypatch.setattr(nodes, "hub_image", lambda root, variant: asked.append(variant))
    assert server.main(["start"], stack, FakeClock()) == 0
    assert "NODE_WORKER_IMAGE" not in nodes.env_values(tmp_path / "node.env")
    assert ("up", True) in stack.calls and ("pull",) not in stack.calls
    assert asked == ["current"]


def test_node_start_builds_when_the_hub_cannot_be_asked(tmp_path, monkeypatch):
    stack = node(tmp_path, env="NODE_REGISTRY=hub.example.ts.net\n")

    def fail(root, variant):
        raise OSError("unreachable")

    monkeypatch.setattr(nodes, "hub_image", fail)
    assert server.main(["start"], stack, FakeClock()) == 0
    assert ("up", True) in stack.calls


def test_node_start_without_a_registry_never_asks_the_hub(tmp_path, monkeypatch):
    stack = node(tmp_path)

    def fail(root, variant):
        raise AssertionError("asked")

    monkeypatch.setattr(nodes, "hub_image", fail)
    assert server.main(["start"], stack, FakeClock()) == 0
    assert ("up", True) in stack.calls


def test_node_start_pull_failure_stops_before_up(tmp_path, monkeypatch):
    stack = node(tmp_path, env="NODE_REGISTRY=hub.example.ts.net\n")
    monkeypatch.setattr(nodes, "hub_image", lambda root, variant: DIGEST)
    stack.pull = lambda: 1
    assert server.main(["start"], stack, FakeClock()) == 1
    assert "up" not in names(stack)


def test_node_start_legacy_uses_the_legacy_image(tmp_path, monkeypatch):
    stack = node(tmp_path, env="NODE_REGISTRY=hub.example.ts.net\n")
    asked = []
    monkeypatch.setattr(nodes, "hub_image", lambda root, variant: asked.append(variant) or DIGEST)
    assert server.main(["start", "--legacy-gpu"], stack, FakeClock()) == 0
    assert asked == ["legacy"]
    assert nodes.env_values(tmp_path / "node.env")["WORKER_GPU_BUILD"] == "legacy"


def test_node_stack_choice_follows_node_processor(tmp_path):
    write(tmp_path / "node.env", "NODE_NAME=a\nNODE_PROCESSOR=cpu\n")
    assert server.node_stack(tmp_path).gpu is False
    write(tmp_path / "node.env", "NODE_NAME=a\nNODE_PROCESSOR=gpu\n")
    assert server.node_stack(tmp_path).gpu is True


def test_needs_delay_asks_for_a_time(capsys):
    args = SimpleNamespace(delay=None, prog_name="python scripts/server.py", action="restart")
    assert server.needs_delay(args, "restart") is False
    assert "restart 5m" in capsys.readouterr().out
    assert server.needs_delay(SimpleNamespace(delay=0.0), "restart") is True


# --- server.py join -----------------------------------------------------------------------------

def test_join_routes_to_join_and_starts_the_node(tmp_path, offline, monkeypatch):
    invitation = make_invite(tmp_path)
    stack = FakeNodeStack(tmp_path)
    assert server.main(["join", str(invitation), "--cpu"], stack, FakeClock()) == 0
    assert nodes.env_values(tmp_path / "node.env")["NODE_PROCESSOR"] == "cpu"
    assert not invitation.exists()
    # Always a new container: a running one read the old password.
    assert ("up", True, True) in stack.calls


def test_join_no_start_only_writes_files(tmp_path, offline):
    invitation = make_invite(tmp_path)
    stack = FakeNodeStack(tmp_path)
    assert server.main(["join", str(invitation), "--gpu", "--no-start"], stack, FakeClock()) == 0
    assert nodes.env_values(tmp_path / "node.env")["NODE_PROCESSOR"] == "gpu"
    assert stack.calls == []


def test_join_reports_a_bad_invite(tmp_path, offline, capsys):
    invitation = make_invite(tmp_path, format=9)
    stack = FakeNodeStack(tmp_path)
    assert server.main(["join", str(invitation)], stack, FakeClock()) == 1
    assert "not an invite" in capsys.readouterr().out
    assert stack.calls == []


# --- server.py on the hub ---------------------------------------------------------------------------

class FakeHubStack(server.ComposeStack):
    def __init__(self, root, build_code=0, reference=DIGEST):
        super().__init__([], root=root)
        self.calls = []
        self.build_code = build_code
        self.reference = reference

    def build_image(self, dockerfile, tag, build_args):
        self.calls.append(("build", dockerfile, tag, build_args))
        return self.build_code

    def push_image(self, tag):
        self.calls.append(("push", tag))
        return self.reference

    def redis(self, *arguments):
        self.calls.append(("redis", *arguments))
        return ""


def test_update_on_the_hub_is_refused(tmp_path, capsys):
    stack = FakeHubStack(tmp_path)
    assert server.main(["update"], stack, FakeClock()) == 1
    assert "cluster node" in capsys.readouterr().out
    assert stack.calls == []


def test_push_images_needs_a_registry(tmp_path, capsys):
    write(tmp_path / ".env", f"HUB_ADDRESS={ADDRESS}\n")
    stack = FakeHubStack(tmp_path)
    assert server.main(["push-images"], stack, FakeClock()) == 1
    assert "no registry" in capsys.readouterr().out
    assert stack.calls == []


def test_push_images_builds_pushes_and_records_the_digest(tmp_path):
    write(tmp_path / ".env", "HUB_REGISTRY=hub.example.ts.net\n")
    stack = FakeHubStack(tmp_path)
    assert server.main(["push-images"], stack, FakeClock()) == 0
    assert stack.calls == [
        ("build", "Dockerfile.worker", "hub.example.ts.net/piano-transcription-worker:current", {"GPU_BUILD": "current"}),
        ("push", "hub.example.ts.net/piano-transcription-worker:current"),
        ("redis", "hset", "cluster:images", "worker-current", DIGEST),
    ]


def test_push_images_legacy_adds_the_legacy_build(tmp_path):
    write(tmp_path / ".env", "HUB_REGISTRY=hub.example.ts.net\n")
    stack = FakeHubStack(tmp_path)
    assert server.main(["push-images", "--legacy"], stack, FakeClock()) == 0
    builds = [call for call in stack.calls if call[0] == "build"]
    assert [call[3] for call in builds] == [{"GPU_BUILD": "current"}, {"GPU_BUILD": "legacy"}]
    assert ("redis", "hset", "cluster:images", "worker-legacy", DIGEST) in stack.calls


def test_push_images_stops_when_the_build_or_push_fails(tmp_path):
    write(tmp_path / ".env", "HUB_REGISTRY=hub.example.ts.net\n")
    stack = FakeHubStack(tmp_path, build_code=1)
    assert server.main(["push-images", "--legacy"], stack, FakeClock()) == 1
    assert names(stack) == ["build"]
    stack = FakeHubStack(tmp_path, reference=None)
    assert server.main(["push-images"], stack, FakeClock()) == 1
    assert names(stack) == ["build", "push"]


def test_serve_hub_does_nothing_for_a_plain_server(tmp_path):
    write(tmp_path / ".env", "")
    stack = FakeHubStack(tmp_path)
    server.serve_hub(stack)
    assert stack.calls == []


def test_serve_hub_pushes_the_images_it_pushed_before(tmp_path):
    write(tmp_path / ".env", f"HUB_ADDRESS={ADDRESS}\nHUB_REGISTRY=hub.example.ts.net\n")
    stack = FakeHubStack(tmp_path)
    stack.redis = lambda *arguments: "worker-current\nworker-legacy" if arguments[0] == "hkeys" else ""
    server.serve_hub(stack)
    assert [call[3] for call in stack.calls if call[0] == "build"] == [{"GPU_BUILD": "current"}, {"GPU_BUILD": "legacy"}]


# --- admin.py nodes ------------------------------------------------------------------------------------

class AdminStack(admin.STACKS.ComposeStack):
    def __init__(self, root, problem="", exec_code=0):
        super().__init__([], root=root)
        self.problem = problem
        self.exec_code = exec_code
        self.api = []
        self.redis_calls = []
        self.restarted = []

    def exec(self, service, *command, **options):
        self.api.append((service, command, options.get("env")))
        return SimpleNamespace(returncode=self.exec_code, stdout="", stderr="")

    def reload_redis_users(self):
        return self.problem

    def redis(self, *arguments):
        self.redis_calls.append(arguments)
        return ""

    def restart_services(self, services):
        self.restarted.append(services)
        return 0


def admin_hub(root, *arguments):
    write(root / ".env", "")
    write(root / "server.env", "")
    admin.NODES.setup_hub(root, server, ADDRESS, None, lan=True)


def test_admin_invite_reloads_redis_and_registers_the_node(tmp_path):
    admin_hub(tmp_path)
    stack = AdminStack(tmp_path)
    assert admin.main(["nodes", "invite", "laptop", "--tasks", "transcribe"], stack) == 0
    [(service, command, env)] = stack.api
    assert service == "api"
    assert command[:3] == ("python", "-m", "app.admin")
    assert command[3:] == ("nodes", "register", "laptop", "transcribe", "--reason", "invited")
    assert env["ADMIN_COMMAND"] == "python scripts/admin.py"
    assert (tmp_path / "secrets/invites/laptop.json").is_file()
    # The node's keys to the hub's bucket: the bucket reads them again.
    assert stack.restarted == [["storage"]]


def test_admin_invite_restarts_the_bucket_for_the_nodes_keys(tmp_path):
    admin_hub(tmp_path)
    stack = AdminStack(tmp_path)
    assert admin.main(["nodes", "invite", "laptop"], stack) == 0
    assert stack.restarted == [["storage"]]
    # By default not dispatch, which would copy the RunPod key to the device.
    assert stack.api[0][1][-1] == "invited"
    assert stack.api[0][1][3:7] == ("nodes", "register", "laptop", "transcribe") and "dispatch" not in stack.api[0][1]
    assert "runpod_api_key" not in json.loads((tmp_path / "secrets/invites/laptop.json").read_text())["secrets"]


def test_admin_invite_fails_when_redis_does_not_reload(tmp_path, capsys):
    admin_hub(tmp_path)
    stack = AdminStack(tmp_path, problem="exit code 1")
    assert admin.main(["nodes", "invite", "laptop", "--tasks", "transcribe"], stack) == 1
    assert stack.api == []
    assert "exit code 1" in capsys.readouterr().out


def test_admin_invite_fails_when_the_api_cannot_register(tmp_path):
    admin_hub(tmp_path)
    assert admin.main(["nodes", "invite", "laptop", "--tasks", "transcribe"], AdminStack(tmp_path, exec_code=1)) == 1


def test_admin_invite_reports_a_cluster_error(tmp_path, capsys):
    admin_hub(tmp_path)
    stack = AdminStack(tmp_path)
    assert admin.main(["nodes", "invite", "hub"], stack) == 1
    assert admin.main(["nodes", "invite", "laptop", "--tasks", "fly"], stack) == 1
    assert stack.api == []
    assert capsys.readouterr().out


def test_admin_revoke_kills_connections_and_forgets_the_node(tmp_path):
    admin_hub(tmp_path)
    stack = AdminStack(tmp_path)
    admin.main(["nodes", "invite", "laptop", "--tasks", "transcribe"], stack)
    stack.api.clear()
    assert admin.main(["nodes", "revoke", "laptop"], stack) == 0
    assert ("client", "kill", "user", "node-laptop") in stack.redis_calls
    [(service, command, env)] = stack.api
    assert command[3:] == ("nodes", "forget", "laptop", "--reason", "revoked")
    assert not (tmp_path / "secrets/redis/nodes/laptop").exists()


def test_admin_revoke_survives_a_failed_redis_kill(tmp_path):
    admin_hub(tmp_path)
    stack = AdminStack(tmp_path)

    def refuse(*arguments):
        raise RuntimeError("redis is not running")

    stack.redis = refuse
    assert admin.main(["nodes", "revoke", "laptop"], stack) == 0
    assert stack.api[0][1][3:5] == ("nodes", "forget")


def test_admin_hub_command_sets_up_the_hub(tmp_path):
    write(tmp_path / ".env", "")
    write(tmp_path / "server.env", "")
    assert admin.main(["nodes", "hub", ADDRESS, "--lan"], AdminStack(tmp_path)) == 0
    assert nodes.env_values(tmp_path / ".env")["HUB_ADDRESS"] == ADDRESS


def test_admin_hub_commands_refuse_a_stack_that_is_not_compose(tmp_path, capsys):
    class Other(admin.STACKS.Stack):
        root = tmp_path

        def exec(self, *a, **k):
            raise AssertionError("must not run")

    for command in (["hub", ADDRESS], ["invite", "laptop"], ["revoke", "laptop"]):
        assert admin.main(["nodes", *command], Other()) == 1
    assert "Compose hub" in capsys.readouterr().err


def test_admin_other_nodes_commands_run_in_the_container(tmp_path, monkeypatch):
    monkeypatch.setattr(admin.sys.stdin, "isatty", lambda: False, raising=False)
    stack = AdminStack(tmp_path)
    assert admin.main(["nodes", "list"], stack) == 0
    assert stack.api[0][1] == ("python", "-m", "app.admin", "nodes", "list")


@pytest.mark.parametrize("server_env, cloud", [("", False), ("JOB_STORAGE=s3\n", True), ("OUTSOURCE_PROVIDER=runpod\n", True)])
def test_revoke_says_when_nodes_had_the_cloud_buckets_keys(tmp_path, server_env, cloud):
    hub(tmp_path, server_env=server_env)
    nodes.invite(tmp_path, "laptop", ["transcribe"])

    assert nodes.revoke(tmp_path, "laptop")["cloud_keys"] is cloud


def test_an_invite_with_the_old_job_storage_names_is_refused(tmp_path):
    invitation = {"format": 1, "node": "laptop", "hub_address": ADDRESS, "tasks": ["transcribe"],
                  "secrets": {"redis_password": "x", "job_storage_access_key": "y"}}
    path = write(tmp_path / "laptop.json", json.dumps(invitation))
    with pytest.raises(ClusterError, match="job_storage_access_key"):
        nodes.read_invite(path)



# --- A hub through Tailscale ------------------------------------------------------------------

class FakeTailscale:
    def __init__(self, address="100.88.2.112", serve_code=0):
        self.address = address
        self.serve_code = serve_code
        self.commands = []

    def __call__(self, command):
        self.commands.append(command)
        if command[:3] == ["tailscale", "ip", "-4"]:
            return subprocess.CompletedProcess(command, 0 if self.address else 1, f"{self.address or ''}\n", "")
        return subprocess.CompletedProcess(command, self.serve_code, "", "" if self.serve_code == 0 else "access denied")


def test_a_hub_finds_its_tailscale_address_and_serves_its_ports_on_the_tailnet(tmp_path, monkeypatch):
    monkeypatch.setattr(nodes.shutil, "which", lambda name: "/usr/bin/tailscale")
    write(tmp_path / ".env", "")
    write(tmp_path / "server.env", "")
    tailscale = FakeTailscale()

    messages = nodes.setup_hub(tmp_path, server, run=tailscale)

    values = nodes.env_values(tmp_path / ".env")
    assert values["HUB_ADDRESS"] == "100.88.2.112"
    # Docker publishes on this machine alone; Tailscale serves the tailnet.
    assert "HUB_BIND_ADDRESS" not in values
    assert ["tailscale", "serve", "--bg", "--tcp=6379", "tcp://localhost:6379"] in tailscale.commands
    assert ["tailscale", "serve", "--bg", "--tcp=8333", "tcp://localhost:8333"] in tailscale.commands
    assert any("to your tailnet only" in message for message in messages)


def test_a_hub_without_tailscale_says_how_to_get_it(tmp_path, monkeypatch):
    monkeypatch.setattr(nodes.shutil, "which", lambda name: None)
    write(tmp_path / ".env", "")
    with pytest.raises(ClusterError, match="install it from tailscale.com"):
        nodes.setup_hub(tmp_path, server, run=FakeTailscale())


def test_a_hub_given_another_address_than_tailscales_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(nodes.shutil, "which", lambda name: "/usr/bin/tailscale")
    write(tmp_path / ".env", "")
    with pytest.raises(ClusterError, match="100.88.2.112"):
        nodes.setup_hub(tmp_path, server, "100.1.1.1", run=FakeTailscale())


def test_a_failed_serve_is_reported_with_the_command_to_run(tmp_path, monkeypatch):
    monkeypatch.setattr(nodes.shutil, "which", lambda name: "/usr/bin/tailscale")
    write(tmp_path / ".env", "")
    write(tmp_path / "server.env", "")

    messages = nodes.setup_hub(tmp_path, server, run=FakeTailscale(serve_code=1))

    assert any("tailscale serve --bg --tcp=6379 tcp://localhost:6379" in message for message in messages)


def test_a_lan_hub_publishes_on_that_address_and_unsets_it_again_for_tailscale(tmp_path, monkeypatch):
    write(tmp_path / ".env", "")
    write(tmp_path / "server.env", "")
    tailscale = FakeTailscale()

    nodes.setup_hub(tmp_path, server, "192.168.0.141", lan=True, run=tailscale)
    assert nodes.env_values(tmp_path / ".env")["HUB_BIND_ADDRESS"] == "192.168.0.141"
    assert tailscale.commands == []

    monkeypatch.setattr(nodes.shutil, "which", lambda name: "/usr/bin/tailscale")
    nodes.setup_hub(tmp_path, server, run=tailscale)
    values = nodes.env_values(tmp_path / ".env")
    assert values["HUB_ADDRESS"] == "100.88.2.112" and "HUB_BIND_ADDRESS" not in values
