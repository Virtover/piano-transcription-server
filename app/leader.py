"""Which API replica runs the periodic tasks: billing backups, the abandoned
job watcher, and the voided-purchase sync. With one API, it always does.

The lead is a Redis key with this replica's token that expires unless
renewed, so a replica that crashes hands the lead to another within LEADER_TTL.
"""

import logging
import socket
import threading
import uuid

from redis import Redis
from redis.exceptions import WatchError


logger = logging.getLogger(__name__)

# Under workers:, which the admin web page may read.
LEADER_KEY = "workers:api:leader"
LEADER_TTL = 30
RENEW_INTERVAL = 10


class Leadership:
    def __init__(self, redis: Redis, key: str = LEADER_KEY, ttl: int = LEADER_TTL):
        self.redis = redis
        self.key = key
        self.ttl = ttl
        # The host name tells an operator which replica leads.
        self.token = f"{socket.gethostname()}:{uuid.uuid4().hex[:12]}"
        self._leading = threading.Event()
        self._stop = threading.Event()

    @property
    def leading(self) -> bool:
        return self._leading.is_set()

    def renew(self) -> bool:
        """Take the lead if no replica holds it, or keep it. Returns whether
        this replica leads. Without Redis, it does not."""
        try:
            held = bool(self.redis.set(self.key, self.token, nx=True, ex=self.ttl)) or self._extend()
        except Exception:
            logger.exception("Could not renew the API lead")
            held = False
        if held and not self.leading:
            logger.info("This API replica (%s) now runs the periodic tasks", self.token)
            self._leading.set()
        elif not held and self.leading:
            logger.warning("This API replica (%s) no longer runs the periodic tasks", self.token)
            self._leading.clear()
        return held

    def _extend(self) -> bool:
        while True:
            try:
                with self.redis.pipeline() as pipe:
                    pipe.watch(self.key)
                    if pipe.get(self.key) != self.token:
                        pipe.unwatch()
                        return False
                    pipe.multi()
                    pipe.expire(self.key, self.ttl)
                    pipe.execute()
                    return True
            except WatchError:
                continue

    def wait(self) -> None:
        """Block until this replica leads."""
        self._leading.wait()

    def start(self) -> None:
        """Keep renewing in the background, so a follower takes over when
        the leader stops."""

        def run():
            while not self._stop.wait(RENEW_INTERVAL):
                self.renew()

        threading.Thread(target=run, name="api-leader", daemon=True).start()

    def stop(self) -> None:
        """Hand the lead over at once, as on a clean shutdown."""
        self._stop.set()
        self._leading.clear()
        try:
            while True:
                try:
                    with self.redis.pipeline() as pipe:
                        pipe.watch(self.key)
                        if pipe.get(self.key) != self.token:
                            pipe.unwatch()
                            return
                        pipe.multi()
                        pipe.delete(self.key)
                        pipe.execute()
                        return
                except WatchError:
                    continue
        except Exception:
            logger.exception("Could not hand over the API lead")


def current_leader(redis: Redis) -> str | None:
    """The token of the replica that leads, for status."""
    return redis.get(LEADER_KEY)
