import asyncio
import json
import os
import signal
import socket
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager, contextmanager, suppress
from urllib.parse import quote, urlsplit

import httpx
import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from file_manager_mcp.audit import AuditLog
from file_manager_mcp.config import (
    CommandExecutorConfig,
    Config,
    LoggingConfig,
    RemoteConfig,
    ServerConfig,
    dump_config,
    load_config,
)
from file_manager_mcp import server as server_module
from file_manager_mcp.runtime import Runtime
from file_manager_mcp.server import StartupError, build_app, check_event_loop, run, serve
from file_manager_mcp.tokens import TokenStore

FILE_TOOLS = {
    "list_roots",
    "list_dir",
    "stat",
    "read_text",
    "write_text",
    "make_dir",
    "move",
    "copy",
    "delete",
    "search",
    "prepare_download",
    "prepare_upload",
}


def make_config(root, port=8765, executor=False, remote_hosts=()):
    return Config(
        allowed_roots=[str(root)],
        server=ServerConfig(port=port),
        command_executor=CommandExecutorConfig(enabled=executor),
        remote=RemoteConfig(allowed_hosts=list(remote_hosts)),
        logging=LoggingConfig(),
    )


@pytest.fixture
def tokens(tmp_path):
    return TokenStore(tmp_path / "tokens.json")


async def tool_names(server):
    async with Client(server) as c:
        return {t.name for t in (await c.list_tools()).tools}


# tool registration (in-process client)


@pytest.mark.anyio
async def test_default_server_exposes_only_file_tools(root, tokens):
    assert await tool_names(Runtime(make_config(root), tokens).server) == FILE_TOOLS


@pytest.mark.anyio
async def test_executor_tools_registered_only_when_enabled(root, tokens):
    names = await tool_names(Runtime(make_config(root, executor=True), tokens).server)
    assert names == FILE_TOOLS | {"run_command", "get_job", "cancel_job"}


@pytest.mark.anyio
async def test_remote_tools_registered_only_with_allowed_hosts(root, tokens):
    names = await tool_names(Runtime(make_config(root, remote_hosts=["buildbox"]), tokens).server)
    assert names == FILE_TOOLS | {"push_to_remote", "pull_from_remote", "get_job", "cancel_job"}


@pytest.mark.anyio
async def test_tool_annotations(root, tokens):
    async with Client(Runtime(make_config(root, executor=True), tokens).server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert tools["list_dir"].annotations.read_only_hint is True
    assert tools["delete"].annotations.destructive_hint is True
    assert tools["run_command"].annotations.destructive_hint is True
    assert tools["write_text"].annotations.read_only_hint is False


@pytest.mark.anyio
async def test_transfer_tools_tell_the_agent_about_for_host(root, tokens):
    server = Runtime(make_config(root), tokens).server
    async with Client(server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        instructions = c.instructions
    assert "for_host" in instructions
    for name in ("prepare_download", "prepare_upload"):
        assert "for_host" in tools[name].description
        assert {"for_host", "expires_in"} <= tools[name].input_schema["properties"].keys()
    upload = tools["prepare_upload"].description
    assert "64 MiB" in upload and "413" in upload
    assert "finished signed file's total" in upload
    assert "64 MiB" in instructions and "continuing from offset" in instructions


@pytest.mark.anyio
async def test_tool_success_is_structured(root, tokens):
    (root / "a.txt").write_text("hi")
    async with Client(Runtime(make_config(root), tokens).server) as c:
        r = await c.call_tool("read_text", {"path": str(root / "a.txt")})
    assert r.is_error is False
    assert r.structured_content["content"] == "hi"


@pytest.mark.anyio
async def test_tool_error_text_starts_with_code(root, tokens):
    async with Client(Runtime(make_config(root), tokens).server) as c:
        r = await c.call_tool("read_text", {"path": str(root / "missing.txt")})
    assert r.is_error is True
    assert r.content[0].text.startswith("NOT_FOUND:")


@pytest.mark.anyio
async def test_run_command_and_job_tools(root, tokens):
    async with Client(Runtime(make_config(root, executor=True), tokens).server) as c:
        r = await c.call_tool("run_command", {"command": "exit 2", "wait_sec": 30})
        assert r.structured_content["status"] == "exited"
        assert r.structured_content["exit_code"] == 2
        missing = await c.call_tool("get_job", {"job_id": "nope"})
        assert missing.content[0].text.startswith("JOB_NOT_FOUND:")


@pytest.mark.anyio
async def test_cancel_job_tool_cancels_running_command(root, tokens):
    sleep = "Start-Sleep -Seconds 30" if sys.platform == "win32" else "sleep 30"
    async with Client(Runtime(make_config(root, executor=True), tokens).server) as c:
        started = await c.call_tool("run_command", {"command": sleep, "wait_sec": 0})
        assert started.structured_content["status"] == "running"
        job_id = started.structured_content["job_id"]
        cancelled = await c.call_tool("cancel_job", {"job_id": job_id})
        assert cancelled.structured_content["status"] == "cancelled"
        again = await c.call_tool("get_job", {"job_id": job_id})
        assert again.structured_content["status"] == "cancelled"


@pytest.mark.anyio
async def test_remote_tools_enforce_host_allowlist(root, tokens):
    (root / "a.txt").write_text("x")
    async with Client(Runtime(make_config(root, remote_hosts=["buildbox"]), tokens).server) as c:
        push = await c.call_tool("push_to_remote", {"local_path": str(root / "a.txt"), "host": "evil", "remote_path": "/tmp/a"})
        pull = await c.call_tool("pull_from_remote", {"host": "evil", "remote_path": "/x", "local_path": str(root / "x")})
    assert push.content[0].text.startswith("HOST_NOT_ALLOWED:")
    assert pull.content[0].text.startswith("HOST_NOT_ALLOWED:")


@pytest.mark.anyio
async def test_every_path_tool_without_roots_returns_no_allowed_roots(root, tmp_path, tokens):
    config = make_config(root, executor=True, remote_hosts=["buildbox"])
    config.allowed_roots = []
    p, p2 = str(tmp_path / "a"), str(tmp_path / "b")
    calls = {
        "list_dir": {"path": p},
        "stat": {"path": p},
        "read_text": {"path": p},
        "write_text": {"path": p, "content": "x"},
        "make_dir": {"path": p},
        "move": {"source": p, "destination": p2},
        "copy": {"source": p, "destination": p2},
        "delete": {"path": p},
        "search": {"path": p},
        "prepare_download": {"path": p},
        "prepare_upload": {"path": p},
        "run_command": {"command": "echo hi"},
        "push_to_remote": {"local_path": p, "host": "buildbox", "remote_path": "/tmp/a"},
        "pull_from_remote": {"host": "buildbox", "remote_path": "/x", "local_path": p},
    }
    expected = "NO_ALLOWED_ROOTS: No Allowed Roots configured yet; ask the operator to add one in the config UI."
    async with Client(Runtime(config, tokens).server) as c:
        assert (await c.call_tool("list_roots", {})).structured_content == {"roots": []}
        for name, args in calls.items():
            r = await c.call_tool(name, args)
            assert r.is_error is True, name
            assert r.content[0].text == expected, name


# event loop and startup


@pytest.mark.skipif(sys.platform != "win32", reason="Proactor is Windows-only")
def test_check_event_loop_rejects_selector_loop():
    async def probe():
        check_event_loop()

    loop = asyncio.SelectorEventLoop()
    try:
        with pytest.raises(StartupError, match="ProactorEventLoop"):
            loop.run_until_complete(probe())
    finally:
        loop.close()


def test_check_event_loop_accepts_default_loop():
    async def probe():
        check_event_loop()

    asyncio.run(probe())


# end to end over real HTTP


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(root, tmp_path):
    port = free_port()
    config = make_config(root, port=port)
    config.logging.audit_file = str(tmp_path / "audit.log")
    store = TokenStore(tmp_path / "tokens.json")
    token = store.add("e2e")
    thread = threading.Thread(target=serve, args=(config, store), kwargs={"open_browser": False}, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(base + "/mcp", timeout=0.2)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    return base, token, tmp_path / "audit.log"


def test_http_requires_token(live_server):
    base, _, _ = live_server
    assert httpx.post(base + "/mcp", json={}).status_code == 401
    assert httpx.get(base + "/files/download?path=x").status_code == 401


def test_http_rejects_unknown_host(live_server):
    base, token, _ = live_server
    r = httpx.get(base + "/mcp", headers={"Authorization": f"Bearer {token}", "Host": "evil.example"})
    assert r.status_code == 421


def test_end_to_end_mcp_and_file_channel(live_server, root):
    base, token, audit_file = live_server
    auth = {"Authorization": f"Bearer {token}"}

    async def scenario():
        http = httpx2.AsyncClient(headers=auth, timeout=30)
        async with Client(streamable_http_client(base + "/mcp", http_client=http)) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert names == FILE_TOOLS
            r = await c.call_tool("write_text", {"path": str(root / "hello.txt"), "content": "secret body"})
            assert r.is_error is False
            link = await c.call_tool("prepare_download", {"path": str(root / "hello.txt")})
            return link.structured_content["url"]

    url = asyncio.run(scenario())

    assert url.startswith(base + "/files/download?path=")
    assert httpx.get(url, headers=auth).content == b"secret body"

    target = quote(str(root / "up.bin"), safe="")
    assert httpx.put(f"{base}/files/upload?path={target}&offset=0", content=b"abc", headers=auth).json() == {
        "offset": 3
    }
    assert httpx.post(f"{base}/files/upload/complete?path={target}", headers=auth).status_code == 200
    assert (root / "up.bin").read_bytes() == b"abc"

    lines = [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines()]
    tool_lines = [line for line in lines if line["kind"] == "tool"]
    assert any(line["name"] == "write_text" and line["token"] == "e2e" and line["outcome"] == "ok" for line in tool_lines)
    assert any(line["kind"] == "http" and line["name"] == "PUT /files/upload" for line in lines)
    raw = audit_file.read_text(encoding="utf-8")
    assert "secret body" not in raw
    assert token not in raw


def test_build_app_rejects_without_token(root, tmp_path, tokens):
    from starlette.testclient import TestClient

    app = build_app(Runtime(make_config(root), tokens, AuditLog(tmp_path / "a.log")))
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/files/download?path=x").status_code == 401


def mcp_session(client):
    headers = {"Accept": "application/json, text/event-stream"}
    init = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
        },
    )
    headers["mcp-session-id"] = init.headers["mcp-session-id"]
    client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(name, arguments):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        r = client.post("/mcp", headers=headers, json=body)
        data = next(line for line in r.text.splitlines() if line.startswith("data: "))
        return json.loads(data.removeprefix("data: "))["result"]

    return call


def test_lan_client_reaches_mcp_with_an_own_ip_host(root, tmp_path, tokens, interfaces):
    from starlette.testclient import TestClient

    interfaces.own = {"192.168.50.1"}
    token = tokens.add("lan")
    app = build_app(Runtime(make_config(root), tokens, AuditLog(tmp_path / "a.log"), interfaces))

    with TestClient(app, base_url="http://192.168.50.1:8765", headers={"Authorization": f"Bearer {token}"}) as client:
        call = mcp_session(client)
        assert call("list_roots", {})["structuredContent"] == {"roots": [os.path.realpath(root)]}


def lan_session(runtime, tokens, interfaces, host="192.168.50.1:8765"):
    from starlette.testclient import TestClient

    interfaces.own = {host.partition(":")[0]}
    token = tokens.add("lan")
    client = TestClient(build_app(runtime), base_url=f"http://{host}", headers={"Authorization": f"Bearer {token}"})
    return client


def test_file_channel_links_use_the_host_the_agent_called(root, tmp_path, tokens, interfaces):
    (root / "a.txt").write_text("hi")
    runtime = Runtime(make_config(root), tokens, AuditLog(tmp_path / "a.log"), interfaces)

    with lan_session(runtime, tokens, interfaces) as client:
        call = mcp_session(client)
        down = call("prepare_download", {"path": str(root / "a.txt")})["structuredContent"]
        up = call("prepare_upload", {"path": str(root / "b.txt")})["structuredContent"]
        assert client.get(down["url"]).content == b"hi"

    assert down["url"].startswith("http://192.168.50.1:8765/files/download?path=")
    assert up["upload_url"].startswith("http://192.168.50.1:8765/files/upload?path=")


def test_public_base_url_wins_over_the_request_host(root, tmp_path, tokens, interfaces):
    (root / "a.txt").write_text("hi")
    config = make_config(root)
    config.server.public_base_url = "https://fm.example.com"
    runtime = Runtime(config, tokens, AuditLog(tmp_path / "a.log"), interfaces)

    with lan_session(runtime, tokens, interfaces) as client:
        down = mcp_session(client)("prepare_download", {"path": str(root / "a.txt")})["structuredContent"]

    assert down["url"].startswith("https://fm.example.com/files/download?path=")


@pytest.mark.anyio
async def test_in_memory_links_use_the_configured_base_url(root, tokens):
    (root / "a.txt").write_text("hi")
    async with Client(Runtime(make_config(root), tokens).server) as c:
        r = await c.call_tool("prepare_download", {"path": str(root / "a.txt")})
    assert r.structured_content["url"].startswith("http://127.0.0.1:8765/files/download?path=")


def test_dut_downloads_with_a_signed_link_and_no_token(root, tmp_path, tokens, interfaces):
    from starlette.testclient import TestClient

    (root / "fw.bin").write_bytes(b"firmware")
    config = make_config(root)
    config.server.host = "0.0.0.0"
    runtime = Runtime(config, tokens, AuditLog(tmp_path / "a.log"), interfaces)
    interfaces.source = "192.168.50.1"

    with lan_session(runtime, tokens, interfaces) as client:
        call = mcp_session(client)
        link = call("prepare_download", {"path": str(root / "fw.bin"), "for_host": "192.168.50.10"})["structuredContent"]
        dut = TestClient(client.app)
        assert dut.get(link["url"]).content == b"firmware"
        assert dut.get(link["url"]).status_code == 200
        tampered = dut.get(link["url"].replace("fw.bin", "fw.bim"))
        assert (tampered.status_code, tampered.json()) == (403, {"error": "invalid_link"})

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        runtime.policy.set_roots([str(elsewhere)])
        moved = dut.get(link["url"])
        assert (moved.status_code, moved.json()["error"]) == (403, "OUTSIDE_ROOTS")

    lines = [json.loads(line) for line in (tmp_path / "a.log").read_text(encoding="utf-8").splitlines()]
    signed = [line for line in lines if line["kind"] == "http" and line["token"] == "signed:lan"]
    assert signed and all(line["client"] for line in signed)


@contextmanager
def dut_upload(root, tmp_path, tokens, interfaces, **link_args):
    from starlette.testclient import TestClient

    config = make_config(root)
    config.server.host = "0.0.0.0"
    runtime = Runtime(config, tokens, AuditLog(tmp_path / "a.log"), interfaces)
    interfaces.source = "192.168.50.1"
    with lan_session(runtime, tokens, interfaces) as client:
        args = {"path": str(root / "log.txt"), "for_host": "192.168.50.10", **link_args}
        link = mcp_session(client)("prepare_upload", args)["structuredContent"]
        yield TestClient(client.app), link


def test_dut_uploads_with_a_signed_link_which_is_then_void(root, tmp_path, tokens, interfaces):
    with dut_upload(root, tmp_path, tokens, interfaces) as (dut, link):
        assert dut.put(link["upload_url"] + "&offset=0", content=b"log").json() == {"offset": 3}
        assert dut.post(link["complete_url"]).status_code == 200
        assert (root / "log.txt").read_bytes() == b"log"

        for r in (dut.put(link["upload_url"] + "&offset=0", content=b"x"), dut.get(link["upload_url"])):
            assert (r.status_code, r.json()) == (403, {"error": "invalid_link"})


def test_signed_upload_keeps_its_overwrite_setting(root, tmp_path, tokens, interfaces):
    (root / "log.txt").write_bytes(b"old")
    with dut_upload(root, tmp_path, tokens, interfaces) as (dut, link):
        dut.put(link["upload_url"] + "&offset=0", content=b"new")
        assert dut.post(link["complete_url"] + "&overwrite=true").status_code == 403
        assert dut.post(link["complete_url"]).status_code == 409
    assert (root / "log.txt").read_bytes() == b"old"


def test_revoking_the_issuing_token_voids_its_signed_links(root, tmp_path, tokens, interfaces):
    with dut_upload(root, tmp_path, tokens, interfaces) as (dut, link):
        assert dut.get(link["upload_url"]).status_code == 200
        tokens.remove("lan")
        r = dut.get(link["upload_url"])
        assert (r.status_code, r.json()) == (403, {"error": "invalid_link"})


def test_signed_complete_refuses_a_partial_beyond_max_bytes(root, tmp_path, tokens, interfaces):
    (root / "log.txt.fmcp-partial").write_bytes(b"0123456789")
    with dut_upload(root, tmp_path, tokens, interfaces, max_bytes=5) as (dut, link):
        assert dut.post(link["complete_url"]).status_code == 413
    assert not (root / "log.txt").exists()


def test_signed_upload_enforces_max_bytes(root, tmp_path, tokens, interfaces):
    with dut_upload(root, tmp_path, tokens, interfaces, max_bytes=5) as (dut, link):
        assert dut.put(link["upload_url"] + "&offset=0", content=b"123").json() == {"offset": 3}
        assert dut.put(link["upload_url"] + "&offset=3", content=b"456").status_code == 413
        assert dut.put(link["upload_url"] + "&offset=3", content=b"45").json() == {"offset": 5}


def summary(config, tokens, capsys, interfaces):
    runtime = Runtime(config, tokens, interfaces=interfaces)
    server_module._print_summary(server_module.Started(runtime, "http://127.0.0.1:1/mcp", "ui", ()))
    return capsys.readouterr().out


def test_summary_lists_lan_urls_when_listening_on_all_interfaces(root, tokens, capsys, interfaces):
    interfaces.own = {"192.168.1.5", "10.0.0.2"}
    config = make_config(root, port=8865)
    config.server.host = "0.0.0.0"

    out = summary(config, tokens, capsys, interfaces)

    assert "LAN: http://10.0.0.2:8865/mcp (plain HTTP)" in out
    assert "LAN: http://192.168.1.5:8865/mcp (plain HTTP)" in out


def test_summary_has_no_lan_urls_on_loopback(root, tokens, capsys, interfaces):
    interfaces.own = {"192.168.1.5"}
    assert "LAN:" not in summary(make_config(root, port=8865), tokens, capsys, interfaces)


def test_replaced_roots_apply_to_file_tools_file_channel_and_run_command(root, tmp_path, tokens):
    from starlette.testclient import TestClient

    new_root = tmp_path / "new"
    new_root.mkdir()
    (root / "old.txt").write_text("old")
    (new_root / "new.txt").write_text("new")
    token = tokens.add("t")
    runtime = Runtime(make_config(root, executor=True), tokens, AuditLog(tmp_path / "a.log"))
    app = build_app(runtime)
    runtime.policy.set_roots([str(new_root)])

    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"Authorization": f"Bearer {token}"}) as client:
        assert client.get("/files/download", params={"path": str(root / "old.txt")}).status_code == 403
        assert client.get("/files/download", params={"path": str(new_root / "new.txt")}).content == b"new"

        call = mcp_session(client)
        assert call("read_text", {"path": str(root / "old.txt")})["content"][0]["text"].startswith("OUTSIDE_ROOTS:")
        assert call("read_text", {"path": str(new_root / "new.txt")})["structuredContent"]["content"] == "new"
        pwd = "(Get-Location).Path" if sys.platform == "win32" else "pwd"
        stdout = call("run_command", {"command": pwd, "wait_sec": 30})["structuredContent"]["stdout"]
    assert os.path.normcase(stdout.strip()) == os.path.normcase(os.path.realpath(new_root))


# single process: MCP and config UI listeners on one loop


def live_config(tmp_path, roots, executor=False):
    config = make_config(tmp_path, port=free_port(), executor=executor)
    config.allowed_roots = [str(r) for r in roots]
    config.logging.audit_file = str(tmp_path / "audit.log")
    path = tmp_path / "config.toml"
    path.write_text(dump_config(config), encoding="utf-8")
    return load_config(path)


@asynccontextmanager
async def running(config, tokens, open_browser=False):
    ready = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(run(config, tokens, open_browser=open_browser, on_ready=ready.set_result))
    await asyncio.wait([task, ready], timeout=30, return_when=asyncio.FIRST_COMPLETED)
    if task.done():
        task.result()
    try:
        yield ready.result()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def ui_client(started):
    url = urlsplit(started.ui_url)
    key = url.fragment.removeprefix("k=")
    return httpx.AsyncClient(base_url=f"http://127.0.0.1:{url.port}", headers={"X-UI-Key": key}, timeout=30)


async def mcp_tool_names(url, token):
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as http:
        async with Client(streamable_http_client(url, http_client=http)) as c:
            return {t.name for t in (await c.list_tools()).tools}


def port_is_free(port):
    with suppress(OSError), socket.create_server(("127.0.0.1", port)):
        return True
    return False


@pytest.mark.anyio
async def test_ui_issued_token_works_and_ui_save_enables_executor_live(root, tmp_path, tokens):
    config = live_config(tmp_path, [root])
    (root / "a.txt").write_text("hi")

    async with running(config, tokens) as started, ui_client(started) as ui:
        token = (await ui.post("/api/tokens", json={"name": "e2e"})).json()["token"]
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as http:
            async with Client(streamable_http_client(started.mcp_url, http_client=http)) as c:
                assert {t.name for t in (await c.list_tools()).tools} == FILE_TOOLS
                r = await c.call_tool("read_text", {"path": str(root / "a.txt")})
                assert r.structured_content["content"] == "hi"

        body = (await ui.get("/api/config")).json()["config"]
        body["command_executor"]["enabled"] = True
        r = await ui.put("/api/config", json=body)
        assert r.status_code == 200
        assert r.json() == {"applied": ["command_executor.enabled"], "restart_required": []}

        assert "run_command" in await mcp_tool_names(started.mcp_url, token)


@pytest.mark.anyio
async def test_token_revoked_in_ui_is_rejected_on_next_mcp_request(root, tmp_path, tokens):
    token = tokens.add("laptop")

    async with running(live_config(tmp_path, [root]), tokens) as started, ui_client(started) as ui:
        assert await mcp_tool_names(started.mcp_url, token) == FILE_TOOLS
        assert (await ui.delete("/api/tokens/laptop")).status_code == 200
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
            assert (await http.post(started.mcp_url, json={})).status_code == 401


@pytest.mark.anyio
async def test_prints_summary_with_warnings_then_opens_browser(tmp_path, tokens, capsys, monkeypatch):
    opened = threading.Event()
    urls = []
    monkeypatch.setattr(webbrowser, "open", lambda url: urls.append(url) or opened.set() or True)
    config = live_config(tmp_path, [], executor=True)

    async with running(config, tokens, open_browser=True) as started:
        assert await asyncio.to_thread(opened.wait, 10)
        out, err = capsys.readouterr()

    assert urls == [started.ui_url]
    assert started.mcp_url == f"http://127.0.0.1:{config.server.port}/mcp"
    assert started.ui_url.startswith("http://127.0.0.1:") and "/#k=" in started.ui_url
    assert f"MCP endpoint: {started.mcp_url}" in out
    assert f"Config UI: {started.ui_url}" in out
    assert "no Allowed Roots yet" in err
    assert "no tokens yet" not in err
    assert "Command Executor is enabled" in err


@pytest.mark.anyio
async def test_first_start_creates_default_token_shown_once_and_never_stored(root, tmp_path, capsys):
    config = live_config(tmp_path, [root])
    tokens_path = tmp_path / "tokens.json"

    async with running(config, TokenStore(tokens_path)) as started, ui_client(started) as ui:
        out, err = capsys.readouterr()
        handoff = (await ui.get("/api/startup-token")).json()
        token = handoff["token"]
        assert handoff == {"name": "default", "token": token, "mcp_url": started.mcp_url}
        assert (await ui.get("/api/startup-token")).json() == {"token": None}
        assert await mcp_tool_names(started.mcp_url, token) == FILE_TOOLS

    assert [e.name for e in TokenStore(tokens_path).list()] == ["default"]
    assert TokenStore(tokens_path).verify(token) == "default"
    assert (out + err).count(token) == 1
    assert (tmp_path / "audit.log").is_file()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert token not in path.read_text(encoding="utf-8", errors="replace"), path

    async with running(config, TokenStore(tokens_path)) as again, ui_client(again) as ui:
        out, err = capsys.readouterr()
        assert again.runtime.startup_token is None
        assert (await ui.get("/api/startup-token")).json() == {"token": None}

    assert [e.name for e in TokenStore(tokens_path).list()] == ["default"]
    assert "fmcp_" not in out + err
    assert "default" not in out + err


@pytest.mark.anyio
async def test_browser_open_failure_is_only_a_warning(root, tmp_path, tokens, capsys, monkeypatch):
    def fail(url):
        raise webbrowser.Error("no browser")

    monkeypatch.setattr(webbrowser, "open", fail)
    tokens.add("laptop")

    async with running(live_config(tmp_path, [root]), tokens, open_browser=True) as started, ui_client(started) as ui:
        err = ""
        for _ in range(100):
            err += capsys.readouterr().err
            if "could not open a browser" in err:
                break
            await asyncio.sleep(0.05)
        assert "could not open a browser" in err
        assert (await ui.get("/")).status_code == 200


@pytest.mark.anyio
async def test_no_browser_does_not_open_browser(root, tmp_path, tokens, monkeypatch):
    urls = []
    monkeypatch.setattr(webbrowser, "open", lambda url: urls.append(url) or True)

    async with running(live_config(tmp_path, [root]), tokens) as started, ui_client(started) as ui:
        assert (await ui.get("/")).status_code == 200

    assert urls == []


@pytest.mark.anyio
async def test_mcp_port_in_use_fails_before_printing_or_opening_browser(root, tmp_path, tokens, capsys, monkeypatch):
    urls = []
    monkeypatch.setattr(webbrowser, "open", lambda url: urls.append(url) or True)
    config = live_config(tmp_path, [root])

    with socket.create_server(("127.0.0.1", config.server.port)):
        with pytest.raises(StartupError, match=f"^port {config.server.port} is in use; change server.port$"):
            await run(config, tokens, open_browser=True)

    assert urls == []
    assert "Config UI" not in capsys.readouterr().out
    assert TokenStore(tmp_path / "tokens.json").list() == []


@pytest.mark.skipif(sys.platform != "win32", reason="SO_EXCLUSIVEADDRUSE is Windows-only")
@pytest.mark.anyio
async def test_exclusively_bound_port_is_reported_as_in_use(root, tmp_path, tokens):
    config = live_config(tmp_path, [root])
    with socket.socket() as holder:
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        holder.bind(("0.0.0.0", config.server.port))
        holder.listen()
        with pytest.raises(StartupError, match=f"^port {config.server.port} is in use; change server.port$"):
            await run(config, tokens, open_browser=False)


@pytest.mark.anyio
async def test_other_bind_errors_name_the_address(root, tmp_path, tokens):
    config = live_config(tmp_path, [root])
    config.server.host = "192.0.2.1"

    with pytest.raises(StartupError, match=rf"^cannot listen on 192\.0\.2\.1:{config.server.port}: .+"):
        await run(config, tokens, open_browser=False)


@pytest.mark.anyio
async def test_server_failing_during_startup_surfaces_its_error(root, tmp_path, tokens, monkeypatch):
    async def boom(self, sockets=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(server_module._Server, "startup", boom)
    config = live_config(tmp_path, [root])

    with pytest.raises(StartupError, match="the server stopped during startup: boom") as exc:
        await run(config, tokens, open_browser=False)

    assert isinstance(exc.value.__cause__, RuntimeError)
    assert port_is_free(config.server.port)


@pytest.mark.anyio
async def test_first_boot_startup_failure_creates_no_token(root, tmp_path, capsys, monkeypatch):
    async def boom(self, sockets=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(server_module._Server, "startup", boom)
    config = live_config(tmp_path, [root])
    tokens_path = tmp_path / "tokens.json"

    with pytest.raises(StartupError):
        await run(config, TokenStore(tokens_path), open_browser=False)
    assert TokenStore(tokens_path).list() == []

    monkeypatch.undo()
    capsys.readouterr()
    async with running(config, TokenStore(tokens_path)) as started:
        token = started.runtime.startup_token
        out = capsys.readouterr().out

    assert [e.name for e in TokenStore(tokens_path).list()] == ["default"]
    assert f"Token 'default' created; it is shown only once: {token}" in out
    assert port_is_free(config.server.port)


@pytest.mark.anyio
async def test_when_one_server_stops_both_stop(root, tmp_path, tokens):
    config = live_config(tmp_path, [root])
    ready = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(run(config, tokens, open_browser=False, on_ready=ready.set_result))
    started = await asyncio.wait_for(ready, 30)
    ui_port = urlsplit(started.ui_url).port

    started.servers[1].should_exit = True
    await asyncio.wait_for(task, 30)

    assert port_is_free(config.server.port)
    assert port_is_free(ui_port)


@pytest.mark.anyio
async def test_stopping_with_an_open_mcp_stream_is_prompt_and_quiet(root, tmp_path, tokens, capsys):
    config = live_config(tmp_path, [root])
    token = tokens.add("e2e")
    ready = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(run(config, tokens, open_browser=False, on_ready=ready.set_result))
    started = await asyncio.wait_for(ready, 30)
    url = started.mcp_url
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    opened = asyncio.Event()

    async with httpx.AsyncClient(headers=headers, timeout=30) as http:
        r = await http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
        session = {"mcp-session-id": r.headers["mcp-session-id"]}
        await http.post(url, headers=session, json={"jsonrpc": "2.0", "method": "notifications/initialized"})

        async def listen():
            async with http.stream("GET", url, headers=session) as resp:
                opened.set()
                async for _ in resp.aiter_bytes():
                    pass

        listener = asyncio.create_task(listen())
        await asyncio.wait_for(opened.wait(), 10)
        began = time.monotonic()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 30)
        elapsed = time.monotonic() - began
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)

    err = capsys.readouterr().err
    assert elapsed < 3
    assert "Traceback" not in err and "graceful shutdown exceeded" not in err


def test_ctrl_c_stops_both_servers_and_closes_audit_log(root, tmp_path, tokens, monkeypatch):
    closed = []
    real_close = AuditLog.close
    monkeypatch.setattr(AuditLog, "close", lambda self: closed.append(self) or real_close(self))
    config = live_config(tmp_path, [root])
    seen = []

    def on_ready(started):
        seen.append(started)
        signal.raise_signal(signal.SIGINT)

    with pytest.raises(KeyboardInterrupt):
        serve(config, tokens, open_browser=False, on_ready=on_ready)

    assert closed == [seen[0].runtime.audit]
    assert port_is_free(config.server.port)
    assert port_is_free(urlsplit(seen[0].ui_url).port)
