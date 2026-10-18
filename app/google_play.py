import json
import logging

from app.config import google_play_service_account_json, settings


logger = logging.getLogger(__name__)
synced_products: set[str] | None = None


ANDROID_PUBLISHER_SCOPE = "https://www.googleapis.com/auth/androidpublisher"


def is_configured() -> bool:
    return bool(settings.google_play.package_name and google_play_service_account_json())


def android_publisher():
    """An Android Publisher API client using the service-account secret."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    credentials = service_account.Credentials.from_service_account_info(
        json.loads(google_play_service_account_json() or "{}"),
        scopes=[ANDROID_PUBLISHER_SCOPE],
    )
    return build("androidpublisher", "v3", credentials=credentials)


def _list_product_ids(service) -> set[str]:
    products = set()
    page_token = None

    while True:
        arguments = {"packageName": settings.google_play.package_name}
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
        products = _list_product_ids(android_publisher())
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