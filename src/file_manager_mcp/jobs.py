"""Jobs: long-running processes tracked by id, with a Job Log on disk (spec section 7)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import FileManagerError
from .joblog import OutputBuffer, StreamLog, cap_text  # noqa: F401  (re-exported)

MAX_WAIT_SEC = 50
JOB_TTL_SEC = 3600
DEFAULT_MAX_LOG_BYTES = 2 * 1024**3
TERMINATION_WAIT_SEC = 10
STREAMS = ("stdout", "stderr")
_MAX_TIMER_SEC = 10**9

logger = logging.getLogger(__name__)

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
    _kernel32.QueryInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
    )
    _PROCESS_TERMINATE_AND_SET_QUOTA = 0x0001 | 0x0100
    _JOB_OBJECT_BASIC_ACCOUNTING = 1

    class _BasicAccounting(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]


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


def _active_in_job_object(job: int) -> int | None:
    """How many processes are still in the Job Object, or None when that cannot be told."""
    info = _BasicAccounting()
    if not _kernel32.QueryInformationJobObject(
        job, _JOB_OBJECT_BASIC_ACCOUNTING, ctypes.byref(info), ctypes.sizeof(info), None
    ):
        return None
    return info.ActiveProcesses


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


class Job:
    def __init__(
        self,
        process: asyncio.subprocess.Process | None,
        started: float,
        kind: str = "",
        job_id: str | None = None,
        caller: str = "",
        directory: Path | None = None,
    ) -> None:
        self.id = job_id or str(uuid.uuid4())
        self.kind = kind
        self.caller = caller
        self.directory = directory
        self.process = process
        self.started = started
        self.started_wall = time.time()
        self.finished_at: float | None = None  # on the registry's clock
        self.finished_wall: float | None = None
        self.status = "running"
        self.exit_code: int | None = None
        self.reaped = False  # the process exited and its output pipes closed
        self.termination_complete: bool | None = None
        self.terminating = False
        self.termination_error: str | None = None
        self.interrupted_at: float | None = None
        self.protect_until = 0.0
        self.downloads = 0
        self.log_reclaimed = False
        self.stdout = StreamLog(None)
        self.stderr = StreamLog(None)
        self.done = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.timeout_task: asyncio.Task | None = None
        self.script_dir: Path | None = None
        self.windows_job = _windows_job_object(process.pid) if process is not None and os.name == "nt" else None

    def stream(self, name: str) -> StreamLog:
        return self.stdout if name == "stdout" else self.stderr

    def log_size(self) -> int:
        return self.stdout.retained + self.stderr.retained

    def close_windows_job(self) -> None:
        if self.windows_job is not None:
            _kernel32.CloseHandle(self.windows_job)
            self.windows_job = None

    def meta(self) -> dict[str, Any]:
        """What survives a restart. Stream lengths are only known once the streams are closed."""
        closed = self.stdout.closed and self.stderr.closed
        return {
            "job_id": self.id,
            "kind": self.kind,
            "caller": self.caller,
            "status": self.status,
            "exit_code": self.exit_code,
            "started_at": self.started_wall,
            "finished_at": self.finished_wall,
            "reaped": self.reaped,
            "termination_complete": self.termination_complete,
            "termination_error": self.termination_error,
            "interrupted_at": self.interrupted_at,
            "protect_until": self.protect_until,
            "log_reclaimed": self.log_reclaimed,
            **{
                name: {
                    "produced_end": self.stream(name).produced if closed else None,
                    "truncated": self.stream(name).truncated,
                }
                for name in STREAMS
            },
        }

    @classmethod
    def from_meta(cls, meta: dict[str, Any], directory: Path) -> Job:
        job = cls(None, 0.0, meta.get("kind", ""), meta["job_id"], meta.get("caller", ""), directory)
        job.status = meta["status"]
        job.exit_code = meta.get("exit_code")
        job.started_wall = float(meta["started_at"])
        job.finished_wall = meta.get("finished_at")
        job.reaped = bool(meta.get("reaped"))
        job.termination_complete = meta.get("termination_complete")
        job.termination_error = meta.get("termination_error")
        job.interrupted_at = meta.get("interrupted_at")
        job.protect_until = float(meta.get("protect_until") or 0.0)
        job.log_reclaimed = bool(meta.get("log_reclaimed"))
        for name in STREAMS:
            info = meta.get(name) or {}
            log = StreamLog.recovered(str(directory / f"{name}.log"), info.get("produced_end"), bool(info.get("truncated")))
            log.reclaimed = job.log_reclaimed
            setattr(job, name, log)
        job.done.set()
        return job


async def _pump(stream: asyncio.StreamReader, log: StreamLog) -> None:
    while chunk := await stream.read(65536):
        log.append(chunk)


def _tree_gone(job: Job) -> bool:
    """Whether the managed process tree has ended.

    On Windows that is the job's Job Object holding no process; elsewhere the job's process group having no
    member. A descendant that left the Job Object or the process group is not covered.
    """
    if job.process is None:
        return False
    if os.name == "nt":
        if job.windows_job is None:
            return job.process.returncode is not None
        active = _active_in_job_object(job.windows_job)
        return active == 0
    try:
        os.killpg(job.process.pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


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
    """Jobs by id. Their Job Logs and metadata live under ``state_dir`` and survive a restart.

    With no ``state_dir`` a private temporary directory is used and removed with the registry.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        ttl_sec: float = JOB_TTL_SEC,
        state_dir: str | Path | None = None,
        max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.clock = clock
        self.ttl_sec = ttl_sec
        self.wall = wall
        self.max_log_bytes = max_log_bytes
        self._jobs: dict[str, Job] = {}
        self._closed_kinds: set[str] = set()
        self._spawn_lock = asyncio.Lock()
        self._total = 0
        if state_dir is None:
            state_dir = tempfile.mkdtemp(prefix="fmcp-state-")
            weakref.finalize(self, shutil.rmtree, state_dir, True)
        self.jobs_dir = Path(state_dir) / "jobs"
        self.scripts_dir = Path(state_dir) / "scripts"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.scripts_dir.mkdir(parents=True, exist_ok=True)
        self.retained_scripts: list[Path] = []
        self._recover()

    # persistence

    def _save(self, job: Job) -> None:
        if job.directory is None:
            return
        target = job.directory / "meta.json"
        try:
            temp = job.directory / "meta.json.tmp"
            temp.write_text(json.dumps(job.meta()), encoding="utf-8")
            os.replace(temp, target)
        except OSError:
            logger.warning("could not save the metadata of job %s", job.id, exc_info=True)

    def _recover(self) -> None:
        now, now_wall = self.clock(), self.wall()
        for directory in sorted(self.jobs_dir.iterdir()):
            try:
                job = Job.from_meta(json.loads((directory / "meta.json").read_text(encoding="utf-8")), directory)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            changed = False
            if job.status == "running":
                job.status, job.termination_complete, changed = "interrupted", False, True
            if job.status == "interrupted":
                if job.interrupted_at is None:
                    job.interrupted_at, changed = now_wall, True
                job.finished_wall = job.interrupted_at
                job.termination_complete = False
            job.started = now - max(0.0, now_wall - job.started_wall)
            if job.finished_wall is not None:
                job.finished_at = now - max(0.0, now_wall - job.finished_wall)
            self._jobs[job.id] = job
            self._total += job.log_size()
            if changed:
                self._save(job)
        self._sweep_scripts()

    def _sweep_scripts(self) -> None:
        """Delete script directories whose Job's process is known to be gone; keep the rest for the user."""
        for directory in sorted(self.scripts_dir.iterdir()):
            try:
                owner = json.loads((directory / "owner.json").read_text(encoding="utf-8"))["job_id"]
            except (OSError, ValueError, KeyError, TypeError):
                owner = None
            job = self._jobs.get(owner) if owner else None
            if job is not None and self._process_gone(job):
                shutil.rmtree(directory, ignore_errors=True)
            if directory.exists():
                self.retained_scripts.append(directory)

    @staticmethod
    def _process_gone(job: Job) -> bool:
        if job.status == "exited":
            return job.reaped
        return job.status in ("cancelled", "timed_out") and job.termination_complete is True

    def retained_script_report(self) -> tuple[int, int, list[Path]]:
        """(count, bytes, paths) of script directories kept since start-up."""
        paths = [p for p in self.retained_scripts if p.exists()]
        size = sum(f.stat().st_size for p in paths for f in p.rglob("*") if f.is_file())
        return len(paths), size, paths

    def new_script_dir(self, job_id: str) -> Path:
        """A private directory for one Job's Script, marked with its owner."""
        directory = self.scripts_dir / job_id
        directory.mkdir(mode=0o700)
        (directory / "owner.json").write_text(json.dumps({"job_id": job_id, "created_at": self.wall()}), encoding="utf-8")
        return directory

    # capacity

    def _protected(self, job: Job) -> bool:
        return job.protect_until > self.wall() or job.downloads > 0

    def _protected_total(self) -> int:
        return sum(j.log_size() for j in self._jobs.values() if j.protect_until > self.wall())

    def _reserve(self, job: Job, n: int) -> int:
        """How many of the next ``n`` bytes of ``job``'s output may be stored; reserves them."""
        cap = self.max_log_bytes
        if self._total + n > cap:
            self._reclaim(self._total + n - cap)
        allowed = min(n, max(0, cap - self._total))
        if job.protect_until > self.wall():
            allowed = min(allowed, max(0, cap // 2 - self._protected_total()))
        self._total += allowed
        return allowed

    def _release(self, n: int) -> None:
        self._total = max(0, self._total - n)

    def _reclaim(self, needed: int) -> None:
        """Delete the logs of the oldest finished, unprotected Jobs until ``needed`` bytes are free."""
        candidates = sorted(
            (j for j in self._jobs.values() if j.finished_at is not None and not j.log_reclaimed and not self._protected(j)),
            key=lambda j: j.finished_at,
        )
        for job in candidates:
            if needed <= 0:
                break
            freed = job.stdout.discard() + job.stderr.discard()
            job.log_reclaimed = True
            self._total = max(0, self._total - freed)
            needed -= freed
            self._save(job)

    def protect(self, job: Job, until: float) -> None:
        """Keep ``job``'s log until wall-clock time ``until``; explicit protection may use half the capacity."""
        if job.log_reclaimed:
            raise FileManagerError("LOG_RECLAIMED", "the job's log was reclaimed to free disk space")
        if job.protect_until <= self.wall() and self._protected_total() + job.log_size() > self.max_log_bytes // 2:
            raise FileManagerError(
                "LOG_PROTECTION_LIMIT", "protected logs already use half of jobs.max_log_bytes; try again later"
            )
        job.protect_until = max(job.protect_until, until)
        self._save(job)

    # running jobs

    async def _spawn(self, argv: list[str] | str, kwargs: dict[str, Any]) -> asyncio.subprocess.Process:
        if isinstance(argv, str):
            comspec = find_executable("cmd.exe") if os.name == "nt" else None
            if comspec is None:
                raise FileNotFoundError("cmd.exe")
            # Popen(shell=True) runs "%ComSpec% /c <command>"; the flags ride in ComSpec so that this cmd.exe,
            # which the command's own cmd.exe is a child of, skips AutoRun and delayed expansion too.
            async with self._spawn_lock:
                saved = os.environ.get("ComSpec")
                os.environ["ComSpec"] = f'"{comspec}" /d /v:off'
                try:
                    return await asyncio.create_subprocess_shell(argv, **kwargs)
                finally:
                    if saved is None:
                        os.environ.pop("ComSpec", None)
                    else:
                        os.environ["ComSpec"] = saved
        exe = argv[0] if os.path.isabs(argv[0]) else find_executable(argv[0])
        if exe is None:
            raise FileNotFoundError(argv[0])
        return await asyncio.create_subprocess_exec(exe, *argv[1:], **kwargs)

    async def start(
        self,
        argv: list[str] | str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        kind: str = "",
        *,
        job_id: str | None = None,
        caller: str = "",
        timeout_sec: float | None = None,
        script_dir: Path | None = None,
    ) -> Job:
        """Start ``argv`` as a job. A ``str`` is a command for ``cmd.exe /d /v:off /c``, passed with its quotes intact.

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
        process = await self._spawn(argv, kwargs)
        job_id = job_id or str(uuid.uuid4())
        directory = self.jobs_dir / job_id
        directory.mkdir(parents=True, exist_ok=True)
        job = Job(process, self.clock(), kind, job_id, caller, directory)
        job.started_wall = self.wall()
        job.script_dir = script_dir
        for name in STREAMS:
            setattr(
                job,
                name,
                StreamLog(
                    str(directory / f"{name}.log"),
                    reserve=lambda n, job=job: self._reserve(job, n),
                    release=self._release,
                ),
            )
        job.task = asyncio.create_task(self._supervise(job))
        if timeout_sec is not None:
            job.timeout_task = asyncio.create_task(self._expire(job, timeout_sec))
        self._jobs[job.id] = job
        self._save(job)
        if kind in self._closed_kinds:
            await self.cancel(job.id)
        return job

    async def _supervise(self, job: Job) -> None:
        await asyncio.gather(_pump(job.process.stdout, job.stdout), _pump(job.process.stderr, job.stderr))
        await job.process.wait()
        job.stdout.close()
        job.stderr.close()
        job.exit_code = job.process.returncode
        job.reaped = True
        if job.status == "running":
            job.status = "exited"
            job.close_windows_job()
            if job.timeout_task is not None:
                job.timeout_task.cancel()
        job.finished_at = self.clock()
        job.finished_wall = self.wall()
        if job.status == "exited":
            self._remove_script(job)
        self._save(job)
        job.done.set()

    def _remove_script(self, job: Job) -> None:
        if job.script_dir is not None:
            shutil.rmtree(job.script_dir, ignore_errors=True)
            job.script_dir = None

    async def _expire(self, job: Job, timeout_sec: float) -> None:
        await asyncio.sleep(min(timeout_sec, _MAX_TIMER_SEC))
        if job.status == "running":
            job.status = "timed_out"
            self._save(job)
            await self._terminate(job)

    async def _terminate(self, job: Job) -> None:
        """Kill ``job``'s process tree and record whether it was confirmed gone within the wait."""
        job.termination_complete, job.termination_error = False, None
        job.terminating = True
        try:
            await self._terminate_tree(job)
        finally:
            job.terminating = False

    async def _terminate_tree(self, job: Job) -> None:
        try:
            await _kill_tree(job)
        except Exception as exc:  # noqa: BLE001  (reported to the caller instead)
            job.termination_error = f"{type(exc).__name__}: {exc}"
        deadline = self.clock() + TERMINATION_WAIT_SEC
        while True:
            if job.done.is_set() and _tree_gone(job):
                job.termination_complete, job.termination_error = True, None
                break
            if self.clock() >= deadline:
                job.termination_error = job.termination_error or (
                    f"the process tree was still running {TERMINATION_WAIT_SEC} seconds after being killed"
                )
                break
            await asyncio.sleep(0.05)
        if job.termination_complete:
            job.close_windows_job()
            self._remove_script(job)
        self._save(job)

    def get(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise FileManagerError("JOB_NOT_FOUND", f"no job with id {job_id!r}")
        return job

    async def wait_result(
        self, job: Job, wait_sec: float, stdout_offset: int | None = None, stderr_offset: int | None = None
    ) -> dict[str, Any]:
        try:
            await asyncio.wait_for(job.done.wait(), clamp_wait(wait_sec))
        except TimeoutError:
            pass
        # A kill in flight settles within TERMINATION_WAIT_SEC; report its outcome, not "not yet".
        while job.terminating and job.done.is_set():
            await asyncio.sleep(0.05)
        return self.result(job, stdout_offset, stderr_offset)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        """Cancel a running job; a cancelled or timed-out job whose termination is unconfirmed is killed again.

        A job recovered after a restart is never killed: its recorded process cannot be told from a new one.
        """
        job = self.get(job_id)
        if job.process is None:
            return self.result(job)
        if job.status == "running":
            job.status = "cancelled"
            if job.timeout_task is not None:
                job.timeout_task.cancel()
            self._save(job)
            await self._terminate(job)
        elif job.status in ("cancelled", "timed_out") and job.termination_complete is False:
            await self._terminate(job)
        return self.result(job)

    async def cancel_kind(self, kind: str) -> None:
        """Cancel running jobs of ``kind`` and cancel any it starts later, until ``open_kind``."""
        self._closed_kinds.add(kind)
        running = [job.id for job in self._jobs.values() if job.kind == kind and job.status == "running"]
        await asyncio.gather(*(self.cancel(job_id) for job_id in running))

    def open_kind(self, kind: str) -> None:
        self._closed_kinds.discard(kind)

    def result(self, job: Job, stdout_offset: int | None = None, stderr_offset: int | None = None) -> dict[str, Any]:
        """The shared result shape; a stream read by cursor gives its next chunk instead of the snapshot."""
        end = job.finished_at if job.finished_at is not None else self.clock()
        result: dict[str, Any] = {"status": job.status, "job_id": job.id}
        if job.status == "exited":
            result["exit_code"] = job.exit_code
        if job.status in ("cancelled", "timed_out", "interrupted"):
            result["termination_complete"] = bool(job.termination_complete)
            if job.termination_error:
                result["termination_error"] = job.termination_error
        result["duration_ms"] = int(max(0.0, end - job.started) * 1000)
        if job.log_reclaimed:
            if stdout_offset is not None or stderr_offset is not None:
                raise FileManagerError("LOG_RECLAIMED", "the job's log was reclaimed to free disk space")
            result["log_reclaimed"] = True
            return result
        truncated = lost_any = False
        for name, offset in (("stdout", stdout_offset), ("stderr", stderr_offset)):
            log = job.stream(name)
            result[f"{name}_retained_end"] = log.retained
            result[f"{name}_produced_end"] = log.produced
            if offset is None:
                result[name], cut = cap_text(*log.snapshot())
                result[f"{name}_next"] = log.bound
                truncated = truncated or cut
            else:
                read = log.read(offset)
                result[name] = read.text
                result[f"{name}_next"] = read.next
                result[f"{name}_lost"] = read.lost
                result[f"{name}_more"] = read.more
                lost_any = lost_any or read.lost > 0
        result["truncated"] = truncated or lost_any
        result["log_truncated"] = job.stdout.truncated or job.stderr.truncated
        return result

    def prune(self) -> None:
        now, wall = self.clock(), self.wall()
        for job_id, job in list(self._jobs.items()):
            unconfirmed = (
                job.process is not None
                and job.status in ("cancelled", "timed_out")
                and job.termination_complete is False
            )  # still ours to kill again
            if (
                job.finished_at is not None
                and now - job.finished_at > self.ttl_sec
                and job.protect_until <= wall
                and job.downloads == 0
                and not unconfirmed
            ):
                self._total = max(0, self._total - job.log_size())
                del self._jobs[job_id]
                if job.directory is not None:
                    shutil.rmtree(job.directory, ignore_errors=True)
