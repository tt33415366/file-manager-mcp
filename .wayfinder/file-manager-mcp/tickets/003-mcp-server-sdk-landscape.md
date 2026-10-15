---
id: 003
title: MCP server SDK landscape
labels: [wayfinder:research]
status: closed
assignee: research-subagent
blocked_by: []
---

## Question

How do the Python (official SDK / FastMCP), TypeScript (official SDK), and Go MCP server SDKs compare for this server? Cover: maturity, Streamable HTTP server support, auth hooks, ease of serving an extra plain HTTP endpoint (the File Channel) alongside MCP, Windows support, and distribution options (single exe, `uvx`, `npx`, etc.).

## Resolution

Findings: branch `research/mcp-sdk-landscape` (commit `78e897a`), file `research/mcp-sdk-landscape.md`.

- All five SDKs (Python `mcp` 2.3, FastMCP 4.1, TypeScript v2, Go `go-sdk` 1.8, `mcp-go` 1.2) support MCP spec `2026-07-28`, which drops `initialize`/sessions for stateless per-request calls.
- Differentiators are auth reuse on the File Channel and Windows distribution:
  - Go `go-sdk`: bearer middleware guards any route, `http.ServeContent` gives range requests, single small native `.exe`, no Windows quirks.
  - TypeScript v2: `requireBearerAuth` guards any route but demands token expiry; single-exe builds are ~75-115 MB.
  - Python / FastMCP: `custom_route` endpoints are never authenticated; Windows subprocesses need the Proactor loop; distribution via `uvx` or PyInstaller (AV false positives).
- Built-in auth helpers assume OAuth; a ~10-line constant-time static-token check works in any stack.
