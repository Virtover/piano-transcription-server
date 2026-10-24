"""What yt-dlp learned about a video when the API checked its length. The API
saves it in the job directory, and the worker downloads from it without
asking the video site again."""

import json
import os
import time
from pathlib import Path
from typing import Any

VIDEO_INFO_FILE_NAME = "video.info.json"
# The audio links in the info are signed and expire, on YouTube after about
# six hours. Older info is not used; the worker asks the site again.
VIDEO_INFO_MAX_AGE = 3600
# Large fields the download does not use.
UNUSED_FIELDS = ("automatic_captions", "subtitles", "requested_subtitles", "heatmap", "thumbnails")


def slim(info: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in info.items() if key not in UNUSED_FIELDS}


def save_video_info(output_dir: Path, info: dict[str, Any], checked_at: float) -> Path:
    """Write the info for the worker, dated when the video was checked."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / VIDEO_INFO_FILE_NAME
    path.write_text(json.dumps(slim(info)))
    os.utime(path, (checked_at, checked_at))
    return path


def saved_video_info(output_dir: Path, now: float | None = None) -> Path | None:
    """The saved info, if it is recent enough to download from. Stale info is
    deleted."""
    path = output_dir / VIDEO_INFO_FILE_NAME
    try:
        age = (now or time.time()) - path.stat().st_mtime
    except OSError:
        return None
    if age > VIDEO_INFO_MAX_AGE:
        path.unlink(missing_ok=True)
        return None
    return path


def format_upload_date(value: str | None) -> str | None:
    if not value or len(value) != 8 or not value.isdigit():
        return None

    return f"{value[:4]}-{value[4:6]}-{value[6:]}"


def video_metadata(info: dict[str, Any], source_url: str) -> dict[str, Any]:
    """The job metadata clients see, from yt-dlp's info."""
    return {
        "title": info.get("title"),
        "author": info.get("uploader") or info.get("channel"),
        "channel": info.get("channel"),
        "channel_id": info.get("channel_id"),
        "channel_url": info.get("channel_url"),
        "upload_date": format_upload_date(info.get("upload_date")),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url") or source_url,
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
    }
