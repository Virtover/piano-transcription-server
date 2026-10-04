from concurrent.futures import ThreadPoolExecutor


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
    settings.billing_provider = "google_play"
    settings.free_minutes = 1
    settings.free_minutes_period = "1d"

    invalid = client.post("/api/transcriptions", json={"source_url": "not-a-url"})
    assert invalid.status_code == 422

    insufficient = client.post(
        "/api/transcriptions",
        headers={"X-User-Id": "short-user"},
        json={"source_url": "https://example.com/video"},
    )
    assert insufficient.status_code == 402
    assert insufficient.json()["detail"] == {
        "message": "Insufficient transcription minutes",
        "required_minutes": 2,
    }


def test_concurrent_transcriptions_cannot_over_reserve_balance(api_context):
    client, settings, billing = api_context
    settings.billing_provider = "google_play"
    settings.billing_products = {"starter": 3}
    settings.free_minutes = 0
    billing.credit_purchase("racer", "starter", "unique-token")

    def submit():
        return client.post(
            "/api/transcriptions",
            headers={"X-User-Id": "racer"},
            json={"source_url": "https://example.com/video"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: submit(), range(2)))

    assert sorted(response.status_code for response in responses) == [202, 402]
    assert client.get("/api/billing/balance", headers={"X-User-Id": "racer"}).json()["minutes"] == 1
