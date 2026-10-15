# 01 — LAN addresses and live Host allowlist

**What to build:** A LAN client can reach the MCP endpoint at `http://<Local PC IPv4>:<port>/mcp` without editing `server.allowed_hosts`, and the startup summary prints those URLs. Design: `docs/superpowers/specs/2026-10-14-lan-and-signed-links-design.md` (decisions 1–3).

**Blocked by:** none

**Status:** done

- [x] Own interface IPv4 addresses are listed live (short cache), excluding loopback and link-local; a default LAN address comes from the OS route toward the internet.
- [x] A request whose `Host` is an own IPv4 literal, with or without `:<port>`, passes the Host check on `/mcp` and `/files`; other IP literals still get 421.
- [x] An address added after startup is accepted without a restart.
- [x] With `server.host` `0.0.0.0` or empty, the startup summary prints `LAN: http://<ip>:<port>/mcp (plain HTTP)` per own IP; with a loopback host it prints none.
- [x] `docs/spec.md` sections 3 and 12 updated.
