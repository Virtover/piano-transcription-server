import logging
import threading
import time

from fastapi import FastAPI
from fastapi import HTTPException
from redis import Redis

from app.api.routes.transcriptions import (
    enable_transcription_submissions,
    router,
)
from app.api.routes.account import router as account_router
from app.api.routes.billing import router as billing_router, server_info
from app.config import settings
from app.google_play import sync_products
from app.worker.tasks import fail_active_jobs, reconcile_billing
from app.billing import (
    Billing,
    backup_interval_seconds,
    backup_retention_seconds,
    prune_backups,
)


logger = logging.getLogger(__name__)


def run_billing_backup() -> None:
    destination = f"{settings.billing_backup_dir}/billing-{int(time.time())}.sqlite3"
    Billing(settings.billing_database_path).backup(destination)
    for path in prune_backups(settings.billing_backup_dir, backup_retention_seconds()):
        logger.info("Removed expired billing backup %s", path.name)


def backup_billing_database() -> None:
    while True:
        try:
            run_billing_backup()
        except Exception:
            logger.exception("Could not back up billing database")
        time.sleep(backup_interval_seconds())

app = FastAPI(
    title="Piano Transcription Server",
    version="0.2.0",
)

app.include_router(router, prefix="/api")
app.include_router(billing_router, prefix="/api")
app.include_router(account_router, prefix="/api")
app.add_api_route("/api/server-info", server_info, methods=["GET"], include_in_schema=False)


@app.on_event("startup")
def sync_billing_catalog() -> None:
    fail_active_jobs("Transcription interrupted because the server restarted")
    reconcile_billing()
    sync_products()
    # Fail at startup instead of in the backup thread when the values are invalid.
    backup_interval_seconds()
    backup_retention_seconds()
    enable_transcription_submissions()
    threading.Thread(target=backup_billing_database, daemon=True).start()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def readiness():
    try:
        Redis.from_url(settings.redis_url).ping()
    except Exception as error:
        raise HTTPException(status_code=503, detail="Redis is unavailable") from error

    return {"status": "ready"}