import json
from pathlib import Path

import fakeredis
import pytest

from app import main as app_main
from app import maintenance
from app.outsource import routing
from app.worker import tasks


@pytest.fixture
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


def auth(token="user"):
    return {"Authorization": f"Bearer {token}"}


# --- Notice -----------------------------------------------------------------

@pytest.mark.parametrize(
    "seconds, text",
    [(300, "Server restart in 5 m"), (299, "Server restart in 5 m"), (89, "Server restart in 1 m"),
     (90, "Server restart in 2 m"), (64, "Server restart in 1 m"), (60, "Server restart in 1 m"),
     (59, "Server restart in 59 s"), (1, "Server restart in 1 s"), (0, "Server is restarting")],
)
def test_restart_message(seconds, text):
    assert maintenance.message("restart", seconds) == text


@pytest.mark.parametrize(
    "seconds, text",
    [(600, "Server shutting down in 10 m"), (30, "Server shutting down in 30 s"), (0, "Server is shutting down")],
)
def test_shutdown_message(seconds, text):
    assert maintenance.message("shutdown", seconds) == text


def test_schedule_status_and_cancel(redis):
    notice = maintenance.schedule(redis, "restart", 300, grace_seconds=120, reason="Update", now=1000)

    assert maintenance.current(redis) == notice
    assert notice.as_response(now=1000).model_dump() == {
        "action": "restart",
        "at": 1300,
        "seconds_remaining": 300,
        "message": "Server restart in 5 m",
        "reason": "Update",
        "back_at": None,
    }
    assert maintenance.cancel(redis)
    assert maintenance.current(redis) is None
    assert not maintenance.cancel(redis)


def test_shutdown_with_return_time(redis):
    notice = maintenance.schedule(redis, "shutdown", 600, reason="Hardware", back_in_seconds=7200, now=1000)

    assert maintenance.current(redis) == notice
    response = notice.as_response(now=1000)
    assert (response.action, response.at, response.back_at) == ("shutdown", 1600, 8200)
    assert response.message == "Server shutting down in 10 m"


def test_return_time_is_ignored_for_restarts(redis):
    notice = maintenance.schedule(redis, "restart", 60, back_in_seconds=7200, now=1000)

    assert notice.back_at is None


def test_unknown_action_is_rejected(redis):
    with pytest.raises(ValueError):
        maintenance.schedule(redis, "reboot", 60)


def test_notice_expires_after_the_grace_period(redis):
    maintenance.schedule(redis, "restart", 300, grace_seconds=120)

    ttl = redis.ttl(maintenance.NOTICE_KEY)
    assert 300 + 120 < ttl <= 300 + 120 + maintenance.NOTICE_SLACK_SECONDS


def test_rescheduling_replaces_the_notice(redis):
    maintenance.schedule(redis, "shutdown", 300, reason="First", back_in_seconds=3600, now=1000)
    maintenance.schedule(redis, "restart", 60, now=1000)

    notice = maintenance.current(redis)
    assert (notice.action, notice.at, notice.reason, notice.back_at) == ("restart", 1060, None, None)


def test_past_notices_are_cleared_at_startup(redis):
    maintenance.schedule(redis, "shutdown", 60, now=1000)

    assert not maintenance.clear_past(redis, now=1030)
    assert maintenance.clear_past(redis, now=1060)
    assert maintenance.current(redis) is None


def test_retry_after(redis):
    restart = maintenance.schedule(redis, "restart", 100, grace_seconds=50, now=1000)
    assert restart.retry_after(now=1000) == 100 + 50 + maintenance.RESTART_SECONDS

    unknown = maintenance.schedule(redis, "shutdown", 100, grace_seconds=50, now=1000)
    assert unknown.retry_after(now=1000) is None

    known = maintenance.schedule(redis, "shutdown", 100, grace_seconds=50, back_in_seconds=7200, now=1000)
    assert known.retry_after(now=1000) == 7200

    # A return time before the shutdown could even finish is not believed.
    early = maintenance.schedule(redis, "shutdown", 100, grace_seconds=50, back_in_seconds=10, now=1000)
    assert early.retry_after(now=1000) == 100 + 50 + maintenance.RESTART_SECONDS


@pytest.mark.parametrize("action", maintenance.ACTIONS)
@pytest.mark.parametrize(
    "delay, estimate, refused",
    [(600, 120, False), (600, 700, True), (59, 1, True), (0, 1, True)],
)
def test_refusal(redis, action, delay, estimate, refused):
    maintenance.schedule(redis, action, delay, now=1000)

    assert (maintenance.refusal(redis, estimate, now=1000) is not None) == refused


def test_no_refusal_without_notice(redis):
    assert maintenance.refusal(redis, 10_000) is None


def test_processing_jobs_ignore_other_states(redis):
    for job_id, status in [("a", "processing"), ("b", "queued"), ("c", "completed"), ("d", "processing")]:
        redis.hset(tasks.job_key(job_id), "status", status)

    assert sorted(maintenance.processing_jobs(redis)) == ["a", "d"]


def test_cli(redis, monkeypatch, capsys):
    monkeypatch.setattr(maintenance.Redis, "from_url", lambda *args, **kwargs: redis)

    assert maintenance.main(["schedule", "restart", "5m", "--grace", "0s", "--reason", "Update"]) == 0
    scheduled = json.loads(capsys.readouterr().out)
    assert scheduled["action"] == "restart"
    assert scheduled["seconds_remaining"] in (299, 300)
    assert scheduled["reason"] == "Update"

    maintenance.main(["status"])
    assert json.loads(capsys.readouterr().out)["at"] == scheduled["at"]

    redis.hset(tasks.job_key("running"), "status", "processing")
    maintenance.main(["active-jobs"])
    assert capsys.readouterr().out.strip() == "1"

    maintenance.main(["cancel"])
    assert capsys.readouterr().out.strip() == "cancelled"
    maintenance.main(["status"])
    assert json.loads(capsys.readouterr().out) is None


def test_cli_schedules_a_shutdown_with_return_time(redis, monkeypatch, capsys):
    monkeypatch.setattr(maintenance.Redis, "from_url", lambda *args, **kwargs: redis)

    maintenance.main(["schedule", "shutdown", "10m", "--back-in", "2h"])

    scheduled = json.loads(capsys.readouterr().out)
    assert scheduled["action"] == "shutdown"
    assert scheduled["back_at"] - scheduled["at"] in (6599, 6600, 6601)


@pytest.mark.parametrize(
    "arguments",
    [["schedule", "restart", "soon"], ["schedule", "reboot", "5m"], ["schedule", "restart", "5m", "--back-in", "1h"]],
)
def test_cli_rejects_invalid_arguments(redis, monkeypatch, arguments):
    monkeypatch.setattr(maintenance.Redis, "from_url", lambda *args, **kwargs: redis)

    with pytest.raises(SystemExit):
        maintenance.main(arguments)


# --- API --------------------------------------------------------------------

@pytest.fixture
def api(api_context):
    client, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 10}
    billing.credit_purchase("user", "starter", "token", "order")
    routing.publish_heartbeat(tasks.redis, capacity=2, average_job_seconds=60)
    return client, settings, billing


def submit(client):
    return client.post("/api/transcriptions", headers=auth(), json={"source_url": "https://example.com/video"})


def test_no_notice_by_default(api):
    client, _, _ = api

    response = client.get("/api/server-info")

    assert response.json()["maintenance"] is None
    assert "X-Server-Maintenance" not in response.headers
    assert "X-Server-Maintenance-At" not in response.headers


@pytest.mark.parametrize("action, text", [("restart", "Server restart in 5 m"), ("shutdown", "Server shutting down in 5 m")])
def test_server_info_and_headers_show_the_notice(api, action, text):
    client, _, _ = api
    maintenance.schedule(tasks.redis, action, 300, reason="Update")

    response = client.get("/api/server-info")

    notice = response.json()["maintenance"]
    assert (notice["action"], notice["message"], notice["reason"]) == (action, text, "Update")
    assert response.headers["X-Server-Maintenance"] == action
    assert response.headers["X-Server-Maintenance-At"] == str(notice["at"])
    assert client.get("/health").headers["X-Server-Maintenance-At"] == str(notice["at"])


def test_job_status_shows_the_notice(api):
    client, _, _ = api
    job_id = submit(client).json()["job_id"]
    maintenance.schedule(tasks.redis, "shutdown", 120, back_in_seconds=3600)

    notice = client.get(f"/api/transcriptions/{job_id}", headers=auth()).json()["maintenance"]

    assert notice["action"] == "shutdown"
    assert notice["seconds_remaining"] in (119, 120)
    assert notice["back_at"] is not None


def test_short_job_is_accepted_before_a_distant_restart(api):
    client, _, _ = api
    maintenance.schedule(tasks.redis, "restart", 3600)

    assert submit(client).status_code == 202


def test_job_that_would_not_finish_is_refused_without_charge(api):
    client, _, billing = api
    maintenance.schedule(tasks.redis, "restart", 90, grace_seconds=60)
    # Two slots busy: a new job waits one average job (60 s), then runs 60 s.
    tasks.redis.sadd(routing.ACTIVE_KEYS["local"], "a", "b")

    response = submit(client)

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["message"].endswith("new transcriptions are paused until it is back")
    assert detail["maintenance"]["seconds_remaining"] in (89, 90)
    assert 90 + 60 <= int(response.headers["Retry-After"]) <= 90 + 60 + maintenance.RESTART_SECONDS
    assert billing.get_balance("user") == 10
    assert billing.reserved_job_ids() == []


def test_refusal_before_a_shutdown_without_return_time_has_no_retry_after(api):
    client, _, _ = api
    maintenance.schedule(tasks.redis, "shutdown", 30)

    response = submit(client)

    assert response.status_code == 503
    assert response.json()["detail"]["message"].startswith("Server shutting down in")
    assert "Retry-After" not in response.headers


def test_refusal_before_a_shutdown_with_return_time(api):
    client, _, _ = api
    maintenance.schedule(tasks.redis, "shutdown", 30, back_in_seconds=7200)

    response = submit(client)

    assert int(response.headers["Retry-After"]) in (7199, 7200)


def test_every_job_is_refused_in_the_final_minute(api):
    client, _, _ = api
    maintenance.schedule(tasks.redis, "restart", 50)

    assert submit(client).status_code == 503


def test_upload_is_refused_before_the_body_is_read(api):
    client, settings, _ = api
    maintenance.schedule(tasks.redis, "restart", 30)

    response = client.post("/api/transcriptions/upload?filename=a.mp3", headers=auth(), content=b"x" * 100)

    assert response.status_code == 503
    assert not (Path(settings.service.data_dir) / "jobs").exists()


def test_startup_clears_a_past_notice(api, monkeypatch):
    monkeypatch.setattr(app_main, "ensure_valid", lambda role: None)
    maintenance.schedule(tasks.redis, "shutdown", 0)

    app_main.sync_billing_catalog()

    assert maintenance.current(tasks.redis) is None


def test_startup_keeps_a_pending_notice(api, monkeypatch):
    monkeypatch.setattr(app_main, "ensure_valid", lambda role: None)
    maintenance.schedule(tasks.redis, "restart", 300)

    app_main.sync_billing_catalog()

    assert maintenance.current(tasks.redis) is not None


def test_starting_server_sends_retry_after(api_context):
    client, _, _ = api_context
    from app.api.routes import transcriptions

    transcriptions.transcription_submissions_enabled = False
    response = client.post("/api/transcriptions", json={"source_url": "https://example.com/video"})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "10"


def test_headers_survive_redis_outage(api, monkeypatch):
    client, _, _ = api

    def down(redis):
        raise ConnectionError("redis down")

    monkeypatch.setattr(maintenance, "current", down)

    assert client.get("/health").status_code == 200
