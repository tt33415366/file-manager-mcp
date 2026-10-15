---
id: 001
title: Remote MCP support in Codex CLI and Cursor Agent
labels: [wayfinder:research]
status: closed
assignee: research-subagent
blocked_by: []
---

## Question

What remote MCP support do Codex CLI, Cursor Cloud Agents, and the `cursor-agent` CLI each offer? For each: supported transports (stdio, SSE, Streamable HTTP), whether custom headers / bearer tokens are supported, whether OAuth is supported or required, and the exact config file location and shape for adding a remote MCP server.

## Resolution

Findings: branch `research/remote-mcp-client-support` (commit `05f3566`), file `research/remote-mcp-client-support.md`.

- The only combination all three clients support is **Streamable HTTP at a public HTTPS URL with a static `Authorization: Bearer` token**. Codex CLI and Cursor Cloud Agents reject legacy SSE.
- Cursor Cloud Agents send HTTP MCP calls from Cursor's backend, not the agent VM, so the Local PC needs a public endpoint for them.
- Config: Codex uses `bearer_token_env_var` / `env_http_headers` in `~/.codex/config.toml`; `cursor-agent` uses `headers` with `${env:NAME}` in `~/.cursor/mcp.json`; Cloud Agents use a literal header entered in the cursor.com dashboard (no env interpolation).
- OAuth is supported by all three and required by none; it is awkward on headless Linux.
- Constraints for later tickets: ~60 s tool-call timeout (fixed in `cursor-agent`), Codex truncates tool output to ~10k tokens, and Codex connects without auth if no credential is configured, so the server must fail closed with 401.
