"""异常翻译成人话。"""
from astrbot_plugin_dsh_go.errors import friendly, friendly_with_detail


def test_connect_failure():
    assert "桥接服务没在运行" in friendly(
        "Cannot connect to host 127.0.0.1:6799 ssl:default"
    )


def test_token_failure():
    assert "口令" in friendly("桥接服务拒绝了 token")


def test_in_flight():
    assert "上一条指令还没结束" in friendly("a prompt is already in flight")


def test_missing_credential():
    assert "API Key" in friendly('no API key for provider route "x"')


def test_unknown_passthrough():
    assert friendly("完全没见过的错误") == "完全没见过的错误"


def test_with_detail_keeps_original():
    out = friendly_with_detail("Cannot connect to host 127.0.0.1:6799")
    assert "桥接服务没在运行" in out and "原始信息" in out


def test_insufficient_balance():
    assert "余额不足" in friendly("Internal error: turn failed: Insufficient Balance")


def test_rate_limit():
    assert "限流" in friendly("429 too many requests")


def test_context_too_long():
    assert "上下文超长" in friendly("maximum context length exceeded")
