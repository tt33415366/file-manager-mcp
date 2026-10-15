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
