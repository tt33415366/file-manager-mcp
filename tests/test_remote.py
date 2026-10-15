import json
import os
import sys

import pytest

from file_manager_mcp.errors import FileManagerError
from file_manager_mcp.jobs import JobRegistry
from file_manager_mcp.paths import PathPolicy
from file_manager_mcp.remote import RemoteTransfer

pytestmark = pytest.mark.anyio


@pytest.fixture
def fake_scp(tmp_path):
    record = tmp_path / "scp-args.json"
    script = tmp_path / "fake_scp.py"
    script.write_text(
        "import json, sys\n"
        f"open(r'{record}', 'w').write(json.dumps(sys.argv[1:]))\n"
        "print('copied')\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)], record


@pytest.fixture
def transfer(root, fake_scp):
    cmd, _ = fake_scp
    return RemoteTransfer(PathPolicy([str(root)]), ["buildbox", "me@10.0.0.5"], JobRegistry(), scp_command=cmd)


async def code_of(coro):
    with pytest.raises(FileManagerError) as exc:
        await coro
    return exc.value.code


async def test_push_runs_scp_in_batch_mode(transfer, fake_scp, root):
    (root / "a.txt").write_text("x")
    result = await transfer.push(str(root / "a.txt"), "buildbox", "/tmp/a.txt", wait_sec=30)

    assert result["status"] == "exited" and result["exit_code"] == 0
    args = json.loads(fake_scp[1].read_text())
    assert args == ["-B", "-o", "BatchMode=yes", "--", os.path.realpath(root / "a.txt"), "buildbox:/tmp/a.txt"]


async def test_push_directory_needs_recursive(transfer, fake_scp, root):
    (root / "d").mkdir()
    assert await code_of(transfer.push(str(root / "d"), "buildbox", "/tmp/d")) == "INVALID_ARGUMENT"
    await transfer.push(str(root / "d"), "buildbox", "/tmp/d", recursive=True, wait_sec=30)
    args = json.loads(fake_scp[1].read_text())
    assert args[:4] == ["-B", "-o", "BatchMode=yes", "-r"]


async def test_pull_runs_scp_with_remote_source(transfer, fake_scp, root):
    await transfer.pull("me@10.0.0.5", "/var/log/x.log", str(root / "x.log"), wait_sec=30)
    args = json.loads(fake_scp[1].read_text())
    assert args[-2:] == ["me@10.0.0.5:/var/log/x.log", os.path.realpath(root / "x.log")]


async def test_host_must_be_allowlisted(transfer, root):
    (root / "a.txt").write_text("x")
    assert await code_of(transfer.push(str(root / "a.txt"), "evil", "/tmp/a")) == "HOST_NOT_ALLOWED"
    assert await code_of(transfer.pull("evil", "/x", str(root / "x"))) == "HOST_NOT_ALLOWED"


async def test_remote_path_cannot_be_an_option(transfer, root):
    (root / "a.txt").write_text("x")
    assert await code_of(transfer.push(str(root / "a.txt"), "buildbox", "-oProxyCommand=x")) == "INVALID_ARGUMENT"
    assert await code_of(transfer.pull("buildbox", "", str(root / "x"))) == "INVALID_ARGUMENT"


async def test_local_path_rules(transfer, root, tmp_path):
    assert await code_of(transfer.push(str(root / "missing"), "buildbox", "/tmp/x")) == "NOT_FOUND"
    assert await code_of(transfer.push(str(tmp_path / "x"), "buildbox", "/tmp/x")) == "OUTSIDE_ROOTS"
    assert await code_of(transfer.pull("buildbox", "/x", str(tmp_path / "x"))) == "OUTSIDE_ROOTS"


async def test_default_scp_command_is_system_scp(root):
    t = RemoteTransfer(PathPolicy([str(root)]), ["buildbox"], JobRegistry())
    assert t.scp_command == ["scp"]
