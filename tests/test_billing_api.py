import types
from concurrent.futures import ThreadPoolExecutor

import pytest


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


class FakeRequest:
    def __init__(self, result):
        self.result = result

    def execute(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def concurrent_update():
    from googleapiclient.errors import HttpError

    return HttpError(
        types.SimpleNamespace(status=409, reason="Conflict"),
        b'{"error": {"errors": [{"reason": "concurrentUpdate"}]}}',
    )


class FakeProducts:
    """Google Play's purchases of one product: get answers purchase, updated
    by later_states one call at a time; consume answers consume_results."""

    def __init__(self, purchase, consume_results=None):
        self.purchase = dict(purchase)
        self.consume_results = list(consume_results or [{}])
        self.consumed = 0
        self.later_states = []

    def get(self, **kwargs):
        if self.later_states:
            self.purchase.update(self.later_states.pop(0))
        return FakeRequest(dict(self.purchase))

    def consume(self, **kwargs):
        result = self.consume_results.pop(0) if self.consume_results else {}
        if not isinstance(result, Exception):
            self.consumed += 1
        return FakeRequest(result)


def google_play_service(monkeypatch, settings, purchase, products=None):
    """Google Play reporting purchase for every token. Returns its products."""
    from app import google_play
    from app.api.routes import billing as billing_routes

    monkeypatch.setattr(billing_routes.time, "sleep", lambda seconds: None)

    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 5}
    settings.google_play.package_name = "com.example.piano"
    settings.secrets.google_play_service_account = '{"type": "service_account"}'
    google_play.synced_products = {"starter"}
    products = products or FakeProducts(purchase)
    service = types.SimpleNamespace(purchases=lambda: types.SimpleNamespace(products=lambda: products))
    monkeypatch.setattr(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *args, **kwargs: service)
    return products


def verify(client, token="token"):
    return client.post(
        "/api/billing/google-play/verify",
        headers={"Authorization": "Bearer user-1"},
        json={"product_id": "starter", "purchase_token": token},
    )


COMPLETED = {"purchaseState": 0, "orderId": "GPA.1", "consumptionState": 0}


def test_consuming_waits_out_a_concurrent_update(api_context, monkeypatch):
    client, settings, _ = api_context
    products = FakeProducts(COMPLETED, [concurrent_update(), concurrent_update(), {}])
    google_play_service(monkeypatch, settings, COMPLETED, products)

    response = verify(client)

    assert response.status_code == 200
    assert response.json()["consumed"] is True
    assert response.json()["credited_minutes"] == 5
    assert products.consumed == 1


def test_a_purchase_consumed_meanwhile_counts_as_consumed(api_context, monkeypatch):
    client, settings, _ = api_context
    # A duplicate request consumed it while this one waited.
    products = FakeProducts(COMPLETED, [concurrent_update()])
    products.later_states = [{"consumptionState": 1}]
    google_play_service(monkeypatch, settings, COMPLETED, products)

    response = verify(client)

    assert response.json()["consumed"] is True
    assert products.consumed == 0


def test_consuming_gives_up_after_its_attempts(api_context, monkeypatch, caplog):
    from app.api.routes import billing as billing_routes

    client, settings, _ = api_context
    attempts = [concurrent_update() for _ in range(billing_routes.CONSUME_ATTEMPTS)]
    google_play_service(monkeypatch, settings, COMPLETED, FakeProducts(COMPLETED, attempts))

    response = verify(client)

    # Credited all the same; the app sends it again later, which consumes it.
    assert response.status_code == 200
    assert response.json()["consumed"] is False
    assert response.json()["minutes"] == 5
    assert "Could not consume Google Play purchase" in caplog.text


def test_other_consume_errors_are_not_retried(api_context, monkeypatch):
    client, settings, _ = api_context
    products = FakeProducts(COMPLETED, [RuntimeError("forbidden"), {}])
    google_play_service(monkeypatch, settings, COMPLETED, products)

    assert verify(client).json()["consumed"] is False
    assert products.consumed == 0


def test_an_already_consumed_purchase_is_not_consumed_again(api_context, monkeypatch):
    client, settings, _ = api_context
    products = google_play_service(monkeypatch, settings, {**COMPLETED, "consumptionState": 1})

    response = verify(client)

    assert response.json()["consumed"] is True
    assert products.consumed == 0


@pytest.mark.parametrize("product, purchase, logged", [
    ("other", {"purchaseState": 0, "orderId": "GPA.1"}, "product other is not in BILLING_PRODUCTS"),
    ("starter", {"purchaseState": 2, "orderId": "GPA.2", "consumptionState": 0},
     "not completed (orderId=GPA.2, purchaseState=2, consumptionState=0"),
])
def test_a_refused_purchase_logs_why(api_context, monkeypatch, caplog, product, purchase, logged):
    client, settings, _ = api_context
    google_play_service(monkeypatch, settings, purchase)

    response = client.post(
        "/api/billing/google-play/verify",
        headers={"Authorization": "Bearer user-1"},
        json={"product_id": product, "purchase_token": "secret-token-value"},
    )

    assert response.status_code == 400
    assert logged in caplog.text
    # Only the start of the token, which cannot be used to claim the purchase.
    assert "secret-t" in caplog.text and "secret-token-value" not in caplog.text


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
