"""Remote transfer: push to / pull from allowlisted Remote Hosts via the system scp (spec section 9)."""

from __future__ import annotations

import os
from typing import Any

from .errors import FileManagerError
from .jobs import JobRegistry
from .paths import PathPolicy

KIND = "transfer"


class RemoteTransfer:
    def __init__(
        self,
        policy: PathPolicy,
        allowed_hosts: list[str],
        jobs: JobRegistry,
        scp_command: list[str] | None = None,
    ) -> None:
        self.policy = policy
        self.allowed_hosts = list(allowed_hosts)
        self.jobs = jobs
        self.scp_command = scp_command or ["scp"]

    def _remote(self, host: str, remote_path: str) -> str:
        if host not in self.allowed_hosts:
            raise FileManagerError("HOST_NOT_ALLOWED", f"{host!r} is not in remote.allowed_hosts")
        if not remote_path or remote_path.startswith("-"):
            raise FileManagerError("INVALID_ARGUMENT", "remote_path must be non-empty and not start with '-'")
        return f"{host}:{remote_path}"

    async def _run(self, src: str, dst: str, recursive: bool, wait_sec: float) -> dict[str, Any]:
        argv = [*self.scp_command, "-B", "-o", "BatchMode=yes", *(["-r"] if recursive else []), "--", src, dst]
        try:
            job = await self.jobs.start(argv, kind=KIND)
        except FileNotFoundError:
            raise FileManagerError("NOT_FOUND", "scp is not installed or not on PATH") from None
        return await self.jobs.wait_result(job, wait_sec)

    async def push(
        self, local_path: str, host: str, remote_path: str, recursive: bool = False, wait_sec: float = 30
    ) -> dict[str, Any]:
        remote = self._remote(host, remote_path)
        local = self.policy.existing(local_path)
        if os.path.isdir(local) and not recursive:
            raise FileManagerError("INVALID_ARGUMENT", f"{local_path!r} is a directory; pass recursive=true")
        return await self._run(local, remote, recursive, wait_sec)

    async def pull(
        self, host: str, remote_path: str, local_path: str, recursive: bool = False, wait_sec: float = 30
    ) -> dict[str, Any]:
        remote = self._remote(host, remote_path)
        local = self.policy.resolve(local_path)
        return await self._run(remote, local, recursive, wait_sec)
