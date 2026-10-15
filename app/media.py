import json
import subprocess
from pathlib import Path


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


class UnsupportedMedia(ValueError):
    """The file is not an audio or video file that can be transcribed."""


def probe_audio(path: Path) -> float:
    """Return the duration in seconds of an uploaded audio or video file."""
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
    return duration
