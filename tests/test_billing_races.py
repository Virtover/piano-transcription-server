"""Scenarios that depend on the order Google's answers arrive in, and races:
the same change made by several threads at the same moment, each with its
own database connection, as several API requests, workers, and the leader's
refresh are. Run on SQLite, and on PostgreSQL with PIANO_TEST_POSTGRES_URL
(see test_subscriptions.py)."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.billing import InsufficientMinutes, SubscriptionOwned
from test_subscriptions import (  # noqa: F401  (fixtures)
    AUTH,
    DAY,
    assert_ledger_adds_up,
    balances,
    google,
    google_subscription,
    sub,
    subs,
    user_id_of,
    verify,
    write,
)

THREADS = 8


def at_once(*calls):
    """Run the calls in threads released together; returns each call's
    result, or the exception it raised."""
    barrier = threading.Barrier(len(calls))

    def run(call):
        barrier.wait()
        try:
            return call()
        except Exception as error:  # noqa: BLE001
            return error

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(run, calls))


def plan_change(billing, old_info, new_info, order):
    """Record the replaced token's last report and the replacing one's in
    the given order, or both at once."""
    if order == "old first":
        billing.record_subscription("user", old_info)
        billing.record_subscription("user", new_info)
    elif order == "new first":
        billing.record_subscription("user", new_info)
        billing.record_subscription("user", old_info)
    else:
        results = at_once(lambda: billing.record_subscription("user", old_info),
                          lambda: billing.record_subscription("user", new_info))
        assert not [result for result in results if isinstance(result, Exception)]


ORDERS = ["old first", "new first", "at once"]


# --- Changes of plan, in any order ----------------------------------------------------------------

@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("cancelled", [False, True], ids=["renewing", "cancelled"])
def test_a_deferred_downgrade_keeps_the_plan_until_the_renewal(subs, order, cancelled):
    """Unlimited, possibly cancelled, then Pro chosen: Google expires the old
    token as replaced and issues a new one carrying Unlimited until the
    renewal. Unlimited must stay whichever token is reported first."""
    _, _, billing = subs
    billing.record_subscription("user", sub(token="old", product="sub_unlimited", order="GPA.1"))
    if cancelled:
        billing.record_subscription("user", sub(token="old", product="sub_unlimited", order="GPA.1",
                                                state="CANCELED", auto_renewing=False))
    old = sub(token="old", product="sub_unlimited", order="GPA.1", state="EXPIRED", days=-0.001,
              auto_renewing=False, replaced=True)
    new = sub(token="new", product="sub_unlimited", order="GPA.2", linked_token="old",
              deferred_product_id="sub_pro")

    plan_change(billing, old, new, order)

    status = billing.subscription_status("user")
    assert status["unlimited"] is True
    assert status["subscription"]["next_name"] == "Pro"
    assert billing.renew_monthly_minutes("user") == 0
    billing.reserve("user", "job-1", 30)
    billing.settle_success("job-1")
    assert balances(billing) == {"minutes": 0, "monthly_minutes": 0, "paid_minutes": 0}

    # The renewal is paid as Pro.
    billing.record_subscription("user", sub(token="new", product="sub_pro", order="GPA.2..0", linked_token="old"))
    assert billing.subscription_status("user")["subscription"]["name"] == "Pro"
    assert balances(billing)["monthly_minutes"] == 100
    assert_ledger_adds_up(billing)


@pytest.mark.parametrize("order", ORDERS)
def test_an_upgrade_gives_the_new_plan_at_once_in_any_order(subs, order):
    _, _, billing = subs
    billing.record_subscription("user", sub(token="old", order="GPA.1"))
    billing.reserve("user", "job-1", 40)
    billing.settle_success("job-1")
    old = sub(token="old", order="GPA.1", state="EXPIRED", days=-0.001, auto_renewing=False, replaced=True)
    new = sub(token="new", product="sub_unlimited", order="GPA.2", linked_token="old")

    plan_change(billing, old, new, order)

    assert billing.subscription_status("user")["unlimited"] is True
    # Pro's unused minutes expired with its period; none were given twice.
    assert balances(billing)["monthly_minutes"] == 0
    assert billing.read("SELECT COUNT(*) AS n FROM minute_ledger WHERE kind = 'subscription'")[0]["n"] == 1
    assert_ledger_adds_up(billing)


def test_a_replaced_token_reported_alone_keeps_its_period(subs):
    """The app may report the old token and never the new one, as when it
    is closed between them: the plan runs to its paid end."""
    _, _, billing = subs
    billing.record_subscription("user", sub(token="old", product="sub_unlimited", order="GPA.1", days=1))
    billing.record_subscription("user", sub(token="old", product="sub_unlimited", order="GPA.1", state="EXPIRED",
                                            days=-0.001, replaced=True))
    assert billing.subscription_status("user")["unlimited"] is True


def test_an_expiry_that_is_not_a_replacement_still_ends_the_period(subs):
    _, _, billing = subs
    billing.record_subscription("user", sub(token="old", product="sub_unlimited", order="GPA.1", days=1))
    billing.record_subscription("user", sub(token="old", product="sub_unlimited", order="GPA.1", state="EXPIRED",
                                            days=-0.001))
    assert billing.subscription_status("user")["unlimited"] is False


def test_google_reports_a_replacement(google):
    from app import google_play

    response = google_subscription(state="EXPIRED", days=-1)
    response["canceledStateContext"] = {"replacementCancellation": {}}
    assert google_play.parse_subscription("t", response).replaced is True
    response["canceledStateContext"] = {"userInitiatedCancellation": {}}
    assert google_play.parse_subscription("t", response).replaced is False


# --- Races ------------------------------------------------------------------------------------------

def test_one_order_reported_by_many_threads_is_given_once(subs):
    _, _, billing = subs
    billing.renew_monthly_minutes("user")

    results = at_once(*[lambda: billing.record_subscription("user", sub()) for _ in range(THREADS)])

    assert [result["status"] for result in results].count("started") == 1
    assert balances(billing)["monthly_minutes"] == 100
    assert_ledger_adds_up(billing)


def test_two_accounts_claiming_one_subscription_at_once_get_it_once(subs):
    _, _, billing = subs
    users = [f"user-{index}" for index in range(THREADS)]

    results = at_once(*[lambda user=user: billing.record_subscription(user, sub()) for user in users])

    owners = [result["user_id"] for result in results if isinstance(result, dict)]
    assert len(owners) == 1
    assert all(isinstance(result, SubscriptionOwned) for result in results if not isinstance(result, dict))
    assert sum(balances(billing, user)["monthly_minutes"] for user in users) == 100


def test_reservations_at_once_never_overdraw(subs):
    _, settings, billing = subs
    settings.billing.free_minutes = None
    billing.credit_purchase("user", "starter", "token", "GPA.P")  # 10 minutes

    results = at_once(*[lambda job=job: billing.reserve("user", f"job-{job}", 3) for job in range(THREADS)])

    accepted = [result for result in results if not isinstance(result, Exception)]
    assert len(accepted) == 3
    assert all(isinstance(result, InsufficientMinutes) for result in results if isinstance(result, Exception))
    assert balances(billing)["minutes"] == 1
    for job in range(THREADS):
        billing.settle_success(f"job-{job}")
    assert balances(billing)["minutes"] == 1
    assert_ledger_adds_up(billing)


def test_settling_and_releasing_one_job_at_once_charges_it_once(subs):
    _, settings, billing = subs
    settings.billing.free_minutes = None
    billing.credit_purchase("user", "starter", "token", "GPA.P")
    for round_ in range(5):
        billing.reserve("user", f"job-{round_}", 2)

        at_once(lambda: billing.settle_success(f"job-{round_}"),
                lambda: billing.settle_success(f"job-{round_}"),
                lambda: billing.settle_cancellation(f"job-{round_}", 1),
                lambda: billing.release(f"job-{round_}"))

        charged = billing.read("SELECT status, charged_minutes FROM transcription_reservations WHERE job_id = ?",
                               (f"job-{round_}",))[0]
        assert charged["status"] in ("settled", "released")
    assert_ledger_adds_up(billing)
    used = -sum(row["minutes"] for row in billing.read("SELECT minutes FROM minute_ledger WHERE kind = 'used'"))
    assert balances(billing)["minutes"] == 10 - used


def test_a_product_verified_at_once_is_credited_once(subs):
    _, _, billing = subs

    results = at_once(*[lambda: billing.credit_purchase("user", "starter", "token", "GPA.P") for _ in range(THREADS)])

    assert sorted(results) == [0] * (THREADS - 1) + [10]
    assert balances(billing)["paid_minutes"] == 10


def test_a_refund_racing_reservations_keeps_the_ledger(subs):
    _, _, billing = subs
    billing.record_subscription("user", sub(product="sub_unlimited", order="GPA.U"))

    at_once(lambda: billing.void_purchase("token-1", "GPA.U"),
            *[lambda job=job: billing.reserve("user", f"job-{job}", 5) for job in range(THREADS - 1)])

    for job in range(THREADS - 1):
        billing.settle_success(f"job-{job}")
    assert billing.void_purchase("token-1", "GPA.U") is None
    assert_ledger_adds_up(billing)
    # Every job either ran on the plan and was charged by the refund, or was
    # refused or paid from the free minutes after it.
    assert balances(billing)["minutes"] >= -5 * (THREADS - 1)


def test_a_renewal_racing_a_reservation_gives_one_period(subs):
    _, _, billing = subs
    billing.record_subscription("user", sub(order="GPA.1", days=0.0001))
    time.sleep(0.05)
    write(billing, "UPDATE subscription_periods SET ends_at = ?", (int(time.time()) - 1,))

    results = at_once(lambda: billing.record_subscription("user", sub(order="GPA.1..0")),
                      lambda: billing.reserve("user", "job-1", 5),
                      lambda: billing.renew_monthly_minutes("user"))

    assert not [result for result in results if isinstance(result, Exception) and
                not isinstance(result, InsufficientMinutes)]
    billing.settle_success("job-1")
    assert billing.subscription_status("user")["subscription"]["name"] == "Pro"
    assert billing.read("SELECT COUNT(*) AS n FROM minute_ledger WHERE kind = 'subscription'")[0]["n"] == 2
    assert_ledger_adds_up(billing)


def test_a_grant_to_everyone_and_its_undo_race_reservations(subs):
    _, settings, billing = subs
    settings.billing.free_minutes = None
    users = [f"user-{index}" for index in range(4)]
    for user in users:
        billing.credit_purchase(user, "starter", f"token-{user}", f"GPA.{user}")

    at_once(*[lambda: billing.credit_all(5, "outage", "admin", "grant") for _ in range(4)])
    assert all(balances(billing, user)["paid_minutes"] == 15 for user in users)

    at_once(lambda: billing.undo_credit_all("grant", "mistake", "admin"),
            lambda: billing.undo_credit_all("grant", "mistake", "admin"),
            *[lambda user=user: billing.reserve(user, f"job-{user}", 12) for user in users])

    for user in users:
        billing.settle_success(f"job-{user}")
        assert balances(billing, user)["paid_minutes"] >= 0
        assert_ledger_adds_up(billing, user)
    assert billing.read("SELECT COUNT(*) AS n FROM admin_actions WHERE action = 'users.credit_all'")[0]["n"] == 1
    assert billing.read("SELECT COUNT(*) AS n FROM admin_actions WHERE action = 'users.credit_all_undo'")[0]["n"] == 1


# --- The API, with requests at once -------------------------------------------------------------------

def test_a_subscription_verified_by_requests_at_once_is_given_once(google):
    client, _, billing, fake = google
    user_id = user_id_of(client)
    fake.subscriptions["token-1"] = google_subscription()

    responses = at_once(*[lambda: verify(client) for _ in range(6)])

    assert all(response.status_code == 200 for response in responses)
    assert [response.json()["status"] for response in responses].count("started") == 1
    assert billing.balances(user_id)["monthly_minutes"] == 100
    assert_ledger_adds_up(billing, user_id)


def test_the_plan_change_reported_by_requests_at_once(google):
    """The app's purchase listener and its recovery send the old and the
    new token at the same moment."""
    client, _, billing, fake = google
    user_id = user_id_of(client)
    fake.subscriptions["old"] = google_subscription(product="sub_unlimited", order="GPA.1")
    verify(client, token="old", product="sub_unlimited")
    expired = google_subscription(product="sub_unlimited", state="EXPIRED", days=-0.001, order="GPA.1",
                                  acknowledged=True)
    expired["canceledStateContext"] = {"replacementCancellation": {}}
    carried = google_subscription(product="sub_unlimited", order="GPA.2", linked="old", acknowledged=True)
    carried["lineItems"][0]["deferredItemReplacement"] = {"productId": "sub_pro"}
    fake.subscriptions["old"], fake.subscriptions["new"] = expired, carried

    responses = at_once(lambda: verify(client, token="old", product="sub_unlimited"),
                        lambda: verify(client, token="new", product="sub_unlimited"),
                        lambda: verify(client, token="old", product="sub_unlimited"),
                        lambda: verify(client, token="new", product="sub_unlimited"))

    assert all(response.status_code == 200 for response in responses)
    balance = client.get("/api/billing/balance", headers=AUTH).json()
    assert balance["unlimited"] is True
    assert balance["subscription"]["next_name"] == "Pro"
    assert_ledger_adds_up(billing, user_id)


def test_the_refresh_racing_a_verification_gives_a_renewal_once(google):
    from app import google_play

    client, _, billing, fake = google
    fake.subscriptions["token-1"] = google_subscription(days=0.001, acknowledged=True)
    verify(client)
    user_id = user_id_of(client)
    fake.subscriptions["token-1"] = google_subscription(days=30, order="GPA.1..0", acknowledged=True)

    at_once(lambda: google_play.refresh_subscriptions(billing, fake, now=int(time.time()) + 120),
            lambda: verify(client),
            lambda: google_play.refresh_subscriptions(billing, fake, now=int(time.time()) + 120),
            lambda: verify(client))

    assert billing.read("SELECT COUNT(*) AS n FROM minute_ledger WHERE kind = 'subscription'")[0]["n"] == 2
    assert billing.balances(user_id)["monthly_minutes"] == 100
    assert_ledger_adds_up(billing, user_id)


def test_jobs_submitted_at_once_hold_their_cost_once(google):
    client, settings, billing, fake = google
    settings.billing.free_minutes = None
    user_id = user_id_of(client)
    billing.credit_purchase(user_id, "starter", "pack", "GPA.P")  # 10 minutes; the video costs 2
    settings.limits.max_active_jobs_per_user = 10

    responses = at_once(*[
        lambda video=video: client.post(
            "/api/transcriptions", json={"source_url": f"https://www.youtube.com/watch?v=v{video}"}, headers=AUTH
        )
        for video in range(THREADS)
    ])

    accepted = [response for response in responses if response.status_code == 202]
    assert len(accepted) == 5
    assert all(response.status_code in (202, 402) for response in responses)
    assert billing.balances(user_id)["minutes"] == 0
