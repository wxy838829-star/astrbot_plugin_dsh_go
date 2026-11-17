"""零依赖的小工具函数。"""

from __future__ import annotations

from typing import Any

from astrbot.api.event import AstrMessageEvent

from .constants import _INSTALL_FACTS


def _match_prefix(arg: str, words: tuple[str, ...]) -> str | None:
    """指令是否以这些词之一开头；是则返回后面的正文。"""
    text = (arg or "").strip()
    low = text.lower()
    for word in words:
        if low.startswith(word):
            return text[len(word) :].lstrip(" ：:，,")
    return None

def _match_install(arg: str) -> str | None:
    """判断这条指令是不是「装插件」，返回需求正文；不是则返回 None。"""
    return _match_prefix(arg, _INSTALL_FACTS)

def _split_ids(raw: Any) -> set[str]:
    if isinstance(raw, (list, tuple, set)):
        return {str(x).strip() for x in raw if str(x).strip()}
    text = str(raw or "")
    parts = [p.strip() for p in text.replace("\n", ",").replace(" ", ",").split(",")]
    return {p for p in parts if p}

def _sender_id(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_sender_id() or "")
    except Exception:
        return ""

def _group_id(event: AstrMessageEvent) -> str:
    try:
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is None:
            return ""
        group = getattr(msg_obj, "group_id", None)
        if group:
            return str(group)
        group_obj = getattr(msg_obj, "group", None)
        return str(getattr(group_obj, "group_id", "") or "")
    except Exception:
        return ""

