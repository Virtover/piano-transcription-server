"""Input checks for the remote inference handler. No torch import."""

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests


DOWNLOAD_TIMEOUT = 60
CHUNK_SIZE = 1024 * 1024
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class InvalidInput(ValueError):
    """The request cannot be processed. Messages never echo the URL."""


def _origin(url: str) -> tuple[str, str, int | None]:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise InvalidInput("The audio URL is malformed") from error
    default_port = {"http": 80, "https": 443}.get(parts.scheme.lower())
    return parts.scheme.lower(), (parts.hostname or "").lower(), port or default_port


def validate_input(job_input: Any, storage_endpoint: str | None) -> tuple[str, str]:
    """Return (audio_url, job_id), or raise InvalidInput."""
    if not isinstance(job_input, dict):
        raise InvalidInput("The input must be an object")
    audio_url = job_input.get("audio_url")
    job_id = job_input.get("job_id")
    if not isinstance(audio_url, str) or not audio_url:
        raise InvalidInput("audio_url is required")
    if not isinstance(job_id, str) or not JOB_ID_PATTERN.match(job_id):
        raise InvalidInput("job_id is missing or invalid")
    if not storage_endpoint:
        raise InvalidInput("STORAGE_ENDPOINT is not configured on the handler")

    allowed = _origin(storage_endpoint)
    scheme, host, port = _origin(audio_url)
    if scheme not in ("http", "https") or not host:
        raise InvalidInput("The audio URL is not an HTTP(S) URL")
    if urlsplit(audio_url).username or urlsplit(audio_url).password:
        raise InvalidInput("The audio URL must not contain credentials")
    if (scheme, host, port) != allowed:
        raise InvalidInput("The audio URL is not on the configured storage host")
    return audio_url, job_id


MODES = ("transcription", "ensemble", "arrangement")
MAX_SOURCE_RATE = 1_000_000


def requested_ensemble(job_input: dict) -> tuple[bool, int | None]:
    """(whether the request is an ensemble job, the source's sample rate or
    None). Servers send mode "ensemble" with source_rate; "arrangement",
    which servers sent before ensemble jobs replaced piano arrangements, is
    an ensemble job too, and its style is ignored. Only known values pass."""
    mode = job_input.get("mode", "transcription")
    if mode not in MODES:
        raise InvalidInput("mode must be transcription or ensemble")
    if mode == "transcription":
        return False, None
    rate = job_input.get("source_rate")
    if rate is None:
        return True, None
    if isinstance(rate, bool) or not isinstance(rate, int) or not 0 < rate <= MAX_SOURCE_RATE:
        raise InvalidInput("source_rate must be a sample rate in Hz")
    return True, rate


def download(url: str, destination: Path, max_bytes: int, session: Any = None) -> int:
    """Stream the audio to destination, stopping above max_bytes."""
    session = session or requests
    try:
        with session.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT, allow_redirects=False) as response:
            if response.status_code != 200:
                raise InvalidInput(f"The audio download failed with HTTP {response.status_code}")
            declared = response.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise InvalidInput("The audio file is larger than OUTSOURCE_MAX_AUDIO_MB")
            size = 0
            with destination.open("wb") as file:
                for chunk in response.iter_content(CHUNK_SIZE):
                    size += len(chunk)
                    if size > max_bytes:
                        raise InvalidInput("The audio file is larger than OUTSOURCE_MAX_AUDIO_MB")
                    file.write(chunk)
    except requests.RequestException as error:
        raise InvalidInput(f"The audio download failed ({type(error).__name__})") from error
    if size == 0:
        raise InvalidInput("The audio file is empty")
    return size
