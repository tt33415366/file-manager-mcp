"""Outermost ASGI middleware: Host/Origin check, then a Signed Link or bearer auth on every path (spec section 3).

This is the only Host/Origin gate: the SDK's own check is off because its host list is static,
while Addressing checks the Local PC's interface addresses live. It is also the only place a Caller is made.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NamedTuple
from urllib.parse import parse_qsl

from .signed import Grant

Scope = dict[str, Any]
_STATE_KEY = "caller"


class UploadRules(NamedTuple):
    """What a Signed Link fixes for an upload; None leaves it to the request."""

    overwrite: bool | None
    max_bytes: int | None


@dataclass(frozen=True)
class Caller:
    """Who a request acts for: a token's name, or a Signed Link issued by that token."""

    name: str
    link: Grant | None = None

    @property
    def audit_name(self) -> str:
        return f"signed:{self.name}" if self.link else self.name

    def permits(self, direction: str, path: str) -> bool:
        """A token may do anything; a Signed Link only its own direction on its own path."""
        return self.link is None or (self.link.direction == direction and self.link.path == path)

    @property
    def upload_rules(self) -> UploadRules:
        if self.link is None:
            return UploadRules(None, None)
        return UploadRules(self.link.overwrite, self.link.max_bytes)


def caller_of(scope_or_request: Any) -> Caller | None:
    """The Caller attached to an ASGI scope, or to an object carrying one (a Starlette Request)."""
    scope = scope_or_request if isinstance(scope_or_request, dict) else getattr(scope_or_request, "scope", None)
    return (scope or {}).get("state", {}).get(_STATE_KEY)


async def _send_json(send: Callable, status: int, body: dict, headers: list[tuple[bytes, bytes]] = ()) -> None:
    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()), *headers],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class AuthMiddleware:
    def __init__(
        self,
        app: Callable,
        *,
        verify: Callable[[str], str | None],
        accepts_host: Callable[[str], bool],
        allowed_origins: list[str],
        direction_of: Callable[[str, str], str | None] | None = None,
        verify_link: Callable[[str, list[tuple[str, str]]], Grant | None] | None = None,
        on_reject: Callable[[Scope, int], None] | None = None,
    ) -> None:
        self.app = app
        self.verify = verify
        self.accepts_host = accepts_host
        self.allowed_origins = set(allowed_origins)
        self.direction_of = direction_of
        self.verify_link = verify_link
        self.on_reject = on_reject

    def _link_caller(self, scope: Scope, query: list[tuple[str, str]]) -> Caller | None:
        direction = self.direction_of(scope["method"], scope["path"]) if self.direction_of else None
        grant = self.verify_link(direction, query) if direction and self.verify_link else None
        return Caller(grant.by, grant) if grant else None

    async def _reject(self, scope: Scope, send: Callable, status: int, body: dict, headers=()) -> None:
        if self.on_reject:
            self.on_reject(scope, status)
        await _send_json(send, status, body, list(headers))

    async def __call__(self, scope: Scope, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}

        if not self.accepts_host(headers.get("host", "")):
            await self._reject(scope, send, 421, {"error": "invalid_host"})
            return
        origin = headers.get("origin")
        if origin is not None and origin not in self.allowed_origins:
            await self._reject(scope, send, 403, {"error": "invalid_origin"})
            return

        query = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        if any(k == "sig" for k, _ in query):
            caller = self._link_caller(scope, query)
            if caller is None:
                await self._reject(scope, send, 403, {"error": "invalid_link"})
                return
        else:
            scheme, _, token = headers.get("authorization", "").partition(" ")
            name = self.verify(token.strip()) if scheme.lower() == "bearer" else None
            if name is None:
                await self._reject(scope, send, 401, {"error": "unauthorized"}, [(b"www-authenticate", b"Bearer")])
                return
            caller = Caller(name)

        scope.setdefault("state", {})[_STATE_KEY] = caller
        await self.app(scope, receive, send)
