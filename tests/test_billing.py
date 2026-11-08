import sqlite3
from contextlib import closing

import pytest

from app.billing import Billing, InsufficientMinutes, cancelled_cost, period_seconds, transcription_cost


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
    assert billing.renew_monthly_minutes("anonymous") == 0


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


@pytest.fixture
def monthly(billed):
    settings, billing = billed
    settings.billing.free_minutes = 5
    settings.billing.free_minutes_period = "1d"
    return settings, billing


def end_period(settings, user_id="user"):
    """Move the user's monthly period back, so that it has ended."""
    with closing(sqlite3.connect(settings.database.path)) as connection, connection:
        connection.execute(
            "UPDATE users SET free_grant_at = free_grant_at - 86400 WHERE user_id = ?",
            (user_id,),
        )


def test_monthly_minutes_renew_once_per_period(monthly):
    _, billing = monthly

    assert billing.renew_monthly_minutes("user") == 5
    assert billing.renew_monthly_minutes("user") == 0
    assert billing.balances("user") == {"minutes": 5, "monthly_minutes": 5, "paid_minutes": 0}
    status = billing.free_minutes_status("user")
    assert 0 < status["free_minutes_seconds_until_next_grant"] <= 24 * 60 * 60


def test_monthly_periods_keep_a_fixed_schedule_from_sign_up(monthly):
    settings, billing = monthly
    billing.renew_monthly_minutes("user")
    signed_up = billing.read("SELECT free_grant_at FROM users")[0]["free_grant_at"]
    # Back two and a half periods, as if the user signed up then and has
    # been away since.
    with closing(sqlite3.connect(settings.database.path)) as connection, connection:
        connection.execute("UPDATE users SET free_grant_at = free_grant_at - 216000")

    assert billing.renew_monthly_minutes("user") == 5
    # The third period, which began half a period ago, not now.
    assert billing.read("SELECT free_grant_at FROM users")[0]["free_grant_at"] == signed_up - 216000 + 2 * 86400
    assert billing.free_minutes_status("user")["free_minutes_seconds_until_next_grant"] <= 43200


def test_a_renewal_held_back_by_a_debt_keeps_the_schedule(monthly):
    settings, billing = monthly
    billing.renew_monthly_minutes("user")
    billing.adjust_minutes("user", -3, "chargeback")
    with closing(sqlite3.connect(settings.database.path)) as connection, connection:
        connection.execute("UPDATE users SET free_grant_at = free_grant_at - 129600")
    start = billing.read("SELECT free_grant_at FROM users")[0]["free_grant_at"]
    assert billing.renew_monthly_minutes("user") == 0

    billing.adjust_minutes("user", 3, "paid back")

    assert billing.renew_monthly_minutes("user") == 5
    assert billing.read("SELECT free_grant_at FROM users")[0]["free_grant_at"] == start + 86400


def test_monthly_minutes_come_beside_paid_ones(monthly):
    # Paying users get them too: they are their own balance.
    _, billing = monthly
    billing.credit_purchase("user", "starter", "token", "order")

    assert billing.renew_monthly_minutes("user") == 5
    assert billing.balances("user") == {"minutes": 15, "monthly_minutes": 5, "paid_minutes": 10}


def test_monthly_minutes_are_spent_first(monthly):
    _, billing = monthly
    billing.credit_purchase("user", "starter", "token", "order")
    billing.renew_monthly_minutes("user")

    assert billing.reserve("user", "job-1", 3) == 12
    assert billing.balances("user") == {"minutes": 12, "monthly_minutes": 2, "paid_minutes": 10}
    assert billing.reserve("user", "job-2", 4) == 8
    assert billing.balances("user") == {"minutes": 8, "monthly_minutes": 0, "paid_minutes": 8}
    billing.settle_success("job-2")
    billing.settle_success("job-1")

    assert billing.balances("user") == {"minutes": 8, "monthly_minutes": 0, "paid_minutes": 8}


def test_unused_monthly_minutes_expire_and_paid_ones_stay(monthly):
    settings, billing = monthly
    billing.credit_purchase("user", "starter", "token", "order")
    billing.renew_monthly_minutes("user")
    billing.reserve("user", "job", 2)
    billing.settle_success("job")
    end_period(settings)

    assert billing.renew_monthly_minutes("user") == 5
    # 3 left over expired; the new period starts again from 5.
    assert billing.balances("user") == {"minutes": 15, "monthly_minutes": 5, "paid_minutes": 10}
    assert ledger(billing)[-2:] == [("user", "expired", -3, None), ("user", "free", 5, None)]


def test_reserving_renews_an_ended_period_first(monthly):
    settings, billing = monthly
    billing.renew_monthly_minutes("user")
    billing.reserve("user", "job", 5)
    billing.settle_success("job")
    end_period(settings)

    # Without asking for the balance first.
    assert billing.reserve("user", "next", 5) == 0


def test_a_running_job_keeps_its_monthly_minutes_across_a_renewal(monthly):
    settings, billing = monthly
    billing.credit_purchase("user", "starter", "token", "order")
    billing.renew_monthly_minutes("user")
    billing.reserve("user", "job", 4)
    end_period(settings)

    billing.renew_monthly_minutes("user")
    # The 1 unheld minute expired; the job still holds its 4.
    assert billing.balances("user") == {"minutes": 15, "monthly_minutes": 5, "paid_minutes": 10}
    billing.settle_success("job")

    assert billing.balances("user") == {"minutes": 15, "monthly_minutes": 5, "paid_minutes": 10}
    assert_ledger_adds_up(billing)


def test_monthly_minutes_of_an_ended_period_expire_when_its_job_is_released(monthly):
    settings, billing = monthly
    billing.renew_monthly_minutes("user")
    billing.reserve("user", "job", 4)
    end_period(settings)
    billing.renew_monthly_minutes("user")

    billing.release("job")

    # Not carried into the new period.
    assert billing.balances("user") == {"minutes": 5, "monthly_minutes": 5, "paid_minutes": 0}
    assert_ledger_adds_up(billing)


def test_cancellation_across_a_renewal_charges_old_minutes_and_expires_the_rest(monthly):
    settings, billing = monthly
    billing.renew_monthly_minutes("user")
    billing.reserve("user", "job", 4)
    end_period(settings)
    billing.renew_monthly_minutes("user")

    billing.settle_cancellation("job", 1)

    assert billing.balances("user") == {"minutes": 5, "monthly_minutes": 5, "paid_minutes": 0}
    assert_ledger_adds_up(billing)


def test_a_job_paid_partly_from_each_balance(monthly):
    _, billing = monthly
    billing.credit_purchase("user", "starter", "token", "order")
    billing.renew_monthly_minutes("user")

    billing.reserve("user", "job", 8)
    billing.settle_cancellation("job", 6)

    # 5 monthly first, then 1 paid.
    assert billing.balances("user") == {"minutes": 9, "monthly_minutes": 0, "paid_minutes": 9}
    assert ("user", "used", -5, "job") in ledger(billing)
    assert ("user", "used", -1, "job") in ledger(billing)
    assert_ledger_adds_up(billing)


def test_a_requeued_job_holds_monthly_minutes_again(monthly):
    _, billing = monthly
    billing.renew_monthly_minutes("user")
    billing.reserve("user", "job", 3)
    billing.release("job")

    billing.reserve("user", "job", 3)
    billing.settle_success("job")

    assert billing.balances("user") == {"minutes": 2, "monthly_minutes": 2, "paid_minutes": 0}


def test_left_over_monthly_minutes_expire_when_free_minutes_are_turned_off(monthly):
    settings, billing = monthly
    billing.renew_monthly_minutes("user")
    settings.billing.free_minutes = None
    end_period(settings)

    assert billing.renew_monthly_minutes("user") == 0
    assert billing.balances("user")["monthly_minutes"] == 0


def test_without_free_minutes_no_account_is_created(billed):
    _, billing = billed

    assert billing.renew_monthly_minutes("stranger") == 0
    assert billing.read("SELECT * FROM users") == []


def test_an_older_database_gains_monthly_minutes_keeping_its_balances(tmp_path, monthly):
    settings, _ = monthly
    path = tmp_path / "old.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE users (user_id TEXT PRIMARY KEY, email TEXT, minutes INTEGER NOT NULL DEFAULT 0,
                                free_grant_at INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE transcription_reservations (job_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                reserved_minutes INTEGER NOT NULL, charged_minutes INTEGER, status TEXT NOT NULL);
            CREATE TABLE minute_ledger (id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL,
                minutes INTEGER NOT NULL, at INTEGER NOT NULL, note TEXT);
            INSERT INTO users VALUES ('user', NULL, 7, strftime('%s', 'now'));
            INSERT INTO transcription_reservations VALUES ('old-job', 'user', 2, NULL, 'reserved');
            INSERT INTO minute_ledger VALUES (1, 'user', 'free', 7, 0, NULL);
            """
        )
    billing = Billing(str(path))

    # What they had stays, as paid minutes; this period was already given.
    assert billing.balances("user") == {"minutes": 5, "monthly_minutes": 0, "paid_minutes": 5}
    assert billing.renew_monthly_minutes("user") == 0
    billing.settle_success("old-job")
    assert billing.balances("user") == {"minutes": 5, "monthly_minutes": 0, "paid_minutes": 5}
    assert billing.read("SELECT pool FROM minute_ledger WHERE id = 1")[0]["pool"] == "paid"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE users SET free_grant_at = 0")
    assert billing.renew_monthly_minutes("user") == 5


def test_free_minutes_period_defaults_to_30_days(api_context):
    _, settings, _ = api_context
    settings.billing.free_minutes_period = None

    assert period_seconds() == 30 * 24 * 60 * 60


def test_unknown_user_cannot_be_credited(billed):
    _, billing = billed

    with pytest.raises(ValueError, match="nobody"):
        billing.adjust_minutes("nobody", 5, "test")


def test_adjust_minutes_can_go_below_zero(billed):
    _, billing = billed
    billing.credit_purchase("user", "starter", "token", "order")

    assert billing.adjust_minutes("user", 7, "goodwill") == 17
    assert billing.adjust_minutes("user", -20, "chargeback") == -3
    assert billing.get_balance("user") == -3


def ledger(billing):
    return [
        (row["user_id"], row["kind"], row["minutes"], row["note"])
        for row in billing.read("SELECT * FROM minute_ledger ORDER BY id")
    ]


def test_every_balance_change_is_in_the_ledger(billed):
    settings, billing = billed
    settings.billing.free_minutes = 5
    settings.billing.free_minutes_period = "1d"

    billing.renew_monthly_minutes("user")
    billing.credit_purchase("user", "starter", "token", "GPA.1")
    billing.reserve("user", "job-1", 3)
    billing.settle_success("job-1")
    billing.reserve("user", "job-2", 4)
    billing.settle_cancellation("job-2", 0)
    billing.void_purchase("token")
    billing.adjust_minutes("user", 2, "goodwill")

    assert ledger(billing) == [
        ("user", "free", 5, None),
        ("user", "purchase", 10, "GPA.1"),
        ("user", "used", -3, "job-1"),
        ("user", "voided", -10, "GPA.1"),
        ("user", "admin", 2, "goodwill"),
    ]
    # The ledger adds up to the balance.
    assert sum(entry[2] for entry in ledger(billing)) == billing.get_balance("user")
    assert_ledger_adds_up(billing)


def assert_ledger_adds_up(billing, user_id="user"):
    """Each pool of the ledger adds up to its balance."""
    user = billing.read("SELECT minutes, monthly_minutes FROM users WHERE user_id = ?", (user_id,))[0]
    pools = {
        row["pool"]: row["total"]
        for row in billing.read(
            "SELECT pool, SUM(minutes) AS total FROM minute_ledger WHERE user_id = ? GROUP BY pool", (user_id,)
        )
    }
    assert (pools.get("paid", 0), pools.get("monthly", 0)) == (user["minutes"], user["monthly_minutes"])


def test_released_reservation_can_be_reserved_again(billed):
    _, billing = billed
    billing.credit_purchase("user", "starter", "token", "order")
    billing.reserve("user", "job", 3)
    billing.release("job")

    assert billing.reserve("user", "job", 3) == 7
    billing.settle_success("job")
    assert billing.get_balance("user") == 7


@pytest.mark.parametrize("finish", ["settle", "reserved"])
def test_reservation_in_use_is_not_replaced(billed, finish):
    _, billing = billed
    billing.credit_purchase("user", "starter", "token", "order")
    billing.reserve("user", "job", 3)
    if finish == "settle":
        billing.settle_success("job")

    with pytest.raises(ValueError, match="already has a reservation"):
        billing.reserve("user", "job", 3)


def test_database_is_created_on_first_use_not_at_construction(tmp_path):
    from app.billing import Billing

    path = tmp_path / "missing" / "billing.sqlite3"
    billing = Billing(str(path))

    # Constructing touches nothing: modules create a Billing at import, also
    # where the billing directory is not available.
    assert not path.parent.exists()

    assert billing.reserved_job_ids() == []
    assert path.exists()


def test_connections_sync_every_commit(tmp_path):
    from contextlib import closing

    from app.billing import Billing

    with closing(Billing(str(tmp_path / "billing.sqlite3"))._connect()) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        # 2 is FULL.
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
