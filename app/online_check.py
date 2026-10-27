"""`python -m app.config check --online`: test each configured service with
the real credentials, so a wrong key fails here instead of on a user's job.

Each check returns a Result with a hint for the most likely fix. Checks never
print secrets, presigned URLs, or raw provider responses.
"""

import re
import uuid
from dataclasses import dataclass
from typing import Callable, Literal

import requests

Status = Literal["ok", "warn", "fail", "skip"]

REQUEST_TIMEOUT = 15
CLIENT_ID_PATTERN = re.compile(r"^\d+-[a-z0-9]+\.apps\.googleusercontent\.com$")
TEST_BODY = b"piano-transcription-server online check\n"


@dataclass(frozen=True)
class Result:
    name: str
    status: Status
    detail: str
    hint: str | None = None


# --- Storage ----------------------------------------------------------------

STORAGE_HINTS = {
    "InvalidAccessKeyId": "The access key ID is wrong. Rewrite secrets/outsource_storage_access_key, or create a new R2 API token.",
    "SignatureDoesNotMatch": "The secret access key does not match the access key ID. Rewrite secrets/outsource_storage_secret_key.",
    "Unauthorized": "The storage keys are wrong. Rewrite both key files, or create a new R2 API token.",
    "AccessDenied": "The token cannot write to this bucket. It needs Object Read & Write, applied to this bucket.",
    "NoSuchBucket": (
        "No bucket with this name at this endpoint. Check OUTSOURCE_STORAGE_BUCKET; a bucket in the EU "
        "jurisdiction is only visible through the .eu. endpoint, and other buckets only without it."
    ),
}


def _error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code")
    return None


def _storage_failure(step: str, error: Exception) -> Result:
    code = _error_code(error)
    if code:
        return Result("Storage", "fail", f"{step}: {code}", STORAGE_HINTS.get(code))
    hint = None
    if "Endpoint" in type(error).__name__ or "Connect" in type(error).__name__:
        hint = "The endpoint cannot be reached. Check OUTSOURCE_STORAGE_ENDPOINT."
    return Result("Storage", "fail", f"{step}: {type(error).__name__}", hint)


def check_storage(config, storage_factory: Callable | None = None, http=requests) -> Result:
    """Upload a small object, download it through a presigned URL like the
    remote handler does, and delete it."""
    from app.outsource.storage import S3Storage

    try:
        storage = (storage_factory or S3Storage.from_settings)(config)
    except Exception as error:
        return Result("Storage", "fail", str(error))

    key = f"online-check/{uuid.uuid4().hex}.txt"
    try:
        storage.client.put_object(Bucket=storage.bucket, Key=key, Body=TEST_BODY)
    except Exception as error:
        return _storage_failure("Upload", error)

    try:
        try:
            url = storage.presigned_url(key, 300)
            response = http.get(url, timeout=REQUEST_TIMEOUT)
        except Exception as error:
            return Result("Storage", "fail", f"Signed download: {type(error).__name__}")
        if response.status_code != 200 or response.content != TEST_BODY:
            return Result(
                "Storage",
                "fail",
                f"Signed download: HTTP {response.status_code}",
                "Signed links are rejected. If the R2 token has client IP address filtering, remove it: "
                "RunPod downloads through these links from its own addresses.",
            )
    finally:
        try:
            storage.client.delete_object(Bucket=storage.bucket, Key=key)
            deleted = None
        except Exception as error:
            deleted = error
    if deleted is not None:
        return _storage_failure("Delete", deleted)
    return Result("Storage", "ok", f"upload, signed download, and delete work in bucket {storage.bucket}")


# --- RunPod -----------------------------------------------------------------

RUNPOD_HINTS = {
    401: "The API key is wrong. Create a new key and rewrite secrets/runpod_api_key.",
    403: "The API key has no access to this endpoint. Give it access to the endpoint, or create a new restricted key.",
    404: "No endpoint with this ID. Check RUNPOD_ENDPOINT_ID.",
}


def check_runpod(config, provider_factory: Callable | None = None) -> list[Result]:
    from app.outsource.provider import RemoteFailed
    from app.outsource.runpod import RunPodProvider

    try:
        provider = (provider_factory or RunPodProvider.from_settings)(config)
    except RemoteFailed as error:
        return [Result("RunPod", "fail", str(error))]

    if provider.local:
        try:
            provider.session.request("GET", provider.base_url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as error:
            return [Result("RunPod", "fail", f"Local test server unreachable: {type(error).__name__}")]
        return [Result("RunPod", "ok", "local test server reachable")]

    try:
        response = provider._request("GET", "health", REQUEST_TIMEOUT)
    except RemoteFailed as error:
        return [Result("RunPod", "fail", str(error))]
    if not response.ok:
        return [
            Result(
                "RunPod",
                "fail",
                f"Endpoint health: HTTP {response.status_code}",
                RUNPOD_HINTS.get(response.status_code),
            )
        ]
    try:
        workers = response.json().get("workers", {})
        detail = ", ".join(f"{count} {state}" for state, count in workers.items() if count) or "no workers running"
    except (ValueError, AttributeError):
        detail = "reachable"
    results = [Result("RunPod", "ok", f"endpoint {provider.endpoint_id}: {detail}")]
    results.extend(check_endpoint_settings(config, provider))
    return results


def _version(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(value).split("."))
    except ValueError:
        return (0,)


def check_endpoint_settings(config, provider) -> list[Result]:
    """Compare the endpoint's settings with `python -m app.config requirements`."""
    from app.outsource.provider import RemoteFailed
    from app.requirements import (
        IDLE_TIMEOUT_SECONDS,
        RUNPOD_CUDA_VERSION,
        execution_timeout_minutes,
        jobs_per_gpu,
    )

    name = "RunPod settings"
    try:
        endpoint = provider.endpoint_settings()
    except RemoteFailed as error:
        if provider.has_read_key:
            hint = (
                "The key in secrets/runpod_read_api_key was rejected. It needs read access to the "
                "account (permission Read Only), not only to the endpoint."
            )
        else:
            hint = (
                "Restricted keys, like the one that runs jobs, cannot read endpoint settings. To check "
                "them, and to use OUTSOURCE_MAX_CONCURRENCY=auto, save a key with read access to the "
                "account (permission Read Only) as secrets/runpod_read_api_key. Jobs run either way."
            )
        return [Result(name, "warn", f"cannot read the endpoint's settings: {error}", hint)]

    results = []
    template = endpoint.get("template") or {}
    image = template.get("imageName")
    results.append(
        Result(name, "ok", f"image {image}")
        if image
        else Result(name, "warn", "the endpoint reports no image", "Check the endpoint's container image.")
    )

    env = {**(template.get("env") or {}), **(endpoint.get("env") or {})}
    expected = config.outsource.storage_endpoint
    if env.get("OUTSOURCE_STORAGE_ENDPOINT") != expected:
        results.append(
            Result(
                name,
                "fail",
                "OUTSOURCE_STORAGE_ENDPOINT on the endpoint "
                + ("is missing" if "OUTSOURCE_STORAGE_ENDPOINT" not in env else "differs from .env"),
                f"Set the endpoint's environment variable OUTSOURCE_STORAGE_ENDPOINT to {expected}; "
                "the handler refuses downloads from any other host.",
            )
        )

    minimum = _version(RUNPOD_CUDA_VERSION)
    allowed = endpoint.get("allowedCudaVersions") or []
    min_cuda = endpoint.get("minCudaVersion")
    if not ((min_cuda and _version(min_cuda) >= minimum) or (allowed and all(_version(v) >= minimum for v in allowed))):
        results.append(
            Result(
                name,
                "warn",
                "hosts with CUDA older than " + RUNPOD_CUDA_VERSION + " are allowed",
                f"Allow only CUDA {RUNPOD_CUDA_VERSION} and newer: jobs fail at start on older drivers.",
            )
        )

    timeout_ms = endpoint.get("executionTimeoutMs")
    required_minutes = execution_timeout_minutes(config)
    if not timeout_ms or timeout_ms < required_minutes * 60_000:
        shown = f"{timeout_ms / 60_000:g} min" if timeout_ms else "not set"
        results.append(
            Result(
                name,
                "warn",
                f"execution timeout {shown}, needs at least {required_minutes} min",
                "Raise the endpoint's execution timeout, or RunPod stops jobs the server still waits for.",
            )
        )

    if (endpoint.get("workersMin") or 0) > 0:
        results.append(
            Result(
                name,
                "warn",
                f"{endpoint['workersMin']} workers always on",
                "Always-on workers are billed while idle. Use 0 unless cold starts are unacceptable.",
            )
        )
    workers_max = endpoint.get("workersMax") or 0
    per_worker = max(1, jobs_per_gpu(16, config))
    if config.outsource.max_concurrency and workers_max * per_worker < config.outsource.max_concurrency:
        results.append(
            Result(
                name,
                "warn",
                f"max workers {workers_max} run about {workers_max * per_worker} jobs, "
                f"fewer than OUTSOURCE_MAX_CONCURRENCY={config.outsource.max_concurrency}",
                "Raise the endpoint's max workers, or lower OUTSOURCE_MAX_CONCURRENCY.",
            )
        )
    elif config.outsource.max_concurrency is None:
        results.append(Result(name, "ok", f"max workers {workers_max} (OUTSOURCE_MAX_CONCURRENCY=auto)"))

    if endpoint.get("scalerType") != "REQUEST_COUNT":
        results.append(
            Result(
                name,
                "warn",
                f"scaling by {str(endpoint.get('scalerType') or 'unknown').lower().replace('_', ' ')}",
                f"Scale by request count, about {per_worker} per worker: each worker batches that many requests.",
            )
        )

    idle = endpoint.get("idleTimeout")
    if idle and idle > 6 * IDLE_TIMEOUT_SECONDS:
        results.append(
            Result(
                name,
                "warn",
                f"idle timeout {idle} s",
                f"Idle workers are billed; about {IDLE_TIMEOUT_SECONDS} s is enough.",
            )
        )

    if len(results) == 1:
        results.append(Result(name, "ok", "match the requirements"))
    return results


# --- Google -----------------------------------------------------------------

def check_client_ids(config) -> Result:
    client_ids = [client_id.strip() for client_id in config.auth.client_ids if client_id.strip()]
    invalid = [client_id for client_id in client_ids if not CLIENT_ID_PATTERN.match(client_id)]
    if invalid:
        return Result(
            "Google sign-in",
            "fail",
            f"{len(invalid)} of {len(client_ids)} client IDs are malformed: {', '.join(invalid)}",
            "Client IDs look like 123456789-abc123.apps.googleusercontent.com. Copy them from the "
            "Google Cloud console, and do not use the client secret.",
        )
    return Result(
        "Google sign-in",
        "ok",
        f"{len(client_ids)} client IDs well formed (Google can only confirm them with a signed-in user's token)",
    )


PLAY_HINTS = {
    401: "Google rejected the service account. Its key may be deleted or disabled: create a new JSON key.",
    403: (
        "The service account lacks Play Console permissions for this app, or they have not applied yet: "
        "new permissions can take up to 24 hours. Check the account under Play Console > Users and permissions."
    ),
    404: "No app with this package name in the service account's Play developer account. Check GOOGLE_PLAY_PACKAGE_NAME.",
}


def check_google_play(config, service_factory: Callable | None = None) -> Result:
    from app.google_play import android_publisher, list_product_ids

    try:
        service = (service_factory or android_publisher)(config)
        products = list_product_ids(service, config.google_play.package_name)
    except Exception as error:
        status = getattr(getattr(error, "resp", None), "status", None)
        if status is not None:
            return Result("Google Play", "fail", f"Listing products: HTTP {status}", PLAY_HINTS.get(int(status)))
        hint = None
        if "RefreshError" in type(error).__name__:
            hint = "The service account key was rejected. Create a new JSON key for the service account."
        if "Google Play Android Developer API" in str(error) or "has not been used" in str(error):
            hint = "Enable the Google Play Android Developer API in the service account's Google Cloud project."
        return Result("Google Play", "fail", f"Listing products: {type(error).__name__}", hint)

    try:
        service.purchases().voidedpurchases().list(
            packageName=config.google_play.package_name, maxResults=1
        ).execute()
    except Exception as error:
        status = getattr(getattr(error, "resp", None), "status", None)
        return Result(
            "Google Play",
            "fail",
            f"Reading voided purchases: {f'HTTP {status}' if status is not None else type(error).__name__}",
            "The service account cannot read refunds and chargebacks, so refunded minutes are never taken back. "
            "It needs access to this app's orders and financial data; new permissions can take up to 24 hours.",
        )

    configured = set(config.billing.products)
    missing = sorted(configured - products)
    unused = sorted(products - configured)
    if missing:
        return Result(
            "Google Play",
            "warn",
            f"{len(products)} products in Play Console; configured but missing there: {', '.join(missing)}",
            "Create and activate these products in Play Console, or remove them from BILLING_PRODUCTS. "
            "Missing products are not offered to users.",
        )
    if unused:
        return Result(
            "Google Play",
            "warn",
            f"products in Play Console but not in BILLING_PRODUCTS: {', '.join(unused)}",
            "Add them to BILLING_PRODUCTS with their minutes, or users cannot buy them.",
        )
    return Result("Google Play", "ok", f"{len(products)} products, all configured")


# --- Cloudflare Tunnel ------------------------------------------------------

# cloudflared's readiness endpoint, on the Compose network (docker-compose.yml).
TUNNEL_READY_URL = "http://cloudflared:20241/ready"


def check_tunnel(config, http=requests) -> list[Result]:
    try:
        response = http.get(TUNNEL_READY_URL, timeout=REQUEST_TIMEOUT)
        connections = response.json().get("readyConnections", 0)
    except Exception as error:
        return [Result(
            "Cloudflare Tunnel",
            "fail",
            f"cloudflared cannot be reached: {type(error).__name__}",
            "Start the stack with COMPOSE_PROFILES=cloudflare in .env, and run this check with "
            "`docker compose run`, which joins the stack's network.",
        )]
    if response.status_code != 200 or not connections:
        return [Result(
            "Cloudflare Tunnel",
            "fail",
            "cloudflared is not connected to Cloudflare",
            "See `docker compose logs cloudflared`. If Cloudflare refuses the tunnel, the token in "
            "secrets/cloudflare/tunnel_token is wrong or its tunnel was deleted: write the token again, then "
            "`docker compose up -d cloudflared`.",
        )]
    results = [Result("Cloudflare Tunnel", "ok", f"connected to Cloudflare ({connections} connections)")]
    results.append(check_public_address(config, http))
    return results


def check_public_address(config, http=requests) -> Result:
    """The public hostname reaches this API, and only with the password if
    one is set."""
    hostname = config.tunnel.hostname
    if not hostname:
        return Result(
            "Public address",
            "warn",
            "CLOUDFLARE_TUNNEL_HOSTNAME is not set, so the public address was not checked",
            "Set it to the tunnel's public hostname, such as piano.example.com.",
        )
    base = f"https://{hostname}"
    try:
        health = http.get(f"{base}/health", timeout=REQUEST_TIMEOUT)
    except Exception as error:
        return Result(
            "Public address",
            "fail",
            f"{base}/health: {type(error).__name__}",
            "Check that the hostname is spelled right and that its DNS record exists.",
        )
    try:
        # A page from Cloudflare or another server is not this API's JSON.
        reached = health.status_code == 200 and health.json().get("status") == "ok"
    except (ValueError, AttributeError):
        reached = False
    if not reached:
        return Result(
            "Public address",
            "fail",
            f"{base}/health: HTTP {health.status_code}, not this server",
            "Route the tunnel's public hostname to the service http://api:8000 (cloudflared reaches the API by "
            "its Compose name, not localhost), with no challenge or access login in front of it: the app "
            "cannot pass one.",
        )
    if config.secrets.server_password is not None:
        response = http.get(f"{base}/api/server-info", timeout=REQUEST_TIMEOUT)
        if response.status_code != 401:
            return Result(
                "Public address",
                "fail",
                f"{base}/api/server-info answered HTTP {response.status_code} without the server password",
                "Something in front of the API answers for it. Turn off caching for the hostname.",
            )
    return Result("Public address", "ok", f"{base} reaches this server")


# --- Off-site backups --------------------------------------------------------

OFFSITE_HINTS = {
    "InvalidAccessKeyId": "The access key ID is wrong. Rewrite secrets/offsite_backup_access_key.",
    "SignatureDoesNotMatch": "The secret access key does not match the access key ID. Rewrite secrets/offsite_backup_secret_key.",
    "Unauthorized": "The storage keys are wrong. Rewrite both secrets/offsite_backup_* files.",
    "AccessDenied": "The keys cannot use this bucket. They need to list, read, write, and delete objects in it.",
    "NoSuchBucket": "No bucket with this name at this endpoint. Check OFFSITE_BACKUP_BUCKET and OFFSITE_BACKUP_ENDPOINT.",
}


def _offsite_failure(step: str, error: Exception) -> Result:
    code = _error_code(error)
    if code:
        return Result("Off-site backups", "fail", f"{step}: {code}", OFFSITE_HINTS.get(code))
    hint = None
    if "Endpoint" in type(error).__name__ or "Connect" in type(error).__name__:
        hint = "The endpoint cannot be reached. Check OFFSITE_BACKUP_ENDPOINT."
    return Result("Off-site backups", "fail", f"{step}: {type(error).__name__}", hint)


def check_offsite(config, store_factory: Callable | None = None) -> Result:
    """Write, read back, list, and delete a small object, as the backups do."""
    from app.backups import OFFSITE_PREFIX, OffsiteStore

    try:
        store = (store_factory or OffsiteStore.from_settings)(config)
    except Exception as error:
        return Result("Off-site backups", "fail", str(error))

    key = f"{OFFSITE_PREFIX}online-check-{uuid.uuid4().hex}.txt"
    try:
        store.client.put_object(Bucket=store.bucket, Key=key, Body=TEST_BODY)
    except Exception as error:
        return _offsite_failure("Upload", error)
    try:
        try:
            body = store.client.get_object(Bucket=store.bucket, Key=key)["Body"].read()
        except Exception as error:
            return _offsite_failure("Download", error)
        if body != TEST_BODY:
            return Result("Off-site backups", "fail", "Download: the object came back changed")
        try:
            count = len(store.backups())
        except Exception as error:
            return _offsite_failure("Listing", error)
    finally:
        try:
            store.client.delete_object(Bucket=store.bucket, Key=key)
            deleted = None
        except Exception as error:
            deleted = error
    if deleted is not None:
        return _offsite_failure("Delete", deleted)
    return Result(
        "Off-site backups",
        "ok",
        f"upload, download, listing, and delete work in bucket {store.bucket}; {count} backups there",
    )


# --- Error reports ----------------------------------------------------------

ERROR_REPORT_HINTS = {
    401: "The website refused the token. Write the same token to secrets/error_report_token and to the website's "
         "SERVER_ERROR_TOKEN.",
    404: "Nothing receives reports at this address. Check ERROR_REPORT_URL, and that the website is up to date.",
    503: "The website has no SERVER_ERROR_TOKEN set, so it does not accept reports yet.",
}


def check_error_report(config, http=requests) -> Result:
    """Sends a test report, which the website shows as an info entry."""
    url = config.error_report.url
    try:
        response = http.post(
            url,
            json={
                "service": "check --online",
                "level": "INFO",
                "type": "TestReport",
                "message": "Test report from python -m app.config check --online",
                "test": True,
            },
            headers={"Authorization": f"Bearer {config.secrets.error_report_token.get_secret_value()}"},
            timeout=REQUEST_TIMEOUT,
            # A redirect is a wrong address; the token must not follow it.
            allow_redirects=False,
        )
    except Exception as error:
        return Result("Error reports", "fail", f"{url}: {type(error).__name__}",
                      "The website cannot be reached from this server. Check ERROR_REPORT_URL.")
    if 300 <= response.status_code < 400:
        return Result("Error reports", "fail", f"{url}: redirected (HTTP {response.status_code})",
                      "Set ERROR_REPORT_URL to the final address, with https:// and the exact host.")
    if response.status_code >= 300:
        return Result("Error reports", "fail", f"{url}: HTTP {response.status_code}",
                      ERROR_REPORT_HINTS.get(response.status_code))
    return Result("Error reports", "ok", f"{url} accepted a test report")


# --- Running ----------------------------------------------------------------

def run_checks(config) -> list[Result]:
    results = []
    if config.outsource.provider == "runpod":
        results.append(check_storage(config))
        results.extend(check_runpod(config))
    else:
        results.append(Result("Outsourcing", "skip", "OUTSOURCE_PROVIDER=none"))
    if config.billing.provider == "google_play":
        results.append(check_client_ids(config))
        results.append(check_google_play(config))
    else:
        results.append(Result("Google", "skip", "BILLING_PROVIDER=none"))
    if config.tunnel.enabled:
        results.extend(check_tunnel(config))
    else:
        results.append(Result("Cloudflare Tunnel", "skip", "COMPOSE_PROFILES does not list cloudflare"))
    if config.offsite.enabled:
        results.append(check_offsite(config))
    else:
        results.append(Result("Off-site backups", "skip", "OFFSITE_BACKUP_BUCKET is not set"))
    if config.error_report.url:
        results.append(check_error_report(config))
    else:
        results.append(Result("Error reports", "skip", "ERROR_REPORT_URL is not set"))
    return results


LABELS = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}


def format_results(results: list[Result]) -> list[str]:
    width = max(len(result.name) for result in results)
    lines = []
    for result in results:
        lines.append(f"{LABELS[result.status]}  {result.name.ljust(width)}  {result.detail}")
        if result.hint:
            lines.append(f"      {' ' * width}  -> {result.hint}")
    return lines
