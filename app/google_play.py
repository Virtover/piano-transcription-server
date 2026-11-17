import json
import logging
import re
import time

from app.config import google_play_service_account_json, settings


logger = logging.getLogger(__name__)
synced_products: set[str] | None = None


ANDROID_PUBLISHER_SCOPE = "https://www.googleapis.com/auth/androidpublisher"


def is_configured() -> bool:
    return bool(settings.google_play.package_name and google_play_service_account_json())


def is_network_error(error: Exception) -> bool:
    """Whether Google could not be reached at all, as when this host's
    network is down or not yet up, rather than Google refusing a request."""
    if isinstance(error, OSError):
        return True
    try:
        import httplib2
        from google.auth.exceptions import TransportError
    except ImportError:
        return False
    return isinstance(error, (httplib2.HttpLib2Error, TransportError))


def android_publisher(config=None):
    """An Android Publisher API client using the service-account secret."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    credentials = service_account.Credentials.from_service_account_info(
        json.loads(google_play_service_account_json(config) or "{}"),
        scopes=[ANDROID_PUBLISHER_SCOPE],
    )
    return build("androidpublisher", "v3", credentials=credentials)


def list_product_ids(service, package_name: str | None = None) -> set[str]:
    products = set()
    page_token = None

    while True:
        arguments = {"packageName": package_name or settings.google_play.package_name}
        if page_token:
            arguments["pageToken"] = page_token
        response = service.monetization().onetimeproducts().list(**arguments).execute()
        products.update(
            product["productId"]
            for product in response.get("oneTimeProducts", [])
            if product.get("productId")
        )
        page_token = response.get("nextPageToken")
        if not page_token:
            return products


def sync_products() -> bool:
    """Read the products from Google Play. Returns False when they could not
    be read; the products read before, if any, stay offered."""
    global synced_products

    if settings.billing.provider != "google_play":
        return True
    if not is_configured():
        global synced_subscriptions
        synced_products = set()
        synced_subscriptions = {}
        logger.warning("Google Play billing is enabled but catalog credentials are not configured")
        return True

    try:
        service = android_publisher()
        products = list_product_ids(service)
        if settings.billing.subscriptions:
            sync_subscriptions(service)
    except Exception as error:
        if synced_products is None:
            synced_products = set()
        logger.warning("Could not sync Google Play products: %s", error)
        return False

    changed = products != synced_products
    synced_products = products
    if not changed:
        return True

    configured = set(settings.billing.products)
    for product_id in sorted(products - configured):
        logger.warning("Google Play product %s exists but is not configured on the server", product_id)
    for product_id in sorted(configured - products):
        logger.warning("Configured product %s does not exist in Google Play and will not be offered", product_id)
    return True

# Google keeps voided purchases for 30 days; each sync reads back over the
# previous one by this much, so nothing voided near the boundary is missed.
VOIDED_HISTORY_MS = 30 * 24 * 60 * 60 * 1000
VOIDED_OVERLAP_MS = 24 * 60 * 60 * 1000
VOIDED_SYNCED_UNTIL = "google_play_voided_synced_until"
# Google refuses an end time that is not in its past. A clock running a
# little ahead, as Docker Desktop's often does after the host sleeps, must
# not fail every sync; the overlap reads the last minutes again next time.
VOIDED_CLOCK_MARGIN_MS = 5 * 60 * 1000


def sync_voided_purchases(billing, service=None, now_ms: int | None = None) -> int:
    """Take back the minutes of purchases Google reports as voided: refunds,
    chargebacks, and cancelled orders. Safe to repeat: each purchase is
    voided once. Returns how many purchases were voided now."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    end = now_ms - VOIDED_CLOCK_MARGIN_MS
    oldest = now_ms - VOIDED_HISTORY_MS + 60_000
    synced_until = billing.get_state(VOIDED_SYNCED_UNTIL)
    if synced_until is None:
        start = oldest
    else:
        start = int(synced_until) - VOIDED_OVERLAP_MS
        if start < oldest:
            logger.warning(
                "Voided Google Play purchases were last read more than 30 days ago; "
                "purchases voided before %s can no longer be read",
                time.strftime("%Y-%m-%d", time.gmtime(oldest / 1000)),
            )
            start = oldest

    service = service or android_publisher()
    voided = 0
    page_token = None
    while True:
        arguments = {
            "packageName": settings.google_play.package_name,
            "startTime": start,
            "endTime": end,
            "maxResults": 1000,
            # Subscriptions' orders too, not only one-time products'.
            "type": 1,
        }
        if page_token:
            arguments["token"] = page_token
        response = service.purchases().voidedpurchases().list(**arguments).execute()
        for purchase in response.get("voidedPurchases", []):
            token = purchase.get("purchaseToken")
            if not token:
                continue
            result = billing.void_purchase(token, purchase.get("orderId"))
            if result:
                user_id, minutes = result
                voided += 1
                logger.warning(
                    "Google Play purchase %s was voided (reason %s); took back %d minutes from user %s",
                    purchase.get("orderId", "?"),
                    purchase.get("voidedReason", "?"),
                    minutes,
                    user_id,
                )
        page_token = (response.get("tokenPagination") or {}).get("nextPageToken")
        if not page_token:
            break
    billing.set_state(VOIDED_SYNCED_UNTIL, str(end))
    return voided


# --- Subscriptions ---------------------------------------------------------------

# Subscription product ID -> its active base plans, as read from Google Play.
synced_subscriptions: dict[str, set[str]] | None = None
STATE_PREFIX = "SUBSCRIPTION_STATE_"


def list_subscriptions(service, package_name: str | None = None) -> dict[str, set[str]]:
    """Subscription product IDs, each with its active base plans."""
    subscriptions: dict[str, set[str]] = {}
    page_token = None
    while True:
        arguments = {"packageName": package_name or settings.google_play.package_name}
        if page_token:
            arguments["pageToken"] = page_token
        response = service.monetization().subscriptions().list(**arguments).execute()
        for subscription in response.get("subscriptions", []):
            product_id = subscription.get("productId")
            if not product_id:
                continue
            subscriptions[product_id] = {
                plan["basePlanId"]
                for plan in subscription.get("basePlans", [])
                if plan.get("basePlanId") and plan.get("state", "ACTIVE") == "ACTIVE"
            }
        page_token = response.get("nextPageToken")
        if not page_token:
            return subscriptions


def offered_subscriptions() -> list[str]:
    """The configured subscriptions the apps may buy: with Google Play, only
    those whose base plan is active there."""
    configured = settings.billing.subscriptions
    if settings.billing.provider != "google_play":
        return list(configured)
    synced = synced_subscriptions or {}
    offered = []
    for product_id, plan in configured.items():
        plans = synced.get(product_id)
        if plans and (plan.base_plan is None or plan.base_plan in plans):
            offered.append(product_id)
    return offered


def sync_subscriptions(service) -> None:
    global synced_subscriptions
    subscriptions = list_subscriptions(service)
    changed = subscriptions != synced_subscriptions
    synced_subscriptions = subscriptions
    if not changed:
        return
    for product_id, plan in settings.billing.subscriptions.items():
        plans = subscriptions.get(product_id)
        if plans is None:
            logger.warning("Configured subscription %s does not exist in Google Play and will not be offered", product_id)
        elif plan.base_plan is not None and plan.base_plan not in plans:
            logger.warning(
                "Subscription %s has no active base plan %s in Google Play and will not be offered",
                product_id,
                plan.base_plan,
            )
        elif not plans:
            logger.warning("Subscription %s has no active base plan in Google Play and will not be offered", product_id)
    for product_id in sorted(set(subscriptions) - set(settings.billing.subscriptions)):
        logger.warning("Google Play subscription %s exists but is not configured on the server", product_id)


def _timestamp(text: str | None) -> int:
    """Seconds since the epoch of an RFC 3339 time, as Google gives them."""
    from datetime import datetime

    if not text:
        return 0
    text = text.strip().replace("Z", "+00:00")
    # Python before 3.11 reads only 3 or 6 digits of a fraction; Google
    # gives from 0 to 9.
    fraction = re.search(r"\.(\d+)", text)
    if fraction:
        digits = (fraction.group(1) + "000000")[:6]
        text = text[:fraction.start()] + "." + digits + text[fraction.end():]
    return int(datetime.fromisoformat(text).timestamp())


def parse_subscription(token: str, response: dict) -> "SubscriptionInfo":
    """A SubscriptionInfo from purchases.subscriptionsv2.get. Of several
    line items, the configured one that expires last counts."""
    from app.billing import SubscriptionInfo

    items = response.get("lineItems") or []
    configured = [item for item in items if item.get("productId") in settings.billing.subscriptions]
    item = max(configured or items or [{}], key=lambda line: _timestamp(line.get("expiryTime")))
    state = (response.get("subscriptionState") or "UNSPECIFIED").removeprefix(STATE_PREFIX)
    return SubscriptionInfo(
        token=token,
        product_id=item.get("productId") or "",
        state=state,
        expires_at=_timestamp(item.get("expiryTime")),
        auto_renewing=bool((item.get("autoRenewingPlan") or {}).get("autoRenewEnabled")),
        # The latest order that was paid: a grace period's is not yet.
        latest_order_id=item.get("latestSuccessfulOrderId") or response.get("latestOrderId") or None,
        linked_token=response.get("linkedPurchaseToken") or None,
        account_tag=(response.get("externalAccountIdentifiers") or {}).get("obfuscatedExternalAccountId") or None,
        acknowledged=response.get("acknowledgementState") != "ACKNOWLEDGEMENT_STATE_PENDING",
    )


def read_subscription(service, token: str) -> "SubscriptionInfo":
    response = service.purchases().subscriptionsv2().get(
        packageName=settings.google_play.package_name, token=token
    ).execute()
    return parse_subscription(token, response)


def acknowledge_subscription(service, product_id: str, token: str) -> None:
    """Acknowledge a subscription purchase, which Google refunds within
    three days otherwise. Acknowledging it again does no harm."""
    service.purchases().subscriptions().acknowledge(
        packageName=settings.google_play.package_name,
        subscriptionId=product_id,
        token=token,
        body={},
    ).execute()


def is_gone(error: Exception) -> bool:
    """Whether Google no longer knows the purchase token: it expired long ago."""
    status = getattr(getattr(error, "resp", None), "status", None)
    return status in (404, 410)


def refresh_subscriptions(billing, service=None, now: int | None = None) -> int:
    """Read again from Google Play the subscriptions whose period ends soon
    or has ended, to start a renewal's period, or end one that was not
    renewed. Returns how many were read."""
    tokens = billing.subscriptions_to_check(now)
    if not tokens:
        return 0
    service = service or android_publisher()
    read = 0
    for token in tokens:
        try:
            info = read_subscription(service, token)
        except Exception as error:
            if is_gone(error):
                billing.end_subscription(token)
                continue
            if is_network_error(error):
                raise
            logger.warning("Could not read a Google Play subscription (%s)", error)
            billing.mark_subscription_checked(token)
            continue
        result = billing.record_subscription(None, info)
        read += 1
        if result["status"] == "started":
            logger.info(
                "Google Play subscription %s renewed for user %s: %d minutes",
                info.product_id,
                result["user_id"],
                result["minutes"],
            )
        if not info.acknowledged and info.paid:
            try:
                acknowledge_subscription(service, info.product_id, token)
                billing.mark_subscription_acknowledged(token)
            except Exception:
                logger.exception("Could not acknowledge a Google Play subscription of %s", info.product_id)
    return read
