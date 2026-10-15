import os
import sys

import pytest

from file_manager_mcp.errors import FileManagerError
from file_manager_mcp.executor import CommandExecutor, default_shell, shell_argv
from file_manager_mcp.jobs import JobRegistry
from file_manager_mcp.paths import PathPolicy

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows shells")


def test_shell_argv_variants():
    ps = shell_argv("powershell", "Get-Date")
    assert ps[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
    assert ps[4].startswith("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;")
    assert ps[4].endswith("Get-Date")
    assert shell_argv("pwsh", "x")[0] == "pwsh.exe"
    assert shell_argv("cmd", "dir") == "dir"
    assert shell_argv("sh", "ls") == ["/bin/sh", "-c", "ls"]


def test_default_shell():
    assert default_shell() == ("powershell" if os.name == "nt" else "sh")


@pytest.mark.anyio
@windows_only
async def test_run_command_powershell_utf8_output(root):
    ex = CommandExecutor(PathPolicy([str(root)]), "powershell", JobRegistry())
    result = await ex.run_command("Write-Output 'héllo'", wait_sec=30)
    assert result["status"] == "exited"
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "héllo"


@pytest.mark.anyio
@windows_only
async def test_run_command_defaults_cwd_to_first_root_and_merges_env(root):
    ex = CommandExecutor(PathPolicy([str(root)]), "cmd", JobRegistry())
    result = await ex.run_command("cd & echo %FM_TEST%", env={"FM_TEST": "v1"}, wait_sec=30)
    lines = result["stdout"].split()
    assert os.path.normcase(lines[0]) == os.path.normcase(os.path.realpath(root))
    assert lines[1] == "v1"


@pytest.mark.anyio
@windows_only
async def test_run_command_cmd_keeps_the_command_quotes(root):
    ex = CommandExecutor(PathPolicy([str(root)]), "cmd", JobRegistry())
    result = await ex.run_command('echo "hi there"', wait_sec=30)
    assert result["stdout"].strip() == '"hi there"'


@pytest.mark.anyio
async def test_run_command_rejects_missing_cwd(root):
    ex = CommandExecutor(PathPolicy([str(root)]), default_shell(), JobRegistry())
    with pytest.raises(FileManagerError) as exc:
        await ex.run_command("echo hi", cwd=str(root / "missing"))
    assert exc.value.code == "NOT_A_DIRECTORY"


@pytest.mark.anyio
async def test_run_command_without_roots_or_cwd_is_no_allowed_roots():
    ex = CommandExecutor(PathPolicy([]), default_shell(), JobRegistry())
    with pytest.raises(FileManagerError) as exc:
        await ex.run_command("echo hi")
    assert exc.value.code == "NO_ALLOWED_ROOTS"


@pytest.mark.anyio
async def test_run_command_without_roots_accepts_explicit_cwd(tmp_path):
    ex = CommandExecutor(PathPolicy([]), default_shell(), JobRegistry())
    result = await ex.run_command("exit 0", cwd=str(tmp_path), wait_sec=30)
    assert result["status"] == "exited" and result["exit_code"] == 0


@pytest.mark.anyio
async def test_run_command_nonzero_exit_is_result(root):
    ex = CommandExecutor(PathPolicy([str(root)]), default_shell(), JobRegistry())
    result = await ex.run_command("exit 4", wait_sec=30)
    assert result["status"] == "exited" and result["exit_code"] == 4
