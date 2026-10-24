import json
import logging
import time

from app.config import google_play_service_account_json, settings


logger = logging.getLogger(__name__)
synced_products: set[str] | None = None


ANDROID_PUBLISHER_SCOPE = "https://www.googleapis.com/auth/androidpublisher"


def is_configured() -> bool:
    return bool(settings.google_play.package_name and google_play_service_account_json())


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


def sync_products() -> None:
    global synced_products

    if settings.billing.provider != "google_play":
        return
    if not is_configured():
        synced_products = set()
        logger.warning("Google Play billing is enabled but catalog credentials are not configured")
        return

    try:
        products = list_product_ids(android_publisher())
    except Exception as error:
        synced_products = set()
        logger.warning("Could not sync Google Play products: %s", error)
        return

    synced_products = products

    configured = set(settings.billing.products)
    for product_id in sorted(products - configured):
        logger.warning("Google Play product %s exists but is not configured on the server", product_id)
    for product_id in sorted(configured - products):
        logger.warning("Configured product %s does not exist in Google Play and will not be offered", product_id)

# Google keeps voided purchases for 30 days; each sync reads back over the
# previous one by this much, so nothing voided near the boundary is missed.
VOIDED_HISTORY_MS = 30 * 24 * 60 * 60 * 1000
VOIDED_OVERLAP_MS = 24 * 60 * 60 * 1000
VOIDED_SYNCED_UNTIL = "google_play_voided_synced_until"


def sync_voided_purchases(billing, service=None, now_ms: int | None = None) -> int:
    """Take back the minutes of purchases Google reports as voided: refunds,
    chargebacks, and cancelled orders. Safe to repeat: each purchase is
    voided once. Returns how many purchases were voided now."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
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
            "endTime": now_ms,
            "maxResults": 1000,
        }
        if page_token:
            arguments["token"] = page_token
        response = service.purchases().voidedpurchases().list(**arguments).execute()
        for purchase in response.get("voidedPurchases", []):
            token = purchase.get("purchaseToken")
            if not token:
                continue
            result = billing.void_purchase(token)
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
    billing.set_state(VOIDED_SYNCED_UNTIL, str(now_ms))
    return voided
