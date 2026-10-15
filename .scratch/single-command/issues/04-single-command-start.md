# 04 — Single command start

**What to build:** `python -m file_manager_mcp` (and the identical `file-manager-mcp` console script) with no subcommand is the only command needed. It starts the MCP listener and the loopback-only config UI listener in one process, sharing the Runtime, and opens the browser. Saving in the UI applies to the running server. Demo: start it, issue a token in the UI, add a root, and an Agent can use that root without restarting. Design: `docs/superpowers/specs/2026-10-14-single-command-design.md` (Command surface, Startup sequence, Config UI changes).

**Blocked by:** 02 — Empty Allowed Roots no longer blocks startup; 03 — Runtime with live apply

**Status:** done (875ce0a)

- [x] With no subcommand, the process writes the default config if missing, then serves MCP on `server.host:server.port` and the config UI on `127.0.0.1:<random port>` (per-launch key and loopback Host check unchanged), on one event loop.
- [x] After both are listening it prints the MCP endpoint, the UI URL, and warnings (no Allowed Roots yet, Command Executor enabled), then opens the UI URL; `--no-browser` skips opening; a browser-open failure is only a warning.
- [x] If the MCP port is taken, it prints `port <N> is in use; change server.port` and exits non-zero without opening the browser.
- [x] If either server stops, both stop; Ctrl+C stops both and closes the audit log.
- [x] Saving in the UI validates, writes atomically, applies via the Runtime, and responds with `applied` / `restart_required`; the dock message reflects that live changes are applied.
- [x] UI token issue/revoke uses the same token store as MCP auth, so a revoked token is rejected on the next MCP request.
- [x] The `init`, `serve`, and `ui` subcommands and their tests are removed; `token add | list | remove` still work.
- [x] An end-to-end test starts both listeners, issues a token through the UI API, calls MCP with it, enables the Command Executor through the UI API, and a new MCP session lists `run_command`.
- [x] `docs/spec.md` section 12 describes the new command surface; `docs/adr/0002-single-command-live-apply.md` records the choice of one process with a loopback-only UI listener over a single port or a supervisor, and why Serial Bridge's loopback exemption is not adopted (tunnels deliver remote requests from loopback).
