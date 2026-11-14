"""Reading a video's info with yt-dlp, to learn its length before a job is
charged. The API checks links itself. In a home cluster, while a video site
refuses the hub's address, it asks a node that reaches the site from
another address and runs the `check` task (app/cluster.py).

A node answers with the length and the metadata clients see, never the
full info: the API would save that for the worker's download, and a node
taken over by a malicious file could fill it with addresses for yt-dlp to
fetch elsewhere. The worker then reads the link from the site itself."""

import json
import logging
import math
import subprocess
import uuid
from typing import Any

from redis import Redis

from app import cluster, video_info, video_sites
from app.config import settings
from app.errors import SourceBusy, SourceError, SourceTemporarilyUnavailable, SourceTimedOut, SourceUnsupported
from app.errors import SourceVideoUnavailable
from app.slots import VIDEO_CHECKS_KEY, LeasedSlot
from app.url_safety import YT_DLP_SAFETY_OPTIONS, ensure_safe_source_url


logger = logging.getLogger(__name__)

METADATA_WAIT = 10
# How long the API waits for a node's answer, beyond the check's own minute.
DELEGATED_WAIT = 80
REPLY_TTL = 120
# What a node may answer with: the length, and video_metadata's fields.
DELEGATED_FIELDS = ("duration", "title", "uploader", "channel", "channel_id", "channel_url", "upload_date",
                    "thumbnail", "webpage_url", "view_count", "like_count")
TEXT_LIMIT = 2000
# Set in the info of a node's answer: it is not saved for the download.
DELEGATED_MARK = "_checked_by_node"
# Errors a node's answer may name, by class name.
ERRORS = {kind.__name__: kind for kind in (
    SourceError, SourceBusy, SourceTemporarilyUnavailable, SourceTimedOut, SourceUnsupported, SourceVideoUnavailable,
)}


def read_video_info(redis: Redis, safe_url: str, take_slot: bool = True) -> dict[str, Any]:
    """The video's info, slimmed, from this device's address. take_slot
    holds one of MAX_VIDEO_CHECKS while it runs; a node checking for the
    API does not, as the API holds one for it."""
    site = video_sites.site_of(safe_url)
    slot = LeasedSlot(redis, VIDEO_CHECKS_KEY, settings.limits.max_video_checks) if take_slot else None
    if slot is not None and not slot.take(METADATA_WAIT):
        raise SourceBusy(video_sites.BUSY_MESSAGE, retry_after=5)
    try:
        with video_sites.access(site, METADATA_WAIT):
            try:
                result = subprocess.run(
                    [
                        "yt-dlp",
                        *YT_DLP_SAFETY_OPTIONS,
                        *video_sites.YT_DLP_SITE_OPTIONS,
                        "--dump-single-json",
                        "--no-download",
                        "--",
                        safe_url,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                raise video_sites.failure(site, error) from error
            except OSError as error:
                raise SourceError("Unable to read video metadata") from error
        info = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise SourceError("Unable to read video metadata") from error
    finally:
        if slot is not None:
            slot.release()
    duration = info.get("duration") if isinstance(info, dict) else None
    if not isinstance(duration, (int, float)) or duration <= 0:
        raise SourceError("Video duration is unavailable")
    return video_info.slim(info)


# --- Checks by a node ------------------------------------------------------------------

def checking_addresses(redis: Redis, site: str) -> list[str]:
    """Addresses of nodes that check links now and that site does not refuse."""
    own = cluster.egress(redis)
    return [
        address for address in cluster.live_egresses(redis, "check")
        if address != own and video_sites.refusal(site, address) is None
    ]


def refusal_everywhere(redis: Redis, site: str) -> SourceTemporarilyUnavailable | None:
    """The refusal of this device's address, unless a node can check the
    link from an address the site does not refuse."""
    refused = video_sites.refusal(site)
    if refused is None or not cluster.has_nodes(redis):
        return refused
    return None if checking_addresses(redis, site) else refused


def should_delegate(redis: Redis, safe_url: str) -> bool:
    site = video_sites.site_of(safe_url)
    return (cluster.has_nodes(redis) and video_sites.refusal(site) is not None
            and bool(checking_addresses(redis, site)))


def delegate(redis: Redis, safe_url: str, send) -> dict[str, Any]:
    """Ask a node to check the link and wait for its answer. send(request,
    url) queues the check; the info returned holds only DELEGATED_FIELDS,
    and `delegated` says not to save it for the download."""
    request = uuid.uuid4().hex
    send(request, safe_url)
    answer = redis.blpop([cluster.check_reply_key(request)], timeout=DELEGATED_WAIT)
    if answer is None:
        raise SourceBusy(video_sites.BUSY_MESSAGE, retry_after=10)
    return parse_answer(answer[1])


def answer(redis: Redis, request: str, safe_url: str) -> None:
    """Run on a node: check the link and send the answer to the API."""
    try:
        info = read_video_info(redis, ensure_safe_source_url(safe_url), take_slot=False)
        reply = {"info": {name: info.get(name) for name in DELEGATED_FIELDS}}
    except SourceError as error:
        reply = {"error": {"kind": type(error).__name__, "message": str(error), "retry_after": error.retry_after}}
    except Exception:
        logger.exception("Could not check a link for the API")
        reply = {"error": {"kind": "SourceError", "message": "Unable to read video metadata", "retry_after": None}}
    key = cluster.check_reply_key(request)
    with redis.pipeline() as pipe:
        pipe.rpush(key, json.dumps(reply))
        pipe.expire(key, REPLY_TTL)
        pipe.execute()


def parse_answer(text: str) -> dict[str, Any]:
    """A node's answer as info, or the SourceError it names. Anything else in
    it is ignored, and text is cut short: nodes are trusted with jobs, not
    with what the API shows or saves."""
    try:
        reply = json.loads(text)
    except ValueError:
        raise SourceError("Unable to read video metadata") from None
    if not isinstance(reply, dict):
        raise SourceError("Unable to read video metadata")
    if isinstance(reply.get("error"), dict):
        failed = reply["error"]
        kind = ERRORS.get(failed.get("kind"), SourceError)
        retry_after = failed.get("retry_after")
        retry_after = int(retry_after) if isinstance(retry_after, (int, float)) and 0 < retry_after < 7 * 86400 else None
        message = str(failed.get("message") or "Unable to read video metadata")[:TEXT_LIMIT]
        raise kind(message, retry_after)
    info = reply.get("info")
    if not isinstance(info, dict):
        raise SourceError("Unable to read video metadata")
    duration = info.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise SourceError("Video duration is unavailable")
    cleaned: dict[str, Any] = {"duration": float(duration)}
    for name in DELEGATED_FIELDS[1:]:
        value = info.get(name)
        if isinstance(value, str):
            cleaned[name] = value[:TEXT_LIMIT]
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            cleaned[name] = value
    return cleaned
