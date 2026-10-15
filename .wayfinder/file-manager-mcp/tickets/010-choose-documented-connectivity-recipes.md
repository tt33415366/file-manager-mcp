---
id: 010
title: Choose documented connectivity recipes
labels: [wayfinder:grilling]
status: closed
assignee: weiqi
blocked_by: []
---

## Question

Which connectivity recipes does the spec recommend for Cursor Cloud Agents (needs a public URL) and for Codex CLI / `cursor-agent` on a remote Linux machine, given "Connectivity recipes for reaching the Local PC" (002)? Settle whether requiring a Cloudflare-hosted domain is acceptable, which fallback is offered, and which recipe limits the spec must design around.

## Resolution

Resolved with the owner's standing auto-approval of recommended answers (2026-10-14).

- **Cursor Cloud Agents**: named Cloudflare Tunnel (`cloudflared service install`) publishing `https://fm.<domain>` to `http://127.0.0.1:<port>`, gated by a Cloudflare Access Service Auth policy. Requiring a domain on Cloudflare is accepted. MCP headers in the Cursor dashboard: `Authorization`, `CF-Access-Client-Id`, `CF-Access-Client-Secret`; the same values go into Runtime Secrets for File Channel `curl`s.
- **Fallback for Cloud Agents**: Tailscale Funnel on port 443 (`tailscale funnel --bg <port>`), protected only by the server's own bearer token.
- **Remote Linux CLIs (Codex, `cursor-agent`)**: Tailscale tailnet + `tailscale serve --bg <port>` (unattended mode on). Alternative: `ssh -N -R 127.0.0.1:<port>:127.0.0.1:<port>` from the PC, with a self-managed reconnect loop.
- **Documented as avoid**: Cloudflare quick tunnels (no SSE) and ngrok free tier (1 GB/month).
- **Limits the server designs around**:
  - 100 MB request bodies → upload chunks ≤ 64 MiB (007).
  - 125 s time-to-first-byte and ~60 s client tool timeout → no tool call blocks longer than 50 s; long work becomes a Job (008, 009).
  - Codex ~10k-token output truncation → paginated listings, capped reads and command output (006, 008).
  - Every recipe arrives via loopback → the server always requires its bearer token and binds `127.0.0.1` by default.
  - Tunnel hostnames must be addable to the Host allowlist (005).
