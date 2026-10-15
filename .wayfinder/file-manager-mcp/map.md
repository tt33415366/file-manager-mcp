---
title: File Manager MCP
labels: [wayfinder:map]
---

## Destination

An implementation-ready spec for the File Manager MCP server running on the Local PC: tool set, File Channel protocol, auth, Command Executor, `push_to_remote`, implementation stack, and recommended connectivity recipes, compatible with both Codex CLI and Cursor Agent (Cloud Agents and remote `cursor-agent` CLI).

## Notes

- Domain: developer tooling. Read `CONTEXT.md` for vocabulary before any ticket; grilling tickets call both the "grilling" and "domain-modeling" skills.
- Tracker conventions: `.wayfinder/TRACKER.md`.
- Topology: Remote Agents run off-box (cloud or remote Linux); files live on the Local PC, which runs the MCP server and only listens on a port.
- Standing preferences: single-user personal tool; Windows first, cross-platform by design (acceptance on Windows only); MCP is a hard constraint; bulk bytes go through the File Channel, not MCP messages.
- Security baseline: bearer token auth; file tools restricted to Allowed Roots; Command Executor off by default, unsandboxed when enabled, with a risk warning.
- Plan, don't do: tickets produce decisions, not implementation.
- Original idea: `File manager mcp.md`.

## Decisions so far

<!-- the index: one line per closed ticket, enough to judge relevance, then zoom the link for the detail the ticket holds -->

- [Remote MCP support in Codex CLI and Cursor Agent](tickets/001-remote-mcp-support-in-codex-cli-and-cursor-agent.md): Streamable HTTP + static bearer header is the common ground; Cloud Agents call from Cursor's backend (public URL needed); ~60 s tool timeout; fail closed.
- [Connectivity recipes for reaching the Local PC](tickets/002-connectivity-recipes-for-reaching-the-local-pc.md): Cloudflare Tunnel + Access for Cloud Agents, Tailscale serve or `ssh -R` for remote CLIs; Cloudflare caps bodies at 100 MB.
- [MCP server SDK landscape](tickets/003-mcp-server-sdk-landscape.md): all SDKs on spec `2026-07-28`; Go official SDK strongest on File Channel auth reuse and Windows distribution, Python weakest.
- [Choose implementation stack](tickets/004-choose-implementation-stack.md): Python + official `mcp` v2 in a self-built Starlette app with one auth middleware over `/mcp` and the File Channel; single-process uvicorn on an asserted Proactor loop; `uv`, Python `>=3.11`.
- [Choose auth mechanism](tickets/005-choose-auth-mechanism.md): multiple named static bearer tokens (`fmcp_…`), SHA-256 digests on disk, constant-time check on every route, 401 fail closed; no OAuth.
- [Define file tool set](tickets/006-define-file-tool-set.md): ten file tools; inline caps 256 KiB read / 1 MiB write; absolute paths confined by realpath + normcase under Allowed Roots.
- [Design File Channel](tickets/007-design-file-channel.md): `/files` download with Range and offset-checked resumable chunked upload (≤64 MiB), same bearer auth; `prepare_download` / `prepare_upload` tools.
- [Define Command Executor tool](tickets/008-define-command-executor-tool.md): off by default; `run_command` waits ≤50 s then becomes a Job (`get_job`, `cancel_job`); 32 KiB head+tail output caps.
- [Define push_to_remote tool](tickets/009-define-push-to-remote-tool.md): system `scp` in batch mode with user's ssh config; exact-match Remote Host allowlist; push and pull, both Job-backed.
- [Choose documented connectivity recipes](tickets/010-choose-documented-connectivity-recipes.md): Cloudflare Tunnel + Access for Cloud Agents (Funnel fallback), Tailscale Serve or `ssh -R` for remote CLIs.
- [Choose distribution method](tickets/011-choose-distribution-method.md): `uv tool install`, one `file-manager-mcp` script; foreground or Task Scheduler at logon, not a service.
- [Choose configuration format and location](tickets/012-choose-configuration-format-and-location.md): TOML in `%APPDATA%\file-manager-mcp\config.toml`, token digests in `tokens.json`; `--config` > env > default.
- [Define audit logging](tickets/013-define-audit-logging.md): JSON lines per request/tool call, rotated file, never tokens or content.
- [Define behavior under concurrent Remote Agents](tickets/014-define-behavior-under-concurrent-remote-agents.md): no locks; atomic renames, last writer wins, upload offset conflicts return 409.
- [Choose spec document structure and location](tickets/015-choose-spec-document-structure-and-location.md): single `docs/spec.md` citing deciding tickets.

## Not yet specified

None: the way to the destination is clear; see `docs/spec.md`.

## Out of scope

- Screen snapshot: not file management; revisit as a separate effort.
- SCP/SFTP server on the Local PC: duplicates the File Channel.
- Built-in NAT traversal or tunneling: connectivity is delegated to existing tools (documented, not built).
- Sandboxing the Command Executor: accepted risk for a single-user tool.
- Multi-user support.
