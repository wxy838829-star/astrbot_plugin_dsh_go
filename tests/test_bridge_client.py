"""桥接客户端：退避、失败归类。不联网（用关闭的端口）。"""
import asyncio
import time

import pytest

from astrbot_plugin_dsh_go.bridge_client import BridgeClient, BridgeError

DEAD = "http://127.0.0.1:6798"   # 假定没人监听


def run(coro):
    return asyncio.run(coro)


def test_first_failure_sets_backoff():
    async def go():
        c = BridgeClient(DEAD, token="x", timeout=2)
        try:
            with pytest.raises(BridgeError):
                await c.health()
            assert c._fail_count == 1
            assert c._blocked_until > time.time()
            # 第二次直接拒绝，不再发请求
            t0 = time.time()
            with pytest.raises(BridgeError) as e:
                await c.health()
            assert time.time() - t0 < 0.5, "退避期应立刻拒绝"
            assert "秒后再试" in str(e.value)
        finally:
            await c.close()

    run(go())


def test_backoff_grows():
    async def go():
        c = BridgeClient(DEAD, token="x", timeout=2)
        try:
            with pytest.raises(BridgeError):
                await c.health()
            first = c._blocked_until - time.time()
            c._blocked_until = 0  # 手动跳过等待
            with pytest.raises(BridgeError):
                await c.health()
            second = c._blocked_until - time.time()
            assert second > first, "退避时间应递增"
        finally:
            await c.close()

    run(go())


def test_success_resets_counter():
    async def go():
        c = BridgeClient("http://127.0.0.1:6799", token="ignored", timeout=3)
        c._fail_count = 3
        c._blocked_until = 0
        try:
            await c.health()
            assert c._fail_count == 0
        except BridgeError:
            pytest.skip("本机没有桥接服务，跳过")
        finally:
            await c.close()

    run(go())
