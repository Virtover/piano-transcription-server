import hmac
import logging
import threading
import time

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse
from redis import Redis

from app import error_report, maintenance
from app import metrics as app_metrics
from app.auth import server_password_matches, server_password_required
from app.api.routes import transcriptions as transcription_routes
from app.api.routes.transcriptions import (
    enable_transcription_submissions,
    router,
)
from app.api.routes.account import router as account_router
from app.api.routes.billing import router as billing_router, server_info
from app.config import ensure_valid, settings
from app import google_play
from app.google_play import sync_products
from app.worker.tasks import (
    ABANDONED_JOB_CHECK_INTERVAL,
    cleanup_stored_jobs,
    fail_abandoned_jobs,
    reconcile_billing,
    recover_active_jobs,
)
from app import backups
from app.backups import run_backup
from app.billing import Billing
from app.leader import Leadership


logger = logging.getLogger(__name__)
# Set at startup. With several API replicas, only the leader runs the
# periodic tasks below; the others wait to take over.
leadership: Leadership | None = None


def wait_for_lead(leader: Leadership | None) -> None:
    if leader is not None:
        leader.wait()


def run_billing_backup() -> None:
    run_backup(Billing())


def backup_billing_database(leader: Leadership | None = None) -> None:
    if settings.billing.backups == "provider":
        return
    while True:
        wait_for_lead(leader)
        try:
            run_billing_backup()
        except Exception:
            logger.exception("Could not back up billing database")
        time.sleep(settings.billing.backup_interval)


def monitor_backup_health(leader: Leadership | None = None) -> None:
    """Warn when configured off-site backups fail or stop advancing."""
    if not settings.offsite.enabled or settings.billing.backups == "provider":
        return
    interval = settings.billing.backup_interval
    while True:
        time.sleep(interval)
        wait_for_lead(leader)
        try:
            state = backups.offsite_status(Billing())
            now = int(time.time())
            if state is None:
                continue
            if state["error"]:
                logger.error("Off-site backup copying is failing: %s", state["error"])
            elif state["succeeded_at"] is None or now - state["succeeded_at"] > interval * 2:
                logger.error("Off-site backup copying has not succeeded for %.0f seconds", now - (state["succeeded_at"] or 0))
        except Exception:
            logger.exception("Could not monitor off-site backup health")


def watch_abandoned_jobs(leader: Leadership | None = None) -> None:
    """Fail and refund processing jobs whose worker stopped, for example
    after a crash, while the API keeps running."""
    while True:
        time.sleep(ABANDONED_JOB_CHECK_INTERVAL)
        wait_for_lead(leader)
        try:
            fail_abandoned_jobs()
        except Exception:
            logger.exception("Could not check for abandoned jobs")


def clean_stored_jobs(leader: Leadership | None = None) -> None:
    """With JOB_STORAGE=s3, delete expired jobs' files from the bucket. The
    cleanup service cannot: it has no storage keys."""
    while True:
        wait_for_lead(leader)
        try:
            cleanup_stored_jobs()
        except Exception:
            logger.exception("Could not delete expired jobs' stored files")
        time.sleep(settings.cleanup.interval)


# The first wait after Google could not be reached; it doubles up to the
# usual interval.
VOIDED_PURCHASES_RETRY = 60.0


def sync_voided_purchases(leader: Leadership | None = None) -> None:
    """Take back refunded minutes, at startup and then periodically. Without
    a network, as at startup before the host's is up, it tries again soon."""
    billing = Billing()
    retry = VOIDED_PURCHASES_RETRY
    while True:
        wait_for_lead(leader)
        delay = settings.google_play.voided_purchases_interval
        try:
            google_play.sync_voided_purchases(billing)
            retry = VOIDED_PURCHASES_RETRY
        except Exception as error:
            if google_play.is_network_error(error):
                delay = min(retry, delay)
                retry *= 2
                logger.warning(
                    "Could not reach Google Play to read voided purchases (%s); trying again in %.0f seconds",
                    error,
                    delay,
                )
            else:
                logger.exception("Could not read voided Google Play purchases")
        time.sleep(delay)


def voided_purchases_enabled() -> bool:
    return settings.billing.provider == "google_play" and google_play.is_configured()


app = FastAPI(
    title="Piano Transcription Server",
    version="0.2.0",
)

app.include_router(router, prefix="/api")
app.include_router(billing_router, prefix="/api")
app.include_router(account_router, prefix="/api")
app.add_api_route("/api/server-info", server_info, methods=["GET"], include_in_schema=False)


@app.middleware("http")
async def maintenance_headers(request: Request, call_next):
    """Tells every client about a planned restart or shutdown, whatever it
    requested."""
    response = await call_next(request)
    try:
        notice = await run_in_threadpool(maintenance.current, transcription_routes.redis)
    except Exception:
        # /health must keep answering while Redis is down.
        notice = None
    if notice is not None:
        response.headers["X-Server-Maintenance"] = notice.action
        response.headers["X-Server-Maintenance-At"] = str(int(notice.at))
    return response


# Runs before every route and the maintenance headers: without the password,
# nothing else is reached. Only the security headers wrap it.
@app.middleware("http")
async def server_password(request: Request, call_next):
    if server_password_required(request.url.path) and not server_password_matches(request.headers):
        return JSONResponse(
            status_code=401,
            content={"detail": "Server password required"},
            headers={"WWW-Authenticate": 'Basic realm="Piano Transcription Server", charset="UTF-8"'},
        )
    return await call_next(request)


@app.middleware("http")
async def count_requests(request: Request, call_next):
    """For piano_api_requests_in_flight."""
    with app_metrics.in_flight:
        return await call_next(request)


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


# Added after the password check, so its refusals get the headers too.
@app.middleware("http")
async def security_headers(request: Request, call_next):
    """No browser may guess a response's type or frame it. Answers under
    /api hold a user's jobs, balance, and MIDI files, so no cache keeps them."""
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.on_event("startup")
def sync_billing_catalog() -> None:
    ensure_valid("api")
    # Before anything reads or changes billing, and before the next backup.
    problem = backups.replaced_database_problem(Billing(), settings.billing.backup_dir)
    if problem:
        raise RuntimeError(problem)
    error_report.install("api")
    # The restart or shutdown a past notice announced has happened.
    maintenance.clear_past(transcription_routes.redis)
    global leadership
    leadership = Leadership(transcription_routes.redis)
    # Taking a lead no one held means no other API replica is running: the
    # server started or restarted. Joining running replicas recovers nothing,
    # because their jobs are not interrupted; the leader's abandoned job
    # watcher still fails those whose worker stopped.
    if leadership.renew():
        recover_active_jobs("Transcription interrupted because the server restarted")
        reconcile_billing()
    else:
        logger.info("Another API replica runs the periodic tasks; this one takes over if it stops")
    leadership.start()
    sync_products()
    enable_transcription_submissions()
    threading.Thread(target=backup_billing_database, args=(leadership,), daemon=True).start()
    threading.Thread(target=monitor_backup_health, args=(leadership,), daemon=True).start()
    threading.Thread(target=watch_abandoned_jobs, args=(leadership,), daemon=True).start()
    if settings.jobs.storage == "s3":
        threading.Thread(target=clean_stored_jobs, args=(leadership,), daemon=True).start()
    if voided_purchases_enabled():
        threading.Thread(target=sync_voided_purchases, args=(leadership,), daemon=True).start()


@app.on_event("shutdown")
def hand_over_lead() -> None:
    if leadership is not None:
        leadership.stop()


@app.get("/metrics", include_in_schema=False)
def metrics(request: Request):
    """Queue depth and wait for autoscaling, with secrets/metrics_token as a
    Bearer token. Without the secret it does not exist."""
    token = settings.secrets.metrics_token
    if token is None:
        raise HTTPException(status_code=404, detail="Not Found")
    expected = f"Bearer {token.get_secret_value()}"
    if not hmac.compare_digest(request.headers.get("authorization", "").encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Metrics token required", headers={"WWW-Authenticate": "Bearer"})
    body = app_metrics.render(app_metrics.collect(transcription_routes.redis))
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def readiness():
    try:
        Redis.from_url(settings.service.redis_url, **settings.service.redis_options()).ping()
    except Exception as error:
        raise HTTPException(status_code=503, detail="Redis is unavailable") from error
    try:
        Billing().check()
    except Exception as error:
        raise HTTPException(status_code=503, detail="The billing database is unavailable") from error

    return {"status": "ready"}