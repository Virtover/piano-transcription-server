import time

from app.config import ensure_valid, settings
from app.worker.tasks import cleanup_expired_jobs


# Billing reconciliation is not done here: this service has no access to the
# billing database. The API reconciles at startup.
def main() -> None:
    ensure_valid("cleanup")
    while True:
        cleanup_expired_jobs()
        time.sleep(settings.cleanup.interval)


if __name__ == "__main__":
    main()
