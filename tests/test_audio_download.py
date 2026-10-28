import json
import subprocess

import pytest

from app import media
from app.transcription import audio


@pytest.fixture
def processes(monkeypatch):
    """Fake yt-dlp and ffmpeg: record each command and create its output."""
    commands = []

    def run_process(command, cancellation_callback=None):
        commands.append(command)
        if command[0] == "yt-dlp":
            output_dir = audio.Path(command[command.index("-o") + 1]).parent
            (output_dir / "download.webm.part").write_bytes(b"partial")
            (output_dir / "download.webm").write_bytes(b"audio")
            return subprocess.CompletedProcess(command, 0, json.dumps({"title": "Nocturne", "duration": 130}), "")
        audio.Path(command[-1]).write_bytes(b"RIFF")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(audio, "run_process", run_process)
    monkeypatch.setattr(audio, "ensure_safe_source_url", lambda url: url)
    monkeypatch.setattr(audio, "ffmpeg_thread_count", lambda: None)
    return commands


def option(command, name):
    return command[command.index(name) + 1]


def test_download_cannot_reach_other_sites(processes, tmp_path):
    audio.download_audio("https://youtube.com/watch?v=1", tmp_path)

    yt_dlp = processes[0]
    assert option(yt_dlp, "--use-extractors") == "default,-generic"
    assert "--ignore-config" in yt_dlp
    assert yt_dlp[-2:] == ["--", "https://youtube.com/watch?v=1"]
    assert option(yt_dlp, "--max-filesize") == audio.MAX_DOWNLOAD_SIZE


def test_download_is_converted_only_by_the_restricted_ffmpeg(processes, tmp_path):
    downloaded = audio.download_audio("https://youtube.com/watch?v=1", tmp_path)

    yt_dlp, ffmpeg = processes
    # yt-dlp's own conversion would run ffmpeg without the protocol whitelist.
    assert "-x" not in yt_dlp and "--audio-format" not in yt_dlp
    assert ffmpeg[0] == "ffmpeg"
    assert option(ffmpeg, "-protocol_whitelist") == "file"
    # Sites serve formats uploads may not have, such as MPEG-TS.
    assert "-format_whitelist" not in ffmpeg
    # The finished download, never the partial file.
    assert option(ffmpeg, "-i") == str(tmp_path / "download.webm")
    assert downloaded.path == tmp_path / "audio.wav"
    assert not (tmp_path / "download.webm").exists()


def test_download_is_converted_up_to_the_paid_duration(processes, tmp_path):
    audio.download_audio("https://youtube.com/watch?v=1", tmp_path, max_seconds=130)

    assert option(processes[1], "-t") == f"{130 + audio.DURATION_TOLERANCE:.3f}"


def test_download_without_a_finished_file_fails(processes, tmp_path, monkeypatch):
    def run_process(command, cancellation_callback=None):
        (tmp_path / "download.webm.part").write_bytes(b"partial")
        return subprocess.CompletedProcess(command, 0, json.dumps({"title": "Nocturne"}), "")

    monkeypatch.setattr(audio, "run_process", run_process)

    with pytest.raises(audio.UserFacingError, match="did not produce an audio file"):
        audio.download_audio("https://youtube.com/watch?v=1", tmp_path)


def test_conversion_without_a_paid_duration_is_not_cut(processes, tmp_path):
    audio.convert_to_wav(tmp_path / "input", tmp_path)

    assert "-t" not in processes[0]


def test_upload_is_converted_up_to_the_paid_duration(processes, tmp_path, monkeypatch):
    probes = []

    def probe_audio(path, measure=True, max_seconds=None):
        probes.append(measure)
        return 3600.0

    monkeypatch.setattr(audio, "probe_audio", probe_audio)

    prepared = audio.prepare_upload(tmp_path / "upload.input", "Song", tmp_path, max_seconds=60)

    # The API measured the length already; the worker only checks the format.
    assert probes == [False]
    assert option(processes[0], "-format_whitelist") == option(media.UPLOAD_INPUT_OPTIONS, "-format_whitelist")
    assert option(processes[0], "-t") == f"{60 + audio.DURATION_TOLERANCE:.3f}"
    assert prepared.metadata["duration"] == 60


def test_fetch_audio_passes_the_paid_duration(processes, tmp_path, monkeypatch):
    monkeypatch.setattr(audio.time, "sleep", lambda seconds: None)

    audio.fetch_audio(tmp_path, source_url="https://youtube.com/watch?v=1", max_seconds=90)

    assert option(processes[1], "-t") == f"{90 + audio.DURATION_TOLERANCE:.3f}"
