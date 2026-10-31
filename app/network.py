"""Outgoing connections on hosts without IPv6.

Docker's default networks give containers an IPv6 loopback address but no
IPv6 route out. The HTTP clients then also try the IPv6 addresses of hosts
like RunPod's and Google's, which fail with "Network is unreachable", and a
connection fails whenever no IPv4 attempt succeeded first. Without an IPv6
route, name lookups here return only IPv4 addresses.
"""

import socket


# Connecting a UDP socket sends nothing: it only looks up a route.
IPV6_PROBE_ADDRESS = ("2001:4860:4860::8888", 53)
_getaddrinfo = socket.getaddrinfo


def has_ipv6_route() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as probe:
            probe.connect(IPV6_PROBE_ADDRESS)
    except OSError:
        return False
    return True


def ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if family == socket.AF_UNSPEC:
        try:
            return _getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
        except socket.gaierror:
            # A name with only IPv6 addresses, or an IPv6 address such as ::1.
            pass
    return _getaddrinfo(host, port, family, type, proto, flags)


def all_addresses_getaddrinfo():
    """The name lookup that returns every address, IPv6 too."""
    return _getaddrinfo if socket.getaddrinfo is ipv4_getaddrinfo else socket.getaddrinfo


def use_ipv4_without_ipv6_route() -> bool:
    """Make name lookups return only IPv4 addresses when there is no IPv6
    route. Returns whether they now do."""
    if socket.getaddrinfo is ipv4_getaddrinfo:
        return True
    if has_ipv6_route():
        return False
    socket.getaddrinfo = ipv4_getaddrinfo
    return True
