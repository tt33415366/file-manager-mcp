import json

from file_manager_mcp.audit import AuditLog, redact_args


def test_audit_log_writes_json_lines_with_timestamp(tmp_path):
    log = AuditLog(tmp_path / "logs" / "audit.log")
    log.write(token="laptop", kind="tool", name="stat", args={"path": "C:\\x"}, outcome="ok", duration_ms=3)
    log.close()

    line = json.loads((tmp_path / "logs" / "audit.log").read_text(encoding="utf-8").strip())
    assert line["token"] == "laptop"
    assert line["kind"] == "tool"
    assert line["name"] == "stat"
    assert line["args"] == {"path": "C:\\x"}
    assert line["outcome"] == "ok"
    assert line["ts"].endswith("+00:00")


def test_redact_args_drops_content_and_env_values():
    args = {
        "path": "C:\\a.txt",
        "content": "secret file body",
        "env": {"TOKEN": "s3cret"},
        "command": "dir",
        "mode": "overwrite",
    }
    assert redact_args(args) == {"path": "C:\\a.txt", "env": ["TOKEN"], "command": "dir", "mode": "overwrite"}


def test_redact_args_handles_none():
    assert redact_args(None) == {}


def test_redact_args_hashes_script_and_arguments_without_a_preview():
    import hashlib

    out = redact_args({"script": "secret \u4e2d", "arguments": ["tok", ""], "cwd": "C:\\x"})
    data = "secret \u4e2d".encode("utf-8")
    assert out["script_bytes"] == len(data) and out["script_sha256"] == hashlib.sha256(data).hexdigest()
    assert out["arguments"][0] == {"bytes": 3, "sha256": hashlib.sha256(b"tok").hexdigest()}
    assert out["arguments"][1]["bytes"] == 0
    assert out["cwd"] == "C:\\x" and "script" not in out and "script_preview" not in out
    assert "secret" not in json.dumps(out) and "tok" not in json.dumps(out["arguments"])


def test_redact_args_preview_is_opt_in_and_limited():
    out = redact_args({"script": "abcdef", "arguments": ["xyz123"]}, preview_chars=3)
    assert out["script_preview"] == "abc" and out["arguments"][0]["preview"] == "xyz"


def test_run_command_is_still_audited_in_full():
    assert redact_args({"command": "echo secret"})["command"] == "echo secret"
