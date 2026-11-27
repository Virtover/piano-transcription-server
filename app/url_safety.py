import ipaddress
import socket
from urllib.parse import urlsplit

from app import network
from app.config import settings
from app.errors import SourceUnsupported

MAX_URL_LENGTH = 2048
ALLOWED_PORTS = {None, 80, 443}
BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home.arpa")

# Options for every yt-dlp call. The checks below only see the submitted URL;
# a site's extractor can hand yt-dlp another URL (a Reddit link post does),
# and the generic extractor would then fetch any address, including private
# ones. Without it, only extractors for known sites run.
YT_DLP_SAFETY_OPTIONS = ["--ignore-config", "--use-extractors", "default,-generic", "--no-playlist"]


class UnsafeSourceUrl(SourceUnsupported, ValueError):
    """The URL must not be passed to yt-dlp."""


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.version == 6:
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip.sixtofour or ip.teredo:  # can embed private IPv4 addresses
            return False
    return ip.is_global and not ip.is_multicast


def client_link(url: object) -> str | None:
    """url, if it is safe to show to a client as a link or an image (a
    thumbnail from a site's info): https, of bounded length, without
    credentials, whitespace, or control characters, on the default port, and
    to a host that is no local name or non-public address. Nothing is looked
    up: the server never opens it. None otherwise."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        return None
    if any(ord(c) <= 0x20 or ord(c) == 0x7F for c in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or parts.username or parts.password or port not in (None, 443):
        return None
    host = (parts.hostname or "").rstrip(".").lower()
    if not host or host == "localhost" or host.endswith(BLOCKED_SUFFIXES):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A name: it must look like one, with a dot, as public hosts have.
        return url if "." in host else None
    return url if _is_public(address) else None


def ensure_safe_source_url(url: str) -> str:
    url = url.strip()
    if not url or len(url) > MAX_URL_LENGTH:
        raise UnsafeSourceUrl("URL is empty or too long")
    if any(ord(c) <= 0x20 or ord(c) == 0x7F for c in url):
        raise UnsafeSourceUrl("URL contains whitespace or control characters")

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise UnsafeSourceUrl("URL is malformed") from error

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise UnsafeSourceUrl("Only http(s) URLs are supported")
    if parts.username or parts.password:
        raise UnsafeSourceUrl("URLs with credentials are not supported")
    if port not in ALLOWED_PORTS:
        raise UnsafeSourceUrl("Unsupported port")

    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeSourceUrl("URL has no host")
    if host == "localhost" or host.endswith(BLOCKED_SUFFIXES):
        raise UnsafeSourceUrl("URL points to a non-public address")

    allowed = [h.strip().lower().lstrip(".") for h in settings.limits.source_url_allowed_hosts if h.strip()]
    if allowed and not any(host == h or host.endswith("." + h) for h in allowed):
        raise UnsafeSourceUrl("This site is not supported")

    # Every address the host resolves to must be public. This also catches
    # tricks like http://2130706433/ or http://0x7f.1/ (both mean 127.0.0.1).
    # Every address, IPv6 too, even where app.network limits lookups to IPv4.
    try:
        infos = network.all_addresses_getaddrinfo()(
            host, port or (443 if scheme == "https" else 80), type=socket.SOCK_STREAM
        )
    except socket.gaierror as error:
        raise UnsafeSourceUrl("Host cannot be resolved") from error
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not _is_public(ip):
            raise UnsafeSourceUrl("URL points to a non-public address")

    return url
