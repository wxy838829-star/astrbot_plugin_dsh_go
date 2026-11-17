"""任务执行：跑一条指令、并发控制、进度播报、结果格式化。"""

from __future__ import annotations

import asyncio
import time

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain

from . import errors as errmsg
from . import formatting, plugin_installer, safety
from .bridge_client import BridgeError
from .dsh_client import (
    DshAcpClient,
    DshError,
    DshNotRunningError,
    PromptResult,
    ToolTrace,
    build_command,
)


class TasksMixin:
    async def _run_task(
        self,
        event: AstrMessageEvent,
        text: str,
        key: str,
        progress: bool = True,
    ) -> PromptResult:
        if self._sem is None:
            return await self._run_task_locked(event, text, key, progress)

        queued = self._sem.locked()
        if queued:
            self.logger.info("[dsh-go] 并发已满，排队等待：%s", key)
        async with self._sem:
            return await self._run_task_locked(event, text, key, progress)

    async def _run_task_locked(
        self,
        event: AstrMessageEvent,
        text: str,
        key: str,
        progress: bool = True,
    ) -> PromptResult:
        if self._use_bridge:
            return await self._run_task_bridge(text, key)
        client = await self._ensure_client()
        sid = await self._session_for(key, client)

        queue: asyncio.Queue[ToolTrace | None] = asyncio.Queue()
        flusher: asyncio.Task | None = None
        if progress and self._conf("show_progress", True):
            flusher = asyncio.create_task(
                self._flush_progress(event, queue), name="dsh-go-progress"
            )

        def on_tool(trace: ToolTrace) -> None:
            if flusher is not None and trace.status == "in_progress":
                try:
                    queue.put_nowait(trace)
                except Exception:
                    pass

        started = time.time()
        result: PromptResult | None = None
        self._inflight += 1
        try:
            result = await client.prompt(
                sid,
                text,
                timeout=float(self._conf("timeout_seconds", 420) or 0),
                on_tool=on_tool,
            )
        finally:
            self._inflight = max(0, self._inflight - 1)
            if flusher is not None:
                try:
                    queue.put_nowait(None)
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(flusher, timeout=5)
                except Exception:
                    flusher.cancel()
        if result is None:  # prompt 抛了非 DshError 的异常，交给上层处理
            raise DshError("dsh-go 没有返回结果（引擎可能已断开）")
        result.elapsed = time.time() - started
        return result

    async def _run_task_bridge(self, text: str, key: str) -> PromptResult:
        """桥接模式：任务交给 dsh-go 那一侧的 dsh_bridge.py 执行。"""
        bridge = self._get_bridge()
        started = time.time()
        timeout = float(self._conf("timeout_seconds", 420) or 0)
        self._inflight += 1
        try:
            data = await bridge.ask(text, key, timeout=timeout)
        except BridgeError as e:
            raise DshError(str(e)) from e
        finally:
            self._inflight = max(0, self._inflight - 1)

        result = data.to_prompt_result(session_id=key)
        if not result.text and result.tools:
            result.text = ""
        result.elapsed = time.time() - started
        self.logger.info(
            "[dsh-go] 桥接完成：ok=%s %.1fs tools=%s",
            data.ok,
            result.elapsed,
            data.tools or "-",
        )
        return result

    async def _flush_progress(
        self, event: AstrMessageEvent, queue: "asyncio.Queue[ToolTrace | None]"
    ) -> None:
        """把 dsh-go 正在用的工具，节流地播报出来。"""
        shown: set[str] = set()
        last = 0.0
        try:
            while True:
                trace = await queue.get()
                if trace is None:
                    return
                title = (trace.title or "工具").strip()
                if not title or title in shown:
                    continue
                now = time.time()
                if now - last < 3.0:
                    continue
                shown.add(title)
                last = now
                await self._send_quiet(event, f"· 正在用 {title}")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.debug("[dsh-go] 进度播报失败", exc_info=True)

    async def _send_quiet(self, event: AstrMessageEvent, text: str) -> None:
        try:
            await event.send(MessageChain([Plain(text)]))
        except Exception:
            self.logger.debug("[dsh-go] 发送进度消息失败", exc_info=True)

    def _start_ack(self, event: AstrMessageEvent, key: str) -> asyncio.Task | None:
        """慢任务才发一句「还在跑」。

        以前不管快慢都先回一句「收到」，群里很吵；现在超过 ack_after_seconds
        还没结束才补一句话。
        """
        after = float(self._conf("ack_after_seconds", 5) or 0)
        if after <= 0:
            return None
        if self._sem is not None and self._sem.locked():
            text = "前面还有任务在跑，你这条已排队，开始处理时我会继续。"
        else:
            text = "还在跑，结果出来我马上发。"
        return asyncio.create_task(self._ack_later(event, after, text), name="dsh-go-ack")

    async def _ack_later(self, event: AstrMessageEvent, after: float, text: str) -> None:
        try:
            await asyncio.sleep(after)
            await self._send_quiet(event, text)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.debug("[dsh-go] 发送等待提示失败", exc_info=True)

    async def _stop_ack(self, ack: asyncio.Task | None) -> None:
        if ack is None or ack.done():
            return
        ack.cancel()
        try:
            await asyncio.gather(ack, return_exceptions=True)
        except Exception:
            pass

    def _format(self, result: PromptResult) -> list[str]:
        """把结果整理成适合发到聊天里的若干段（Markdown → 纯文本 + 安全切片）。"""
        limit = int(self._conf("max_reply_chars", 1800) or 1800)
        body = (result.text or "").strip()
        if result.error:
            if result.timed_out:
                hint = f"dsh-go 跑了 {result.elapsed:.0f} 秒还没结束，我先不等了。"
                if body:
                    return formatting.split_message(
                        formatting.to_plain(body) + f"\n\n（{hint}可以发 /dsh 停止，或把 timeout_seconds 调大）",
                        limit,
                    )
                return [
                    f"{hint}任务可能还在后台跑，发 /dsh 停止 可以中断；"
                    f"也可以把插件配置 timeout_seconds 调大。"
                ]
            if body:
                return formatting.split_message(
                    formatting.to_plain(body) + f"\n\n（dsh-go 没正常收尾：{result.error}）", limit
                )
            return [f"dsh-go 这边出错了：{result.error}"[:limit]]
        if not body:
            tools = result.tool_summary()
            text = "dsh-go 跑完了，但没有文字回复。"
            if tools:
                text += f"\n用到的工具：{tools}"
            return [text]
        return formatting.split_message(formatting.to_plain(body), limit)

    # ------------------------------------------------------------------ 指令

