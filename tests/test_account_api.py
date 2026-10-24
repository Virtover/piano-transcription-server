import sqlite3
import time
from pathlib import Path

from app.worker import tasks


def enable_billing(settings, free_minutes=5):
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 10}
    settings.billing.free_minutes = free_minutes
    settings.billing.free_minutes_period = "1d"


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def rows(settings, query, *params):
    with sqlite3.connect(settings.billing.database_path) as connection:
        return connection.execute(query, params).fetchall()


def test_delete_account_is_unavailable_without_billing(api_context):
    client, _, _ = api_context

    response = client.delete("/api/account")

    assert response.status_code == 409


def test_delete_account_requires_authentication(api_context):
    client, settings, _ = api_context
    enable_billing(settings)

    assert client.delete("/api/account").status_code == 401
    assert client.delete("/api/account", headers=auth("invalid")).status_code == 401


def test_delete_account_removes_user_data_jobs_and_files(api_context):
    client, settings, billing = api_context
    enable_billing(settings)
    billing.credit_purchase("owner", "starter", "purchase-token", "order-1", "owner@example.com")

    created = client.post(
        "/api/transcriptions",
        headers=auth("owner"),
        json={"source_url": "https://example.com/video"},
    )
    assert created.status_code == 202
    job_id = created.json()["job_id"]
    job_dir = Path(settings.service.data_dir) / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "transcription.mid").write_bytes(b"MThd")

    other = client.post(
        "/api/transcriptions",
        headers=auth("other"),
        json={"source_url": "https://example.com/video"},
    )
    assert other.status_code == 202

    response = client.delete("/api/account", headers=auth("owner"))

    assert response.status_code == 204
    assert not tasks.redis.exists(tasks.job_key(job_id))
    assert not job_dir.exists()
    assert tasks.redis.exists(tasks.job_key(other.json()["job_id"]))
    assert rows(settings, "SELECT * FROM users WHERE user_id = 'owner'") == []
    assert rows(settings, "SELECT * FROM users WHERE email = 'owner@example.com'") == []
    assert rows(settings, "SELECT * FROM transcription_reservations WHERE user_id = 'owner'") == []
    assert rows(settings, "SELECT user_id, order_id FROM purchases") == [("deleted", "order-1")]
    assert rows(settings, "SELECT user_id FROM users") == [("other",)]


def test_deleted_purchase_token_cannot_be_credited_again(api_context):
    _, settings, billing = api_context
    enable_billing(settings)
    billing.credit_purchase("owner", "starter", "purchase-token", "order-1")

    billing.delete_user("owner")

    assert billing.credit_purchase("owner", "starter", "purchase-token", "order-1") == 0
    assert billing.get_balance("owner") == 0


def test_deleting_account_does_not_reset_free_minutes(api_context):
    client, settings, billing = api_context
    enable_billing(settings, free_minutes=5)

    assert client.get("/api/billing/balance", headers=auth("owner")).json()["minutes"] == 5
    assert client.delete("/api/account", headers=auth("owner")).status_code == 204

    recreated = client.get("/api/billing/balance", headers=auth("owner")).json()
    assert recreated["minutes"] == 0
    assert recreated["free_minutes_seconds_until_next_grant"] > 0
    assert rows(settings, "SELECT * FROM deleted_users") == []


def test_deleted_account_gets_free_minutes_after_period(api_context):
    client, settings, billing = api_context
    enable_billing(settings, free_minutes=5)
    client.get("/api/billing/balance", headers=auth("owner"))
    with sqlite3.connect(settings.billing.database_path) as connection:
        connection.execute(
            "UPDATE users SET free_grant_at = ? WHERE user_id = 'owner'",
            (int(time.time()) - 2 * 24 * 60 * 60,),
        )

    client.delete("/api/account", headers=auth("owner"))

    assert rows(settings, "SELECT * FROM deleted_users") == []
    assert client.get("/api/billing/balance", headers=auth("owner")).json()["minutes"] == 5


def test_deletion_marker_stores_no_user_id(api_context):
    client, settings, billing = api_context
    enable_billing(settings, free_minutes=5)
    client.get("/api/billing/balance", headers=auth("owner"))

    client.delete("/api/account", headers=auth("owner"))

    markers = rows(settings, "SELECT user_hash FROM deleted_users")
    assert len(markers) == 1
    assert "owner" not in markers[0][0]


def test_no_deletion_marker_without_free_minutes(api_context):
    client, settings, billing = api_context
    enable_billing(settings, free_minutes=None)
    billing.credit_purchase("owner", "starter", "purchase-token", "order-1")

    assert client.delete("/api/account", headers=auth("owner")).status_code == 204
    assert rows(settings, "SELECT * FROM deleted_users") == []


def test_deleting_unknown_account_succeeds(api_context):
    client, settings, billing = api_context
    enable_billing(settings, free_minutes=None)

    assert client.delete("/api/account", headers=auth("never-seen")).status_code == 204
    assert billing.delete_user("never-seen") is False


def test_running_job_of_deleted_account_is_not_charged(api_context, monkeypatch):
    client, settings, billing = api_context
    enable_billing(settings, free_minutes=None)
    billing.credit_purchase("owner", "starter", "purchase-token", "order-1")
    created = client.post(
        "/api/transcriptions",
        headers=auth("owner"),
        json={"source_url": "https://example.com/video"},
    ).json()
    tasks.update_job(created["job_id"], status="processing", progress="0.5")

    client.delete("/api/account", headers=auth("owner"))

    # The worker notices on its next progress check and stops.
    assert tasks.is_job_cancelled(created["job_id"])
    tasks.reconcile_billing()
    assert billing.reserved_job_ids() == []


def run_admin_delete(monkeypatch, *arguments, answer=None):
    from app.admin.__main__ import main

    if answer is not None:
        monkeypatch.setattr("builtins.input", lambda prompt: answer)
    return main(["user", *arguments])


def test_admin_delete_command_removes_account(api_context, monkeypatch, capsys):
    client, settings, billing = api_context
    enable_billing(settings, free_minutes=None)
    billing.credit_purchase("owner", "starter", "purchase-token", "order-1", "owner@example.com")
    job_id = client.post(
        "/api/transcriptions",
        headers=auth("owner"),
        json={"source_url": "https://example.com/video"},
    ).json()["job_id"]

    assert run_admin_delete(monkeypatch, " Owner@Example.com ", "delete", "--yes") == 0

    assert "Deleted the account of owner@example.com" in capsys.readouterr().out
    assert rows(settings, "SELECT * FROM users") == []
    assert rows(settings, "SELECT user_id FROM purchases") == [("deleted",)]
    assert not tasks.redis.exists(tasks.job_key(job_id))


def test_admin_delete_command_asks_for_confirmation(api_context, monkeypatch, capsys):
    _, settings, billing = api_context
    enable_billing(settings, free_minutes=None)
    billing.credit_purchase("owner", "starter", "purchase-token", "order-1", "owner@example.com")

    run_admin_delete(monkeypatch, "owner@example.com", "delete", answer="n")
    assert "Nothing changed" in capsys.readouterr().out
    assert billing.get_balance("owner") == 10

    run_admin_delete(monkeypatch, "owner@example.com", "delete", answer="y")
    assert billing.get_balance("owner") == 0
    assert rows(settings, "SELECT * FROM users") == []


def test_admin_delete_command_rejects_unknown_email(api_context, monkeypatch, capsys):
    _, settings, _ = api_context
    enable_billing(settings, free_minutes=None)

    assert run_admin_delete(monkeypatch, "missing@example.com", "delete", "--yes") == 1
    assert "missing@example.com" in capsys.readouterr().err
