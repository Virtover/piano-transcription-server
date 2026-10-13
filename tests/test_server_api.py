import subprocess

import pytest
from fastapi import HTTPException

from app import google_play, main
from app.api.routes import billing as billing_routes


def test_health(api_context):
    client, _, _ = api_context

    assert client.get("/health").json() == {"status": "ok"}


def test_ready_reports_redis_state(api_context, monkeypatch):
    client, _, _ = api_context

    class FakeRedis:
        def __init__(self, healthy):
            self.healthy = healthy

        def ping(self):
            if not self.healthy:
                raise ConnectionError("down")

    monkeypatch.setattr(main.Redis, "from_url", lambda url: FakeRedis(True))
    assert client.get("/ready").json() == {"status": "ready"}

    monkeypatch.setattr(main.Redis, "from_url", lambda url: FakeRedis(False))
    assert client.get("/ready").status_code == 503


def test_server_info_lists_only_synced_offers(api_context):
    client, settings, _ = api_context
    settings.billing_provider = "google_play"
    settings.billing_products = {"starter": 10, "pro": 60, "retired": 5}
    settings.free_minutes = 3
    settings.free_minutes_period = "7d"
    google_play.synced_products = {"starter", "pro", "unknown"}

    info = client.get("/api/server-info").json()

    assert info["offers"] == [
        {"product_id": "starter", "transcription_minutes": 10},
        {"product_id": "pro", "transcription_minutes": 60},
    ]
    assert info["free_minutes"] == 3
    assert info["free_minutes_period"] == "7d"
    assert info["max_video_length_minutes"] == 20
    assert client.get("/api/billing/server-info").json() == info


def test_server_info_without_billing_lists_configured_offers(api_context):
    client, settings, _ = api_context
    settings.billing_products = {"starter": 10}

    info = client.get("/api/server-info").json()

    assert info["billing_provider"] == "none"
    assert info["offers"] == [{"product_id": "starter", "transcription_minutes": 10}]
    assert info.get("free_minutes") is None
    assert "free_minutes" not in client.get("/api/billing/server-info").json()


@pytest.fixture
def yt_dlp(api_context, monkeypatch):
    output = {"stdout": '{"duration": 90}'}

    def fake_run(command, **kwargs):
        if isinstance(output["stdout"], Exception):
            raise output["stdout"]
        assert command[0] == "yt-dlp"
        assert command[-2:] == ["--", "https://youtube.com/watch?v=1"]
        return subprocess.CompletedProcess(command, 0, stdout=output["stdout"])

    monkeypatch.setattr(billing_routes, "ensure_safe_source_url", lambda url: url)
    monkeypatch.setattr(billing_routes.subprocess, "run", fake_run)
    return output


def duration_error(url="https://youtube.com/watch?v=1"):
    with pytest.raises(HTTPException) as error:
        billing_routes.video_duration(url)
    return error.value.status_code


def test_video_duration_reads_metadata(yt_dlp):
    assert billing_routes.video_duration("https://youtube.com/watch?v=1") == 90.0


@pytest.mark.parametrize(
    "stdout",
    ["not json", '{"duration": null}', '{"duration": 0}', subprocess.TimeoutExpired("yt-dlp", 60)],
)
def test_video_duration_rejects_unreadable_metadata(yt_dlp, stdout):
    yt_dlp["stdout"] = stdout

    assert duration_error() == 422


def test_video_duration_enforces_maximum_length(yt_dlp, api_context):
    _, settings, _ = api_context
    yt_dlp["stdout"] = '{"duration": 1201}'

    assert duration_error() == 413
    settings.max_video_length_minutes = None
    assert billing_routes.video_duration("https://youtube.com/watch?v=1") == 1201.0


def test_unsafe_urls_are_rejected_before_yt_dlp(api_context, monkeypatch):
    client, _, _ = api_context
    monkeypatch.setattr(billing_routes.subprocess, "run", lambda *args, **kwargs: pytest.fail("yt-dlp ran"))

    assert duration_error("http://localhost/video") == 422
    response = client.get("/api/billing/cost", params={"source_url": "http://127.0.0.1/video"})
    assert response.status_code == 422
