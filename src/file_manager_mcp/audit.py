"""Audit logging: one JSON line per authenticated HTTP request and per tool call (spec section 10)."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from .auth import caller_of

_CODE = re.compile(r"^[A-Z_]+(?=:)")


def redact_args(args: dict[str, Any] | None) -> dict[str, Any]:
    """Drop file content and env values; keep path-like arguments and the command string."""
    out: dict[str, Any] = {}
    for key, value in (args or {}).items():
        if key == "content":
            continue
        if key == "env":
            out[key] = sorted(value) if isinstance(value, dict) else []
            continue
        out[key] = value
    return out


class AuditLog:
    def __init__(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handler = RotatingFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
        self._logger = logging.Logger("file_manager_mcp.audit")
        self._logger.addHandler(self._handler)

    def write(self, **fields: Any) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **fields}
        self._logger.info(json.dumps(record, ensure_ascii=False, default=str))

    def close(self) -> None:
        self._handler.close()


def _outcome(result: Any) -> str:
    if isinstance(result, dict):
        is_error, content = result.get("isError"), result.get("content") or []
    else:
        is_error, content = getattr(result, "is_error", False), getattr(result, "content", None) or []
    if not is_error:
        return "ok"
    first = content[0] if content else None
    text = first.get("text", "") if isinstance(first, dict) else getattr(first, "text", "")
    match = _CODE.match(text or "")
    return match.group(0) if match else "error"


def tool_audit_middleware(audit: AuditLog) -> Callable:
    """MCPServer middleware that records every tools/call."""

    async def middleware(ctx: Any, call_next: Callable) -> Any:
        if ctx.method != "tools/call":
            return await call_next(ctx)
        params = dict(ctx.params or {})
        caller = caller_of(ctx.request)
        line = {
            "token": caller.audit_name if caller else None,
            "kind": "tool",
            "name": params.get("name"),
            "args": redact_args(params.get("arguments")),
        }
        start = time.monotonic()
        try:
            result = await call_next(ctx)
        except Exception as exc:
            audit.write(**line, outcome=type(exc).__name__, duration_ms=int((time.monotonic() - start) * 1000))
            raise
        audit.write(**line, outcome=_outcome(result), duration_ms=int((time.monotonic() - start) * 1000))
        return result

    return middleware


class HttpAuditMiddleware:
    """ASGI middleware (inside auth) recording each authenticated HTTP request."""

    def __init__(self, app: Callable, audit: AuditLog) -> None:
        self.app = app
        self.audit = audit

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = {"code": 500}

        async def capture(message: dict) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        start = time.monotonic()
        try:
            await self.app(scope, receive, capture)
        finally:
            query = {k: v[0] for k, v in parse_qs(scope.get("query_string", b"").decode("latin-1")).items()}
            caller, client = caller_of(scope), scope.get("client")
            self.audit.write(
                token=caller.audit_name if caller else None,
                kind="http",
                name=f"{scope['method']} {scope['path']}",
                args={"path": query["path"]} if "path" in query else {},
                outcome=status["code"],
                duration_ms=int((time.monotonic() - start) * 1000),
                **({"client": client[0] if client else None} if caller and caller.link else {}),
            )

    def reject(self, scope: dict, status: int) -> None:
        client = scope.get("client")
        self.audit.write(
            token=None,
            kind="http",
            name=f"{scope.get('method')} {scope.get('path')}",
            args={},
            outcome=status,
            client=client[0] if client else None,
        )
