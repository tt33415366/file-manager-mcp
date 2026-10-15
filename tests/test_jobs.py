import os
import shutil
import subprocess
import sys
import time

import anyio
import pytest

from file_manager_mcp.errors import FileManagerError
from file_manager_mcp.jobs import JobRegistry, OutputBuffer, cap_text, clamp_wait, find_executable

pytestmark = pytest.mark.anyio

PY = sys.executable


def py(code: str) -> list[str]:
    return [PY, "-c", code]


async def test_quick_command_exits_with_output():
    reg = JobRegistry()
    job = await reg.start(py("import sys; print('out'); print('err', file=sys.stderr)"))
    result = await reg.wait_result(job, 10)

    assert result["status"] == "exited"
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "out"
    assert result["stderr"].strip() == "err"
    assert result["truncated"] is False
    assert result["duration_ms"] >= 0


async def test_nonzero_exit_code_is_reported():
    reg = JobRegistry()
    result = await reg.wait_result(await reg.start(py("raise SystemExit(3)")), 10)
    assert result["status"] == "exited" and result["exit_code"] == 3


async def test_slow_command_becomes_running_job_then_finishes():
    reg = JobRegistry()
    job = await reg.start(py("import time; print('start', flush=True); time.sleep(1.5); print('done')"))

    first = await reg.wait_result(job, 0.5)
    assert first["status"] == "running"
    assert first["job_id"] == job.id

    final = await reg.wait_result(reg.get(job.id), 10)
    assert final["status"] == "exited"
    assert "done" in final["stdout"]


async def test_cancel_kills_job():
    reg = JobRegistry()
    job = await reg.start(py("import time; time.sleep(30)"))
    result = await reg.cancel(job.id)
    assert result["status"] == "cancelled"
    assert job.process.returncode is not None


async def test_cancel_kind_cancels_only_running_jobs_of_that_kind():
    reg = JobRegistry()
    done = await reg.start(py("pass"), kind="command")
    await reg.wait_result(done, 10)
    command = await reg.start(py("import time; time.sleep(30)"), kind="command")
    transfer = await reg.start(py("import time; time.sleep(30)"), kind="transfer")

    await reg.cancel_kind("command")

    assert command.status == "cancelled" and command.process.returncode is not None
    assert done.status == "exited"
    assert transfer.status == "running"
    await reg.cancel(transfer.id)


async def test_cancelled_kind_stays_closed_until_reopened():
    reg = JobRegistry()
    await reg.cancel_kind("command")

    closed = await reg.start(py("import time; time.sleep(30)"), kind="command")
    assert closed.status == "cancelled" and closed.process.returncode is not None

    reg.open_kind("command")
    reopened = await reg.start(py("pass"), kind="command")
    assert (await reg.wait_result(reopened, 10))["status"] == "exited"


@pytest.mark.skipif(sys.platform != "win32", reason="tasklist check")
async def test_cancel_kills_process_tree_on_windows(tmp_path):
    pid_file = tmp_path / "child.pid"
    code = (
        "import subprocess, sys, time; "
        f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"open(r'{pid_file}', 'w').write(str(c.pid)); time.sleep(60)"
    )
    reg = JobRegistry()
    job = await reg.start(py(code))
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text():
            break
        await anyio.sleep(0.1)
    child = pid_file.read_text()

    await reg.cancel(job.id)

    time.sleep(0.5)
    listing = subprocess.run(["tasklist", "/FI", f"PID eq {child}"], capture_output=True, text=True).stdout
    assert child not in listing


async def test_cancel_kills_descendants_that_outlive_the_shell():
    code = (
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdout=sys.stdout, stderr=sys.stderr)"
    )
    reg = JobRegistry()
    job = await reg.start(py(code))
    for _ in range(100):
        if job.process.returncode is not None:
            break
        await anyio.sleep(0.1)
    assert job.process.returncode == 0
    assert (await reg.wait_result(job, 0.5))["status"] == "running"

    started = time.monotonic()
    result = await reg.cancel(job.id)

    assert result["status"] == "cancelled"
    assert job.done.is_set()
    assert time.monotonic() - started < 5


async def test_executable_in_the_current_directory_is_not_run(tmp_path, monkeypatch):
    planted = tmp_path / "cwd"
    planted.mkdir()
    name = "fmcp-planted.exe" if os.name == "nt" else "fmcp-planted"
    shutil.copy(PY, planted / name)
    monkeypatch.chdir(planted)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", str(tmp_path / "empty")]))

    with pytest.raises(FileNotFoundError):
        await JobRegistry().start(["fmcp-planted", "-c", "pass"])


def test_find_executable_searches_only_absolute_path_entries(tmp_path, monkeypatch):
    planted, bindir = tmp_path / "cwd", tmp_path / "bin"
    planted.mkdir()
    bindir.mkdir()
    name = "fmcp-tool.exe" if os.name == "nt" else "fmcp-tool"
    for d in (planted, bindir):
        (d / name).write_bytes(b"")
        (d / name).chmod(0o755)
    monkeypatch.chdir(planted)

    monkeypatch.setenv("PATH", os.pathsep.join([".", ""]))
    assert find_executable("fmcp-tool") is None
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(bindir)]))
    assert os.path.normcase(find_executable("fmcp-tool")) == os.path.normcase(str(bindir / name))


async def test_unknown_job_is_job_not_found():
    reg = JobRegistry()
    with pytest.raises(FileManagerError) as exc:
        reg.get("nope")
    assert exc.value.code == "JOB_NOT_FOUND"
    with pytest.raises(FileManagerError):
        await reg.cancel("nope")


async def test_large_output_is_capped_with_marker():
    reg = JobRegistry()
    result = await reg.wait_result(await reg.start(py("print('a' * 100000)")), 10)
    assert result["truncated"] is True
    assert "bytes truncated]" in result["stdout"]
    assert len(result["stdout"]) < 33 * 1024


async def test_finished_jobs_are_pruned_after_ttl():
    now = [1000.0]
    reg = JobRegistry(clock=lambda: now[0], ttl_sec=3600)
    job = await reg.start(py("pass"))
    await reg.wait_result(job, 10)

    now[0] += 3601
    reg.prune()

    with pytest.raises(FileManagerError):
        reg.get(job.id)


def test_cap_text_keeps_head_and_tail():
    data = b"H" * 100 + b"M" * 1000 + b"T" * 100
    text, truncated = cap_text(data, 0, cap=200)
    assert truncated is True
    assert text.startswith("H" * 100) and text.endswith("T" * 100)
    assert "...[1000 bytes truncated]..." in text


def test_cap_text_small_input_untouched():
    assert cap_text(b"hello", 0, cap=200) == ("hello", False)


def test_output_buffer_keeps_head_and_tail_within_limit():
    buf = OutputBuffer(limit=20)
    for _ in range(10):
        buf.append(b"0123456789")
    data, dropped = buf.snapshot()
    assert data == b"0123456789" + b"0123456789"
    assert dropped == 80


def test_clamp_wait():
    assert clamp_wait(-5) == 0
    assert clamp_wait(10) == 10
    assert clamp_wait(1000) == 50
