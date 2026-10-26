import functools
import http.server
import json
import shutil
import subprocess
import threading

import pytest

from app import video_info
from app.errors import SourceError
from app.transcription import audio
from app.video_info import VIDEO_INFO_FILE_NAME, VIDEO_INFO_MAX_AGE, save_video_info, saved_video_info


def test_saved_info_is_slimmed_and_dated_by_the_check(tmp_path):
    info = {"title": "Nocturne", "duration": 90, "formats": [], "automatic_captions": {"en": []}, "heatmap": []}

    path = save_video_info(tmp_path / "job", info, checked_at=1_000_000)

    assert json.loads(path.read_text()) == {"title": "Nocturne", "duration": 90, "formats": []}
    assert path.stat().st_mtime == 1_000_000


def test_recent_info_is_used(tmp_path):
    save_video_info(tmp_path, {"title": "x"}, checked_at=1_000_000)

    assert saved_video_info(tmp_path, now=1_000_000 + VIDEO_INFO_MAX_AGE) == tmp_path / VIDEO_INFO_FILE_NAME


def test_stale_info_is_deleted(tmp_path):
    save_video_info(tmp_path, {"title": "x"}, checked_at=1_000_000)

    assert saved_video_info(tmp_path, now=1_000_001 + VIDEO_INFO_MAX_AGE) is None
    assert not (tmp_path / VIDEO_INFO_FILE_NAME).exists()


def test_missing_info(tmp_path):
    assert saved_video_info(tmp_path) is None


def test_metadata_from_info():
    info = {"title": "Nocturne", "channel": "Chopin", "upload_date": "20240131", "duration": 90}

    metadata = video_info.video_metadata(info, "https://youtu.be/1")

    assert metadata["author"] == "Chopin"
    assert metadata["upload_date"] == "2024-01-31"
    assert metadata["webpage_url"] == "https://youtu.be/1"


@pytest.fixture
def processes(monkeypatch):
    """Fake yt-dlp and ffmpeg. yt-dlp fails while `failing` holds a source."""
    commands = []
    failing = set()

    def run_process(command, cancellation_callback=None):
        commands.append(command)
        if command[0] == "yt-dlp":
            output_dir = audio.Path(command[command.index("-o") + 1]).parent
            (output_dir / "download.webm.part").write_bytes(b"partial")
            if command[-2] in failing:
                raise subprocess.CalledProcessError(1, command, stderr="ERROR: HTTP Error 403: Forbidden")
            (output_dir / "download.webm").write_bytes(b"audio")
            return subprocess.CompletedProcess(command, 0, json.dumps({"title": "Nocturne", "duration": 130}), "")
        audio.Path(command[-1]).write_bytes(b"RIFF")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(audio, "run_process", run_process)
    monkeypatch.setattr(audio, "ensure_safe_source_url", lambda url: url)
    monkeypatch.setattr(audio, "ffmpeg_thread_count", lambda: None)
    return commands, failing


def yt_dlp_sources(commands):
    return [command[-2:] for command in commands if command[0] == "yt-dlp"]


def test_download_uses_the_saved_info(processes, tmp_path):
    commands, _ = processes
    info = save_video_info(tmp_path, {"title": "Nocturne"}, checked_at=video_info.time.time())

    downloaded = audio.download_audio("https://youtube.com/watch?v=1", tmp_path)

    # One yt-dlp run, and it never sees the URL.
    assert yt_dlp_sources(commands) == [["--load-info-json", str(info)]]
    assert downloaded.metadata["title"] == "Nocturne"
    # Kept for a retry on another backend until the job ends.
    assert info.exists()


def test_download_asks_the_site_again_when_the_saved_links_fail(processes, tmp_path, caplog):
    commands, failing = processes
    failing.add("--load-info-json")
    info = save_video_info(tmp_path, {"title": "Nocturne"}, checked_at=video_info.time.time())

    downloaded = audio.download_audio("https://youtube.com/watch?v=1", tmp_path)

    assert yt_dlp_sources(commands) == [["--load-info-json", str(info)], ["--", "https://youtube.com/watch?v=1"]]
    assert downloaded.path == tmp_path / "audio.wav"
    assert not info.exists()
    assert "403" in caplog.text
    # The failed attempt's partial file is not converted.
    assert commands[-1][commands[-1].index("-i") + 1] == str(tmp_path / "download.webm")


def test_download_fails_once_the_site_fails_too(processes, tmp_path):
    commands, failing = processes
    failing.update({"--load-info-json", "--"})
    save_video_info(tmp_path, {"title": "Nocturne"}, checked_at=video_info.time.time())

    with pytest.raises(SourceError) as error:
        audio.download_audio("https://youtube.com/watch?v=1", tmp_path)

    assert len(yt_dlp_sources(commands)) == 2
    assert isinstance(error.value.__cause__, subprocess.CalledProcessError)


def test_download_with_stale_info_asks_the_site(processes, tmp_path):
    commands, _ = processes
    save_video_info(tmp_path, {"title": "Nocturne"}, checked_at=video_info.time.time() - VIDEO_INFO_MAX_AGE - 1)

    audio.download_audio("https://youtube.com/watch?v=1", tmp_path)

    assert yt_dlp_sources(commands) == [["--", "https://youtube.com/watch?v=1"]]


def test_cancelled_download_is_not_retried(processes, tmp_path, monkeypatch):
    calls = []

    def run_process(command, cancellation_callback=None):
        calls.append(command)
        raise audio.TranscriptionCancelled()

    monkeypatch.setattr(audio, "run_process", run_process)
    save_video_info(tmp_path, {"title": "Nocturne"}, checked_at=video_info.time.time())

    with pytest.raises(audio.TranscriptionCancelled):
        audio.download_audio("https://youtube.com/watch?v=1", tmp_path)

    assert len(calls) == 1


def test_saved_info_is_removed_with_the_job_audio(tmp_path):
    save_video_info(tmp_path, {"title": "x"}, checked_at=1)

    audio.remove_audio_files(tmp_path, keep_upload=True)
    assert (tmp_path / VIDEO_INFO_FILE_NAME).exists()

    audio.remove_audio_files(tmp_path)
    assert not (tmp_path / VIDEO_INFO_FILE_NAME).exists()


@pytest.fixture
def media_server(tmp_path):
    """A local server for a video's audio and video streams; paths under
    /expired answer 403, like an expired signed link."""
    served = tmp_path / "served"
    served.mkdir()
    (served / "audio.webm").write_bytes(b"AUDIO" * 100)
    (served / "video.mp4").write_bytes(b"VIDEO" * 100)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.startswith("/expired"):
                self.send_error(403)
            else:
                super().do_GET()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(served)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def info_with_streams(base):
    return {
        "id": "jNQXAC9IVRw",
        "title": "Me at the zoo",
        "duration": 19,
        "extractor": "youtube",
        "extractor_key": "Youtube",
        "webpage_url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "formats": [
            {"format_id": "251", "url": f"{base}/audio.webm", "ext": "webm", "acodec": "opus", "vcodec": "none"},
            {"format_id": "134", "url": f"{base}/video.mp4", "ext": "mp4", "acodec": "none", "vcodec": "avc1"},
        ],
        # What the check chose; the download chooses again.
        "format_id": "134",
    }


requires_yt_dlp = pytest.mark.skipif(not shutil.which("yt-dlp"), reason="yt-dlp is not installed")


@requires_yt_dlp
def test_real_yt_dlp_downloads_the_audio_from_saved_info(tmp_path, media_server):
    output = tmp_path / "job"
    info = save_video_info(output, info_with_streams(media_server), checked_at=1)

    result = audio.run_yt_dlp(["--load-info-json", str(info)], output)

    assert json.loads(result.stdout.splitlines()[-1])["format_id"] == "251"
    assert (output / "download.webm").read_bytes() == b"AUDIO" * 100


@requires_yt_dlp
def test_real_yt_dlp_fails_on_expired_links(tmp_path, media_server):
    # yt-dlp does not ask the site again by itself; download_audio does.
    output = tmp_path / "job"
    info = save_video_info(output, info_with_streams(f"{media_server}/expired"), checked_at=1)

    with pytest.raises(subprocess.CalledProcessError) as error:
        audio.run_yt_dlp(["--load-info-json", str(info)], output)

    assert "403" in error.value.stderr
    assert not (output / "download.webm").exists()
