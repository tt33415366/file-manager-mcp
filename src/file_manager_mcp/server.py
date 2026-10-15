"""MCP server assembly: the Starlette app and startup (spec sections 1, 5, 12)."""

from __future__ import annotations

import asyncio
import errno
import os
import secrets
import socket
import sys
import threading
import webbrowser
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Mount

from .audit import AuditLog, HttpAuditMiddleware
from .auth import AuthMiddleware
from .config import Config, default_log_dir
from .runtime import EXECUTOR_WARNING, Runtime
from .tokens import TokenStore
from .ui import build_ui_app


class StartupError(Exception):
    pass


def build_app(runtime: Runtime) -> Callable:
    config, server = runtime.config, runtime.server
    origins = list(config.server.allowed_origins)
    mcp_app = server.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host=config.server.host,
    )

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with server.session_manager.run():
            yield

    inner = Starlette(
        routes=[*runtime.channel.routes, Mount("/", app=mcp_app)],
        lifespan=lifespan,
    )
    http_audit = HttpAuditMiddleware(inner, runtime.audit)
    return AuthMiddleware(
        http_audit,
        verify=runtime.tokens.verify,
        accepts_host=runtime.addressing.accepts_host,
        allowed_origins=origins,
        direction_of=runtime.channel.direction_of,
        verify_link=runtime.signer.verify,
        on_reject=http_audit.reject,
    )


def check_event_loop() -> None:
    """Async subprocesses on Windows need the Proactor loop; refuse to run on anything else."""
    if os.name == "nt" and not isinstance(asyncio.get_running_loop(), asyncio.ProactorEventLoop):
        raise StartupError("the server requires asyncio.ProactorEventLoop on Windows")


@dataclass
class Started:
    runtime: Runtime
    mcp_url: str
    ui_url: str
    servers: tuple[uvicorn.Server, uvicorn.Server]


class _Server(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        """Leave Ctrl+C to asyncio.Runner: it cancels run(), which stops both servers together."""
        yield


def _uvicorn(app: Callable) -> _Server:
    return _Server(uvicorn.Config(app, loop="none", lifespan="on", log_level="warning", timeout_graceful_shutdown=5))


def _listen(host: str, port: int) -> socket.socket:
    try:
        return socket.create_server((host, port), family=socket.AF_INET6 if ":" in host else socket.AF_INET)
    except OSError as exc:
        # Windows reports a port held with SO_EXCLUSIVEADDRUSE (or in an excluded range) as WSAEACCES.
        if exc.errno == errno.EADDRINUSE or (os.name == "nt" and exc.errno == errno.EACCES):
            raise StartupError(f"port {port} is in use; change server.port") from None
        raise StartupError(f"cannot listen on {host}:{port}: {exc}") from None


def _print_summary(started: Started) -> None:
    config = started.runtime.config
    print(f"MCP endpoint: {started.mcp_url}")
    for url in started.runtime.addressing.lan_urls():
        print(f"LAN: {url} (plain HTTP)")
    print(f"Config UI: {started.ui_url}", flush=True)
    if started.runtime.startup_token:
        print(f"Token 'default' created; it is shown only once: {started.runtime.startup_token}", flush=True)
    if not config.allowed_roots:
        print("WARNING: no Allowed Roots yet; add one in the config UI.", file=sys.stderr)
    if config.command_executor.enabled:
        print(EXECUTOR_WARNING, file=sys.stderr)
    print("Press Ctrl+C to stop.", flush=True)


async def _end_mcp_sessions(runtime: Runtime) -> None:
    """Open MCP streams never end on their own; uvicorn would wait out its graceful timeout, then cancel them noisily."""
    for transport in list(runtime.server.session_manager._server_instances.values()):
        await transport.terminate()


def _open_browser(url: str) -> None:
    try:
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    if not opened:
        print("WARNING: could not open a browser; open the Config UI URL above.", file=sys.stderr, flush=True)


async def run(
    config: Config,
    tokens: TokenStore,
    *,
    open_browser: bool,
    on_ready: Callable[[Started], None] | None = None,
) -> None:
    """Serve MCP on server.host:server.port and the config UI on 127.0.0.1 until either stops."""
    check_event_loop()
    mcp_listener = _listen(config.server.host, config.server.port)
    with mcp_listener as mcp_sock, socket.create_server(("127.0.0.1", 0)) as ui_sock:
        ui_port = ui_sock.getsockname()[1]
        audit_path = Path(config.logging.audit_file) if config.logging.audit_file else default_log_dir() / "audit.log"
        audit = AuditLog(audit_path)
        try:
            runtime = Runtime(config, tokens, audit)
            key = secrets.token_urlsafe(16)
            servers = (_uvicorn(build_app(runtime)), _uvicorn(build_ui_app(runtime, key, ui_port)))
            tasks = [asyncio.create_task(s.serve(sockets=[sock])) for s, sock in zip(servers, (mcp_sock, ui_sock))]
            try:
                while not all(s.started for s in servers) and not any(t.done() for t in tasks):
                    await asyncio.sleep(0.05)
                if all(s.started for s in servers):
                    if not tokens.list():
                        runtime.startup_token = tokens.add("default")
                    started = Started(runtime, runtime.addressing.mcp_url, f"http://127.0.0.1:{ui_port}/#k={key}", servers)
                    _print_summary(started)
                    if open_browser:
                        threading.Thread(target=_open_browser, args=(started.ui_url,), daemon=True).start()
                    if on_ready:
                        on_ready(started)
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                await _end_mcp_sessions(runtime)
                for server in servers:
                    server.should_exit = True
                await asyncio.wait(tasks)
            errors = [t.exception() for t in tasks if t.exception()]
            if not all(s.started for s in servers):
                detail = f": {errors[0]}" if errors else ""
                raise StartupError(f"the server stopped during startup{detail}") from (errors[0] if errors else None)
            if errors:
                raise errors[0]
        finally:
            audit.close()


def serve(
    config: Config,
    tokens: TokenStore,
    *,
    open_browser: bool,
    on_ready: Callable[[Started], None] | None = None,
) -> None:
    loop_factory = asyncio.ProactorEventLoop if os.name == "nt" else None
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(run(config, tokens, open_browser=open_browser, on_ready=on_ready))
