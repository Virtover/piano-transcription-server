from pathlib import Path

import pytest
from fastapi import HTTPException

from app import job_limits
from app.api.routes import billing as billing_routes
from app.api.routes import transcriptions
from app.worker import tasks

URL = "https://youtube.com/watch?v=1"


def submit(client, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/api/transcriptions", json={"source_url": URL}, headers=headers)


def finish(job_id, status="completed"):
    tasks.redis.hset(tasks.job_key(job_id), "status", status)


@pytest.fixture
def billed(api_context):
    _, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 100}
    for user in ("alice", "bob"):
        billing.credit_purchase(user, "starter", f"token-{user}", f"order-{user}")
    return settings


def test_total_limit(api_context):
    client, settings, _ = api_context
    settings.limits.max_active_jobs = 2

    first = submit(client)
    assert submit(client).status_code == 202
    full = submit(client)

    assert full.status_code == 429
    assert full.headers["Retry-After"] == "60"
    assert "server is busy" in full.json()["detail"]
    # A finished job gives its place back.
    finish(first.json()["job_id"])
    assert submit(client).status_code == 202


def test_per_user_limit_with_billing(api_context, billed):
    client, _, _ = api_context
    billed.limits.max_active_jobs_per_user = 1

    assert submit(client, "alice").status_code == 202
    second = submit(client, "alice")

    assert second.status_code == 429
    assert "at most 1 transcriptions" in second.json()["detail"]
    assert submit(client, "bob").status_code == 202


def test_refused_user_does_not_take_a_total_place(api_context, billed):
    client, _, _ = api_context
    billed.limits.max_active_jobs = 2
    billed.limits.max_active_jobs_per_user = 1

    assert submit(client, "alice").status_code == 202
    assert submit(client, "alice").status_code == 429

    assert submit(client, "bob").status_code == 202


def test_without_billing_only_the_total_applies(api_context):
    client, settings, _ = api_context
    settings.limits.max_active_jobs_per_user = 1

    assert submit(client).status_code == 202
    assert submit(client).status_code == 202


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_ended_jobs_give_their_place_back(api_context, status):
    client, settings, _ = api_context
    settings.limits.max_active_jobs = 1
    job_id = submit(client).json()["job_id"]

    finish(job_id, status)

    assert submit(client).status_code == 202


def test_expired_jobs_give_their_place_back(api_context):
    client, settings, _ = api_context
    settings.limits.max_active_jobs = 1
    job_id = submit(client).json()["job_id"]

    tasks.redis.delete(tasks.job_key(job_id))

    assert submit(client).status_code == 202


def test_place_is_held_while_the_video_is_checked(api_context, monkeypatch):
    client, settings, _ = api_context
    settings.limits.max_active_jobs = 1
    seen = {}

    def check_video(url):
        [key] = [key for key in tasks.redis.scan_iter("transcription:*")]
        seen["status"] = tasks.redis.hget(key, "status")
        seen["second"] = submit(client).status_code
        return billing_routes.CheckedVideo(130.0, {"title": "Nocturne", "duration": 130}, 0)

    monkeypatch.setattr(transcriptions, "check_video", check_video)

    assert submit(client).status_code == 202
    assert seen == {"status": "preparing", "second": 429}


def test_failed_video_check_gives_the_place_back(api_context, monkeypatch):
    client, settings, _ = api_context
    settings.limits.max_active_jobs = 1

    def check_video(url):
        raise HTTPException(status_code=422, detail="Unable to read video metadata")

    monkeypatch.setattr(transcriptions, "check_video", check_video)

    assert submit(client).status_code == 422
    assert list(tasks.redis.scan_iter("transcription:*")) == []
    assert tasks.redis.scard(job_limits.ALL_JOBS_KEY) == 0


def test_insufficient_minutes_gives_the_place_back(api_context, billed):
    client, _, billing = api_context
    billed.limits.max_active_jobs_per_user = 1
    billed.billing.products = {"starter": 100}

    response = client.post(
        "/api/transcriptions", json={"source_url": URL}, headers={"Authorization": "Bearer carol"}
    )

    assert response.status_code == 402
    assert tasks.redis.scard(job_limits.user_jobs_key("carol")) == 0
    assert list(tasks.redis.scan_iter("transcription:*")) == []


def test_uploads_count_and_full_server_refuses_before_reading(api_context, monkeypatch):
    client, settings, _ = api_context
    settings.limits.max_active_jobs = 1
    seen = {}

    def probe_audio(path, measure=True, max_seconds=None):
        seen["url_job"] = submit(client).status_code
        return 130.0

    monkeypatch.setattr(transcriptions, "probe_audio", probe_audio)

    def upload():
        return client.post(
            "/api/transcriptions/upload",
            params={"filename": "song.mp3"},
            content=b"audio",
            headers={"Content-Type": "application/octet-stream"},
        )

    assert upload().status_code == 202
    assert seen["url_job"] == 429
    refused = upload()
    assert refused.status_code == 429
    # Only the accepted upload's directory exists.
    assert len(list((Path(settings.service.data_dir) / "jobs").iterdir())) == 1


def test_deleting_an_account_forgets_its_places(api_context, billed):
    client, _, _ = api_context
    submit(client, "alice")

    client.delete("/api/account", headers={"Authorization": "Bearer alice"})

    assert not tasks.redis.exists(job_limits.user_jobs_key("alice"))


def test_racing_requests_never_both_take_the_last_place(api_context):
    _, settings, _ = api_context
    settings.limits.max_active_jobs = 1
    for job_id in ("a", "b"):
        tasks.redis.hset(tasks.job_key(job_id), "status", "preparing")
    # "a" took the last place: "b" is refused and removes only itself.
    tasks.redis.sadd(job_limits.ALL_JOBS_KEY, "a")

    with pytest.raises(job_limits.TooManyJobs):
        job_limits.acquire(tasks.redis, "b", "anonymous", tasks.job_key)
    assert tasks.redis.smembers(job_limits.ALL_JOBS_KEY) == {"a"}
