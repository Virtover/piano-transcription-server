"""Subscriptions, welcome minutes, and minutes for every account.

The billing tests run on SQLite, and also on a real PostgreSQL when
PIANO_TEST_POSTGRES_URL names an empty database the tests may wipe, such as
`postgresql://postgres:test@localhost:55432/postgres`."""

import os
import time
import types

import pytest
from pydantic import ValidationError

from app.billing import (
    DELETED_USER_ID,
    FairUseLimit,
    InsufficientMinutes,
    SubscriptionInfo,
    SubscriptionOwned,
    account_tag,
)
from app.config import BillingSettings, SubscriptionPlan

DAY = 24 * 60 * 60
POSTGRES_URL = os.environ.get("PIANO_TEST_POSTGRES_URL")


def plans():
    return {
        "sub_pro": SubscriptionPlan(name="Pro", minutes=100, base_plan="pro-monthly"),
        "sub_unlimited": SubscriptionPlan(name="Unlimited", minutes="unlimited", base_plan="unlimited-monthly"),
    }


@pytest.fixture(params=["sqlite"] + (["postgres"] if POSTGRES_URL else []))
def subs(request, api_context, monkeypatch):
    """Billing with free minutes, one product, and two subscription plans,
    on each database there is."""
    client, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 10}
    settings.billing.subscriptions = plans()
    settings.billing.free_minutes = 10
    settings.billing.free_minutes_period = "30d"
    settings.billing.unlimited_daily_minutes = 60
    if request.param == "postgres":
        import psycopg

        from app.api.routes import billing as billing_routes
        from app.api.routes import transcriptions as transcription_routes
        from app.billing import Billing
        from app.worker import tasks

        with psycopg.connect(POSTGRES_URL, autocommit=True) as connection:
            connection.execute("DROP SCHEMA public CASCADE")
            connection.execute("CREATE SCHEMA public")
            # As in a new database: every role may use it.
            connection.execute("GRANT USAGE ON SCHEMA public TO PUBLIC")
        settings.database.url = POSTGRES_URL
        billing = Billing()
        for module in (billing_routes, transcription_routes, tasks):
            monkeypatch.setattr(module, "billing", billing)
    return client, settings, billing


def sub(token="token-1", product="sub_pro", state="ACTIVE", days=30, order="GPA.1", **extra):
    return SubscriptionInfo(
        token=token,
        product_id=product,
        state=state,
        expires_at=int(time.time()) + int(days * DAY),
        auto_renewing=extra.pop("auto_renewing", True),
        latest_order_id=order,
        **extra,
    )


def write(billing, query, parameters=()):
    with billing._connect() as connection:
        connection.execute(query, parameters)


def balances(billing, user_id="user"):
    return billing.balances(user_id)


def assert_ledger_adds_up(billing, user_id="user"):
    """Each pool of the ledger adds up to its balance."""
    user = billing.read("SELECT minutes, monthly_minutes FROM users WHERE user_id = ?", (user_id,))[0]
    pools = {
        row["pool"]: int(row["total"])
        for row in billing.read(
            "SELECT pool, SUM(minutes) AS total FROM minute_ledger WHERE user_id = ? GROUP BY pool", (user_id,)
        )
    }
    assert (pools.get("paid", 0), pools.get("monthly", 0)) == (user["minutes"], user["monthly_minutes"])


def end_subscription_period(billing, user_id="user"):
    """Make the user's running subscription period one that has ended."""
    now = int(time.time())
    write(
        billing,
        "UPDATE subscription_periods SET starts_at = starts_at - ?, ends_at = ? WHERE user_id = ? AND ends_at > ?",
        (40 * DAY, now - 1, user_id, now),
    )
    write(billing, "UPDATE users SET free_grant_at = free_grant_at - ? WHERE user_id = ?", (40 * DAY, user_id))


# --- Settings ------------------------------------------------------------------------------------

def test_plans_need_distinct_names_and_minutes():
    with pytest.raises(ValidationError, match="same name"):
        BillingSettings(_env_file=None, subscriptions={"a": {"name": "Pro", "minutes": 100}, "b": {"name": " pro ", "minutes": 200}})
    with pytest.raises(ValidationError, match="both give 100 minutes"):
        BillingSettings(_env_file=None, subscriptions={"a": {"name": "Pro", "minutes": 100}, "b": {"name": "Max", "minutes": 100}})
    with pytest.raises(ValidationError, match="both give unlimited minutes"):
        BillingSettings(_env_file=None,
            subscriptions={"a": {"name": "Pro", "minutes": "unlimited"}, "b": {"name": "Max", "minutes": "unlimited"}}
        )
    with pytest.raises(ValidationError, match="in both BILLING_PRODUCTS and BILLING_SUBSCRIPTIONS"):
        BillingSettings(_env_file=None, products={"a": 10}, subscriptions={"a": {"name": "Pro", "minutes": 100}})
    with pytest.raises(ValidationError):
        BillingSettings(_env_file=None, subscriptions={"a": {"name": "Pro"}})
    with pytest.raises(ValidationError):
        BillingSettings(_env_file=None, subscriptions={"a": {"name": "Pro", "minutes": 0}})
    with pytest.raises(ValidationError):
        BillingSettings(_env_file=None, subscriptions={"a": {"name": "  ", "minutes": 5}})
    with pytest.raises(ValidationError):
        BillingSettings(_env_file=None, subscriptions={"a": {"name": "Pro", "minutes": 5, "price": 3}})


def test_levels_follow_minutes_with_unlimited_highest():
    settings = BillingSettings(_env_file=None, subscriptions={
        "max": {"name": "Max", "minutes": "unlimited"},
        "big": {"name": "Big", "minutes": 300},
        "small": {"name": "Small", "minutes": 50},
    })
    assert settings.subscription_levels() == {"small": 1, "big": 2, "max": 3}
    assert settings.subscriptions["max"].unlimited


def test_subscriptions_are_read_from_the_environment(monkeypatch):
    from app import config

    monkeypatch.setenv(
        "BILLING_SUBSCRIPTIONS",
        '{"sub_pro":{"name":"Pro","minutes":100,"base_plan":"pro-monthly"},'
        '"sub_unlimited":{"name":"Unlimited","minutes":"unlimited"}}',
    )
    monkeypatch.setenv("WELCOME_MINUTES", "15")
    loaded = config.load_settings(env_file=None, secrets_dir=None)
    assert loaded.billing.subscriptions["sub_pro"].minutes == 100
    assert loaded.billing.subscriptions["sub_unlimited"].minutes is None
    assert loaded.billing.welcome_minutes == 15


# --- Periods ----------------------------------------------------------------------------------------

def test_a_subscription_replaces_the_free_minutes(subs):
    _, _, billing = subs
    billing.renew_monthly_minutes("user")
    assert balances(billing)["monthly_minutes"] == 10

    result = billing.record_subscription("user", sub())

    assert result == {"status": "started", "minutes": 100, "user_id": "user"}
    assert balances(billing) == {"minutes": 100, "monthly_minutes": 100, "paid_minutes": 0}
    status = billing.subscription_status("user")
    assert status["subscription"]["name"] == "Pro"
    assert status["subscription"]["level"] == 1
    assert status["subscription"]["auto_renewing"] is True
    assert status["unlimited"] is False
    assert_ledger_adds_up(billing)


def test_an_order_gives_its_minutes_once(subs):
    _, _, billing = subs
    billing.record_subscription("user", sub())
    billing.reserve("user", "job-1", 30)
    billing.settle_success("job-1")

    again = billing.record_subscription("user", sub())

    assert again["status"] == "recorded"
    assert balances(billing)["monthly_minutes"] == 70
    assert_ledger_adds_up(billing)


def test_free_renewal_waits_while_a_subscription_period_runs(subs):
    _, _, billing = subs
    billing.record_subscription("user", sub())
    # The free schedule would renew now, but the subscription's period runs.
    write(billing, "UPDATE users SET free_grant_at = free_grant_at - ?", (40 * DAY,))

    assert billing.renew_monthly_minutes("user") == 0
    assert balances(billing)["monthly_minutes"] == 100


def test_a_renewal_starts_a_new_period_and_unused_minutes_expire(subs):
    _, _, billing = subs
    billing.record_subscription("user", sub(order="GPA.1"))
    billing.reserve("user", "job-1", 40)
    billing.settle_success("job-1")

    renewed = billing.record_subscription("user", sub(order="GPA.1..0", days=60))

    assert renewed["minutes"] == 100
    assert balances(billing)["monthly_minutes"] == 100
    unused = billing.read("SELECT unused_minutes FROM subscription_periods WHERE order_id = 'GPA.1'")[0]
    assert unused["unused_minutes"] == 60
    assert_ledger_adds_up(billing)


def test_a_running_job_keeps_its_minutes_across_a_renewal(subs):
    _, _, billing = subs
    billing.record_subscription("user", sub(order="GPA.1"))
    billing.reserve("user", "job-1", 30)

    billing.record_subscription("user", sub(order="GPA.1..0", days=60))
    # 30 still held by the job, beside the new period's 100.
    assert balances(billing) == {"minutes": 100, "monthly_minutes": 100, "paid_minutes": 0}
    billing.settle_success("job-1")

    assert balances(billing)["monthly_minutes"] == 100
    assert_ledger_adds_up(billing)


def test_an_unrenewed_subscription_falls_back_to_free_minutes(subs):
    _, _, billing = subs
    billing.record_subscription("user", sub())
    end_subscription_period(billing)

    assert billing.renew_monthly_minutes("user") == 10

    assert balances(billing)["monthly_minutes"] == 10
    assert billing.subscription_status("user")["subscription"] is None
    assert billing.read("SELECT unused_minutes FROM subscription_periods")[0]["unused_minutes"] == 100
    # The next free period follows the schedule from the subscription's end.
    assert billing.renew_monthly_minutes("user") == 0
    assert_ledger_adds_up(billing)


def test_a_cancelled_subscription_keeps_its_period_until_it_ends(subs):
    _, _, billing = subs
    billing.record_subscription("user", sub())

    billing.record_subscription("user", sub(state="CANCELED", auto_renewing=False))

    status = billing.subscription_status("user")["subscription"]
    assert status["auto_renewing"] is False
    assert status["state"] == "CANCELED"
    assert balances(billing)["monthly_minutes"] == 100


def test_a_grace_period_keeps_the_plan_but_gives_nothing_until_paid(subs):
    _, _, billing = subs
    billing.record_subscription("user", sub(days=1))

    # Google tried to renew; the payment failed. latestSuccessfulOrderId
    # stays the paid one, and the expiry moves to the grace period's end.
    grace = billing.record_subscription("user", sub(state="IN_GRACE_PERIOD", days=3))

    assert grace["status"] == "recorded"
    period = billing.read("SELECT ends_at FROM subscription_periods")[0]
    assert period["ends_at"] >= int(time.time()) + 2 * DAY
    assert balances(billing)["monthly_minutes"] == 100

    # Paid at last: a new order.
    assert billing.record_subscription("user", sub(order="GPA.1..0", days=30))["minutes"] == 100


@pytest.mark.parametrize("state", ["ON_HOLD", "PAUSED", "EXPIRED", "PENDING"])
def test_a_subscription_without_payment_gives_nothing(subs, state):
    _, _, billing = subs
    billing.renew_monthly_minutes("user")

    result = billing.record_subscription("user", sub(state=state, days=-1 if state != "PENDING" else 30))

    assert result["minutes"] == 0
    assert billing.subscription_status("user")["subscription"] is None
    assert balances(billing)["monthly_minutes"] == 10


def test_an_account_hold_ends_the_running_period(subs):
    _, _, billing = subs
    billing.record_subscription("user", sub())

    billing.record_subscription("user", sub(state="ON_HOLD", days=-0.01))

    assert billing.subscription_status("user")["subscription"] is None
    # Its minutes expire with the period, and free ones come instead.
    billing.renew_monthly_minutes("user")
    assert balances(billing)["monthly_minutes"] == 10
    assert_ledger_adds_up(billing)


def test_an_upgrade_replaces_the_old_subscription(subs):
    _, _, billing = subs
    billing.record_subscription("user", sub(token="old", order="GPA.1"))

    upgraded = billing.record_subscription(
        "user", sub(token="new", product="sub_unlimited", order="GPA.2", linked_token="old")
    )

    assert upgraded["status"] == "started"
    status = billing.subscription_status("user")
    assert status["unlimited"] is True
    assert status["subscription"]["level"] == 2
    assert status["unlimited_minutes_left"] == 60
    old = billing.read("SELECT replaced_by FROM subscriptions WHERE purchase_token = 'old'")[0]
    assert old["replaced_by"] is not None
    # A late renewal report of the replaced one changes nothing.
    assert billing.record_subscription("user", sub(token="old", order="GPA.1..0"))["status"] == "recorded"
    assert billing.subscription_status("user")["unlimited"] is True
    assert_ledger_adds_up(billing)


def test_a_subscription_belongs_to_one_account(subs):
    _, _, billing = subs
    billing.record_subscription("user", sub())

    with pytest.raises(SubscriptionOwned):
        billing.record_subscription("other", sub())
    with pytest.raises(SubscriptionOwned):
        billing.record_subscription("other", sub(token="token-2", account_tag=account_tag("user")))
    assert billing.subscription_status("other")["subscription"] is None


def test_an_unconfigured_product_gives_nothing(subs):
    _, _, billing = subs
    assert billing.record_subscription("user", sub(product="sub_retired"))["minutes"] == 0
    assert billing.subscription_status("user")["subscription"] is None


def test_a_deleted_account_gets_its_subscription_back_when_made_again(subs):
    _, _, billing = subs
    billing.record_subscription("user", sub(account_tag=account_tag("user")))
    billing.delete_user("user")
    assert billing.read("SELECT user_id FROM subscriptions")[0]["user_id"] == DELETED_USER_ID

    result = billing.record_subscription("user", sub(account_tag=account_tag("user")))

    # Its order was given once, to the deleted account; the plan comes back.
    assert result["status"] == "recorded"
    assert billing.subscription_status("user")["subscription"]["name"] == "Pro"
    # Another person cannot take it.
    with pytest.raises(SubscriptionOwned):
        billing.record_subscription("thief", sub(account_tag=account_tag("user")))


def test_subscriptions_to_check_back_off_after_expiry(subs):
    _, _, billing = subs
    billing.record_subscription("user", sub(token="far", days=40))
    billing.record_subscription("user", sub(token="soon", order="GPA.2", days=0.001))
    now = int(time.time())

    assert billing.subscriptions_to_check(now + 120) == ["soon"]
    billing.mark_subscription_checked("soon")
    assert billing.subscriptions_to_check(now + 30) == []
    # Long expired: read again at most every six hours, and for 90 days.
    assert billing.subscriptions_to_check(now + 30 * DAY) == ["soon"]
    assert billing.subscriptions_to_check(now + 140 * DAY) == []
    billing.end_subscription("soon")
    assert billing.subscriptions_to_check(now + 30 * DAY) == []


# --- Unlimited ---------------------------------------------------------------------------------

def test_unlimited_jobs_are_not_charged_up_to_the_fair_use_limit(subs):
    _, _, billing = subs
    billing.record_subscription("user", sub(product="sub_unlimited"))

    billing.reserve("user", "job-1", 40)
    billing.settle_success("job-1")
    with pytest.raises(FairUseLimit) as refused:
        billing.reserve("user", "job-2", 30)

    assert 0 < refused.value.retry_after <= DAY
    assert balances(billing) == {"minutes": 0, "monthly_minutes": 0, "paid_minutes": 0}
    assert billing.subscription_status("user")["unlimited_minutes_left"] == 20
    billing.reserve("user", "job-3", 20)


def test_released_and_cancelled_unlimited_jobs_count_as_far_as_they_ran(subs):
    _, _, billing = subs
    billing.record_subscription("user", sub(product="sub_unlimited"))
    billing.reserve("user", "job-1", 50)
    billing.release("job-1")
    billing.reserve("user", "job-2", 50)
    billing.settle_cancellation("job-2", 10)

    assert billing.subscription_status("user")["unlimited_minutes_left"] == 50
    # A released job can be queued again.
    billing.reserve("user", "job-1", 50)
    with pytest.raises(ValueError):
        billing.reserve("user", "job-1", 5)


def test_the_fair_use_window_is_24_hours(subs):
    _, _, billing = subs
    billing.record_subscription("user", sub(product="sub_unlimited"))
    billing.reserve("user", "job-1", 60)
    write(billing, "UPDATE unlimited_usage SET at = at - ?", (DAY + 1,))

    billing.reserve("user", "job-2", 60)


def test_after_unlimited_ends_jobs_are_charged_again(subs):
    _, _, billing = subs
    billing.record_subscription("user", sub(product="sub_unlimited"))
    end_subscription_period(billing)

    with pytest.raises(InsufficientMinutes):
        billing.reserve("user", "job-1", 30)
    billing.reserve("user", "job-2", 10)
    assert balances(billing)["minutes"] == 0


def test_a_developer_is_not_limited_by_fair_use(subs):
    _, _, billing = subs
    billing.record_subscription("user", sub(product="sub_unlimited"))
    billing.set_developer("user", True, "admin")
    for job in range(5):
        billing.reserve("user", f"job-{job}", 60)


# --- Refunds ------------------------------------------------------------------------------------

def test_a_refunded_running_order_takes_its_minutes_back(subs):
    _, _, billing = subs
    billing.credit_purchase("user", "starter", "product-token", "GPA.P")
    billing.record_subscription("user", sub(order="GPA.1"))
    billing.reserve("user", "job-1", 70)
    billing.settle_success("job-1")

    assert billing.void_purchase("token-1", "GPA.1") == ("user", 100)

    # 30 unused from the monthly balance, the 70 used from the paid one.
    assert balances(billing) == {"minutes": -60, "monthly_minutes": 0, "paid_minutes": -60}
    assert billing.subscription_status("user")["subscription"] is None
    assert billing.void_purchase("token-1", "GPA.1") is None
    # A refunded subscription is not given again by a late report.
    assert billing.record_subscription("user", sub(order="GPA.1"))["minutes"] == 0
    assert_ledger_adds_up(billing)


def test_a_refunded_past_order_takes_back_what_was_used(subs):
    _, _, billing = subs
    billing.record_subscription("user", sub(order="GPA.1"))
    billing.reserve("user", "job-1", 25)
    billing.settle_success("job-1")
    billing.record_subscription("user", sub(order="GPA.1..0", days=60))

    assert billing.void_purchase("token-1", "GPA.1") == ("user", 25)

    # The running period is not touched; its order was paid.
    assert balances(billing) == {"minutes": 75, "monthly_minutes": 100, "paid_minutes": -25}
    assert billing.subscription_status("user")["subscription"] is not None
    assert_ledger_adds_up(billing)


def test_a_refunded_unlimited_order_charges_its_jobs(subs):
    _, _, billing = subs
    billing.record_subscription("user", sub(product="sub_unlimited", order="GPA.U"))
    billing.reserve("user", "job-1", 30)
    billing.settle_success("job-1")
    billing.reserve("user", "job-2", 20)
    billing.release("job-2")

    assert billing.void_purchase("token-1", "GPA.U") == ("user", 30)

    assert balances(billing)["paid_minutes"] == -30
    assert billing.subscription_status("user")["unlimited"] is False
    with pytest.raises(InsufficientMinutes):
        billing.reserve("user", "job-3", 5)
    assert_ledger_adds_up(billing)


def test_a_refund_without_an_order_id_voids_the_latest_order(subs):
    _, _, billing = subs
    billing.record_subscription("user", sub(order="GPA.1"))
    assert billing.void_purchase("token-1") == ("user", 100)
    assert billing.void_purchase("unknown-token") is None


def test_a_refunded_product_is_still_voided_as_before(subs):
    _, _, billing = subs
    billing.credit_purchase("user", "starter", "product-token", "GPA.P")
    assert billing.void_purchase("product-token", "GPA.P") == ("user", 10)


# --- Crashes --------------------------------------------------------------------------------------

def test_a_crash_while_starting_a_period_changes_nothing(subs, monkeypatch):
    from app import billing as billing_module

    _, _, billing = subs
    billing.renew_monthly_minutes("user")

    def crash(*args, **kwargs):
        raise RuntimeError("power cut")

    monkeypatch.setattr(billing_module, "_log_minutes", crash)
    with pytest.raises(RuntimeError):
        billing.record_subscription("user", sub())
    monkeypatch.undo()

    assert balances(billing)["monthly_minutes"] == 10
    assert billing.read("SELECT COUNT(*) AS n FROM subscription_periods")[0]["n"] == 0
    # Reported again after the restart: given once.
    assert billing.record_subscription("user", sub())["minutes"] == 100
    assert billing.record_subscription("user", sub())["minutes"] == 0
    assert_ledger_adds_up(billing)


def test_a_crash_while_voiding_changes_nothing(subs, monkeypatch):
    from app import billing as billing_module

    _, _, billing = subs
    billing.record_subscription("user", sub(order="GPA.1"))

    def crash(*args, **kwargs):
        raise RuntimeError("power cut")

    monkeypatch.setattr(billing_module, "_log_minutes", crash)
    with pytest.raises(RuntimeError):
        billing.void_purchase("token-1", "GPA.1")
    monkeypatch.undo()

    assert balances(billing)["monthly_minutes"] == 100
    assert billing.void_purchase("token-1", "GPA.1") == ("user", 100)


# --- Welcome minutes ------------------------------------------------------------------------------

def test_welcome_minutes_are_given_once_per_person(subs):
    _, settings, billing = subs
    billing.renew_monthly_minutes("existing")
    settings.billing.welcome_minutes = 25

    billing.renew_monthly_minutes("user")
    billing.renew_monthly_minutes("user")

    assert balances(billing) == {"minutes": 35, "monthly_minutes": 10, "paid_minutes": 25}
    # Accounts made before are not given them.
    assert balances(billing, "existing")["paid_minutes"] == 0
    billing.delete_user("user")
    billing.renew_monthly_minutes("user")
    assert balances(billing)["paid_minutes"] == 0
    kinds = [row["kind"] for row in billing.read("SELECT kind FROM minute_ledger WHERE kind = 'welcome'")]
    assert kinds == ["welcome"]


def test_welcome_minutes_alone_create_the_account(subs):
    _, settings, billing = subs
    settings.billing.free_minutes = None
    settings.billing.welcome_minutes = 5

    billing.renew_monthly_minutes("user")

    assert balances(billing) == {"minutes": 5, "monthly_minutes": 0, "paid_minutes": 5}
    assert_ledger_adds_up(billing)


def test_welcome_minutes_come_with_a_first_purchase_too(subs):
    _, settings, billing = subs
    settings.billing.free_minutes = None
    settings.billing.welcome_minutes = 5

    billing.credit_purchase("user", "starter", "token", "GPA.1")

    assert balances(billing)["paid_minutes"] == 15


# --- Minutes for every account ----------------------------------------------------------------------

def test_credit_all_gives_every_account_once(subs):
    _, _, billing = subs
    for user in ("a", "b", "c"):
        billing.renew_monthly_minutes(user)
    billing.credit_purchase("d", "starter", "token", "GPA.1")
    billing.delete_user("d")

    first = billing.credit_all(7, "outage", "admin", "grant-1")
    again = billing.credit_all(7, "outage", "admin", "grant-1")

    assert first == {"grant_id": "grant-1", "repeated": False, "minutes": 7, "accounts": 3, "total": 21}
    assert again == {"grant_id": "grant-1", "repeated": True, "minutes": 7, "accounts": 3, "total": 21}
    for user in ("a", "b", "c"):
        assert balances(billing, user)["paid_minutes"] == 7
        assert_ledger_adds_up(billing, user)
    assert billing.read("SELECT COUNT(*) AS n FROM users WHERE user_id = ?", (DELETED_USER_ID,))[0]["n"] == 0
    audit = billing.read("SELECT actor, action, target, detail, reason FROM admin_actions")
    assert [dict(row) for row in audit] == [{
        "actor": "admin", "action": "users.credit_all", "target": "grant:grant-1",
        "detail": "+7 minutes to 3 accounts", "reason": "outage",
    }]
    with pytest.raises(ValueError):
        billing.credit_all(0, "nothing", "admin", "grant-2")


def test_a_crash_during_credit_all_gives_nobody_anything(subs, monkeypatch):
    from app import billing as billing_module

    _, _, billing = subs
    billing.renew_monthly_minutes("a")
    billing.renew_monthly_minutes("b")

    def crash(*args, **kwargs):
        raise RuntimeError("power cut")

    monkeypatch.setattr(billing_module, "_log_admin_action", crash)
    with pytest.raises(RuntimeError):
        billing.credit_all(5, "outage", "admin", "grant-1")
    monkeypatch.undo()

    assert balances(billing, "a")["paid_minutes"] == 0
    assert billing.read("SELECT COUNT(*) AS n FROM minute_ledger WHERE kind = 'admin'")[0]["n"] == 0
    assert billing.credit_all(5, "outage", "admin", "grant-1")["accounts"] == 2


# --- Google Play --------------------------------------------------------------------------------------

class FakeRequest:
    def __init__(self, result):
        self.result = result

    def execute(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def google_error(status):
    from googleapiclient.errors import HttpError

    return HttpError(types.SimpleNamespace(status=status, reason="error"), b"{}")


def rfc3339(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%S.123Z", time.gmtime(seconds))


def google_subscription(product="sub_pro", state="ACTIVE", days=30, order="GPA.1", tag=None, linked=None,
                        acknowledged=False):
    response = {
        "subscriptionState": f"SUBSCRIPTION_STATE_{state}",
        "latestOrderId": order,
        "acknowledgementState": "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED" if acknowledged
        else "ACKNOWLEDGEMENT_STATE_PENDING",
        "lineItems": [{
            "productId": product,
            "expiryTime": rfc3339(time.time() + days * DAY),
            "autoRenewingPlan": {"autoRenewEnabled": True},
            "offerDetails": {"basePlanId": "pro-monthly"},
            "latestSuccessfulOrderId": order,
        }],
    }
    if tag:
        response["externalAccountIdentifiers"] = {"obfuscatedExternalAccountId": tag}
    if linked:
        response["linkedPurchaseToken"] = linked
    return response


class FakeGooglePlay:
    """The Android Publisher API's subscription calls: subscriptions by
    token, the acknowledgements made, catalogue, and voided purchases."""

    def __init__(self):
        self.subscriptions = {}
        self.acknowledged = []
        self.acknowledge_error = None
        self.catalogue = []
        self.voided = []
        self.voided_arguments = []

    def purchases(self):
        return self

    def subscriptionsv2(self):
        return types.SimpleNamespace(get=lambda packageName, token: FakeRequest(
            self.subscriptions.get(token, google_error(410))
        ))

    def subscriptions_(self, packageName, subscriptionId, token, body):
        if self.acknowledge_error:
            return FakeRequest(self.acknowledge_error)
        self.acknowledged.append((subscriptionId, token))
        return FakeRequest({})

    def voidedpurchases(self):
        def listing(**arguments):
            self.voided_arguments.append(arguments)
            return FakeRequest({"voidedPurchases": self.voided})
        return types.SimpleNamespace(list=listing)

    def monetization(self):
        return types.SimpleNamespace(subscriptions=lambda: types.SimpleNamespace(
            list=lambda **arguments: FakeRequest({"subscriptions": self.catalogue})
        ))


@pytest.fixture
def google(subs, monkeypatch):
    from app import google_play

    client, settings, billing = subs
    settings.google_play.package_name = "com.example.piano"
    fake = FakeGooglePlay()
    # purchases().subscriptions() is a different resource from subscriptionsv2.
    fake.subscriptions_resource = types.SimpleNamespace(acknowledge=fake.subscriptions_)
    monkeypatch.setattr(google_play, "android_publisher", lambda config=None: fake)
    monkeypatch.setattr(google_play, "is_configured", lambda: True)
    monkeypatch.setattr(google_play, "synced_subscriptions", {
        "sub_pro": {"pro-monthly"}, "sub_unlimited": {"unlimited-monthly"},
    })
    original = FakeGooglePlay.purchases
    monkeypatch.setattr(FakeGooglePlay, "purchases", lambda self: types.SimpleNamespace(
        subscriptionsv2=self.subscriptionsv2,
        subscriptions=lambda: self.subscriptions_resource,
        voidedpurchases=self.voidedpurchases,
    ))
    assert original
    return client, settings, billing, fake


AUTH = {"Authorization": "Bearer person"}


def user_id_of(client):
    return client.get("/api/billing/balance", headers=AUTH).json()["user_id"]


def verify(client, token="token-1", product="sub_pro"):
    return client.post(
        "/api/billing/google-play/subscriptions/verify",
        json={"product_id": product, "purchase_token": token},
        headers=AUTH,
    )


def test_server_info_lists_the_plans_by_level(google):
    client, settings, _, _ = google
    settings.billing.welcome_minutes = 20

    info = client.get("/api/billing/server-info").json()

    assert info["subscriptions"] == [
        {"product_id": "sub_pro", "base_plan": "pro-monthly", "name": "Pro", "minutes_per_period": 100,
         "unlimited": False, "level": 1},
        {"product_id": "sub_unlimited", "base_plan": "unlimited-monthly", "name": "Unlimited",
         "minutes_per_period": None, "unlimited": True, "level": 2},
    ]
    assert info["welcome_minutes"] == 20
    assert info["unlimited_daily_minutes"] == 60


def test_server_info_offers_only_plans_active_in_google_play(google, monkeypatch):
    from app import google_play

    client, _, _, _ = google
    monkeypatch.setattr(google_play, "synced_subscriptions", {"sub_pro": {"other-plan"}, "sub_unlimited": set()})
    assert client.get("/api/billing/server-info").json()["subscriptions"] == []
    monkeypatch.setattr(google_play, "synced_subscriptions", None)
    assert client.get("/api/billing/server-info").json()["subscriptions"] == []


def test_catalogue_sync_reads_active_base_plans(google):
    from app import google_play

    _, _, _, fake = google
    fake.catalogue = [
        {"productId": "sub_pro", "basePlans": [{"basePlanId": "pro-monthly", "state": "ACTIVE"},
                                               {"basePlanId": "pro-yearly", "state": "INACTIVE"}]},
        {"productId": "sub_unlimited", "basePlans": [{"basePlanId": "unlimited-monthly", "state": "ACTIVE"}]},
    ]
    google_play.sync_subscriptions(fake)
    assert google_play.synced_subscriptions == {"sub_pro": {"pro-monthly"}, "sub_unlimited": {"unlimited-monthly"}}
    assert google_play.offered_subscriptions() == ["sub_pro", "sub_unlimited"]


def test_verifying_a_subscription_starts_and_acknowledges_it(google):
    client, _, _, fake = google
    user_id = user_id_of(client)
    fake.subscriptions["token-1"] = google_subscription(tag=account_tag(user_id))

    response = verify(client)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "started"
    assert body["granted_minutes"] == 100
    assert body["acknowledged"] is True
    assert body["balance"]["monthly_minutes"] == 100
    assert body["balance"]["subscription"]["name"] == "Pro"
    assert body["balance"]["subscription"]["minutes_per_period"] == 100
    assert fake.acknowledged == [("sub_pro", "token-1")]

    # Restoring it on another device gives nothing again.
    fake.subscriptions["token-1"] = google_subscription(tag=account_tag(user_id), acknowledged=True)
    again = verify(client).json()
    assert again["status"] == "recorded"
    assert again["granted_minutes"] == 0
    assert fake.acknowledged == [("sub_pro", "token-1")]


def test_a_failed_acknowledgement_is_reported_and_retried_by_the_refresh(google):
    from app import google_play

    client, _, billing, fake = google
    # Far from its renewal: only the missing acknowledgement brings it back.
    fake.subscriptions["token-1"] = google_subscription(days=30)
    fake.acknowledge_error = google_error(500)

    body = verify(client).json()

    assert body["status"] == "started"
    assert body["acknowledged"] is False
    assert billing.subscriptions_to_check(int(time.time()) + 30) == []
    fake.acknowledge_error = None
    google_play.refresh_subscriptions(billing, fake, now=int(time.time()) + 120)
    assert fake.acknowledged == [("sub_pro", "token-1")]
    # Acknowledged: not read again until its period ends.
    assert billing.subscriptions_to_check(int(time.time()) + 300) == []


def test_verification_refusals(google):
    client, _, _, fake = google

    assert verify(client, product="sub_retired").status_code == 400
    assert verify(client, token="missing").status_code == 400
    fake.subscriptions["broken"] = google_error(500)
    assert verify(client, token="broken").status_code == 502
    fake.subscriptions["other"] = google_subscription(tag=account_tag("someone else"))
    response = verify(client, token="other")
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "subscription_owned"
    no_auth = client.post(
        "/api/billing/google-play/subscriptions/verify", json={"product_id": "sub_pro", "purchase_token": "t"}
    )
    assert no_auth.status_code == 401


def test_verification_is_rate_limited_per_account(google):
    from app.api.routes import billing as billing_routes

    client, _, _, fake = google
    fake.subscriptions["token-1"] = google_subscription()
    for _ in range(billing_routes.VERIFY_LIMIT):
        assert verify(client).status_code == 200

    refused = verify(client)

    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) > 0


def test_verification_needs_google_play_billing(google):
    client, settings, _, _ = google
    settings.billing.provider = "none"
    assert verify(client).status_code in (401, 409)


def test_the_refresh_starts_renewals_and_ends_lapsed_subscriptions(google):
    from app import google_play

    client, _, billing, fake = google
    fake.subscriptions["token-1"] = google_subscription(days=0.001)
    verify(client)
    user_id = user_id_of(client)
    billing.reserve(user_id, "job-1", 60)
    billing.settle_success("job-1")

    fake.subscriptions["token-1"] = google_subscription(days=30, order="GPA.1..0", acknowledged=True)
    assert google_play.refresh_subscriptions(billing, fake, now=int(time.time()) + 120) == 1
    assert billing.balances(user_id)["monthly_minutes"] == 100

    # Google no longer knows the token.
    del fake.subscriptions["token-1"]
    write(billing, "UPDATE subscriptions SET expires_at = ?, checked_at = 0", (int(time.time()) - 60,))
    google_play.refresh_subscriptions(billing, fake)
    assert billing.read("SELECT state FROM subscriptions")[0]["state"] == "EXPIRED"


def test_a_network_failure_stops_the_refresh_to_try_again(google):
    from app import google_play

    client, _, billing, fake = google
    fake.subscriptions["token-1"] = google_subscription(days=0.001)
    verify(client)
    fake.subscriptions["token-1"] = OSError("network is unreachable")

    with pytest.raises(OSError):
        google_play.refresh_subscriptions(billing, fake, now=int(time.time()) + 120)


def test_voided_subscription_orders_are_read_and_taken_back(google):
    from app import google_play

    client, _, billing, fake = google
    fake.subscriptions["token-1"] = google_subscription()
    verify(client)
    user_id = user_id_of(client)
    fake.voided = [{"purchaseToken": "token-1", "orderId": "GPA.1", "voidedReason": 1}]

    assert google_play.sync_voided_purchases(billing, fake) == 1

    assert fake.voided_arguments[0]["type"] == 1
    assert billing.balances(user_id)["monthly_minutes"] == 0
    assert google_play.sync_voided_purchases(billing, fake) == 0


def test_rfc3339_times_with_any_fraction(google):
    from app import google_play

    assert google_play._timestamp("2026-01-01T00:00:00Z") == 1767225600
    assert google_play._timestamp("2026-01-01T00:00:00.1Z") == 1767225600
    assert google_play._timestamp("2026-01-01T00:00:00.123456789Z") == 1767225600
    assert google_play._timestamp(None) == 0


def test_balance_shows_an_unlimited_plan(google):
    client, _, _, fake = google
    fake.subscriptions["token-1"] = google_subscription(product="sub_unlimited")
    verify(client, product="sub_unlimited")

    balance = client.get("/api/billing/balance", headers=AUTH).json()

    assert balance["unlimited"] is True
    assert balance["unlimited_minutes_left"] == 60
    assert balance["subscription"]["minutes_per_period"] is None
    assert balance["subscription"]["level"] == 2


def test_an_unlimited_plan_refuses_jobs_past_fair_use(google):
    client, settings, billing, fake = google
    settings.billing.unlimited_daily_minutes = 3
    fake.subscriptions["token-1"] = google_subscription(product="sub_unlimited")
    assert verify(client, product="sub_unlimited").json()["status"] == "started"
    user_id = user_id_of(client)
    # The test video costs 2 minutes.
    first = client.post("/api/transcriptions", json={"source_url": "https://www.youtube.com/watch?v=abc"}, headers=AUTH)
    assert first.status_code in (200, 201, 202), first.text

    second = client.post("/api/transcriptions", json={"source_url": "https://www.youtube.com/watch?v=abd"}, headers=AUTH)

    assert second.status_code == 429
    assert second.json()["detail"]["error_code"] == "fair_use_limit"
    assert int(second.headers["Retry-After"]) > 0
    # Neither was charged: the free minutes are all there.
    assert billing.balances(user_id)["monthly_minutes"] == 0
    assert billing.subscription_status(user_id)["unlimited_minutes_left"] == 1


# --- Admin ---------------------------------------------------------------------------------------

def test_admin_credit_all_needs_positive_minutes_and_accounts(subs):
    from app.admin import users
    from app.admin.common import AdminError

    _, _, billing = subs
    with pytest.raises(AdminError, match="no accounts"):
        users.credit_all_plan(5)
    billing.renew_monthly_minutes("a")
    with pytest.raises(AdminError, match="positive"):
        users.credit_all_plan(-5)
    with pytest.raises(AdminError, match="At most"):
        users.credit_all_plan(users.CREDIT_ALL_LIMIT + 1)
    with pytest.raises(AdminError, match="reason"):
        users.credit_all(5, "admin", None, "g")
    assert users.credit_all_plan(5) == {"minutes": 5, "accounts": 1, "total": 5}
    assert users.credit_all(5, "admin", "thanks", "g")["accounts"] == 1


def test_admin_user_view_shows_the_subscription(subs):
    from app.admin import users

    _, _, billing = subs
    billing.record_subscription("user", sub())

    account = users.account("user")

    assert account["subscription"]["name"] == "Pro"
    assert account["subscriptions"][0]["product_id"] == "sub_pro"
    assert "purchase_token" not in account["subscriptions"][0]


@pytest.mark.skipif(not POSTGRES_URL, reason="needs PIANO_TEST_POSTGRES_URL")
def test_the_admin_web_role_gives_minutes_to_all_and_never_reads_tokens(subs, monkeypatch, tmp_path):
    """The admin web page's read-write role may give every account minutes
    and cancel an unlimited job, and cannot read purchase tokens."""
    from pathlib import Path
    from urllib.parse import urlsplit, urlunsplit

    import psycopg

    from app.billing import Billing

    _, settings, billing = subs
    if not billing._postgres:
        pytest.skip("PostgreSQL only")
    secrets = Path(settings.secrets_dir) / "admin-web"
    secrets.mkdir(parents=True, exist_ok=True)
    (secrets / "postgres-read-write").write_text("rw-secret", encoding="utf-8")
    owner = Billing()
    owner._create_schema()
    owner.record_subscription("user", sub(product="sub_unlimited"))
    owner.reserve("user", "job-1", 30)

    parts = urlsplit(POSTGRES_URL)
    role_url = urlunsplit((parts.scheme, f"admin-read-write:rw-secret@{parts.hostname}:{parts.port}",
                           parts.path, "", ""))
    settings.database.url = role_url
    page = Billing(create_schema=False)

    assert page.credit_all(3, "outage", "web:admin", "grant1")["accounts"] == 1
    assert page.undo_credit_all("grant1", "mistake", "web:admin")["taken"] == 3
    page.release("job-1")
    assert owner.subscription_status("user")["unlimited_minutes_left"] == 60
    with psycopg.connect(role_url) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("SELECT purchase_token FROM subscriptions")
    with psycopg.connect(role_url) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("DELETE FROM unlimited_usage")
    with psycopg.connect(role_url) as connection:
        assert connection.execute("SELECT product_id FROM subscriptions").fetchall() == [("sub_unlimited",)]


def test_undoing_a_grant_never_makes_a_balance_negative(subs):
    _, settings, billing = subs
    settings.billing.free_minutes = None
    for user in ("keeps", "spent", "partly", "running"):
        billing.credit_purchase(user, "starter", f"token-{user}", f"GPA.{user}")
    billing.credit_all(20, "outage", "admin", "grant1")
    # spent used everything; partly used 25 of its 30; running holds 28.
    billing.reserve("spent", "job-spent", 30)
    billing.settle_success("job-spent")
    billing.reserve("partly", "job-partly", 25)
    billing.settle_success("job-partly")
    billing.reserve("running", "job-running", 28)

    plan = billing.undo_credit_all_plan("grant1")
    assert (plan["accounts"], plan["granted"], plan["taken"], plan["kept"]) == (4, 80, 27, 53)

    result = billing.undo_credit_all("grant1", "given by mistake", "admin")

    assert result["taken"] == 27 and result["repeated"] is False
    assert {user: billing.balances(user)["paid_minutes"] for user in ("keeps", "spent", "partly", "running")} == {
        "keeps": 10, "spent": 0, "partly": 0, "running": 0,
    }
    billing.settle_success("job-running")
    assert billing.balances("running")["paid_minutes"] == 0
    for user in ("keeps", "spent", "partly", "running"):
        assert_ledger_adds_up(billing, user)
    # Once only.
    assert billing.undo_credit_all("grant1", "again", "admin")["repeated"] is True
    assert billing.balances("keeps")["paid_minutes"] == 10
    [grant] = billing.credit_all_grants()
    assert grant["grant_id"] == "grant1" and grant["undone_at"] is not None and grant["minutes"] == 20
    with pytest.raises(ValueError):
        billing.undo_credit_all_plan("unknown")


def test_admin_undo_of_a_grant(subs):
    from app.admin import users
    from app.admin.common import AdminError

    _, _, billing = subs
    billing.renew_monthly_minutes("a")
    given = users.credit_all(5, "admin", "outage", "g1")

    plan = users.credit_all_undo_plan(given["grant_id"])
    assert plan["taken"] == 5 and "take" not in plan
    with pytest.raises(AdminError, match="reason"):
        users.undo_credit_all("g1", "admin", None)
    assert users.undo_credit_all("g1", "admin", "mistake")["taken"] == 5
    with pytest.raises(AdminError, match="already undone"):
        users.credit_all_undo_plan("g1")
    with pytest.raises(AdminError, match="No grant"):
        users.credit_all_undo_plan("nope")
