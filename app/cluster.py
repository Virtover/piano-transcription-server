"""A home cluster: one hub runs the Compose stack, and nodes, other devices,
run workers that reach the hub's Redis over the tailnet. See docs/cluster.md.

Everything a node and the hub share is in Redis, under `cluster:`:

* `cluster:hub`: what the hub publishes for nodes: the protocol version
  they must speak, the code version, and the server's settings, so a node
  needs no copy of server.env.
* `cluster:nodes`: the nodes the hub invited, by name.
* `cluster:node:<name>:config`: the switches an administrator sets: whether
  the device takes work at all, and which tasks. A node only reads its own.
* `cluster:node:<name>:status:<service>`: what each of its services runs
  now, renewed while it runs and gone soon after it stops.
* `cluster:egress:<name>`: the device's public address, as video sites see
  it, so a rate limit on one address does not stop the others.
* `cluster:settle` and `cluster:record`: jobs a node ended, whose billing and
  diagnostics the hub writes. Nodes never reach the billing database.

The hub's own services are the node `hub`. Without invited nodes nothing
here changes how a single machine behaves.
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Iterable

from redis import Redis

from app.config import COMPOSE_ONLY_VARIABLES, GROUPS, env_name, is_secret, settings


logger = logging.getLogger(__name__)

# Raised when what the hub and the nodes exchange changes, so a node of
# another version waits for an update instead of running jobs wrongly.
PROTOCOL = 1

HUB = "hub"
HUB_KEY = "cluster:hub"
NODES_KEY = "cluster:nodes"
SETTLE_KEY = "cluster:settle"
RECORD_KEY = "cluster:record"
CHECK_REPLY_PREFIX = "cluster:check:"
# A node's name is part of its Redis user and of key patterns in its rules.
NODE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")

# What a device can be allowed to do. transcribe and arrange consume the
# local queues, dispatch sends jobs to RunPod, and check reads a video's
# length for the API when the hub's own address is rate-limited.
TASKS = ("transcribe", "arrange", "dispatch", "check")
TASK_TITLES = {
    "transcribe": "transcriptions",
    "arrange": "piano arrangements",
    "dispatch": "sending jobs to RunPod",
    "check": "checking video links for the hub",
}

# Seconds a status stays without renewal; services renew it every
# STATUS_INTERVAL.
STATUS_TTL = 45
STATUS_INTERVAL = 10
HUB_PUBLISH_INTERVAL = 30
EGRESS_TTL = 10 * 60
EGRESS_CACHE = 60
# Answers with the address the request came from; it learns nothing else.
EGRESS_URL = "https://www.cloudflare.com/cdn-cgi/trace"

# Settings a node never takes from the hub: its own wiring, the database it
# does not use, and what depends on the device itself, which its node.env
# sets if needed. Nor what only the API uses: sign-in, billing, the tunnel,
# the admin page with its administrators' addresses, the off-site backups.
# Nodes see only what their workers need.
NOT_SYNCED_GROUPS = ("service", "database", "node", "auth", "billing", "google_play", "tunnel", "admin_web",
                     "offsite")
DEVICE_SETTINGS = {
    "WORKER_THREADS", "WORKER_ENFORCE_CPU", "WORKER_GPU_RESERVE_GIB", "WORKER_CPU_RESERVE_GIB",
    "WORKER_BATCH_SIZE", "WORKER_MAX_CONCURRENCY",
}
# Set by the launcher: the names it took from the hub, and their hash.
SYNCED_NAMES_VARIABLE = "CLUSTER_SYNCED_SETTINGS"
SYNCED_HASH_VARIABLE = "CLUSTER_SETTINGS_HASH"


def node_name() -> str:
    return settings.node.name


def is_node() -> bool:
    """Whether this process runs on a node rather than on the hub."""
    return settings.service.deployment == "node"


def config_key(node: str) -> str:
    return f"cluster:node:{node}:config"


def status_key(node: str, service: str) -> str:
    return f"cluster:node:{node}:status:{service}"


def egress_key(node: str) -> str:
    return f"cluster:egress:{node}"


def check_reply_key(request: str) -> str:
    return f"{CHECK_REPLY_PREFIX}{request}"


def check_node_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not NODE_NAME.match(name) or name == HUB:
        raise ValueError(
            "A node's name is 1 to 31 lowercase letters, digits, and hyphens, starting with a letter or digit, "
            f"and not {HUB}"
        )
    return name


def parse_tasks(text: str | Iterable[str] | None) -> list[str]:
    """Tasks from a comma-separated list, in TASKS order; ValueError for an
    unknown one. `all` is every task."""
    if text is None:
        return []
    items = text.split(",") if isinstance(text, str) else list(text)
    names = {item.strip().lower() for item in items if item.strip()}
    if "all" in names:
        return list(TASKS)
    unknown = sorted(names - set(TASKS))
    if unknown:
        raise ValueError(f"Unknown task {', '.join(unknown)}; the tasks are {', '.join(TASKS)}")
    return [task for task in TASKS if task in names]


# --- Switches an administrator sets ---------------------------------------------

def node_config(redis: Redis, node: str) -> dict:
    """{"enabled": bool, "tasks": list or None}. tasks None allows every
    task the device offers, as for the hub until a switch is set."""
    stored = redis.hgetall(config_key(node))
    tasks = stored.get("tasks")
    return {
        "enabled": stored.get("enabled", "1") != "0",
        "tasks": None if tasks is None else parse_tasks_lenient(tasks),
    }


def parse_tasks_lenient(text: str) -> list[str]:
    """A stored list: unknown names, as from a newer hub, are ignored."""
    names = {item.strip() for item in text.split(",")}
    return [task for task in TASKS if task in names]


def allowed_tasks(redis: Redis, node: str, offered: Iterable[str]) -> list[str]:
    """What this node's service may run now: what it offers, as far as the
    switches allow."""
    config = node_config(redis, node)
    if not config["enabled"]:
        return []
    return [task for task in offered if config["tasks"] is None or task in config["tasks"]]


def set_enabled(redis: Redis, node: str, enabled: bool) -> None:
    redis.hset(config_key(node), "enabled", "1" if enabled else "0")


def set_tasks(redis: Redis, node: str, tasks: Iterable[str]) -> None:
    redis.hset(config_key(node), "tasks", ",".join(parse_tasks(list(tasks))))


def register(redis: Redis, node: str, tasks: Iterable[str]) -> None:
    """Make an invited node known, with the tasks it may run."""
    with redis.pipeline() as pipe:
        pipe.sadd(NODES_KEY, node)
        pipe.hset(config_key(node), mapping={"enabled": "1", "tasks": ",".join(parse_tasks(list(tasks)))})
        pipe.execute()


def forget(redis: Redis, node: str) -> None:
    """Remove a revoked node and its switches. Its jobs are recovered when
    their leases expire."""
    with redis.pipeline() as pipe:
        pipe.srem(NODES_KEY, node)
        pipe.delete(config_key(node), egress_key(node))
        for key in redis.scan_iter(match=status_key(node, "*")):
            pipe.delete(key)
        pipe.execute()


def known_nodes(redis: Redis) -> list[str]:
    return sorted(redis.smembers(NODES_KEY))


_has_nodes: tuple[float, bool] | None = None


def has_nodes(redis: Redis) -> bool:
    """Whether any node was invited, cached for a minute. Only then does
    anything here change what one machine does."""
    global _has_nodes
    if is_node():
        return True
    now = time.monotonic()
    if _has_nodes is None or now - _has_nodes[0] > EGRESS_CACHE:
        try:
            _has_nodes = (now, bool(redis.scard(NODES_KEY)))
        except Exception:
            return bool(_has_nodes and _has_nodes[1])
    return _has_nodes[1]


# --- Status, published by each service -----------------------------------------------

def publish_status(redis: Redis, node: str, service: str, status: dict) -> None:
    values = {key: json.dumps(value) if not isinstance(value, str) else value for key, value in status.items()}
    values["updated_at"] = str(time.time())
    with redis.pipeline() as pipe:
        pipe.delete(status_key(node, service))
        pipe.hset(status_key(node, service), mapping=values)
        pipe.expire(status_key(node, service), STATUS_TTL)
        pipe.execute()


def clear_status(redis: Redis, node: str, service: str) -> None:
    redis.delete(status_key(node, service))


def _decoded(value: str):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def statuses(redis: Redis) -> list[dict]:
    """Every live service of every node, as published."""
    found = []
    for key in sorted(redis.scan_iter(match="cluster:node:*:status:*")):
        parts = key.split(":")
        if len(parts) != 5:
            continue
        stored = redis.hgetall(key)
        if not stored:
            continue
        status = {name: _decoded(value) for name, value in stored.items()}
        status.update({"node": parts[2], "service": parts[4]})
        try:
            status["updated_at"] = float(status.get("updated_at") or 0)
        except (TypeError, ValueError):
            status["updated_at"] = 0.0
        found.append(status)
    return found


def running_tasks(redis: Redis) -> dict[str, set[str]]:
    """Node -> the tasks its live services run now."""
    found: dict[str, set[str]] = {}
    for status in statuses(redis):
        found.setdefault(status["node"], set()).update(status.get("running") or [])
    return found


# --- What the hub publishes for nodes ------------------------------------------------

_code_version: str | None = None


def code_version() -> str:
    """A short hash of this copy of the server's code: the same on the hub
    and a node built from the same version, also when one was checked out
    on Windows, with its line endings."""
    global _code_version
    if _code_version is None:
        digest = hashlib.sha256()
        root = Path(__file__).resolve().parent
        for path in sorted(root.rglob("*.py"), key=lambda item: item.relative_to(root).as_posix()):
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        _code_version = digest.hexdigest()[:12]
    return _code_version


def synced_names() -> set[str]:
    """The settings a node takes from the hub."""
    names = set()
    for group_name, group in GROUPS.items():
        if group_name in NOT_SYNCED_GROUPS:
            continue
        for field in group.model_fields:
            if not is_secret(group, field):
                names.add(env_name(group, field))
    return names - DEVICE_SETTINGS - COMPOSE_ONLY_VARIABLES


def hub_settings(environment: dict[str, str] | None = None) -> dict[str, str]:
    """The settings this hub runs with, as nodes take them: from its
    environment, which server.env set. Never secrets: they are files."""
    environment = os.environ if environment is None else environment
    names = synced_names()
    return {name: value for name, value in sorted(environment.items()) if name in names and value}


def settings_hash(values: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:12]


def publish_hub(redis: Redis) -> None:
    """Run by the leading API: what nodes need from the hub."""
    values = hub_settings()
    redis.hset(HUB_KEY, mapping={
        "protocol": str(PROTOCOL),
        "code": code_version(),
        "settings": json.dumps(values, sort_keys=True),
        "settings_hash": settings_hash(values),
        "updated_at": str(time.time()),
    })


def hub_info(redis: Redis) -> dict | None:
    stored = redis.hgetall(HUB_KEY)
    if not stored:
        return None
    try:
        values = json.loads(stored.get("settings") or "{}")
    except ValueError:
        values = {}
    try:
        protocol = int(stored.get("protocol") or 0)
    except ValueError:
        protocol = 0
    return {
        "protocol": protocol,
        "code": stored.get("code"),
        "settings": values if isinstance(values, dict) else {},
        "settings_hash": stored.get("settings_hash"),
    }


def apply_hub_settings(environment: dict[str, str], values: dict[str, str]) -> dict[str, str]:
    """The environment a node's services run with: the hub's settings,
    except where the node's own environment (node.env) sets one. Names taken
    from the hub before are replaced, so a setting removed on the hub goes."""
    result = dict(environment)
    for name in result.get(SYNCED_NAMES_VARIABLE, "").split(","):
        if name:
            result.pop(name, None)
    names = synced_names()
    taken = []
    for name, value in values.items():
        if name in names and name not in result:
            result[name] = str(value)
            taken.append(name)
    result[SYNCED_NAMES_VARIABLE] = ",".join(sorted(taken))
    result[SYNCED_HASH_VARIABLE] = settings_hash(values)
    return result


# --- The public address video sites see ------------------------------------------------

_egress_cache: tuple[float, str] | None = None
_egress_lock = threading.Lock()


def detect_public_address(url: str = EGRESS_URL, timeout: float = 5.0) -> str | None:
    """This device's public IP address, or None if it cannot be learned."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            text = response.read(4096).decode("utf-8", "replace")
    except Exception:
        return None
    for line in text.splitlines():
        if line.startswith("ip="):
            return line[3:].strip() or None
    return None


def egress_label(address: str) -> str:
    """The part of video site keys that tells addresses apart. A hash, so
    the keys do not show the address."""
    return hashlib.sha256(address.encode()).hexdigest()[:10]


def egress(redis: Redis) -> str:
    """The label of the address this device reaches video sites from, or ""
    without invited nodes, which keeps one machine's keys as they were.
    NODE_EGRESS names it by hand; auto learns the address, refreshed every
    ten minutes and shared by the device's services through Redis."""
    global _egress_cache
    if not has_nodes(redis):
        return ""
    configured = settings.node.egress.strip()
    if configured and configured.lower() != "auto":
        return egress_label(f"label:{configured}")
    now = time.monotonic()
    with _egress_lock:
        if _egress_cache is not None and now - _egress_cache[0] < EGRESS_CACHE:
            return _egress_cache[1]
        address = None
        try:
            address = redis.get(egress_key(node_name()))
        except Exception:
            pass
        if not address:
            address = detect_public_address()
            if address:
                try:
                    redis.set(egress_key(node_name()), address, ex=EGRESS_TTL)
                except Exception:
                    pass
        # Unknown: the device counts as an address of its own, which at
        # worst moves a job to a node that is refused as well.
        label = egress_label(address) if address else egress_label(f"node:{node_name()}")
        _egress_cache = (now, label)
        return label


def egress_address(redis: Redis, node: str) -> str | None:
    """The public address a node last learned, for administrators."""
    return redis.get(egress_key(node))


def forget_egress_cache() -> None:
    global _egress_cache, _has_nodes
    _egress_cache = None
    _has_nodes = None


def live_egresses(redis: Redis, task: str) -> dict[str, list[str]]:
    """Egress label -> the nodes that run task now from that address."""
    found: dict[str, list[str]] = {}
    for status in statuses(redis):
        if task in (status.get("running") or []) and status.get("egress"):
            found.setdefault(status["egress"], []).append(status["node"])
    return found


# --- Billing for nodes ----------------------------------------------------------------

class HubBilling:
    """What a node's worker does instead of writing the billing database,
    which it never reaches: it names the job, and the leading API settles it
    by the job's state, as it settles jobs after a crash. A node therefore
    cannot set a charge or a refund itself."""

    def __init__(self, redis: Redis):
        self.redis = redis

    def _queue(self, key: str, job_id: str) -> None:
        self.redis.zadd(key, {job_id: time.time()})

    def settle_success(self, job_id: str) -> None:
        self._queue(SETTLE_KEY, job_id)

    def settle_cancellation(self, job_id: str, charged_minutes: int) -> None:
        self._queue(SETTLE_KEY, job_id)

    def release(self, job_id: str) -> None:
        self._queue(SETTLE_KEY, job_id)

    def record_job(self, job_id: str, job: dict[str, str]) -> None:
        self._queue(RECORD_KEY, job_id)


# Jobs still running when a node asked are looked at again this much later;
# the leading API's reconciliation settles whatever remains after a crash.
SETTLE_RETRY = 5.0
SETTLE_BATCH = 200


def process_node_requests(redis: Redis, settle, record, now: float | None = None) -> int:
    """Run by the leading API: settle and record the jobs nodes ended.
    settle(job_id, job) settles one ended job by its state and returns
    whether it was ended; record(job_id) writes its diagnostics."""
    now = time.time() if now is None else now
    done = 0
    for job_id in redis.zrangebyscore(RECORD_KEY, "-inf", now, start=0, num=SETTLE_BATCH):
        try:
            record(job_id)
        finally:
            redis.zrem(RECORD_KEY, job_id)
        done += 1
    for job_id in redis.zrangebyscore(SETTLE_KEY, "-inf", now, start=0, num=SETTLE_BATCH):
        job = redis.hgetall(f"transcription:{job_id}")
        if job and not settle(job_id, job):
            # Asked a moment before its end was written: look again soon.
            redis.zadd(SETTLE_KEY, {job_id: now + SETTLE_RETRY}, xx=True)
            continue
        if not job:
            settle(job_id, {})
        redis.zrem(SETTLE_KEY, job_id)
        done += 1
    return done


# --- python -m app.cluster ----------------------------------------------------------

def print_status(redis: Redis) -> int:
    """This device's switches and services, for scripts/server.py status."""
    node = node_name()
    config = node_config(redis, node)
    hub = hub_info(redis)
    allowed = "all" if config["tasks"] is None else (", ".join(config["tasks"]) or "none")
    print(f"{node}: {'takes work' if config['enabled'] else 'switched off by an administrator'}; allowed: {allowed}")
    if hub is None:
        print("  the hub has not published anything: is its API running?")
    elif hub["protocol"] != PROTOCOL:
        print(f"  the hub speaks protocol {hub['protocol']}, this device {PROTOCOL}: update this device")
    elif hub["code"] != code_version():
        print("  this device runs other code than the hub: update it with python scripts/server.py update")
    mine = [status for status in statuses(redis) if status["node"] == node]
    if not mine:
        print("  no service of this device is running")
    for status in mine:
        running = ", ".join(status.get("running") or []) or "nothing"
        print(f"  {status['service']}: {status.get('state')}; running {running}")
        if status.get("summary"):
            print(f"    {status['summary']}")
        if status.get("problem"):
            print(f"    {status['problem']}")
    return 0


def create_bucket() -> int:
    """Create the hub's bucket for job files, if missing."""
    from app.job_storage import S3JobStorage

    files = S3JobStorage.hub()
    try:
        files.client.head_bucket(Bucket=files.bucket)
        print(f"the hub's bucket {files.bucket} exists")
    except Exception:
        files.client.create_bucket(Bucket=files.bucket)
        print(f"created the hub's bucket {files.bucket}")
    return 0


def main(arguments: list[str]) -> int:
    import sys

    command = arguments[0] if arguments else ""
    if command == "status":
        redis = Redis.from_url(settings.service.redis_url, decode_responses=True, **settings.service.redis_options())
        return print_status(redis)
    if command == "create-bucket":
        return create_bucket()
    print("usage: python -m app.cluster status | create-bucket", file=sys.stderr)
    return 2


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
