"""Markdown → 纯文本、长文本切片。"""
from astrbot_plugin_dsh_go.formatting import split_message, to_plain


def test_inline_styles():
    out = to_plain("这是 **加粗** 和 *斜体* 还有 `代码`，链接 [文档](https://x.com/a)。")
    assert "**" not in out and "*斜体*" not in out and "`" not in out
    assert "加粗" in out and "文档（https://x.com/a）" in out


def test_headings_lists_quotes():
    out = to_plain("# 大标题\n## 二级\n\n- 一\n- 二\n  - 嵌套\n\n> 引用")
    assert "【大标题】" in out and "【二级】" in out
    assert "· 一" in out and "  · 嵌套" in out
    assert "┆ 引用" in out


def test_table_becomes_bullets():
    out = to_plain("| 名称 | 值 |\n| --- | --- |\n| 内存 | 82% |")
    assert "· 名称 ｜ 值" in out
    assert "· 内存 ｜ 82%" in out


def test_code_block_preserved():
    src = "改好了：\n```python\nreturn x ** 2  # ** 不能变成加粗\n```"
    out = to_plain(src)
    assert "x ** 2" in out, "代码块内容被改动了"
    assert "```" not in out, "围栏没去掉"


def test_horizontal_rule_not_eaten_as_table():
    out = to_plain("上面\n---\n下面")
    assert "上面" in out and "下面" in out and "—" in out


def test_split_keeps_code_fence_balanced():
    long = ("段落。" * 200) + "\n\n```python\n" + ("print(1)\n" * 100) + "```"
    chunks = split_message(long, 300)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, "出现了半个代码块"


def test_split_respects_limit():
    chunks = split_message("句子。" * 500, 200)
    assert all(len(c) <= 200 for c in chunks)


def test_short_text_untouched():
    assert split_message("短消息", 300) == ["短消息"]
    assert split_message("", 300) == []


def test_format_translates_error():
    """余额不足这类错误应翻译成中文，而不是原样糊给用户。"""
    import json, sys, types
    from pathlib import Path
    # 直接测 errors 层，避免构造整个插件
    from astrbot_plugin_dsh_go.errors import friendly, friendly_with_detail

    raw = "Internal error: turn failed: Insufficient Balance (request_id: xxx) (-32603)"
    assert "余额不足" in friendly(raw), "余额不足未翻译"
    out = friendly_with_detail(raw)
    assert "余额不足" in out and "原始信息" in out
