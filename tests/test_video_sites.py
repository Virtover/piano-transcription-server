import json
import subprocess
import sys
import threading
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
    SourceTimedOut,
    SourceUnsupported,
    SourceVideoUnavailable,
    detail,
    error_code,
)
from app.transcription import audio
from app.transcription.cancellation import TranscriptionCancelled
from app.worker import tasks

RATE_LIMITED = "ERROR: [youtube] abc: Video unavailable. This content isn't available, try again later."
BOT_CHECK = "ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies-from-browser"
PRIVATE = "ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video"
READ_TIMEOUT = "ERROR: [youtube] abc: Unable to download API page: The read operation timed out"
URL = "https://youtube.com/watch?v=1"
YOUTUBE = "youtube.com"
VIMEO = "vimeo.com"


def failed(stderr):
    return subprocess.CalledProcessError(1, ["yt-dlp", "--", URL], stderr=stderr)


def expire_cooldown(site=YOUTUBE):
    video_sites.redis.delete(video_sites._key(site, video_sites.BLOCKED))


# --- Which site a link belongs to ----------------------------------------------

@pytest.mark.parametrize(
    ("url", "site"),
    [
        ("https://www.youtube.com/watch?v=1", YOUTUBE),
        ("https://m.youtube.com/watch?v=1", YOUTUBE),
        ("https://youtu.be/1", YOUTUBE),
        ("https://www.youtube-nocookie.com/embed/1", YOUTUBE),
        ("https://vimeo.com/1", VIMEO),
        ("https://x.com/a/status/1", "twitter.com"),
        ("https://v.redd.it/1", "reddit.com"),
    ],
)
def test_links_share_their_sites_breaker(url, site):
    assert video_sites.site_of(url) == site


def test_without_an_allowed_list_the_site_is_the_domain(default_settings):
    default_settings.limits.source_url_allowed_hosts = []

    assert video_sites.site_of("https://media.example.org/v/1") == "example.org"
    assert video_sites.site_of("https://youtu.be/1") == YOUTUBE


# --- What yt-dlp's failures mean -----------------------------------------------

@pytest.mark.parametrize(
    ("site", "stderr"),
    [
        (YOUTUBE, RATE_LIMITED),
        (YOUTUBE, BOT_CHECK),
        (YOUTUBE, "ERROR: unable to download video data: HTTP Error 402: Payment Required"),
        (YOUTUBE, "ERROR: unable to download video data: HTTP Error 429: Too Many Requests"),
        (VIMEO, "ERROR: [vimeo] 1: HTTP Error 429: Too Many Requests"),
        (VIMEO, "ERROR: [vimeo] 1: You are being rate-limited"),
    ],
)
def test_a_site_limiting_the_server_opens_its_breaker(site, stderr):
    error = video_sites.failure(site, failed(stderr))

    assert isinstance(error, SourceTemporarilyUnavailable)
    assert error.status == 503
    assert error.retry_after == 30 * 60
    assert site in str(error)
    assert video_sites.state(site)["refused"]["kind"] == "limited"


@pytest.mark.parametrize(
    ("site", "stderr", "kind"),
    [
        (YOUTUBE, PRIVATE, SourceVideoUnavailable),
        (YOUTUBE, "ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader", SourceVideoUnavailable),
        (YOUTUBE, "ERROR: [youtube] abc: This live event will begin in 3 hours. Try again later", SourceVideoUnavailable),
        (YOUTUBE, "ERROR: Unsupported URL: https://youtube.com/feed", SourceUnsupported),
        (YOUTUBE, "ERROR: [youtube] abc: Requested format is not available", SourceError),
        # Phrases YouTube uses for its rate limit mean nothing of the kind elsewhere.
        (VIMEO, "ERROR: [vimeo] 1: This video is being processed, try again later", SourceError),
        (VIMEO, "ERROR: [vimeo] 1: Video unavailable. This content isn't available, try again later.", SourceVideoUnavailable),
        (VIMEO, "ERROR: [vimeo] 1: HTTP Error 402: Payment Required", SourceError),
        (VIMEO, "ERROR: [vimeo] 1: Sign in to confirm you're not a bot", SourceError),
    ],
)
def test_other_failures_leave_the_breaker_closed(site, stderr, kind):
    error = video_sites.failure(site, failed(stderr))

    assert type(error) is kind
    assert video_sites.refusal(site) is None


def test_error_codes_and_detail():
    error = SourceVideoUnavailable("The video could not be downloaded: Private video")
    error.__cause__ = failed(PRIVATE)

    assert error_code(error) == "source_video_unavailable"
    assert error_code(SourceTimedOut("slow")) == "source_temporarily_unavailable"
    assert error_code(failed(PRIVATE)) == "source_failed"
    assert error_code(RuntimeError("model")) == "transcription_failed"
    assert "Caused by CalledProcessError" in detail(error)
    assert "Private video. Sign in" in detail(error)


# --- Timeouts ------------------------------------------------------------------

@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("yt-dlp", 60), failed(READ_TIMEOUT)])
def test_a_single_timeout_is_temporary_but_opens_nothing(error):
    result = video_sites.failure(YOUTUBE, error)

    assert isinstance(result, SourceTimedOut)
    assert result.retry_after == video_sites.TIMEOUT_RETRY_AFTER
    assert video_sites.refusal(YOUTUBE) is None


def test_repeated_timeouts_open_a_short_breaker():
    for _ in range(video_sites.TIMEOUT_LIMIT - 1):
        video_sites.failure(YOUTUBE, subprocess.TimeoutExpired("yt-dlp", 60))

    error = video_sites.failure(YOUTUBE, subprocess.TimeoutExpired("yt-dlp", 60))

    assert error.retry_after == 2 * 60
    refused = video_sites.refusal(YOUTUBE)
    assert "not answering" in str(refused)
    assert video_sites.state(YOUTUBE)["refused"]["kind"] == "timeouts"
    assert video_sites.refusal(VIMEO) is None


def test_continued_timeouts_extend_the_short_breaker_up_to_the_cooldown():
    cooldowns = []
    for _ in range(6):
        cooldowns.append(video_sites.open_breaker(YOUTUBE, video_sites.TIMED_OUT, "timeouts"))
        expire_cooldown()

    assert cooldowns == [2 * 60, 4 * 60, 8 * 60, 16 * 60, 30 * 60, 30 * 60]


def test_a_success_forgets_earlier_timeouts():
    for _ in range(video_sites.TIMEOUT_LIMIT - 1):
        video_sites.failure(YOUTUBE, subprocess.TimeoutExpired("yt-dlp", 60))
    with video_sites.access(YOUTUBE, wait=0):
        pass

    video_sites.failure(YOUTUBE, subprocess.TimeoutExpired("yt-dlp", 60))

    assert video_sites.refusal(YOUTUBE) is None


def test_a_rate_limit_replaces_a_breaker_for_timeouts():
    video_sites.open_breaker(YOUTUBE, video_sites.TIMED_OUT, "timeouts")

    assert video_sites.open_breaker(YOUTUBE, video_sites.LIMITED, "429") == 30 * 60
    assert video_sites.state(YOUTUBE)["refused"]["kind"] == "limited"
    # Not the other way round.
    assert video_sites.open_breaker(YOUTUBE, video_sites.TIMED_OUT, "timeouts") > 2 * 60
    assert video_sites.state(YOUTUBE)["refused"]["kind"] == "limited"


# --- The breaker ---------------------------------------------------------------

def test_one_incident_counts_once():
    assert video_sites.open_breaker(YOUTUBE, video_sites.LIMITED, "429") == 30 * 60
    assert video_sites.open_breaker(YOUTUBE, video_sites.LIMITED, "429 again") <= 30 * 60

    state = video_sites.state(YOUTUBE)
    assert state["incidents_in_a_row"] == 1
    assert state["incidents"] == 1
    assert state["refused"]["reason"] == "429"


def test_incidents_in_a_row_double_the_cooldown_up_to_the_maximum(default_settings):
    default_settings.limits.video_site_max_cooldown = 3 * 60 * 60
    cooldowns = []
    for _ in range(5):
        cooldowns.append(video_sites.open_breaker(YOUTUBE, video_sites.LIMITED, "429"))
        expire_cooldown()

    assert cooldowns == [30 * 60, 60 * 60, 2 * 60 * 60, 3 * 60 * 60, 3 * 60 * 60]


def test_no_request_reaches_the_site_while_refused():
    video_sites.open_breaker(YOUTUBE, video_sites.LIMITED, "429")

    with pytest.raises(SourceTemporarilyUnavailable) as error:
        with video_sites.access(YOUTUBE, wait=60):
            pytest.fail("the site was contacted")

    assert 0 < error.value.retry_after <= 30 * 60
    # Other sites are not affected.
    with video_sites.access(VIMEO, wait=0):
        pass


def test_after_the_cooldown_one_request_at_a_time_until_one_succeeds():
    video_sites.open_breaker(YOUTUBE, video_sites.LIMITED, "429")
    expire_cooldown()
    assert video_sites.state(YOUTUBE)["recovering"]

    with video_sites.access(YOUTUBE, wait=0):
        # A second request waits for the first; with no time to wait, it is refused.
        with pytest.raises(SourceBusy):
            with video_sites.access(YOUTUBE, wait=0):
                pytest.fail("two requests while recovering")

    assert not video_sites.state(YOUTUBE)["recovering"]
    with video_sites.access(YOUTUBE, wait=0), video_sites.access(YOUTUBE, wait=0):
        pass


def test_a_failed_recovery_request_keeps_recovering():
    video_sites.open_breaker(YOUTUBE, video_sites.LIMITED, "429")
    expire_cooldown()

    with pytest.raises(SourceVideoUnavailable):
        with video_sites.access(YOUTUBE, wait=0):
            raise SourceVideoUnavailable("private")

    assert video_sites.state(YOUTUBE)["recovering"]
    assert not video_sites.redis.exists(video_sites._key(YOUTUBE, video_sites.PROBE))


def test_a_refused_recovery_request_starts_a_longer_cooldown():
    video_sites.open_breaker(YOUTUBE, video_sites.LIMITED, "429")
    expire_cooldown()

    with pytest.raises(SourceTemporarilyUnavailable):
        with video_sites.access(YOUTUBE, wait=0):
            raise video_sites.failure(YOUTUBE, failed(RATE_LIMITED))

    assert video_sites.state(YOUTUBE)["refused"]["expires_in"] == 60 * 60


def test_requests_to_a_site_are_spaced_across_processes(default_settings, monkeypatch):
    default_settings.limits.video_site_interval = 3
    # A clock that sleeping moves forward.
    now = [1_000_000.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(video_sites.time, "sleep", sleep)
    monkeypatch.setattr(video_sites.time, "time", lambda: now[0])
    monkeypatch.setattr(video_sites.time, "monotonic", lambda: now[0])

    with video_sites.access(YOUTUBE, wait=10):
        pass
    with pytest.raises(SourceBusy) as error:
        with video_sites.access(YOUTUBE, wait=1):
            pytest.fail("started too soon")
    assert error.value.retry_after >= 3
    with video_sites.access(YOUTUBE, wait=10):
        pass

    # The second start waited for its turn, about 3 seconds after the first.
    assert 2.5 < sum(sleeps) <= 3
    sleeps.clear()
    # Another site has its own spacing, and downloads from saved info are not spaced.
    with video_sites.access(VIMEO, wait=0):
        pass
    with video_sites.access(YOUTUBE, wait=0, paced=False):
        pass
    assert sleeps == []


def test_waiting_for_a_turn_stops_when_the_job_is_cancelled(default_settings, monkeypatch):
    default_settings.limits.video_site_interval = 60
    monkeypatch.setattr(video_sites.time, "sleep", lambda seconds: None)
    with video_sites.access(YOUTUBE, wait=0):
        pass

    with pytest.raises(TranscriptionCancelled):
        with video_sites.access(YOUTUBE, wait=600, cancelled=lambda: True):
            pytest.fail("started after cancelling")


def test_reset_closes_one_site_or_every_site():
    video_sites.open_breaker(YOUTUBE, video_sites.LIMITED, "429")
    video_sites.open_breaker(VIMEO, video_sites.LIMITED, "429")

    assert video_sites.reset(YOUTUBE) == [YOUTUBE]
    assert video_sites.refusal(YOUTUBE) is None
    assert video_sites.refusal(VIMEO) is not None
    assert video_sites.state(YOUTUBE)["incidents_in_a_row"] == 0
    assert video_sites.state(YOUTUBE)["incidents"] == 1

    assert video_sites.reset() == [VIMEO]
    assert video_sites.reset() == []


# --- Download places -----------------------------------------------------------

def test_downloads_from_a_site_are_limited_across_processes(default_settings):
    default_settings.limits.video_site_max_downloads = 2

    with video_sites.access(YOUTUBE, wait=0, download=True), video_sites.access(YOUTUBE, wait=0, download=True):
        assert video_sites.state(YOUTUBE)["downloads"] == 2
        with pytest.raises(SourceBusy):
            with video_sites.access(YOUTUBE, wait=0, download=True, paced=False):
                pytest.fail("a third download ran")
        # Link checks and other sites do not take download places.
        with video_sites.access(YOUTUBE, wait=0), video_sites.access(VIMEO, wait=0, download=True):
            pass

    assert video_sites.state(YOUTUBE)["downloads"] == 0


def test_a_download_waits_for_a_free_place(default_settings):
    default_settings.limits.video_site_max_downloads = 1
    started = threading.Event()
    release = threading.Event()

    def first():
        with video_sites.access(YOUTUBE, wait=0, download=True):
            started.set()
            release.wait(5)

    thread = threading.Thread(target=first)
    thread.start()
    started.wait(5)
    threading.Timer(0.2, release.set).start()

    with video_sites.access(YOUTUBE, wait=5, download=True):
        assert video_sites.state(YOUTUBE)["downloads"] == 1
    thread.join(5)


def test_the_place_of_a_stopped_worker_is_freed_when_its_lease_ends(default_settings):
    default_settings.limits.video_site_max_downloads = 1
    key = video_sites._key(YOUTUBE, video_sites.DOWNLOADS)
    video_sites.redis.zadd(key, {"dead-worker": time.time() - 1})

    with video_sites.access(YOUTUBE, wait=0, download=True):
        assert "dead-worker" not in video_sites.redis.zrange(key, 0, -1)


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
    [
        (RATE_LIMITED, SourceTemporarilyUnavailable),
        (BOT_CHECK, SourceTemporarilyUnavailable),
        (PRIVATE, SourceVideoUnavailable),
        (READ_TIMEOUT, SourceTimedOut),
    ],
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
    video_sites.open_breaker(YOUTUBE, video_sites.LIMITED, "429")
    video_info.save_video_info(tmp_path, {"title": "Nocturne"}, checked_at=time.time())

    with pytest.raises(SourceTemporarilyUnavailable):
        audio.download_audio("https://youtu.be/1", tmp_path)

    assert commands == []


def test_downloads_from_saved_info_take_a_download_place(processes, tmp_path, default_settings, monkeypatch):
    default_settings.limits.video_site_max_downloads = 1
    monkeypatch.setattr(audio, "SITE_WAIT", 0)
    video_info.save_video_info(tmp_path, {"title": "Nocturne"}, checked_at=time.time())

    with video_sites.access(YOUTUBE, wait=0, download=True):
        with pytest.raises(SourceBusy):
            audio.download_audio(URL, tmp_path)

    audio.download_audio(URL, tmp_path)


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
        raise video_sites.failure(YOUTUBE, failed(RATE_LIMITED))

    module.transcribe_source = transcribe_source
    monkeypatch.setitem(sys.modules, "app.transcription.pipeline", module)

    tasks.transcribe_job.fn("job", URL)

    job = tasks.redis.hgetall(tasks.job_key("job"))
    assert job["status"] == "failed"
    assert job["error_code"] == "source_temporarily_unavailable"
    assert job["error"] == video_sites.refused_message(YOUTUBE)
    assert billing.get_balance("user") == 10
    status = client.get("/api/transcriptions/job", headers={"Authorization": "Bearer user"}).json()
    assert status["error_code"] == "source_temporarily_unavailable"


# --- API -----------------------------------------------------------------------

@pytest.fixture
def yt_dlp(api_context, monkeypatch):
    output = {"stdout": '{"duration": 90, "title": "Nocturne"}', "error": None, "commands": []}

    def fake_run(command, **kwargs):
        output["commands"].append(command)
        if isinstance(output["error"], Exception):
            raise output["error"]
        if output["error"] is not None:
            raise subprocess.CalledProcessError(1, command, stderr=output["error"])
        return subprocess.CompletedProcess(command, 0, stdout=output["stdout"])

    monkeypatch.setattr(billing_routes, "ensure_safe_source_url", lambda url: url)
    monkeypatch.setattr(billing_routes.subprocess, "run", fake_run)
    return output


def refusal(url=URL):
    with pytest.raises(HTTPException) as error:
        billing_routes.check_video(url)
    return error.value


def test_rate_limited_check_is_refused_with_retry_after(yt_dlp):
    yt_dlp["error"] = RATE_LIMITED

    error = refusal()

    assert error.status_code == 503
    assert error.detail == {"message": video_sites.refused_message(YOUTUBE), "error_code": "source_temporarily_unavailable"}
    assert error.headers["Retry-After"] == str(30 * 60)
    # Another YouTube link is refused without asking the site; another site is asked.
    yt_dlp["error"] = None
    assert refusal("https://youtu.be/2").status_code == 503
    assert len(yt_dlp["commands"]) == 1
    billing_routes.check_video("https://vimeo.com/2")
    assert len(yt_dlp["commands"]) == 2


@pytest.mark.parametrize(
    ("error", "status", "code", "ttl"),
    [
        (PRIVATE, 422, "source_video_unavailable", 60),
        (subprocess.TimeoutExpired("yt-dlp", 60), 503, "source_temporarily_unavailable", 30),
    ],
)
def test_some_failures_are_remembered_briefly(yt_dlp, error, status, code, ttl):
    yt_dlp["error"] = error

    first, second = refusal(), refusal()

    assert first.status_code == second.status_code == status
    assert second.detail == first.detail
    assert second.detail["error_code"] == code
    assert len(yt_dlp["commands"]) == 1
    [key] = billing_routes.redis.scan_iter(billing_routes.VIDEO_CHECK_KEY + "*")
    assert 0 < billing_routes.redis.ttl(key) <= ttl


def test_timeouts_of_different_links_open_the_short_breaker(yt_dlp):
    yt_dlp["error"] = subprocess.TimeoutExpired("yt-dlp", 60)
    for number in range(video_sites.TIMEOUT_LIMIT):
        refusal(f"https://youtube.com/watch?v={number}")

    yt_dlp["error"] = None
    error = refusal("https://youtube.com/watch?v=new")

    assert error.status_code == 503
    assert "not answering" in error.detail["message"]
    assert len(yt_dlp["commands"]) == video_sites.TIMEOUT_LIMIT


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
    key = billing_routes.VIDEO_CHECK_KEY + billing_routes.hashlib.sha256(URL.encode()).hexdigest()
    billing_routes.redis.set(key + billing_routes.CHECKING_SUFFIX, "1")
    checked = {"info": {"duration": 90, "title": "Nocturne"}, "checked_at": 1.0}
    # The running check finishes while this one waits.
    monkeypatch.setattr(billing_routes.time, "sleep", lambda seconds: billing_routes.redis.set(key, json.dumps(checked)))

    video = billing_routes.check_video(URL)

    assert video.info["title"] == "Nocturne"
    assert yt_dlp["commands"] == []


def test_links_to_a_refused_site_are_refused_but_others_and_uploads_accepted(api_context, monkeypatch):
    client, _, _ = api_context
    from app.api.routes import transcriptions

    monkeypatch.setattr(transcriptions, "probe_audio", lambda path, measure=True, max_seconds=None: 130.0)
    monkeypatch.setattr(billing_routes, "video_duration", lambda url: 90.0)
    monkeypatch.setattr(transcriptions, "check_video", lambda url: pytest.fail("the link was checked"))
    video_sites.open_breaker(YOUTUBE, video_sites.LIMITED, "429")

    link = client.post("/api/transcriptions", json={"source_url": "https://youtu.be/1"})
    cost = client.get("/api/billing/cost", params={"source_url": URL})
    other = client.get("/api/billing/cost", params={"source_url": "https://vimeo.com/1"})
    upload = client.post(
        "/api/transcriptions/upload",
        params={"filename": "song.mp3"},
        content=b"audio",
        headers={"Content-Type": "application/octet-stream"},
    )

    assert link.status_code == cost.status_code == 503
    assert link.json()["detail"]["error_code"] == "source_temporarily_unavailable"
    assert 0 < int(link.headers["Retry-After"]) <= 30 * 60
    assert other.status_code == 200
    assert upload.status_code == 202
    # No place was held for the refused link.
    statuses = [tasks.redis.hget(key, "status") for key in tasks.redis.scan_iter("transcription:*")]
    assert statuses == ["queued"]


# --- Administration ------------------------------------------------------------

def test_status_shows_and_limits_reset_ends_a_refusal(api_context, capsys):
    main(["status"])
    assert "Video sites   all answering" in capsys.readouterr().out

    video_sites.open_breaker(YOUTUBE, video_sites.LIMITED, "HTTP Error 429: Too Many Requests")
    video_sites.open_breaker(VIMEO, video_sites.TIMED_OUT, "3 timeouts within 5 minutes")
    main(["status"])
    output = capsys.readouterr().out
    assert "Video sites   vimeo.com: LINKS REFUSED for 2 m: it is not answering (3 timeouts" in output
    assert "\n              youtube.com: LINKS REFUSED for 30 m: it refused this server (HTTP Error 429" in output

    main(["limits", "sites", "reset", "youtu.be", "--by", "admin"])
    assert "Links to youtube.com are accepted again" in capsys.readouterr().out
    assert video_sites.refusal(YOUTUBE) is None
    assert video_sites.refusal(VIMEO) is not None
    main(["limits"])
    assert "\n                 youtube.com: answering; incidents so far: 1" in capsys.readouterr().out
    main(["limits", "sites", "reset", "--by", "admin"])
    assert "Links to vimeo.com are accepted again" in capsys.readouterr().out
