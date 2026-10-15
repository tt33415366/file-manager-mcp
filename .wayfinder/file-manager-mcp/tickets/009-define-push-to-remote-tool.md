---
id: 009
title: Define push_to_remote tool
labels: [wayfinder:grilling]
status: closed
assignee: weiqi
blocked_by: []
---

## Question

What is the `push_to_remote` tool's contract, where the Local PC acts as an SCP client pushing files to a Linux server: how it reuses the local ssh/scp and ssh config, how hosts and keys are referenced, whether there is a host allowlist, and whether pulling (fetch) is also offered?

## Resolution

Resolved with the owner's standing auto-approval of recommended answers (2026-10-14).

- **Mechanism**: runs the system `scp` (Windows OpenSSH `scp.exe`) as the current user, so `~/.ssh/config`, keys, the ssh-agent, and `known_hosts` apply unchanged. The server manages no keys and never prompts: `-B -o BatchMode=yes` makes missing keys or unknown host keys fail fast with scp's stderr in the result.
- **Remote Host allowlist**: `remote.allowed_hosts` lists exact host strings (ssh config aliases or `user@host`). The tools are registered only when the list is non-empty; a host not on the list fails with `HOST_NOT_ALLOWED`.
- **`push_to_remote(local_path, host, remote_path, recursive=False, wait_sec=30)`**: `local_path` must be inside an Allowed Root (directories need `recursive=True`).
- **`pull_from_remote(host, remote_path, local_path, recursive=False, wait_sec=30)`**: offered, symmetric; the local destination must be inside an Allowed Root.
- **Long transfers**: both wait up to `wait_sec` (max 50) and then hand off to a **Job** exactly like `run_command` ("Define Command Executor tool" (008)); `get_job` / `cancel_job` are registered whenever either feature is enabled.
- **Injection safety**: arguments are passed as a list (no shell); `--` precedes the operands; `remote_path` may not start with `-`; the host must match the allowlist exactly.
