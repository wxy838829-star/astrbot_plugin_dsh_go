"""审计日志：追加、轮转、读取。"""
import json

from astrbot_plugin_dsh_go.audit import AuditLog


def test_append_and_tail(tmp_path):
    log = AuditLog(str(tmp_path / "audit.jsonl"))
    log.write(kind="task", mode="inline", task="干点活", ok=True)
    entries = log.tail(5)
    assert len(entries) == 1 and entries[0]["task"] == "干点活"


def test_task_is_truncated(tmp_path):
    log = AuditLog(str(tmp_path / "audit.jsonl"), task_preview=10)
    log.write(kind="task", task="x" * 100)
    assert len(log.tail(1)[0]["task"]) <= 12


def test_rotate_over_limit(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(str(path), max_bytes=200)
    for _ in range(30):
        log.write(kind="task", task="y" * 50)
    assert (tmp_path / "audit.jsonl.1").exists(), "超过上限应轮转"


def test_missing_file_returns_empty(tmp_path):
    assert AuditLog(str(tmp_path / "nope.jsonl")).tail(5) == []


def test_bad_line_skipped(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text('{"kind":"task"}\nnot json\n', encoding="utf-8")
    assert len(AuditLog(str(path)).tail(5)) == 1
