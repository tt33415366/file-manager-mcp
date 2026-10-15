"""Local configuration UI: loopback-only pages for config.toml, tokens.json, and the MCP setup guide.

Served on its own 127.0.0.1 listener next to the MCP listener, never on the MCP port, so it is not
reachable through a tunnel. Every API call must carry the per-launch key. Saves apply to the
shared Runtime.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import tempfile
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from .config import Config, ConfigError, config_from_dict, config_to_dict, dump_config, load_config
from .guide import setup_payload
from .runtime import RESTART_FIELDS, RESTART_SECTIONS, Runtime
from .tokens import TokenError

KEY_HEADER = "x-ui-key"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def build_ui_app(runtime: Runtime, key: str, port: int) -> Callable:
    config_path = runtime.config.path
    tokens = runtime.tokens
    save_lock = asyncio.Lock()
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    package = resources.files("file_manager_mcp")
    page = package.joinpath("ui.html").read_text(encoding="utf-8")
    setup_page = package.joinpath("setup.html").read_text(encoding="utf-8")
    stylesheet = package.joinpath("ui.css").read_text(encoding="utf-8")

    async def index(_request: Request) -> Response:
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    async def setup(_request: Request) -> Response:
        return HTMLResponse(setup_page, headers={"Cache-Control": "no-store"})

    async def css(_request: Request) -> Response:
        return Response(stylesheet, media_type="text/css", headers={"Cache-Control": "no-store"})

    async def get_setup(_request: Request) -> Response:
        return JSONResponse(setup_payload(runtime.config, runtime.addressing))

    async def get_config(_request: Request) -> Response:
        body: dict[str, Any] = {"path": str(config_path), "exists": config_path.exists()}
        try:
            body["config"] = config_to_dict(load_config(config_path))
        except ConfigError as exc:
            body["config"] = config_to_dict(Config(allowed_roots=[]))
            if body["exists"]:
                body["error"] = str(exc)
        running = config_to_dict(runtime.config)
        body["running"] = {section: running[section] for section in RESTART_SECTIONS}
        body["restart_fields"] = list(RESTART_FIELDS)
        return JSONResponse(body)

    async def put_config(request: Request) -> Response:
        try:
            cfg = config_from_dict(await request.json(), config_path)
        except ConfigError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": f"invalid request body: {exc}"}, status_code=400)
        async with save_lock:
            _write_atomic(config_path, dump_config(cfg))
            return JSONResponse(await runtime.apply(cfg))

    async def check_dir(request: Request) -> Response:
        return JSONResponse({"exists": os.path.isdir(request.query_params.get("path", ""))})

    async def list_tokens(_request: Request) -> Response:
        entries = tokens.list()
        return JSONResponse({"tokens": [{"name": e.name, "created": e.created} for e in entries]})

    async def add_token(request: Request) -> Response:
        name = str((await request.json()).get("name", "")).strip()
        if not name:
            return JSONResponse({"error": "token name is required"}, status_code=400)
        try:
            token = tokens.add(name)
        except TokenError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse({"name": name, "token": token})

    async def remove_token(request: Request) -> Response:
        try:
            tokens.remove(request.path_params["name"])
        except TokenError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        return JSONResponse({"removed": True})

    async def startup_token(_request: Request) -> Response:
        token, runtime.startup_token = runtime.startup_token, None
        if token is None:
            return JSONResponse({"token": None})
        return JSONResponse({"name": "default", "token": token, "mcp_url": runtime.addressing.mcp_url})

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/setup", setup),
            Route("/ui.css", css),
            Route("/api/setup", get_setup, methods=["GET"]),
            Route("/api/config", get_config, methods=["GET"]),
            Route("/api/config", put_config, methods=["PUT"]),
            Route("/api/check-dir", check_dir),
            Route("/api/tokens", list_tokens, methods=["GET"]),
            Route("/api/tokens", add_token, methods=["POST"]),
            Route("/api/tokens/{name}", remove_token, methods=["DELETE"]),
            Route("/api/startup-token", startup_token, methods=["GET"]),
        ]
    )

    async def guarded(scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            if headers.get("host", "").lower() not in allowed_hosts:
                await JSONResponse({"error": "invalid host"}, status_code=421)(scope, receive, send)
                return
            if scope["path"].startswith("/api/") and not hmac.compare_digest(headers.get(KEY_HEADER, ""), key):
                await JSONResponse({"error": "missing or wrong UI key"}, status_code=403)(scope, receive, send)
                return
        await app(scope, receive, send)

    return guarded
