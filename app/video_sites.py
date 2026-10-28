"""Keeping this server in good standing with the video sites it reads links
from. Every yt-dlp call that contacts a site, in the API or a worker, goes
through `access`, and everything here is kept per site in Redis, so the API
and every worker share it:

* A rate limit or bot check opens the site's breaker. Links to that site,
  and their cost, are refused with 503 until it closes; other sites and
  uploads are unaffected. Each incident in a row doubles the cooldown, from
  VIDEO_SITE_COOLDOWN up to VIDEO_SITE_MAX_COOLDOWN.
* TIMEOUT_LIMIT timeouts within TIMEOUT_WINDOW open it for a shorter time,
  from VIDEO_SITE_TIMEOUT_COOLDOWN, doubling up to VIDEO_SITE_COOLDOWN. A
  single timeout does not.
* When a cooldown ends, one request at a time is let through until one
  succeeds, so waiting users do not all reach the site at once.
* Requests that read a link start at least VIDEO_SITE_INTERVAL apart, and
  at most VIDEO_SITE_MAX_DOWNLOADS downloads from a site run at once.

`limits links off` remains the switch for every site. Failed jobs are never
retried on their own."""

import json
import logging
import math
import re
import secrets
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

from redis import Redis
from redis.exceptions import WatchError

from app.config import settings
from app.errors import (
    SourceBusy,
    SourceError,
    SourceTemporarilyUnavailable,
    SourceTimedOut,
    SourceUnsupported,
    SourceVideoUnavailable,
    public_message,
    yt_dlp_reason,
)
from app.transcription.cancellation import TranscriptionCancelled


logger = logging.getLogger(__name__)
redis = Redis.from_url(settings.service.redis_url, decode_responses=True, **settings.service.redis_options())

CancellationCallback = Callable[[], bool]

# Keys are PREFIX + site + ":" + one of the names below.
PREFIX = "video_sites:"
# The breaker: present while open, as {"kind", "reason"}; expires with the cooldown.
BLOCKED = "blocked"
LIMITED = "limited"
TIMED_OUT = "timeouts"
# Incidents of each kind in a row, which set the next cooldown.
STRIKES = {LIMITED: "strikes", TIMED_OUT: "timeout_strikes"}
# Present from an incident until a request succeeds after the cooldown.
RECOVERING = "recovering"
# The one request let through while recovering.
PROBE = "probe"
PROBE_TTL = 5 * 60
# When the next paced request may start, as a Unix time.
NEXT_START = "next_start"
# Downloads running now: token -> when its lease expires. A worker renews its
# lease while it downloads, so one that dies frees its place within the lease.
DOWNLOADS = "downloads"
DOWNLOAD_LEASE = 60
# Recent timeouts, counted within TIMEOUT_WINDOW.
TIMEOUTS_SEEN = "timeouts"
TIMEOUT_LIMIT = 3
TIMEOUT_WINDOW = 5 * 60
TIMEOUT_RETRY_AFTER = 30
# Counts and the last incident, for `python -m app.admin status`.
INCIDENTS = "incidents"
POLL_INTERVAL = 0.5
BUSY_RETRY_AFTER = 5

BUSY_MESSAGE = "Too many videos are being checked; try again in a moment"
UNSUPPORTED_MESSAGE = "This link cannot be transcribed; upload the file instead"

# Hosts that are another name of the same site, which shares its breaker.
SITE_ALIASES = {
    "youtu.be": "youtube.com",
    "youtube-nocookie.com": "youtube.com",
    "instagr.am": "instagram.com",
    "fb.watch": "facebook.com",
    "x.com": "twitter.com",
    "redd.it": "reddit.com",
}

# A rate limit, on any site.
LIMITED_ANYWHERE = re.compile(r"HTTP Error 429|Too Many Requests|rate[- ]?limit", re.IGNORECASE)
# What only these sites say when they limit this server. Elsewhere, "try
# again later" can be about one video. YouTube's rate limit reads "Video
# unavailable. This content isn't available, try again later", so these are
# checked before UNAVAILABLE.
LIMITED_BY_SITE = {
    "youtube.com": re.compile(
        r"HTTP Error 402|confirm you.?re not a bot|content isn.?t available, try again later|unusual traffic",
        re.IGNORECASE,
    ),
}
# The video itself cannot be read; asking again would not help.
UNAVAILABLE = re.compile(
    r"Video unavailable|Private video|video is private|has been removed|members[- ]only"
    r"|confirm your age|age[- ]restricted|in your country"
    r"|copyright|live event will begin|Premieres in|HTTP Error 404|does not exist",
    re.IGNORECASE,
)
UNSUPPORTED = re.compile(r"Unsupported URL|is not a valid URL", re.IGNORECASE)
# yt-dlp's socket timeout, as in "The read operation timed out".
TIMEOUT = re.compile(r"timed out", re.IGNORECASE)

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


def site_of(url: str) -> str:
    """The site a link belongs to, whose breaker it uses."""
    return canonical_site(urlsplit(url).hostname or "")


def canonical_site(host: str) -> str:
    """The SOURCE_URL_ALLOWED_HOSTS entry a host belongs to, under one name
    per site, or its last two labels if no entry matches."""
    host = host.rstrip(".").lower()
    allowed = [entry.strip().lower().lstrip(".") for entry in settings.limits.source_url_allowed_hosts]
    matches = [entry for entry in allowed if entry and (host == entry or host.endswith("." + entry))]
    site = max(matches, key=len) if matches else ".".join(host.split(".")[-2:])
    return SITE_ALIASES.get(site, site)


def refused_message(site: str) -> str:
    return f"Links to {site} cannot be read at the moment; try again later, or upload the file instead"


def not_answering_message(site: str) -> str:
    return f"{site} is not answering at the moment; try again later, or upload the file instead"


def _key(site: str, name: str) -> str:
    return f"{PREFIX}{site}:{name}"


def failure(site: str, error: subprocess.CalledProcessError | subprocess.TimeoutExpired) -> SourceError:
    """The error to raise for a failed yt-dlp call to site. A rate limit or
    bot check opens the site's breaker, and so do repeated timeouts."""
    if isinstance(error, subprocess.TimeoutExpired):
        return _timed_out(site)
    stderr = error.stderr or ""
    limited_here = LIMITED_BY_SITE.get(site)
    if LIMITED_ANYWHERE.search(stderr) or (limited_here and limited_here.search(stderr)):
        cooldown = open_breaker(site, LIMITED, yt_dlp_reason(stderr) or "rate limited")
        return SourceTemporarilyUnavailable(refused_message(site), retry_after=cooldown)
    if UNAVAILABLE.search(stderr):
        return SourceVideoUnavailable(public_message(error))
    if UNSUPPORTED.search(stderr):
        return SourceUnsupported(UNSUPPORTED_MESSAGE)
    if TIMEOUT.search(stderr):
        return _timed_out(site)
    return SourceError(public_message(error))


def _timed_out(site: str) -> SourceTimedOut:
    key = _key(site, TIMEOUTS_SEEN)
    count = redis.incr(key)
    if count == 1:
        redis.expire(key, TIMEOUT_WINDOW)
    if count < TIMEOUT_LIMIT:
        return SourceTimedOut(f"{site} did not answer in time; try again, or upload the file instead", TIMEOUT_RETRY_AFTER)
    redis.delete(key)
    cooldown = open_breaker(site, TIMED_OUT, f"{count} timeouts within {TIMEOUT_WINDOW // 60} minutes")
    return SourceTimedOut(not_answering_message(site), retry_after=cooldown)


def _cooldowns(kind: str) -> tuple[float, float]:
    """The first and the longest cooldown of an incident of this kind."""
    limits = settings.limits
    if kind == LIMITED:
        return limits.video_site_cooldown, max(limits.video_site_cooldown, limits.video_site_max_cooldown)
    return limits.video_site_timeout_cooldown, max(limits.video_site_timeout_cooldown, limits.video_site_cooldown)


def open_breaker(site: str, kind: str, reason: str) -> int:
    """Stop contacting site for a cooldown; returns its seconds. An open
    breaker is left as it is, so the failures of one incident count once,
    except that a rate limit replaces a shorter breaker for timeouts."""
    base, longest = _cooldowns(kind)
    blocked = _key(site, BLOCKED)
    try:
        with redis.pipeline() as pipe:
            pipe.watch(blocked)
            current = pipe.get(blocked)
            if current is not None and not (kind == LIMITED and json.loads(current)["kind"] == TIMED_OUT):
                return max(pipe.ttl(blocked), 1)
            pipe.multi()
            pipe.set(blocked, json.dumps({"kind": kind, "reason": reason}), ex=math.ceil(base))
            pipe.execute()
    except WatchError:
        # Another request opened it at the same moment.
        return max(redis.ttl(blocked), 1)
    strikes = redis.incr(_key(site, STRIKES[kind]))
    cooldown = math.ceil(min(base * 2 ** min(strikes - 1, 20), longest))
    # Incidents count as in a row until a quiet longest cooldown has passed.
    memory = cooldown + math.ceil(longest)
    with redis.pipeline() as pipe:
        pipe.set(blocked, json.dumps({"kind": kind, "reason": reason}), ex=cooldown)
        pipe.set(_key(site, RECOVERING), "1", ex=memory)
        pipe.expire(_key(site, STRIKES[kind]), memory)
        pipe.hincrby(_key(site, INCIDENTS), "count", 1)
        pipe.hset(_key(site, INCIDENTS), mapping={"last_at": str(time.time()), "last_reason": reason})
        pipe.execute()
    logger.warning(
        "%s %s (%s); its links are refused for %d s (incident %d in a row)",
        site,
        "refused this server" if kind == LIMITED else "is not answering",
        reason,
        cooldown,
        strikes,
    )
    return cooldown


def refusal(site: str) -> SourceTemporarilyUnavailable | None:
    """The error for a link to site while its breaker is open."""
    with redis.pipeline() as pipe:
        pipe.get(_key(site, BLOCKED))
        pipe.ttl(_key(site, BLOCKED))
        value, ttl = pipe.execute()
    if value is None:
        return None
    kind = json.loads(value)["kind"]
    return SourceTemporarilyUnavailable(
        refused_message(site) if kind == LIMITED else not_answering_message(site),
        retry_after=ttl if ttl > 0 else math.ceil(_cooldowns(kind)[0]),
    )


@contextmanager
def access(
    site: str,
    wait: float,
    paced: bool = True,
    download: bool = False,
    cancelled: CancellationCallback | None = None,
) -> Iterator[None]:
    """Permission to contact site once, waiting up to `wait` seconds for a
    turn. Raises SourceTemporarilyUnavailable while the site's breaker is
    open, SourceBusy when no turn comes in time, and TranscriptionCancelled
    if cancelled() while waiting. A download takes one of the site's
    download places; paced=False is for downloads from saved info, which
    fetch the audio, not the site's pages."""
    deadline = time.monotonic() + wait
    probe = _probe_turn(site, deadline, cancelled)
    slot = None
    try:
        if download:
            slot = DownloadSlot(site)
            slot.take(deadline, cancelled)
        if paced:
            _wait_for_start(site, deadline, cancelled)
        yield
        # Reached only if the request succeeded.
        redis.delete(_key(site, TIMEOUTS_SEEN))
        if probe is not None and redis.delete(_key(site, RECOVERING)):
            logger.info("%s answers this server again", site)
    finally:
        if slot is not None:
            slot.release()
        if probe is not None and redis.get(_key(site, PROBE)) == probe:
            redis.delete(_key(site, PROBE))


def _pause(deadline: float, cancelled: CancellationCallback | None, retry_after: int = BUSY_RETRY_AFTER) -> None:
    """Wait a moment before trying again, or raise SourceBusy once deadline has passed."""
    if cancelled is not None and cancelled():
        raise TranscriptionCancelled()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SourceBusy(BUSY_MESSAGE, retry_after=retry_after)
    time.sleep(min(POLL_INTERVAL, remaining))


def _probe_turn(site: str, deadline: float, cancelled: CancellationCallback | None) -> str | None:
    """While the site is recovering, the probe token this request holds; otherwise None."""
    token = secrets.token_hex(8)
    while True:
        refused = refusal(site)
        if refused is not None:
            raise refused
        if not redis.exists(_key(site, RECOVERING)):
            return None
        if redis.set(_key(site, PROBE), token, nx=True, ex=PROBE_TTL):
            return token
        _pause(deadline, cancelled)


def _wait_for_start(site: str, deadline: float, cancelled: CancellationCallback | None) -> None:
    """Take the site's next start time, VIDEO_SITE_INTERVAL after the
    previous one, and wait until it. Nothing is taken if it would come
    after deadline."""
    interval = settings.limits.video_site_interval
    if not interval:
        return
    key = _key(site, NEXT_START)
    while True:
        try:
            with redis.pipeline() as pipe:
                pipe.watch(key)
                now = time.time()
                previous = pipe.get(key)
                start = max(now, float(previous or 0))
                if previous is not None and start - now > deadline - time.monotonic():
                    raise SourceBusy(BUSY_MESSAGE, retry_after=math.ceil(start - now) + 1)
                pipe.multi()
                pipe.set(key, str(start + interval), px=math.ceil((start + interval - now) * 1000))
                pipe.execute()
                break
        except WatchError:
            continue
    while (remaining := start - time.time()) > 0:
        if cancelled is not None and cancelled():
            raise TranscriptionCancelled()
        time.sleep(min(POLL_INTERVAL, remaining))


class DownloadSlot:
    """One of a site's VIDEO_SITE_MAX_DOWNLOADS places, across processes."""

    def __init__(self, site: str):
        self.key = _key(site, DOWNLOADS)
        self.token = secrets.token_hex(8)
        self._stop = threading.Event()

    def take(self, deadline: float, cancelled: CancellationCallback | None) -> None:
        while not self._try_take():
            _pause(deadline, cancelled)
        threading.Thread(target=self._renew, name="video-site-download", daemon=True).start()

    def _try_take(self) -> bool:
        while True:
            try:
                with redis.pipeline() as pipe:
                    pipe.watch(self.key)
                    now = time.time()
                    if (
                        settings.limits.video_site_max_downloads is not None
                        and pipe.zcount(self.key, now, "+inf") >= settings.limits.video_site_max_downloads
                    ):
                        return False
                    pipe.multi()
                    pipe.zremrangebyscore(self.key, "-inf", now)
                    pipe.zadd(self.key, {self.token: now + DOWNLOAD_LEASE})
                    pipe.expire(self.key, DOWNLOAD_LEASE * 2)
                    pipe.execute()
                    return True
            except WatchError:
                continue

    def _renew(self) -> None:
        while not self._stop.wait(DOWNLOAD_LEASE / 3):
            try:
                redis.zadd(self.key, {self.token: time.time() + DOWNLOAD_LEASE}, xx=True)
                redis.expire(self.key, DOWNLOAD_LEASE * 2)
            except Exception:
                logger.exception("Could not renew a video site download place")

    def release(self) -> None:
        self._stop.set()
        redis.zrem(self.key, self.token)


def sites() -> list[str]:
    """The sites anything is kept for."""
    return sorted({key[len(PREFIX):].rsplit(":", 1)[0] for key in redis.scan_iter(match=PREFIX + "*")})


def state(site: str) -> dict:
    """A site's breaker, for administrators. expires_in is in seconds."""
    with redis.pipeline() as pipe:
        pipe.get(_key(site, BLOCKED))
        pipe.ttl(_key(site, BLOCKED))
        pipe.exists(_key(site, RECOVERING))
        pipe.get(_key(site, STRIKES[LIMITED]))
        pipe.get(_key(site, STRIKES[TIMED_OUT]))
        pipe.hgetall(_key(site, INCIDENTS))
        pipe.zcount(_key(site, DOWNLOADS), time.time(), "+inf")
        blocked, ttl, recovering, strikes, timeout_strikes, incidents, downloads = pipe.execute()
    refused = None
    if blocked is not None:
        refused = {**json.loads(blocked), "expires_in": ttl if ttl > 0 else None}
    return {
        "site": site,
        "refused": refused,
        "recovering": bool(recovering) and refused is None,
        "incidents_in_a_row": int(strikes or 0),
        "timeout_incidents_in_a_row": int(timeout_strikes or 0),
        "incidents": int(incidents.get("count") or 0),
        "last_incident_at": float(incidents["last_at"]) if incidents.get("last_at") else None,
        "last_reason": incidents.get("last_reason"),
        "downloads": downloads,
    }


def states() -> list[dict]:
    return [state(site) for site in sites()]


def reset(site: str | None = None) -> list[str]:
    """Close the breaker of site, or of every site, now, and forget the
    incidents in a row. Returns the sites that were refused or recovering."""
    changed = []
    for name in [site] if site else sites():
        if redis.exists(_key(name, BLOCKED), _key(name, RECOVERING)):
            changed.append(name)
        redis.delete(*(_key(name, part) for part in (BLOCKED, RECOVERING, PROBE, TIMEOUTS_SEEN, *STRIKES.values())))
    return changed
