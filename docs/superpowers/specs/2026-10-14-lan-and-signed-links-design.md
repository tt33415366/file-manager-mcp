# LAN direct path and Signed Links — Design

Status: approved 2026-10-14 (grilling session Q1–Q22). Decision record: ADR 0003. Glossary: Connection Path, Signed Link, DUT in `CONTEXT.md`.

## Problem

With `server.host = "0.0.0.0"` the startup summary and the Setup guide show `http://127.0.0.1:<port>/mcp`, and the Host allowlist only admits loopback, so a client on another LAN machine has neither the right address nor a working one. File Channel links use one base URL fixed at startup, so a Remote Agent on the LAN gets `curl` commands pointing at itself. And a DUT on a second network of the Local PC cannot fetch or deliver files at all without holding a full MCP token.

## Decisions

1. **Local addresses.** "Own interface IPs" are the Local PC's current IPv4 addresses, excluding `127.0.0.0/8` and `169.254.0.0/16`, cached for a few seconds. The default LAN address is the source address the OS routes toward the internet (UDP `connect`, no packet sent).
2. **Host allowlist.** Besides loopback and `server.allowed_hosts`, a `Host` of `<own IPv4>` or `<own IPv4>:<port>` is accepted, checked live. `AuthMiddleware` is the single Host/Origin gate for every path; the SDK's duplicate check is turned off because its list is static.
3. **Startup summary.** When `server.host` is `0.0.0.0` or empty, one extra line per own IP: `LAN: http://<ip>:<port>/mcp (plain HTTP)`.
4. **File Channel links.** `public_base_url` if set; else `http://<Host of the requesting MCP call>`; else (no HTTP request, e.g. in-memory tests) the configured base URL.
5. **Signed Links.** `prepare_download` and `prepare_upload` gain `for_host` (DUT IP or hostname) and `expires_in` (seconds, default 3600, 60–86400).
   - The server resolves `for_host` to IPv4, asks the OS which local address routes to it, and builds links on `http://<that address>:<port>`. The result must be an own interface IP (not loopback) and must be covered by the listener (`server.host` wildcard or equal to it), else `INVALID_ARGUMENT` explaining what to change.
   - The query carries `exp`, `by` (issuing token name), for uploads `overwrite` and optional `max_bytes`, and `sig` = HMAC-SHA256 over direction, path, and those fields, keyed by a per-launch random secret.
   - Download links allow `GET` on `/files/download`, reusable until `exp`. Upload links allow `GET`/`PUT`/`DELETE` on `/files/upload` and `POST /files/upload/complete`; `overwrite` and `max_bytes` come from the signed fields, and the link is void after a successful complete.
   - Every use re-checks the current Allowed Roots. Bad, expired, or consumed links → `403 {"error": "invalid_link"}`.
   - `curl` for Signed Links has no `Authorization` header.
6. **Audit.** Signed requests are logged with `token = "signed:<by>"` and `client`; the issuing tool call already records `for_host`.
7. **Setup guide.** New LAN Connection Path: address dropdown from `/api/setup` (`lan_ips`, `lan_default`), default when there is no `public_base_url` and the listener is a wildcard; red guard and broken wire when the running listener is loopback; plain-HTTP warning recommending Tailscale. "This PC" is labelled local testing. Panel listen-host warning mentions LAN plain HTTP. Agent rule and tool descriptions mention `for_host`.

## Out of scope

IPv6 own addresses, HTTPS on the LAN, persisted signing keys, per-DUT tokens.
