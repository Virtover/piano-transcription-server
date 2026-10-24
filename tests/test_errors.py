import subprocess

import pytest

from app.errors import GENERIC_MESSAGE, detail, public_message
from app.media import UnsupportedMedia
from app.outsource.provider import RemoteFailed
from app.transcription.batching import GpuOutOfMemory
from app.url_safety import UnsafeSourceUrl


def failed(tool, stderr=""):
    command = [tool, "--secret-option", "/data/jobs/abc/download.%(ext)s", "--", "https://youtube.com/watch?v=1"]
    return subprocess.CalledProcessError(1, command, output="", stderr=stderr)


@pytest.mark.parametrize(
    "error",
    [
        UnsupportedMedia("The file has no audio"),
        UnsafeSourceUrl("This site is not supported"),
        GpuOutOfMemory(),
        RemoteFailed("Remote transcription failed"),
    ],
)
def test_messages_written_for_users_are_shown(error):
    assert public_message(error) == str(error)


def test_command_lines_are_never_shown():
    message = public_message(failed("ffmpeg", "Invalid data found when processing input"))

    assert message == "The audio could not be converted"


def test_yt_dlp_reason_is_shown_without_the_command():
    stderr = "WARNING: something\nERROR: [youtube] dQw4w9WgXcQ: Video unavailable. This video is private\n"

    message = public_message(failed("/usr/local/bin/yt-dlp", stderr))

    assert message == "The video could not be downloaded: Video unavailable. This video is private"
    assert "/data" not in message and "secret" not in message


def test_yt_dlp_without_a_reason():
    assert public_message(failed("yt-dlp", "Traceback ...")) == "The video could not be downloaded"


@pytest.mark.parametrize("error", [RuntimeError("CUDA error at /secret/path"), KeyError("x"), ValueError("y")])
def test_other_errors_are_generic(error):
    assert public_message(error) == GENERIC_MESSAGE


def test_detail_keeps_everything_for_operators():
    text = detail(failed("ffmpeg", "Invalid data found"))

    assert text.startswith("CalledProcessError: Command")
    assert "/data/jobs/abc" in text
    assert text.endswith("Invalid data found")


def test_detail_is_bounded():
    assert len(detail(failed("ffmpeg", "x" * 100_000))) <= 4000
