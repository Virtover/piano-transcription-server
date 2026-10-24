"""Job failures as users see them. The full error goes to the logs and the
job diagnostics instead: it can contain command lines and internal paths."""

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


def public_message(error: BaseException) -> str:
    from app.outsource.provider import RemoteFailed

    if isinstance(error, (UserFacingError, RemoteFailed)):
        return str(error)
    if isinstance(error, subprocess.CalledProcessError):
        tool = Path(str(error.cmd[0])).name if error.cmd else ""
        message = TOOL_MESSAGES.get(tool, GENERIC_MESSAGE)
        if tool == "yt-dlp":
            match = YT_DLP_ERROR.search(error.stderr or "")
            if match:
                reason = match.group("reason").strip()[:MAX_REASON_LENGTH]
                message = f"{message}: {reason}"
        return message
    return GENERIC_MESSAGE


def detail(error: BaseException) -> str:
    """The error for operators: its type, message, and a command's output."""
    text = f"{type(error).__name__}: {error}"
    if isinstance(error, subprocess.CalledProcessError) and error.stderr:
        text += f"\n{str(error.stderr).strip()[-MAX_DETAIL_LENGTH:]}"
    return text[:MAX_DETAIL_LENGTH * 2]
