"""Jobs: long-running processes tracked by id after their tool call's wait (spec section 7)."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
import uuid
from collections.abc import Callable
from typing import Any

from .errors import FileManagerError

MAX_WAIT_SEC = 50
RESULT_CAP_BYTES = 32 * 1024
BUFFER_LIMIT_BYTES = 1024 * 1024
JOB_TTL_SEC = 3600

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _PROCESS_TERMINATE_AND_SET_QUOTA = 0x0001 | 0x0100


def _windows_job_object(pid: int) -> int | None:
    """Put process ``pid`` in a new Job Object, so its descendants can be killed even after it exits."""
    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    process = _kernel32.OpenProcess(_PROCESS_TERMINATE_AND_SET_QUOTA, False, pid)
    assigned = bool(process) and bool(_kernel32.AssignProcessToJobObject(job, process))
    if process:
        _kernel32.CloseHandle(process)
    if not assigned:
        _kernel32.CloseHandle(job)
        return None
    return job


def find_executable(name: str) -> str | None:
    """Find ``name`` in the absolute PATH entries; the current directory is never searched."""
    exts = [""]
    if os.name == "nt":
        pathext = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
        if os.path.splitext(name)[1].upper() not in (e.upper() for e in pathext):
            exts = pathext
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not os.path.isabs(directory):
            continue
        for ext in exts:
            candidate = os.path.join(directory, name + ext)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def clamp_wait(wait_sec: float) -> float:
    return max(0, min(wait_sec, MAX_WAIT_SEC))


def cap_text(data: bytes, dropped: int, cap: int = RESULT_CAP_BYTES) -> tuple[str, bool]:
    """Decode ``data``, keeping its head and tail around a truncation marker when over ``cap``.

    ``dropped`` counts bytes already discarded from the middle of ``data`` by an OutputBuffer.
    """
    if len(data) <= cap and not dropped:
        return data.decode("utf-8", errors="replace"), False
    if len(data) > cap:
        half = cap // 2
        head, tail = data[:half], data[len(data) - half :]
        removed = len(data) - 2 * half + dropped
    else:
        mid = len(data) // 2
        head, tail = data[:mid], data[mid:]
        removed = dropped
    marker = f"\n...[{removed} bytes truncated]...\n"
    return head.decode("utf-8", errors="replace") + marker + tail.decode("utf-8", errors="replace"), True


class OutputBuffer:
    """Keeps the first and last ``limit // 2`` bytes of a stream and counts what was dropped."""

    def __init__(self, limit: int = BUFFER_LIMIT_BYTES) -> None:
        self.half = limit // 2
        self.head = bytearray()
        self.tail = bytearray()
        self.dropped = 0

    def append(self, chunk: bytes) -> None:
        room = self.half - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        self.tail += chunk
        excess = len(self.tail) - self.half
        if excess > 0:
            del self.tail[:excess]
            self.dropped += excess

    def snapshot(self) -> tuple[bytes, int]:
        return bytes(self.head + self.tail), self.dropped


class Job:
    def __init__(self, process: asyncio.subprocess.Process, started: float, kind: str = "") -> None:
        self.id = str(uuid.uuid4())
        self.kind = kind
        self.process = process
        self.started = started
        self.finished_at: float | None = None
        self.status = "running"
        self.stdout = OutputBuffer()
        self.stderr = OutputBuffer()
        self.done = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.windows_job = _windows_job_object(process.pid) if os.name == "nt" else None

    def close_windows_job(self) -> None:
        if self.windows_job is not None:
            _kernel32.CloseHandle(self.windows_job)
            self.windows_job = None


async def _pump(stream: asyncio.StreamReader, buf: OutputBuffer) -> None:
    while chunk := await stream.read(65536):
        buf.append(chunk)


async def _kill_tree(job: Job) -> None:
    """Kill the job's process and every descendant, including those that outlived it."""
    if os.name != "nt":
        try:
            os.killpg(job.process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    elif job.windows_job is not None:
        _kernel32.TerminateJobObject(job.windows_job, 1)
    elif job.process.returncode is None and (taskkill := find_executable("taskkill.exe")):
        await asyncio.to_thread(
            subprocess.run,
            [taskkill, "/T", "/F", "/PID", str(job.process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


class JobRegistry:
    def __init__(self, clock: Callable[[], float] = time.monotonic, ttl_sec: float = JOB_TTL_SEC) -> None:
        self.clock = clock
        self.ttl_sec = ttl_sec
        self._jobs: dict[str, Job] = {}
        self._closed_kinds: set[str] = set()

    async def start(
        self, argv: list[str] | str, cwd: str | None = None, env: dict[str, str] | None = None, kind: str = ""
    ) -> Job:
        """Start ``argv`` as a job. A ``str`` is a command for ``cmd.exe /c``, passed with its quotes intact.

        Raises FileNotFoundError when the executable is not an absolute path or found by ``find_executable``.
        """
        self.prune()
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": cwd,
            "env": env,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        if isinstance(argv, str):
            comspec = find_executable("cmd.exe") if os.name == "nt" else None
            if comspec is None:
                raise FileNotFoundError("cmd.exe")
            process = await asyncio.create_subprocess_shell(argv, executable=comspec, **kwargs)
        else:
            exe = argv[0] if os.path.isabs(argv[0]) else find_executable(argv[0])
            if exe is None:
                raise FileNotFoundError(argv[0])
            process = await asyncio.create_subprocess_exec(exe, *argv[1:], **kwargs)
        job = Job(process, self.clock(), kind)
        job.task = asyncio.create_task(self._supervise(job))
        self._jobs[job.id] = job
        if kind in self._closed_kinds:
            await self.cancel(job.id)
        return job

    async def _supervise(self, job: Job) -> None:
        await asyncio.gather(_pump(job.process.stdout, job.stdout), _pump(job.process.stderr, job.stderr))
        await job.process.wait()
        if job.status == "running":
            job.status = "exited"
        job.close_windows_job()
        job.finished_at = self.clock()
        job.done.set()

    def get(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise FileManagerError("JOB_NOT_FOUND", f"no job with id {job_id!r}")
        return job

    async def wait_result(self, job: Job, wait_sec: float) -> dict[str, Any]:
        try:
            await asyncio.wait_for(job.done.wait(), clamp_wait(wait_sec))
        except TimeoutError:
            pass
        return self.result(job)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job.status == "running":
            job.status = "cancelled"
            await _kill_tree(job)
            try:
                await asyncio.wait_for(job.done.wait(), 10)
            except TimeoutError:
                pass
        return self.result(job)

    async def cancel_kind(self, kind: str) -> None:
        """Cancel running jobs of ``kind`` and cancel any it starts later, until ``open_kind``."""
        self._closed_kinds.add(kind)
        running = [job.id for job in self._jobs.values() if job.kind == kind and job.status == "running"]
        await asyncio.gather(*(self.cancel(job_id) for job_id in running))

    def open_kind(self, kind: str) -> None:
        self._closed_kinds.discard(kind)

    def result(self, job: Job) -> dict[str, Any]:
        stdout, out_cut = cap_text(*job.stdout.snapshot())
        stderr, err_cut = cap_text(*job.stderr.snapshot())
        end = job.finished_at if job.finished_at is not None else self.clock()
        result: dict[str, Any] = {"status": job.status, "job_id": job.id}
        if job.status == "exited":
            result["exit_code"] = job.process.returncode
        result.update(
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((end - job.started) * 1000),
            truncated=out_cut or err_cut,
        )
        return result

    def prune(self) -> None:
        now = self.clock()
        for job_id, job in list(self._jobs.items()):
            if job.finished_at is not None and now - job.finished_at > self.ttl_sec:
                del self._jobs[job_id]
