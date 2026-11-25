"""Links sent faster than they can be checked, and clients whose links keep
failing: refused before yt-dlp runs, gently enough for a user who taps
Transcribe again and again."""

import subprocess
import time

import pytest
from fastapi import HTTPException

from app import job_limits, video_check
from app.api.routes import billing as billing_routes
from app.api.routes import transcriptions


GOOD = "https://youtube.com/watch?v=good"


@pytest.fixture
def links(api_context, monkeypatch, default_settings):
    """yt-dlp faked per link: a good one has a length, any other is unreadable."""
    ran = []

    def fake_run(command, **kwargs):
        url = command[-1]
        ran.append(url)
        stdout = '{"duration": 90}' if url == GOOD else "not json"
        return subprocess.CompletedProcess(command, 0, stdout=stdout)

    monkeypatch.setattr(billing_routes, "ensure_safe_source_url", lambda url: url)
    monkeypatch.setattr(video_check.subprocess, "run", fake_run)
    default_settings.limits.max_failed_video_checks = 3
    return ran


def bad(number):
    return f"https://youtube.com/watch?v=bad{number}"


def check(url, client="user:u1", user_id=None):
    with pytest.raises(HTTPException) as error:
        billing_routes.check_video(url, user_id, client)
    return error.value


def test_different_failed_links_are_refused_before_yt_dlp(links):
    # A good link checked before is still answered from memory.
    billing_routes.check_video(GOOD, None, "user:u1")
    for number in range(3):
        assert check(bad(number)).status_code == 422

    refused = check(bad(3))

    assert refused.status_code == 429
    assert refused.detail["error_code"] == "too_many_failed_links"
    assert "upload the file" in refused.detail["message"]
    assert 290 <= int(refused.headers["Retry-After"]) <= 300
    assert bad(3) not in links
    assert billing_routes.check_video(GOOD, None, "user:u1").duration == 90.0
    # Others are not refused.
    assert check(bad(3), client="user:u2").status_code == 422


def test_the_same_link_sent_again_counts_once(links):
    # A user tapping Transcribe again and again on one link that fails.
    for _ in range(20):
        assert check(bad(0)).status_code == 422

    assert check(bad(1)).status_code == 422


def test_refusal_lasts_until_the_oldest_failure_leaves_the_window(links):
    key = billing_routes.FAILED_CHECKS_KEY + "user:u1"
    now = time.time()
    billing_routes.redis.zadd(key, {"a": now - 400, "b": now - 150, "c": now - 100, "d": now - 50})

    # "a" is out of the 5-minute window; "b" leaves it in 150 seconds.
    assert 149 <= int(check(bad(0)).headers["Retry-After"]) <= 150
    billing_routes.redis.zadd(key, {"b": now - 301})
    assert check(bad(0)).status_code == 422


def test_failures_that_are_not_the_links_fault_are_not_counted(links, monkeypatch):
    def timed_out(command, **kwargs):
        raise subprocess.TimeoutExpired("yt-dlp", 60)

    monkeypatch.setattr(video_check.subprocess, "run", timed_out)
    for number in range(2):
        assert check(bad(number)).status_code == 503
    # A busy server neither.
    monkeypatch.setattr(billing_routes, "check_threads", type("Full", (), {"acquire": lambda self, blocking: False})())
    for number in range(5):
        assert check(bad(10 + number)).detail["error_code"] == "source_busy"

    assert billing_routes.redis.zcard(billing_routes.FAILED_CHECKS_KEY + "user:u1") == 0


def test_counted_failures_expire_however_the_api_stops(links):
    check(bad(0))

    ttl = billing_routes.redis.ttl(billing_routes.FAILED_CHECKS_KEY + "user:u1")
    assert 0 < ttl <= 300


def test_unlimited_and_internal_checks_are_never_refused(links, default_settings):
    for number in range(5):
        check(bad(number), client=None)
    assert check(bad(5), client=None).status_code == 422

    default_settings.limits.max_failed_video_checks = None
    for number in range(5):
        assert check(bad(number)).status_code == 422
    assert not billing_routes.redis.exists(billing_routes.FAILED_CHECKS_KEY + "user:u1")


def test_developer_accounts_are_never_refused(links, monkeypatch):
    monkeypatch.setattr(billing_routes.billing, "is_developer", lambda user_id: user_id == "dev")
    for number in range(5):
        assert check(bad(number), client="user:dev", user_id="dev").status_code == 422


def test_a_redis_failure_while_counting_still_answers_with_the_links_error(links, monkeypatch):
    from redis.exceptions import ConnectionError

    def broken():
        raise ConnectionError("down")

    monkeypatch.setattr(billing_routes.redis, "pipeline", broken)
    # Logged, not raised: the caller then raises the link's own error.
    billing_routes.remember_failure("user:u1", "link")


def test_checks_beyond_the_threads_are_refused_at_once(links, monkeypatch):
    monkeypatch.setattr(billing_routes, "check_threads", billing_routes.threading.BoundedSemaphore(1))
    assert billing_routes.check_threads.acquire(blocking=False)

    refused = check(GOOD)

    assert refused.status_code == 503
    assert refused.headers["Retry-After"] == "5"
    assert links == []
    billing_routes.check_threads.release()
    assert billing_routes.check_video(GOOD).duration == 90.0
    # Given back after a failure too.
    check(bad(0))
    assert billing_routes.check_threads.acquire(blocking=False)


def test_submissions_without_sign_in_count_by_address(links, api_context, monkeypatch):
    client, _, _ = api_context
    monkeypatch.setattr(transcriptions, "check_video", billing_routes.check_video)
    assert client.get("/api/billing/cost", params={"source_url": GOOD}).status_code == 200
    for number in range(3):
        assert client.post("/api/transcriptions", json={"source_url": bad(number)}).status_code == 422

    refused = client.post("/api/transcriptions", json={"source_url": bad(3)})
    cost = client.get("/api/billing/cost", params={"source_url": bad(4)})

    assert refused.status_code == cost.status_code == 429
    assert refused.json()["detail"]["error_code"] == "too_many_failed_links"
    assert "Retry-After" in refused.headers
    # The refused submission gave its place back.
    assert billing_routes.redis.scard(job_limits.ALL_JOBS_KEY) == 0
    assert billing_routes.redis.exists(billing_routes.FAILED_CHECKS_KEY + "address:testclient")
    # A link checked before the refusals is still accepted.
    assert client.post("/api/transcriptions", json={"source_url": GOOD}).status_code == 202


def test_signed_in_cost_checks_count_for_the_user(links, api_context):
    client, settings, _ = api_context
    settings.billing.provider = "google_play"
    for number in range(3):
        response = client.get(
            "/api/billing/cost", params={"source_url": bad(number)}, headers={"Authorization": "Bearer user"}
        )
        assert response.status_code == 422

    [key] = billing_routes.redis.scan_iter(billing_routes.FAILED_CHECKS_KEY + "*")
    assert key.startswith(billing_routes.FAILED_CHECKS_KEY + "user:")
    response = client.get("/api/billing/cost", params={"source_url": bad(3)}, headers={"Authorization": "Bearer user"})
    assert response.status_code == 429
