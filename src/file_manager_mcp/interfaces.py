"""The Local PC's own IPv4 addresses and OS route lookups: the production Interfaces for Addressing (ADR 0003)."""

from __future__ import annotations

import ipaddress
import socket
from time import monotonic

CACHE_SECONDS = 5.0
_INTERNET_PROBE = "8.8.8.8"

_cache: tuple[float, frozenset[str]] | None = None


def route_source(ip: str) -> str | None:
    """The local address the OS would send from to reach ``ip``; connecting a UDP socket sends no packet."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((ip, 9))
            return sock.getsockname()[0]
    except OSError:
        return None


def default_ipv4() -> str | None:
    """The address on the interface holding the default route, if there is one."""
    return route_source(_INTERNET_PROBE)


def _scan() -> list[str]:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    found = [info[4][0] for info in infos]
    default = default_ipv4()
    return found + ([default] if default else [])


def _usable(addr: str) -> bool:
    ip = ipaddress.IPv4Address(addr)
    return not (ip.is_loopback or ip.is_link_local)


def local_ipv4s() -> frozenset[str]:
    """Own interface IPv4 addresses (no loopback or link-local), refreshed every few seconds."""
    global _cache
    now = monotonic()
    if _cache is None or now >= _cache[0]:
        _cache = (now + CACHE_SECONDS, frozenset(a for a in _scan() if _usable(a)))
    return _cache[1]
