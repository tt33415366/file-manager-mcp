import socket
import subprocess
import sys
import tomllib
import webbrowser

import pytest

from file_manager_mcp.cli import main
from file_manager_mcp.tokens import TokenStore


def test_token_add_list_remove(tmp_path, capsys):
    cfg = tmp_path / "config.toml"

    assert main(["token", "add", "laptop", "--config", str(cfg)]) == 0
    token = capsys.readouterr().out.strip().splitlines()[-1]
    assert token.startswith("fmcp_")
    assert TokenStore(tmp_path / "tokens.json").verify(token) == "laptop"

    assert main(["token", "list", "--config", str(cfg)]) == 0
    assert "laptop" in capsys.readouterr().out

    assert main(["token", "add", "laptop", "--config", str(cfg)]) == 1
    assert "already exists" in capsys.readouterr().err

    assert main(["token", "remove", "laptop", "--config", str(cfg)]) == 0
    assert TokenStore(tmp_path / "tokens.json").verify(token) is None
    out = capsys.readouterr().out
    assert "applies immediately" in out
    assert "restart" not in out


@pytest.mark.parametrize("config_first", [True, False], ids=["config-before-token", "config-after-action"])
def test_token_commands_honor_config_in_either_position(tmp_path, monkeypatch, capsys, config_first):
    fallback = tmp_path / "fallback"
    monkeypatch.setenv("FILE_MANAGER_MCP_CONFIG", str(fallback / "config.toml"))
    cfg = tmp_path / "chosen" / "config.toml"
    TokenStore(cfg.parent / "tokens.json").add("laptop")
    option = ["--config", str(cfg)]

    def argv(*command):
        return option + list(command) if config_first else list(command) + option

    assert main(argv("token", "list")) == 0
    assert "laptop" in capsys.readouterr().out
    assert main(argv("token", "remove", "laptop")) == 0
    assert TokenStore(cfg.parent / "tokens.json").list() == []
    assert not fallback.exists()


@pytest.fixture
def served(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "file_manager_mcp.server.serve",
        lambda config, tokens, *, open_browser: calls.append((config, tokens, open_browser)),
    )
    return calls


def test_start_writes_default_config_when_missing(tmp_path, served):
    cfg = tmp_path / "conf" / "config.toml"

    assert main(["--config", str(cfg)]) == 0

    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["allowed_roots"] == []
    assert data["command_executor"]["enabled"] is False
    config, tokens, open_browser = served[0]
    assert config.allowed_roots == []
    assert config.path == cfg
    assert tokens.path == cfg.parent / "tokens.json"
    assert open_browser is True


def test_start_keeps_existing_config(tmp_path, root, served):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"allowed_roots = ['{root}']\n", encoding="utf-8")

    assert main(["--config", str(cfg)]) == 0

    assert cfg.read_text(encoding="utf-8") == f"allowed_roots = ['{root}']\n"
    assert served[0][0].allowed_roots == [str(root)]


def test_no_browser_flag(tmp_path, served):
    assert main(["--config", str(tmp_path / "config.toml"), "--no-browser"]) == 0
    assert served[0][2] is False


def test_start_with_invalid_config_fails_cleanly(tmp_path, served, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text("allowed_roots = [", encoding="utf-8")

    assert main(["--config", str(cfg)]) == 2
    assert "invalid TOML" in capsys.readouterr().err
    assert served == []


def test_port_in_use_exits_nonzero_without_opening_browser(tmp_path, root, capsys, monkeypatch):
    urls = []
    monkeypatch.setattr(webbrowser, "open", lambda url: urls.append(url) or True)
    with socket.create_server(("127.0.0.1", 0)) as busy:
        port = busy.getsockname()[1]
        cfg = tmp_path / "config.toml"
        cfg.write_text(f"allowed_roots = []\n[server]\nport = {port}\n", encoding="utf-8")

        assert main(["--config", str(cfg)]) == 2

    assert f"port {port} is in use; change server.port" in capsys.readouterr().err
    assert urls == []


@pytest.mark.parametrize("command", ["init", "serve", "ui"])
def test_removed_subcommands_are_rejected(command, tmp_path, served, capsys):
    with pytest.raises(SystemExit) as exc:
        main([command, "--config", str(tmp_path / "config.toml")])
    assert exc.value.code == 2
    assert served == []


def test_runs_as_python_module(tmp_path):
    with socket.create_server(("127.0.0.1", 0)) as busy:
        port = busy.getsockname()[1]
        cfg = tmp_path / "config.toml"
        cfg.write_text(f"allowed_roots = []\n[server]\nport = {port}\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "file_manager_mcp", "--config", str(cfg), "--no-browser"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert result.returncode == 2
    assert f"port {port} is in use; change server.port" in result.stderr
