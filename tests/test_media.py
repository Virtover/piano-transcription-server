import json
import shutil
import subprocess

import pytest

from app import media
from app.media import UnsupportedMedia, probe_audio


def fake_ffprobe(monkeypatch, output=None, error=None):
    def run(command, **kwargs):
        assert command[0] == "ffprobe"
        assert command[command.index("-protocol_whitelist") + 1] == "file"
        if error:
            raise error
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(output))

    monkeypatch.setattr(media.subprocess, "run", run)


def probe_output(format_name, duration="185.3", streams=({"codec_type": "audio"},)):
    return {"format": {"format_name": format_name, "duration": duration}, "streams": list(streams)}


@pytest.mark.parametrize("format_name", ["mp3", "wav", "flac", "ogg", "mov,mp4,m4a,3gp,3g2,mj2", "matroska,webm"])
def test_supported_formats(monkeypatch, tmp_path, format_name):
    fake_ffprobe(monkeypatch, probe_output(format_name))

    assert probe_audio(tmp_path / "file") == 185.3


@pytest.mark.parametrize("format_name", ["hls", "concat", "image2", "tty", "lavfi", "sdp", ""])
def test_unsupported_formats(monkeypatch, tmp_path, format_name):
    fake_ffprobe(monkeypatch, probe_output(format_name))

    with pytest.raises(UnsupportedMedia, match="format is not supported"):
        probe_audio(tmp_path / "file")


def test_file_without_audio(monkeypatch, tmp_path):
    fake_ffprobe(monkeypatch, probe_output("mov,mp4,m4a,3gp,3g2,mj2", streams=()))

    with pytest.raises(UnsupportedMedia, match="no audio"):
        probe_audio(tmp_path / "file")


@pytest.mark.parametrize("duration", [None, "N/A", "0", "-1"])
def test_file_without_duration(monkeypatch, tmp_path, duration):
    fake_ffprobe(monkeypatch, probe_output("mp3", duration=duration))

    with pytest.raises(UnsupportedMedia, match="duration"):
        probe_audio(tmp_path / "file")


@pytest.mark.parametrize(
    "error",
    [subprocess.CalledProcessError(1, "ffprobe"), subprocess.TimeoutExpired("ffprobe", 60)],
)
def test_unreadable_file(monkeypatch, tmp_path, error):
    fake_ffprobe(monkeypatch, error=error)

    with pytest.raises(UnsupportedMedia, match="not a readable"):
        probe_audio(tmp_path / "file")


requires_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg is not installed",
)


def ffmpeg(*arguments):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *arguments], check=True)


@requires_ffmpeg
@pytest.mark.parametrize("suffix", ["wav", "mp3", "flac", "ogg", "m4a"])
def test_real_audio_files(tmp_path, suffix):
    path = tmp_path / f"tone.{suffix}"
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=3", str(path))

    assert probe_audio(path) == pytest.approx(3, abs=0.1)


@requires_ffmpeg
def test_real_video_with_audio(tmp_path):
    path = tmp_path / "clip.mp4"
    ffmpeg(
        "-f", "lavfi", "-i", "color=size=64x64:duration=2",
        "-f", "lavfi", "-i", "sine=duration=2",
        "-shortest", str(path),
    )

    assert probe_audio(path) == pytest.approx(2, abs=0.1)


@requires_ffmpeg
def test_real_video_without_audio(tmp_path):
    path = tmp_path / "silent.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=size=64x64:duration=2", str(path))

    with pytest.raises(UnsupportedMedia, match="no audio"):
        probe_audio(path)


@requires_ffmpeg
def test_real_playlist_cannot_reach_other_files(tmp_path):
    audio = tmp_path / "secret.wav"
    ffmpeg("-f", "lavfi", "-i", "sine=duration=2", str(audio))
    playlist = tmp_path / "upload.input"
    playlist.write_text(f"#EXTM3U\n#EXTINF:2,\nfile://{audio}\n#EXT-X-ENDLIST\n")

    with pytest.raises(UnsupportedMedia):
        probe_audio(playlist)


@requires_ffmpeg
@pytest.mark.parametrize(
    "content",
    [b"", b"<html>not audio</html>", bytes(range(256)) * 64],
    ids=["empty", "html", "binary"],
)
def test_real_garbage_is_rejected(tmp_path, content):
    path = tmp_path / "upload.input"
    path.write_bytes(content)

    with pytest.raises(UnsupportedMedia):
        probe_audio(path)
