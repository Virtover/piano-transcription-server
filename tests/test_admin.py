import builtins
import json
import sys
import time
import types

import pytest

from app import controls, maintenance
from app.admin.__main__ import main
from app.billing import deleted_user_target
from app.outsource import routing
from app.worker import tasks


def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def billed(api_context):
    client, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 30}
    billing.credit_purchase("user", "starter", "secret-token", "GPA.1", "user@example.com")
    return client, settings, billing


def admin(capsys, *arguments, answer=None, monkeypatch=None):
    """Run python -m app.admin; returns (exit code, output)."""
    if answer is not None:
        monkeypatch.setattr(builtins, "input", lambda question: answer)
    code = main(list(arguments))
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def submit(client, token="user"):
    response = client.post("/api/transcriptions", headers=auth(token), json={"source_url": "https://example.com/video"})
    assert response.status_code == 202
    return response.json()["job_id"]


def fail(job_id, monkeypatch):
    module = types.ModuleType("app.transcription.pipeline")

    def transcribe_source(**kwargs):
        raise RuntimeError("download failed")

    module.transcribe_source = transcribe_source
    monkeypatch.setitem(sys.modules, "app.transcription.pipeline", module)
    tasks.transcribe_job.fn(job_id, "https://example.com/video")


def balance(billing):
    return billing.get_balance("user")


def audit_rows(billing):
    return [dict(row) for row in billing.read("SELECT * FROM admin_actions ORDER BY id")]


# --- status -------------------------------------------------------------------

def test_status_shows_jobs_workers_and_controls(billed, capsys):
    client, settings, _ = billed
    submit(client)
    routing.publish_heartbeat(tasks.redis, 4, 42.0)
    controls.pause(tasks.redis)
    maintenance.schedule(tasks.redis, "restart", 300, reason="upgrade")

    code, output = admin(capsys, "status")

    assert code == 0
    assert f"1 of {settings.limits.max_active_jobs} places used" in output
    assert "1 queued" in output
    assert "1 local, 0 remote" in output
    assert "4 jobs at once, average job 42 s" in output
    assert "Remote        off" in output
    assert "Server restart in 5 m (upgrade)" in output
    assert "submissions paused" in output


def test_status_without_a_worker(billed, capsys):
    _, output = admin(capsys, "status")

    assert "no heartbeat" in output
    assert "Maintenance   none planned" in output
    assert "Controls      none" in output


def test_status_as_json(billed, capsys):
    client, _, _ = billed
    submit(client)

    code, output = admin(capsys, "status", "--json")

    state = json.loads(output)
    assert code == 0
    assert state["jobs"]["queued"] == 1
    assert state["places"]["used"] == 1
    assert state["local_worker"] is None
    assert state["queues"]["default"] == {"messages": 0, "dead": 0}


# --- jobs ---------------------------------------------------------------------

def test_jobs_lists_recent_and_failed_jobs(billed, capsys, monkeypatch):
    client, _, _ = billed
    failed = submit(client)
    fail(failed, monkeypatch)
    queued = submit(client)

    _, output = admin(capsys, "jobs")
    assert failed[:8] in output and queued[:8] in output
    assert "user@example.com" in output

    _, output = admin(capsys, "jobs", "--failed", "--since", "1d")
    assert failed[:8] in output and queued[:8] not in output
    assert "RuntimeError: download failed" in output


def test_jobs_filter_by_user_email(billed, capsys):
    client, _, billing = billed
    billing.credit_purchase("other", "starter", "other-token", "GPA.2", "other@example.com")
    mine = submit(client)
    theirs = submit(client, "other")

    _, output = admin(capsys, "jobs", "--user", "OTHER@example.com")

    assert theirs[:8] in output and mine[:8] not in output


def test_jobs_rejects_a_bad_duration(billed, capsys):
    code, output = admin(capsys, "jobs", "--since", "yesterday")

    assert code == 1
    assert "--since must use a format" in output


def test_job_show_has_the_full_error_and_reservation(billed, capsys, monkeypatch):
    client, _, _ = billed
    job_id = submit(client)
    fail(job_id, monkeypatch)

    code, output = admin(capsys, "jobs", "show", job_id[:8])

    assert code == 0
    assert f"Job {job_id}" in output
    assert "error_detail: RuntimeError: download failed" in output
    assert "Reservation: released, 2 minutes reserved" in output
    assert "source_url: https://example.com/video" in output


def test_job_show_after_redis_expiry(billed, capsys, monkeypatch):
    client, _, _ = billed
    job_id = submit(client)
    fail(job_id, monkeypatch)
    tasks.redis.delete(tasks.job_key(job_id))

    _, output = admin(capsys, "jobs", "show", job_id)

    assert "Not in Redis" in output
    assert "error: RuntimeError: download failed" in output


def test_job_prefix_must_be_unique_and_known(billed, capsys):
    tasks.redis.hset(tasks.job_key("abc1-one"), "status", "preparing")
    tasks.redis.hset(tasks.job_key("abc2-two"), "status", "preparing")

    code, output = admin(capsys, "jobs", "show", "abc")
    assert code == 1 and "Several jobs start with abc" in output
    code, output = admin(capsys, "jobs", "show", "fff")
    assert code == 1 and "No job starts with fff" in output
    code, output = admin(capsys, "jobs", "show", "x*")
    assert code == 1 and "Not a job ID" in output


def test_cancel_charges_like_the_user_cancelling(billed, capsys):
    client, _, billing = billed
    job_id = submit(client)
    tasks.redis.hset(tasks.job_key(job_id), mapping={"status": "processing", "progress": "0.5"})

    code, output = admin(capsys, "jobs", "cancel", job_id, "--yes", "--by", "chris", "--reason", "stuck")

    assert code == 0
    assert "1 of 2 minutes are charged" in output
    assert tasks.redis.hget(tasks.job_key(job_id), "status") == "cancelled"
    assert balance(billing) == 29
    [action] = audit_rows(billing)
    assert (action["actor"], action["action"], action["target"], action["reason"]) == ("chris", "job.cancel", job_id, "stuck")


def test_cancel_with_refund_charges_nothing(billed, capsys):
    client, _, billing = billed
    job_id = submit(client)
    tasks.redis.hset(tasks.job_key(job_id), mapping={"status": "processing", "progress": "0.9"})

    admin(capsys, "jobs", "cancel", job_id, "--refund", "--yes")

    assert balance(billing) == 30
    reservation = billing.read("SELECT status FROM transcription_reservations WHERE job_id = ?", (job_id,))[0]
    assert reservation["status"] == "released"


def test_cancel_asks_first(billed, capsys, monkeypatch):
    client, _, billing = billed
    job_id = submit(client)

    code, output = admin(capsys, "jobs", "cancel", job_id, answer="n", monkeypatch=monkeypatch)

    assert code == 0 and "Nothing changed" in output
    assert tasks.redis.hget(tasks.job_key(job_id), "status") == "queued"
    assert audit_rows(billing) == []


def test_cancel_refuses_jobs_that_are_not_running(billed, capsys, monkeypatch):
    client, _, _ = billed
    job_id = submit(client)
    fail(job_id, monkeypatch)
    tasks.redis.hset(tasks.job_key("aaaa0001"), mapping={"status": "preparing"})

    code, output = admin(capsys, "jobs", "cancel", job_id, "--yes")
    assert code == 1 and "this one is failed" in output
    code, output = admin(capsys, "jobs", "cancel", "aaaa0001", "--yes")
    assert code == 1 and "expires on its own" in output


def test_requeue_queues_a_failed_job_again_under_its_id(billed, capsys, monkeypatch):
    client, settings, billing = billed
    job_id = submit(client)
    fail(job_id, monkeypatch)
    assert balance(billing) == 30
    sent = []
    monkeypatch.setattr(tasks.transcribe_job, "send", lambda *args, **kwargs: sent.append(args))

    code, output = admin(capsys, "jobs", "requeue", job_id, "--yes", "--reason", "YouTube hiccup")

    assert code == 0, output
    job = tasks.redis.hgetall(tasks.job_key(job_id))
    assert job["status"] == "queued"
    assert job["backend"] == "local"
    assert "error" not in job and "error_detail" not in job
    assert tasks.redis.ttl(tasks.job_key(job_id)) == -1
    assert sent == [(job_id, "https://example.com/video")]
    # Charged again like a new submission: the failure was refunded.
    assert balance(billing) == 28
    record = billing.read("SELECT * FROM job_diagnostics WHERE job_id = ?", (job_id,))[0]
    assert record["status"] == "queued"
    assert record["error"] is None and record["finished_at"] is None
    assert record["requeue_reason"] == "requeued by admin: YouTube hiccup"
    # The user's app sees it again.
    assert client.get(f"/api/transcriptions/{job_id}", headers=auth("user")).json()["status"] == "queued"


def test_requeued_job_can_be_charged_once_on_success(billed, capsys, monkeypatch):
    client, _, billing = billed
    job_id = submit(client)
    fail(job_id, monkeypatch)
    admin(capsys, "jobs", "requeue", job_id, "--yes")

    tasks.redis.hset(tasks.job_key(job_id), mapping={"status": "completed"})
    billing.settle_success(job_id)
    billing.settle_success(job_id)

    assert balance(billing) == 28


def test_requeue_on_a_chosen_backend(billed, capsys, monkeypatch):
    client, settings, _ = billed
    job_id = submit(client)
    fail(job_id, monkeypatch)

    code, output = admin(capsys, "jobs", "requeue", job_id, "--remote", "--yes")
    assert code == 1 and "Outsourcing is off" in output

    settings.outsource.provider = "runpod"
    code, output = admin(capsys, "jobs", "requeue", job_id, "--remote", "--yes")
    assert code == 0, output
    assert tasks.redis.hget(tasks.job_key(job_id), "backend") == "remote"
    assert tasks.redis.sismember(routing.ACTIVE_KEYS["remote"], job_id)


def test_requeue_refuses_what_cannot_run_again(billed, capsys, monkeypatch):
    client, _, _ = billed
    queued = submit(client)
    tasks.redis.hset(tasks.job_key("bbbb0001"), mapping={"status": "failed", "source": "upload", "user_id": "user"})

    code, output = admin(capsys, "jobs", "requeue", queued, "--yes")
    assert code == 1 and "this one is queued" in output
    code, output = admin(capsys, "jobs", "requeue", "bbbb0001", "--yes")
    assert code == 1 and "upload it again" in output


def test_failed_requeue_leaves_the_job_failed(billed, capsys, monkeypatch):
    client, _, billing = billed
    job_id = submit(client)
    fail(job_id, monkeypatch)
    billing.adjust_minutes("user", -30, "test")

    code, output = admin(capsys, "jobs", "requeue", job_id, "--yes")

    assert code == 1
    assert "Insufficient transcription minutes (HTTP 402)" in output
    job = tasks.redis.hgetall(tasks.job_key(job_id))
    assert job["status"] == "failed"
    assert job["error_detail"] == "RuntimeError: download failed"
    assert tasks.redis.ttl(tasks.job_key(job_id)) > 0
    assert audit_rows(billing) == []


# --- user ---------------------------------------------------------------------

def test_user_shows_balance_purchases_and_history(billed, capsys, monkeypatch):
    client, _, billing = billed
    job_id = submit(client)
    fail(job_id, monkeypatch)

    code, output = admin(capsys, "user", "USER@example.com")

    assert code == 0
    assert "balance: 30 minutes, 0 reserved by running jobs, 30 available" in output
    assert "GPA.1" in output
    # Purchase tokens are never printed.
    assert "secret-token" not in output
    assert "purchase  +30" in output
    assert job_id[:8] in output


def test_user_shows_a_block(billed, capsys):
    controls.block(tasks.redis, "user", "chargeback fraud", seconds=3600)

    _, output = admin(capsys, "user", "user")

    assert "BLOCKED for 1.0 h more: chargeback fraud" in output


def test_unknown_user_and_billing_off(billed, capsys):
    _, settings, _ = billed

    code, output = admin(capsys, "user", "nobody@example.com")
    assert code == 1 and "No user with the email address or ID nobody@example.com" in output

    settings.billing.provider = "none"
    code, output = admin(capsys, "user", "user@example.com")
    assert code == 1 and "Billing is off" in output


def test_credit_adds_minutes_with_a_logged_reason(billed, capsys):
    _, _, billing = billed

    code, output = admin(capsys, "user", "user@example.com", "credit", "15", "--reason", "refund for job abc", "--by", "chris")

    assert code == 0
    assert "Credited 15 minutes to user@example.com; balance 45 minutes" in output
    entry = billing.read("SELECT * FROM minute_ledger WHERE kind = 'admin'")[0]
    assert (entry["minutes"], entry["note"]) == (15, "refund for job abc")
    [action] = audit_rows(billing)
    assert (action["action"], action["target"], action["detail"]) == ("user.credit", "user", "+15 minutes")


def test_debit_asks_first_and_may_go_negative(billed, capsys, monkeypatch):
    _, _, billing = billed

    code, output = admin(capsys, "user", "user@example.com", "credit", "-40", "--reason", "chargeback", answer="n", monkeypatch=monkeypatch)
    assert "balance 30 -> -10 minutes" in output and "Nothing changed" in output
    assert balance(billing) == 30

    admin(capsys, "user", "user@example.com", "credit", "-40", "--reason", "chargeback", answer="y", monkeypatch=monkeypatch)
    assert balance(billing) == -10


def test_credit_rejects_zero(billed, capsys):
    code, output = admin(capsys, "user", "user@example.com", "credit", "0")

    assert code == 1 and "other than 0" in output


def test_delete_removes_the_account_and_anonymises_its_logs(billed, capsys):
    client, settings, billing = billed
    job_id = submit(client)
    admin(capsys, "user", "user", "credit", "5", "--reason", "goodwill")
    controls.block(tasks.redis, "user")

    code, output = admin(capsys, "user", "user@example.com", "delete", "--yes", "--reason", "deletion request")

    assert code == 0 and "Deleted the account of user@example.com" in output
    assert billing.read("SELECT * FROM users WHERE user_id = 'user'") == []
    assert not tasks.redis.exists(tasks.job_key(job_id))
    assert controls.blocked(tasks.redis, "user") is None
    assert {row["user_id"] for row in billing.read("SELECT user_id FROM minute_ledger")} == {"deleted"}
    targets = [row["target"] for row in audit_rows(billing)]
    assert targets == [deleted_user_target("user")] * 2
    assert "user" not in targets


def test_delete_asks_first(billed, capsys, monkeypatch):
    _, _, billing = billed

    code, output = admin(capsys, "user", "user@example.com", "delete", answer="", monkeypatch=monkeypatch)

    assert "Nothing changed" in output
    assert billing.read("SELECT * FROM users WHERE user_id = 'user'")


# --- billing ------------------------------------------------------------------

def test_billing_report_totals_minutes_per_period(billed, capsys, monkeypatch):
    client, settings, billing = billed
    settings.billing.free_minutes = 5
    billing.grant_free_minutes("fresh", "fresh@example.com")
    job_id = submit(client)
    tasks.redis.hset(tasks.job_key(job_id), "status", "completed")
    billing.settle_success(job_id)
    billing.void_purchase("secret-token")
    billing.adjust_minutes("user", 3, "goodwill")

    code, output = admin(capsys, "billing", "--per", "month")

    assert code == 0
    total = next(line for line in output.splitlines() if line.startswith("Total"))
    # Sold, free, used, voided, by hand.
    assert total.split() == ["Total", "30", "5", "2", "30", "+3"]
    assert "Negative balances: none" in output


def test_billing_report_says_when_the_ledger_started(billed, capsys):
    _, _, billing = billed
    billing.adjust_minutes("user", -5, "chargeback")
    billing.set_state("minute_ledger_since", str(int(time.time()) - 60 * 86400))

    _, output = admin(capsys, "billing", "--since", "1d")
    assert next(line for line in output.splitlines() if line.startswith("Total")).split()[-1] == "-5"
    assert "earlier ones are not in this report" not in output

    billing.set_state("minute_ledger_since", str(int(time.time()) - 3600))
    _, output = admin(capsys, "billing", "--since", "1d")
    assert "Balance changes are recorded from" in output


def test_billing_report_lists_negative_balances_and_sync(billed, capsys):
    client, settings, billing = billed
    billing.adjust_minutes("user", -35, "chargeback")
    billing.set_state("google_play_voided_synced_until", "1700000000000")

    _, output = admin(capsys, "billing")

    assert "Negative balances: 1 user, owing 5 minutes" in output
    assert "user@example.com" in output
    assert "Voided purchases: last synced" in output and "2023-11-14" in output


def test_billing_report_estimates_remote_cost(billed, capsys):
    _, _, billing = billed
    for job_id, backend in (("a", "remote"), ("b", "remote"), ("c", "local")):
        billing.record_job(job_id, {"status": "queued", "backend": backend, "user_id": "user"})
    import sqlite3

    with sqlite3.connect(billing.database_path) as connection:
        connection.execute("UPDATE job_diagnostics SET status = 'completed', started_at = created_at, finished_at = created_at + 1800")

    _, output = admin(capsys, "billing", "--remote-price-per-hour", "0.5")

    assert "Estimated remote cost: 0.50 at 0.5 per hour" in output
    assert "remote   2     1.00   0.50" in output


def test_billing_report_with_billing_off(billed, capsys):
    _, settings, _ = billed
    settings.billing.provider = "none"

    code, output = admin(capsys, "billing")

    assert code == 0
    assert "Billing is off" in output
    assert "Completed jobs" in output


# --- limits -------------------------------------------------------------------

def test_limits_show_the_defaults(billed, capsys):
    _, settings, _ = billed

    _, output = admin(capsys, "limits")

    assert "Submissions:     accepted" in output
    assert f"Per-user limit:  {settings.limits.max_active_jobs_per_user} (MAX_ACTIVE_JOBS_PER_USER)" in output
    assert "Blocked users:   none" in output


def test_limits_pause_and_resume(billed, capsys):
    client, _, billing = billed

    code, output = admin(capsys, "limits", "pause", "--message", "Upgrading", "--for", "30m", "--by", "chris")
    assert code == 0 and "paused for 30 m" in output
    response = client.post("/api/transcriptions", headers=auth("user"), json={"source_url": "https://example.com/video"})
    assert response.status_code == 503 and response.json()["detail"] == "Upgrading"
    _, output = admin(capsys, "limits")
    assert "paused for 30 m: Upgrading" in output

    admin(capsys, "limits", "resume")
    submit(client)
    assert [row["action"] for row in audit_rows(billing)] == ["limits.pause", "limits.resume"]


def test_limits_links_off_and_on(billed, capsys):
    code, output = admin(capsys, "limits", "links", "off")
    assert code == 0 and "uploads are still accepted" in output
    assert controls.links_off(tasks.redis) is not None

    admin(capsys, "limits", "links", "on")
    assert controls.links_off(tasks.redis) is None


def test_limits_block_by_email(billed, capsys):
    client, _, billing = billed

    code, output = admin(capsys, "limits", "block", "user@example.com", "--reason", "abuse", "--for", "1d")

    assert code == 0
    response = client.post("/api/transcriptions", headers=auth("user"), json={"source_url": "https://example.com/video"})
    assert response.status_code == 403
    _, output = admin(capsys, "limits")
    assert "user@example.com for 24.0 h: abuse" in output

    admin(capsys, "limits", "unblock", "user@example.com")
    submit(client)
    assert [row["target"] for row in audit_rows(billing)] == ["user", "user"]


def test_limits_per_user(billed, capsys):
    _, settings, _ = billed

    code, output = admin(capsys, "limits", "per-user", "2", "--for", "2h")
    assert code == 0 and controls.per_user_job_limit(tasks.redis) == 2

    code, output = admin(capsys, "limits", "per-user", "0")
    assert code == 1 and "1 or more" in output

    admin(capsys, "limits", "per-user", "default")
    assert controls.per_user_job_limit(tasks.redis) == settings.limits.max_active_jobs_per_user

    settings.billing.provider = "none"
    code, output = admin(capsys, "limits", "per-user", "2")
    assert code == 1 and "only with billing on" in output


def test_limits_reject_a_bad_duration(billed, capsys):
    code, output = admin(capsys, "limits", "pause", "--for", "soon")

    assert code == 1 and "--for must use a format" in output
    assert controls.paused(tasks.redis) is None


# --- audit --------------------------------------------------------------------

def test_audit_lists_actions_newest_first(billed, capsys):
    admin(capsys, "limits", "pause", "--by", "chris")
    admin(capsys, "limits", "resume", "--by", "sam", "--reason", "done")

    _, output = admin(capsys, "audit")

    lines = output.splitlines()
    assert "sam" in lines[2] and "limits.resume" in lines[2] and "done" in lines[2]
    assert "chris" in lines[3] and "limits.pause" in lines[3]


def test_audit_when_empty(billed, capsys):
    assert admin(capsys, "audit")[1].strip() == "No admin actions yet"


def test_admin_output_never_contains_secrets(billed, capsys, monkeypatch):
    client, settings, _ = billed
    job_id = submit(client)
    fail(job_id, monkeypatch)
    settings.secrets.server_password = "a-long-password-123"

    outputs = [
        admin(capsys, *arguments)[1]
        for arguments in (["status", "--json"], ["jobs"], ["jobs", "show", job_id], ["user", "user"], ["billing"], ["limits"])
    ]

    for output in outputs:
        assert "a-long-password-123" not in output
        assert "secret-token" not in output


# --- Data functions -----------------------------------------------------------
# What a web page would call: plain dicts, the same checks and audit entries.

def test_read_functions_return_plain_data(billed, monkeypatch):
    from app.admin import audit, jobs, limits, report, status, users

    client, _, _ = billed
    job_id = submit(client)
    fail(job_id, monkeypatch)
    controls.block(tasks.redis, "user", "spam")

    results = [
        status.collect(),
        jobs.list_jobs(status="failed"),
        jobs.job_details(job_id[:8]),
        users.account("user@example.com"),
        report.billing_report(since=86400),
        limits.current(),
        audit.entries(),
    ]

    for result in results:
        assert json.loads(json.dumps(result)) == result
    assert results[1][0]["job_id"] == job_id
    assert results[2]["record"]["error"] == "RuntimeError: download failed"
    assert results[3]["purchases"] == [{"product_id": "starter", "order_id": "GPA.1", "minutes": 30, "voided_at": None}]
    assert results[4]["minutes"]["total"]["purchase"] == 30
    assert results[5]["blocked"] == [{"user_id": "user", "email": "user@example.com", "reason": "spam", "expires_in": None}]


def test_plans_change_nothing(billed, monkeypatch):
    from app.admin import jobs, users

    client, _, billing = billed
    queued = submit(client)
    failed = submit(client)
    fail(failed, monkeypatch)

    assert jobs.cancel_plan(queued)["charge"] == 0
    assert jobs.cancel_plan(queued, refund=True)["charge"] is None
    assert jobs.requeue_plan(failed)["charged"] is True
    assert users.credit_plan("user", -40)["balance_after"] == -10
    assert users.delete_plan("user")["email"] == "user@example.com"

    assert tasks.redis.hget(tasks.job_key(queued), "status") == "queued"
    assert tasks.redis.hget(tasks.job_key(failed), "status") == "failed"
    assert billing.read("SELECT minutes FROM users WHERE user_id = 'user'")[0]["minutes"] == 30
    assert audit_rows(billing) == []


def test_actions_without_the_command_line(billed, monkeypatch):
    from app.admin import jobs, limits, users
    from app.admin.common import AdminError

    client, _, billing = billed
    job_id = submit(client)

    assert jobs.cancel(job_id, False, "web:sam", "stuck")["job_id"] == job_id
    assert users.credit("user@example.com", 5, "web:sam")["balance_after"] == 35
    assert limits.block("user", 3600, "web:sam", "abuse")["expires_in"] == 3600
    assert limits.set_per_user_limit(None, None, "web:sam")["changed"] is False
    with pytest.raises(AdminError, match="this one is cancelled"):
        jobs.cancel(job_id, False, "web:sam")

    assert [(row["actor"], row["action"]) for row in audit_rows(billing)] == [
        ("web:sam", "job.cancel"),
        ("web:sam", "user.credit"),
        ("web:sam", "limits.block"),
        ("web:sam", "limits.per_user"),
    ]


@pytest.mark.parametrize(
    "arguments",
    [["jobs"], ["user", "user"], ["billing"], ["limits"], ["audit"], ["status"]],
)
def test_read_commands_print_json(billed, capsys, arguments):
    client, _, _ = billed
    submit(client)

    code, output = admin(capsys, *arguments, "--json")

    assert code == 0
    json.loads(output)


def test_job_show_prints_json(billed, capsys):
    client, _, _ = billed
    job_id = submit(client)

    _, output = admin(capsys, "jobs", "show", job_id, "--json")

    assert json.loads(output)["live"]["status"] == "queued"
