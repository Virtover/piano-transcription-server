"""Keeping this server in good standing with the video sites it reads links
from. Every yt-dlp call that contacts a site, in the API or a worker, goes
through `access`:

* A rate limit or bot check opens a breaker kept in Redis, so the API and
  every worker stop contacting video sites at once. Links, and their cost,
  are refused with 503 until it closes; uploads are unaffected. Each incident
  in a row doubles the cooldown, from VIDEO_SITE_COOLDOWN up to
  VIDEO_SITE_MAX_COOLDOWN.
* When a cooldown ends, one request at a time is let through until one
  succeeds, so waiting users do not all reach the site at once.
* New link checks start at least VIDEO_SITE_INTERVAL apart, whichever
  process starts them.

One breaker covers every site, as `limits links off` does: what a site
limits is this server's IP. Failed jobs are never retried on their own."""

import logging
import math
import re
import secrets
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager

from redis import Redis
from redis.exceptions import WatchError

from app.config import settings
from app.errors import (
    SourceBusy,
    SourceError,
    SourceTemporarilyUnavailable,
    SourceUnsupported,
    SourceVideoUnavailable,
    public_message,
    yt_dlp_reason,
)


logger = logging.getLogger(__name__)
redis = Redis.from_url(settings.service.redis_url, decode_responses=True)

# The breaker: present while open, with the site's reason; expires with the cooldown.
BLOCKED_KEY = "video_sites:blocked"
# Incidents in a row, which set the next cooldown.
STRIKES_KEY = "video_sites:strikes"
# Present from an incident until a request succeeds after the cooldown.
RECOVERING_KEY = "video_sites:recovering"
# The one request let through while recovering.
PROBE_KEY = "video_sites:probe"
PROBE_TTL = 5 * 60
# When the next paced request may start, as a Unix time.
NEXT_START_KEY = "video_sites:next_start"
# Counts and the last incident, for `python -m app.admin status`.
INCIDENTS_KEY = "video_sites:incidents"
POLL_INTERVAL = 0.5
BUSY_RETRY_AFTER = 5

REFUSED_MESSAGE = "Video links cannot be read at the moment; try again later, or upload the file instead"
TIMED_OUT_MESSAGE = "The video site did not answer in time; try again, or upload the file instead"
BUSY_MESSAGE = "Too many videos are being checked; try again in a moment"
UNSUPPORTED_MESSAGE = "This link cannot be transcribed; upload the file instead"

# What yt-dlp prints when a site limits this server. Checked first: YouTube's
# rate limit reads "Video unavailable. This content isn't available, try
# again later".
LIMITED = re.compile(
    r"HTTP Error (?:429|402)|Too Many Requests|rate[- ]?limit|confirm you.?re not a bot"
    r"|try again later|unusual traffic",
    re.IGNORECASE,
)
# The video itself cannot be read; asking again would not help.
UNAVAILABLE = re.compile(
    r"Video unavailable|Private video|video is private|has been removed|members[- ]only"
    r"|confirm your age|age[- ]restricted|in your country"
    r"|copyright|live event will begin|Premieres in|HTTP Error 404|does not exist",
    re.IGNORECASE,
)
UNSUPPORTED = re.compile(r"Unsupported URL|is not a valid URL", re.IGNORECASE)

# Options for every yt-dlp call that reads a link. Retries are few and back
# off: a site that refuses requests must not be asked harder.
YT_DLP_SITE_OPTIONS = [
    "--js-runtimes",
    "deno",
    "--extractor-args",
    "youtube:player_client=web,android",
    "--socket-timeout",
    "30",
    "--extractor-retries",
    "1",
    "--retry-sleep",
    "extractor:exp=2:30",
]


def failure(error: subprocess.CalledProcessError | subprocess.TimeoutExpired) -> SourceError:
    """The error to raise for a failed yt-dlp call. A rate limit or bot check
    also opens the breaker."""
    if isinstance(error, subprocess.TimeoutExpired):
        return SourceTemporarilyUnavailable(TIMED_OUT_MESSAGE, retry_after=30)
    stderr = error.stderr or ""
    if LIMITED.search(stderr):
        cooldown = open_breaker(yt_dlp_reason(stderr) or "rate limited")
        return SourceTemporarilyUnavailable(REFUSED_MESSAGE, retry_after=cooldown)
    if UNAVAILABLE.search(stderr):
        return SourceVideoUnavailable(public_message(error))
    if UNSUPPORTED.search(stderr):
        return SourceUnsupported(UNSUPPORTED_MESSAGE)
    return SourceError(public_message(error))


def open_breaker(reason: str) -> int:
    """Stop contacting video sites for a cooldown; returns its seconds. An
    open breaker is left as it is, so the failures of one incident count once."""
    base = settings.limits.video_site_cooldown
    longest = max(base, settings.limits.video_site_max_cooldown)
    if not redis.set(BLOCKED_KEY, reason, nx=True, ex=math.ceil(base)):
        return max(redis.ttl(BLOCKED_KEY), 1)
    strikes = redis.incr(STRIKES_KEY)
    cooldown = math.ceil(min(base * 2 ** min(strikes - 1, 20), longest))
    # Incidents count as in a row until a quiet maximum cooldown has passed.
    memory = cooldown + math.ceil(longest)
    with redis.pipeline() as pipe:
        pipe.set(BLOCKED_KEY, reason, ex=cooldown)
        pipe.set(RECOVERING_KEY, "1", ex=memory)
        pipe.expire(STRIKES_KEY, memory)
        pipe.hincrby(INCIDENTS_KEY, "count", 1)
        pipe.hset(INCIDENTS_KEY, mapping={"last_at": str(time.time()), "last_reason": reason})
        pipe.execute()
    logger.warning(
        "A video site refused this server (%s); video links are refused for %d s (incident %d in a row)",
        reason,
        cooldown,
        strikes,
    )
    return cooldown


def refusal() -> SourceTemporarilyUnavailable | None:
    """The error for a link while the breaker is open."""
    ttl = redis.ttl(BLOCKED_KEY)
    if ttl == -2:
        return None
    return SourceTemporarilyUnavailable(
        REFUSED_MESSAGE,
        retry_after=ttl if ttl > 0 else math.ceil(settings.limits.video_site_cooldown),
    )


@contextmanager
def access(wait: float, paced: bool = True) -> Iterator[None]:
    """Permission to contact a video site once, waiting up to `wait` seconds
    for a turn. Raises SourceTemporarilyUnavailable while the breaker is
    open, and SourceBusy when no turn comes in time. paced=False is for
    downloads from saved info, which fetch the audio, not the site's pages."""
    deadline = time.monotonic() + wait
    probe = _probe_turn(deadline)
    try:
        if paced:
            _wait_for_start(deadline)
        yield
        # Reached only if the request succeeded.
        if probe is not None and redis.delete(RECOVERING_KEY):
            logger.info("Video sites answer this server again")
    finally:
        if probe is not None and redis.get(PROBE_KEY) == probe:
            redis.delete(PROBE_KEY)


def _probe_turn(deadline: float) -> str | None:
    """While recovering, the probe token this request holds; otherwise None."""
    token = secrets.token_hex(8)
    while True:
        refused = refusal()
        if refused is not None:
            raise refused
        if not redis.exists(RECOVERING_KEY):
            return None
        if redis.set(PROBE_KEY, token, nx=True, ex=PROBE_TTL):
            return token
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SourceBusy(BUSY_MESSAGE, retry_after=BUSY_RETRY_AFTER)
        time.sleep(min(POLL_INTERVAL, remaining))


def _wait_for_start(deadline: float) -> None:
    """Take the next start time, VIDEO_SITE_INTERVAL after the previous one,
    and sleep until it. Nothing is taken if it would come after deadline."""
    interval = settings.limits.video_site_interval
    if not interval:
        return
    while True:
        try:
            with redis.pipeline() as pipe:
                pipe.watch(NEXT_START_KEY)
                now = time.time()
                start = max(now, float(pipe.get(NEXT_START_KEY) or 0))
                if start - now > deadline - time.monotonic():
                    raise SourceBusy(BUSY_MESSAGE, retry_after=math.ceil(start - now) + 1)
                pipe.multi()
                pipe.set(NEXT_START_KEY, str(start + interval), px=math.ceil((start + interval - now) * 1000))
                pipe.execute()
                break
        except WatchError:
            continue
    time.sleep(max(0.0, start - time.time()))


def state() -> dict:
    """The breaker, for administrators. expires_in is in seconds."""
    with redis.pipeline() as pipe:
        pipe.get(BLOCKED_KEY)
        pipe.ttl(BLOCKED_KEY)
        pipe.exists(RECOVERING_KEY)
        pipe.get(STRIKES_KEY)
        pipe.hgetall(INCIDENTS_KEY)
        reason, ttl, recovering, strikes, incidents = pipe.execute()
    return {
        "refused": {"reason": reason, "expires_in": ttl if ttl > 0 else None} if reason is not None else None,
        "recovering": bool(recovering) and reason is None,
        "incidents_in_a_row": int(strikes or 0),
        "incidents": int(incidents.get("count") or 0),
        "last_incident_at": float(incidents["last_at"]) if incidents.get("last_at") else None,
        "last_reason": incidents.get("last_reason"),
    }


def reset() -> bool:
    """Close the breaker now and forget the incidents in a row. Returns
    whether it was open or recovering."""
    return bool(redis.delete(BLOCKED_KEY, RECOVERING_KEY, STRIKES_KEY, PROBE_KEY))
