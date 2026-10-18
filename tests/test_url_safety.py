import socket

import pytest

from app import url_safety
from app.url_safety import UnsafeSourceUrl, ensure_safe_source_url


@pytest.fixture
def resolve(api_context, monkeypatch):
    addresses = {"youtube.com": "142.250.74.14", "music.youtube.com": "142.250.74.14"}

    def fake_getaddrinfo(host, port, type=None):
        if host not in addresses:
            raise socket.gaierror("unknown host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addresses[host], port))]

    monkeypatch.setattr(url_safety.socket, "getaddrinfo", fake_getaddrinfo)
    return addresses


@pytest.mark.parametrize(
    "url",
    [
        "https://youtube.com/watch?v=1",
        "http://music.youtube.com/watch?v=1",
        "https://YOUTUBE.com./watch?v=1",
        "https://youtube.com:443/watch",
    ],
)
def test_public_allowed_urls_are_accepted(resolve, url):
    assert ensure_safe_source_url(f"  {url} ") == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://youtube.com/" + "a" * 2048,
        "https://youtube.com/watch?v=1 2",
        "ftp://youtube.com/video",
        "file:///etc/passwd",
        "https://user:pass@youtube.com/video",
        "https://youtube.com:8080/video",
        "https://youtube.com:99999/video",
        "https:///video",
        "http://localhost/video",
        "http://printer.local/video",
        "https://notyoutube.com/video",
        "https://youtube.com.evil.example/video",
        "https://unresolvable.youtube.com/video",
    ],
)
def test_unsafe_urls_are_rejected(resolve, url):
    with pytest.raises(UnsafeSourceUrl):
        ensure_safe_source_url(url)


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "::ffff:127.0.0.1", "2002:7f00:1::1", "224.0.0.1"],
)
def test_hosts_resolving_to_private_addresses_are_rejected(resolve, address):
    resolve["youtube.com"] = address

    with pytest.raises(UnsafeSourceUrl, match="non-public"):
        ensure_safe_source_url("https://youtube.com/video")


def test_empty_allow_list_allows_any_public_host(resolve, api_context):
    _, settings, _ = api_context
    resolve["example.org"] = "93.184.216.34"
    settings.limits.source_url_allowed_hosts = []

    assert ensure_safe_source_url("https://example.org/video") == "https://example.org/video"
