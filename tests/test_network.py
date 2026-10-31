import socket

import pytest

from app import network, url_safety


IPV4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.20.37.192", 443))
IPV6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:10::6814:25c0", 443, 0, 0))


@pytest.fixture
def lookups(monkeypatch):
    """Records the families asked for; names starting with v6only have no
    IPv4 address."""
    asked = []

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        asked.append(family)
        if family == socket.AF_INET:
            if host.startswith("v6only"):
                raise socket.gaierror(socket.EAI_NONAME, "no IPv4 address")
            return [IPV4]
        if family == socket.AF_INET6:
            return [IPV6]
        return [IPV4, IPV6]

    monkeypatch.setattr(network, "_getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return asked


def test_without_an_ipv6_route_lookups_return_only_ipv4(lookups, monkeypatch):
    monkeypatch.setattr(network, "has_ipv6_route", lambda: False)
    assert network.use_ipv4_without_ipv6_route()
    assert socket.getaddrinfo("rest.runpod.io", 443) == [IPV4]
    assert lookups == [socket.AF_INET]


def test_with_an_ipv6_route_lookups_are_unchanged(lookups, monkeypatch):
    monkeypatch.setattr(network, "has_ipv6_route", lambda: True)
    assert not network.use_ipv4_without_ipv6_route()
    assert socket.getaddrinfo("rest.runpod.io", 443) == [IPV4, IPV6]


def test_names_with_only_ipv6_addresses_still_resolve(lookups):
    assert network.ipv4_getaddrinfo("v6only.example", 443) == [IPV4, IPV6]
    assert lookups == [socket.AF_INET, socket.AF_UNSPEC]


def test_an_asked_family_is_kept(lookups):
    assert network.ipv4_getaddrinfo("rest.runpod.io", 443, socket.AF_INET6) == [IPV6]


def test_installing_twice_keeps_one_wrapper(lookups, monkeypatch):
    monkeypatch.setattr(network, "has_ipv6_route", lambda: False)
    network.use_ipv4_without_ipv6_route()
    assert network.use_ipv4_without_ipv6_route()
    assert socket.getaddrinfo is network.ipv4_getaddrinfo


def test_the_probe_reports_a_missing_route(monkeypatch):
    class Unroutable:
        def __init__(self, *arguments):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *details):
            return False

        def connect(self, address):
            raise OSError(101, "Network is unreachable")

    monkeypatch.setattr(network.socket, "socket", Unroutable)
    assert not network.has_ipv6_route()


def test_source_urls_are_checked_against_ipv6_addresses_too(monkeypatch):
    private = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00::1", 443, 0, 0))
    monkeypatch.setattr(network, "_getaddrinfo", lambda *arguments, **options: [IPV4, private])
    monkeypatch.setattr(socket, "getaddrinfo", network.ipv4_getaddrinfo)
    monkeypatch.setattr(url_safety.settings.limits, "source_url_allowed_hosts", [])
    with pytest.raises(url_safety.UnsafeSourceUrl, match="non-public"):
        url_safety.ensure_safe_source_url("https://video.example/watch")
