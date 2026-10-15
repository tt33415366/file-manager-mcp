"""Command line entry point: start (no subcommand), token add|list|remove (spec section 12)."""

from __future__ import annotations

import argparse
import sys

from .config import ConfigError, load_config, resolve_config_path
from .tokens import TokenError, TokenStore

STARTER_CONFIG = """\
# File Manager MCP configuration. See docs/spec.md.
# allowed_roots: directories Remote Agents may reach, e.g. 'D:\\Projects'. Add them in the config UI.
allowed_roots = []

[server]
host = "127.0.0.1"
port = 8765
public_base_url = ""     # e.g. "https://fm.example.com" when behind a tunnel
allowed_hosts = []       # tunnel hostnames, e.g. "fm.example.com"
allowed_origins = []

[command_executor]
# WARNING: when enabled, Remote Agents run shell commands unsandboxed as you.
enabled = false
shell = ""               # powershell | pwsh | cmd | sh; empty = platform default

[remote]
allowed_hosts = []       # ssh aliases or user@host for push_to_remote / pull_from_remote

[logging]
audit_file = ""          # empty = default log directory
"""


def _error(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="file-manager-mcp",
        description="Start the MCP server and the local config UI (no subcommand), or manage tokens.",
    )
    parser.add_argument("--config")
    parser.add_argument("--no-browser", action="store_true", help="do not open the config UI in a browser")
    sub = parser.add_subparsers(dest="command")

    token = sub.add_parser("token", help="manage bearer tokens")
    token_sub = token.add_subparsers(dest="action", required=True)
    for action in ("add", "remove"):
        p = token_sub.add_parser(action)
        p.add_argument("name")
        p.add_argument("--config", default=argparse.SUPPRESS)
    token_sub.add_parser("list").add_argument("--config", default=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config_path = resolve_config_path(args.config)
    tokens_path = config_path.parent / "tokens.json"

    if args.command == "token":
        store = TokenStore(tokens_path)
        try:
            if args.action == "add":
                token = store.add(args.name)
                print(f"token {args.name!r} created; it is shown only once:")
                print(token)
            elif args.action == "remove":
                store.remove(args.name)
                print(f"token {args.name!r} removed (applies immediately, also to a running server)")
            else:
                for entry in store.list():
                    print(f"{entry.name}\t{entry.created}")
        except TokenError as exc:
            _error(str(exc))
            return 1
        return 0

    from .server import StartupError, serve

    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(STARTER_CONFIG, encoding="utf-8")
        print(f"wrote default config {config_path}")
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        _error(str(exc))
        return 2
    try:
        serve(config, TokenStore(tokens_path), open_browser=not args.no_browser)
    except StartupError as exc:
        _error(str(exc))
        return 2
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
