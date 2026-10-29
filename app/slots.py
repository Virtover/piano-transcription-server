"""Places limited across every process and machine that shares Redis, such
as at most four video checks at once whichever API replica runs them.

The places are a sorted set of holders by lease expiry. A holder renews its
lease while it works, so a process that crashes frees its place within one
lease instead of keeping it.
"""

import logging
import secrets
import threading
import time

from redis import Redis
from redis.exceptions import WatchError


logger = logging.getLogger(__name__)

LEASE = 60
# Video checks of every API replica; under workers:, which the admin web
# page may read.
VIDEO_CHECKS_KEY = "workers:api:video_checks"


class LeasedSlot:
    """One of limit places under key; limit None means unlimited."""

    def __init__(self, redis: Redis, key: str, limit: int | None, lease: int = LEASE):
        self.redis = redis
        self.key = key
        self.limit = limit
        self.lease = lease
        self.token = secrets.token_hex(8)
        self._stop = threading.Event()

    def try_take(self) -> bool:
        """Take a place if one is free, and keep it until release."""
        while True:
            try:
                with self.redis.pipeline() as pipe:
                    pipe.watch(self.key)
                    now = time.time()
                    if self.limit is not None and pipe.zcount(self.key, now, "+inf") >= self.limit:
                        return False
                    pipe.multi()
                    pipe.zremrangebyscore(self.key, "-inf", now)
                    pipe.zadd(self.key, {self.token: now + self.lease})
                    pipe.expire(self.key, self.lease * 2)
                    pipe.execute()
                    break
            except WatchError:
                continue
        threading.Thread(target=self._renew, name=f"slot-{self.key}", daemon=True).start()
        return True

    def take(self, timeout: float, poll: float = 0.25) -> bool:
        """Wait up to timeout seconds for a place."""
        deadline = time.monotonic() + timeout
        while not self.try_take():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(poll, remaining))
        return True

    def _renew(self) -> None:
        while not self._stop.wait(self.lease / 3):
            try:
                self.redis.zadd(self.key, {self.token: time.time() + self.lease}, xx=True)
                self.redis.expire(self.key, self.lease * 2)
            except Exception:
                logger.exception("Could not renew a place in %s", self.key)

    def release(self) -> None:
        self._stop.set()
        self.redis.zrem(self.key, self.token)


def in_use(redis: Redis, key: str) -> int:
    """Places held now, for status."""
    return redis.zcount(key, time.time(), "+inf")
