"""会话映射与后台任务标记的持久化。"""
import json

from astrbot_plugin_dsh_go.store import StateStore


def test_sessions_roundtrip(tmp_path):
    store = StateStore(str(tmp_path))
    sessions = {"chat:1": "session-abc"}
    store.save_sessions(sessions)
    assert StateStore(str(tmp_path)).load_sessions() == {"chat:1": "session-abc"}


def test_sessions_trim_to_limit(tmp_path):
    store = StateStore(str(tmp_path), max_sessions=2)
    sessions = {"a": "1", "b": "2", "c": "3"}
    store.save_sessions(sessions)
    assert list(sessions) == ["b", "c"], "应淘汰最久未用的"


def test_touch_session_moves_to_end(tmp_path):
    store = StateStore(str(tmp_path), max_sessions=3)
    sessions = {"a": "1", "b": "2"}
    store.touch_session(sessions, "a")
    assert list(sessions) == ["b", "a"]


def test_missing_file_returns_empty(tmp_path):
    assert StateStore(str(tmp_path)).load_sessions() == {}


def test_broken_json_returns_empty(tmp_path):
    (tmp_path / "sessions.json").write_text("{ not json", encoding="utf-8")
    assert StateStore(str(tmp_path)).load_sessions() == {}


def test_pending_take_once(tmp_path):
    store = StateStore(str(tmp_path))
    store.set_pending("跑测试", "chat:1")
    first = store.take_pending()
    assert first["task"] == "跑测试"
    assert store.take_pending() is None, "读后应删除，只提醒一次"


def test_no_dir_is_noop():
    store = StateStore("")
    store.save_sessions({"a": "1"})
    store.set_pending("x")
    assert store.load_sessions() == {}
    assert store.take_pending() is None
