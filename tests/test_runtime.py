import asyncio
import copy
import stat
import sys

import pytest
from mcp.client import Client

from file_manager_mcp.config import CommandExecutorConfig, Config, RemoteConfig
from file_manager_mcp.runtime import Runtime
from file_manager_mcp.tokens import TokenStore

pytestmark = pytest.mark.anyio

JOB_TOOLS = {"get_job", "cancel_job"}
REMOTE_TOOLS = {"push_to_remote", "pull_from_remote"}
SLEEP = "Start-Sleep -Seconds 30" if sys.platform == "win32" else "sleep 30"
SLOW_SCP = [sys.executable, "-c", "import time; time.sleep(30)"]


def make_config(roots, executor=False, shell="", remote_hosts=()):
    return Config(
        allowed_roots=[str(r) for r in roots],
        command_executor=CommandExecutorConfig(enabled=executor, shell=shell),
        remote=RemoteConfig(allowed_hosts=list(remote_hosts)),
    )


def changed(config, **edits):
    new = copy.deepcopy(config)
    for dotted, value in edits.items():
        target = new
        *parents, leaf = dotted.split("__")
        for name in parents:
            target = getattr(target, name)
        setattr(target, leaf, value)
    return new


@pytest.fixture
def make_runtime(tmp_path):
    def _make(config):
        return Runtime(config, TokenStore(tmp_path / "tokens.json"))

    return _make


async def names(client):
    return {t.name for t in (await client.list_tools()).tools}


async def test_apply_without_changes_reports_nothing(root, make_runtime):
    config = make_config([root])
    runtime = make_runtime(config)
    assert await runtime.apply(copy.deepcopy(config)) == {"applied": [], "restart_required": []}


async def test_apply_reports_changed_settings_by_dotted_name(root, tmp_path, make_runtime):
    other = tmp_path / "other"
    other.mkdir()
    config = make_config([root])
    runtime = make_runtime(config)
    new = changed(
        config,
        allowed_roots=[str(other)],
        command_executor__enabled=True,
        command_executor__shell="cmd",
        remote__allowed_hosts=["buildbox"],
        server__port=9999,
        logging__audit_file=str(tmp_path / "a.log"),
    )
    assert await runtime.apply(new) == {
        "applied": ["allowed_roots", "command_executor.enabled", "command_executor.shell", "remote.allowed_hosts"],
        "restart_required": ["server.port", "logging.audit_file"],
    }


async def test_apply_diffs_against_a_copy_when_the_caller_edits_its_config_in_place(root, make_runtime):
    config = make_config([root])
    runtime = make_runtime(config)
    async with Client(runtime.server) as c:
        config.command_executor.enabled = True
        assert (await runtime.apply(config))["applied"] == ["command_executor.enabled"]
        assert "run_command" in await names(c)
        config.command_executor.enabled = False
        assert (await runtime.apply(config))["applied"] == ["command_executor.enabled"]
        assert "run_command" not in await names(c)


async def test_failed_job_cancellation_does_not_fail_apply(root, make_runtime, monkeypatch, caplog):
    config = make_config([root], executor=True)
    runtime = make_runtime(config)

    async def broken_cancel(kind):
        raise RuntimeError("taskkill failed")

    monkeypatch.setattr(runtime.jobs, "cancel_kind", broken_cancel)
    result = await runtime.apply(changed(config, command_executor__enabled=False))
    assert result == {"applied": ["command_executor.enabled"], "restart_required": []}
    assert runtime.config.command_executor.enabled is False
    assert "taskkill failed" in caplog.text


async def test_changed_roots_take_effect_on_next_tool_call(root, tmp_path, make_runtime):
    other = tmp_path / "other"
    other.mkdir()
    (root / "old.txt").write_text("old")
    (other / "new.txt").write_text("new")
    config = make_config([root])
    runtime = make_runtime(config)
    async with Client(runtime.server) as c:
        assert (await c.call_tool("read_text", {"path": str(root / "old.txt")})).is_error is False
        await runtime.apply(changed(config, allowed_roots=[str(other)]))
        old = await c.call_tool("read_text", {"path": str(root / "old.txt")})
        new = await c.call_tool("read_text", {"path": str(other / "new.txt")})
    assert old.content[0].text.startswith("OUTSIDE_ROOTS:")
    assert new.structured_content["content"] == "new"
    assert runtime.config.allowed_roots == [str(other)]


async def test_os_errors_become_coded_tool_errors(root, make_runtime):
    locked = root / "locked.txt"
    locked.write_text("x")
    locked.chmod(stat.S_IREAD)
    runtime = make_runtime(make_config([root]))
    try:
        async with Client(runtime.server) as c:
            r = await c.call_tool("write_text", {"path": str(locked), "content": "y", "mode": "append"})
    finally:
        locked.chmod(stat.S_IREAD | stat.S_IWRITE)
    assert r.is_error is True
    assert r.content[0].text.startswith("PERMISSION_DENIED:")


async def test_executor_toggle_adds_and_removes_run_command(root, make_runtime):
    config = make_config([root])
    runtime = make_runtime(config)
    async with Client(runtime.server) as c:
        assert not (await names(c)) & ({"run_command"} | JOB_TOOLS)
        await runtime.apply(changed(config, command_executor__enabled=True))
        assert {"run_command"} | JOB_TOOLS <= await names(c)
        r = await c.call_tool("run_command", {"command": "exit 3", "wait_sec": 30})
        assert r.structured_content["exit_code"] == 3
        await runtime.apply(config)
        assert not (await names(c)) & ({"run_command"} | JOB_TOOLS)
        gone = await c.call_tool("run_command", {"command": "exit 3"})
    assert gone.is_error is True
    assert "Unknown tool" in gone.content[0].text


@pytest.mark.skipif(sys.platform != "win32", reason="cmd and powershell are Windows shells")
async def test_changed_shell_is_used_by_next_run_command(root, make_runtime):
    config = make_config([root], executor=True, shell="cmd")
    runtime = make_runtime(config)
    async with Client(runtime.server) as c:
        in_cmd = await c.call_tool("run_command", {"command": "echo %OS%", "wait_sec": 30})
        assert await runtime.apply(changed(config, command_executor__shell="powershell")) == {
            "applied": ["command_executor.shell"],
            "restart_required": [],
        }
        in_ps = await c.call_tool("run_command", {"command": "echo %OS%", "wait_sec": 30})
    assert in_cmd.structured_content["stdout"].strip() == "Windows_NT"
    assert in_ps.structured_content["stdout"].strip() == "%OS%"


async def test_remote_allowlist_adds_removes_tools_and_applies_to_next_transfer(root, make_runtime):
    missing = str(root / "missing.txt")
    config = make_config([root])
    runtime = make_runtime(config)
    async with Client(runtime.server) as c:
        assert not (await names(c)) & (REMOTE_TOOLS | JOB_TOOLS)
        first = changed(config, remote__allowed_hosts=["buildbox"])
        await runtime.apply(first)
        assert REMOTE_TOOLS | JOB_TOOLS <= await names(c)
        await runtime.apply(changed(first, remote__allowed_hosts=["other"]))
        old_host = await c.call_tool("push_to_remote", {"local_path": missing, "host": "buildbox", "remote_path": "/a"})
        new_host = await c.call_tool("push_to_remote", {"local_path": missing, "host": "other", "remote_path": "/a"})
        await runtime.apply(config)
        assert not (await names(c)) & (REMOTE_TOOLS | JOB_TOOLS)
    assert old_host.content[0].text.startswith("HOST_NOT_ALLOWED:")
    assert new_host.content[0].text.startswith("NOT_FOUND:")


async def test_job_tools_listed_while_executor_or_remote_is_on(root, make_runtime):
    both = make_config([root], executor=True, remote_hosts=["buildbox"])
    runtime = make_runtime(both)
    async with Client(runtime.server) as c:
        assert JOB_TOOLS <= await names(c)
        remote_only = changed(both, command_executor__enabled=False)
        await runtime.apply(remote_only)
        assert JOB_TOOLS <= await names(c)
        executor_only = changed(both, remote__allowed_hosts=[])
        await runtime.apply(executor_only)
        assert JOB_TOOLS <= await names(c)
        await runtime.apply(changed(executor_only, command_executor__enabled=False))
        assert not (await names(c)) & JOB_TOOLS


async def start_command_and_transfer(c, runtime, root):
    runtime.transfer.scp_command = SLOW_SCP
    (root / "a.txt").write_text("x")
    command = await c.call_tool("run_command", {"command": SLEEP, "wait_sec": 0})
    transfer = await c.call_tool(
        "push_to_remote", {"local_path": str(root / "a.txt"), "host": "buildbox", "remote_path": "/a", "wait_sec": 0}
    )
    assert command.structured_content["status"] == transfer.structured_content["status"] == "running"
    return runtime.jobs.get(command.structured_content["job_id"]), runtime.jobs.get(transfer.structured_content["job_id"])


async def test_disabling_executor_cancels_running_command_jobs_only(root, make_runtime):
    config = make_config([root], executor=True, remote_hosts=["buildbox"])
    runtime = make_runtime(config)
    async with Client(runtime.server) as c:
        command, transfer = await start_command_and_transfer(c, runtime, root)
        await runtime.apply(changed(config, command_executor__enabled=False))
        polled = await c.call_tool("get_job", {"job_id": command.id})
        assert transfer.status == "running"
        await runtime.jobs.cancel(transfer.id)
    assert polled.structured_content["status"] == "cancelled"
    assert command.process.returncode is not None


async def test_removing_remote_tools_cancels_running_transfer_jobs_only(root, make_runtime):
    config = make_config([root], executor=True, remote_hosts=["buildbox"])
    runtime = make_runtime(config)
    async with Client(runtime.server) as c:
        command, transfer = await start_command_and_transfer(c, runtime, root)
        await runtime.apply(changed(config, remote__allowed_hosts=[]))
        polled = await c.call_tool("get_job", {"job_id": transfer.id})
        assert command.status == "running"
        await runtime.jobs.cancel(command.id)
    assert polled.structured_content["status"] == "cancelled"
    assert transfer.process.returncode is not None


async def test_command_starting_while_executor_is_disabled_does_not_keep_running(root, make_runtime, monkeypatch):
    created, release = asyncio.Event(), asyncio.Event()
    real_exec = asyncio.create_subprocess_exec

    async def paused_exec(*args, **kwargs):
        process = await real_exec(*args, **kwargs)
        created.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", paused_exec)
    config = make_config([root], executor=True)
    runtime = make_runtime(config)
    async with Client(runtime.server) as c:
        call = asyncio.create_task(c.call_tool("run_command", {"command": SLEEP, "wait_sec": 0}))
        await created.wait()
        await runtime.apply(changed(config, command_executor__enabled=False))
        release.set()
        result = await call
        job = runtime.jobs.get(result.structured_content["job_id"])
        assert result.structured_content["status"] == "cancelled"
        assert job.process.returncode is not None

        await runtime.apply(config)
        again = await c.call_tool("run_command", {"command": "exit 3", "wait_sec": 30})
    assert again.structured_content["exit_code"] == 3


async def test_enabling_executor_prints_the_risk_warning(root, make_runtime, capsys):
    config = make_config([root])
    runtime = make_runtime(config)
    enabled = changed(config, command_executor__enabled=True)
    await runtime.apply(enabled)
    assert "WARNING: Command Executor is enabled" in capsys.readouterr().err

    await runtime.apply(changed(enabled, command_executor__shell="cmd"))
    await runtime.apply(config)
    assert capsys.readouterr().err == ""


async def test_overlapping_applies_leave_run_command_usable(root, make_runtime):
    config = make_config([root], executor=True)
    runtime = make_runtime(config)
    async with Client(runtime.server) as c:
        await asyncio.gather(runtime.apply(changed(config, command_executor__enabled=False)), runtime.apply(config))
        assert "run_command" in await names(c)
        r = await c.call_tool("run_command", {"command": "exit 3", "wait_sec": 30})
    assert r.structured_content["exit_code"] == 3


async def test_restart_required_settings_keep_running_values(root, tmp_path, make_runtime):
    (root / "a.txt").write_text("x")
    config = make_config([root])
    running = copy.deepcopy(config)
    runtime = make_runtime(config)
    new = changed(
        config,
        server__host="0.0.0.0",
        server__port=9999,
        server__public_base_url="https://fm.example.com",
        server__allowed_hosts=["fm.example.com"],
        server__allowed_origins=["https://fm.example.com"],
        logging__audit_file=str(tmp_path / "a.log"),
        command_executor__enabled=True,
    )
    result = await runtime.apply(new)
    assert result == {
        "applied": ["command_executor.enabled"],
        "restart_required": [
            "server.host",
            "server.port",
            "server.public_base_url",
            "server.allowed_hosts",
            "server.allowed_origins",
            "logging.audit_file",
        ],
    }
    assert runtime.config.server == running.server
    assert runtime.config.logging == running.logging
    assert runtime.config.command_executor.enabled is True
    async with Client(runtime.server) as c:
        link = await c.call_tool("prepare_download", {"path": str(root / "a.txt")})
    assert link.structured_content["url"].startswith("http://127.0.0.1:8765/")


async def test_apply_sends_no_list_changed_notification(root, make_runtime):
    seen = []

    async def handler(message):
        seen.append(message)

    config = make_config([root])
    runtime = make_runtime(config)
    async with Client(runtime.server, message_handler=handler, mode="legacy") as c:
        await runtime.apply(changed(config, command_executor__enabled=True, remote__allowed_hosts=["buildbox"]))
        assert "run_command" in await names(c)
    assert not [m for m in seen if "list_changed" in repr(m)]
