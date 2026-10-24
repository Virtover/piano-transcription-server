from pathlib import Path

import pytest

from app import controls
from app.api.routes import billing as billing_routes
from app.api.routes import transcriptions
from app.worker import tasks


def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def billed(api_context, monkeypatch):
    client, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 30}
    for user in ("user", "other"):
        billing.credit_purchase(user, "starter", f"token-{user}", f"order-{user}", f"{user}@example.com")
    monkeypatch.setattr(transcriptions, "probe_audio", lambda path, measure=True, max_seconds=None: 130.0)
    monkeypatch.setattr(billing_routes, "check_video", transcriptions.check_video)
    return client, settings, billing


def submit_link(client, token="user"):
    return client.post("/api/transcriptions", headers=auth(token), json={"source_url": "https://example.com/video"})


def upload(client, token="user"):
    return client.post(
        "/api/transcriptions/upload",
        params={"filename": "song.mp3"},
        content=b"audio",
        headers={"Content-Type": "application/octet-stream", **auth(token)},
    )


def cost_of_link(client, token="user"):
    return client.get("/api/billing/cost", params={"source_url": "https://example.com/video"}, headers=auth(token))


def test_nothing_is_refused_without_controls(billed):
    client, _, _ = billed

    assert submit_link(client).status_code == 202
    assert upload(client).status_code == 202


def test_pause_refuses_new_submissions_with_the_message(billed):
    client, _, _ = billed
    controls.pause(tasks.redis, "Back after the upgrade")

    for response in (submit_link(client), upload(client)):
        assert response.status_code == 503
        assert response.json()["detail"] == "Back after the upgrade"
        assert "retry-after" not in response.headers
    # Asking a cost is not a submission.
    assert cost_of_link(client).status_code == 200


def test_pause_has_a_default_message_and_retry_after_when_it_expires(billed):
    client, _, _ = billed
    controls.pause(tasks.redis, seconds=600)

    response = submit_link(client)

    assert response.json()["detail"] == controls.DEFAULT_PAUSE_MESSAGE
    assert 590 <= int(response.headers["retry-after"]) <= 600


def test_paused_upload_is_refused_before_the_body_is_stored(billed):
    client, settings, _ = billed
    controls.pause(tasks.redis)

    upload(client)

    jobs = Path(settings.service.data_dir) / "jobs"
    assert not jobs.exists() or not any(jobs.iterdir())


def test_resume_accepts_submissions_again(billed):
    client, _, _ = billed
    controls.pause(tasks.redis)

    assert controls.resume(tasks.redis) is True
    assert submit_link(client).status_code == 202
    assert controls.resume(tasks.redis) is False


def test_links_off_refuses_links_and_their_cost_but_not_uploads(billed):
    client, _, _ = billed
    controls.turn_links_off(tasks.redis)

    response = submit_link(client)
    assert response.status_code == 503
    assert response.json()["detail"] == controls.DEFAULT_LINKS_OFF_MESSAGE
    assert cost_of_link(client).status_code == 503
    assert upload(client).status_code == 202
    assert client.get("/api/billing/cost", params={"duration_seconds": 60}, headers=auth("user")).status_code == 200

    controls.turn_links_on(tasks.redis)
    assert submit_link(client).status_code == 202


def test_blocked_user_is_refused_and_others_are_not(billed):
    client, _, _ = billed
    controls.block(tasks.redis, "user", "spam")

    for response in (submit_link(client), upload(client), cost_of_link(client)):
        assert response.status_code == 403
        # The reason is for administrators only.
        assert response.json()["detail"] == controls.BLOCKED_MESSAGE
    assert submit_link(client, "other").status_code == 202
    # Reading status and balance still works.
    assert client.get("/api/billing/balance", headers=auth("user")).status_code == 200

    controls.unblock(tasks.redis, "user")
    assert submit_link(client).status_code == 202


def test_block_expires_on_its_own(billed):
    controls.block(tasks.redis, "user", seconds=120)

    control = controls.blocked(tasks.redis, "user")
    assert 110 <= control.expires_in <= 120
    assert controls.blocked_users(tasks.redis) == {"user": control}


def test_per_user_limit_override_applies_at_once(billed):
    client, settings, _ = billed
    settings.limits.max_active_jobs_per_user = 5
    controls.set_per_user_limit(tasks.redis, 1)

    assert submit_link(client).status_code == 202
    refused = submit_link(client)
    assert refused.status_code == 429
    assert "at most 1 transcriptions" in refused.json()["detail"]

    controls.reset_per_user_limit(tasks.redis)
    assert submit_link(client).status_code == 202


def test_per_user_limit_falls_back_to_the_setting(billed):
    _, settings, _ = billed
    settings.limits.max_active_jobs_per_user = 3

    assert controls.per_user_job_limit(tasks.redis) == 3
    controls.set_per_user_limit(tasks.redis, 7, seconds=60)
    assert controls.per_user_job_limit(tasks.redis) == 7
    tasks.redis.set(controls.PER_USER_LIMIT_KEY, "garbage")
    assert controls.per_user_job_limit(tasks.redis) == 3


def test_controls_never_touch_running_jobs(billed):
    client, _, _ = billed
    job_id = submit_link(client).json()["job_id"]

    controls.pause(tasks.redis)
    controls.block(tasks.redis, "user")

    response = client.get(f"/api/transcriptions/{job_id}", headers=auth("user"))
    assert response.json()["status"] == "queued"
    assert client.delete(f"/api/transcriptions/{job_id}", headers=auth("user")).json()["status"] == "cancelled"
