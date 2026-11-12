import json
import sys
import time

import pytest

from file_manager_mcp import jobs, joblog
from file_manager_mcp.errors import FileManagerError
from file_manager_mcp.jobs import JobRegistry

pytestmark = pytest.mark.anyio

PY = sys.executable
SLEEP = [PY, "-c", "import time; time.sleep(60)"]


def py(code: str) -> list[str]:
    return [PY, "-c", code]


def emit(n: int, stream: str = "stdout") -> list[str]:
    return py(f"import sys; sys.{stream}.buffer.write(b'x' * {n}); sys.{stream}.flush()")


async def finished(reg: JobRegistry, argv, **kwargs):
    job = await reg.start(argv, **kwargs)
    await reg.wait_result(job, 20)
    return job


# Output Cursor


async def test_a_cursor_returns_only_new_output(tmp_path):
    reg = JobRegistry(state_dir=tmp_path)
    job = await finished(reg, py("import sys; sys.stdout.buffer.write(b'one\\ntwo\\n')"))

    snapshot = reg.result(job)
    assert snapshot["stdout"] == "one\ntwo\n" and snapshot["stdout_next"] == 8
    assert snapshot["stdout_produced_end"] == snapshot["stdout_retained_end"] == 8
    assert "stdout_lost" not in snapshot

    tail = reg.result(job, stdout_offset=4)
    assert (tail["stdout"], tail["stdout_next"], tail["stdout_more"], tail["stdout_lost"]) == ("two\n", 8, False, 0)
    again = reg.result(job, stdout_offset=tail["stdout_next"])
    assert (again["stdout"], again["stdout_next"]) == ("", 8)
    assert again["stderr"] == "" and again["stderr_next"] == 0  # the other stream still gets its snapshot


async def test_a_cursor_pages_through_output_larger_than_the_cap(tmp_path):
    reg = JobRegistry(state_dir=tmp_path)
    job = await finished(reg, emit(100_000))
    seen, offset = 0, 0
    while True:
        r = reg.result(job, stdout_offset=offset)
        seen += len(r["stdout"])
        offset = r["stdout_next"]
        if not r["stdout_more"]:
            break
    assert seen == 100_000 and offset == 100_000


async def test_a_cursor_past_the_end_is_an_invalid_argument(tmp_path):
    reg = JobRegistry(state_dir=tmp_path)
    job = await finished(reg, emit(3))
    with pytest.raises(FileManagerError) as exc:
        reg.result(job, stdout_offset=4)
    assert exc.value.code == "INVALID_ARGUMENT"


async def test_output_past_the_stream_limit_is_drained_counted_and_not_stored(tmp_path, monkeypatch):
    monkeypatch.setattr(joblog, "STREAM_LOG_LIMIT", 1000)
    reg = JobRegistry(state_dir=tmp_path)
    job = await finished(reg, emit(5000))
    result = reg.result(job)
    assert result["status"] == "exited" and result["exit_code"] == 0
    assert (result["stdout_produced_end"], result["stdout_retained_end"]) == (5000, 1000)
    assert result["log_truncated"] is True
    assert (job.directory / "stdout.log").stat().st_size == 1000


# persistence and recovery


async def test_a_finished_job_is_recovered_after_a_restart(tmp_path):
    first = JobRegistry(state_dir=tmp_path)
    job = await finished(first, py("import sys; sys.stdout.buffer.write(b'hello'); raise SystemExit(3)"), caller="laptop")

    second = JobRegistry(state_dir=tmp_path)
    recovered = second.get(job.id)
    result = second.result(recovered)

    assert (result["status"], result["exit_code"]) == ("exited", 3)
    assert result["stdout"] == "hello" and result["stdout_produced_end"] == 5
    assert recovered.caller == "laptop"
    assert second.result(recovered, stdout_offset=2)["stdout"] == "llo"
    assert json.loads((job.directory / "meta.json").read_text())["caller"] == "laptop"


async def test_a_job_running_at_a_restart_becomes_interrupted_and_is_not_killed(tmp_path):
    first = JobRegistry(state_dir=tmp_path)
    running = await first.start(py("import sys,time; sys.stdout.buffer.write(b'early'); sys.stdout.flush(); time.sleep(60)"))
    try:
        await jobs_wait_for_output(first, running)
        second = JobRegistry(state_dir=tmp_path)
        recovered = second.get(running.id)
        result = second.result(recovered)

        assert result["status"] == "interrupted" and "exit_code" not in result
        assert result["termination_complete"] is False
        assert result["stdout"] == "early" and result["stdout_produced_end"] is None
        assert result["stdout_retained_end"] == 5

        cancelled = await second.cancel(running.id)
        assert cancelled["status"] == "interrupted" and cancelled["termination_complete"] is False
        assert running.process.returncode is None  # nothing was killed
    finally:
        await first.cancel(running.id)


async def jobs_wait_for_output(reg, job):
    import asyncio

    for _ in range(100):
        if job.stdout.produced:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the job produced no output")


async def test_interrupted_ttl_counts_from_the_first_interruption(tmp_path):
    now, wall = [0.0], [1000.0]
    first = JobRegistry(state_dir=tmp_path, clock=lambda: now[0], wall=lambda: wall[0], ttl_sec=3600)
    running = await first.start(SLEEP)
    try:
        wall[0] = 2000.0
        second = JobRegistry(state_dir=tmp_path, clock=lambda: now[0], wall=lambda: wall[0], ttl_sec=3600)
        assert second.get(running.id).interrupted_at == 2000.0

        wall[0] = 5000.0  # another restart much later does not move the start of the TTL
        third = JobRegistry(state_dir=tmp_path, clock=lambda: now[0], wall=lambda: wall[0], ttl_sec=3600)
        assert third.get(running.id).interrupted_at == 2000.0

        wall[0] = 2000.0 + 3601
        now[0] = 3601.0
        fourth = JobRegistry(state_dir=tmp_path, clock=lambda: now[0], wall=lambda: wall[0], ttl_sec=3600)
        fourth.prune()
        with pytest.raises(FileManagerError) as exc:
            fourth.get(running.id)
        assert exc.value.code == "JOB_NOT_FOUND"
    finally:
        await first.cancel(running.id)


async def test_prune_removes_the_job_directory(tmp_path):
    now = [0.0]
    reg = JobRegistry(state_dir=tmp_path, clock=lambda: now[0], ttl_sec=10)
    job = await finished(reg, py("pass"))
    assert job.directory.exists()
    now[0] = 11
    reg.prune()
    assert not job.directory.exists()


# timeout and termination


async def test_timeout_kills_the_tree_and_reports_timed_out(tmp_path):
    reg = JobRegistry(state_dir=tmp_path)
    job = await reg.start(SLEEP, timeout_sec=0.3)
    result = await reg.wait_result(job, 20)
    assert result["status"] == "timed_out" and "exit_code" not in result
    assert result["termination_complete"] is True and "termination_error" not in result
    assert job.process.returncode is not None


async def test_a_job_that_finishes_in_time_is_not_timed_out(tmp_path):
    reg = JobRegistry(state_dir=tmp_path)
    job = await reg.start(py("pass"), timeout_sec=30)
    result = await reg.wait_result(job, 20)
    assert (result["status"], result["exit_code"]) == ("exited", 0)
    assert "termination_complete" not in result


async def test_unconfirmed_termination_is_reported_kept_and_can_be_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "TERMINATION_WAIT_SEC", 0.3)
    monkeypatch.setattr(jobs, "_tree_gone", lambda job: False)
    reg = JobRegistry(state_dir=tmp_path)
    job = await reg.start(SLEEP)

    first = await reg.cancel(job.id)
    assert first["status"] == "cancelled" and first["termination_complete"] is False
    assert "still running" in first["termination_error"]
    assert (job.directory / "meta.json").exists()

    monkeypatch.undo()
    second = await reg.cancel(job.id)
    assert second["status"] == "cancelled" and second["termination_complete"] is True


async def test_cancelled_and_timed_out_jobs_are_not_pruned_while_unconfirmed(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "TERMINATION_WAIT_SEC", 0.2)
    monkeypatch.setattr(jobs, "_tree_gone", lambda job: False)
    skew = [0.0]  # the termination wait needs a clock that really advances
    reg = JobRegistry(state_dir=tmp_path, clock=lambda: time.monotonic() + skew[0], ttl_sec=10)
    job = await reg.start(SLEEP)
    await reg.cancel(job.id)
    skew[0] = 1000
    reg.prune()
    assert reg.get(job.id) is job
    monkeypatch.undo()
    await reg.cancel(job.id)


# capacity, reclaiming, protection


async def test_the_oldest_finished_log_is_reclaimed_when_space_runs_out(tmp_path):
    reg = JobRegistry(state_dir=tmp_path, max_log_bytes=100_000)
    old = await finished(reg, emit(60_000))
    await finished(reg, emit(30_000))
    new = await finished(reg, emit(30_000))  # 120 000 > 100 000: the oldest goes

    assert old.log_reclaimed and not new.log_reclaimed
    result = reg.result(old)
    assert result["log_reclaimed"] is True and result["status"] == "exited" and result["exit_code"] == 0
    assert not {"stdout", "stderr", "stdout_next"} & result.keys()
    with pytest.raises(FileManagerError) as exc:
        reg.result(old, stdout_offset=0)
    assert exc.value.code == "LOG_RECLAIMED"
    assert not (old.directory / "stdout.log").exists() and (old.directory / "meta.json").exists()
    assert reg.result(new)["log_truncated"] is False


async def test_a_protected_log_is_not_reclaimed_and_the_protection_survives_a_restart(tmp_path):
    reg = JobRegistry(state_dir=tmp_path, max_log_bytes=100_000)
    kept = await finished(reg, emit(40_000))
    reg.protect(kept, reg.wall() + 3600)
    await finished(reg, emit(40_000))
    await finished(reg, emit(40_000))

    assert not kept.log_reclaimed
    assert JobRegistry(state_dir=tmp_path, max_log_bytes=100_000).get(kept.id).protect_until == kept.protect_until


async def test_protection_may_use_at_most_half_of_the_capacity(tmp_path):
    reg = JobRegistry(state_dir=tmp_path, max_log_bytes=100_000)
    first = await finished(reg, emit(30_000))
    second = await finished(reg, emit(30_000))
    reg.protect(first, reg.wall() + 3600)
    with pytest.raises(FileManagerError) as exc:
        reg.protect(second, reg.wall() + 3600)  # 60 000 > 50 000
    assert exc.value.code == "LOG_PROTECTION_LIMIT"
    assert second.protect_until == 0
    reg.protect(first, reg.wall() + 7200)  # extending an existing protection is not a new claim


async def test_growth_of_a_protected_log_stops_at_the_protection_quota(tmp_path):
    reg = JobRegistry(state_dir=tmp_path, max_log_bytes=100_000)
    job = await finished(reg, emit(30_000))
    reg.protect(job, reg.wall() + 3600)
    assert reg._reserve(job, 1_000_000) == 20_000  # 50 000 quota - 30 000 already protected


async def test_a_protecting_a_reclaimed_log_fails(tmp_path):
    reg = JobRegistry(state_dir=tmp_path, max_log_bytes=50_000)
    old = await finished(reg, emit(40_000))
    await finished(reg, emit(40_000))
    assert old.log_reclaimed
    with pytest.raises(FileManagerError) as exc:
        reg.protect(old, reg.wall() + 60)
    assert exc.value.code == "LOG_RECLAIMED"


async def test_a_job_being_downloaded_is_neither_reclaimed_nor_pruned(tmp_path):
    now = [0.0]
    reg = JobRegistry(state_dir=tmp_path, max_log_bytes=50_000, clock=lambda: now[0], ttl_sec=10)
    busy = await finished(reg, emit(40_000))
    busy.downloads = 1
    await finished(reg, emit(40_000))
    assert not busy.log_reclaimed
    now[0] = 100
    reg.prune()
    assert reg.get(busy.id) is busy
    busy.downloads = 0
    reg.prune()
    with pytest.raises(FileManagerError):
        reg.get(busy.id)


# script directories


async def test_a_script_directory_goes_when_its_job_exits(tmp_path):
    reg = JobRegistry(state_dir=tmp_path)
    directory = reg.new_script_dir("job-1")
    (directory / "script.sh").write_text("x")
    await finished(reg, py("pass"), job_id="job-1", script_dir=directory)
    assert not directory.exists()


async def test_a_script_directory_goes_once_termination_is_confirmed(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "TERMINATION_WAIT_SEC", 0.2)
    monkeypatch.setattr(jobs, "_tree_gone", lambda job: False)
    reg = JobRegistry(state_dir=tmp_path)
    directory = reg.new_script_dir("job-2")
    job = await reg.start(SLEEP, job_id="job-2", script_dir=directory)

    await reg.cancel(job.id)
    assert directory.exists()  # termination is not confirmed: the process might still be reading it
    monkeypatch.undo()
    await reg.cancel(job.id)
    assert not directory.exists()


async def test_start_up_deletes_only_script_directories_whose_process_is_known_gone(tmp_path):
    first = JobRegistry(state_dir=tmp_path)
    done = await finished(first, py("pass"))
    running = await first.start(SLEEP)
    try:
        gone = first.new_script_dir(done.id)  # left behind although its job exited and was reaped
        unsure = first.new_script_dir(running.id)
        orphan = first.scripts_dir / "no-owner"
        orphan.mkdir()

        second = JobRegistry(state_dir=tmp_path)

        assert not gone.exists()
        assert unsure.exists() and orphan.exists()
        count, size, paths = second.retained_script_report()
        assert count == 2 and set(paths) == {unsure, orphan} and size > 0
        assert json.loads((unsure / "owner.json").read_text())["job_id"] == running.id
    finally:
        await first.cancel(running.id)


async def test_start_up_keeps_the_script_directory_of_an_exited_job_without_reaped_evidence(tmp_path):
    first = JobRegistry(state_dir=tmp_path)
    done = await finished(first, py("pass"))
    leftover = first.new_script_dir(done.id)
    meta = done.directory / "meta.json"
    data = json.loads(meta.read_text())
    data["reaped"] = False
    meta.write_text(json.dumps(data))

    JobRegistry(state_dir=tmp_path)

    assert leftover.exists()


async def test_a_protected_job_is_not_pruned_after_its_ttl(tmp_path):
    skew = [0.0]
    reg = JobRegistry(state_dir=tmp_path, clock=lambda: time.monotonic() + skew[0], ttl_sec=10)
    job = await finished(reg, py("pass"))
    reg.protect(job, reg.wall() + 3600)
    skew[0] = 100
    reg.prune()
    assert reg.get(job.id) is job
