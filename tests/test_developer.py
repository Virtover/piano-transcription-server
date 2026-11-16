"""Developer accounts: their jobs are never charged nor held to the per-user
job limit, and they alone see job details and the server's state."""
import pytest

from app.billing import Billing


def bearer(user):
    return {"Authorization": f"Bearer {user}"}


@pytest.fixture
def billed(api_context):
    client, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.free_minutes = 3
    settings.billing.free_minutes_period = "30d"
    for user in ("dev", "regular"):
        # The first request creates the account, with its monthly minutes.
        assert client.get("/api/billing/balance", headers=bearer(user)).status_code == 200
    assert billing.set_developer("dev", True, "test")
    return client, settings, billing


def submit(client, user):
    return client.post("/api/transcriptions", headers=bearer(user), json={"source_url": "https://example.com/video"})


def test_a_developer_is_never_charged(billed):
    client, _, billing = billed

    # Each job costs 2 of the 3 minutes a user has.
    for _ in range(3):
        assert submit(client, "dev").status_code == 202

    balance = client.get("/api/billing/balance", headers=bearer("dev")).json()
    assert balance["developer"] is True and balance["minutes"] == 3
    assert billing.reserved_job_ids() == []


def test_a_developer_with_nothing_left_can_still_transcribe(billed):
    client, _, billing = billed
    billing.adjust_minutes("dev", -10, "test")

    assert submit(client, "dev").status_code == 202
    assert submit(client, "regular").status_code == 202
    assert submit(client, "regular").status_code == 402


def test_a_developer_is_not_held_to_the_per_user_limit(billed):
    client, settings, billing = billed
    settings.limits.max_active_jobs_per_user = 1
    billing.adjust_minutes("regular", 100, "test")

    assert [submit(client, "dev").status_code for _ in range(3)] == [202, 202, 202]
    assert [submit(client, "regular").status_code for _ in range(2)] == [202, 429]


def test_only_a_developer_sees_job_details(billed):
    client, _, _ = billed
    developer_job = submit(client, "dev").json()["job_id"]
    regular_job = submit(client, "regular").json()["job_id"]

    details = client.get(f"/api/transcriptions/{developer_job}", headers=bearer("dev")).json()["developer"]
    assert details["cost_minutes"] == 2 and details["duration_seconds"] == 130.0
    assert details["outsourced"] is False and details["queued_at"] > 0

    assert client.get(f"/api/transcriptions/{regular_job}", headers=bearer("regular")).json()["developer"] is None


def test_the_server_state_is_only_for_developers(billed):
    client, _, _ = billed
    submit(client, "dev")

    assert client.get("/api/developer/server").status_code == 401
    assert client.get("/api/developer/server", headers=bearer("regular")).status_code == 403

    state = client.get("/api/developer/server", headers=bearer("dev")).json()
    assert state["active_jobs"] == 1
    assert state["backends"]["local"]["waiting"] == 1
    assert state["paused"] is False and state["outsourcing"] is False


def test_ending_a_developer_account_charges_again(billed):
    client, _, billing = billed

    assert billing.set_developer("dev", False, "test")
    assert not billing.set_developer("dev", False, "test")

    assert submit(client, "dev").status_code == 202
    balance = client.get("/api/billing/balance", headers=bearer("dev")).json()
    assert (balance["minutes"], balance["developer"]) == (1, False)
    assert [row["action"] for row in billing.read("SELECT action FROM admin_actions ORDER BY id")] == [
        "user.developer", "user.developer",
    ]


def test_a_database_that_cannot_be_reached_leaves_no_placeholder(billed, monkeypatch):
    client, _, billing = billed
    from app.api.routes import transcriptions
    from app.worker import tasks

    down = [True]
    reachable = billing.is_developer

    def unreachable(user_id):
        if down[0]:
            raise ConnectionError("database down")
        return reachable(user_id)

    monkeypatch.setattr(billing, "is_developer", unreachable)
    with pytest.raises(ConnectionError):
        client.post(
            "/api/transcriptions",
            headers={**bearer("dev"), "Idempotency-Key": "retry-me"},
            json={"source_url": "https://example.com/video"},
        )
    assert list(transcriptions.redis.scan_iter(match=tasks.job_key("*"))) == []

    # Back up: the same submission goes through instead of waiting out a placeholder.
    down[0] = False
    response = client.post(
        "/api/transcriptions",
        headers={**bearer("dev"), "Idempotency-Key": "retry-me"},
        json={"source_url": "https://example.com/video"},
    )
    assert response.status_code == 202


def test_recovery_after_a_crash_leaves_a_developers_jobs_alone(billed):
    client, _, billing = billed
    job_id = submit(client, "dev").json()["job_id"]
    from app.worker import tasks

    # What the API runs at start, after a crash or a power cut: nothing is held, so nothing changes.
    tasks.reconcile_billing()
    billing.settle_success(job_id)
    billing.release(job_id)

    assert client.get("/api/billing/balance", headers=bearer("dev")).json()["minutes"] == 3


def test_a_developers_job_ended_on_a_cluster_node_settles_once_and_free(billed):
    client, _, billing = billed
    from app import cluster
    from app.api.routes import transcriptions
    from app.worker import tasks
    job_id = submit(client, "dev").json()["job_id"]
    # A node never reaches the database: it marks the job ended and names it for the hub.
    transcriptions.redis.hset(tasks.job_key(job_id), "status", "completed")
    cluster.HubBilling(transcriptions.redis).settle_success(job_id)

    assert tasks.settle_node_jobs() == 1
    # Settled for good: not looked at again, whatever it holds (nothing).
    assert transcriptions.redis.zcard(cluster.SETTLE_KEY) == 0
    assert client.get("/api/billing/balance", headers=bearer("dev")).json()["minutes"] == 3


def test_ending_a_developer_account_mid_job_does_not_charge_that_job(billed):
    client, _, billing = billed
    job_id = submit(client, "dev").json()["job_id"]

    billing.set_developer("dev", False, "test")
    billing.settle_success(job_id)

    assert client.get("/api/billing/balance", headers=bearer("dev")).json()["minutes"] == 3


def test_an_old_database_gets_the_developer_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE users (user_id TEXT PRIMARY KEY, email TEXT UNIQUE, minutes INTEGER NOT NULL DEFAULT 0, "
            "free_grant_at INTEGER NOT NULL DEFAULT 0)"
        )
        connection.execute("INSERT INTO users (user_id, minutes) VALUES ('old', 5)")

    billing = Billing(str(path))

    assert billing.read("SELECT developer FROM users WHERE user_id = 'old'")[0]["developer"] == 0
