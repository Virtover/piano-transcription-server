"""Start the admin web page: `python -m app.admin_web`, the admin service.

With ADMIN_WEB_MODE=disabled it exits at once. Otherwise it serves only when
the settings are complete and its own credentials are what its mode allows:
before serving, it tries changes in Redis and in the billing database that
its mode's users must be refused, and it stops if any is accepted. In
read-only mode that includes changes read-write may make, so read-write
credentials cannot serve a read-only page. It never creates or changes the
billing database's tables.
"""

import logging
import sys
from contextlib import closing

from app.billing import Billing
from app.config import ConfigError, ensure_valid, settings
from app.worker import tasks

logger = logging.getLogger("app.admin_web")

# For each mode, keys its Redis user must not write and changes its database
# role must not make. A key expires at once and a change matches no row, so
# each is harmless even where it is allowed.
REDIS_PROBES = {
    "read-only": ["workers:admin_web_probe", "controls:admin_web_probe"],
    "read-write": ["workers:admin_web_probe"],
}
DATABASE_PROBES = {
    "read-only": [
        "UPDATE billing_state SET value = value WHERE false",
        "UPDATE users SET minutes = minutes WHERE false",
    ],
    "read-write": [
        "UPDATE billing_state SET value = value WHERE false",
        "DELETE FROM users WHERE false",
    ],
}


def least_privilege_problems(mode: str, redis=None, billing=None) -> list[str]:
    """What the service's credentials may do but must not in mode."""
    from redis.exceptions import NoPermissionError

    redis = redis or tasks.redis
    billing = billing or tasks.billing
    problems = []
    for key in REDIS_PROBES[mode]:
        try:
            redis.set(key, "1", px=1)
            problems.append(f"its Redis user may write {key}; REDIS_URL must name the user admin-{mode}, see redis/users.acl")
        except NoPermissionError:
            pass
    for statement in DATABASE_PROBES[mode]:
        # A refused statement ends its transaction, so each gets its own.
        with closing(billing._connect()) as connection:
            try:
                connection.execute(statement)
                problems.append(f"its database role may run {statement!r}; BILLING_DATABASE_URL must name the role admin-{mode}")
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
    problems = least_privilege_problems(settings.admin_web.mode)
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
