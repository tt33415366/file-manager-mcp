---
id: 012
title: Choose configuration format and location
labels: [wayfinder:grilling]
status: closed
assignee: weiqi
blocked_by: []
---

## Question

What file format and on-disk location hold the server's configuration (listen address/port, Allowed Roots, Command Executor toggle, auth settings), and how do CLI flags or environment variables override it? Decide format, location, precedence, and how secrets are kept out of the file if needed; the individual settings' semantics belong to their own tickets.

## Resolution

Resolved with the owner's standing auto-approval of recommended answers (2026-10-14).

- **Format**: TOML, read with stdlib `tomllib`; hand-edited.
- **Location**: config directory = `%APPDATA%\file-manager-mcp\` on Windows, `$XDG_CONFIG_HOME/file-manager-mcp/` (default `~/.config/file-manager-mcp/`) elsewhere. Config file `config.toml` in it.
- **Precedence**: `--config PATH` > `FILE_MANAGER_MCP_CONFIG` env var > default location. `serve --host/--port` override the file. No other env overrides.
- **Secrets**: token digests live in a separate machine-managed `tokens.json` next to the config file, written only by `file-manager-mcp token ...`, so the hand-edited file holds no secrets.
- **Schema** (unknown keys are a startup error):

```toml
allowed_roots = ['D:\Projects', 'D:\Downloads']   # required, non-empty, existing directories

[server]
host = "127.0.0.1"
port = 8765
public_base_url = ""            # e.g. "https://fm.example.com"; used in File Channel URLs
allowed_hosts = []              # extra Host header values, e.g. tunnel hostnames
allowed_origins = []

[command_executor]
enabled = false
shell = ""                      # powershell | pwsh | cmd | sh; empty = platform default

[remote]
allowed_hosts = []              # ssh aliases or user@host for push/pull

[logging]
audit_file = ""                 # empty = <log dir>/audit.log
```

- Startup fails with a clear message if the config is missing, invalid, or no tokens exist (fail closed).
