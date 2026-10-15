# 03 — Runtime with live apply

**What to build:** A single in-process Runtime owns the current config, token store, shared path policy, MCP server, job registry, Command Executor, and remote transfer. Applying a new config updates the running server immediately where possible and reports what still needs a restart. Design: `docs/superpowers/specs/2026-10-14-single-command-design.md` (Runtime and live apply).

**Blocked by:** 01 — Prefactor: one shared PathPolicy

**Status:** done (875ce0a)

- [x] Applying a config returns `{"applied": [...], "restart_required": [...]}` listing the dotted names of changed settings.
- [x] Changed `allowed_roots` take effect on the next tool call.
- [x] Enabling/disabling the Command Executor adds/removes `run_command` from the tool list; a changed shell is used by the next `run_command`.
- [x] A non-empty/empty remote allowlist adds/removes `push_to_remote` and `pull_from_remote`; allowlist edits apply to the next transfer.
- [x] `get_job` and `cancel_job` are listed exactly when the Command Executor is on or the remote allowlist is non-empty.
- [x] Disabling the Command Executor cancels its running jobs (process tree killed); removing the remote tools cancels running transfer jobs.
- [x] Changes to `server.*` or `logging.audit_file` appear in `restart_required` and do not alter the running values.
- [x] Tests drive these through an in-memory MCP client (`list_tools`, tool calls); no `tools/list_changed` notification is sent.
