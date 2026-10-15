---
id: 013
title: Define audit logging
labels: [wayfinder:grilling]
status: closed
assignee: weiqi
blocked_by: ["005", "012"]
---

## Question

What does the server record about each tool call and File Channel request, where, in what format, and what must never be logged?

## Resolution

Resolved with the owner's standing auto-approval of recommended answers (2026-10-14).

- **What**: one JSON line per authenticated HTTP request and per tool call: `ts` (ISO 8601 UTC), `token` (token name), `kind` (`tool` | `http`), `name` (tool name or `METHOD /route`), `args` (path-like arguments and the command string; never file content), `outcome` (`ok` | error code | HTTP status), `duration_ms`. Rejected auth attempts are logged with `token: null` and the client address.
- **Never logged**: tokens, `Authorization` / `CF-Access-*` headers, file content, `write_text` content, `env` values.
- **Where**: `audit.log` in the log directory (`%LOCALAPPDATA%\file-manager-mcp\logs\` on Windows, `$XDG_STATE_HOME/file-manager-mcp/` (default `~/.local/state/file-manager-mcp/`) elsewhere) unless `logging.audit_file` is set; rotated at 10 MB, 5 files kept. Operational messages (startup, warnings) go to stderr.
