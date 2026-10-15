"""Runtime: the in-process owner of config, tools, and jobs, with live config apply (spec sections 5, 7-9)."""

from __future__ import annotations

import asyncio
import copy
import functools
import inspect
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from typing import Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from . import interfaces as os_interfaces
from .addressing import Addressing, Interfaces
from .audit import AuditLog, tool_audit_middleware
from .channel import FileChannel
from .config import Config
from .errors import FileManagerError, from_os_error
from .executor import KIND as COMMAND_KIND
from .executor import CommandExecutor, default_shell
from .files import FileOps
from .jobs import JobRegistry
from .paths import PathPolicy
from .remote import KIND as TRANSFER_KIND
from .remote import RemoteTransfer
from .signed import LinkSigner
from .tokens import TokenStore

INSTRUCTIONS = (
    "File Manager MCP: manage files on the user's Local PC. Paths are absolute Local PC paths inside the "
    "Allowed Roots; call list_roots first. read_text returns up to 256 KiB per call and write_text accepts up "
    "to 1 MiB; for larger or binary files call prepare_download / prepare_upload and use the File Channel "
    "over HTTP with the same bearer token. To hand a file to or from a DUT (a device on another network of "
    "this PC), pass for_host to prepare_download / prepare_upload and run the returned curl on the DUT; it "
    "needs no token. Errors start with a code such as NOT_FOUND:. Long-running commands and transfers return "
    "status 'running' with a job_id; poll get_job."
)

READ_ONLY = ToolAnnotations(readOnlyHint=True)
MUTATING = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

DESCRIPTIONS = {
    "list_roots": "List the Allowed Roots: the only directory trees this server can touch.",
    "list_dir": "List a directory, sorted by name, paginated: pass next_cursor back as cursor. limit 1-1000.",
    "stat": "Describe a path: exists, type (file|dir|symlink|other), size, modified. exists=false if missing.",
    "read_text": (
        "Read text from a file starting at byte offset, up to max_bytes (max 262144). Never splits a "
        "character; continue from offset + bytes_read until eof. Binary files fail with NOT_TEXT."
    ),
    "write_text": (
        "Write text (max 1 MiB encoded). mode: create (fail if exists), overwrite (atomic replace), append. "
        "create_parents makes missing parent directories."
    ),
    "make_dir": "Create a directory and any missing parents; an existing directory is fine.",
    "move": "Move or rename a file or directory. overwrite only replaces an existing file with a file.",
    "copy": "Copy a file or directory (recursive). overwrite only replaces an existing file with a file.",
    "delete": "Permanently delete a file, link, or directory; non-empty directories need recursive=true.",
    "search": (
        "Recursively search under a directory. name_glob matches names (e.g. '*.py'); content_regex "
        "additionally matches lines in UTF-8 text files up to 1 MiB. Stops at limit (truncated=true)."
    ),
    "prepare_download": (
        "Get a File Channel URL and curl example to download a file of any size (supports Range). With for_host "
        "(a DUT's IP or hostname) the link is a tokenless Signed Link the DUT can curl, valid expires_in seconds."
    ),
    "prepare_upload": (
        "Get File Channel URLs to upload a file of any size: PUT chunks (max_chunk_bytes) to upload_url&offset=N "
        "starting at the returned offset, then POST complete_url (optionally &sha256=..). With for_host (a DUT's "
        "IP or hostname) the links are a tokenless Signed Link for the DUT with overwrite and max_bytes fixed; "
        "they stop working after the upload completes."
    ),
    "run_command": (
        "Run a shell command on the Local PC (unsandboxed, as the user). Waits up to wait_sec (max 50); "
        "if still running returns status 'running' with job_id. Non-zero exit codes are results."
    ),
    "get_job": "Get a job's status and output, waiting up to wait_sec (max 50) for it to finish.",
    "cancel_job": "Cancel a running job, killing its whole process tree.",
    "push_to_remote": (
        "Copy a local file or directory (recursive=true) to an allowlisted Remote Host with scp. "
        "Waits up to wait_sec (max 50), then continues as a job."
    ),
    "pull_from_remote": (
        "Copy a file or directory (recursive=true) from an allowlisted Remote Host to a local path with scp. "
        "Waits up to wait_sec (max 50), then continues as a job."
    ),
}

JOB_TOOLS = frozenset({"get_job", "cancel_job"})


@dataclass(frozen=True)
class Feature:
    """An optional capability: when it is on, the tools it adds, and its lines in the Agent rule."""

    enabled: Callable[[Config], bool]
    tools: frozenset[str]
    rule: str


FEATURES = {
    COMMAND_KIND: Feature(
        enabled=lambda config: config.command_executor.enabled,
        tools=frozenset({"run_command"}),
        rule="""\
- `run_command` runs unsandboxed on the Local PC as the user. If it returns status `running`, poll \
`get_job` (`wait_sec` up to 50) and stop it with `cancel_job`.
""",
    ),
    TRANSFER_KIND: Feature(
        enabled=lambda config: bool(config.remote.allowed_hosts),
        tools=frozenset({"push_to_remote", "pull_from_remote"}),
        rule="""\
- `push_to_remote` / `pull_from_remote` copy with scp to and from allowlisted Remote Hosts only; long \
transfers continue as jobs (`get_job`).
""",
    ),
}
RESTART_SECTIONS = ("server", "logging")
RESTART_FIELDS = tuple(
    f"{section}.{f.name}" for section in RESTART_SECTIONS for f in fields(getattr(Config(allowed_roots=[]), section))
)
EXECUTOR_WARNING = (
    "WARNING: Command Executor is enabled. Remote Agents can run any command on this PC, "
    "unsandboxed, as the current user."
)

logger = logging.getLogger(__name__)


def _error_result(exc: FileManagerError) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=str(exc))], is_error=True)


def _errors_as_results(fn: Callable) -> Callable:
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except FileManagerError as exc:
                return _error_result(exc)
            except OSError as exc:
                return _error_result(from_os_error(exc))

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except FileManagerError as exc:
            return _error_result(exc)
        except OSError as exc:
            return _error_result(from_os_error(exc))

    return wrapper


def _changed(section: str, old: Any, new: Any) -> list[str]:
    return [f"{section}.{f.name}" for f in fields(old) if getattr(old, f.name) != getattr(new, f.name)]


class Runtime:
    def __init__(
        self,
        config: Config,
        tokens: TokenStore,
        audit: AuditLog | None = None,
        interfaces: Interfaces = os_interfaces,
    ) -> None:
        config = copy.deepcopy(config)
        self.config = config
        self.tokens = tokens
        self.audit = audit
        self.startup_token: str | None = None
        self._apply_lock = asyncio.Lock()
        self.addressing = Addressing(config.server, interfaces)
        self.policy = PathPolicy(config.allowed_roots)
        self.jobs = JobRegistry()
        self.executor = CommandExecutor(self.policy, config.command_executor.shell, self.jobs)
        self.transfer = RemoteTransfer(self.policy, config.remote.allowed_hosts, self.jobs)
        ops = FileOps(self.policy)
        self.signer = LinkSigner(issuers=lambda: {e.name for e in tokens.list()})
        self.channel = FileChannel(self.policy, self.addressing, self.signer)
        self._tools: dict[str, tuple[Callable, ToolAnnotations]] = {
            "list_roots": (ops.list_roots, READ_ONLY),
            "list_dir": (ops.list_dir, READ_ONLY),
            "stat": (ops.stat, READ_ONLY),
            "read_text": (ops.read_text, READ_ONLY),
            "write_text": (ops.write_text, DESTRUCTIVE),
            "make_dir": (ops.make_dir, MUTATING),
            "move": (ops.move, DESTRUCTIVE),
            "copy": (ops.copy, DESTRUCTIVE),
            "delete": (ops.delete, DESTRUCTIVE),
            "search": (ops.search, READ_ONLY),
            "prepare_download": (self.channel.prepare_download, READ_ONLY),
            "prepare_upload": (self.channel.prepare_upload, MUTATING),
            "get_job": (self._get_job, READ_ONLY),
            "cancel_job": (self._cancel_job, DESTRUCTIVE),
            "run_command": (self.executor.run_command, DESTRUCTIVE),
            "push_to_remote": (self.transfer.push, MUTATING),
            "pull_from_remote": (self.transfer.pull, DESTRUCTIVE),
        }
        self.server = MCPServer(
            "file-manager-mcp",
            instructions=INSTRUCTIONS,
            middleware=[tool_audit_middleware(audit)] if audit else None,
        )
        self._sync_tools(set(), self._tool_names(config))

    async def _get_job(self, job_id: str, wait_sec: float = 0) -> dict[str, Any]:
        return await self.jobs.wait_result(self.jobs.get(job_id), wait_sec)

    async def _cancel_job(self, job_id: str) -> dict[str, Any]:
        return await self.jobs.cancel(job_id)

    def _tool_names(self, config: Config) -> set[str]:
        names = set(self._tools) - JOB_TOOLS.union(*(f.tools for f in FEATURES.values()))
        for feature in FEATURES.values():
            if feature.enabled(config):
                names |= feature.tools | JOB_TOOLS
        return names

    def _sync_tools(self, old: set[str], new: set[str]) -> None:
        for name in old - new:
            self.server.remove_tool(name)
        for name, (fn, annotations) in self._tools.items():
            if name in new - old:
                self.server.add_tool(
                    _errors_as_results(fn),
                    name=name,
                    description=DESCRIPTIONS[name],
                    annotations=annotations,
                    structured_output=True,
                )

    async def apply(self, new: Config) -> dict[str, list[str]]:
        """Apply ``new`` to the running server; ``server`` and ``logging`` keep their running values.

        Calls run one at a time: an overlapping apply could reopen a job kind that a slower one then closes.
        """
        async with self._apply_lock:
            return await self._apply(new)

    async def _apply(self, new: Config) -> dict[str, list[str]]:
        old, new = self.config, copy.deepcopy(new)
        applied = ["allowed_roots"] if new.allowed_roots != old.allowed_roots else []
        applied += _changed("command_executor", old.command_executor, new.command_executor)
        applied += _changed("remote", old.remote, new.remote)
        restart_required = [n for s in RESTART_SECTIONS for n in _changed(s, getattr(old, s), getattr(new, s))]

        self.policy.set_roots(new.allowed_roots)
        self.executor.shell = new.command_executor.shell or default_shell()
        self.transfer.allowed_hosts = list(new.remote.allowed_hosts)
        old_tools, new_tools = self._tool_names(old), self._tool_names(new)
        self._sync_tools(old_tools, new_tools)
        self.config = replace(new, **{s: getattr(old, s) for s in RESTART_SECTIONS})

        added, removed = new_tools - old_tools, old_tools - new_tools
        if FEATURES[COMMAND_KIND].tools & added:
            print(EXECUTOR_WARNING, file=sys.stderr, flush=True)
        closing = []
        for kind, feature in FEATURES.items():
            if feature.tools & added:
                self.jobs.open_kind(kind)
            if feature.tools & removed:
                closing.append(kind)
        results = await asyncio.gather(*(self.jobs.cancel_kind(kind) for kind in closing), return_exceptions=True)
        for kind, result in zip(closing, results):
            if isinstance(result, Exception):
                logger.error("cancelling running %s jobs failed", kind, exc_info=result)
        return {"applied": applied, "restart_required": restart_required}
