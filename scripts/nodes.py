"""A home cluster from the host (docs/cluster.md).

The hub's side, run by scripts/admin.py:

    python scripts/admin.py nodes hub 100.101.102.103 [--registry hub.example.ts.net]
    python scripts/admin.py nodes invite laptop --tasks transcribe,arrange
    python scripts/admin.py nodes revoke laptop

A device's side, run by scripts/server.py on a device with node.env:

    python scripts/server.py join laptop.json [--cpu | --gpu] [--legacy-gpu]
    python scripts/server.py start | stop | restart | update | status

An invite is a file with the node's Redis password and the keys its tasks
need, and nothing else of the hub's secrets. It is written to
secrets/invites, which git ignores, and deleted by join. Copy it to the
device by a private way, such as Tailscale's file sending; never by email
or chat.

Runs on the host with only the Python standard library.
"""

import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
from pathlib import Path

INVITE_FORMAT = 1
NODE_ENV = "node.env"
NODE_SECRETS = "secrets/node"
INVITES = "secrets/invites"
NODE_PASSWORDS = "secrets/redis/nodes"
S3_CONFIG = "secrets/storage/s3.json"
HUB_FILES = "docker-compose.yml:docker-compose.hub.yml"
HUB_BUCKET = "piano-jobs"
STORAGE_PORT = 8333
REDIS_PORT = 6379
NODE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
HOSTNAME = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
TASKS = ("transcribe", "arrange", "dispatch", "check")
# What a node may get from the hub's secrets, by the task that needs it.
STORAGE_KEYS = ("storage_access_key", "storage_secret_key")
# The hub's bucket for job files: the hub's keys, and on a node its own.
HUB_KEYS = ("cluster_storage_access_key", "cluster_storage_secret_key")
RUNPOD_KEYS = ("runpod_api_key", "runpod_read_api_key")
ALWAYS = ("error_report_token",)
# Variables of node.env that scripts/server.py writes.
NODE_VARIABLES = ("NODE_NAME", "HUB_ADDRESS", "NODE_PROCESSOR", "WORKER_GPU_BUILD", "NODE_REGISTRY",
                  "NODE_WORKER_IMAGE")
# The images the hub pushes for nodes: Redis hash field -> GPU build.
IMAGES_KEY = "cluster:images"
IMAGE_NAME = "piano-transcription-worker"


class ClusterError(Exception):
    """What a cluster command cannot do; the message says why."""


def check_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not NODE_NAME.match(name) or name == "hub":
        raise ClusterError("A node's name is 1 to 31 lowercase letters, digits, and hyphens, and not hub")
    return name


def check_address(address: str) -> str:
    """A tailnet address: an IP address, as Tailscale gives each device."""
    try:
        return str(ipaddress.ip_address((address or "").strip()))
    except ValueError:
        raise ClusterError(f"{address!r} is not an IP address; give the hub's Tailscale address, "
                           "from `tailscale ip -4` on the hub") from None


def check_tasks(text: str) -> list[str]:
    names = {item.strip().lower() for item in (text or "").split(",") if item.strip()}
    if "all" in names:
        return list(TASKS)
    unknown = sorted(names - set(TASKS))
    if unknown or not names:
        raise ClusterError(f"Give tasks from: {', '.join(TASKS)}, or all")
    return [task for task in TASKS if task in names]


def random_key(length: int = 24) -> str:
    return secrets.token_hex(length)


def write_secret(path: Path, value: str) -> None:
    """A secret file the services read through a bind mount, as user 10001:
    readable, inside a folder of secrets only this user can change."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    partial.write_text(value, encoding="utf-8")
    partial.chmod(0o644)
    os.replace(partial, path)


def write_private(path: Path, text: str) -> None:
    """A file only this user reads, such as an invite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.replace(partial, path)


def read_secret(root: Path, name: str) -> str | None:
    path = root / "secrets" / name
    return path.read_text(encoding="utf-8").strip() if path.is_file() and path.stat().st_size else None


# --- The hub ------------------------------------------------------------------

def seaweed_identity(name: str, access: str, secret: str, admin: bool, bucket: str) -> dict:
    actions = ["Admin", "Read", "Write", "List", "Tagging"] if admin else [
        f"Read:{bucket}", f"Write:{bucket}", f"List:{bucket}", f"Tagging:{bucket}"]
    return {"name": name, "credentials": [{"accessKey": access, "secretKey": secret}], "actions": actions}


def read_s3_config(root: Path) -> dict | None:
    path = root / S3_CONFIG
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_s3_config(root: Path, config: dict) -> None:
    # SeaweedFS reads it as another user, through a bind mount.
    write_secret(root / S3_CONFIG, json.dumps(config, indent=2))


def setup_hub(root: Path, server, address: str, registry: str | None) -> list[str]:
    """Make this machine a hub: .env publishes Redis and the hub's bucket on
    the tailnet address, and the bucket gets the hub's keys. server is
    scripts/server.py, whose EnvFile edits .env and server.env in place."""
    address = check_address(address)
    messages = []
    compose = server.EnvFile(root / ".env")
    compose.set("HUB_ADDRESS", address)
    compose.set("COMPOSE_FILE", HUB_FILES)
    # The same separator on every OS, so the line is portable.
    compose.set("COMPOSE_PATH_SEPARATOR", ":")
    if registry:
        registry = registry.strip().lower().rstrip(".")
        if not HOSTNAME.match(registry):
            raise ClusterError("--registry is the hub's Tailscale name, such as hub.example.ts.net")
        compose.add_profile("registry")
        compose.set("HUB_REGISTRY", registry)
        messages.append(f"the registry for nodes' images: run `tailscale serve --bg --https=443 http://127.0.0.1:5000` "
                        f"on this machine, then nodes pull from {registry}")
    config = read_s3_config(root) or {"identities": []}
    if not any(identity.get("name") == "hub" for identity in config["identities"]) or not read_secret(root, HUB_KEYS[0]):
        access, secret = random_key(10), random_key(20)
        for name, value in zip(HUB_KEYS, (access, secret)):
            write_secret(root / "secrets" / name, value)
        config["identities"] = [identity for identity in config["identities"] if identity.get("name") != "hub"]
        config["identities"].append(seaweed_identity("hub", access, secret, True, HUB_BUCKET))
        write_s3_config(root, config)
        messages.append(f"created the keys of the hub's bucket, in secrets/{HUB_KEYS[0]} and secrets/{HUB_KEYS[1]}")
    if env_values(root / "server.env").get("JOB_STORAGE") == "s3":
        messages.append("job files stay in your STORAGE_ bucket (JOB_STORAGE=s3), which every node gets the keys of; "
                        "with JOB_STORAGE=local they would go to the hub's own bucket, which nodes have keys of their own to")
    else:
        messages.append(f"job files go to the hub's own bucket on {address}:{STORAGE_PORT} (JOB_STORAGE=local); "
                        "results are sent by the API, so the bucket stays private")
    compose.save()
    messages.append(f"Redis and the bucket are published on {address} only, for the nodes; PostgreSQL stays private")
    messages.append("apply it with: python scripts/server.py restart 1m")
    return messages


def invite(root: Path, name: str, tasks: list[str], again: bool = False) -> tuple[Path, dict]:
    """Create a node's password and keys, and its invite file. Returns the
    invite's path and what the hub must do next: reload Redis's users, and
    restart storage when its keys changed."""
    name = check_name(name)
    compose_values = env_values(root / ".env")
    address = compose_values.get("HUB_ADDRESS")
    config = read_s3_config(root)
    if not address or config is None:
        raise ClusterError("This machine is not a hub yet: run python scripts/admin.py nodes hub <its tailnet address>")
    password_file = root / NODE_PASSWORDS / name
    if password_file.exists() and not again:
        raise ClusterError(f"{name} was invited already. Revoke it first, or add --again for a new invite, "
                           "which replaces its password and keys")
    password = random_key()
    write_secret(password_file, password)
    node_secrets = {"redis_password": password}
    # The hub's bucket: keys of the node's own, which revoke removes.
    access, secret = random_key(10), random_key(20)
    config["identities"] = [identity for identity in config["identities"] if identity.get("name") != f"node-{name}"]
    config["identities"].append(seaweed_identity(f"node-{name}", access, secret, False, HUB_BUCKET))
    write_s3_config(root, config)
    node_secrets.update(zip(HUB_KEYS, (access, secret)))
    # The STORAGE_ bucket's keys, which every node then shares: for job files
    # with JOB_STORAGE=s3, and for RunPod's audio with dispatch.
    cloud_job_files = env_values(root / "server.env").get("JOB_STORAGE") == "s3"
    if cloud_job_files or "dispatch" in tasks:
        for key in STORAGE_KEYS:
            value = read_secret(root, key)
            if value:
                node_secrets[key] = value
    if "dispatch" in tasks:
        for key in RUNPOD_KEYS:
            value = read_secret(root, key)
            if value:
                node_secrets[key] = value
    for key in ALWAYS:
        value = read_secret(root, key)
        if value:
            node_secrets[key] = value
    invitation = {
        "format": INVITE_FORMAT,
        "node": name,
        "hub_address": address,
        "tasks": tasks,
        "registry": compose_values.get("HUB_REGISTRY") or None,
        "secrets": node_secrets,
    }
    path = root / INVITES / f"{name}.json"
    write_private(path, json.dumps(invitation, indent=2))
    shared = sorted(key for key in node_secrets if key in STORAGE_KEYS)
    return path, {"storage_changed": True, "shared_keys": shared}


def revoke(root: Path, name: str) -> dict:
    """Remove a node's password, its bucket keys, and any invite left."""
    name = check_name(name)
    removed = []
    for path in (root / NODE_PASSWORDS / name, root / INVITES / f"{name}.json"):
        if path.exists():
            path.unlink()
            removed.append(str(path.relative_to(root)))
    config = read_s3_config(root)
    storage_changed = False
    if config is not None:
        kept = [identity for identity in config["identities"] if identity.get("name") != f"node-{name}"]
        storage_changed = len(kept) != len(config["identities"])
        if storage_changed:
            config["identities"] = kept
            write_s3_config(root, config)
    server_env = env_values(root / "server.env")
    # Nodes got the STORAGE_ bucket's keys for job files or RunPod's audio.
    cloud_keys = server_env.get("JOB_STORAGE") == "s3" or server_env.get("OUTSOURCE_PROVIDER", "none") != "none"
    return {"removed": removed, "storage_changed": storage_changed, "cloud_keys": cloud_keys}


def invited(root: Path) -> list[str]:
    folder = root / NODE_PASSWORDS
    return sorted(path.name for path in folder.iterdir() if path.is_file()) if folder.is_dir() else []


# --- A node -------------------------------------------------------------------

def env_values(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of an env file; comments and blank lines skipped."""
    values = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            values[key.strip()] = value
    return values


def set_env_value(path: Path, name: str, value: str | None) -> None:
    """Set or remove one line of node.env, keeping the others."""
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    kept = [line for line in lines if not re.match(rf"^\s*{re.escape(name)}\s*=", line)]
    if value is not None:
        kept.append(f"{name}={value}")
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")


def is_node(root: Path) -> bool:
    return bool(env_values(root / NODE_ENV).get("NODE_NAME"))


def has_nvidia_gpu() -> bool:
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        return subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def read_invite(path: Path) -> dict:
    try:
        invitation = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ClusterError(f"{path} is not an invite file ({type(error).__name__})") from None
    if not isinstance(invitation, dict) or invitation.get("format") != INVITE_FORMAT:
        raise ClusterError(f"{path} is not an invite of this version; invite the device again on the hub")
    check_name(invitation.get("node", ""))
    check_address(invitation.get("hub_address", ""))
    check_tasks(",".join(invitation.get("tasks") or []))
    if not isinstance(invitation.get("secrets"), dict) or not invitation["secrets"].get("redis_password"):
        raise ClusterError(f"{path} has no Redis password; invite the device again on the hub")
    allowed = {"redis_password", *STORAGE_KEYS, *HUB_KEYS, *RUNPOD_KEYS, *ALWAYS}
    unknown = sorted(set(invitation["secrets"]) - allowed)
    if unknown:
        raise ClusterError(f"{path} holds files no node needs ({', '.join(unknown)}); it was not made by nodes invite")
    registry = invitation.get("registry")
    if registry is not None and not (isinstance(registry, str) and HOSTNAME.match(registry)):
        raise ClusterError(f"{path} names a registry that is not a hostname")
    return invitation


def join(root: Path, invite_path: Path, processor: str | None, legacy: bool) -> list[str]:
    """Make this device a node from its invite: node.env and secrets/node.
    The invite is deleted once both are written."""
    invitation = read_invite(invite_path)
    name = invitation["node"]
    existing = env_values(root / NODE_ENV).get("NODE_NAME")
    if existing and existing != name:
        raise ClusterError(f"This device is the node {existing} already; revoke it on the hub, delete node.env and "
                           "secrets/node here, then join again")
    if (root / ".env").exists() and (root / "billing-data").exists() and not existing:
        raise ClusterError("This checkout runs a hub (it has .env and billing-data); join from another checkout or "
                           "device")
    folder = root / NODE_SECRETS
    if folder.is_dir():
        shutil.rmtree(folder)
    for file_name, value in invitation["secrets"].items():
        write_secret(folder / file_name, str(value))
    folder.chmod(0o755)
    processor = processor or ("gpu" if has_nvidia_gpu() else "cpu")
    env = root / NODE_ENV
    if not env.exists():
        env.write_text(
            "# This device in a home cluster; see docs/cluster.md. scripts/server.py join writes the\n"
            "# first lines. Settings below them override the hub's for this device only, such as\n"
            "# WORKER_MAX_CONCURRENCY=1, or NODE_EGRESS=office to group devices by hand.\n",
            encoding="utf-8",
        )
    set_env_value(env, "NODE_NAME", name)
    set_env_value(env, "HUB_ADDRESS", invitation["hub_address"])
    set_env_value(env, "NODE_PROCESSOR", processor)
    set_env_value(env, "WORKER_GPU_BUILD", "legacy" if legacy else None)
    set_env_value(env, "NODE_REGISTRY", invitation.get("registry"))
    invite_path.unlink()
    messages = [f"this device is the node {name}, for: {', '.join(invitation['tasks'])}; on the {processor}",
                f"wrote node.env and secrets/node; deleted {invite_path.name}"]
    if not reachable(invitation["hub_address"], REDIS_PORT):
        messages.append(f"cannot reach the hub at {invitation['hub_address']}:{REDIS_PORT}: is Tailscale up here and "
                        "on the hub, and is the hub started?")
    return messages


def reachable(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --- Reading the hub's Redis from a node's host, for the registry's images ----------

def resp_command(stream, *parts: str):
    stream.write(f"*{len(parts)}\r\n".encode() + b"".join(
        f"${len(part.encode())}\r\n".encode() + part.encode() + b"\r\n" for part in parts))
    stream.flush()
    return resp_read(stream)


def resp_read(stream):
    line = stream.readline()
    if not line:
        raise ClusterError("the hub's Redis closed the connection")
    kind, rest = line[:1], line[1:].rstrip(b"\r\n")
    if kind == b"+":
        return rest.decode()
    if kind == b"-":
        raise ClusterError(f"the hub's Redis refused: {rest.decode()[:80]}")
    if kind == b":":
        return int(rest)
    if kind == b"$":
        size = int(rest)
        if size < 0:
            return None
        data = stream.read(size + 2)
        return data[:-2].decode()
    if kind == b"*":
        return [resp_read(stream) for _ in range(int(rest))]
    raise ClusterError("unexpected answer from the hub's Redis")


def hub_image(root: Path, variant: str) -> str | None:
    """The worker image the hub pushed for this GPU build, by digest."""
    values = env_values(root / NODE_ENV)
    password = (root / NODE_SECRETS / "redis_password").read_text(encoding="utf-8").strip()
    with socket.create_connection((values["HUB_ADDRESS"], REDIS_PORT), timeout=10) as connection:
        stream = connection.makefile("rwb")
        resp_command(stream, "AUTH", f"node-{values['NODE_NAME']}", password)
        reference = resp_command(stream, "HGET", IMAGES_KEY, f"worker-{variant}")
    if reference and not re.match(r"^[a-z0-9.:-]+/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$", reference):
        raise ClusterError("the hub published an image reference that is not one by digest")
    return reference
