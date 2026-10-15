import asyncio
import re

import httpx
import pytest
from starlette.testclient import TestClient

from file_manager_mcp.config import Config, load_config
from file_manager_mcp.runtime import RESTART_FIELDS, Runtime
from file_manager_mcp.tokens import TokenStore
from file_manager_mcp.ui import build_ui_app

KEY = {"X-UI-Key": "k123"}


@pytest.fixture
def cfg_path(tmp_path):
    return tmp_path / "conf" / "config.toml"


@pytest.fixture
def runtime(cfg_path, interfaces):
    config = Config(allowed_roots=[], path=cfg_path)
    return Runtime(config, TokenStore(cfg_path.parent / "tokens.json"), interfaces=interfaces)


@pytest.fixture
def client(runtime):
    return TestClient(build_ui_app(runtime, key="k123", port=4321), base_url="http://127.0.0.1:4321")


def valid_body(root):
    return {
        "allowed_roots": [str(root)],
        "server": {
            "host": "127.0.0.1",
            "port": 8765,
            "public_base_url": "",
            "allowed_hosts": [],
            "allowed_origins": [],
        },
        "command_executor": {"enabled": False, "shell": ""},
        "remote": {"allowed_hosts": []},
        "logging": {"audit_file": ""},
    }


def test_page_is_served_without_key(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "File Manager MCP" in r.text


def test_setup_page_and_stylesheet_are_served_without_key(client):
    r = client.get("/setup")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Setup" in r.text
    css = client.get("/ui.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert client.get("/setup", headers={"Host": "evil.example"}).status_code == 421


def test_setup_api_requires_key(client):
    assert client.get("/api/setup").status_code == 403


def test_setup_api_returns_running_values(client, runtime, root):
    runtime.config.server.public_base_url = "https://fm.example.com"
    runtime.config.server.allowed_hosts = ["fm.example.com"]
    runtime.config.allowed_roots = [str(root)]

    body = client.get("/api/setup", headers=KEY).json()

    assert body["mcp_url"] == "https://fm.example.com/mcp"
    assert body["local_url"] == "http://127.0.0.1:8765"
    assert body["port"] == 8765
    assert body["public_base_url"] == "https://fm.example.com"
    assert body["allowed_hosts"] == ["fm.example.com"]
    assert body["roots"] == 1
    assert body["executor_enabled"] is False
    assert body["remote_enabled"] is False
    assert "list_roots" in body["agent_rule"]
    assert "run_command" not in body["agent_rule"]
    assert "push_to_remote" not in body["agent_rule"]


def test_setup_api_lists_lan_addresses_and_listener(client, runtime, interfaces):
    interfaces.own, interfaces.default = {"192.168.50.1", "10.0.0.2"}, "192.168.50.1"
    runtime.config.server.host = "0.0.0.0"

    body = client.get("/api/setup", headers=KEY).json()

    assert body["lan_ips"] == ["10.0.0.2", "192.168.50.1"]
    assert body["lan_default"] == "192.168.50.1"
    assert body["listen_host"] == "0.0.0.0"
    assert body["listens_on_lan"] is True
    assert "for_host" in body["agent_rule"]


def test_setup_agent_rule_follows_enabled_features(client, runtime):
    runtime.config.command_executor.enabled = True
    runtime.config.remote.allowed_hosts = ["buildbox"]

    body = client.get("/api/setup", headers=KEY).json()

    assert body["executor_enabled"] is True
    assert body["remote_enabled"] is True
    assert "run_command" in body["agent_rule"]
    assert "push_to_remote" in body["agent_rule"]


def test_api_requires_key(client):
    assert client.get("/api/config").status_code == 403
    assert client.get("/api/config", headers={"X-UI-Key": "wrong"}).status_code == 403


def test_foreign_host_is_rejected(client):
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 421
    assert client.get("/api/config", headers={**KEY, "Host": "evil.example:4321"}).status_code == 421


def test_missing_config_returns_defaults(client, cfg_path):
    r = client.get("/api/config", headers=KEY)
    body = r.json()
    assert body["exists"] is False
    assert body["path"] == str(cfg_path)
    assert body["config"]["allowed_roots"] == []
    assert body["config"]["server"]["port"] == 8765


def test_save_valid_config_writes_file_and_applies_it(client, cfg_path, root, runtime):
    body = valid_body(root)
    body["command_executor"]["enabled"] = True

    r = client.put("/api/config", json=body, headers=KEY)

    assert r.status_code == 200
    assert r.json() == {"applied": ["allowed_roots", "command_executor.enabled"], "restart_required": []}
    saved = load_config(cfg_path)
    assert saved.allowed_roots == [str(root)]
    assert saved.command_executor.enabled is True
    assert runtime.config.command_executor.enabled is True
    assert client.get("/api/config", headers=KEY).json()["exists"] is True


def test_save_reports_restart_required_and_get_returns_saved_file(client, root, runtime):
    body = valid_body(root)
    body["server"]["port"] = 9999

    r = client.put("/api/config", json=body, headers=KEY)

    assert r.json()["restart_required"] == ["server.port"]
    assert runtime.config.server.port == 8765
    assert client.get("/api/config", headers=KEY).json()["config"]["server"]["port"] == 9999


def test_get_returns_running_server_and_logging(client, root, tmp_path):
    body = valid_body(root)
    body["server"]["port"] = 9999
    body["server"]["allowed_hosts"] = ["fm.example.com"]
    body["logging"]["audit_file"] = str(tmp_path / "audit.jsonl")
    client.put("/api/config", json=body, headers=KEY)

    got = client.get("/api/config", headers=KEY).json()

    assert got["running"] == {
        "server": {
            "host": "127.0.0.1",
            "port": 8765,
            "public_base_url": "",
            "allowed_hosts": [],
            "allowed_origins": [],
        },
        "logging": {"audit_file": ""},
    }
    assert got["config"]["server"]["port"] == 9999
    assert got["config"]["logging"]["audit_file"] == str(tmp_path / "audit.jsonl")


def test_get_lists_the_fields_that_need_a_restart(client):
    assert client.get("/api/config", headers=KEY).json()["restart_fields"] == [
        "server.host",
        "server.port",
        "server.public_base_url",
        "server.allowed_hosts",
        "server.allowed_origins",
        "logging.audit_file",
    ]


def test_every_restart_tag_on_the_page_names_a_restart_field(client):
    tagged = set(re.findall(r'data-rst="([^"]+)"', client.get("/").text))
    assert tagged == set(RESTART_FIELDS)


@pytest.mark.anyio
async def test_concurrent_saves_apply_one_at_a_time(runtime, root):
    active, overlaps = [], []
    real_apply = runtime.apply

    async def slow_apply(cfg):
        overlaps.append(len(active))
        active.append(cfg)
        await asyncio.sleep(0.05)
        active.remove(cfg)
        return await real_apply(cfg)

    runtime.apply = slow_apply
    transport = httpx.ASGITransport(app=build_ui_app(runtime, key="k123", port=4321))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:4321", headers=KEY) as c:
        responses = await asyncio.gather(*(c.put("/api/config", json=valid_body(root)) for _ in range(3)))

    assert [r.status_code for r in responses] == [200, 200, 200]
    assert overlaps == [0, 0, 0]


@pytest.mark.anyio
async def test_invalid_save_is_rejected_while_another_save_holds_the_lock(runtime, root):
    entered, release = asyncio.Event(), asyncio.Event()
    real_apply = runtime.apply

    async def blocked_apply(cfg):
        entered.set()
        await release.wait()
        return await real_apply(cfg)

    runtime.apply = blocked_apply
    transport = httpx.ASGITransport(app=build_ui_app(runtime, key="k123", port=4321))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:4321", headers=KEY) as c:
        first = asyncio.create_task(c.put("/api/config", json=valid_body(root)))
        await entered.wait()
        try:
            bad = await asyncio.wait_for(c.put("/api/config", content=b"{not json"), 2)
        finally:
            release.set()
        assert (await first).status_code == 200

    assert bad.status_code == 400
    assert "invalid request body" in bad.json()["error"]


def test_save_with_empty_roots_is_accepted(client, cfg_path, root):
    body = valid_body(root)
    body["allowed_roots"] = []

    r = client.put("/api/config", json=body, headers=KEY)

    assert r.status_code == 200
    assert load_config(cfg_path).allowed_roots == []


def test_save_invalid_config_is_400_and_leaves_file(client, cfg_path, root, tmp_path):
    client.put("/api/config", json=valid_body(root), headers=KEY)
    before = cfg_path.read_text(encoding="utf-8")
    body = valid_body(root)
    body["allowed_roots"] = [str(tmp_path / "missing")]

    r = client.put("/api/config", json=body, headers=KEY)

    assert r.status_code == 400
    assert "not an existing directory" in r.json()["error"]
    assert cfg_path.read_text(encoding="utf-8") == before


def test_existing_invalid_file_is_reported(client, cfg_path):
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("allowed_roots = [", encoding="utf-8")
    body = client.get("/api/config", headers=KEY).json()
    assert body["exists"] is True
    assert "invalid TOML" in body["error"]


def test_check_dir(client, root, tmp_path):
    assert client.get("/api/check-dir", params={"path": str(root)}, headers=KEY).json() == {"exists": True}
    assert client.get("/api/check-dir", params={"path": str(tmp_path / "x")}, headers=KEY).json() == {
        "exists": False
    }


def test_startup_token_is_returned_once(client, runtime):
    runtime.startup_token = "fmcp_first"

    assert client.get("/api/startup-token").status_code == 403
    assert client.get("/api/startup-token", headers=KEY).json() == {
        "name": "default",
        "token": "fmcp_first",
        "mcp_url": "http://127.0.0.1:8765/mcp",
    }
    assert runtime.startup_token is None
    assert client.get("/api/startup-token", headers=KEY).json() == {"token": None}


def test_startup_token_mcp_url_uses_public_base_url(client, runtime):
    runtime.config.server.public_base_url = "https://fm.example.com"
    runtime.startup_token = "fmcp_first"

    assert client.get("/api/startup-token", headers=KEY).json()["mcp_url"] == "https://fm.example.com/mcp"


def test_token_lifecycle_uses_the_runtime_token_store(client, cfg_path, runtime):
    r = client.post("/api/tokens", json={"name": "laptop"}, headers=KEY)
    assert r.status_code == 200
    token = r.json()["token"]
    assert runtime.tokens.verify(token) == "laptop"
    assert TokenStore(cfg_path.parent / "tokens.json").verify(token) == "laptop"

    listed = client.get("/api/tokens", headers=KEY).json()["tokens"]
    assert [t["name"] for t in listed] == ["laptop"]
    assert "sha256" not in listed[0]

    assert client.post("/api/tokens", json={"name": "laptop"}, headers=KEY).status_code == 409
    assert client.post("/api/tokens", json={"name": "  "}, headers=KEY).status_code == 400
    assert client.delete("/api/tokens/laptop", headers=KEY).status_code == 200
    assert runtime.tokens.verify(token) is None
    assert client.delete("/api/tokens/laptop", headers=KEY).status_code == 404
