import json
import subprocess
import sys
import time
import types

import pytest
from fastapi import HTTPException

from app import video_info, video_sites
from app.admin.__main__ import main
from app.api.routes import billing as billing_routes
from app.errors import (
    SourceBusy,
    SourceError,
    SourceTemporarilyUnavailable,
    SourceUnsupported,
    SourceVideoUnavailable,
    detail,
    error_code,
)
from app.transcription import audio
from app.worker import tasks

RATE_LIMITED = "ERROR: [youtube] abc: Video unavailable. This content isn't available, try again later."
BOT_CHECK = "ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies-from-browser"
PRIVATE = "ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video"
URL = "https://youtube.com/watch?v=1"


def failed(stderr):
    return subprocess.CalledProcessError(1, ["yt-dlp", "--", URL], stderr=stderr)


# --- What yt-dlp's failures mean ---------------------------------------------

@pytest.mark.parametrize(
    "stderr",
    [
        RATE_LIMITED,
        BOT_CHECK,
        "ERROR: unable to download video data: HTTP Error 429: Too Many Requests",
        "ERROR: unable to download video data: HTTP Error 402: Payment Required",
    ],
)
def test_a_site_limiting_the_server_opens_the_breaker(stderr):
    error = video_sites.failure(failed(stderr))

    assert isinstance(error, SourceTemporarilyUnavailable)
    assert error.status == 503
    assert error.retry_after == 30 * 60
    assert video_sites.state()["refused"]["expires_in"] == 30 * 60


@pytest.mark.parametrize(
    ("stderr", "kind"),
    [
        (PRIVATE, SourceVideoUnavailable),
        ("ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader", SourceVideoUnavailable),
        ("ERROR: Unsupported URL: https://youtube.com/feed", SourceUnsupported),
        ("ERROR: [youtube] abc: Requested format is not available", SourceError),
    ],
)
def test_other_failures_leave_the_breaker_closed(stderr, kind):
    error = video_sites.failure(failed(stderr))

    assert type(error) is kind
    assert video_sites.refusal() is None


def test_a_timeout_is_temporary_but_not_a_refusal():
    error = video_sites.failure(subprocess.TimeoutExpired("yt-dlp", 60))

    assert error.code == "source_temporarily_unavailable"
    assert video_sites.refusal() is None


def test_error_codes_and_detail():
    error = SourceVideoUnavailable("The video could not be downloaded: Private video")
    error.__cause__ = failed(PRIVATE)

    assert error_code(error) == "source_video_unavailable"
    assert error_code(failed(PRIVATE)) == "source_failed"
    assert error_code(RuntimeError("model")) == "transcription_failed"
    assert "Caused by CalledProcessError" in detail(error)
    assert "Private video. Sign in" in detail(error)


# --- The breaker ---------------------------------------------------------------

def expire_cooldown():
    video_sites.redis.delete(video_sites.BLOCKED_KEY)


def test_one_incident_counts_once():
    assert video_sites.open_breaker("429") == 30 * 60
    assert video_sites.open_breaker("429 again") <= 30 * 60

    state = video_sites.state()
    assert state["incidents_in_a_row"] == 1
    assert state["incidents"] == 1
    assert state["refused"]["reason"] == "429"


def test_incidents_in_a_row_double_the_cooldown_up_to_the_maximum(default_settings):
    default_settings.limits.video_site_max_cooldown = 3 * 60 * 60
    cooldowns = []
    for _ in range(5):
        cooldowns.append(video_sites.open_breaker("429"))
        expire_cooldown()

    assert cooldowns == [30 * 60, 60 * 60, 2 * 60 * 60, 3 * 60 * 60, 3 * 60 * 60]


def test_no_request_reaches_the_site_while_refused():
    video_sites.open_breaker("429")

    with pytest.raises(SourceTemporarilyUnavailable) as error:
        with video_sites.access(wait=60):
            pytest.fail("the site was contacted")

    assert 0 < error.value.retry_after <= 30 * 60


def test_after_the_cooldown_one_request_at_a_time_until_one_succeeds():
    video_sites.open_breaker("429")
    expire_cooldown()
    assert video_sites.state()["recovering"]

    with video_sites.access(wait=0):
        # A second request waits for the first; with no time to wait, it is refused.
        with pytest.raises(SourceBusy):
            with video_sites.access(wait=0):
                pytest.fail("two requests while recovering")

    assert not video_sites.state()["recovering"]
    with video_sites.access(wait=0), video_sites.access(wait=0):
        pass


def test_a_failed_recovery_request_keeps_recovering():
    video_sites.open_breaker("429")
    expire_cooldown()

    with pytest.raises(SourceVideoUnavailable):
        with video_sites.access(wait=0):
            raise SourceVideoUnavailable("private")

    assert video_sites.state()["recovering"]
    assert not video_sites.redis.exists(video_sites.PROBE_KEY)


def test_a_refused_recovery_request_starts_a_longer_cooldown():
    video_sites.open_breaker("429")
    expire_cooldown()

    with pytest.raises(SourceTemporarilyUnavailable):
        with video_sites.access(wait=0):
            raise video_sites.failure(failed(RATE_LIMITED))

    assert video_sites.state()["refused"]["expires_in"] == 60 * 60


def test_requests_are_spaced_across_processes(default_settings, monkeypatch):
    default_settings.limits.video_site_interval = 3
    sleeps = []
    monkeypatch.setattr(video_sites.time, "sleep", sleeps.append)

    with video_sites.access(wait=10):
        pass
    with video_sites.access(wait=10):
        pass
    with pytest.raises(SourceBusy) as error:
        with video_sites.access(wait=1):
            pytest.fail("started too soon")

    assert sleeps[0] == 0
    assert 2.5 < sleeps[1] <= 3
    assert error.value.retry_after >= 5
    # Downloads from saved info are not spaced.
    with video_sites.access(wait=0, paced=False):
        pass


def test_reset_closes_the_breaker_and_forgets_the_incidents():
    video_sites.open_breaker("429")

    assert video_sites.reset()
    assert video_sites.refusal() is None
    assert video_sites.state()["incidents_in_a_row"] == 0
    assert video_sites.state()["incidents"] == 1
    assert not video_sites.reset()


# --- Worker downloads ----------------------------------------------------------

@pytest.fixture
def processes(monkeypatch):
    """Fake yt-dlp and ffmpeg. yt-dlp fails with errors[source] if set."""
    commands = []
    errors = {}

    def run_process(command, cancellation_callback=None):
        commands.append(command)
        if command[0] == "yt-dlp":
            output_dir = audio.Path(command[command.index("-o") + 1]).parent
            if command[-2] in errors:
                raise subprocess.CalledProcessError(1, command, stderr=errors[command[-2]])
            (output_dir / "download.webm").write_bytes(b"audio")
            return subprocess.CompletedProcess(command, 0, json.dumps({"title": "Nocturne", "duration": 130}), "")
        audio.Path(command[-1]).write_bytes(b"RIFF")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(audio, "run_process", run_process)
    monkeypatch.setattr(audio, "ensure_safe_source_url", lambda url: url)
    monkeypatch.setattr(audio, "ffmpeg_thread_count", lambda: None)
    return commands, errors


def yt_dlp_runs(commands):
    return [command[-2] for command in commands if command[0] == "yt-dlp"]


@pytest.mark.parametrize(
    ("stderr", "kind"),
    [(RATE_LIMITED, SourceTemporarilyUnavailable), (BOT_CHECK, SourceTemporarilyUnavailable), (PRIVATE, SourceVideoUnavailable)],
)
def test_saved_info_download_is_not_retried_when_the_site_said_no(processes, tmp_path, stderr, kind):
    commands, errors = processes
    errors["--load-info-json"] = stderr
    video_info.save_video_info(tmp_path, {"title": "Nocturne"}, checked_at=time.time())

    with pytest.raises(kind):
        audio.download_audio(URL, tmp_path)

    assert yt_dlp_runs(commands) == ["--load-info-json"]


def test_refused_link_is_not_downloaded_at_all(processes, tmp_path):
    commands, _ = processes
    video_sites.open_breaker("429")
    video_info.save_video_info(tmp_path, {"title": "Nocturne"}, checked_at=time.time())

    with pytest.raises(SourceTemporarilyUnavailable):
        audio.download_audio(URL, tmp_path)

    assert commands == []


def test_downloads_retry_little_and_back_off(processes, tmp_path):
    commands, _ = processes
    audio.download_audio(URL, tmp_path)

    [command] = [command for command in commands if command[0] == "yt-dlp"]
    assert command[command.index("--extractor-retries") + 1] == "1"
    assert command[command.index("--retries") + 1] == "2"
    assert "http:exp=2:30" in command


def test_refused_job_fails_with_a_code_and_is_refunded(api_context, monkeypatch):
    client, settings, billing = api_context
    settings.billing.provider = "google_play"
    settings.billing.products = {"starter": 10}
    billing.credit_purchase("user", "starter", "token", "order")
    billing.reserve("user", "job", 4)
    tasks.redis.hset(
        tasks.job_key("job"),
        mapping={"status": "queued", "progress": "0.0", "user_id": "user", "full_cost": "4"},
    )
    module = types.ModuleType("app.transcription.pipeline")

    def transcribe_source(**kwargs):
        raise video_sites.failure(failed(RATE_LIMITED))

    module.transcribe_source = transcribe_source
    monkeypatch.setitem(sys.modules, "app.transcription.pipeline", module)

    tasks.transcribe_job.fn("job", URL)

    job = tasks.redis.hgetall(tasks.job_key("job"))
    assert job["status"] == "failed"
    assert job["error_code"] == "source_temporarily_unavailable"
    assert job["error"] == video_sites.REFUSED_MESSAGE
    assert billing.get_balance("user") == 10
    status = client.get("/api/transcriptions/job", headers={"Authorization": "Bearer user"}).json()
    assert status["error_code"] == "source_temporarily_unavailable"


# --- API -----------------------------------------------------------------------

@pytest.fixture
def yt_dlp(api_context, monkeypatch):
    output = {"stdout": '{"duration": 90, "title": "Nocturne"}', "stderr": None, "commands": []}

    def fake_run(command, **kwargs):
        output["commands"].append(command)
        if output["stderr"] is not None:
            raise subprocess.CalledProcessError(1, command, stderr=output["stderr"])
        return subprocess.CompletedProcess(command, 0, stdout=output["stdout"])

    monkeypatch.setattr(billing_routes, "ensure_safe_source_url", lambda url: url)
    monkeypatch.setattr(billing_routes.subprocess, "run", fake_run)
    return output


def refusal(url=URL):
    with pytest.raises(HTTPException) as error:
        billing_routes.check_video(url)
    return error.value


def test_rate_limited_check_is_refused_with_retry_after(yt_dlp):
    yt_dlp["stderr"] = RATE_LIMITED

    error = refusal()

    assert error.status_code == 503
    assert error.detail == {"message": video_sites.REFUSED_MESSAGE, "error_code": "source_temporarily_unavailable"}
    assert error.headers["Retry-After"] == str(30 * 60)
    # Another link is refused without asking the site.
    yt_dlp["stderr"] = None
    assert refusal("https://youtube.com/watch?v=2").status_code == 503
    assert len(yt_dlp["commands"]) == 1


def test_unavailable_video_is_remembered_briefly(yt_dlp):
    yt_dlp["stderr"] = PRIVATE

    first, second = refusal(), refusal()

    assert first.status_code == second.status_code == 422
    assert second.detail["error_code"] == "source_video_unavailable"
    assert second.detail == first.detail
    assert len(yt_dlp["commands"]) == 1
    [key] = billing_routes.redis.scan_iter(billing_routes.VIDEO_CHECK_KEY + "*")
    assert 0 < billing_routes.redis.ttl(key) <= billing_routes.FAILED_CHECK_TTL


def test_a_url_being_checked_is_not_checked_twice(yt_dlp, monkeypatch):
    monkeypatch.setattr(billing_routes, "METADATA_WAIT", 0.01)
    billing_routes.check_video(URL)
    [key] = billing_routes.redis.scan_iter(billing_routes.VIDEO_CHECK_KEY + "*")
    billing_routes.redis.delete(key)
    billing_routes.redis.set(key + billing_routes.CHECKING_SUFFIX, "1")

    error = refusal()

    assert error.status_code == 503
    assert error.detail["error_code"] == "source_busy"
    assert len(yt_dlp["commands"]) == 1


def test_waiting_check_uses_the_result_of_the_running_one(yt_dlp, monkeypatch):
    [key] = [billing_routes.VIDEO_CHECK_KEY + billing_routes.hashlib.sha256(URL.encode()).hexdigest()]
    billing_routes.redis.set(key + billing_routes.CHECKING_SUFFIX, "1")
    checked = {"info": {"duration": 90, "title": "Nocturne"}, "checked_at": 1.0}
    # The running check finishes while this one waits.
    monkeypatch.setattr(billing_routes.time, "sleep", lambda seconds: billing_routes.redis.set(key, json.dumps(checked)))

    video = billing_routes.check_video(URL)

    assert video.info["title"] == "Nocturne"
    assert yt_dlp["commands"] == []


def test_links_are_refused_but_uploads_accepted_while_refused(api_context, monkeypatch):
    client, _, _ = api_context
    from app.api.routes import transcriptions

    monkeypatch.setattr(transcriptions, "probe_audio", lambda path, measure=True, max_seconds=None: 130.0)
    monkeypatch.setattr(transcriptions, "check_video", lambda url: pytest.fail("the link was checked"))
    video_sites.open_breaker("429")

    link = client.post("/api/transcriptions", json={"source_url": URL})
    cost = client.get("/api/billing/cost", params={"source_url": URL})
    upload = client.post(
        "/api/transcriptions/upload",
        params={"filename": "song.mp3"},
        content=b"audio",
        headers={"Content-Type": "application/octet-stream"},
    )

    assert link.status_code == cost.status_code == 503
    assert link.json()["detail"]["error_code"] == "source_temporarily_unavailable"
    assert 0 < int(link.headers["Retry-After"]) <= 30 * 60
    assert upload.status_code == 202
    # No place was held for the refused link.
    statuses = [tasks.redis.hget(key, "status") for key in tasks.redis.scan_iter("transcription:*")]
    assert statuses == ["queued"]


# --- Administration ------------------------------------------------------------

def test_status_shows_and_limits_reset_ends_a_refusal(api_context, capsys):
    video_sites.open_breaker("HTTP Error 429: Too Many Requests")

    main(["status"])
    output = capsys.readouterr().out
    assert "LINKS REFUSED for 30 m" in output
    assert "HTTP Error 429" in output

    main(["limits", "sites", "reset", "--by", "admin"])
    assert "accepted again" in capsys.readouterr().out
    assert video_sites.refusal() is None
    main(["limits"])
    assert "Video sites:     answering; incidents so far: 1" in capsys.readouterr().out
