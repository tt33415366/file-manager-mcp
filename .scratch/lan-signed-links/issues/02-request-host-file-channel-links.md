# 02 — File Channel links from the request Host

**What to build:** `prepare_download` / `prepare_upload` links point at the address the Remote Agent actually used, so a LAN or SSH `-R` agent gets working `curl` commands. Design: decision 4.

**Blocked by:** 01

**Status:** done

- [x] Without `public_base_url`, links use `http://<Host header of the MCP request>`.
- [x] With `public_base_url`, links use it regardless of Host.
- [x] Without an HTTP request (in-memory client), links use the configured base URL.
- [x] `docs/spec.md` section 5.2 updated.
