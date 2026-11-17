"""把 dsh-go 的 Markdown 输出整理成聊天里好看的样子。

QQ 这类平台不渲染 Markdown：`**加粗**` 会原样显示成星号，表格会散架，
长文本还会被平台截断。这里做两件事：

1. ``to_plain``  —— Markdown 转成纯文本（保留层次，去掉记号）
2. ``split_message`` —— 按「段落 → 行」边界切片，代码块要么完整进一段，
   要么在行边界拆开并各自补上围栏，绝不出现「半个代码块」。
"""

from __future__ import annotations

import re

_FENCE = re.compile(r"^\s*(```|~~~)\s*([\w+-]*)\s*$")
# 表格分隔行必须含竖线，否则 "---" 这种分隔线会被误判
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_HR = re.compile(r"^\s*([-*_])\1{2,}\s*$")

_BOLD = re.compile(r"(\*\*|__)(.+?)\1")
_ITALIC = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)")
_STRIKE = re.compile(r"~~(.+?)~~")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _inline(line: str) -> str:
    """处理一行普通文本里的行内 Markdown。"""
    line = _LINK.sub(lambda m: f"{m.group(1)}（{m.group(2)}）", line)
    line = _BOLD.sub(r"\2", line)
    line = _STRIKE.sub(r"\1", line)
    line = _ITALIC.sub(r"\1", line)
    line = _INLINE_CODE.sub(r"\1", line)
    return line


def _table_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def to_plain(text: str) -> str:
    """Markdown → 纯文本。代码块内容一个字都不改。"""
    if not text:
        return ""
    out: list[str] = []
    in_code = False
    table_rows: list[list[str]] = []

    def flush_table() -> None:
        nonlocal table_rows
        for row in table_rows:
            cells = [c for c in row if c]
            if cells:
                out.append("· " + " ｜ ".join(cells))
        table_rows = []

    for raw in text.splitlines():
        fence = _FENCE.match(raw)
        if fence:
            flush_table()
            in_code = not in_code
            continue
        if in_code:
            out.append(raw)  # 代码原样保留
            continue

        if _TABLE_SEP.match(raw):
            continue
        if raw.strip().startswith("|") and raw.strip().endswith("|"):
            table_rows.append(_table_row(raw))
            continue
        flush_table()

        if _HR.match(raw):
            out.append("—" * 10)
            continue

        heading = _HEADING.match(raw)
        if heading:
            out.append(f"【{heading.group(2).strip()}】")
            continue

        quote = _QUOTE.match(raw)
        if quote:
            out.append("┆ " + _inline(quote.group(1).strip()))
            continue

        bullet = _BULLET.match(raw)
        if bullet:
            indent = "  " * (len(bullet.group(1)) // 2)
            out.append(f"{indent}· {_inline(bullet.group(2).strip())}")
            continue

        out.append(_inline(raw))

    flush_table()
    body = "\n".join(out)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def _segments(text: str) -> list[tuple[str, str]]:
    """把文本拆成 ("text"|"code", 内容) 的段，代码块带围栏信息。"""
    segs: list[tuple[str, str]] = []
    buf: list[str] = []
    code: list[str] = []
    lang = ""
    in_code = False

    def flush_text() -> None:
        if buf:
            segs.append(("text", "\n".join(buf)))
            buf.clear()

    for raw in text.splitlines():
        fence = _FENCE.match(raw)
        if fence:
            if in_code:
                segs.append(("code", f"{lang}\n" + "\n".join(code)))
                code.clear()
                in_code = False
                lang = ""
            else:
                flush_text()
                in_code = True
                lang = fence.group(2) or ""
            continue
        (code if in_code else buf).append(raw)
    if in_code and code:
        segs.append(("code", f"{lang}\n" + "\n".join(code)))
    flush_text()
    return segs


def _pack(text: str, limit: int) -> list[str]:
    """把一段普通文本按行打包成不超过 limit 的块。"""
    chunks: list[str] = []
    cur = ""
    for line in text.splitlines():
        if not cur:
            cur = line
        elif len(cur) + 1 + len(line) <= limit:
            cur += "\n" + line
        else:
            chunks.append(cur)
            cur = line
        # 单行本身超长，硬切
        while len(cur) > limit:
            chunks.append(cur[:limit])
            cur = cur[limit:]
    if cur:
        chunks.append(cur)
    return chunks


def split_message(text: str, limit: int = 1800) -> list[str]:
    """按语义边界切片，保证每段不超过 limit，且代码围栏成对。"""
    text = (text or "").strip()
    if not text:
        return []
    if limit <= 0 or len(text) <= limit:
        return [text]

    chunks: list[str] = []
    cur = ""

    def push(block: str) -> None:
        nonlocal cur
        candidate = f"{cur}\n\n{block}" if cur else block
        if len(candidate) <= limit:
            cur = candidate
        else:
            if cur:
                chunks.append(cur)
            cur = block

    for kind, body in _segments(text):
        if kind == "code":
            lines = body.splitlines()
            head = lines[0] if lines else ""
            payload = lines[1:]
            block = "```" + head + "\n" + "\n".join(payload) + "\n```"
            if len(block) <= limit:
                push(block)
                continue
            # 代码块太长：按行拆，每段自己带围栏
            piece: list[str] = []
            size = len(head) + 8
            for line in payload:
                if piece and size + len(line) + 1 > limit:
                    push("```" + head + "\n" + "\n".join(piece) + "\n```")
                    piece, size = [], len(head) + 8
                piece.append(line)
                size += len(line) + 1
            if piece:
                push("```" + head + "\n" + "\n".join(piece) + "\n```")
        else:
            for block in _pack(body, limit - 2):
                push(block)

    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


__all__ = ["to_plain", "split_message"]
