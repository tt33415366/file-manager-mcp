# Single Command Start with Live Apply — Design

Status: approved 2026-10-14. Reference project: `D:\SourceCode\serial_bridge` (one process, one command, works on first boot).

## Problem

Starting the server takes four separate steps (`init`, `token add`, `ui`, `serve`), each of which can block: the console script may not be on `PATH`, `serve` refuses to start without roots or tokens, and the config UI is a second process whose saves only apply after a server restart.

## Goal

`python -m file_manager_mcp` (or the `file-manager-mcp` console script, identical) is the only command needed, including on first boot. Config UI saves apply to the running server without a restart where possible.

Out of scope: moving the config out of `%APPDATA%`, plaintext tokens, a Setup page, restructuring modules.

## Command surface

- No subcommand: start the server and the config UI, then open the browser.
  - `--config PATH`: same resolution as today (`--config` > `FILE_MANAGER_MCP_CONFIG` > `<config dir>/config.toml`).
  - `--no-browser`: do not open the browser (for the Task Scheduler logon task); the UI URL is still printed.
- `token add NAME | list | remove NAME [--config PATH]`: kept unchanged for headless use.
- `init`, `serve`, `ui`: removed.

## Startup sequence

1. Resolve the config path.
2. If the config file is missing, write the default config (`allowed_roots = []`, every other value at its default).
3. Load and validate the config. An invalid existing config is a startup error that prints the reason and exits non-zero.
4. Build the `Runtime` (below), then start two uvicorn servers on one asyncio loop (Proactor on Windows, asserted as today):
   - MCP app on `server.host:server.port`, unchanged auth and Host/Origin rules.
   - Config UI app on `127.0.0.1:<random free port>`, unchanged per-launch key (`#k=` fragment, `X-UI-Key` header) and loopback `Host` check.
5. After both are listening, if `tokens.json` has no tokens, create one named `default` and keep its plaintext in `Runtime.startup_token`.
6. Print the MCP endpoint, the UI URL, the startup token (first boot only), and warnings ("no Allowed Roots yet", "Command Executor is enabled"); then open `http://127.0.0.1:<ui-port>/#k=<key>` unless `--no-browser`.

If the MCP port cannot be bound, print `port <N> is in use; change server.port` and exit non-zero before opening the browser. A browser-open failure is a warning. If either server stops, both stop; Ctrl+C stops both and closes the audit log.

## Empty Allowed Roots

Empty `allowed_roots` is no longer a startup error. `list_roots` returns an empty list. Every path-taking tool and File Channel route (HTTP 403, code in the JSON body) returns the error code `NO_ALLOWED_ROOTS` with the message `No Allowed Roots configured yet; ask the operator to add one in the config UI.` A non-directory root remains a validation error.

## Runtime and live apply

New module `runtime.py` with class `Runtime`, the single in-process owner of: current `Config`, `TokenStore`, `PathPolicy`, `MCPServer`, `JobRegistry`, `CommandExecutor`, `RemoteTransfer`, `startup_token`.

`Runtime.apply(new_config) -> {"applied": [...], "restart_required": [...]}` (lists of dotted setting names that changed):

| Setting | Effect |
|---|---|
| `allowed_roots` | Live: `PathPolicy.set_roots(...)`. `FileOps`, `TransferLinks`, File Channel routes, `RemoteTransfer`, and `CommandExecutor` all read the one shared `PathPolicy`. |
| `command_executor.enabled` | Live: `run_command` added/removed via `MCPServer.add_tool`/`remove_tool`. Disabling cancels running `run_command` jobs (process tree kill). |
| `command_executor.shell` | Live: used by the next `run_command`. |
| `remote.allowed_hosts` | Live: allowlist updated; `push_to_remote`/`pull_from_remote` added when it becomes non-empty, removed when empty. Removing them cancels running transfer jobs. |
| `get_job` / `cancel_job` | Registered exactly when the Command Executor is enabled or the remote allowlist is non-empty. |
| `server.*`, `logging.audit_file` | Restart required: reported in `restart_required`; the in-memory value stays the running one. |

Tokens are live without `apply`: the UI and the MCP auth middleware share one `TokenStore` instance.

Rules:

- `apply` runs on the event loop thread, so no tool call observes a half-applied config. In-flight tool calls finish under the config they started with.
- No `tools/list_changed` notification; clients see tool changes on their next `tools/list` or reconnect. Calling a removed tool returns the SDK's unknown-tool error.
- `config.toml` is not watched; hand edits apply on the next start and are overwritten by the next UI save.

## Config UI changes

- `PUT /api/config`: validate (`config_from_dict`), write atomically, `runtime.apply(...)`, respond `{"applied": [...], "restart_required": [...]}`. The dock message reads `Applied.` or `Applied. Restart needed for: server.port, ...`.
- `GET /api/startup-token`: returns `{"name": "default", "token": "...", "mcp_url": "..."}` once and clears `Runtime.startup_token`; afterwards `{"token": null}`. The UI calls it on load and, if present, opens the token receipt with Copy and a Cursor `mcpServers` snippet (`url` = MCP URL using `public_base_url` when set, `headers.Authorization` = `Bearer <token>`).
- Token routes use `Runtime`'s `TokenStore` instance.
- `GET /api/config` adds `"running": {"server": {...}, "logging": {...}}` with the values the process is using. The Network and Audit Log modules show a `restart to apply` tag on fields whose saved value differs from the running value.

## Testing

TDD; existing suite stays green apart from the removed subcommands.

- `Runtime.apply`: root swap changes `FileOps` results; executor and remote toggles change `list_tools` (in-memory `Client`); shell change used by next `run_command`; disabling the executor cancels a running job; `server.port` change lands in `restart_required`.
- Empty roots: a path tool returns `NO_ALLOWED_ROOTS`; `list_roots` returns `[]`.
- First boot: `main([])` in a temp config dir (listening patched out) creates the config and a `default` token; a second start creates no new token.
- UI API: `PUT /api/config` returns `applied`/`restart_required`; `GET /api/startup-token` returns the token once.
- End to end: start both listeners; the startup token authenticates an MCP call; after enabling the executor via the UI API, a new MCP session lists `run_command`.
- Remove tests for `init`, `serve`, and `ui` subcommands.

## Docs

- `docs/spec.md`: section 2 (empty roots allowed, default config written on first start), section 3/startup, section 12 (command surface).
- `docs/adr/0002-single-command-live-apply.md`: one process with a loopback-only UI listener chosen over a single port with `/ui` (the UI would be reachable through tunnels) and over a supervisor that restarts `serve` (drops sessions and jobs). Serial Bridge's loopback exemption is not adopted: SSH `-R`, cloudflared, and `tailscale serve` deliver remote requests from loopback.
