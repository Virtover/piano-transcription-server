import hashlib
import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, HttpUrl
from redis import Redis
from redis.exceptions import RedisError

from app.billing import Billing, SubscriptionOwned, transcription_cost
from app.config import settings
from app import clips, controls, google_play, maintenance, video_check, video_sites
from app.errors import SourceBusy, SourceError, SourceRefused, SourceTimedOut, SourceUnsupported
from app.errors import SourceVideoUnavailable
from app.maintenance import NoticeResponse
from app.slots import VIDEO_CHECKS_KEY, LeasedSlot
from app.auth import AuthenticatedUser, current_user, google_oauth_client_ids
from app.url_safety import ensure_safe_source_url


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["billing"])
redis = Redis.from_url(settings.service.redis_url, decode_responses=True, **settings.service.redis_options())
billing = Billing()
# Each video check runs yt-dlp for up to a minute. MAX_VIDEO_CHECKS bounds
# them across every API replica, as it bounds the requests the server's
# addresses send to video sites, whoever calls.
METADATA_SLOTS_KEY = VIDEO_CHECKS_KEY
METADATA_WAIT = 10
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
# Checks that are not answered from memory, in this process at once. Each
# holds one of the threads every sync route shares (AnyIO's 40) while it
# waits for a place and runs yt-dlp, up to a minute, so links sent faster
# than they can be checked are refused at once instead of leaving none
# for job status and downloads.
CHECK_THREADS = 16
check_threads = threading.BoundedSemaphore(CHECK_THREADS)
# The links whose check failed lately, per client, scored by when: a sorted
# set, so a link counts once however often it is sent.
FAILED_CHECKS_KEY = "video_check:failed:"
# Failures about the link itself; a busy server or a site that refuses or
# does not answer is nobody's fault, and an unsafe link never runs yt-dlp.
COUNTED_FAILURES = {SourceError, SourceUnsupported, SourceVideoUnavailable}


def check_client(request: Request, user: AuthenticatedUser | None) -> str:
    """Whose failed links count together: a signed-in user, or without
    sign-in the address connecting to the API. That is never a header a
    client could forge, so behind a proxy or tunnel all such clients share
    the proxy's address."""
    if user is not None:
        return f"user:{user.user_id}"
    return f"address:{request.client.host if request.client else 'unknown'}"


class CostResponse(BaseModel):
    # The whole video's or file's length, also when a part is asked for.
    duration_seconds: float
    cost_minutes: int
    # The part's length, when start_seconds or end_seconds asked for one;
    # cost_minutes is then its cost.
    clip_seconds: float | None = None


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


class SubscriptionOffer(BaseModel):
    product_id: str
    # The base plan to buy; None for the subscription's first.
    base_plan: str | None = None
    name: str
    # Given each paid period in place of free_minutes; None for unlimited.
    minutes_per_period: int | None
    unlimited: bool
    # From 1, by minutes; unlimited is the highest.
    level: int
    # Longer videos than max_video_length_minutes for its subscribers; None
    # when the plan has the general limit.
    max_video_length_minutes: int | None = None


class SubscriptionResponse(BaseModel):
    product_id: str
    name: str
    minutes_per_period: int | None
    level: int
    # When the period that runs now ends: renewed then if auto_renewing.
    period_ends_at: int
    auto_renewing: bool
    # Google Play's state, as ACTIVE, CANCELED (ends at period_ends_at), or
    # IN_GRACE_PERIOD (a payment failed; it is retried).
    state: str | None = None
    # The plan it turns into at period_ends_at, after a change of plan the
    # user deferred to the renewal, as a downgrade; None when it stays.
    next_product_id: str | None = None
    next_name: str | None = None
    # The plan's longer video limit, if it has one.
    max_video_length_minutes: int | None = None


class ServerInfoResponse(BaseModel):
    billing_provider: str
    offers: list[Offer]
    cleanup_interval_seconds: int
    # How long a finished job's status and MIDI file can be retrieved.
    result_retention_seconds: int
    max_video_length_minutes: int | None = None
    max_upload_size_mb: float | None
    # Whether piano arrangements are on (WORKER_ARRANGEMENT), so an app can offer them.
    arrangement: bool = False
    # Whether a submission may ask for only a part of its video or file
    # (start_seconds, end_seconds); older servers would transcribe it all.
    trimming: bool = False
    # Subscription plans, lowest level first; only when billing is enabled.
    subscriptions: list[SubscriptionOffer] | None = None
    # Given once to each new account; only when billing is enabled.
    welcome_minutes: int | None = None
    # only present when billing is enabled
    free_minutes: int | None = None
    free_minutes_period: str | None = None
    google_oauth_client_ids: list[str] | None = None
    # A planned server restart or shutdown, if one is pending.
    maintenance: NoticeResponse | None = None


class BalanceResponse(BaseModel):
    user_id: str
    # Available in all: monthly_minutes, spent first, plus paid_minutes.
    minutes: int
    # Renewed to free_minutes every free_minutes_period (FREE_MINUTES_INTERVAL);
    # unused ones expire.
    monthly_minutes: int = 0
    # Bought or given by hand; they never expire. Negative after a refund.
    paid_minutes: int = 0
    free_minutes_seconds_until_next_grant: int | None = None
    free_minutes_next_grant_at: int | None = None
    # A developer's account, made so by an administrator: its jobs are never
    # charged, so apps show its minutes as unlimited.
    developer: bool = False
    # The subscription whose period runs now; its minutes are monthly_minutes.
    subscription: SubscriptionResponse | None = None
    # An unlimited subscription: jobs are never charged nor refused for use.
    # After heavy use in the last 24 hours (unlimited_heavy_use), they run
    # one at a time.
    unlimited: bool = False
    unlimited_heavy_use: bool = False
    # The longest video this user may submit now, with their plan; None for
    # no limit.
    max_video_length_minutes: float | None = None


class GoogleSubscriptionRequest(BaseModel):
    product_id: str
    purchase_token: str = Field(min_length=1, max_length=4096)


class VerifySubscriptionResponse(BaseModel):
    user_id: str
    # started: a paid order's period started now; recorded: known already,
    # pending payment, or ended.
    status: str
    granted_minutes: int
    acknowledged: bool
    balance: BalanceResponse


@dataclass
class CheckedVideo:
    duration: float
    info: dict[str, Any]
    checked_at: float


def check_video(source_url: HttpUrl, user_id: str | None = None, client: str | None = None) -> CheckedVideo:
    """Read a video's info with yt-dlp, and check its length. A check is
    reused for VIDEO_CHECK_TTL, so asking the cost and then submitting the
    video asks the site once. Requests for a URL being checked wait for that
    check instead of starting another. With client (check_client), a client
    whose links failed MAX_FAILED_VIDEO_CHECKS times lately is refused
    before yt-dlp runs; answers from memory are still given."""
    try:
        # An unsafe URL is refused as unsupported.
        safe_url = ensure_safe_source_url(str(source_url))
        key = VIDEO_CHECK_KEY + hashlib.sha256(safe_url.encode()).hexdigest()
        checked = cached_check(key)
        if checked is None:
            ensure_few_failures(client, user_id)
            checked = fresh_check(key, safe_url, client)
    except SourceError as error:
        raise source_refusal(error) from error
    return CheckedVideo(
        ensure_allowed_length(float(checked["info"]["duration"]), user_id), checked["info"], checked["checked_at"]
    )


def fresh_check(key: str, safe_url: str, client: str | None) -> dict[str, Any]:
    if not check_threads.acquire(blocking=False):
        raise SourceBusy(video_sites.BUSY_MESSAGE, retry_after=5)
    try:
        return run_check(key, safe_url)
    except SourceError as error:
        if client is not None and type(error) in COUNTED_FAILURES:
            remember_failure(client, key)
        raise
    finally:
        check_threads.release()


def remember_failure(client: str, link: str) -> None:
    """Count a link whose check failed against client. In one transaction,
    so the set expires however the API stops."""
    window = settings.limits.failed_video_check_window
    if settings.limits.max_failed_video_checks is None:
        return
    key = FAILED_CHECKS_KEY + client
    now = time.time()
    try:
        with redis.pipeline() as pipe:
            pipe.zadd(key, {link: now})
            pipe.zremrangebyscore(key, "-inf", now - window)
            pipe.expire(key, math.ceil(window))
            pipe.execute()
    except RedisError:
        # The user's answer is the link's failure, not this.
        logger.warning("Could not count a failed video check", exc_info=True)


def ensure_few_failures(client: str | None, user_id: str | None) -> None:
    """Refuse a client with MAX_FAILED_VIDEO_CHECKS different failed links
    in the last FAILED_VIDEO_CHECK_WINDOW, until enough of them leave it.
    A developer's account, which tests failures, is never refused."""
    limit = settings.limits.max_failed_video_checks
    if client is None or limit is None:
        return
    window = settings.limits.failed_video_check_window
    key = FAILED_CHECKS_KEY + client
    now = time.time()
    with redis.pipeline() as pipe:
        pipe.zremrangebyscore(key, "-inf", now - window)
        pipe.zrange(key, 0, -1, withscores=True)
        _, failures = pipe.execute()
    if len(failures) < limit or (user_id is not None and billing.is_developer(user_id)):
        return
    # When enough have left the window for one more.
    _, since = failures[len(failures) - limit]
    retry_after = max(1, math.ceil(since + window - now))
    wait = "a minute" if retry_after <= 60 else f"{math.ceil(retry_after / 60)} minutes"
    raise SourceRefused(
        f"Several links you sent lately could not be read, so new links can be checked again in {wait}. "
        "Check the link, or upload the file instead",
        retry_after=retry_after,
    )


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
    """The video's info, from this server's address, or in a home cluster,
    while the site refuses it, from a node's (app.video_check). A node's
    answer is marked `delegated`, and its info is never saved for the
    download."""
    if video_check.should_delegate(redis, safe_url):
        slot = LeasedSlot(redis, METADATA_SLOTS_KEY, settings.limits.max_video_checks)
        if not slot.take(METADATA_WAIT):
            raise SourceBusy(video_sites.BUSY_MESSAGE, retry_after=5)
        try:
            from app.worker.tasks import check_video_link

            info = video_check.delegate(redis, safe_url, check_video_link.send)
        finally:
            slot.release()
        return {**info, video_check.DELEGATED_MARK: True}
    return video_check.read_video_info(redis, safe_url)


def error_detail(error: SourceError) -> dict[str, str]:
    return {"message": str(error), "error_code": error.code}


def source_refusal(error: SourceError) -> HTTPException:
    return HTTPException(
        status_code=error.status,
        detail=error_detail(error),
        headers={"Retry-After": str(error.retry_after)} if error.retry_after else None,
    )


def video_duration(source_url: HttpUrl, user_id: str | None = None, client: str | None = None) -> float:
    return check_video(source_url, user_id, client).duration


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
    # Refused before anything is checked or held: in a home cluster, only
    # when no node can check the link from an address the site accepts.
    if source_url is not None and (
        limited := video_check.refusal_everywhere(redis, video_sites.site_of(source_url))
    ) is not None:
        raise source_refusal(limited)


def plan_video_minutes(product_id: str | None) -> int | None:
    """A plan's longer video limit, or None when it has none above the
    general MAX_VIDEO_LENGTH_MINUTES, which then applies."""
    plan = settings.billing.subscriptions.get(product_id) if product_id else None
    general = settings.limits.max_video_length_minutes
    if plan is None or plan.max_video_length_minutes is None or general is None:
        return None
    return plan.max_video_length_minutes if plan.max_video_length_minutes > general else None


def max_video_minutes(user_id: str | None) -> float | None:
    """The longest video the user may submit, in minutes; None for no limit:
    the general limit, or a longer one of the plan whose period runs."""
    general = settings.limits.max_video_length_minutes
    if general is None or not user_id or settings.billing.provider == "none" or not any(
        plan.max_video_length_minutes for plan in settings.billing.subscriptions.values()
    ):
        return general
    subscription = billing.subscription_status(user_id)["subscription"]
    return plan_video_minutes(subscription["product_id"] if subscription else None) or general


def ensure_allowed_length(duration: float, user_id: str | None = None) -> float:
    minutes = max_video_minutes(user_id)
    if minutes and duration > minutes * 60:
        raise HTTPException(
            status_code=413,
            detail={
                "message": f"Videos can be up to {minutes:g} minutes long",
                "error_code": "video_too_long",
                "max_video_length_minutes": minutes,
            },
        )
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
        arrangement=settings.worker.arrangement,
        trimming=True,
    )
    # Always present, null when nothing is planned.
    notice = maintenance.current(redis)
    info.maintenance = notice.as_response() if notice else None
    if settings.billing.provider != "none":
        levels = settings.billing.subscription_levels()
        offered = google_play.offered_subscriptions()
        info.subscriptions = sorted(
            (
                SubscriptionOffer(
                    product_id=product_id,
                    base_plan=plan.base_plan,
                    name=plan.name,
                    minutes_per_period=plan.minutes,
                    unlimited=plan.unlimited,
                    level=levels[product_id],
                    max_video_length_minutes=plan_video_minutes(product_id),
                )
                for product_id, plan in settings.billing.subscriptions.items()
                if product_id in offered
            ),
            key=lambda offer: offer.level,
        )
        info.welcome_minutes = settings.billing.welcome_minutes
        info.free_minutes = settings.billing.free_minutes
        info.free_minutes_period = settings.billing.free_minutes_period
        info.google_oauth_client_ids = google_oauth_client_ids()
    return info


@router.get("/balance", response_model=BalanceResponse)
def balance(user: AuthenticatedUser | None = Depends(current_user)) -> BalanceResponse:
    user_id = user.user_id if user else "anonymous"
    email = user.email if user else None
    if settings.billing.provider != "none":
        billing.renew_monthly_minutes(user_id, email)
    return balance_of(user_id)


def balance_of(user_id: str) -> BalanceResponse:
    status = billing.subscription_status(user_id)
    subscription = status["subscription"]
    return BalanceResponse(
        user_id=user_id,
        **billing.balances(user_id),
        **billing.free_minutes_status(user_id),
        developer=billing.is_developer(user_id),
        subscription=SubscriptionResponse(
            **{key: value for key, value in subscription.items() if key != "minutes"},
            minutes_per_period=subscription["minutes"],
            max_video_length_minutes=plan_video_minutes(subscription["product_id"]),
        ) if subscription else None,
        max_video_length_minutes=(
            max_video_minutes(user_id) if settings.billing.provider != "none" else settings.limits.max_video_length_minutes
        ),
        unlimited=status["unlimited"],
        unlimited_heavy_use=status["unlimited_heavy_use"],
    )


@router.get("/cost", response_model=CostResponse, response_model_exclude_none=True)
def cost(
    connection: Request,
    source_url: HttpUrl | None = None,
    duration_seconds: float | None = Query(default=None, gt=0),
    start_seconds: float | None = Query(default=None, ge=0, allow_inf_nan=False),
    end_seconds: float | None = Query(default=None, gt=0, allow_inf_nan=False),
    # Only to require sign-in when billing is on: checking a URL runs yt-dlp.
    user: AuthenticatedUser | None = Depends(current_user),
) -> CostResponse:
    """Cost of a video URL, or of a file of the given duration before it is
    uploaded; with start_seconds or end_seconds, of only that part of it."""
    if (source_url is None) == (duration_seconds is None):
        raise HTTPException(status_code=422, detail="Provide either source_url or duration_seconds")
    ensure_accepting(
        user.user_id if user else "anonymous",
        link=source_url is not None,
        submission=False,
        source_url=str(source_url) if source_url is not None else None,
    )
    user_id = user.user_id if user else None
    if source_url is not None:
        duration = video_duration(source_url, user_id, check_client(connection, user))
    else:
        duration = ensure_allowed_length(duration_seconds, user_id)
    clip = checked_clip(start_seconds, end_seconds, duration)
    if clip is None:
        return CostResponse(duration_seconds=duration, cost_minutes=transcription_cost(duration))
    length = clip.length(duration)
    return CostResponse(duration_seconds=duration, cost_minutes=transcription_cost(length), clip_seconds=length)


def checked_clip(start: float | None, end: float | None, duration: float) -> clips.Clip | None:
    """The part of audio of duration seconds a request asks for; 422 with
    error_code invalid_clip for one outside it."""
    try:
        return clips.check_clip(start, end, duration)
    except clips.InvalidClip as error:
        raise HTTPException(
            status_code=422, detail={"message": str(error), "error_code": clips.INVALID_CLIP}
        ) from error


def purchase_summary(response: dict) -> str:
    """What Google reported about a purchase, for the log: no token, no user."""
    return ", ".join(
        f"{field}={response.get(field)}"
        for field in ("orderId", "purchaseState", "consumptionState", "acknowledgementState", "purchaseType")
    )


# Google refuses to consume a purchase while another change to it is under
# way, as right after the purchase or while a duplicate request consumes it.
# The waits double: 1, 2, and 4 seconds.
CONSUME_ATTEMPTS = 4
CONSUME_FIRST_WAIT = 1.0


def is_concurrent_update(error: Exception) -> bool:
    status = getattr(getattr(error, "resp", None), "status", None)
    return status == 409 and b"concurrentUpdate" in (getattr(error, "content", None) or b"")


def _consumed(products: Any, package: str, product_id: str, token: str) -> bool:
    try:
        state = products.get(packageName=package, productId=product_id, token=token).execute()
    except Exception:
        return False
    return state.get("consumptionState") == 1


def consume_purchase(service: Any, product_id: str, token: str) -> None:
    """Consume the purchase, waiting out concurrent changes. Returns once it
    is consumed, by this call or by another; raises otherwise."""
    products = service.purchases().products()
    package = settings.google_play.package_name
    wait = CONSUME_FIRST_WAIT
    for attempt in range(1, CONSUME_ATTEMPTS + 1):
        try:
            products.consume(packageName=package, productId=product_id, token=token).execute()
            return
        except Exception as error:
            if not is_concurrent_update(error):
                # Another request may have consumed it just before: Google
                # then says the product is no longer owned.
                if _consumed(products, package, product_id, token):
                    return
                raise
            if attempt == CONSUME_ATTEMPTS:
                raise
        time.sleep(wait)
        wait *= 2
        # The change under way may have been another request's consumption.
        state = products.get(packageName=package, productId=product_id, token=token).execute()
        if state.get("consumptionState") == 1:
            return


# Each verification asks Google; a client may ask this many times in
# VERIFY_WINDOW seconds, plenty for retries and restoring purchases.
VERIFY_LIMIT = 60
VERIFY_WINDOW = 600
VERIFY_KEY = "billing:verify:"


def limit_verifications(user_id: str) -> None:
    key = VERIFY_KEY + hashlib.sha256(user_id.encode()).hexdigest()[:24]
    count = redis.incr(key)
    if count == 1 or redis.ttl(key) < 0:
        redis.expire(key, VERIFY_WINDOW)
    if count > VERIFY_LIMIT:
        raise HTTPException(
            status_code=429,
            detail="Too many purchase checks; try again later",
            headers={"Retry-After": str(max(1, redis.ttl(key)))},
        )


@router.post("/google-play/verify")
def verify_google_purchase(
    request: GooglePurchaseRequest,
    user: AuthenticatedUser | None = Depends(current_user),
) -> VerifyPurchaseResponse:
    if settings.billing.provider != "google_play":
        raise HTTPException(status_code=409, detail="Google Play billing is disabled")
    if user is None:
        raise HTTPException(status_code=409, detail="Google Play billing is disabled")
    limit_verifications(user.user_id)
    if request.product_id not in settings.billing.products:
        logger.warning(
            "Refused Google Play purchase %s: product %s is not in BILLING_PRODUCTS",
            request.purchase_token[:8],
            request.product_id,
        )
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
        logger.warning(
            "Refused Google Play purchase %s of %s: not completed (%s)",
            request.purchase_token[:8],
            request.product_id,
            purchase_summary(response),
        )
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
        # A repeated request finds it consumed already.
        if response.get("consumptionState") != 1:
            consume_purchase(service, request.product_id, request.purchase_token)
    except Exception:
        consumed = False
        logger.exception(
            "Could not consume Google Play purchase %s of %s (%s)",
            request.purchase_token[:8],
            request.product_id,
            purchase_summary(response),
        )
    return VerifyPurchaseResponse(
        user_id=user.user_id,
        credited_minutes=credited,
        minutes=billing.get_balance(user.user_id),
        consumed=consumed,
    )


@router.post("/google-play/subscriptions/verify")
def verify_google_subscription(
    request: GoogleSubscriptionRequest,
    user: AuthenticatedUser | None = Depends(current_user),
) -> VerifySubscriptionResponse:
    """Check a subscription with Google Play and record it for the user: a
    new purchase, a renewal, an upgrade, or restoring one on another device.
    Safe to repeat: each paid order gives its minutes once."""
    if settings.billing.provider != "google_play" or user is None:
        raise HTTPException(status_code=409, detail="Google Play billing is disabled")
    if request.product_id not in settings.billing.subscriptions:
        logger.warning(
            "Refused Google Play subscription %s: %s is not in BILLING_SUBSCRIPTIONS",
            request.purchase_token[:8],
            request.product_id,
        )
        raise HTTPException(status_code=400, detail="Unknown subscription")
    if not google_play.is_configured():
        raise HTTPException(status_code=503, detail="Google Play verification is not configured")
    limit_verifications(user.user_id)
    try:
        service = google_play.android_publisher()
        info = google_play.read_subscription(service, request.purchase_token)
    except Exception as error:
        if google_play.is_gone(error):
            raise HTTPException(status_code=400, detail="Unknown subscription purchase") from error
        raise HTTPException(status_code=502, detail="Google Play subscription verification failed") from error
    if info.product_id not in settings.billing.subscriptions:
        raise HTTPException(status_code=400, detail="Unknown subscription")
    try:
        result = billing.record_subscription(user.user_id, info, user.email)
    except SubscriptionOwned as error:
        logger.warning("Refused Google Play subscription %s: another account has it", request.purchase_token[:8])
        raise HTTPException(
            status_code=409,
            detail={"message": "This subscription belongs to another account", "error_code": "subscription_owned"},
        ) from error
    acknowledged = info.acknowledged
    if not acknowledged and info.paid:
        try:
            google_play.acknowledge_subscription(service, info.product_id, request.purchase_token)
            billing.mark_subscription_acknowledged(request.purchase_token)
            acknowledged = True
        except Exception as error:  # noqa: BLE001
            logger.error(
                "Could not acknowledge a Google Play subscription of %s (%s)",
                info.product_id,
                google_play.google_error_summary(error),
            )
    if result["status"] == "started":
        logger.info(
            "Google Play subscription %s started for user %s: %d minutes",
            info.product_id,
            user.user_id,
            result["minutes"],
        )
    return VerifySubscriptionResponse(
        user_id=user.user_id,
        status=result["status"],
        granted_minutes=result["minutes"],
        acknowledged=acknowledged,
        balance=balance_of(user.user_id),
    )
