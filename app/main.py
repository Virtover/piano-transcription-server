from fastapi import FastAPI
from fastapi import HTTPException
from redis import Redis

from app.api.routes.transcriptions import (
    enable_transcription_submissions,
    router,
)
from app.api.routes.billing import router as billing_router, server_info
from app.config import settings
from app.google_play import sync_products
from app.worker.tasks import fail_active_jobs

app = FastAPI(
    title="Piano Transcription Server",
    version="0.2.0",
)

app.include_router(router, prefix="/api")
app.include_router(billing_router, prefix="/api")
app.add_api_route("/api/server-info", server_info, methods=["GET"], include_in_schema=False)


@app.on_event("startup")
def sync_billing_catalog() -> None:
    fail_active_jobs("Transcription interrupted because the server restarted")
    sync_products()
    enable_transcription_submissions()


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