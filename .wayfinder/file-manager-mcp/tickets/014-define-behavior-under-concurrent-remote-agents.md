---
id: 014
title: Define behavior under concurrent Remote Agents
labels: [wayfinder:grilling]
status: closed
assignee: weiqi
blocked_by: ["006", "007", "008"]
---

## Question

What guarantees does the server give when several Remote Agents call tools or the File Channel at the same time, especially on the same paths?

## Resolution

Resolved with the owner's standing auto-approval of recommended answers (2026-10-14).

- No locks and no sessions: every request is independent; Remote Agents coordinate among themselves if they need to.
- Guarantees are per operation: `write_text` overwrite and upload completion are atomic renames (readers see the old or the new file, never a mix); last writer wins.
- Two uploads to the same path share one partial file; the offset check makes the loser see `409` with the current offset rather than corrupting silently.
- Jobs are global (any token can `get_job` / `cancel_job` any job id); ids are random UUIDs.
- Blocking filesystem and subprocess work runs off the event loop so one slow call does not stall others.

**Amended (2026-10-15)**: the offset check alone let two concurrent chunks at the same offset both pass, so upload requests on the same partial now run one at a time; that per-partial serialization is the one exception to "no locks". See `docs/spec.md` sections 6 and 11.
