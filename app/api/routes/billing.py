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
from app import google_play, maintenance, video_info
from app.maintenance import NoticeResponse
from app.auth import AuthenticatedUser, current_user, google_oauth_client_ids
from app.url_safety import YT_DLP_SAFETY_OPTIONS, ensure_safe_source_url, UnsafeSourceUrl


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
    max_video_length_minutes: int | None = None
    max_upload_size_mb: float
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
    video asks the site once."""
    try:
        safe_url = ensure_safe_source_url(str(source_url))
    except UnsafeSourceUrl as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    key = VIDEO_CHECK_KEY + hashlib.sha256(safe_url.encode()).hexdigest()
    cached = redis.get(key)
    if cached:
        checked = json.loads(cached)
        info, checked_at = checked["info"], checked["checked_at"]
    else:
        info, checked_at = read_video_info(safe_url), time.time()
        redis.set(key, json.dumps({"info": info, "checked_at": checked_at}), ex=VIDEO_CHECK_TTL)
    return CheckedVideo(ensure_allowed_length(float(info["duration"])), info, checked_at)


def read_video_info(safe_url: str) -> dict[str, Any]:
    if not metadata_slots.acquire(timeout=METADATA_WAIT):
        raise HTTPException(
            status_code=503,
            detail="Too many videos are being checked; try again in a moment",
            headers={"Retry-After": "5"},
        )
    try:
        result = subprocess.run(
            [
                "yt-dlp",
                *YT_DLP_SAFETY_OPTIONS,
                "--js-runtimes",
                "deno",
                "--extractor-args",
                "youtube:player_client=web,android",
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
        info = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=422, detail="Unable to read video metadata") from error
    finally:
        metadata_slots.release()
    duration = info.get("duration") if isinstance(info, dict) else None
    if not isinstance(duration, (int, float)) or duration <= 0:
        raise HTTPException(status_code=422, detail="Video duration is unavailable")
    return video_info.slim(info)


def video_duration(source_url: HttpUrl) -> float:
    return check_video(source_url).duration


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