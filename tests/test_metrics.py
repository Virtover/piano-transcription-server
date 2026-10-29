import time

import pytest

from app import metrics
from app.outsource import routing
from app.worker import tasks


@pytest.fixture
def token(api_context):
    _, settings, _ = api_context
    settings.secrets.metrics_token = "scrape-token"
    return "scrape-token"


def scrape(client, token="scrape-token"):
    return client.get("/metrics", headers={"Authorization": f"Bearer {token}"})


def samples(text: str) -> dict[str, float]:
    return {
        line.rsplit(" ", 1)[0]: float(line.rsplit(" ", 1)[1])
        for line in text.splitlines()
        if line and not line.startswith("#")
    }


def add_job(job_id, backend, status, queued_at=None):
    fields = {"status": status}
    if queued_at is not None:
        fields["queued_at"] = str(queued_at)
    tasks.redis.hset(tasks.job_key(job_id), mapping=fields)
    tasks.redis.sadd(routing.ACTIVE_KEYS[backend], job_id)


def test_without_the_token_secret_there_are_no_metrics(api_context):
    client, _, _ = api_context

    assert scrape(client).status_code == 404


def test_a_wrong_token_is_refused(api_context, token):
    client, _, _ = api_context

    assert scrape(client, "guess").status_code == 401
    assert client.get("/metrics").status_code == 401


def test_the_server_password_does_not_hide_the_metrics(api_context, token):
    client, settings, _ = api_context
    settings.secrets.server_password = "a-long-server-password"

    assert scrape(client).status_code == 200


def test_queues_report_waiting_and_running_jobs_and_the_oldest_wait(api_context, token):
    client, _, _ = api_context
    now = time.time()
    add_job("a", "local", "queued", now - 90)
    add_job("b", "local", "queued", now - 30)
    add_job("c", "local", "processing", now - 300)
    add_job("d", "remote", "queued", now - 5)
    routing.publish_heartbeat(tasks.redis, 2, 60.0, worker="gpu:1")

    response = scrape(client)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    found = samples(response.text)
    assert found['piano_queue_jobs{queue="default",backend="local"}'] == 2
    assert 89 <= found['piano_queue_oldest_job_age_seconds{queue="default",backend="local"}'] <= 92
    assert found['piano_jobs_running{queue="default",backend="local"}'] == 1
    assert found['piano_queue_jobs{queue="remote",backend="remote"}'] == 1
    assert found["piano_local_workers"] == 1
    assert found["piano_local_worker_capacity"] == 2
    assert found["piano_video_checks_limit"] == 4
    # This scrape itself.
    assert found["piano_api_requests_in_flight"] == 1
    assert "# TYPE piano_queue_jobs gauge" in response.text


def test_empty_queues_wait_zero(api_context):
    assert metrics.job_queues(tasks.redis)["local"] == {"waiting": 0, "running": 0, "oldest_wait_seconds": 0.0}


def test_a_job_queued_before_the_time_was_recorded_counts_without_a_wait(api_context):
    add_job("old", "local", "queued")

    assert metrics.job_queues(tasks.redis)["local"] == {"waiting": 1, "running": 0, "oldest_wait_seconds": 0.0}


def test_a_submitted_job_records_when_it_was_queued(api_context):
    client, _, _ = api_context

    response = client.post("/api/transcriptions", json={"source_url": "https://example.com/video"})

    queued_at = float(tasks.redis.hget(tasks.job_key(response.json()["job_id"]), "queued_at"))
    assert abs(queued_at - time.time()) < 5


def test_a_job_moved_to_another_backend_waits_from_the_move(api_context, monkeypatch):
    monkeypatch.setattr(tasks.transcribe_remote_job, "send", lambda *args, **kwargs: None)
    tasks.redis.hset(tasks.job_key("job"), mapping={"status": "processing", "queued_at": "1"})

    assert tasks.requeue_job("job", "local", "remote", "out of GPU memory")

    assert float(tasks.redis.hget(tasks.job_key("job"), "queued_at")) > time.time() - 5


def test_status_shows_the_waits(api_context, capsys):
    from app.admin.__main__ import main

    add_job("a", "local", "queued", time.time() - 120)

    main(["status"])

    assert "Waiting       local 1, the oldest for 2 m; remote 0" in capsys.readouterr().out
