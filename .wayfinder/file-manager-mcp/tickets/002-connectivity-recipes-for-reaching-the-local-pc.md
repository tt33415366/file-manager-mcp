---
id: 002
title: Connectivity recipes for reaching the Local PC
labels: [wayfinder:research]
status: closed
assignee: research-subagent
blocked_by: []
---

## Question

Which existing tools can make a port on a NAT'd Windows Local PC reachable by (a) Cursor Cloud Agents running in Cursor's cloud and (b) Codex CLI / `cursor-agent` CLI on a remote Linux machine? Evaluate at least Tailscale (incl. Funnel), `ssh -R` reverse tunnels, and cloudflared: whether each works for each case, setup cost, and security implications.

## Resolution

Findings: branch `research/connectivity-recipes` (commit `94c76e3`), file `research/connectivity-recipes.md`.

- Cloud Agents (case a) need a publicly reachable URL; a tailnet cannot serve them. Best fit: a named Cloudflare Tunnel (`cloudflared` as a Windows service) with Cloudflare Access service tokens sent as headers; needs a domain on Cloudflare. Fallback: Tailscale Funnel (no domain, but public with no extra auth layer, beta).
- Cloudflare limits: 100 MB request body on Free, 125 s time-to-first-byte, 900 s idle. Quick tunnels don't support SSE; avoid them.
- Remote Linux CLI (case b): Tailscale tailnet with `tailscale serve`, or `ssh -R` (no HTTP limits, needs a DIY reconnect loop).
- ngrok works for both but the free tier is 1 GB / 20k requests per month.
- Every recipe delivers requests via loopback, so the server must always require its own token.
