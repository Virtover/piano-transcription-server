"""A node's Redis rules (redis/node.acl), on a real Redis: fakeredis does not
enforce users. Starts redis:7-alpine with redis/start.sh, as the hub does,
so it needs Docker; run with PIANO_DOCKER_TESTS=1 (docs/testing.md).

A node must be able to do everything a worker does, Dramatiq's queues
included, and nothing that changes the server's controls, another node's
switches, or Redis itself."""

import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from redis import Redis
from redis.exceptions import NoPermissionError, ResponseError

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    os.environ.get("PIANO_DOCKER_TESTS") != "1" or shutil.which("docker") is None,
    reason="needs Docker: set PIANO_DOCKER_TESTS=1",
)

PASSWORDS = {"operator": "operator-pw", "app": "app-pw", "admin-read-only": "ro-pw", "admin-read-write": "rw-pw"}
NODES = {"laptop": "laptop-pw", "desktop": "desktop-pw"}


@pytest.fixture(scope="module")
def redis_port(tmp_path_factory):
    secrets = tmp_path_factory.mktemp("secrets")
    (secrets / "nodes").mkdir()
    for name, password in PASSWORDS.items():
        (secrets / name).write_text(password, encoding="utf-8")
    for name, password in NODES.items():
        (secrets / "nodes" / name).write_text(password, encoding="utf-8")
    # A file whose name is no node's is skipped, never made a user.
    (secrets / "nodes" / "bad name").write_text("x", encoding="utf-8")
    name = f"piano-acl-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::6379",
         "-v", f"{ROOT / 'redis'}:/redis:ro", "-v", f"{secrets}:/run/secrets/redis:ro",
         "--entrypoint", "/bin/sh", "redis:7-alpine", "/redis/start.sh", "redis-server"],
        check=True, capture_output=True,
    )
    try:
        port = None
        for _ in range(60):
            mapped = subprocess.run(["docker", "port", name, "6379/tcp"], capture_output=True, text=True)
            if mapped.returncode == 0 and mapped.stdout.strip():
                port = int(mapped.stdout.strip().splitlines()[0].rsplit(":", 1)[1])
                try:
                    Redis(port=port, username="operator", password=PASSWORDS["operator"]).ping()
                    break
                except Exception:
                    pass
            time.sleep(0.5)
        else:
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
            pytest.fail(f"Redis did not start: {logs.stdout} {logs.stderr}")
        yield port
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def node(port, name="laptop", decode=True):
    return Redis(port=port, username=f"node-{name}", password=NODES[name], decode_responses=decode)


def refused(call):
    with pytest.raises((NoPermissionError, ResponseError)) as error:
        call()
    text = str(error.value).lower()
    # DEBUG is also refused by the server's own configuration.
    assert "noperm" in text or "no permissions" in text or "command not allowed" in text


def test_a_node_does_what_a_worker_does(redis_port):
    from app import cluster
    from app.outsource import routing
    from app.slots import LeasedSlot

    client = node(redis_port)
    app = Redis(port=redis_port, username="app", password=PASSWORDS["app"], decode_responses=True)
    app.hset("transcription:job", mapping={"status": "queued"})
    cluster.register(app, "laptop", ["transcribe"])
    cluster.publish_hub(app)

    # Jobs, leases, routing, heartbeats, video sites, remote capacity.
    with client.pipeline() as pipe:
        pipe.watch("transcription:job")
        assert pipe.hget("transcription:job", "status") == "queued"
        pipe.multi()
        pipe.hset("transcription:job", "status", "processing")
        pipe.set("transcription-lease:job", "token", ex=60)
        pipe.execute()
    routing.publish_heartbeat(client, 2, 30.0, worker="laptop:1")
    routing.mark_active(client, "local", "job")
    assert routing.local_capacity(client)[0] == 2
    slot = LeasedSlot(client, "video_sites:youtube.com@x:downloads", 2)
    assert slot.try_take()
    slot.release()
    client.hset("outsource:remote:capacity", "jobs_per_worker", 12)
    client.sadd("jobs:running:remote", "job")

    # Its own part of the cluster's keys.
    assert cluster.node_config(client, "laptop")["tasks"] == ["transcribe"]
    assert cluster.hub_info(client)["protocol"] == cluster.PROTOCOL
    cluster.publish_status(client, "laptop", "worker", {"running": ["transcribe"]})
    assert [status["node"] for status in cluster.statuses(client)] == ["laptop"]
    client.set(cluster.egress_key("laptop"), "203.0.113.7", ex=600)
    cluster.HubBilling(client).settle_success("job")
    cluster.HubBilling(client).record_job("job", {})
    client.rpush(cluster.check_reply_key("abc"), "{}")
    assert client.hget(cluster.IMAGES_KEY if hasattr(cluster, "IMAGES_KEY") else "cluster:images", "x") is None


def test_dramatiq_queues_work_for_a_node(redis_port):
    from dramatiq.brokers.redis import RedisBroker
    from dramatiq.message import Message

    broker = RedisBroker(client=node(redis_port, decode=False), namespace="dramatiq")
    broker.declare_queue("default")
    message = Message(queue_name="default", actor_name="transcribe_job", args=("job",), kwargs={}, options={})
    broker.enqueue(message)
    broker.enqueue(message.copy(message_id=str(uuid.uuid4())), delay=10)

    consumer = broker.consume("default", prefetch=1, timeout=1000)
    received = next(item for item in consumer if item is not None)
    assert received.message_id == message.message_id
    consumer.ack(received)
    consumer.close()


def test_a_node_cannot_change_the_servers_controls_or_redis(redis_port):
    client = node(redis_port)

    refused(lambda: client.set("controls:pause", "x"))
    refused(lambda: client.get("controls:pause"))
    refused(lambda: client.set("maintenance:notice", "x"))
    refused(lambda: client.set("workers:api:leader", "laptop"))
    refused(lambda: client.set("admin_web:confirm:x", "x"))
    refused(lambda: client.hset("cluster:node:laptop:config", "enabled", "1"))
    refused(lambda: client.hset("cluster:node:desktop:status:worker", "state", "x"))
    refused(lambda: client.get("cluster:node:desktop:config"))
    refused(lambda: client.set(f"cluster:egress:desktop", "x"))
    refused(lambda: client.hset("cluster:hub", "protocol", "0"))
    refused(lambda: client.hset("cluster:images", "worker-current", "evil/image@sha256:" + "0" * 64))
    refused(lambda: client.sadd("cluster:nodes", "evil"))
    refused(lambda: client.flushall())
    refused(lambda: client.config_get("*"))
    refused(lambda: client.acl_list())
    refused(lambda: client.execute_command("FUNCTION", "FLUSH"))
    refused(lambda: client.execute_command("SCRIPT", "FLUSH"))
    refused(lambda: client.execute_command("DEBUG", "SLEEP", "0"))
    refused(lambda: client.publish("channel", "x"))
    # A script cannot reach what the node itself cannot.
    refused(lambda: client.eval("return redis.call('set', KEYS[1], 'x')", 1, "controls:pause"))
    refused(lambda: client.eval("return redis.call('set', 'controls:pause', 'x')", 0))


def test_only_node_names_become_users(redis_port):
    operator = Redis(port=redis_port, username="operator", password=PASSWORDS["operator"], decode_responses=True)

    users = operator.acl_users()

    assert {"node-laptop", "node-desktop"} <= set(users)
    assert not any(" " in user or user == "node-bad name" for user in users)


def test_a_node_signs_in_with_its_own_password_only(redis_port):
    with pytest.raises(Exception):
        Redis(port=redis_port, username="node-laptop", password=NODES["desktop"]).ping()
    with pytest.raises(Exception):
        Redis(port=redis_port).ping()
