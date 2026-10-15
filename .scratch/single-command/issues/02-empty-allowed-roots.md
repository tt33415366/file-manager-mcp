# 02 — Empty Allowed Roots no longer blocks startup

**What to build:** A config with no Allowed Roots is valid, so the server can run before the operator has chosen any directory. Remote Agents get a clear, actionable error instead of a startup failure. Design: `docs/superpowers/specs/2026-10-14-single-command-design.md` (Empty Allowed Roots).

**Blocked by:** None — can start immediately.

**Status:** done (875ce0a)

- [x] Config validation accepts an empty `allowed_roots`; a root that is not an existing directory is still rejected.
- [x] `list_roots` returns an empty list when no roots are configured.
- [x] Every path-taking tool returns error code `NO_ALLOWED_ROOTS` with the message `No Allowed Roots configured yet; ask the operator to add one in the config UI.`
- [x] File Channel download and upload return HTTP 403 with `NO_ALLOWED_ROOTS` in the JSON body.
- [x] The config UI no longer treats empty roots as a save error (its "server will not start" hint is updated to describe the new behavior).
- [x] `docs/spec.md` section 2 no longer lists empty roots as a startup error and documents the new error code.
