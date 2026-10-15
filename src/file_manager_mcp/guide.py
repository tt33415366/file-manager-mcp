"""Running values and the Agent rule for the config UI's MCP setup guide page."""

from __future__ import annotations

from typing import Any

from .addressing import Addressing
from .config import Config
from .executor import KIND as COMMAND_KIND
from .remote import KIND as TRANSFER_KIND
from .runtime import FEATURES

_RULE_HEAD = """\
# File Manager MCP

The `file-manager` MCP server manages files on the user's Local PC (Windows).

- Call `list_roots` first. Every path is an absolute Local PC path inside an Allowed Root. \
`NO_ALLOWED_ROOTS` means the operator has not added one yet: ask them instead of retrying.
- Errors start with a code (`NOT_FOUND:`, `OUTSIDE_ROOTS:`, `ALREADY_EXISTS:`, ...). Read the code before retrying.
- `read_text` returns up to 256 KiB per call: continue from `offset + bytes_read` until `eof`. \
`write_text` takes up to 1 MiB (`mode`: create, overwrite, append).
- For larger or binary files call `prepare_download` / `prepare_upload` and run the returned `curl` \
commands with the same bearer token.
- To hand a file to a DUT (a device on another network of the Local PC), call `prepare_download` with \
`for_host=<DUT IP>` and run the returned `curl` on the DUT: it needs no token and expires. `prepare_upload` \
with `for_host` lets the DUT send a file back.
- Prefer `search` (`name_glob`, `content_regex`) over walking `list_dir` page by page.
- `delete` is permanent; non-empty directories need `recursive=true`.
"""

_RULE_TAIL = """\
- Tools appear or disappear when the operator changes settings. If a tool is missing, list tools again \
instead of guessing.
"""


def agent_rule(config: Config) -> str:
    rules = [feature.rule for feature in FEATURES.values() if feature.enabled(config)]
    return "".join([_RULE_HEAD, *rules, _RULE_TAIL])


def setup_payload(config: Config, addressing: Addressing) -> dict[str, Any]:
    return {
        "lan_ips": addressing.lan_ips(),
        "lan_default": addressing.lan_default(),
        "listen_host": config.server.host,
        "listens_on_lan": addressing.listens_on_lan,
        "mcp_url": addressing.mcp_url,
        "local_url": addressing.local_url,
        "port": config.server.port,
        "public_base_url": config.server.public_base_url,
        "allowed_hosts": list(config.server.allowed_hosts),
        "roots": len(config.allowed_roots),
        "executor_enabled": FEATURES[COMMAND_KIND].enabled(config),
        "remote_enabled": FEATURES[TRANSFER_KIND].enabled(config),
        "agent_rule": agent_rule(config),
    }
