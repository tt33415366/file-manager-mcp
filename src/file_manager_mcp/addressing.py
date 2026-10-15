"""Connection Path addressing: which Host headers reach this server, and which base URL each link uses (ADR 0003)."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Collection
from typing import Protocol

from . import interfaces as os_interfaces
from .config import ServerConfig
from .errors import FileManagerError

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
WILDCARD_HOSTS = ("0.0.0.0", "")


class Interfaces(Protocol):
    """The Local PC's own addresses; the ``interfaces`` module asks the OS, tests pass fixed ones."""

    def local_ipv4s(self) -> Collection[str]: ...

    def route_source(self, ip: str) -> str | None: ...

    def default_ipv4(self) -> str | None: ...


class Addressing:
    """Every question about how Remote Agents and DUTs reach this server, for one ``[server]`` table.

    ``server`` is read on each call, so this follows the running values the Runtime keeps.
    """

    def __init__(self, server: ServerConfig, interfaces: Interfaces = os_interfaces) -> None:
        self.server = server
        self.interfaces = interfaces

    @property
    def listens_on_lan(self) -> bool:
        return self.server.host in WILDCARD_HOSTS

    @property
    def local_url(self) -> str:
        host = "127.0.0.1" if self.listens_on_lan else self.server.host
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.server.port}"

    @property
    def base_url(self) -> str:
        if self.server.public_base_url:
            return self.server.public_base_url.rstrip("/")
        return self.local_url

    @property
    def mcp_url(self) -> str:
        return self.base_url + "/mcp"

    def accepts_host(self, host: str) -> bool:
        """Loopback names and ``allowed_hosts``, or one of this PC's current IPv4 addresses on the server port."""
        host, port = host.lower(), str(self.server.port)
        named = {h for name in LOOPBACK_HOSTS for h in (name, f"{name}:{port}")}
        if host in named or host in {h.lower() for h in self.server.allowed_hosts}:
            return True
        ip, sep, given = host.partition(":")
        if sep and given != port:
            return False
        return ip in self.interfaces.local_ipv4s()

    def link_base(self, request_host: str | None) -> str:
        """Base URL for a Remote Agent's link: the Host it called (already accepted), unless public_base_url is set."""
        if request_host and not self.server.public_base_url:
            return f"http://{request_host}"
        return self.base_url

    def dut_base(self, for_host: str) -> str:
        """Base URL on the local address the OS routes to ``for_host``, if the listener covers it."""
        try:
            ip = socket.getaddrinfo(for_host, None, socket.AF_INET)[0][4][0]
        except (OSError, UnicodeError):
            raise FileManagerError("INVALID_ARGUMENT", f"cannot resolve for_host {for_host!r} to an IPv4 address") from None
        source = self.interfaces.route_source(ip)
        if source is None:
            raise FileManagerError("INVALID_ARGUMENT", f"no route from this PC to {for_host!r}")
        if ipaddress.IPv4Address(source).is_loopback:
            raise FileManagerError("INVALID_ARGUMENT", f"{for_host!r} is this PC; for_host must be another device")
        if source not in self.interfaces.local_ipv4s():
            raise FileManagerError(
                "INVALID_ARGUMENT",
                f"this PC reaches {for_host} from {source}, which DUTs cannot use (link-local addresses are not "
                "supported); give that interface a static IPv4 address",
            )
        if not self.listens_on_lan and self.server.host != source:
            raise FileManagerError(
                "INVALID_ARGUMENT",
                f"the server listens on {self.server.host} only, so {for_host} cannot reach it at {source}; "
                "set server.host to 0.0.0.0 and restart",
            )
        return f"http://{source}:{self.server.port}"

    def lan_ips(self) -> list[str]:
        return sorted(self.interfaces.local_ipv4s())

    def lan_default(self) -> str | None:
        """The address on the default route if it is a LAN address, else the first LAN address."""
        ips, default = self.lan_ips(), self.interfaces.default_ipv4()
        return default if default in ips else next(iter(ips), None)

    def lan_urls(self) -> list[str]:
        return [f"http://{ip}:{self.server.port}/mcp" for ip in self.lan_ips()] if self.listens_on_lan else []
