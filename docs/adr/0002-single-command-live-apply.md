# One process with a loopback-only config UI listener and live apply

`file-manager-mcp` with no subcommand is the only command needed: one process runs the MCP listener on `server.host:server.port` and the config UI on its own listener bound to `127.0.0.1` on a random port, both on one event loop and sharing one Runtime, so a UI save applies to the running server without a restart (only `server.*` and `logging.audit_file` still need one). The UI keeps its own listener because tunnels forward only the MCP port, so the UI is never reachable from outside the PC; its per-launch key and loopback `Host` check stay as before. Serial Bridge's exemption of loopback clients from authentication is not adopted: SSH `-R`, cloudflared, and `tailscale serve` all deliver remote requests from loopback, so a loopback peer address says nothing about who is calling.

## Considered Options

- One port with the UI mounted at `/ui`: simplest wiring, but the UI would be reachable through every tunnel that exposes the MCP endpoint.
- A supervisor that restarts `serve` after each UI save: keeps today's server untouched, but every save drops MCP sessions and running jobs.
