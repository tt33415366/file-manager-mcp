"""Command Executor: runs shell commands as Jobs (spec section 8)."""

from __future__ import annotations

import os
from typing import Any

from .errors import FileManagerError
from .jobs import JobRegistry
from .paths import PathPolicy, no_allowed_roots

KIND = "command"
_PS_PREFIX = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "


def default_shell() -> str:
    return "powershell" if os.name == "nt" else "sh"


def shell_argv(shell: str, command: str) -> list[str] | str:
    """The argv for ``shell``; for ``cmd`` the bare command, which JobRegistry.start hands to ``cmd.exe /c``."""
    if shell in ("powershell", "pwsh"):
        exe = "powershell.exe" if shell == "powershell" else "pwsh.exe"
        return [exe, "-NoProfile", "-NonInteractive", "-Command", _PS_PREFIX + command]
    if shell == "cmd":
        return command
    return ["/bin/sh", "-c", command]


class CommandExecutor:
    def __init__(self, policy: PathPolicy, shell: str, jobs: JobRegistry) -> None:
        self.policy = policy
        self.shell = shell or default_shell()
        self.jobs = jobs

    async def run_command(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        wait_sec: float = 30,
    ) -> dict[str, Any]:
        if not cwd and not self.policy.roots:
            raise no_allowed_roots()
        workdir = cwd or self.policy.roots[0]
        if not os.path.isdir(workdir):
            raise FileManagerError("NOT_A_DIRECTORY", f"cwd {workdir!r} is not an existing directory")
        merged = {**os.environ, **(env or {})}
        try:
            job = await self.jobs.start(shell_argv(self.shell, command), cwd=workdir, env=merged, kind=KIND)
        except FileNotFoundError:
            raise FileManagerError("NOT_FOUND", f"shell {self.shell!r} is not installed") from None
        return await self.jobs.wait_result(job, wait_sec)
