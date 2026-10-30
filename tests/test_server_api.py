import json
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import google_play, main, video_info
from app.api.routes import billing as billing_routes
from app.slots import LeasedSlot, in_use


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


def test_ready_reports_billing_database_state(api_context, monkeypatch, tmp_path):
    client, settings, _ = api_context
    monkeypatch.setattr(main.Redis, "from_url", lambda url: type("Up", (), {"ping": lambda self: True})())
    assert client.get("/ready").json() == {"status": "ready"}

    # A directory cannot be opened as a database.
    settings.database.path = str(tmp_path)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "The billing database is unavailable"}


def test_server_info_lists_only_synced_offers(api_context):
    client, settings, _ = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 10, "pro": 60, "retired": 5}
    settings.billing.free_minutes = 3
    settings.billing.free_minutes_period = "7d"
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
    settings.billing.products = {"starter": 10}

    info = client.get("/api/server-info").json()

    assert info["billing_provider"] == "none"
    assert info["offers"] == [{"product_id": "starter", "transcription_minutes": 10}]
    assert info.get("free_minutes") is None
    assert "free_minutes" not in client.get("/api/billing/server-info").json()


@pytest.fixture
def yt_dlp(api_context, monkeypatch):
    output = {"stdout": '{"duration": 90}', "commands": []}

    def fake_run(command, **kwargs):
        output["commands"].append(command)
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


@pytest.mark.parametrize("stdout", ["not json", '{"duration": null}', '{"duration": 0}'])
def test_video_duration_rejects_unreadable_metadata(yt_dlp, stdout):
    yt_dlp["stdout"] = stdout

    assert duration_error() == 422


def test_video_check_that_times_out_can_be_retried(yt_dlp):
    yt_dlp["stdout"] = subprocess.TimeoutExpired("yt-dlp", 60)

    with pytest.raises(HTTPException) as error:
        billing_routes.video_duration("https://youtube.com/watch?v=1")

    assert error.value.status_code == 503
    assert error.value.detail["error_code"] == "source_temporarily_unavailable"
    assert error.value.headers["Retry-After"] == "30"


def test_video_duration_enforces_maximum_length(yt_dlp, api_context):
    _, settings, _ = api_context
    yt_dlp["stdout"] = '{"duration": 1201}'

    assert duration_error() == 413
    settings.limits.max_video_length_minutes = None
    assert billing_routes.video_duration("https://youtube.com/watch?v=1") == 1201.0


def test_video_check_cannot_reach_other_sites_through_yt_dlp(yt_dlp):
    billing_routes.video_duration("https://youtube.com/watch?v=1")

    command = yt_dlp["commands"][0]
    # The generic extractor would fetch any URL a site's page links to.
    assert command[command.index("--use-extractors") + 1] == "default,-generic"
    assert "--ignore-config" in command
    assert "--no-playlist" in command


def test_video_checks_at_once_are_limited_across_replicas(yt_dlp, monkeypatch, default_settings):
    default_settings.limits.max_video_checks = 1
    monkeypatch.setattr(billing_routes, "METADATA_WAIT", 0.01)
    # Another replica's check holds the only place.
    other = LeasedSlot(billing_routes.redis, billing_routes.METADATA_SLOTS_KEY, 1)
    assert other.try_take()

    with pytest.raises(HTTPException) as error:
        billing_routes.video_duration("https://youtube.com/watch?v=1")

    assert error.value.status_code == 503
    assert error.value.headers["Retry-After"] == "5"
    assert yt_dlp["commands"] == []
    other.release()
    assert billing_routes.video_duration("https://youtube.com/watch?v=1") == 90.0


@pytest.mark.parametrize("stdout", ['{"duration": 90}', "not json", subprocess.TimeoutExpired("yt-dlp", 60)])
def test_video_check_gives_its_slot_back(yt_dlp, stdout):
    yt_dlp["stdout"] = stdout

    try:
        billing_routes.video_duration("https://youtube.com/watch?v=1")
    except HTTPException:
        pass

    assert in_use(billing_routes.redis, billing_routes.METADATA_SLOTS_KEY) == 0


def test_cost_needs_sign_in_when_billing(api_context, monkeypatch):
    client, settings, _ = api_context
    settings.billing.provider = "google_play"
    monkeypatch.setattr(billing_routes, "video_duration", lambda url: pytest.fail("yt-dlp ran"))

    response = client.get("/api/billing/cost", params={"source_url": "https://youtube.com/watch?v=1"})

    assert response.status_code == 401
    signed_in = client.get(
        "/api/billing/cost", params={"duration_seconds": 300}, headers={"Authorization": "Bearer user"}
    )
    assert signed_in.json()["cost_minutes"] == 5


def test_unsafe_urls_are_rejected_before_yt_dlp(api_context, monkeypatch):
    client, _, _ = api_context
    monkeypatch.setattr(billing_routes.subprocess, "run", lambda *args, **kwargs: pytest.fail("yt-dlp ran"))

    assert duration_error("http://localhost/video") == 422
    response = client.get("/api/billing/cost", params={"source_url": "http://127.0.0.1/video"})
    assert response.status_code == 422


def test_video_check_is_reused_for_the_same_url(yt_dlp):
    yt_dlp["stdout"] = '{"duration": 90, "title": "Nocturne", "automatic_captions": {"en": []}}'

    first = billing_routes.check_video("https://youtube.com/watch?v=1")
    second = billing_routes.check_video("https://youtube.com/watch?v=1")

    assert len(yt_dlp["commands"]) == 1
    assert second.info == first.info == {"duration": 90, "title": "Nocturne"}
    assert second.checked_at == first.checked_at
    [key] = billing_routes.redis.scan_iter(billing_routes.VIDEO_CHECK_KEY + "*")
    assert 0 < billing_routes.redis.ttl(key) <= billing_routes.VIDEO_CHECK_TTL


def test_reused_video_check_still_enforces_the_length_limit(yt_dlp, api_context):
    _, settings, _ = api_context
    yt_dlp["stdout"] = '{"duration": 1201}'
    settings.limits.max_video_length_minutes = None
    billing_routes.video_duration("https://youtube.com/watch?v=1")

    settings.limits.max_video_length_minutes = 20

    assert duration_error() == 413
    assert len(yt_dlp["commands"]) == 1


@pytest.mark.parametrize("stdout", ["not json", '{"duration": 0}'])
def test_failed_video_check_is_not_reused(yt_dlp, stdout):
    yt_dlp["stdout"] = stdout
    assert duration_error() == 422

    yt_dlp["stdout"] = '{"duration": 90}'

    assert billing_routes.video_duration("https://youtube.com/watch?v=1") == 90.0


@pytest.fixture
def real_check(yt_dlp, api_context, monkeypatch):
    """Submissions run the real video check, with yt-dlp faked."""
    from app.api.routes import transcriptions

    monkeypatch.setattr(transcriptions, "check_video", billing_routes.check_video)
    yt_dlp["stdout"] = json.dumps({"duration": 90, "title": "Nocturne", "channel": "Chopin", "formats": []})
    return yt_dlp


def submit(client):
    return client.post("/api/transcriptions", json={"source_url": "https://youtube.com/watch?v=1"})


def test_cost_then_submit_asks_the_site_once(real_check, api_context):
    client, _, _ = api_context

    assert client.get("/api/billing/cost", params={"source_url": "https://youtube.com/watch?v=1"}).status_code == 200
    assert submit(client).status_code == 202

    assert len(real_check["commands"]) == 1


def test_submit_saves_the_video_info_for_the_worker(real_check, api_context):
    client, settings, _ = api_context

    job_id = submit(client).json()["job_id"]

    info_path = Path(settings.service.data_dir) / "jobs" / job_id / video_info.VIDEO_INFO_FILE_NAME
    assert json.loads(info_path.read_text())["title"] == "Nocturne"
    assert video_info.saved_video_info(info_path.parent) == info_path


def test_title_is_shown_while_the_job_waits(real_check, api_context):
    client, _, _ = api_context

    job_id = submit(client).json()["job_id"]
    status = client.get(f"/api/transcriptions/{job_id}").json()

    assert status["status"] == "queued"
    assert status["title"] == "Nocturne"
    assert status["metadata"]["author"] == "Chopin"
    assert status["metadata"]["duration"] == 90


def test_refused_submit_removes_the_saved_info(real_check, api_context):
    client, settings, _ = api_context
    settings.billing.provider = "google_play"
    settings.billing.free_minutes = 0

    response = client.post(
        "/api/transcriptions",
        json={"source_url": "https://youtube.com/watch?v=1"},
        headers={"Authorization": "Bearer poor"},
    )

    assert response.status_code == 402
    jobs = Path(settings.service.data_dir) / "jobs"
    assert not jobs.exists() or list(jobs.iterdir()) == []
