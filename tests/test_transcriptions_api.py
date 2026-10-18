from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.api.routes import transcriptions
from app.worker import tasks


def test_transcription_lifecycle_and_invalid_jobs(api_context):
    client, _, _ = api_context

    missing = client.get("/api/transcriptions/missing")
    assert missing.status_code == 404

    created = client.post(
        "/api/transcriptions",
        json={"source_url": "https://example.com/video"},
    )
    assert created.status_code == 202
    job_id = created.json()["job_id"]
    assert created.json()["status"] == "queued"

    status = client.get(f"/api/transcriptions/{job_id}")
    assert status.status_code == 200
    assert status.json()["status"] == "queued"
    assert status.json()["progress"] == 0.0

    cancelled = client.delete(f"/api/transcriptions/{job_id}")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    repeated = client.delete(f"/api/transcriptions/{job_id}")
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "cancelled"

    midi = client.get(f"/api/transcriptions/{job_id}/midi")
    assert midi.status_code == 409


def test_transcription_validation_and_insufficient_balance(api_context):
    client, settings, _ = api_context
    settings.billing.provider = "google_play"
    settings.billing.free_minutes = 1
    settings.billing.free_minutes_period = "1d"

    invalid = client.post(
        "/api/transcriptions",
        headers={"Authorization": "Bearer short-user"},
        json={"source_url": "not-a-url"},
    )
    assert invalid.status_code == 422

    insufficient = client.post(
        "/api/transcriptions",
        headers={"Authorization": "Bearer short-user"},
        json={"source_url": "https://example.com/video"},
    )
    assert insufficient.status_code == 402
    assert insufficient.json()["detail"] == {
        "message": "Insufficient transcription minutes",
        "required_minutes": 2,
    }


def test_concurrent_transcriptions_cannot_over_reserve_balance(api_context):
    client, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 3}
    settings.billing.free_minutes = 0
    billing.credit_purchase("racer", "starter", "unique-token", "order-1")

    def submit():
        return client.post(
            "/api/transcriptions",
            headers={"Authorization": "Bearer racer"},
            json={"source_url": "https://example.com/video"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: submit(), range(2)))

    assert sorted(response.status_code for response in responses) == [202, 402]
    assert client.get(
        "/api/billing/balance",
        headers={"Authorization": "Bearer racer"},
    ).json()["minutes"] == 1


def test_billed_job_is_only_visible_to_authenticated_owner(api_context):
    client, settings, _ = api_context
    settings.billing.provider = "google_play"
    settings.billing.free_minutes = 5
    settings.billing.free_minutes_period = "1d"

    created = client.post(
        "/api/transcriptions",
        headers={"Authorization": "Bearer owner"},
        json={"source_url": "https://example.com/video"},
    )
    assert created.status_code == 202

    job_id = created.json()["job_id"]
    response = client.get(
        f"/api/transcriptions/{job_id}",
        headers={"Authorization": "Bearer other-user"},
    )

    assert response.status_code == 404


def complete_job(job_id, result):
    tasks.redis.hset(tasks.job_key(job_id), mapping={"status": "completed", "result": str(result)})


def create_job(client, headers=None):
    response = client.post(
        "/api/transcriptions",
        headers=headers or {},
        json={"source_url": "https://example.com/video"},
    )
    assert response.status_code == 202
    return response.json()["job_id"]


def test_completed_midi_can_be_downloaded(api_context):
    client, settings, _ = api_context
    job_id = create_job(client)
    result = Path(settings.service.data_dir) / "jobs" / job_id / "transcription.mid"
    result.parent.mkdir(parents=True)
    result.write_bytes(b"MThd-data")
    complete_job(job_id, result)

    response = client.get(f"/api/transcriptions/{job_id}/midi")

    assert response.status_code == 200
    assert response.content == b"MThd-data"
    assert response.headers["content-type"] == "audio/midi"
    assert "transcription.mid" in response.headers["content-disposition"]


def test_midi_outside_data_dir_is_refused(api_context, tmp_path):
    client, _, _ = api_context
    job_id = create_job(client)
    outside = tmp_path / "secret.mid"
    outside.write_bytes(b"secret")
    complete_job(job_id, outside)

    response = client.get(f"/api/transcriptions/{job_id}/midi")

    assert response.status_code == 500
    assert response.json()["detail"] == "Invalid result path"


def test_missing_midi_result(api_context):
    client, settings, _ = api_context
    job_id = create_job(client)
    complete_job(job_id, Path(settings.service.data_dir) / "jobs" / job_id / "transcription.mid")

    assert client.get(f"/api/transcriptions/{job_id}/midi").status_code == 404
    assert client.get("/api/transcriptions/unknown/midi").status_code == 404


def test_completed_job_cannot_be_cancelled(api_context):
    client, settings, _ = api_context
    job_id = create_job(client)
    complete_job(job_id, Path(settings.service.data_dir) / "result.mid")

    assert client.delete(f"/api/transcriptions/{job_id}").status_code == 409
    assert client.delete("/api/transcriptions/unknown").status_code == 404


def test_status_includes_metadata_and_clamps_progress(api_context):
    client, _, _ = api_context
    job_id = create_job(client)
    tasks.update_job(
        job_id,
        status="processing",
        progress="1.7",
        title="Clair de Lune",
        metadata={"title": "Clair de Lune", "duration": 300, "view_count": 12},
    )

    status = client.get(f"/api/transcriptions/{job_id}").json()

    assert status["progress"] == 1
    assert status["title"] == "Clair de Lune"
    assert status["metadata"]["duration"] == 300
    assert status["minutes"] is None


def test_invalid_metadata_is_ignored(api_context):
    client, _, _ = api_context
    job_id = create_job(client)
    tasks.update_job(job_id, progress="not-a-number", metadata="{broken")

    status = client.get(f"/api/transcriptions/{job_id}").json()

    assert status["progress"] == 0
    assert status["metadata"] is None


def test_submissions_are_rejected_until_startup_finishes(api_context):
    client, _, _ = api_context
    transcriptions.transcription_submissions_enabled = False

    response = client.post("/api/transcriptions", json={"source_url": "https://example.com/video"})

    assert response.status_code == 503


def test_unavailable_worker_releases_reservation(api_context, monkeypatch):
    client, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 5}
    billing.credit_purchase("user", "starter", "token", "order")

    def broken_send(*args, **kwargs):
        raise ConnectionError("broker is down")

    monkeypatch.setattr(tasks.transcribe_job, "send", broken_send)
    response = client.post(
        "/api/transcriptions",
        headers={"Authorization": "Bearer user"},
        json={"source_url": "https://example.com/video"},
    )

    assert response.status_code == 503
    assert list(tasks.redis.scan_iter("transcription:*")) == []
    assert billing.get_balance("user") == 5


def test_billed_cancellation_charges_partial_cost(api_context):
    client, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 10}
    billing.credit_purchase("user", "starter", "token", "order")
    headers = {"Authorization": "Bearer user"}

    created = client.post(
        "/api/transcriptions",
        headers=headers,
        json={"source_url": "https://example.com/video"},
    )
    assert created.json()["minutes"] == 8

    job_id = created.json()["job_id"]
    tasks.update_job(job_id, status="processing", progress="0.5")
    cancelled = client.delete(f"/api/transcriptions/{job_id}", headers=headers)

    assert cancelled.status_code == 200
    # 130 s costs 2 minutes; cancelling at 50 % charges ceil(0.6 * 2 * 0.5) = 1.
    assert cancelled.json()["minutes"] == 9
    assert client.get(f"/api/transcriptions/{job_id}", headers=headers).json()["minutes"] == 9


def test_billed_job_actions_are_hidden_from_other_users(api_context):
    client, settings, _ = api_context
    settings.billing.provider = "google_play"
    settings.billing.free_minutes = 5
    settings.billing.free_minutes_period = "1d"
    job_id = create_job(client, {"Authorization": "Bearer owner"})
    other = {"Authorization": "Bearer other-user"}

    assert client.delete(f"/api/transcriptions/{job_id}", headers=other).status_code == 404
    assert client.get(f"/api/transcriptions/{job_id}/midi", headers=other).status_code == 404
    assert client.get(f"/api/transcriptions/{job_id}").status_code == 401
