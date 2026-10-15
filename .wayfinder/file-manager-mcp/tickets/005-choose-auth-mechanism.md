---
id: 005
title: Choose auth mechanism
labels: [wayfinder:grilling]
status: closed
assignee: weiqi
blocked_by: ["001"]
---

## Question

How exactly do Remote Agents authenticate to the MCP server, so that both Codex CLI and Cursor Agent can use it given "Remote MCP support in Codex CLI and Cursor Agent" (001)? Settle token format, how it is generated, stored, sent, and rotated, and whether OAuth is needed.

## Resolution

Resolved with the owner's standing auto-approval of recommended answers (2026-10-14).

- **Scheme**: static bearer tokens sent as `Authorization: Bearer <token>`, the one scheme all three clients support. No OAuth, no RFC 9728 protected-resource metadata, no SDK `AuthSettings`/`TokenVerifier` (they would advertise a non-existent authorization server).
- **Multiple named tokens**: one per client (e.g. `cursor-cloud`, `codex-buildbox`) so each can be revoked alone; the Cloud Agent token cannot be read back from Cursor's dashboard, so per-client revocation is the rotation tool.
- **Format**: `fmcp_` + `secrets.token_urlsafe(32)` (256 bits of entropy).
- **Generation**: `file-manager-mcp token add <name>` prints the token once; `token list` shows names and creation dates; `token remove <name>` revokes.
- **Storage**: only the SHA-256 hex digest is stored, in the machine-managed tokens file (location per "Choose configuration format and location" (012)). Unsalted SHA-256 is adequate for 256-bit random tokens.
- **Verification**: one outermost ASGI middleware (placement from "Choose implementation stack" (004)) hashes the presented token and compares against every stored digest with `hmac.compare_digest`. Missing or invalid token on **any** path returns `401` with `WWW-Authenticate: Bearer`; there are no unauthenticated routes. The matching token name is attached to the request for audit logging.
- **Rotation**: add a new token, update the client, remove the old one. Tokens are loaded at startup; changes take effect on restart.
- **Host/Origin**: the SDK's DNS-rebinding protection stays on; `allowed_hosts` defaults to loopback forms and is extended by config with tunnel hostnames. Requests carrying an `Origin` header must match an allowed origin; requests without `Origin` (non-browser agents) pass.
- **Secrets hygiene**: tokens are never logged; Cloudflare Access headers are enforced by Cloudflare, not by the server.
