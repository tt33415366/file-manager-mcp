# File Manager MCP

An MCP server running on a user's local PC that lets agents running elsewhere manage files on that PC.

## Language

**Local PC**:
The user's own machine (primarily Windows) that holds the files and runs the File Manager MCP server.
_Avoid_: Host, client machine, workstation

**Remote Agent**:
An AI agent running off the Local PC (cloud or remote Linux server) that calls the File Manager MCP server's tools.
_Avoid_: Client, bot, remote

**Connection Path**:
How a Remote Agent's requests reach the Local PC: LAN direct, SSH reverse tunnel, Tailscale, or Cloudflare Tunnel.
_Avoid_: Route, transport, network mode

**File Channel**:
A dedicated HTTP endpoint on the Local PC for moving file bytes, separate from MCP messages, so large files never travel inside a tool call.
_Avoid_: HTTP server, file server, download server

**Signed Link**:
A File Channel URL that a Remote Agent obtains for a DUT, valid for one path and one direction until it expires, and usable without a token.
_Avoid_: Presigned URL, share link, token link

**DUT**:
A device on one of the Local PC's networks that exchanges files with the File Channel only through Signed Links; it holds no token and is not a Remote Agent.
_Avoid_: Device, target, Remote Host

**Caller**:
Who a request to the Local PC acts for: a Remote Agent identified by its token's name, or a DUT using a Signed Link, identified by the token that issued that link.
_Avoid_: User, client, principal, requester

**Allowed Root**:
A directory on the Local PC that the user has configured as reachable; file tools can only touch paths inside an Allowed Root.
_Avoid_: Share, mount, workspace

**Command Executor**:
The opt-in capability that runs shell commands (PowerShell on Windows) on the Local PC on a Remote Agent's behalf.
_Avoid_: Terminal, remote shell, exec

**Job**:
A long-running process (a command or a remote transfer) that outlived its tool call's wait and is tracked by id until it exits or is cancelled.
_Avoid_: Task, background process, ticket

**Remote Host**:
A Linux server, named in the user's ssh configuration and allowlisted, that the Local PC pushes files to or pulls files from.
_Avoid_: Target, destination server, Remote Agent
