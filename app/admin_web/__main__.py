"""Start the admin web page: `python -m app.admin_web`, the admin service.

With ADMIN_WEB_MODE=disabled it exits at once. Otherwise it serves only when
the settings are complete and its own credentials are what its mode allows:
before serving, it tries a change in Redis and in the billing database that
its users must be refused, and it stops if either is accepted. It never
creates or changes the billing database's tables.
"""

import logging
import sys
from contextlib import closing

from app.billing import Billing
from app.config import ConfigError, ensure_valid, settings
from app.worker import tasks

logger = logging.getLogger("app.admin_web")

# A key no admin user may write, and a change no admin role may make. The
# change matches no row, so it changes nothing even where it is allowed.
REDIS_PROBE_KEY = "admin_web:probe"
DATABASE_PROBE = "UPDATE billing_state SET value = value WHERE false"


def least_privilege_problems(redis=None, billing=None) -> list[str]:
    """What the service's credentials may do but must not."""
    from redis.exceptions import NoPermissionError

    redis = redis or tasks.redis
    billing = billing or tasks.billing
    problems = []
    try:
        # Expires at once, so it is harmless if it is accepted.
        redis.set(REDIS_PROBE_KEY, "1", px=1)
        problems.append("its Redis user may write; REDIS_URL must name the user admin-<mode>, see redis/users.acl")
    except NoPermissionError:
        pass
    with closing(billing._connect()) as connection:
        try:
            connection.execute(DATABASE_PROBE)
            problems.append("its database role may change tables; BILLING_DATABASE_URL must name the role admin-<mode>")
        except Exception as error:
            if "permission denied" not in str(error).lower():
                raise
    return problems


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if settings.admin_web.mode == "disabled":
        logger.info("ADMIN_WEB_MODE=disabled: the admin web page does not start")
        return 0
    try:
        ensure_valid("admin")
    except ConfigError as error:
        for problem in error.problems:
            logger.error("%s", problem)
        return 1
    # The admin roles may not create tables; the API keeps the schema.
    tasks.billing = Billing(create_schema=False)
    problems = least_privilege_problems()
    if problems:
        for problem in problems:
            logger.error("refusing to start: %s", problem)
        return 1

    import uvicorn

    from app.admin_web import PORT
    from app.admin_web.server import create_app

    uvicorn.run(create_app(), host="0.0.0.0", port=PORT, proxy_headers=False, server_header=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
