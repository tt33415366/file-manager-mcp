# 04 — Signed upload links for DUTs

**What to build:** A Remote Agent calls `prepare_upload(path, for_host=<DUT>, overwrite=..., max_bytes=...)` and the DUT can push a file back without a token. Design: decision 5.

**Blocked by:** 03

**Status:** done

- [x] `prepare_upload` accepts `for_host`, `expires_in`, `overwrite=False`, `max_bytes=None`; signed links cover upload status, chunk PUT, discard, and complete for that path only.
- [x] Complete uses the signed `overwrite`; a query `overwrite` cannot change it.
- [x] A chunk that would grow the partial beyond `max_bytes` → 413.
- [x] After a successful complete the link is void (403 `invalid_link`) for every upload route.
- [x] Download routes reject upload links and vice versa.
- [x] `docs/spec.md` sections 5.2 and 6 updated.
