---
id: 011
title: Choose distribution method
labels: [wayfinder:grilling]
status: closed
assignee: weiqi
blocked_by: []
---

## Question

How is the Python server (decided in "Choose implementation stack" (004)) installed, started, and upgraded on the Local PC: `uvx` / `uv tool install`, PyInstaller `--onedir`, a plain virtualenv, or something else? Weigh install prerequisites, antivirus/SmartScreen friction, upgrade path, and whether it should run as a background service or a foreground process.

## Resolution

Resolved with the owner's standing auto-approval of recommended answers (2026-10-14).

- **Install**: `uv tool install <path-or-git-url>` (prerequisite: `uv`), exposing one console script, `file-manager-mcp`. Upgrade with `uv tool upgrade file-manager-mcp`. PyInstaller is rejected (antivirus false positives, no upgrade path).
- **Commands**: `file-manager-mcp serve [--config PATH] [--host H] [--port N]`, `file-manager-mcp token add|list|remove`, `file-manager-mcp init` (writes a commented starter config if none exists).
- **Running**: a foreground process by default. Auto-start is documented as a Task Scheduler task "at log on" running as the user, **not** a Windows service: a service runs under another account, so it would see the wrong user profile, ssh config, and mapped drives.
