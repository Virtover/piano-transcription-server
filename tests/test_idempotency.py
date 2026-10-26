"""A submission retried with the same Idempotency-Key, for example after the
connection dropped before the response arrived, returns the first job and
reserves minutes only once."""

from pathlib import Path

import pytest

from app import controls
from app.api.routes import transcriptions
from app.worker import tasks

LINK = {"source_url": "https://example.com/video"}


def headers(user="user", key=None):
    values = {"Authorization": f"Bearer {user}"}
    if key is not None:
        values["Idempotency-Key"] = key
    return values


@pytest.fixture
def billed(api_context, monkeypatch):
    client, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 25}
    billing.credit_purchase("user", "starter", "token", "order")
    sent = []
    monkeypatch.setattr(tasks.transcribe_job, "send", lambda *args, **kwargs: sent.append(args))
    monkeypatch.setattr(transcriptions, "probe_audio", lambda path, measure=True, max_seconds=None: 130.0)
    return client, settings, billing, sent


def reserved_minutes(billing):
    return [
        row["reserved_minutes"]
        for row in billing.read("SELECT reserved_minutes FROM transcription_reservations WHERE status = 'reserved'")
    ]


def test_retried_link_returns_the_first_job_and_reserves_once(billed):
    client, _, billing, sent = billed

    first = client.post("/api/transcriptions", json=LINK, headers=headers(key="a1"))
    # The response was lost; the app sends the same submission again.
    retry = client.post("/api/transcriptions", json=LINK, headers=headers(key="a1"))

    assert first.status_code == retry.status_code == 202
    assert retry.json()["job_id"] == first.json()["job_id"]
    assert retry.json()["status"] == "queued"
    assert len(sent) == 1
    assert reserved_minutes(billing) == [2]
    assert retry.json()["minutes"] == first.json()["minutes"] == 23


def test_retry_reports_the_jobs_current_status(billed):
    client, _, _, _ = billed
    job_id = client.post("/api/transcriptions", json=LINK, headers=headers(key="a1")).json()["job_id"]
    tasks.redis.hset(tasks.job_key(job_id), "status", "completed")

    retry = client.post("/api/transcriptions", json=LINK, headers=headers(key="a1"))

    assert retry.json()["status"] == "completed"


def test_different_keys_and_users_get_different_jobs(billed):
    client, _, billing, sent = billed
    billing.credit_purchase("other", "starter", "token-2", "order-2")

    first = client.post("/api/transcriptions", json=LINK, headers=headers(key="a1")).json()["job_id"]
    second = client.post("/api/transcriptions", json=LINK, headers=headers(key="a2")).json()["job_id"]
    other_user = client.post("/api/transcriptions", json=LINK, headers=headers("other", key="a1")).json()["job_id"]

    assert len({first, second, other_user}) == 3
    assert len(sent) == 3


def test_without_a_key_every_request_is_a_new_job(billed):
    client, _, billing, sent = billed

    first = client.post("/api/transcriptions", json=LINK, headers=headers()).json()["job_id"]
    second = client.post("/api/transcriptions", json=LINK, headers=headers()).json()["job_id"]

    assert first != second
    assert reserved_minutes(billing) == [2, 2]


def test_failed_submission_can_be_retried_with_the_same_key(billed, monkeypatch):
    client, _, billing, sent = billed

    def unavailable(*args, **kwargs):
        raise ConnectionError("broker down")

    monkeypatch.setattr(tasks.transcribe_job, "send", unavailable)
    assert client.post("/api/transcriptions", json=LINK, headers=headers(key="a1")).status_code == 503
    monkeypatch.setattr(tasks.transcribe_job, "send", lambda *args, **kwargs: sent.append(args))

    retry = client.post("/api/transcriptions", json=LINK, headers=headers(key="a1"))

    assert retry.status_code == 202
    assert len(sent) == 1
    assert reserved_minutes(billing) == [2]


def test_retry_while_the_first_request_still_runs_is_told_to_wait(billed):
    client, _, _, sent = billed
    job_id = transcriptions.submission_job_id("user", "a1")
    # The first request is still checking the link.
    tasks.redis.hset(tasks.job_key(job_id), mapping={"status": "preparing", "user_id": "user"})

    retry = client.post("/api/transcriptions", json=LINK, headers=headers(key="a1"))

    assert retry.status_code == 409
    assert retry.headers["Retry-After"] == "5"
    assert sent == []


def test_simultaneous_copies_queue_one_job(billed, monkeypatch):
    client, _, billing, sent = billed
    job_id = transcriptions.submission_job_id("user", "a1")
    real_check = transcriptions.earlier_submission
    calls = []

    def first_sees_nothing(job, user):
        calls.append(job)
        if len(calls) == 1:
            # The other copy claims the job between the check and the claim.
            tasks.redis.hset(tasks.job_key(job_id), mapping={"status": "queued", "user_id": "user"})
            return None
        return real_check(job, user)

    monkeypatch.setattr(transcriptions, "earlier_submission", first_sees_nothing)

    response = client.post("/api/transcriptions", json=LINK, headers=headers(key="a1"))

    assert response.status_code == 202
    assert response.json() == {"job_id": job_id, "status": "queued", "minutes": 25}
    assert sent == []
    assert reserved_minutes(billing) == []


def test_expired_submission_is_not_run_again(billed):
    client, _, billing, sent = billed
    job_id = client.post("/api/transcriptions", json=LINK, headers=headers(key="a1")).json()["job_id"]
    billing.settle_success(job_id)
    tasks.redis.delete(tasks.job_key(job_id))

    retry = client.post("/api/transcriptions", json=LINK, headers=headers(key="a1"))

    assert retry.status_code == 409
    assert "already processed" in retry.json()["detail"]
    assert len(sent) == 1


def test_retry_is_answered_while_submissions_are_paused(billed):
    client, _, _, _ = billed
    job_id = client.post("/api/transcriptions", json=LINK, headers=headers(key="a1")).json()["job_id"]
    controls.pause(tasks.redis)

    retry = client.post("/api/transcriptions", json=LINK, headers=headers(key="a1"))

    assert retry.status_code == 202
    assert retry.json()["job_id"] == job_id
    # A new submission is refused.
    assert client.post("/api/transcriptions", json=LINK, headers=headers(key="a2")).status_code == 503


@pytest.mark.parametrize("key", ["", "x" * 201, "line\nbreak"])
def test_invalid_keys_are_refused(billed, key):
    client, _, _, sent = billed

    response = client.post("/api/transcriptions", json=LINK, headers=headers(key=key))

    assert response.status_code == 422
    assert sent == []


def test_retried_upload_returns_the_first_job_without_storing_the_file_again(billed):
    client, settings, billing, sent = billed

    def upload():
        return client.post(
            "/api/transcriptions/upload",
            params={"filename": "Song.mp3"},
            content=b"audio",
            headers={"Content-Type": "application/octet-stream", **headers(key="u1")},
        )

    first = upload()
    retry = upload()

    assert first.status_code == retry.status_code == 202
    assert retry.json()["job_id"] == first.json()["job_id"]
    assert len(sent) == 1
    assert reserved_minutes(billing) == [2]
    assert len(list((Path(settings.service.data_dir) / "jobs").iterdir())) == 1
