import sys

import pytest

from app import admin_credit
from app.billing import InsufficientMinutes, cancelled_cost, period_seconds, transcription_cost


@pytest.fixture
def billed(api_context):
    _, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 10}
    return settings, billing


@pytest.mark.parametrize(
    ("seconds", "minutes"),
    [(1, 1), (60, 1), (119, 1), (120, 2), (179, 2), (180, 3), (1800, 30)],
)
def test_transcription_cost(seconds, minutes):
    assert transcription_cost(seconds) == minutes


@pytest.mark.parametrize(
    ("full_cost", "progress", "charged"),
    [(10, 0.0, 0), (10, 0.049, 0), (10, 0.05, 1), (10, 0.5, 3), (10, 1.0, 6), (1, 0.5, 1)],
)
def test_cancelled_cost(full_cost, progress, charged):
    assert cancelled_cost(full_cost, progress) == charged


def test_billing_is_inactive_without_provider(api_context):
    _, _, billing = api_context

    assert billing.reserve("anonymous", "job", 5) is None
    assert billing.get_balance("anonymous") == 0
    assert billing.grant_free_minutes("anonymous") == 0


def test_reserve_settle_and_release(billed):
    _, billing = billed
    billing.credit_purchase("user", "starter", "token", "order")

    assert billing.reserve("user", "job-1", 4) == 6
    assert billing.reserve("user", "job-2", 3) == 3
    assert billing.get_balance("user") == 3
    with pytest.raises(InsufficientMinutes):
        billing.reserve("user", "job-3", 4)

    billing.settle_success("job-1")
    billing.release("job-2")

    assert billing.get_balance("user") == 6
    assert billing.reserved_job_ids() == []


def test_settlement_happens_only_once(billed):
    _, billing = billed
    billing.credit_purchase("user", "starter", "token", "order")
    billing.reserve("user", "job", 4)

    billing.settle_success("job")
    billing.settle_success("job")
    billing.settle_cancellation("job", 1)
    billing.release("job")

    assert billing.get_balance("user") == 6


def test_cancellation_charge_is_capped_by_reservation(billed):
    _, billing = billed
    billing.credit_purchase("user", "starter", "token", "order")
    billing.reserve("user", "too-much", 2)
    billing.reserve("user", "negative", 2)

    billing.settle_cancellation("too-much", 50)
    billing.settle_cancellation("negative", -5)

    assert billing.get_balance("user") == 8


def test_free_minutes_top_up_once_per_period(billed):
    settings, billing = billed
    settings.billing.free_minutes = 5
    settings.billing.free_minutes_period = "1d"
    billing.credit_purchase("user", "starter", "token", "order")
    billing.reserve("user", "job", 8)
    billing.settle_success("job")

    assert billing.grant_free_minutes("user") == 3
    assert billing.grant_free_minutes("user") == 0
    assert billing.get_balance("user") == 5
    status = billing.free_minutes_status("user")
    assert 0 < status["free_minutes_seconds_until_next_grant"] <= 24 * 60 * 60


def test_free_minutes_never_exceed_the_configured_amount(billed):
    settings, billing = billed
    settings.billing.free_minutes = 5
    billing.credit_purchase("user", "starter", "token", "order")

    assert billing.grant_free_minutes("user") == 0
    assert billing.get_balance("user") == 10


def test_free_minutes_period_defaults_to_30_days(api_context):
    _, settings, _ = api_context
    settings.billing.free_minutes_period = None

    assert period_seconds() == 30 * 24 * 60 * 60


def test_unknown_email_cannot_be_credited(billed):
    _, billing = billed

    with pytest.raises(ValueError, match="nobody@example.com"):
        billing.credit_minutes_by_email(" Nobody@Example.com ", 5)


def test_admin_credit_command(billed, monkeypatch, capsys):
    _, billing = billed
    billing.credit_purchase("user", "starter", "token", "order", "user@example.com")
    monkeypatch.setattr(admin_credit, "Billing", lambda: billing)
    monkeypatch.setattr(sys, "argv", ["admin_credit", "--user", "USER@example.com", "--minutes", "7"])

    admin_credit.main()

    assert "Credited 7 minutes to user@example.com" in capsys.readouterr().out
    assert billing.get_balance("user") == 17


@pytest.mark.parametrize(
    "argv",
    [
        ["--user", "user@example.com", "--minutes", "0"],
        ["--user", "missing@example.com", "--minutes", "5"],
    ],
)
def test_admin_credit_command_rejects_invalid_input(billed, monkeypatch, argv):
    _, billing = billed
    monkeypatch.setattr(admin_credit, "Billing", lambda: billing)
    monkeypatch.setattr(sys, "argv", ["admin_credit", *argv])

    with pytest.raises(SystemExit) as error:
        admin_credit.main()
    assert error.value.code == 2


def test_database_is_created_on_first_use_not_at_construction(tmp_path):
    from app.billing import Billing

    path = tmp_path / "missing" / "billing.sqlite3"
    billing = Billing(str(path))

    # Constructing touches nothing: modules create a Billing at import, also
    # where the billing directory is not available.
    assert not path.parent.exists()

    assert billing.reserved_job_ids() == []
    assert path.exists()
