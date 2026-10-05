from concurrent.futures import ThreadPoolExecutor


def access_token(client, user_id):
    return client.post("/api/auth/sign-in", json={"google_id_token": user_id}).json()["access_token"]


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

    invalid = client.post(
        "/api/transcriptions",
        headers={"Authorization": f"Bearer {access_token(client, 'short-user')}"},
        json={"source_url": "not-a-url"},
    )
    assert invalid.status_code == 422

    insufficient = client.post(
        "/api/transcriptions",
        headers={"Authorization": f"Bearer {access_token(client, 'short-user')}"},
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
    racer_token = access_token(client, "racer")

    def submit():
        return client.post(
            "/api/transcriptions",
            headers={"Authorization": f"Bearer {racer_token}"},
            json={"source_url": "https://example.com/video"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: submit(), range(2)))

    assert sorted(response.status_code for response in responses) == [202, 402]
    assert client.get(
        "/api/billing/balance",
        headers={"Authorization": f"Bearer {racer_token}"},
    ).json()["minutes"] == 1


def test_billed_job_is_only_visible_to_authenticated_owner(api_context):
    client, settings, _ = api_context
    settings.billing_provider = "google_play"
    settings.free_minutes = 5
    settings.free_minutes_period = "1d"

    created = client.post(
        "/api/transcriptions",
        headers={"Authorization": f"Bearer {access_token(client, 'owner')}"},
        json={"source_url": "https://example.com/video"},
    )
    assert created.status_code == 202

    job_id = created.json()["job_id"]
    response = client.get(
        f"/api/transcriptions/{job_id}",
        headers={"Authorization": f"Bearer {access_token(client, 'other-user')}"},
    )

    assert response.status_code == 404
