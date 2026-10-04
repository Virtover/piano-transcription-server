from concurrent.futures import ThreadPoolExecutor


def test_none_provider_exposes_balance_and_rejects_google_play(api_context):
    client, settings, _ = api_context

    balance = client.get("/api/billing/balance")
    assert balance.status_code == 200
    assert balance.json() == {
        "user_id": "anonymous",
        "minutes": 0,
        "free_minutes_seconds_until_next_grant": None,
        "free_minutes_next_grant_at": None,
    }

    response = client.post(
        "/api/billing/google-play/verify",
        json={"product_id": "starter", "purchase_token": "token"},
    )
    assert response.status_code == 409

    settings.billing_provider = "google_play"
    response = client.get("/api/billing/balance")
    assert response.status_code == 401


def test_cost_endpoint_validates_duration_and_calculates_minutes(api_context, monkeypatch):
    client, _, _ = api_context
    from app.api.routes import billing as billing_routes

    monkeypatch.setattr(billing_routes, "video_duration", lambda source_url: 130.0)
    response = client.get("/api/billing/cost", params={"source_url": "https://example.com/video"})

    assert response.status_code == 200
    assert response.json() == {"duration_seconds": 130.0, "cost_minutes": 2}


def test_google_play_purchase_is_idempotent(api_context, monkeypatch):
    client, settings, _ = api_context
    from app import google_play

    settings.billing_provider = "google_play"
    settings.billing_products = {"starter": 5}
    settings.google_play_package_name = "com.example.piano"
    settings.google_play_service_account_file = "service-account.json"
    google_play.synced_products = {"starter"}

    class FakeRequest:
        def execute(self):
            return {"purchaseState": 0}

    class FakeProducts:
        def get(self, **kwargs):
            return FakeRequest()

        def consume(self, **kwargs):
            return FakeRequest()

    class FakePurchases:
        def products(self):
            return FakeProducts()

    class FakeService:
        def purchases(self):
            return FakePurchases()

    monkeypatch.setattr(
        "google.oauth2.service_account.Credentials.from_service_account_file",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *args, **kwargs: FakeService())

    first = client.post(
        "/api/billing/google-play/verify",
        headers={"X-User-Id": "user-1"},
        json={"product_id": "starter", "purchase_token": "token"},
    )
    second = client.post(
        "/api/billing/google-play/verify",
        headers={"X-User-Id": "user-1"},
        json={"product_id": "starter", "purchase_token": "token"},
    )

    assert first.status_code == 200
    assert first.json()["credited_minutes"] == 5
    assert first.json()["minutes"] == 5
    assert second.status_code == 200
    assert second.json()["credited_minutes"] == 0
    assert second.json()["minutes"] == 5


def test_google_play_free_grant_is_thread_safe(api_context):
    client, settings, _ = api_context
    settings.billing_provider = "google_play"
    settings.free_minutes = 7
    settings.free_minutes_period = "1d"

    def read_balance():
        return client.get("/api/billing/balance", headers={"X-User-Id": "racer"})

    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(lambda _: read_balance(), range(8)))

    assert all(response.status_code == 200 for response in responses)
    assert client.get("/api/billing/balance", headers={"X-User-Id": "racer"}).json()["minutes"] == 7
