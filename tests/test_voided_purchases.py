import sqlite3

import pytest

from app import google_play, main
from app.billing import InsufficientMinutes

DAY_MS = 24 * 60 * 60 * 1000
NOW_MS = 1_800_000_000_000


@pytest.fixture
def billed(api_context):
    _, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 10, "big": 60}
    settings.google_play.package_name = "com.example.app"
    return settings, billing


class VoidedService:
    """A fake Android Publisher service that serves voided purchases in pages."""

    def __init__(self, *pages):
        self.pages = list(pages)
        self.requests = []

    def purchases(self):
        return self

    def voidedpurchases(self):
        return self

    def list(self, **arguments):
        self.requests.append(arguments)
        return self

    def execute(self):
        return self.pages.pop(0)


def voided(*tokens, next_page=None):
    page = {"voidedPurchases": [{"purchaseToken": token, "orderId": f"GPA.{token}", "voidedReason": 1} for token in tokens]}
    if next_page:
        page["tokenPagination"] = {"nextPageToken": next_page}
    return page


# --- Billing ------------------------------------------------------------------

def test_voiding_takes_the_minutes_back(billed):
    _, billing = billed
    billing.credit_purchase("user", "starter", "token-1", "order-1")

    assert billing.void_purchase("token-1") == ("user", 10)
    assert billing.get_balance("user") == 0


def test_balance_goes_below_zero_when_the_minutes_were_used(billed):
    _, billing = billed
    billing.credit_purchase("user", "starter", "token-1", "order-1")
    billing.reserve("user", "job", 8)
    billing.settle_success("job")

    billing.void_purchase("token-1")

    assert billing.get_balance("user") == -8
    with pytest.raises(InsufficientMinutes):
        billing.reserve("user", "job-2", 1)


def test_a_purchase_is_voided_once(billed):
    _, billing = billed
    billing.credit_purchase("user", "starter", "token-1", "order-1")

    billing.void_purchase("token-1")

    assert billing.void_purchase("token-1") is None
    assert billing.get_balance("user") == 0


def test_unknown_token_changes_nothing(billed):
    _, billing = billed
    billing.credit_purchase("user", "starter", "token-1", "order-1")

    assert billing.void_purchase("someone-elses-token") is None
    assert billing.get_balance("user") == 10


def test_voided_purchase_cannot_be_credited_again(billed):
    _, billing = billed
    billing.credit_purchase("user", "starter", "token-1", "order-1")
    billing.void_purchase("token-1")

    assert billing.credit_purchase("user", "starter", "token-1", "order-1") == 0
    assert billing.get_balance("user") == 0


def test_deleted_accounts_purchase_is_marked_without_a_balance(billed):
    settings, billing = billed
    billing.credit_purchase("user", "starter", "token-1", "order-1")
    billing.delete_user("user")

    assert billing.void_purchase("token-1") is None
    with sqlite3.connect(settings.billing.database_path) as connection:
        assert connection.execute("SELECT voided_at IS NOT NULL FROM purchases").fetchone() == (1,)


def test_free_minutes_do_not_pay_off_a_refund(billed):
    settings, billing = billed
    settings.billing.free_minutes = 5
    settings.billing.free_minutes_period = "1d"
    billing.credit_purchase("user", "starter", "token-1", "order-1")
    billing.reserve("user", "job", 10)
    billing.settle_success("job")
    billing.void_purchase("token-1")

    assert billing.grant_free_minutes("user") == 0
    assert billing.get_balance("user") == -10


def test_free_minutes_come_once_the_debt_is_paid(billed):
    # The refused grant did not use up the period.
    settings, billing = billed
    settings.billing.free_minutes = 5
    settings.billing.free_minutes_period = "1d"
    billing.credit_purchase("user", "starter", "token-1", "order-1")
    billing.reserve("user", "job", 10)
    billing.settle_success("job")
    billing.void_purchase("token-1")
    assert billing.grant_free_minutes("user") == 0

    with sqlite3.connect(settings.billing.database_path) as connection:
        connection.execute("UPDATE users SET minutes = 0 WHERE user_id = 'user'")

    assert billing.grant_free_minutes("user") == 5


def test_negative_balance_is_reported(api_context, billed):
    client, _, billing = api_context
    billing.credit_purchase("user", "starter", "token-1", "order-1")
    billing.reserve("user", "job", 4)
    billing.settle_success("job")
    billing.void_purchase("token-1")

    response = client.get("/api/billing/balance", headers={"Authorization": "Bearer user"})

    assert response.json()["minutes"] == -4


# --- Sync from Google Play ----------------------------------------------------

def test_sync_voids_every_page(billed):
    _, billing = billed
    for index in range(3):
        billing.credit_purchase(f"user-{index}", "starter", f"token-{index}", f"order-{index}")
    service = VoidedService(voided("token-0", "token-1", next_page="page-2"), voided("token-2", "unknown"))

    assert google_play.sync_voided_purchases(billing, service, NOW_MS) == 3

    assert [billing.get_balance(f"user-{index}") for index in range(3)] == [0, 0, 0]
    assert "token" not in service.requests[0]
    assert service.requests[1]["token"] == "page-2"
    assert service.requests[0]["packageName"] == "com.example.app"


def test_first_sync_reads_the_last_30_days(billed):
    _, billing = billed
    service = VoidedService(voided())

    google_play.sync_voided_purchases(billing, service, NOW_MS)

    request = service.requests[0]
    assert request["endTime"] == NOW_MS - google_play.VOIDED_CLOCK_MARGIN_MS
    assert NOW_MS - request["startTime"] < 30 * DAY_MS


def test_later_syncs_continue_with_an_overlap(billed):
    _, billing = billed
    google_play.sync_voided_purchases(billing, VoidedService(voided()), NOW_MS)
    service = VoidedService(voided())

    google_play.sync_voided_purchases(billing, service, NOW_MS + 60 * 60 * 1000)

    assert service.requests[0]["startTime"] == (
        NOW_MS - google_play.VOIDED_CLOCK_MARGIN_MS - google_play.VOIDED_OVERLAP_MS
    )


def test_sync_after_a_long_pause_reads_what_google_still_has(billed, caplog):
    _, billing = billed
    google_play.sync_voided_purchases(billing, VoidedService(voided()), NOW_MS)
    service = VoidedService(voided())
    later = NOW_MS + 45 * DAY_MS

    google_play.sync_voided_purchases(billing, service, later)

    assert later - service.requests[0]["startTime"] < 30 * DAY_MS
    assert "more than 30 days ago" in caplog.text


def test_repeated_sync_takes_minutes_back_once(billed):
    _, billing = billed
    billing.credit_purchase("user", "starter", "token-1", "order-1")

    google_play.sync_voided_purchases(billing, VoidedService(voided("token-1")), NOW_MS)
    google_play.sync_voided_purchases(billing, VoidedService(voided("token-1")), NOW_MS + DAY_MS)

    assert billing.get_balance("user") == 0


def test_failed_sync_is_retried_from_the_same_point(billed):
    _, billing = billed
    google_play.sync_voided_purchases(billing, VoidedService(voided()), NOW_MS)

    class Broken(VoidedService):
        def execute(self):
            raise ConnectionError("network down")

    with pytest.raises(ConnectionError):
        google_play.sync_voided_purchases(billing, Broken(), NOW_MS + DAY_MS)
    service = VoidedService(voided())
    google_play.sync_voided_purchases(billing, service, NOW_MS + 2 * DAY_MS)

    assert service.requests[0]["startTime"] == (
        NOW_MS - google_play.VOIDED_CLOCK_MARGIN_MS - google_play.VOIDED_OVERLAP_MS
    )


def test_sync_runs_only_with_configured_google_play(billed, monkeypatch):
    settings, _ = billed
    monkeypatch.setattr(google_play, "is_configured", lambda: True)
    assert main.voided_purchases_enabled()

    monkeypatch.setattr(google_play, "is_configured", lambda: False)
    assert not main.voided_purchases_enabled()

    monkeypatch.setattr(google_play, "is_configured", lambda: True)
    settings.billing.provider = "none"
    assert not main.voided_purchases_enabled()


class StopLoop(Exception):
    pass


def run_sync_loop(monkeypatch, outcomes, interval=3600.0):
    """Runs main.sync_voided_purchases until outcomes run out; returns its waits."""
    waits = []
    outcomes = list(outcomes)

    def sync(billing):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome

    def sleep(seconds):
        waits.append(seconds)
        if not outcomes:
            raise StopLoop

    monkeypatch.setattr(main, "Billing", lambda: None)
    monkeypatch.setattr(main.google_play, "sync_voided_purchases", sync)
    monkeypatch.setattr(main.time, "sleep", sleep)
    monkeypatch.setattr(main.settings.google_play, "voided_purchases_interval", interval)
    with pytest.raises(StopLoop):
        main.sync_voided_purchases()
    return waits


def test_voided_sync_retries_soon_without_a_network(monkeypatch):
    unreachable = OSError(101, "Network is unreachable")
    waits = run_sync_loop(monkeypatch, [unreachable, unreachable, None, unreachable])
    assert waits == [60.0, 120.0, 3600.0, 60.0]


def test_voided_sync_network_retry_never_exceeds_the_interval(monkeypatch):
    waits = run_sync_loop(monkeypatch, [OSError("down")] * 4, interval=200.0)
    assert waits == [60.0, 120.0, 200.0, 200.0]


def test_voided_sync_waits_the_interval_after_other_errors(monkeypatch):
    waits = run_sync_loop(monkeypatch, [ValueError("refused"), None])
    assert waits == [3600.0, 3600.0]


def test_network_errors_are_told_apart():
    assert google_play.is_network_error(OSError(101, "Network is unreachable"))
    assert google_play.is_network_error(TimeoutError())
    assert not google_play.is_network_error(ValueError())
