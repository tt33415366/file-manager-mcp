"""Command Executor: runs shell commands and Scripts as Jobs (spec section 8)."""

from __future__ import annotations

import math
import os
import re
import shutil
import uuid
from typing import Any

from mcp.server.mcpserver import Context

from .auth import caller_name
from .errors import FileManagerError
from .jobs import JobRegistry
from .paths import PathPolicy, no_allowed_roots

KIND = "command"
MAX_SCRIPT_BYTES = 1024 * 1024
_PS_ENCODING = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8"
_PS_PREFIX = _PS_ENCODING + "; "
_CMD_UNSAFE = re.compile(r'[&|<>^%"\r\n]')
_SCRIPT_SUFFIX = {"powershell": ".ps1", "pwsh": ".ps1", "cmd": ".cmd", "sh": ".sh"}


def default_shell() -> str:
    return "powershell" if os.name == "nt" else "sh"


def shell_argv(shell: str, command: str) -> list[str] | str:
    """The argv for ``shell``; for ``cmd`` the bare command, which JobRegistry.start hands to ``cmd.exe /d /v:off /c``."""
    if shell in ("powershell", "pwsh"):
        exe = "powershell.exe" if shell == "powershell" else "pwsh.exe"
        return [exe, "-NoProfile", "-NonInteractive", "-Command", _PS_PREFIX + command]
    if shell == "cmd":
        return command
    return ["/bin/sh", "-c", command]


def _encodable(text: str, what: str) -> None:
    """Reject text that cannot be written as UTF-8 or carries a NUL, which no shell or file can hold."""
    if "\0" in text:
        raise FileManagerError("INVALID_ARGUMENT", f"{what} contains a NUL character")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise FileManagerError("INVALID_ARGUMENT", f"{what} contains a lone surrogate, which is not valid UTF-8") from None


def script_text(shell: str, script: str) -> bytes:
    """The bytes of the script file: the script as given plus what each shell needs to read it as UTF-8."""
    if shell in ("powershell", "pwsh"):
        return b"\xef\xbb\xbf" + (_PS_ENCODING + "\r\n" + script).encode("utf-8")
    newline = "\r\n" if shell == "cmd" else "\n"
    body = newline.join(re.split(r"\r\n|\r|\n", script))
    if shell == "cmd":
        body = "@chcp 65001>nul" + newline + body
    return body.encode("utf-8")


def script_argv(shell: str, path: str, arguments: list[str]) -> list[str] | str:
    """How to run the script file ``path`` with ``arguments``; for ``cmd`` the line handed to ``cmd.exe /c``."""
    if shell in ("powershell", "pwsh"):
        exe = "powershell.exe" if shell == "powershell" else "pwsh.exe"
        return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", path, *arguments]
    if shell == "cmd":
        return " ".join(f'"{part}"' for part in (path, *arguments))
    return ["/bin/sh", path, *arguments]


class CommandExecutor:
    def __init__(self, policy: PathPolicy, shell: str, jobs: JobRegistry) -> None:
        self.policy = policy
        self.shell = shell or default_shell()
        self.jobs = jobs

    def _workdir(self, cwd: str | None) -> str:
        if not cwd and not self.policy.roots:
            raise no_allowed_roots()
        workdir = cwd or self.policy.roots[0]
        if not os.path.isdir(workdir):
            raise FileManagerError("NOT_A_DIRECTORY", f"cwd {workdir!r} is not an existing directory")
        return workdir

    async def run_command(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        wait_sec: float = 30,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        workdir = self._workdir(cwd)
        merged = {**os.environ, **(env or {})}
        try:
            job = await self.jobs.start(
                shell_argv(self.shell, command), cwd=workdir, env=merged, kind=KIND, caller=caller_name(ctx)
            )
        except FileNotFoundError:
            raise FileManagerError("NOT_FOUND", f"shell {self.shell!r} is not installed") from None
        return await self.jobs.wait_result(job, wait_sec)

    async def run_script(
        self,
        script: str,
        arguments: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        wait_sec: float = 30,
        timeout_sec: float | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        arguments = list(arguments or [])
        self._check_script(script, arguments, timeout_sec)
        workdir = self._workdir(cwd)
        merged = {**os.environ, **(env or {})}
        job_id = str(uuid.uuid4())
        directory = self.jobs.new_script_dir(job_id)
        try:
            path = directory / ("script" + _SCRIPT_SUFFIX[self.shell])
            path.write_bytes(script_text(self.shell, script))
            job = await self.jobs.start(
                script_argv(self.shell, str(path), arguments),
                cwd=workdir,
                env=merged,
                kind=KIND,
                job_id=job_id,
                caller=caller_name(ctx),
                timeout_sec=timeout_sec,
                script_dir=directory,
            )
        except FileNotFoundError:
            shutil.rmtree(directory, ignore_errors=True)
            raise FileManagerError("NOT_FOUND", f"shell {self.shell!r} is not installed") from None
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return await self.jobs.wait_result(job, wait_sec)

    def _check_script(self, script: str, arguments: list[str], timeout_sec: float | None) -> None:
        if len(script.encode("utf-8", errors="surrogatepass")) > MAX_SCRIPT_BYTES:
            raise FileManagerError("INVALID_ARGUMENT", f"script is larger than {MAX_SCRIPT_BYTES} bytes of UTF-8")
        _encodable(script, "script")
        for i, argument in enumerate(arguments):
            _encodable(argument, f"arguments[{i}]")
            if self.shell == "cmd" and _CMD_UNSAFE.search(argument):
                raise FileManagerError(
                    "INVALID_ARGUMENT",
                    f'arguments[{i}] contains a character cmd.exe cannot pass safely (one of & | < > ^ % " CR LF); '
                    "use PowerShell or put the value in env",
                )
        if timeout_sec is not None and (
            isinstance(timeout_sec, bool) or not math.isfinite(timeout_sec) or timeout_sec <= 0
        ):
            raise FileManagerError("INVALID_ARGUMENT", "timeout_sec must be a finite number greater than 0")
