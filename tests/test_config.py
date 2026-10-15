from pathlib import Path

import pytest

from file_manager_mcp.config import (
    ConfigError,
    config_from_dict,
    config_to_dict,
    default_config_dir,
    dump_config,
    load_config,
    resolve_config_path,
)


def toml_path(p: Path) -> str:
    return "'" + str(p) + "'"


def test_minimal_config_gets_defaults(write_config, root):
    cfg = load_config(write_config(f"allowed_roots = [{toml_path(root)}]"))

    assert cfg.allowed_roots == [str(root)]
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 8765
    assert cfg.server.public_base_url == ""
    assert cfg.server.allowed_hosts == []
    assert cfg.command_executor.enabled is False
    assert cfg.remote.allowed_hosts == []
    assert cfg.logging.audit_file == ""


def test_full_config_is_read(write_config, root):
    cfg = load_config(
        write_config(
            f"""
            allowed_roots = [{toml_path(root)}]
            [server]
            host = "0.0.0.0"
            port = 9000
            public_base_url = "https://fm.example.com"
            allowed_hosts = ["fm.example.com"]
            allowed_origins = ["https://fm.example.com"]
            [command_executor]
            enabled = true
            shell = "cmd"
            [remote]
            allowed_hosts = ["buildbox"]
            [logging]
            audit_file = "x.log"
            """
        )
    )

    assert cfg.server.port == 9000
    assert cfg.server.public_base_url == "https://fm.example.com"
    assert cfg.server.allowed_hosts == ["fm.example.com"]
    assert cfg.server.allowed_origins == ["https://fm.example.com"]
    assert cfg.command_executor.enabled is True
    assert cfg.command_executor.shell == "cmd"
    assert cfg.remote.allowed_hosts == ["buildbox"]
    assert cfg.logging.audit_file == "x.log"


def test_unknown_key_is_rejected(write_config, root):
    with pytest.raises(ConfigError, match="bogus"):
        load_config(write_config(f"allowed_roots = [{toml_path(root)}]\nbogus = 1"))


def test_unknown_section_key_is_rejected(write_config, root):
    with pytest.raises(ConfigError, match="server.prot"):
        load_config(write_config(f"allowed_roots = [{toml_path(root)}]\n[server]\nprot = 1"))


def test_empty_allowed_roots_is_accepted(write_config):
    assert load_config(write_config("allowed_roots = []")).allowed_roots == []


def test_non_list_allowed_roots_is_rejected(write_config):
    with pytest.raises(ConfigError, match="allowed_roots"):
        load_config(write_config("allowed_roots = 'D:/x'"))
    with pytest.raises(ConfigError, match="allowed_roots"):
        load_config(write_config("allowed_roots = [1]"))


def test_missing_root_directory_is_rejected(write_config, tmp_path):
    with pytest.raises(ConfigError, match="not an existing directory"):
        load_config(write_config(f"allowed_roots = [{toml_path(tmp_path / 'nope')}]"))


def test_invalid_shell_is_rejected(write_config, root):
    with pytest.raises(ConfigError, match="shell"):
        load_config(
            write_config(f"allowed_roots = [{toml_path(root)}]\n[command_executor]\nshell = 'zsh'")
        )


def test_empty_host_means_the_loopback_default(root):
    cfg = config_from_dict({"allowed_roots": [str(root)], "server": {"host": ""}})
    assert cfg.server.host == "127.0.0.1"


def test_ipv6_wildcard_host_is_rejected(root):
    with pytest.raises(ConfigError, match="0.0.0.0"):
        config_from_dict({"allowed_roots": [str(root)], "server": {"host": "::"}})
    assert config_from_dict({"allowed_roots": [str(root)], "server": {"host": "::1"}}).server.host == "::1"


def test_wrong_type_is_rejected(write_config, root):
    with pytest.raises(ConfigError, match="server.port"):
        load_config(write_config(f"allowed_roots = [{toml_path(root)}]\n[server]\nport = 'x'"))


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.toml")


def test_invalid_toml_is_a_config_error(write_config):
    with pytest.raises(ConfigError):
        load_config(write_config("allowed_roots = ["))


def test_dump_then_load_round_trips(tmp_path, root):
    weird = tmp_path / "we'ird dir"
    weird.mkdir()
    cfg = config_from_dict(
        {
            "allowed_roots": [str(root), str(weird)],
            "server": {"port": 9001, "allowed_hosts": ["fm.example.com"]},
            "command_executor": {"enabled": True, "shell": "cmd"},
            "remote": {"allowed_hosts": ["me@box"]},
            "logging": {"audit_file": 'C:\\logs\\"quoted" ünï.log'},
        }
    )
    path = tmp_path / "out.toml"
    path.write_text(dump_config(cfg), encoding="utf-8")

    assert config_to_dict(load_config(path)) == config_to_dict(cfg)


def test_config_to_dict_has_every_section(root):
    d = config_to_dict(config_from_dict({"allowed_roots": [str(root)]}))
    assert set(d) == {"allowed_roots", "server", "command_executor", "remote", "logging"}
    assert d["server"]["port"] == 8765


def test_config_from_dict_validates(root):
    with pytest.raises(ConfigError, match="unknown key"):
        config_from_dict({"allowed_roots": [str(root)], "nope": 1})
    with pytest.raises(ConfigError, match="allowed_roots"):
        config_from_dict({})
    assert config_from_dict({"allowed_roots": []}).allowed_roots == []


def test_config_path_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("FILE_MANAGER_MCP_CONFIG", str(tmp_path / "env.toml"))
    assert resolve_config_path(str(tmp_path / "cli.toml")) == tmp_path / "cli.toml"
    assert resolve_config_path(None) == tmp_path / "env.toml"
    monkeypatch.delenv("FILE_MANAGER_MCP_CONFIG")
    assert resolve_config_path(None) == default_config_dir() / "config.toml"


def test_default_config_dir_on_windows_uses_appdata(monkeypatch, tmp_path):
    monkeypatch.setattr("file_manager_mcp.config._is_windows", lambda: True)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert default_config_dir() == tmp_path / "file-manager-mcp"


def test_default_config_dir_elsewhere_uses_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr("file_manager_mcp.config._is_windows", lambda: False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert default_config_dir() == tmp_path / "file-manager-mcp"
