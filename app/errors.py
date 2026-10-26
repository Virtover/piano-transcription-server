"""Job failures as users see them. The full error goes to the logs and the
job diagnostics instead: it can contain command lines and internal paths.

Each failure also has a stable code, for clients to act on instead of
parsing the message."""

import re
import subprocess
from pathlib import Path


GENERIC_MESSAGE = "Transcription failed"
TOOL_MESSAGES = {
    "yt-dlp": "The video could not be downloaded",
    "ffmpeg": "The audio could not be converted",
}
# yt-dlp's own explanation, such as "Video unavailable. This video is private".
YT_DLP_ERROR = re.compile(r"^ERROR: (?:\[[^\]]+\] [^:\s]+: )?(?P<reason>.+)$", re.MULTILINE)
MAX_REASON_LENGTH = 200
MAX_DETAIL_LENGTH = 2000


class UserFacingError(Exception):
    """A failure whose message is written for the user."""

    code = "transcription_failed"


class SourceError(UserFacingError):
    """A video link that could not be read. status is the API's answer when
    this happens before the job is queued, with Retry-After if retry_after
    is set."""

    code = "source_failed"
    status = 422

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class SourceTemporarilyUnavailable(SourceError):
    """A video site refused this server, or did not answer; see app.video_sites."""

    code = "source_temporarily_unavailable"
    status = 503


class SourceBusy(SourceError):
    """Too many links are being read; the link itself may be fine."""

    code = "source_busy"
    status = 503


class SourceVideoUnavailable(SourceError):
    """The video is private, removed, restricted, or not out yet."""

    code = "source_video_unavailable"


class SourceUnsupported(SourceError):
    """The link is not one this server can read."""

    code = "source_unsupported"


def public_message(error: BaseException) -> str:
    from app.outsource.provider import RemoteFailed

    if isinstance(error, (UserFacingError, RemoteFailed)):
        return str(error)
    if isinstance(error, subprocess.CalledProcessError):
        tool = Path(str(error.cmd[0])).name if error.cmd else ""
        message = TOOL_MESSAGES.get(tool, GENERIC_MESSAGE)
        if tool == "yt-dlp":
            reason = yt_dlp_reason(error.stderr)
            if reason:
                message = f"{message}: {reason}"
        return message
    return GENERIC_MESSAGE


def yt_dlp_reason(stderr: str | None) -> str | None:
    match = YT_DLP_ERROR.search(stderr or "")
    return match.group("reason").strip()[:MAX_REASON_LENGTH] if match else None


def error_code(error: BaseException) -> str:
    if isinstance(error, UserFacingError):
        return error.code
    if isinstance(error, subprocess.CalledProcessError) and error.cmd and Path(str(error.cmd[0])).name == "yt-dlp":
        return SourceError.code
    return UserFacingError.code


def detail(error: BaseException) -> str:
    """The error for operators: its type, message, and a command's output,
    also when the command's failure is the cause of the error."""
    text = f"{type(error).__name__}: {error}"
    cause = error.__cause__
    if cause is not None:
        text += f"\nCaused by {type(cause).__name__}: {cause}"
    for failed in (error, cause):
        if isinstance(failed, subprocess.CalledProcessError) and failed.stderr:
            text += f"\n{str(failed.stderr).strip()[-MAX_DETAIL_LENGTH:]}"
    return text[:MAX_DETAIL_LENGTH * 2]
