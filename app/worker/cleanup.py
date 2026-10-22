import signal
import sys
import time

from app.config import ensure_valid, settings
from app.outsource import routing
from app.worker.tasks import cleanup_expired_jobs, job_key, redis


def stop(signum, frame) -> None:
    # As a container's main process, Python ignores SIGTERM unless it handles
    # it, so `docker compose stop` would wait and then kill the service.
    sys.exit(0)


# Billing reconciliation is not done here: this service has no access to the
# billing database. The API reconciles at startup.
def main() -> None:
    ensure_valid("cleanup")
    signal.signal(signal.SIGTERM, stop)
    while True:
        cleanup_expired_jobs()
        # Forget active jobs that ended without being counted out, for
        # example after a worker crash, so routing does not overestimate load.
        routing.prune_active(redis, job_key)
        time.sleep(settings.cleanup.interval)


if __name__ == "__main__":
    main()
