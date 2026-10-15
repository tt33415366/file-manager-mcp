---
id: 008
title: Define Command Executor tool
labels: [wayfinder:grilling]
status: closed
assignee: weiqi
blocked_by: []
---

## Question

What is the Command Executor tool's contract: parameters (command, shell, cwd, env, timeout), output capture and size limits, long-running / background commands, exit-code reporting, and how the user enables it?

## Resolution

Resolved with the owner's standing auto-approval of recommended answers (2026-10-14).

- **Enablement**: `command_executor.enabled = false` by default. When disabled, its tools are not registered at all. When enabled, startup prints a risk warning to stderr: commands run unsandboxed as the current user.
- **Shell**: chosen by config, not by the caller: `powershell` (default on Windows; `powershell.exe -NoProfile -NonInteractive -Command`, with `[Console]::OutputEncoding` forced to UTF-8), `pwsh`, `cmd` (`cmd.exe /d /s /c`), or `sh` (default elsewhere; `/bin/sh -c`).
- **`run_command(command, cwd=None, env=None, wait_sec=30)`**: `cwd` defaults to the first Allowed Root and must be an existing directory (not confined: the executor is unsandboxed anyway); `env` entries are merged over the server's environment; stdin is closed. Waits up to `wait_sec` (max 50, under the ~60 s client tool timeout):
  - finished → `{status: "exited", exit_code, stdout, stderr, duration_ms, truncated}`;
  - still running → it becomes a **Job** and returns `{status: "running", job_id, stdout, stderr}` with output so far.
- **Jobs** (shared with "Define push_to_remote tool" (009)): in-memory, lost on restart. `get_job(job_id, wait_sec=0)` returns the same shape as `run_command`; `cancel_job(job_id)` kills the whole process tree (`taskkill /T /F` on Windows, process group kill elsewhere) and reports `status: "cancelled"`. Finished jobs are kept for 1 hour.
- **Output**: stdout and stderr captured separately and decoded as UTF-8 with replacement. Each stream returned is capped at 32 KiB; when exceeded, the first and last 16 KiB are kept around a `...[N bytes truncated]...` marker and `truncated` is true. A job buffers at most 1 MiB per stream (head and tail kept the same way).
- Exit codes are reported verbatim; a non-zero exit code is not a tool error.

**Amended (2026-10-15)**: argv quoting mangled quotes for `cmd`, so it now runs as the verbatim command line `cmd.exe /c "<command>"`; process-tree kill uses a Windows Job Object (with `taskkill` as fallback) so descendants that outlive the shell die too; shells are resolved from absolute `PATH` entries, never the current directory. See `docs/spec.md` sections 7 and 8.
