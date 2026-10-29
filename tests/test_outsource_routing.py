import math
import time

import pytest

from app.outsource import routing
from app.worker import tasks


@pytest.fixture(autouse=True)
def fake_redis(api_context):
    """Every test here uses the in-memory Redis from api_context."""


@pytest.fixture
def outsourcing(api_context):
    _, settings, _ = api_context
    settings.outsource.provider = "runpod"
    settings.outsource.local_first_max_wait = 120
    settings.outsource.max_concurrency = 2
    return settings


def heartbeat(capacity, average=60.0):
    routing.publish_heartbeat(tasks.redis, capacity, average)


def busy(count):
    """Add `count` more active local jobs."""
    start = tasks.redis.scard(routing.ACTIVE_KEYS["local"])
    for index in range(start, start + count):
        tasks.redis.sadd(routing.ACTIVE_KEYS["local"], f"busy-{index}")


def test_wait_estimate():
    heartbeat(capacity=2, average=100)
    assert routing.estimated_local_wait(tasks.redis) == 0

    busy(2)
    assert routing.estimated_local_wait(tasks.redis) == 100
    busy(3)
    assert routing.estimated_local_wait(tasks.redis) == 200


def test_missing_heartbeat_means_no_local_capacity():
    assert routing.local_capacity(tasks.redis) == (0, routing.DEFAULT_JOB_SECONDS)
    assert routing.estimated_local_wait(tasks.redis) == math.inf


def test_heartbeat_expires():
    heartbeat(capacity=4)

    assert 0 < tasks.redis.ttl(routing.heartbeat_key(routing.worker_id())) <= routing.HEARTBEAT_TTL


def test_heartbeat_without_history_uses_default_duration():
    routing.publish_heartbeat(tasks.redis, 3, None)

    assert routing.local_capacity(tasks.redis) == (3, routing.DEFAULT_JOB_SECONDS)


# --- Several local workers ---------------------------------------------------

def test_capacity_of_several_workers_adds_up():
    routing.publish_heartbeat(tasks.redis, 2, 60.0, worker="gpu-a:1")
    routing.publish_heartbeat(tasks.redis, 1, 60.0, worker="gpu-b:1")

    assert routing.local_capacity(tasks.redis) == (3, 60.0)
    busy(3)
    assert routing.estimated_local_wait(tasks.redis) == 60.0


def test_average_job_weighs_workers_by_capacity():
    routing.publish_heartbeat(tasks.redis, 3, 40.0, worker="fast:1")
    routing.publish_heartbeat(tasks.redis, 1, 120.0, worker="slow:1")
    # A worker that has not finished a job yet does not count.
    routing.publish_heartbeat(tasks.redis, 2, None, worker="new:1")

    assert routing.local_capacity(tasks.redis) == (6, 60.0)


def test_a_worker_whose_heartbeat_expired_does_not_count(monkeypatch):
    routing.publish_heartbeat(tasks.redis, 2, 60.0, worker="stopped:1")
    # Its heartbeat key expired; only the list still names it.
    tasks.redis.delete(routing.heartbeat_key("stopped:1"))
    routing.publish_heartbeat(tasks.redis, 1, 60.0, worker="running:1")

    assert routing.local_capacity(tasks.redis) == (1, 60.0)
    assert [worker["id"] for worker in routing.local_workers(tasks.redis)] == ["running:1"]


def test_a_worker_silent_for_longer_than_the_ttl_is_forgotten(monkeypatch):
    now = time.time()
    monkeypatch.setattr(routing.time, "time", lambda: now - routing.HEARTBEAT_TTL - 1)
    routing.publish_heartbeat(tasks.redis, 2, 60.0, worker="silent:1")
    monkeypatch.setattr(routing.time, "time", lambda: now)

    assert routing.local_workers(tasks.redis) == []
    routing.publish_heartbeat(tasks.redis, 1, 60.0, worker="running:1")
    assert tasks.redis.zrange(routing.WORKERS_KEY, 0, -1) == ["running:1"]


def test_a_stopping_worker_clears_only_its_own_heartbeat():
    routing.publish_heartbeat(tasks.redis, 2, 60.0, worker="a:1")
    routing.publish_heartbeat(tasks.redis, 1, 60.0, worker="b:1")

    routing.clear_heartbeat(tasks.redis, worker="a:1")

    assert routing.local_capacity(tasks.redis) == (1, 60.0)


def test_reading_workers_writes_nothing(monkeypatch):
    routing.publish_heartbeat(tasks.redis, 2, 60.0, worker="a:1")
    for command in ("zrem", "zremrangebyscore", "delete", "hset"):
        monkeypatch.setattr(tasks.redis, command, lambda *args, **kwargs: pytest.fail(f"wrote with {command}"))

    assert routing.local_capacity(tasks.redis) == (2, 60.0)


def test_provider_none_always_routes_locally(api_context):
    busy(50)

    assert routing.choose_backend(tasks.redis, "job") == "local"
    assert tasks.redis.sismember(routing.ACTIVE_KEYS["local"], "job")


def test_below_threshold_routes_locally(outsourcing):
    heartbeat(capacity=2, average=100)
    busy(2)

    assert routing.choose_backend(tasks.redis, "job") == "local"


def test_above_threshold_routes_remotely(outsourcing):
    heartbeat(capacity=2, average=100)
    busy(4)

    assert routing.choose_backend(tasks.redis, "job") == "remote"
    assert tasks.redis.sismember(routing.ACTIVE_KEYS["remote"], "job")
    assert not tasks.redis.sismember(routing.ACTIVE_KEYS["local"], "job")


def test_remote_concurrency_cap(outsourcing):
    assert [routing.choose_backend(tasks.redis, f"job-{index}") for index in range(4)] == [
        "remote",
        "remote",
        "local",
        "local",
    ]
    assert tasks.redis.scard(routing.ACTIVE_KEYS["remote"]) == 2


@pytest.mark.parametrize("mode", ["remote_first_overflow", "remote_first_fallback"])
def test_remote_first_modes_do_not_depend_on_local_wait(outsourcing, mode):
    outsourcing.outsource.routing = mode

    assert [routing.choose_backend(tasks.redis, f"job-{index}") for index in range(4)] == (
        ["remote", "remote", "local", "local"]
        if mode == "remote_first_overflow"
        else ["remote", "remote", "remote", "remote"]
    )


def test_remote_first_fallback_waits_for_a_remote_slot(outsourcing):
    outsourcing.outsource.routing = "remote_first_fallback"
    outsourcing.outsource.max_concurrency = 1
    tasks.redis.sadd(routing.ACTIVE_KEYS["remote"], "waiting")
    tasks.redis.sadd(routing.REMOTE_RUNNING_KEY, "running")

    assert routing.choose_backend(tasks.redis, "waiting") == "remote"
    assert not routing.wait_for_remote_slot(tasks.redis, "waiting", lambda: True)
    assert not tasks.redis.sismember(routing.ACTIVE_KEYS["local"], "waiting")


def test_zero_max_wait_outsources_whenever_local_is_full(outsourcing):
    outsourcing.outsource.local_first_max_wait = 0.001
    heartbeat(capacity=1)

    assert routing.choose_backend(tasks.redis, "first") == "local"
    assert routing.choose_backend(tasks.redis, "second") == "remote"


def test_prune_forgets_finished_jobs(api_context):
    tasks.redis.hset(tasks.job_key("running"), "status", "processing")
    tasks.redis.hset(tasks.job_key("done"), "status", "completed")
    for job_id in ("running", "done", "gone"):
        tasks.redis.sadd(routing.ACTIVE_KEYS["local"], job_id)

    assert routing.prune_active(tasks.redis, tasks.job_key) == 2
    assert tasks.redis.smembers(routing.ACTIVE_KEYS["local"]) == {"running"}


def test_restart_recovery_rebuilds_counters_from_queued_jobs(api_context):
    busy(3)
    tasks.redis.sadd(routing.ACTIVE_KEYS["remote"], "remote-job")
    tasks.redis.hset(tasks.job_key("waiting"), mapping={"status": "queued", "backend": "local", "source_url": "u"})

    tasks.recover_active_jobs("restarted")

    assert tasks.redis.smembers(routing.ACTIVE_KEYS["local"]) == {"waiting"}
    assert tasks.redis.scard(routing.ACTIVE_KEYS["remote"]) == 0


# --- API --------------------------------------------------------------------

@pytest.fixture
def sent(monkeypatch):
    calls = {"local": [], "remote": []}
    monkeypatch.setattr(tasks.transcribe_job, "send", lambda *args, **kwargs: calls["local"].append((args, kwargs)))
    monkeypatch.setattr(tasks.transcribe_remote_job, "send", lambda *args, **kwargs: calls["remote"].append((args, kwargs)))
    return calls


def submit(client):
    response = client.post("/api/transcriptions", json={"source_url": "https://example.com/video"})
    assert response.status_code == 202
    return response.json()["job_id"]


def test_api_queues_locally_without_outsourcing(api_context, sent):
    client, _, _ = api_context

    job_id = submit(client)

    assert sent["local"] == [((job_id, "https://example.com/video"), {})]
    assert sent["remote"] == []
    assert client.get(f"/api/transcriptions/{job_id}").json()["backend"] == "local"


def test_api_outsources_when_local_is_overloaded(api_context, outsourcing, sent):
    client, _, _ = api_context
    heartbeat(capacity=1, average=300)
    busy(1)

    job_id = submit(client)

    assert sent["remote"] == [((job_id, "https://example.com/video"), {})]
    assert sent["local"] == []
    status = client.get(f"/api/transcriptions/{job_id}").json()
    assert status["backend"] == "remote"
    assert set(status) >= {"job_id", "status", "progress", "title", "metadata", "error", "minutes"}


def test_api_outsources_uploads_with_their_title(api_context, outsourcing, sent, monkeypatch):
    from app.api.routes import transcriptions

    client, _, _ = api_context
    monkeypatch.setattr(transcriptions, "probe_audio", lambda path, measure=True, max_seconds=None: 130.0)

    response = client.post("/api/transcriptions/upload", params={"filename": "Song.mp3"}, content=b"audio")

    job_id = response.json()["job_id"]
    assert sent["remote"] == [((job_id,), {"upload_title": "Song"})]


def test_api_unavailable_queue_frees_the_slot(api_context, outsourcing, monkeypatch):
    client, _, billing = api_context

    def broken(*args, **kwargs):
        raise ConnectionError("broker down")

    monkeypatch.setattr(tasks.transcribe_remote_job, "send", broken)

    response = client.post("/api/transcriptions", json={"source_url": "https://example.com/video"})

    assert response.status_code == 503
    assert tasks.redis.scard(routing.ACTIVE_KEYS["remote"]) == 0
