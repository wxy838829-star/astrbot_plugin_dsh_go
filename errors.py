"""把底层异常翻译成人话。

用户不该看到 `Cannot connect to host 127.0.0.1:6799 ssl:default` 这种字符串。
这里维护一张对照表，把常见失败映射成「发生了什么 + 怎么办」。
"""

from __future__ import annotations

import re

# (匹配模式, 给用户的话)
_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"Connect call failed|Cannot connect to host|Connection refused|ClientConnectorError", re.I),
        "桥接服务没在运行。在手机上打开 DSH 的终端敲个回车会自动拉起，"
        "或运行 sh /root/dsh-go-bridge/启动.sh",
    ),
    (
        re.compile(r"401|token 不对|口令|bridge_token|拒绝了 token", re.I),
        "桥接服务口令对不上。检查插件配置里的 bridge_token 是否和手机上的一致",
    ),
    (
        re.compile(r"a prompt is already in flight|上一条任务还在跑", re.I),
        "这个聊天上一条指令还没结束，等它完成或发 /dsh 停止",
    ),
    (
        re.compile(r"MISSING_CREDENTIAL|no API key|no credential", re.I),
        "模型没有可用的 API Key，去 dsh-go 的模型设置里配一下",
    ),
    (
        re.compile(r"dsh 进程已退出|进程管道已断开|BrokenPipe", re.I),
        "dsh 引擎中途退出了，下一条指令会自动重新拉起",
    ),
    (
        re.compile(r"UNSUPPORTED_REASONING_EFFORT|does not support reasoning effort", re.I),
        "这个模型不支持当前的思考深度设置，去 dsh-go 里换一个档位",
    ),
    (
        re.compile(r"session .* does not exist|already active", re.I),
        "会话状态对不上了，发 /dsh 新对话 重开一个就好",
    ),
    (
        re.compile(r"鉴权失败|401 Unauthorized|invalid_api_key", re.I),
        "模型渠道鉴权失败，检查 API Key 是否过期",
    ),
]


def friendly(message: str) -> str:
    """把异常文本换成人话；认不出来就原样返回。"""
    text = (message or "").strip()
    if not text:
        return text
    for pattern, hint in _RULES:
        if pattern.search(text):
            return hint
    return text


def friendly_with_detail(message: str) -> str:
    """人话 + 括号里保留原始信息（方便排查）。"""
    text = (message or "").strip()
    hint = friendly(text)
    if hint == text:
        return text
    return f"{hint}\n（原始信息：{text[:160]}）"


__all__ = ["friendly", "friendly_with_detail"]
