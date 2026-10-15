# Python and the official MCP SDK for the server

The File Manager MCP server is built in Python on the official `mcp` SDK v2, chosen by the owner even though the SDK research rated Go's official `go-sdk` strongest (shared auth middleware for the File Channel, no Windows subprocess quirks, a single small native `.exe`). Python's known weaknesses are handled deliberately: SDK `custom_route` endpoints are never authenticated, so the server is a self-built Starlette app with one outermost auth middleware covering `/mcp` and the File Channel; uvicorn silently switches away from the Proactor loop under `reload`/`workers`, breaking async subprocesses on Windows, so it runs single-process with an explicit Proactor loop factory and a startup assertion; distribution friction (`uvx` needs `uv`, PyInstaller trips antivirus) is left to a dedicated distribution decision.

## Considered Options

- Go `go-sdk`: strongest on File Channel auth reuse, Windows subprocesses, and distribution; not chosen.
- TypeScript SDK v2: Node already installed, but 75–115 MB single-exe builds and a bearer helper that demands token expiry.
- FastMCP 4 and Go `mcp-go`: community SDKs adding a second release train with no offsetting advantage here.
