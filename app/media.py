import json
import subprocess
from pathlib import Path

from app.errors import UserFacingError


# ffprobe format names accepted for uploaded files. Playlist and concat
# formats are excluded because they make ffmpeg open other files or URLs.
ALLOWED_FORMATS = {
    "aac",
    "aiff",
    "asf",
    "flac",
    "m4a",
    "matroska",
    "mov",
    "mp3",
    "mp4",
    "ogg",
    "wav",
    "webm",
}


# Decoding a file to measure it may take this long; longer files are rejected.
MEASURE_TIMEOUT = 180


class UnsupportedMedia(UserFacingError, ValueError):
    """The file is not an audio or video file that can be transcribed."""


def probe_audio(path: Path, measure: bool = True, max_seconds: float | None = None) -> float:
    """Return the duration in seconds of an uploaded audio or video file.

    The duration in the file's header can be wrong, by accident (a VBR MP3
    without a length header) or on purpose, to pay for less than is
    transcribed. With measure, the audio is decoded and its real length is
    returned; decoding stops just past max_seconds, the length limit.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "file",
                "-select_streams",
                "a",
                "-show_entries",
                "format=format_name,duration:stream=codec_type",
                "-of",
                "json",
                "--",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        info = json.loads(result.stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        raise UnsupportedMedia("The file is not a readable audio or video file") from error

    format_info = info.get("format") or {}
    format_names = set(str(format_info.get("format_name", "")).split(","))
    if not format_names & ALLOWED_FORMATS:
        raise UnsupportedMedia("This file format is not supported")
    if not info.get("streams"):
        raise UnsupportedMedia("The file has no audio")

    try:
        duration = float(format_info["duration"])
    except (KeyError, TypeError, ValueError) as error:
        raise UnsupportedMedia("The file duration is unavailable") from error
    if duration <= 0:
        raise UnsupportedMedia("The file duration is unavailable")
    if measure:
        duration = measure_audio(path, max_seconds)
    return duration


def measure_audio(path: Path, max_seconds: float | None = None) -> float:
    """The length of the first audio stream, found by decoding all of it."""
    command = ["ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file", "-i", str(path)]
    if max_seconds is not None:
        # Enough to see the file is too long, without decoding the rest.
        command += ["-t", f"{max_seconds + 1:.3f}"]
    command += ["-map", "0:a:0", "-vn", "-f", "null", "-progress", "pipe:1", "-nostats", "-"]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=MEASURE_TIMEOUT)
    except subprocess.TimeoutExpired as error:
        raise UnsupportedMedia("The file is too long to process") from error
    except subprocess.CalledProcessError as error:
        raise UnsupportedMedia("The file is not a readable audio or video file") from error

    # -progress writes key=value lines; the last out_time_us is the decoded length.
    times = [
        line.split("=", 1)[1]
        for line in result.stdout.splitlines()
        if line.startswith("out_time_us=") and line.split("=", 1)[1].isdigit()
    ]
    try:
        duration = int(times[-1]) / 1_000_000
    except (IndexError, ValueError) as error:
        raise UnsupportedMedia("The file duration is unavailable") from error
    if duration <= 0:
        raise UnsupportedMedia("The file has no audio")
    return duration
