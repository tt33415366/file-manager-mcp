# 05 — First-boot token handoff

**What to build:** On the very first start there is nothing to do by hand: if no tokens exist, a token named `default` is created, printed once in the terminal, and shown once in the opened config UI as a receipt slip with Copy and a ready-to-paste Cursor `mcpServers` snippet. Token storage stays hash-only. Design: `docs/superpowers/specs/2026-10-14-single-command-design.md` (Startup sequence step 4, Config UI changes).

**Blocked by:** 04 — Single command start

**Status:** done (875ce0a)

- [x] Starting with an empty token store creates a token named `default` and prints its plaintext once in the startup summary; starting again creates no new token and prints none.
- [x] The config UI API returns `{"name": "default", "token": "...", "mcp_url": "..."}` from the startup-token endpoint exactly once, then `{"token": null}`.
- [x] On load, if a startup token is available, the UI opens the token receipt showing the token, a Copy button, and an `mcpServers` snippet whose `url` is the MCP URL (using `public_base_url` when set) and whose `Authorization` header is `Bearer <token>`.
- [x] The token plaintext is never written to disk or to the audit log.
- [x] `docs/spec.md` section 2 documents the default config and `default` token created on first start.
