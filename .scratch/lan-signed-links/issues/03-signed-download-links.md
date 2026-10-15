# 03 — Signed download links for DUTs

**What to build:** A Remote Agent calls `prepare_download(path, for_host=<DUT>)` and gets a tokenless, expiring `curl` the DUT can run, on the Local PC address that routes to the DUT. Design: decisions 5–6; ADR 0003.

**Blocked by:** 02

**Status:** done

- [x] `for_host` resolves to the local source address the OS routes to it; links use `http://<that address>:<port>`.
- [x] `INVALID_ARGUMENT` when `for_host` does not resolve, routes to loopback, or the listener does not cover the chosen address (message says to set `server.host` to `0.0.0.0` and restart).
- [x] `expires_in` defaults to 3600 and must be 60–86400; the link carries `exp`, `by`, `sig` and its `curl` has no `Authorization` header.
- [x] `GET /files/download` with a valid signature works without a token, repeatedly until `exp`; a tampered path/exp/by, an expired link, a signature from another launch, or any other route/method → 403 `invalid_link`.
- [x] A signed path moved out of the Allowed Roots after issue → 403 `OUTSIDE_ROOTS`.
- [x] Signed HTTP requests are audited with `token = "signed:<issuer>"` and `client`.
- [x] `docs/spec.md` sections 3, 5.2, 6, 10 updated.
