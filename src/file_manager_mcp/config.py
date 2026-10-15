"""Configuration file loading (spec section 2)."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

APP_NAME = "file-manager-mcp"
CONFIG_ENV_VAR = "FILE_MANAGER_MCP_CONFIG"
SHELLS = ("powershell", "pwsh", "cmd", "sh")


class ConfigError(Exception):
    pass


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    public_base_url: str = ""
    allowed_hosts: list[str] = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=list)


@dataclass
class CommandExecutorConfig:
    enabled: bool = False
    shell: str = ""


@dataclass
class RemoteConfig:
    allowed_hosts: list[str] = field(default_factory=list)


@dataclass
class LoggingConfig:
    audit_file: str = ""


@dataclass
class Config:
    allowed_roots: list[str]
    server: ServerConfig = field(default_factory=ServerConfig)
    command_executor: CommandExecutorConfig = field(default_factory=CommandExecutorConfig)
    remote: RemoteConfig = field(default_factory=RemoteConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    path: Path | None = None


_SECTIONS = {
    "server": ServerConfig,
    "command_executor": CommandExecutorConfig,
    "remote": RemoteConfig,
    "logging": LoggingConfig,
}


def _is_windows() -> bool:
    return os.name == "nt"


def default_config_dir() -> Path:
    if _is_windows():
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_NAME


def default_log_dir() -> Path:
    if _is_windows():
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return Path(base) / APP_NAME / "logs"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / APP_NAME


def resolve_config_path(cli_path: str | None) -> Path:
    if cli_path:
        return Path(cli_path)
    if os.environ.get(CONFIG_ENV_VAR):
        return Path(os.environ[CONFIG_ENV_VAR])
    return default_config_dir() / "config.toml"


def _check_type(key: str, value: Any, expected: Any) -> None:
    if expected is bool or expected == "bool":
        ok = isinstance(value, bool)
    elif expected is int or expected == "int":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif expected is str or expected == "str":
        ok = isinstance(value, str)
    else:
        ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
    if not ok:
        raise ConfigError(f"{key}: expected {expected}, got {value!r}")


def _build_section(name: str, cls: type, raw: Any) -> Any:
    if not isinstance(raw, dict):
        raise ConfigError(f"[{name}] must be a table")
    known = {f.name: f.type for f in fields(cls)}
    for key, value in raw.items():
        if key not in known:
            raise ConfigError(f"unknown key: {name}.{key}")
        _check_type(f"{name}.{key}", value, known[key])
    return cls(**raw)


def load_config(path: Path) -> Config:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from None
    return config_from_dict(raw, path)


def config_to_dict(cfg: Config) -> dict[str, Any]:
    return {
        "allowed_roots": list(cfg.allowed_roots),
        **{name: asdict(getattr(cfg, name)) for name in _SECTIONS},
    }


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return "[" + ", ".join(_toml_value(v) for v in value) + "]"


def dump_config(cfg: Config) -> str:
    """Serialize to TOML. Comments in a hand-edited file are not preserved."""
    data = config_to_dict(cfg)
    lines = [f"allowed_roots = {_toml_value(data['allowed_roots'])}"]
    for name in _SECTIONS:
        lines += ["", f"[{name}]"]
        lines += [f"{key} = {_toml_value(value)}" for key, value in data[name].items()]
    return "\n".join(lines) + "\n"


def config_from_dict(raw: dict[str, Any], path: Path | None = None) -> Config:
    for key in raw:
        if key != "allowed_roots" and key not in _SECTIONS:
            raise ConfigError(f"unknown key: {key}")

    roots = raw.get("allowed_roots")
    if not isinstance(roots, list):
        raise ConfigError("allowed_roots must be a list of directories")
    _check_type("allowed_roots", roots, "list[str]")
    for r in roots:
        if not os.path.isdir(r):
            raise ConfigError(f"allowed_roots: {r!r} is not an existing directory")

    sections = {name: _build_section(name, cls, raw.get(name, {})) for name, cls in _SECTIONS.items()}
    if not sections["server"].host:
        sections["server"].host = ServerConfig().host
    if sections["server"].host == "::":
        raise ConfigError("server.host '::' is not supported; use 0.0.0.0 to listen on every IPv4 interface")
    shell = sections["command_executor"].shell
    if shell and shell not in SHELLS:
        raise ConfigError(f"command_executor.shell must be one of {', '.join(SHELLS)}")

    return Config(allowed_roots=list(roots), path=path, **sections)
