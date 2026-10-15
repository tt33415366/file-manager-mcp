---
id: 004
title: Choose implementation stack
labels: [wayfinder:grilling]
status: closed
assignee: weiqi
blocked_by: ["003"]
---

## Question

Which language and MCP SDK should the File Manager MCP server be built with, given the findings of "MCP server SDK landscape" (003)?

## Resolution

ADR: `docs/adr/0001-python-mcp-sdk-stack.md`.

- **Language / SDK**: Python with the official `mcp` SDK v2 (`MCPServer`); not FastMCP, not Go. Chosen by the owner over the Go `go-sdk` recommendation.
- **ASGI host**: a self-built Starlette app mounts `mcp.streamable_http_app()` at `/mcp`; File Channel endpoints are ordinary routes in the same app; one outermost ASGI auth middleware covers every path and denies by default. The app lifespan enters `mcp.session_manager.run()`. No `@mcp.custom_route` (never authenticated) and no FastAPI.
- **Process / event loop**: started programmatically via `uvicorn.Server(Config(...))`, single process, no `reload` or `workers`, with an explicit Proactor loop factory; startup asserts the running loop is `ProactorEventLoop` on Windows and exits otherwise.
- **Python floor**: `requires-python = ">=3.11"` (the SDK's floor).
- **Dev tooling**: `uv` manages `pyproject.toml`, the lockfile, and the virtualenv. This does not decide distribution.
- **Left to other tickets**: distribution method and configuration format graduate to "Choose distribution method" (011) and "Choose configuration format and location" (012); the credential and its verification stay with "Choose auth mechanism" (005), which only inherits the placement (one ASGI middleware for all routes).
