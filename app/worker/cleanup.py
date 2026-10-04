import time

from app.config import settings
from app.worker.tasks import cleanup_expired_jobs, reconcile_billing


while True:
    reconcile_billing()
    cleanup_expired_jobs()
    time.sleep(settings.cleanup_interval_seconds)