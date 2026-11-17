"""任务审计日志。

把「谁、什么时候、让它执行了什么、结果如何」记下来。
出事时这是唯一的追溯依据，所以：
- 每条任务一行 JSON（方便 grep / 导入表格）
- 追加写，不重写整个文件
- 超过大小上限自动轮转，不会无限长
- 写日志失败绝不打断任务（宁可丢日志也不能丢任务）
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class AuditLog:
    """一个极简的 JSONL 审计日志。"""

    def __init__(
        self,
        path: str,
        max_bytes: int = 1024 * 1024,
        task_preview: int = 300,
    ) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.task_preview = task_preview

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                backup = self.path.with_suffix(self.path.suffix + ".1")
                if backup.exists():
                    backup.unlink()
                self.path.rename(backup)
        except OSError:
            pass

    def write(self, **fields: Any) -> None:
        """追加一条记录。任何异常都吞掉，不影响主流程。"""
        record = {"ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        for key, value in fields.items():
            if key == "task" and isinstance(value, str) and len(value) > self.task_preview:
                value = value[: self.task_preview] + "…"
            record[key] = value
        line = json.dumps(record, ensure_ascii=False)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_if_needed()
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def tail(self, count: int = 10) -> list[dict]:
        """读最近几条，用于 /dsh 历史。读不到就返回空。"""
        try:
            if not self.path.exists():
                return []
            with open(self.path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                chunk = min(size, 64 * 1024)
                f.seek(size - chunk)
                raw = f.read().decode("utf-8", "replace")
        except OSError:
            return []
        out: list[dict] = []
        for line in reversed(raw.strip().splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(out) >= count:
                break
        return out


def default_audit_dir() -> str:
    """插件数据目录：<AstrBot>/data/plugin_data/astrbot_plugin_dsh_go。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return str(Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_dsh_go")
    except Exception:
        return ""


__all__ = ["AuditLog", "default_audit_dir"]
