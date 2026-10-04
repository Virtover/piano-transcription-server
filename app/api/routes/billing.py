import json
import logging
import subprocess
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, HttpUrl
from redis import Redis

from app.billing import Billing, transcription_cost
from app.config import settings
from app import google_play


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["billing"])
redis = Redis.from_url(settings.redis_url, decode_responses=True)
billing = Billing()


class CostResponse(BaseModel):
    duration_seconds: float
    cost_minutes: int


class GooglePurchaseRequest(BaseModel):
    product_id: str
    purchase_token: str = Field(min_length=1)


def user_id(value: str | None) -> str:
    normalized = (value or "").strip()
    if settings.billing_provider != "none" and not normalized:
        raise HTTPException(
            status_code=401,
            detail="X-User-Id header is required when billing is enabled",
        )
    return normalized or "anonymous"


def video_duration(source_url: HttpUrl) -> float:
    try:
        result = subprocess.run(
            ["yt-dlp", "--dump-single-json", "--no-download", str(source_url)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        duration = json.loads(result.stdout).get("duration")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=422, detail="Unable to read video metadata") from error
    if not isinstance(duration, (int, float)) or duration <= 0:
        raise HTTPException(status_code=422, detail="Video duration is unavailable")
    if settings.max_video_length_minutes and duration > settings.max_video_length_minutes * 60:
        raise HTTPException(status_code=413, detail="Video exceeds the configured maximum length")
    return float(duration)


@router.get("/server-info")
def server_info() -> dict[str, Any]:
    product_ids = set(settings.billing_products)
    if settings.billing_provider == "google_play":
        product_ids &= google_play.synced_products or set()
    response = {
        "billing_provider": settings.billing_provider,
        "offers": [
            {"product_id": product_id, "transcription_minutes": minutes}
            for product_id, minutes in settings.billing_products.items()
            if product_id in product_ids
        ],
        "cleanup_interval_seconds": settings.cleanup_interval_seconds,
        "max_video_length_minutes": settings.max_video_length_minutes,
    }
    if settings.billing_provider != "none":
        response.update({
            "free_minutes": settings.free_minutes,
            "free_minutes_period": settings.free_minutes_period,
        })
    return response


@router.get("/balance")
def balance(x_user_id: str | None = Header(default=None)) -> dict[str, int | str | None]:
    current_user = user_id(x_user_id)
    if settings.billing_provider != "none":
        billing.grant_free_minutes(current_user)
    return {
        "user_id": current_user,
        "minutes": billing.get_balance(current_user),
        **billing.free_minutes_status(current_user),
    }


@router.get("/cost", response_model=CostResponse)
def cost(source_url: HttpUrl) -> CostResponse:
    duration = video_duration(source_url)
    return CostResponse(duration_seconds=duration, cost_minutes=transcription_cost(duration))


@router.post("/google-play/verify")
def verify_google_purchase(
    request: GooglePurchaseRequest,
    x_user_id: str | None = Header(default=None),
) -> dict[str, int | str]:
    if settings.billing_provider != "google_play":
        raise HTTPException(status_code=409, detail="Google Play billing is disabled")
    current_user = user_id(x_user_id)
    if request.product_id not in settings.billing_products:
        raise HTTPException(status_code=400, detail="Unknown product")
    if not settings.google_play_package_name or not settings.google_play_service_account_file:
        raise HTTPException(status_code=503, detail="Google Play verification is not configured")

    service: Any = None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        credentials = service_account.Credentials.from_service_account_file(
            google_play.service_account_file_path(),
            scopes=["https://www.googleapis.com/auth/androidpublisher"],
        )
        service = build("androidpublisher", "v3", credentials=credentials)
        response = service.purchases().products().get(
            packageName=settings.google_play_package_name,
            productId=request.product_id,
            token=request.purchase_token,
        ).execute()
    except Exception as error:
        raise HTTPException(status_code=502, detail="Google Play purchase verification failed") from error

    if response.get("purchaseState") != 0:
        raise HTTPException(status_code=400, detail="Purchase is not completed")
    credited = billing.credit_purchase(current_user, request.product_id, request.purchase_token)
    try:
        service.purchases().products().consume(
            packageName=settings.google_play_package_name,
            productId=request.product_id,
            token=request.purchase_token,
        ).execute()
    except Exception as error:
        logger.exception("Could not consume Google Play purchase")
        raise HTTPException(status_code=502, detail="Google Play purchase consumption failed") from error
    return {"user_id": current_user, "credited_minutes": credited, "minutes": billing.get_balance(current_user)}