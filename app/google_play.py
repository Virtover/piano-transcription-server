import logging
from pathlib import Path

from app.config import settings


logger = logging.getLogger(__name__)
synced_products: set[str] | None = None


def service_account_file_path() -> str:
    configured_file = settings.google_play_service_account_file
    if not configured_file:
        return ""
    configured_path = Path(configured_file)
    if configured_path.is_absolute():
        return str(configured_path)
    return str(Path("/run/secrets") / configured_path.name)


def _list_product_ids(service) -> set[str]:
    products = set()
    page_token = None

    while True:
        arguments = {"packageName": settings.google_play_package_name}
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

    if settings.billing_provider != "google_play":
        return
    if not settings.google_play_package_name or not settings.google_play_service_account_file:
        synced_products = set()
        logger.warning("Google Play billing is enabled but catalog credentials are not configured")
        return

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        credentials = service_account.Credentials.from_service_account_file(
            service_account_file_path(),
            scopes=["https://www.googleapis.com/auth/androidpublisher"],
        )
        service = build("androidpublisher", "v3", credentials=credentials)
        products = _list_product_ids(service)
    except Exception as error:
        synced_products = set()
        logger.warning("Could not sync Google Play products: %s", error)
        return

    synced_products = products

    configured = set(settings.billing_products)
    for product_id in sorted(products - configured):
        logger.warning("Google Play product %s exists but is not configured on the server", product_id)
    for product_id in sorted(configured - products):
        logger.warning("Configured product %s does not exist in Google Play and will not be offered", product_id)