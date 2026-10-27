import hashlib
import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, HttpUrl
from redis import Redis

from app.billing import Billing, transcription_cost
from app.config import settings
from app import controls, google_play, maintenance, video_info, video_sites
from app.errors import SourceBusy, SourceError, SourceTimedOut, SourceUnsupported, SourceVideoUnavailable
from app.maintenance import NoticeResponse
from app.auth import AuthenticatedUser, current_user, google_oauth_client_ids
from app.url_safety import YT_DLP_SAFETY_OPTIONS, ensure_safe_source_url


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["billing"])
redis = Redis.from_url(settings.service.redis_url, decode_responses=True)
billing = Billing()
# Each video check runs yt-dlp for up to a minute. This bounds the CPU and the
# requests this server's IP sends to video sites, whoever calls.
METADATA_SLOTS = 4
METADATA_WAIT = 10
metadata_slots = threading.BoundedSemaphore(METADATA_SLOTS)
# Recent video checks, by URL. A video's info is a few KB.
VIDEO_CHECK_KEY = "video_check:"
VIDEO_CHECK_TTL = 300
# Failures remembered for a URL, in seconds, so asking again does not reach
# the site: a video the site said cannot be read, and a site that timed out.
FAILED_CHECK_TTLS = {SourceVideoUnavailable: 60, SourceUnsupported: 60, SourceTimedOut: 30}
FAILED_CHECK_KINDS = {kind.__name__: kind for kind in FAILED_CHECK_TTLS}
# Held while a URL is checked, so the same URL is checked once at a time.
CHECKING_SUFFIX = ":checking"
CHECKING_TTL = 90


class CostResponse(BaseModel):
    duration_seconds: float
    cost_minutes: int


class GooglePurchaseRequest(BaseModel):
    product_id: str
    purchase_token: str = Field(min_length=1)


class VerifyPurchaseResponse(BaseModel):
    user_id: str
    credited_minutes: int
    minutes: int
    consumed: bool


class Offer(BaseModel):
    product_id: str
    transcription_minutes: int


class ServerInfoResponse(BaseModel):
    billing_provider: str
    offers: list[Offer]
    cleanup_interval_seconds: int
    # How long a finished job's status and MIDI file can be retrieved.
    result_retention_seconds: int
    max_video_length_minutes: int | None = None
    max_upload_size_mb: float | None
    # only present when billing is enabled
    free_minutes: int | None = None
    free_minutes_period: str | None = None
    google_oauth_client_ids: list[str] | None = None
    # A planned server restart or shutdown, if one is pending.
    maintenance: NoticeResponse | None = None


class BalanceResponse(BaseModel):
    user_id: str
    minutes: int
    free_minutes_seconds_until_next_grant: int | None = None
    free_minutes_next_grant_at: int | None = None


@dataclass
class CheckedVideo:
    duration: float
    info: dict[str, Any]
    checked_at: float


def check_video(source_url: HttpUrl) -> CheckedVideo:
    """Read a video's info with yt-dlp, and check its length. A check is
    reused for VIDEO_CHECK_TTL, so asking the cost and then submitting the
    video asks the site once. Requests for a URL being checked wait for that
    check instead of starting another."""
    try:
        # An unsafe URL is refused as unsupported.
        safe_url = ensure_safe_source_url(str(source_url))
        key = VIDEO_CHECK_KEY + hashlib.sha256(safe_url.encode()).hexdigest()
        checked = cached_check(key)
        if checked is None:
            checked = run_check(key, safe_url)
    except SourceError as error:
        raise source_refusal(error) from error
    return CheckedVideo(ensure_allowed_length(float(checked["info"]["duration"])), checked["info"], checked["checked_at"])


def cached_check(key: str, wait: float = 0) -> dict[str, Any] | None:
    """A recent check of the URL, waiting up to wait seconds for one to
    appear. Raises the error of a recent check the site refused."""
    deadline = time.monotonic() + wait
    while True:
        cached = redis.get(key)
        if cached:
            checked = json.loads(cached)
            if "error" in checked:
                failed = checked["error"]
                raise FAILED_CHECK_KINDS[failed["kind"]](failed["message"], failed.get("retry_after"))
            return checked
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(0.25, remaining))


def run_check(key: str, safe_url: str) -> dict[str, Any]:
    lock = key + CHECKING_SUFFIX
    if not redis.set(lock, "1", nx=True, ex=CHECKING_TTL):
        checked = cached_check(key, METADATA_WAIT)
        if checked is None:
            raise SourceBusy(video_sites.BUSY_MESSAGE, retry_after=5)
        return checked
    try:
        info = read_video_info(safe_url)
    except SourceError as error:
        ttl = FAILED_CHECK_TTLS.get(type(error))
        if ttl is not None:
            failed = {"kind": type(error).__name__, "message": str(error), "retry_after": error.retry_after}
            redis.set(key, json.dumps({"error": failed}), ex=ttl)
        raise
    finally:
        redis.delete(lock)
    checked = {"info": info, "checked_at": time.time()}
    redis.set(key, json.dumps(checked), ex=VIDEO_CHECK_TTL)
    return checked


def read_video_info(safe_url: str) -> dict[str, Any]:
    site = video_sites.site_of(safe_url)
    if not metadata_slots.acquire(timeout=METADATA_WAIT):
        raise SourceBusy(video_sites.BUSY_MESSAGE, retry_after=5)
    try:
        with video_sites.access(site, METADATA_WAIT):
            try:
                result = subprocess.run(
                    [
                        "yt-dlp",
                        *YT_DLP_SAFETY_OPTIONS,
                        *video_sites.YT_DLP_SITE_OPTIONS,
                        "--dump-single-json",
                        "--no-download",
                        "--",
                        safe_url,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                raise video_sites.failure(site, error) from error
            except OSError as error:
                raise SourceError("Unable to read video metadata") from error
        info = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise SourceError("Unable to read video metadata") from error
    finally:
        metadata_slots.release()
    duration = info.get("duration") if isinstance(info, dict) else None
    if not isinstance(duration, (int, float)) or duration <= 0:
        raise SourceError("Video duration is unavailable")
    return video_info.slim(info)


def error_detail(error: SourceError) -> dict[str, str]:
    return {"message": str(error), "error_code": error.code}


def source_refusal(error: SourceError) -> HTTPException:
    return HTTPException(
        status_code=error.status,
        detail=error_detail(error),
        headers={"Retry-After": str(error.retry_after)} if error.retry_after else None,
    )


def video_duration(source_url: HttpUrl) -> float:
    return check_video(source_url).duration


def ensure_accepting(user_id: str, link: bool, submission: bool = True, source_url: str | None = None) -> None:
    """Refuse a request that an administrator's control stops, see
    app.controls, or a link to a site whose breaker is open, see app.video_sites."""
    refused = controls.refusal(redis, user_id, link, submission)
    if refused is not None:
        status, message, retry_after = refused
        raise HTTPException(
            status_code=status,
            detail=message,
            headers={"Retry-After": str(retry_after)} if retry_after else None,
        )
    # Refused before anything is checked or held.
    if source_url is not None and (limited := video_sites.refusal(video_sites.site_of(source_url))) is not None:
        raise source_refusal(limited)


def ensure_allowed_length(duration: float) -> float:
    if settings.limits.max_video_length_minutes and duration > settings.limits.max_video_length_minutes * 60:
        raise HTTPException(status_code=413, detail="Video exceeds the configured maximum length")
    return duration


@router.get(
    "/server-info",
    response_model=ServerInfoResponse,
    response_model_exclude_unset=True,
)
def server_info() -> ServerInfoResponse:
    product_ids = set(settings.billing.products)
    if settings.billing.provider == "google_play":
        product_ids &= google_play.synced_products or set()
    info = ServerInfoResponse(
        billing_provider=settings.billing.provider,
        offers=[
            Offer(product_id=product_id, transcription_minutes=minutes)
            for product_id, minutes in settings.billing.products.items()
            if product_id in product_ids
        ],
        cleanup_interval_seconds=int(settings.cleanup.interval),
        result_retention_seconds=int(settings.limits.result_retention),
        max_video_length_minutes=settings.limits.max_video_length_minutes,
        max_upload_size_mb=settings.limits.max_upload_size_mb,
    )
    # Always present, null when nothing is planned.
    notice = maintenance.current(redis)
    info.maintenance = notice.as_response() if notice else None
    if settings.billing.provider != "none":
        info.free_minutes = settings.billing.free_minutes
        info.free_minutes_period = settings.billing.free_minutes_period
        info.google_oauth_client_ids = google_oauth_client_ids()
    return info


@router.get("/balance", response_model=BalanceResponse)
def balance(user: AuthenticatedUser | None = Depends(current_user)) -> BalanceResponse:
    user_id = user.user_id if user else "anonymous"
    email = user.email if user else None
    if settings.billing.provider != "none":
        billing.grant_free_minutes(user_id, email)
    return BalanceResponse(
        user_id=user_id,
        minutes=billing.get_balance(user_id),
        **billing.free_minutes_status(user_id),
    )


@router.get("/cost", response_model=CostResponse)
def cost(
    source_url: HttpUrl | None = None,
    duration_seconds: float | None = Query(default=None, gt=0),
    # Only to require sign-in when billing is on: checking a URL runs yt-dlp.
    user: AuthenticatedUser | None = Depends(current_user),
) -> CostResponse:
    """Cost of a video URL, or of a file of the given duration before it is uploaded."""
    if (source_url is None) == (duration_seconds is None):
        raise HTTPException(status_code=422, detail="Provide either source_url or duration_seconds")
    ensure_accepting(
        user.user_id if user else "anonymous",
        link=source_url is not None,
        submission=False,
        source_url=str(source_url) if source_url is not None else None,
    )
    if source_url is not None:
        duration = video_duration(source_url)
    else:
        duration = ensure_allowed_length(duration_seconds)
    return CostResponse(duration_seconds=duration, cost_minutes=transcription_cost(duration))


@router.post("/google-play/verify")
def verify_google_purchase(
    request: GooglePurchaseRequest,
    user: AuthenticatedUser | None = Depends(current_user),
) -> VerifyPurchaseResponse:
    if settings.billing.provider != "google_play":
        raise HTTPException(status_code=409, detail="Google Play billing is disabled")
    if user is None:
        raise HTTPException(status_code=409, detail="Google Play billing is disabled")
    if request.product_id not in settings.billing.products:
        raise HTTPException(status_code=400, detail="Unknown product")
    if not google_play.is_configured():
        raise HTTPException(status_code=503, detail="Google Play verification is not configured")

    service: Any = None
    try:
        service = google_play.android_publisher()
        response = service.purchases().products().get(
            packageName=settings.google_play.package_name,
            productId=request.product_id,
            token=request.purchase_token,
        ).execute()
    except Exception as error:
        raise HTTPException(status_code=502, detail="Google Play purchase verification failed") from error

    if response.get("purchaseState") != 0:
        raise HTTPException(status_code=400, detail="Purchase is not completed")
    order_id = response.get("orderId")
    if not isinstance(order_id, str) or not order_id:
        raise HTTPException(status_code=502, detail="Google Play purchase has no order ID")
    credited = billing.credit_purchase(
        user.user_id,
        request.product_id,
        request.purchase_token,
        order_id,
        user.email,
    )
    consumed = True
    try:
        service.purchases().products().consume(
            packageName=settings.google_play.package_name,
            productId=request.product_id,
            token=request.purchase_token,
        ).execute()
    except Exception:
        consumed = False
        logger.exception("Could not consume Google Play purchase %s", request.purchase_token[:8])
    return VerifyPurchaseResponse(
        user_id=user.user_id,
        credited_minutes=credited,
        minutes=billing.get_balance(user.user_id),
        consumed=consumed,
    )