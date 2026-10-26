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


def test_server_info_exposes_oauth_client_ids_only_when_billing_is_enabled(api_context):
    client, settings, _ = api_context

    disabled = client.get("/api/billing/server-info")
    assert disabled.status_code == 200
    assert "google_oauth_client_ids" not in disabled.json()

    settings.billing.provider = "google_play"
    enabled = client.get("/api/billing/server-info")
    assert enabled.status_code == 200
    assert enabled.json()["google_oauth_client_ids"] == ["test-client-id"]

    settings.billing.provider = "google_play"
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

    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 5}
    settings.google_play.package_name = "com.example.piano"
    settings.secrets.google_play_service_account = '{"type": "service_account"}'
    google_play.synced_products = {"starter"}

    class FakeRequest:
        def execute(self):
            return {"purchaseState": 0, "orderId": "order-1"}

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
        "google.oauth2.service_account.Credentials.from_service_account_info",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *args, **kwargs: FakeService())

    first = client.post(
        "/api/billing/google-play/verify",
        headers={"Authorization": "Bearer user-1"},
        json={"product_id": "starter", "purchase_token": "token"},
    )
    second = client.post(
        "/api/billing/google-play/verify",
        headers={"Authorization": "Bearer user-1"},
        json={"product_id": "starter", "purchase_token": "token"},
    )

    assert first.status_code == 200
    assert first.json()["credited_minutes"] == 5
    assert first.json()["minutes"] == 5
    assert second.status_code == 200
    assert second.json()["credited_minutes"] == 0
    assert second.json()["minutes"] == 5


def test_users_are_found_by_authenticated_email(api_context):
    _, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 1}
    billing.credit_purchase("user-1", "starter", "purchase-token", "order-1", "user@example.com")

    assert billing.user_id_by_email("USER@EXAMPLE.COM") == "user-1"


def test_google_play_free_grant_is_thread_safe(api_context):
    client, settings, _ = api_context
    settings.billing.provider = "google_play"
    settings.billing.free_minutes = 7
    settings.billing.free_minutes_period = "1d"

    def read_balance():
        return client.get("/api/billing/balance", headers={"Authorization": "Bearer racer"})

    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(lambda _: read_balance(), range(8)))

    assert all(response.status_code == 200 for response in responses)
    assert client.get("/api/billing/balance", headers={"Authorization": "Bearer racer"}).json()["minutes"] == 7


def test_billed_balance_requires_verified_bearer_token(api_context):
    client, settings, _ = api_context
    settings.billing.provider = "google_play"

    missing = client.get("/api/billing/balance")
    assert missing.status_code == 401

    spoofed = client.get(
        "/api/billing/balance",
        headers={"X-User-Id": "spoofed"},
    )
    assert spoofed.status_code == 401


def test_invalid_google_token_is_rejected(api_context):
    client, settings, _ = api_context

    settings.billing.provider = "google_play"
    response = client.get(
        "/api/billing/balance",
        headers={"Authorization": "Bearer invalid"},
    )

    assert response.status_code == 401


def test_google_oauth_client_ids_use_configured_values(api_context):
    _, settings, _ = api_context
    from app import auth

    settings.auth.client_ids = [
        "direct-client-id.apps.googleusercontent.com",
        "another-client-id.apps.googleusercontent.com",
    ]

    assert auth.google_oauth_client_ids() == settings.auth.client_ids


def test_server_info_tells_how_long_results_are_kept(api_context):
    client, settings, _ = api_context

    assert client.get("/api/server-info").json()["result_retention_seconds"] == 60 * 60

    settings.limits.result_retention = "7d"
    assert client.get("/api/server-info").json()["result_retention_seconds"] == 7 * 24 * 60 * 60
