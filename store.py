"""插件状态持久化。

以前会话映射、后台任务标记全在内存里，AstrBot 一重启就全没了：
用户回来发现「上下文没了」，更糟的是后台任务直接消失、连个交代都没有。

这里把两件小状态落到磁盘：

- ``sessions.json``：聊天 → dsh 会话 id，重启后还能接上上下文
- ``pending.json``：正在跑的后台任务标记，重启后能告诉用户「上次那个没跑完」

设计上刻意做得很轻：写失败只影响持久化，不影响任务本身。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


class StateStore:
    """会话映射 + 后台任务标记的磁盘存储。"""

    def __init__(self, base_dir: str, max_sessions: int = 50) -> None:
        self.base_dir = Path(base_dir) if base_dir else None
        self.max_sessions = max(1, int(max_sessions))
        self.sessions_file = self.base_dir / "sessions.json" if self.base_dir else None
        self.pending_file = self.base_dir / "pending.json" if self.base_dir else None

    # ------------------------------------------------------------------ 会话

    def load_sessions(self) -> dict[str, str]:
        """读回会话映射；文件缺失或损坏时返回空字典。"""
        if self.sessions_file is None:
            return {}
        try:
            raw = json.loads(self.sessions_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, str] = {}
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, str) and value:
                out[key] = value
        return out

    def save_sessions(self, sessions: dict[str, str]) -> None:
        """保存会话映射，超过上限时丢掉最久没更新的。"""
        if self.sessions_file is None:
            return
        # dict 保持插入顺序：把最近用到的挪到末尾，再从头部裁剪
        items = list(sessions.items())
        if len(items) > self.max_sessions:
            items = items[-self.max_sessions :]
            sessions.clear()
            sessions.update(items)
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.sessions_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(dict(sessions), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.sessions_file)
        except OSError:
            pass

    def touch_session(self, sessions: dict[str, str], key: str) -> None:
        """把某个会话标记为「最近用过」（挪到末尾）。"""
        value = sessions.pop(key, None)
        if value is not None:
            sessions[key] = value

    # ------------------------------------------------------------- 后台任务

    def set_pending(self, task: str, key: str = "") -> None:
        """记录一个开始跑的后台任务。"""
        if self.pending_file is None:
            return
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            self.pending_file.write_text(
                json.dumps(
                    {"task": task[:300], "key": key, "started": time.strftime("%Y-%m-%d %H:%M:%S")},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def clear_pending(self) -> None:
        if self.pending_file is None:
            return
        try:
            if self.pending_file.exists():
                self.pending_file.unlink()
        except OSError:
            pass

    def take_pending(self) -> dict | None:
        """读走上次没跑完的任务标记（读后删除，只提醒一次）。"""
        if self.pending_file is None:
            return None
        try:
            raw = json.loads(self.pending_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.clear_pending()
            return None
        self.clear_pending()
        return raw if isinstance(raw, dict) else None


__all__ = ["StateStore"]
