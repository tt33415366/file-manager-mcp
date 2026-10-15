# MCP Setup Guide Page — Design

Status: approved 2026-10-14. Reference: Serial Bridge's Setup page (`D:\SourceCode\serial_bridge\static\setup.html`).

## Goal

A page in the config UI that walks the operator through wiring a Remote Agent client to this server: connection path, client config snippet, token, and a Cursor rule for the Agent.

## Decisions

- Separate page `GET /setup` on the config UI listener (loopback Host check; key from the `#k=` fragment like `/`). Linked from the control panel rail and the token receipt, carrying `#k=`.
- Tokens stay hash-only. Snippets default to the environment variable `FILE_MANAGER_TOKEN` (`Bearer ${env:FILE_MANAGER_TOKEN}` for Cursor / `cursor-agent`, `bearer_token_env_var = "FILE_MANAGER_TOKEN"` for Codex). An "Issue token for this client" button calls the existing `POST /api/tokens` and fills the plaintext into the `setx` / `export` line and the Cursor Cloud Agents header value; it lives only in page memory.
- `GET /api/setup` (key required) returns running values: `mcp_url`, `port`, `public_base_url`, `allowed_hosts`, `executor_enabled`, `remote_enabled`, `roots`, `agent_rule`. The agent rule is a server-side constant; `run_command` and remote-transfer lines appear only when those features are on.

## Page

A five-step commissioning sheet:

1. Connection path: This PC (`http://127.0.0.1:<port>/mcp`), SSH `-R` (`ssh -N -R 127.0.0.1:<port>:127.0.0.1:<port> user@linuxbox`, URL `http://127.0.0.1:<port>/mcp` on the Linux box), Tailscale (`tailscale serve --bg <port>`, `https://<host>/mcp`), Cloudflare Tunnel (`https://<host>/mcp` + Cloudflare Access service token headers). Hostname input for Tailscale/Cloudflare, prefilled from `public_base_url`; warn when the host is not in the running `server.allowed_hosts`. An SVG wiring diagram (Agent → link → Local PC) lights the chosen route.
2. Client: Cursor desktop (`~/.cursor/mcp.json`), Cursor Cloud Agents (dashboard fields), Codex CLI (`~/.codex/config.toml`), `cursor-agent` CLI (`~/.cursor/mcp.json` on the remote machine).
3. Token: env-var default; issue button.
4. Config snippet for the chosen client and path, with Copy.
5. Agent rule, with Copy.

Same palette and fonts as the control panel.

## Testing

`GET /setup` serves the page and rejects a non-loopback Host; `/api/setup` requires the key and returns running values; the agent rule includes/omits executor and remote lines. One-off Playwright check of path/client switching and token issue.
