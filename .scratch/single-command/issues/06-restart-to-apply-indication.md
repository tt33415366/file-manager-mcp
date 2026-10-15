# 06 — Restart-to-apply indication

**What to build:** The operator can see which saved settings are not yet in effect. The UI shows the running values for settings that need a restart and flags fields whose saved value differs. Design: `docs/superpowers/specs/2026-10-14-single-command-design.md` (Config UI changes).

**Blocked by:** 04 — Single command start

**Status:** done (875ce0a)

- [x] Reading the config through the UI API also returns `running.server` and `running.logging` with the values the process is using.
- [x] Network and Audit Log fields whose saved value differs from the running value show a `restart to apply` tag; the tag clears when they match again.
- [x] After a save that includes restart-only changes, the dock message reads `Applied. Restart needed for: <setting names>`; otherwise `Applied.`
- [x] A browser check (Playwright) captures the tag and dock message after changing `server.port`.
