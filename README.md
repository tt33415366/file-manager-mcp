# File Manager MCP

An MCP server on your **Local PC** that lets a **Remote Agent** (Cursor, Codex CLI, `cursor-agent`) manage files there. Paths stay on the PC. The agent calls tools over Streamable HTTP; large and binary files move on a separate HTTP **File Channel**, not inside the tool call.

Vocabulary for the rest of these docs is in [CONTEXT.md](CONTEXT.md). The implementation contract is [docs/spec.md](docs/spec.md).

## What it does

- **File tools.** List, read, write, move, copy, delete, and search inside directories you mark as Allowed Roots. Nothing outside those roots is reachable.
- **File Channel.** `prepare_download` and `prepare_upload` return `curl` commands for files of any size. Downloads support HTTP ranges. Uploads are chunked, then completed with an optional SHA-256 check.
- **DUT links.** Pass `for_host` and the tool returns a **Signed Link** the device can use with no bearer token. The link is bound to one path and one direction, expires, and dies with the token that issued it.
- **Command Executor** (off by default). `run_command` runs PowerShell or a shell on the Local PC, unsandboxed, as you.
- **Remote transfer.** `push_to_remote` and `pull_from_remote` copy files with `scp` to hosts you allowlist.
- **Config UI.** One command starts the MCP listener and a loopback-only setup page: Allowed Roots, tokens, and a guide for LAN, SSH, Tailscale, and Cloudflare Tunnel.

Long-running commands and transfers return a job id. Poll `get_job` or stop them with `cancel_job`.

## Requirements

- Python 3.11+
- On Windows, the server runs on the Proactor event loop (the `file-manager-mcp` command does this)
- `scp` on `PATH` if you use remote transfer

## Install and start

From a checkout:

```powershell
uv tool install --editable .
file-manager-mcp
```

`python -m file_manager_mcp` is the same command. The first start writes a default config and a `default` token, prints both the MCP endpoint and the config UI URL, and opens the UI. Pass `--no-browser` to skip that.

| | |
|---|---|
| Config | `%APPDATA%\file-manager-mcp\config.toml` on Windows, `$XDG_CONFIG_HOME/file-manager-mcp/config.toml` elsewhere. Override with `--config` or `FILE_MANAGER_MCP_CONFIG`. |
| Tokens | `tokens.json` next to the config. Only digests are stored. A new token is shown once. |
| Audit log | `%LOCALAPPDATA%\file-manager-mcp\logs\audit.log` (or `$XDG_STATE_HOME/...`). Tokens and file contents are not logged. |

Add at least one Allowed Root in the config UI before calling file tools. An empty list is valid: every path-taking tool returns `NO_ALLOWED_ROOTS` and asks the agent to stop and ask you.

`server.*` and the audit-log path apply on the next start. Allowed Roots, the Command Executor, and remote hosts apply on the next tool call.

## Connect an agent

The setup page (`/setup` on the config UI) prints a client snippet for the connection path you pick. A minimal Cursor config for this PC:

```json
{
  "mcpServers": {
    "file-manager": {
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer ${env:FILE_MANAGER_TOKEN}" }
    }
  }
}
```

Put the token in `FILE_MANAGER_TOKEN`. The `curl` examples from `prepare_download` and `prepare_upload` use that same variable.

LAN listeners (`server.host = "0.0.0.0"`) speak plain HTTP. For a Remote Agent on another network, prefer an SSH reverse tunnel, Tailscale, or a named Cloudflare Tunnel, and set `server.allowed_hosts` plus `server.public_base_url` so links use that hostname. Recipes are in the setup page and in [docs/spec.md](docs/spec.md) section 13.

## Tokens

```powershell
file-manager-mcp token add laptop
file-manager-mcp token list
file-manager-mcp token remove laptop
```

Adding or removing a token takes effect on the next request, including for a server that is already running. Removing a token voids the Signed Links it issued.

## Tools

Always available: `list_roots`, `list_dir`, `stat`, `read_text`, `write_text`, `make_dir`, `move`, `copy`, `delete`, `search`, `prepare_download`, `prepare_upload`.

While the Command Executor is enabled: `run_command`. While `remote.allowed_hosts` is non-empty: `push_to_remote`, `pull_from_remote`. Either of those also registers `get_job` and `cancel_job`.

`read_text` returns at most 256 KiB per call. `write_text` accepts at most 1 MiB. Larger or binary files go through the File Channel. Errors are tool errors whose text starts with a code (`NOT_FOUND:`, `OUTSIDE_ROOTS:`, `ALREADY_EXISTS:`, ...).

## Security

- Reach is the Allowed Roots, not the whole disk. An agent cannot widen that list; only the config UI or the config file can.
- The Command Executor is a shell as your user. Leave it off unless you mean that.
- The config UI binds `127.0.0.1` only and requires a per-launch key.
- A Signed Link is not a token. It can read or write only the one path it names, and only until it expires.

## Development

```powershell
uv sync --group dev
uv run pytest
```

The suite uses `pytest` and `httpx`. Decisions that are hard to reverse are in [docs/adr/](docs/adr/).
