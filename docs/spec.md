# File Manager MCP — Implementation Spec

An MCP server on the **Local PC** that lets **Remote Agents** (Codex CLI, Cursor Cloud Agents, remote `cursor-agent`) manage files on it. Vocabulary: `CONTEXT.md`. Each section cites the wayfinder ticket under `.wayfinder/file-manager-mcp/tickets/` that holds the rationale.

## 1. Stack (004, ADR 0001)

- Python `>=3.11`, official `mcp` SDK v2 (`mcp.server.MCPServer`), managed with `uv`.
- One self-built Starlette app: `MCPServer.streamable_http_app()` mounted at `/mcp` (Streamable HTTP only), File Channel routes under `/files`, one outermost ASGI auth middleware over every path. The app lifespan enters `mcp.session_manager.run()`. No `custom_route`, no FastAPI.
- Started in code via `uvicorn.Server(Config(...))`: single process, no reload, no workers. On Windows the loop must be `ProactorEventLoop`; startup asserts it and exits otherwise.
- Blocking filesystem and subprocess work runs off the event loop (014).

## 2. Configuration (012)

- TOML, read with `tomllib`. Config dir: `%APPDATA%\file-manager-mcp\` (Windows) or `$XDG_CONFIG_HOME/file-manager-mcp/` (default `~/.config/file-manager-mcp/`).
- Config file resolution: `--config PATH` > `FILE_MANAGER_MCP_CONFIG` > `<config dir>/config.toml`.
- If the config file is missing at start, the default config (`allowed_roots = []`, every other value at its default) is written first.
- Token digests: `tokens.json` next to the config file, written by the config UI and the `token` commands.
- If `tokens.json` has no tokens at start, a token named `default` is created. Its plaintext is kept only in memory and shown once: in the startup summary in the terminal, and in the config UI (`GET /api/startup-token` returns it on the first call, then `{"token": null}`) as a token receipt with a Cursor `mcpServers` snippet. It is never written to disk or to the audit log.
- Unknown keys, a missing or non-list `allowed_roots`, or a non-directory root are startup errors. Zero tokens is not: if every token is revoked while running, every MCP request is rejected until a token is issued.
- An empty `allowed_roots = []` is valid: `list_roots` returns an empty list, and every path-taking tool and File Channel route returns `NO_ALLOWED_ROOTS` with the message `No Allowed Roots configured yet; ask the operator to add one in the config UI.`

```toml
allowed_roots = ['D:\Projects']

[server]
host = "127.0.0.1"
port = 8765
public_base_url = ""
allowed_hosts = []
allowed_origins = []

[command_executor]
enabled = false
shell = ""          # powershell | pwsh | cmd | sh; empty = powershell on Windows, sh elsewhere

[remote]
allowed_hosts = []

[logging]
audit_file = ""
```

`tokens.json`: `{"tokens": [{"name": "...", "sha256": "<hex>", "created": "<ISO 8601>"}]}`.

## 3. Auth (005)

- `Authorization: Bearer <token>` on every request, `/mcp` and `/files` alike. The only exception is a Signed Link (section 6): a `/files` request whose query has `sig` is checked as a Signed Link instead, never as a bearer request; an invalid, expired, or consumed link → `403 {"error": "invalid_link"}`, and a valid one puts `signed:<issuer>` in `token_name`.
- Token format `fmcp_` + `secrets.token_urlsafe(32)`. Stored as SHA-256 hex. Verification hashes the presented token and compares with each digest via `hmac.compare_digest`.
- Missing/invalid → `401`, `WWW-Authenticate: Bearer`, JSON body `{"error": "unauthorized"}`.
- The matched token name is put in the ASGI scope (`scope["state"]["token_name"]`) for auditing.
- DNS-rebinding protection (ADR 0003): one Host/Origin check in the outermost middleware for every path. `Host` must be a loopback form with or without port, an entry of `server.allowed_hosts`, or one of the Local PC's own interface IPv4 addresses (not loopback or link-local) with or without `:<port>`, checked live with a few seconds of caching; else `421`. `Origin`, when present, must be in `server.allowed_origins`; else `403`. The SDK's static check is disabled.
- No OAuth, no protected-resource metadata.

## 4. Paths and Allowed Roots (006)

A path argument is valid when all hold:

1. It is a non-empty string without NUL bytes.
2. It does not start with `\\?\` or `\\.\`.
3. It is absolute (`os.path.isabs` after converting `/` to the native separator).
4. On Windows: no `:` after the drive prefix (alternate data streams), no component whose stem is a reserved device name (`CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9`, case-insensitive), and no component other than `.` and `..` ending in a dot or space (Win32 strips them, so such a name aliases another path, e.g. `Root\ ` is `Root`).
5. `os.path.realpath(path)` is equal to or under `os.path.realpath(root)` for some Allowed Root, compared on `os.path.normcase` values with `os.path.commonpath`.

With no Allowed Roots configured, every path → `NO_ALLOWED_ROOTS` (checked first). Violations of 1–4 → `INVALID_PATH`; of 5 → `OUTSIDE_ROOTS`. An Allowed Root may be listed and written into but not deleted, moved, or overwritten.

## 5. MCP tools

Errors are MCP tool errors (`isError: true`) whose text begins with a code: `NO_ALLOWED_ROOTS`, `OUTSIDE_ROOTS`, `NOT_FOUND`, `ALREADY_EXISTS`, `NOT_A_FILE`, `NOT_A_DIRECTORY`, `DIRECTORY_NOT_EMPTY`, `TOO_LARGE`, `NOT_TEXT`, `INVALID_PATH`, `INVALID_ARGUMENT`, `HOST_NOT_ALLOWED`, `JOB_NOT_FOUND`, `PERMISSION_DENIED`, `IO_ERROR`. An unexpected OS error maps by type: file not found → `NOT_FOUND`, exists → `ALREADY_EXISTS`, is a directory → `NOT_A_FILE`, not a directory → `NOT_A_DIRECTORY`, permission (incl. a file locked by another process) → `PERMISSION_DENIED`, any other → `IO_ERROR`. Results are structured JSON objects.

### 5.1 File tools (006) — always registered

| Tool | Parameters | Result |
|---|---|---|
| `list_roots` | — | `{roots: [path]}` |
| `list_dir` | `path`, `cursor=None`, `limit=200` (1–1000) | `{entries: [{name, type, size, modified}], next_cursor}` |
| `stat` | `path` | `{path, exists, type?, size?, modified?}` |
| `read_text` | `path`, `offset=0`, `max_bytes=32768` (1–262144), `encoding="utf-8"` | `{content, offset, bytes_read, total_size, eof}` |
| `write_text` | `path`, `content`, `mode="create"`, `encoding="utf-8"`, `create_parents=False` | `{path, bytes_written}` |
| `make_dir` | `path` | `{path}` |
| `move` | `source`, `destination`, `overwrite=False` | `{source, destination}` |
| `copy` | `source`, `destination`, `overwrite=False` | `{source, destination}` |
| `delete` | `path`, `recursive=False` | `{path}` |
| `search` | `path`, `name_glob="*"`, `content_regex=None`, `limit=100` (1–1000) | `{matches: [{path, line?, text?}], truncated}` |

- `type`: `file` | `dir` | `symlink` | `other`. `modified`: ISO 8601 UTC. `size`: bytes (0 for directories).
- `list_dir` sorts by name; `cursor` is the opaque index string from the previous `next_cursor` (`null` at the end).
- `read_text`: reads `max_bytes` from `offset`; if the slice ends inside a UTF-8 multi-byte sequence it is trimmed to the last complete character (`bytes_read` reflects that); undecodable bytes → `NOT_TEXT` mentioning `prepare_download`. Offset past EOF returns empty content with `eof: true`.
- `write_text`: content encoded size > 1 MiB → `TOO_LARGE` mentioning `prepare_upload`. `create` fails with `ALREADY_EXISTS`; `overwrite` writes a temp file in the same directory then `os.replace`; `append` appends. Missing parent → `NOT_FOUND` unless `create_parents`.
- `move`/`copy`: existing destination → `ALREADY_EXISTS` unless `overwrite` (overwriting a directory is refused with `INVALID_ARGUMENT`). Copy of a directory is recursive. A destination equal to or inside the source directory → `INVALID_ARGUMENT`.
- `delete`: non-empty directory without `recursive` → `DIRECTORY_NOT_EMPTY`.
- `search`: recursive under `path`; `name_glob` matched against entry names (files and directories) with `fnmatch` (case-insensitive on Windows); with `content_regex`, only UTF-8 text files ≤ 1 MiB are scanned and each matching line is a match (`line` 1-based, `text` trimmed to 200 chars). Stops at `limit` with `truncated: true`. Links (symlinks, junctions) match by name only: their targets are neither descended into nor scanned.
- Annotations: `readOnlyHint` on `list_roots`, `list_dir`, `stat`, `read_text`, `search`, `prepare_download`, `get_job`; `destructiveHint` on `write_text`, `move`, `copy`, `delete`, `run_command`, `cancel_job`, `pull_from_remote`.

### 5.2 File Channel tools (007) — always registered

- `prepare_download(path, for_host="", expires_in=3600)` → `{url, size, modified, curl}`, plus `expires` with `for_host`; path must be an existing file.
- `for_host` (DUT IP or hostname) makes a Signed Link: the server resolves it to IPv4, takes the local address the OS routes to it, and builds the link on `http://<that address>:<server.port>`; its `curl` has no `Authorization` header. `INVALID_ARGUMENT` when it does not resolve, has no route, routes to loopback (this PC), routes from an address that is not an own interface IP (e.g. link-local), or the listener does not cover that address (`server.host` neither `0.0.0.0`/empty nor equal to it). `expires_in` must be 60–86400.
- `prepare_upload(path, for_host="", expires_in=3600, overwrite=False, max_bytes=None)` → `{upload_url, complete_url, offset, max_chunk_bytes, curl}`, plus `expires` with `for_host`; parent directory must exist. `overwrite` is used in the `curl` example, and in a Signed Link it is fixed. `max_bytes` (≥ 1) needs `for_host` and caps the total upload size.
- Base URL: `server.public_base_url` without trailing `/` if set, else `http://<host>:<port>`, where a wildcard host (`0.0.0.0`, `::`, empty) becomes `127.0.0.1` and an IPv6 literal is bracketed. The startup summary and the config UI token receipt use the same base URL. `curl` examples use `$FILE_MANAGER_TOKEN`.
- Link base (ADR 0003): `server.public_base_url` if set; else `http://<Host header of the MCP request>` (already validated, section 3), so links work over the Connection Path the Remote Agent used; else (no HTTP request) the base URL above.

## 6. File Channel HTTP contract (007)

| Request | Behaviour |
|---|---|
| `GET /files/download?path=P` | streams P with `Range`/`If-Range` support; 400 directory/invalid, 403 outside roots or no Allowed Roots (`NO_ALLOWED_ROOTS`, every route), 404 missing |
| `GET /files/upload?path=P` | `{"offset": size of P.fmcp-partial or 0}` |
| `PUT /files/upload?path=P&offset=N` | `N=0` truncates/creates `P.fmcp-partial`; else `N` must equal its size (409 `{"error": "OFFSET_MISMATCH", "message", "offset": size}`); body > 64 MiB → 413; returns `{"offset": new size}` |
| `POST /files/upload/complete?path=P&overwrite=false&sha256=H` | no partial → 404; digest mismatch → 422 (partial kept); existing P without overwrite → 409; else `os.replace` → `{"path", "size", "sha256"}` |
| `DELETE /files/upload?path=P` | removes the partial; `{"deleted": bool}` |

Error bodies are JSON `{"error": "<CODE>", "message": "..."}` using the codes of §5, including for unexpected OS errors (`PERMISSION_DENIED` → 403, `IO_ERROR` → 500). An existing `P.fmcp-partial` that is a link or not a regular file → 400 `NOT_A_FILE` on every upload route. Upload requests on the same partial run one at a time, so of two chunks sent at the same offset one gets 409.

Signed Links (ADR 0003): the query adds `exp` (Unix seconds), `by` (issuing token name), and `sig` = unpadded base64url HMAC-SHA256 over direction, `path`, `exp`, `by`, `overwrite`, `max_bytes`, keyed by a random secret made at each launch (a restart revokes every link); a link is also void once its issuing token is removed. A repeated signed field invalidates the link. A download link allows `GET`/`HEAD /files/download` and is reusable until `exp`. An upload link adds `overwrite` (and `max_bytes` when set) to the query and allows `GET`/`PUT`/`DELETE /files/upload` and `POST /files/upload/complete` for its path; complete uses the signed `overwrite`, a chunk that would grow the partial beyond `max_bytes`, or a complete of a partial larger than it, → 413, and after a successful complete the link is void. Every use re-checks the current Allowed Roots, so a path no longer inside them → 403 `OUTSIDE_ROOTS`.

## 7. Jobs (008, 009)

- In-memory registry keyed by random UUID strings; lost on restart; finished jobs kept 1 hour.
- Each job captures stdout and stderr separately, keeping at most 1 MiB per stream (head and tail halves).
- Result shape (shared by `run_command`, `push_to_remote`, `pull_from_remote`, `get_job`):
  - `{status: "exited", job_id, exit_code, stdout, stderr, duration_ms, truncated}`
  - `{status: "running", job_id, stdout, stderr, duration_ms, truncated}`
  - `{status: "cancelled", job_id, stdout, stderr, duration_ms, truncated}`
  - Returned streams are capped at 32 KiB each: first 16 KiB + `\n...[N bytes truncated]...\n` + last 16 KiB.
- `wait_sec` is clamped to 0–50.
- A job is `running` until its process and every descendant still holding its stdout/stderr have exited.
- `get_job(job_id, wait_sec=0)`, `cancel_job(job_id)` (kills the process tree, including descendants that outlived the job's process: on Windows each job's process is put in its own Job Object, which is terminated, with `taskkill /T /F /PID` as the fallback when that fails; elsewhere `killpg` on the job's process group). Unknown id → `JOB_NOT_FOUND`. Registered while the Command Executor or remote transfer is enabled, added and removed live with them (section 12).
- Executables given by name (`powershell.exe`, `pwsh.exe`, `cmd.exe`, `scp`, `taskkill.exe`) are resolved to absolute paths from the absolute `PATH` entries (with `PATHEXT` on Windows); the current directory is never searched. Not found → `NOT_FOUND`.

## 8. Command Executor (008)

- Registered while `command_executor.enabled = true`, added and removed live (section 12). A risk warning is printed to stderr at startup when enabled and whenever a config UI save enables it.
- `run_command(command, cwd=None, env=None, wait_sec=30)`. `cwd` defaults to the first Allowed Root (`NO_ALLOWED_ROOTS` if there is none) and must be an existing directory. `env` merges over the server environment. stdin closed.
- Shell argv:
  - `powershell`: `powershell.exe -NoProfile -NonInteractive -Command "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; <command>"`
  - `pwsh`: same with `pwsh.exe`
  - `cmd`: the command line `<cmd.exe> /c "<command>"`, passed verbatim (not argv-quoted) so quotes in the command reach `cmd.exe` intact
  - `sh`: `/bin/sh -c <command>`
- Non-zero exit codes are results, not tool errors.

## 9. Remote transfer (009)

- Registered while `remote.allowed_hosts` is non-empty, added and removed live (section 12). Host must match exactly, else `HOST_NOT_ALLOWED`.
- `push_to_remote(local_path, host, remote_path, recursive=False, wait_sec=30)`: `local_path` existing and inside roots; a directory requires `recursive`.
- `pull_from_remote(host, remote_path, local_path, recursive=False, wait_sec=30)`: `local_path` inside roots.
- argv: `scp -B -o BatchMode=yes [-r] -- <src> <dst>` with remote operand `host:remote_path`; `remote_path` must be non-empty and not start with `-`. Executable: `scp` found on `PATH` as in section 7.

## 10. Audit logging (013)

- JSON lines: `ts`, `token`, `kind` (`tool`|`http`), `name`, `args`, `outcome`, `duration_ms`; rejected auth lines include `client`. Signed Link requests have `token` = `signed:<issuer>` and include `client`; the issuing `prepare_*` tool line records `for_host`.
- Never: tokens, auth headers, file content, `write_text.content`, `env` values.
- File: `logging.audit_file`, else `%LOCALAPPDATA%\file-manager-mcp\logs\audit.log` / `$XDG_STATE_HOME/file-manager-mcp/audit.log`; `RotatingFileHandler` 10 MB × 5.

## 11. Concurrency (014)

Remote Agent requests have no sessions and take no locks, except that File Channel upload requests on the same partial run one at a time (section 6); config UI saves and their live apply also run one at a time. Atomic renames for overwrites and upload completion; last writer wins; upload offset conflicts return 409. Jobs are visible to every token.

## 12. Distribution and CLI (011)

- `uv tool install <path-or-git-url>` → console script `file-manager-mcp`; `python -m file_manager_mcp` is identical.
- `file-manager-mcp [--config PATH] [--no-browser]` (ADR 0002): the one command. Writes the default config if missing, then runs the MCP listener on `server.host:server.port` and the config UI listener on `127.0.0.1:<random port>` in one process on one event loop, sharing one Runtime. After both listen it prints the MCP endpoint, one `LAN: http://<ip>:<port>/mcp (plain HTTP)` line per own interface IPv4 address when `server.host` is `0.0.0.0` or empty, the config UI URL, the `default` token if it was just created (section 2), and warnings (no Allowed Roots yet, Command Executor enabled), then opens the UI URL in a browser unless `--no-browser`; a browser-open failure is only a warning. If the MCP port is taken it prints `port <N> is in use; change server.port` and exits non-zero without opening the browser. If either listener stops, both stop; Ctrl+C stops both and closes the audit log. Stopping terminates open MCP sessions first, so their long-lived streams do not hold up shutdown.
- Config UI: binds `127.0.0.1` only, requires a per-launch key carried in the URL fragment and sent as `X-UI-Key`, and rejects non-loopback `Host` headers. Saving validates with the same rules as startup, rewrites the TOML atomically (comments are not preserved), applies it to the running server, and responds with `applied` and `restart_required` (`server.*` and `logging.audit_file` need a restart). Reading the config also returns the running `server` and `logging` values, and fields whose saved value differs from the running one are tagged `restart to apply`. Tokens issued or revoked in the UI use the same token store as MCP auth, so they take effect on the next MCP request.
- Setup guide: `/setup` on the config UI listener (same key and Host rules), linked from the panel. It reads running values from `/api/setup` and builds, for a chosen connection path (This PC for local testing, LAN, SSH `-R`, Tailscale, Cloudflare Tunnel) and client (Cursor, Cursor Cloud Agents, Codex CLI, `cursor-agent`), the tunnel command, the client config snippet, and an Agent rule that mentions only the tools currently registered. Snippets reference the `FILE_MANAGER_TOKEN` environment variable; "Issue token for this client" mints a named token whose plaintext lives only in the page. It warns when a tunnel hostname is missing from the running `server.allowed_hosts` or differs from `public_base_url`. The LAN path offers the own interface IPs (`lan_ips`, preselecting `lan_default`, the address that routes to the internet) and the URL `http://<ip>:<port>/mcp`, warns that it is plain HTTP, and shows a red guard and a broken wire when the running listener (`listen_host`) does not cover the chosen address. LAN is the initial path when `public_base_url` is empty and the listener is a wildcard.
- Live apply: changes to `allowed_roots`, `command_executor.*`, and `remote.allowed_hosts` take effect on the next tool call; in-flight calls finish under the config they started with. Tools are added or removed when a save changes the setting that registers them (sections 7–9). Disabling the Command Executor cancels running `run_command` jobs, and emptying `remote.allowed_hosts` cancels running transfer jobs (process tree kill, as `cancel_job`). No `notifications/tools/list_changed` is sent: clients see the new tool list on their next `tools/list` or reconnect, and calling a removed tool returns the SDK's unknown-tool error. `server.*` and `logging.audit_file` keep their running values until restart.
- `file-manager-mcp token add NAME | list | remove NAME [--config PATH]`: for headless use; `add` prints the token once; duplicate names rejected. These edit `tokens.json` with load-modify-save (as does the config UI), and a running server reloads `tokens.json` when its modification time or size changes, so CLI token changes apply immediately: the next MCP request uses them.
- Auto-start: Task Scheduler "At log on" task running `file-manager-mcp --no-browser` as the user; not a Windows service.

## 13. Connectivity recipes (010)

The config UI's Setup guide (section 12) generates these per client.

- **Cursor Cloud Agents**: named Cloudflare Tunnel to `http://127.0.0.1:<port>` + Cloudflare Access Service Auth. Dashboard MCP: type HTTP, URL `https://fm.<domain>/mcp`, headers `Authorization: Bearer <token>`, `CF-Access-Client-Id`, `CF-Access-Client-Secret`; same values as Runtime Secrets for File Channel `curl`. Add `fm.<domain>` to `server.allowed_hosts` and set `public_base_url`. Fallback: `tailscale funnel --bg <port>`.
- **Codex CLI / `cursor-agent` on remote Linux**: `tailscale serve --bg <port>` (unattended mode) and URL `https://<pc>.<tailnet>.ts.net/mcp`; or `ssh -N -R 127.0.0.1:<port>:127.0.0.1:<port> user@linuxbox` and URL `http://127.0.0.1:<port>/mcp`.
  - Codex: `[mcp_servers.file_manager] url = "..."`, `bearer_token_env_var = "FILE_MANAGER_TOKEN"`.
  - `cursor-agent`: `~/.cursor/mcp.json` `"headers": {"Authorization": "Bearer ${env:FILE_MANAGER_TOKEN}"}`.
- Avoid Cloudflare quick tunnels and the ngrok free tier.
