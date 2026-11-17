"""危险命令拦截。

这个插件把「聊天消息」变成「手机上的命令执行」。默认权限又是
``danger-full-access``，所以必须有最后一道闸：命中明显会造成
不可逆破坏的操作时，要求用户显式确认。

设计原则：
- 只拦**明确灾难性**的操作（删根目录、格式化、刷盘、fork 炸弹、关机）
- 宽进严出：宁可少拦，也不要因为误报把正常任务卡住
- 拦截时给出命中原因和放行办法，而不是干巴巴一句「不允许」
"""

from __future__ import annotations

import re

# 用户在指令里带上这句话，就视为确认执行危险操作
CONFIRM_KEYWORD = "确认执行"

# (名称, 正则) —— 命中任意一条就要确认
_DANGEROUS: list[tuple[str, re.Pattern[str]]] = [
    # 删根 / 删家目录 / 删整个盘
    ("删除根目录", re.compile(r"\brm\s+(?:-[a-zA-Z]*\s+)*-[a-zA-Z]*[rf][a-zA-Z]*\s+(?:/|/\*|~|\$HOME)(?:\s|$)")),
    ("删除根目录", re.compile(r"\brm\s+-rf\s+/\s*$")),
    # 格式化 / 分区
    ("格式化磁盘", re.compile(r"\bmkfs(?:\.\w+)?\b")),
    ("分区操作", re.compile(r"\b(?:fdisk|parted|gdisk)\b")),
    # 直接写裸设备
    ("写入块设备", re.compile(r"\bdd\b[^\n]*\bof=/dev/")),
    ("覆盖块设备", re.compile(r">\s*/dev/(?:sd|nvme|mmcblk|block)")),
    # 全盘权限放开的经典事故
    ("递归改根权限", re.compile(r"\bchmod\s+(?:-[a-zA-Z]+\s+)*[0-7]{3,4}\s+/(?:\s|$)")),
    ("递归改属主", re.compile(r"\bchown\s+(?:-[a-zA-Z]+\s+)*[^\s]+\s+/(?:\s|$)")),
    # fork 炸弹
    ("fork 炸弹", re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:")),
    # 关机 / 重启
    ("关机或重启", re.compile(r"\b(?:shutdown|poweroff|halt|reboot)\b")),
    # 下载即执行
    ("下载后直接执行", re.compile(r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:ba|z|k|da)?sh\b")),
    # 清空系统关键目录
    ("清空系统目录", re.compile(r"\brm\s+(?:-[a-zA-Z]+\s+)*/(?:etc|usr|bin|sbin|boot|system)\b")),
]


class DangerHit:
    """一次命中的结果。"""

    def __init__(self, name: str) -> None:
        self.name = name

    def message(self) -> str:
        return (
            f"⚠️ 这条指令里检测到「{self.name}」这类不可逆操作，我先没执行。\n\n"
            f"确认要这么做的话，把指令重发一遍，并在末尾加上「{CONFIRM_KEYWORD}」。\n"
            "例：/dsh 把测试目录清掉 确认执行"
        )


def check(task: str) -> DangerHit | None:
    """检查任务文本，命中危险模式返回 DangerHit，否则 None。"""
    text = task or ""
    if CONFIRM_KEYWORD in text:
        return None
    for name, pattern in _DANGEROUS:
        if pattern.search(text):
            return DangerHit(name)
    return None


def strip_confirm(task: str) -> str:
    """去掉确认词，避免它被当成任务的一部分发给 dsh-go。"""
    return (task or "").replace(CONFIRM_KEYWORD, "").strip()


__all__ = ["CONFIRM_KEYWORD", "DangerHit", "check", "strip_confirm"]
