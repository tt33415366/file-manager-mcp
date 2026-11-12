import os
import sys

import pytest

from file_manager_mcp.errors import FileManagerError
from file_manager_mcp.executor import (
    MAX_SCRIPT_BYTES,
    CommandExecutor,
    default_shell,
    script_argv,
    script_text,
    shell_argv,
)
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
    result = await ex.run_command("Write-Output 'h茅llo'", wait_sec=30)
    assert result["status"] == "exited"
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "h茅llo"


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


# run_script


def test_script_text_per_shell():
    ps = script_text("powershell", "Write-Output 1\nWrite-Output 2")
    assert ps.startswith(b"\xef\xbb\xbf[Console]::OutputEncoding=") and ps.endswith(b"Write-Output 2")
    assert script_text("cmd", "echo a\necho b") == b"@chcp 65001>nul\r\necho a\r\necho b"
    assert script_text("sh", "echo a\r\necho b\rls") == b"echo a\necho b\nls"
    assert script_text("sh", "echo \xe4\xb8\xad") == "echo \xe4\xb8\xad".encode("utf-8")


def test_script_argv_per_shell():
    assert script_argv("powershell", "C:/s.ps1", ["a b", ""]) == [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", "C:/s.ps1", "a b", "",
    ]
    assert script_argv("cmd", "C:\\s.cmd", ["a b", "c"]) == '"C:\\s.cmd" "a b" "c"'
    assert script_argv("sh", "/t/s.sh", ["x"]) == ["/bin/sh", "/t/s.sh", "x"]


def executor(root, shell, tmp_path):
    return CommandExecutor(PathPolicy([str(root)]), shell, JobRegistry(state_dir=tmp_path))


@pytest.mark.anyio
@pytest.mark.parametrize(
    "script,arguments,timeout_sec",
    [
        ("a\0b", None, None),
        ("a\ud800b", None, None),
        ("x" * (MAX_SCRIPT_BYTES + 1), None, None),
        ("x", ["a\0"], None),
        ("x", None, 0),
        ("x", None, -1),
        ("x", None, float("inf")),
        ("x", None, float("nan")),
        ("x", None, True),
    ],
    ids=["nul", "surrogate", "too-big", "arg-nul", "zero", "negative", "inf", "nan", "bool"],
)
async def test_run_script_rejects_invalid_input_before_launching(root, tmp_path, script, arguments, timeout_sec):
    ex = executor(root, default_shell(), tmp_path)
    with pytest.raises(FileManagerError) as exc:
        await ex.run_script(script, arguments, timeout_sec=timeout_sec)
    assert exc.value.code == "INVALID_ARGUMENT"
    assert not any((tmp_path / "scripts").glob("*")) if (tmp_path / "scripts").exists() else True


@pytest.mark.anyio
async def test_the_script_size_limit_counts_utf8_bytes_not_characters(root, tmp_path):
    ex = executor(root, default_shell(), tmp_path)
    ex._check_script("\u4e2d" * (MAX_SCRIPT_BYTES // 3), [], None)  # exactly the limit minus 1 byte
    with pytest.raises(FileManagerError):
        ex._check_script("\u4e2d" * (MAX_SCRIPT_BYTES // 3 + 1), [], None)


@pytest.mark.anyio
async def test_cmd_rejects_arguments_it_cannot_pass_safely(root, tmp_path):
    ex = executor(root, "cmd", tmp_path)
    for bad in ["a&b", "a|b", "a<b", "a>b", "a^b", "%PATH%", 'a"b', "a\nb"]:
        with pytest.raises(FileManagerError) as exc:
            await ex.run_script("echo hi", [bad])
        assert exc.value.code == "INVALID_ARGUMENT"


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh")
async def test_run_script_sh_passes_arguments_and_removes_the_script(root, tmp_path):
    ex = executor(root, "sh", tmp_path)
    result = await ex.run_script('printf "%s|" "$@"; echo "$FM_X"', ["a b", "", "\u4e2d"], env={"FM_X": "v"})
    assert result["stdout"] == "a b||\u4e2d|v\n"
    assert not list((tmp_path / "scripts").glob("*"))
    assert "FM_X" not in os.environ


@pytest.mark.anyio
@windows_only
@pytest.mark.parametrize("shell", ["powershell"])
async def test_run_script_powershell_passes_arguments_verbatim(root, tmp_path, shell):
    ex = executor(root, shell, tmp_path)
    script = "foreach ($a in $args) { Write-Output ('[' + $a + ']') }; Write-Output $env:FM_X"
    result = await ex.run_script(script, ["a b", "", "\u4e2d\u6587", "$x", "it's", "c:\\dir\\"], env={"FM_X": "v"})
    assert result["exit_code"] == 0, result
    assert result["stdout"].split("\r\n")[:7] == ["[a b]", "[]", "[\u4e2d\u6587]", "[$x]", "[it's]", "[c:\\dir\\]", "v"]
    assert "FM_X" not in os.environ


@pytest.mark.anyio
@windows_only
async def test_run_script_cmd_passes_arguments_and_handles_utf8(root, tmp_path):
    ex = executor(root, "cmd", tmp_path)
    result = await ex.run_script("@echo off\necho [%~1] [%~2]\nexit /b 7", ["a b", "\u4e2d\u6587"])
    assert result["status"] == "exited" and result["exit_code"] == 7
    assert result["stdout"].strip() == "[a b] [\u4e2d\u6587]"


@pytest.mark.anyio
@windows_only
async def test_run_script_cmd_ignores_delayed_expansion_defaults(root, tmp_path):
    import winreg

    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Command Processor")
    try:
        try:
            old = winreg.QueryValueEx(key, "DelayedExpansion")
        except FileNotFoundError:
            old = None
        winreg.SetValueEx(key, "DelayedExpansion", 0, winreg.REG_DWORD, 1)
        ex = executor(root, "cmd", tmp_path)
        result = await ex.run_command("echo !PATH!", wait_sec=30)
        assert result["stdout"].strip() == "!PATH!"
    finally:
        if old is None:
            winreg.DeleteValue(key, "DelayedExpansion")
        else:
            winreg.SetValueEx(key, "DelayedExpansion", 0, old[1], old[0])
        winreg.CloseKey(key)


@pytest.mark.anyio
async def test_run_script_timeout_reports_timed_out_and_removes_the_script(root, tmp_path):
    shell = default_shell()
    ex = executor(root, shell, tmp_path)
    script = "Start-Sleep 60" if shell == "powershell" else "sleep 60"
    result = await ex.run_script(script, timeout_sec=0.5, wait_sec=30)
    assert result["status"] == "timed_out" and result["termination_complete"] is True
    assert not list((tmp_path / "scripts").glob("*"))
