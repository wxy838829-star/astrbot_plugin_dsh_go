"""引擎与会话：启动子进程、恢复会话、空闲回收、关闭。"""

from __future__ import annotations

import asyncio
import os
import shutil
import time

from astrbot.api.event import AstrMessageEvent

from . import errors as errmsg
from . import formatting, plugin_installer, safety
from .bridge_client import BridgeClient, BridgeError
from .dsh_client import (
    DshAcpClient,
    DshError,
    DshNotRunningError,
    PromptResult,
    ToolTrace,
    build_command,
)


class EngineMixin:
    def _bridge_url(self) -> str:
        return str(self._conf("bridge_url", "") or "").strip()

    @property
    def _use_bridge(self) -> bool:
        """配置了 bridge_url 就走桥接模式（AstrBot 与 dsh-go 不在同一环境时用）。"""
        return bool(self._bridge_url())

    def _get_bridge(self) -> BridgeClient:
        url = self._bridge_url()
        if self._bridge is None or self._bridge.url != url:
            self._bridge = BridgeClient(
                url,
                token=str(self._conf("bridge_token", "") or ""),
                # 0 表示不限时（BridgeClient 内部会转成 None）
                timeout=float(self._conf("timeout_seconds", 420) or 0),
                log=self.logger,
            )
        return self._bridge

    async def _ensure_client(self) -> DshAcpClient:
        """拿到可用的引擎连接；必要时拉起子进程。

        启动只在“单飞”任务里做一次，锁内不等待启动过程，
        这样别的聊天发来的指令不会一起卡在一个几十秒的启动上。
        """
        client = self._client
        if client is not None and client.alive:
            return client

        async with self._client_lock:
            # 刚拿到锁，可能别人已经启动好了
            client = self._client
            if client is not None and client.alive:
                return client
            if self._starting is None or self._starting.done():
                self._starting = asyncio.create_task(
                    self._start_client(), name="dsh-go-start"
                )
            starter = self._starting

        try:
            return await asyncio.shield(starter)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 启动失败的 task 会被复用一次是没意义的：清掉引用，
            # 下一条指令重新尝试（真起不来就每次几十秒，但至少有救）。
            self.logger.warning("[dsh-go] 引擎启动失败：%s", e)
            async with self._client_lock:
                if self._starting is starter:
                    self._starting = None
            raise

    async def _start_client(self) -> DshAcpClient:
        """真正去起进程。锁只用来交接指针，耗时的 start() 在锁外做。"""
        async with self._client_lock:
            old, self._client = self._client, None
        if old is not None:
            try:
                await old.stop()
            except Exception:
                self.logger.debug("[dsh-go] 关闭旧引擎出错", exc_info=True)

        cmd = build_command(
            dsh_bin=self._dsh_bin(),
            profile=str(self._conf("profile", "acp")),
            patch_files=self._patch_list(),
            extra_args=self._extra_args(),
        )
        cfg_timeout = float(self._conf("timeout_seconds", 420) or 0)
        client = DshAcpClient(
            cmd,
            cwd=self._work_dir(),
            env=self._build_env(),
            # 单个任务可以设成不限时，但握手/建会话这类内部请求仍要有个上限
            request_timeout=420.0 if cfg_timeout <= 0 else cfg_timeout,
            auto_approve=bool(self._conf("auto_approve", True)),
            log=self.logger,
        )
        self.logger.info("[dsh-go] 启动引擎：%s", " ".join(cmd))
        try:
            await client.start()
        except DshNotRunningError:
            raise
        except Exception as e:
            raise DshError(f"引擎启动失败：{e}") from e

        async with self._client_lock:
            if self._closed:
                # 卸载/关闭已经发生过了，别再复活一个没人回收的引擎
                self._client = None
            else:
                self._client = client
                self._arm_idle_timer()
                return client
        # 走到这里说明插件已关闭：把刚起的进程收掉，别留孤儿
        await client.stop()
        raise DshError("插件已卸载，启动被取消")

    # ------------------------------------------------------------------ 任务登记

    async def _session_for(self, key: str, client: DshAcpClient) -> str:
        """取这个聊天对应的 dsh 会话；能续就续，续不上就新建。"""
        existing = self._sessions.get(key)
        if existing and self._conf("keep_context", True):
            if await client.resume_session(existing):
                self._store.touch_session(self._sessions, key)
                await self._apply_model(client, existing)
                return existing
        sid = await client.new_session(self._work_dir())
        self._sessions[key] = sid
        self._save_sessions()
        await self._apply_model(client, sid)
        return sid

    async def _apply_model(self, client: DshAcpClient, sid: str) -> None:
        raw = str(self._conf("model", "") or "").strip()
        if not raw or "/" not in raw:
            return
        provider, _, model = raw.partition("/")
        if provider.strip() and model.strip():
            await client.set_model_for(sid, provider.strip(), model.strip())

    def _arm_idle_timer(self) -> None:
        """确保空闲回收守护在跑（幂等）。"""
        limit = float(self._conf("idle_shutdown_minutes", 20) or 0)
        if limit <= 0 or self._closed:
            return
        if self._idle_task is not None and not self._idle_task.done():
            return
        self._idle_task = asyncio.create_task(
            self._idle_watchdog(), name="dsh-go-idle"
        )

    async def _idle_watchdog(self) -> None:
        """每分钟看一眼，闲够了就关引擎。

        用轮询而不是“睡满一段再判断”：任务跨过时间点时下一个周期还能收掉，
        不会因为一次 busy 就永久失去回收能力。
        """
        try:
            while not self._closed:
                await asyncio.sleep(60)
                limit = float(self._conf("idle_shutdown_minutes", 20) or 0)
                if limit <= 0 or self._closed:
                    return
                if await self._reap_if_idle(limit * 60):
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("[dsh-go] 空闲回收出错")

    async def _reap_if_idle(self, idle_seconds: float) -> bool:
        """闲够了就关掉引擎，返回是否真的关掉了。

        「检查 + 摘掉引擎」在同一把锁里做，避免刚判断完空闲、
        别的聊天就领了任务、结果引擎被半路关掉。
        """
        async with self._client_lock:
            client = self._client
            if client is None or not client.alive:
                return False
            if self._inflight > 0 or self._running:
                return False
            if client.idle_seconds() < idle_seconds:
                return False
            self._client = None
            self.logger.info(
                "[dsh-go] 空闲超过 %.0f 分钟，关闭引擎子进程", idle_seconds / 60
            )
        try:
            # 用 shield：万一此时插件正好卸载把这个 reaper 取消，
            # dsh_client 的清理（kill 子进程）也不会半路被打断。
            await asyncio.shield(client.stop())
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.debug("[dsh-go] 空闲关闭引擎出错", exc_info=True)
        return True

    async def _cancel_idle_task(self) -> None:
        task = self._idle_task
        self._idle_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _shutdown(self) -> None:
        self._closed = True
        for key, _ in self._background_tasks():
            try:
                await self._cancel_background(key)
            except Exception:
                pass
        await self._cancel_idle_task()
        bridge, self._bridge = self._bridge, None
        if bridge is not None:
            try:
                await bridge.close()
            except Exception:
                self.logger.debug("[dsh-go] 关闭桥接客户端出错", exc_info=True)
        starter = self._starting
        self._starting = None
        if starter is not None and not starter.done():
            starter.cancel()
            await asyncio.gather(starter, return_exceptions=True)
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.stop()
            except Exception:
                self.logger.debug("[dsh-go] 关闭引擎时出错", exc_info=True)

    async def terminate(self) -> None:
        """AstrBot 卸载/停用插件时调用。"""
        await self._shutdown()

    # ------------------------------------------------------------------ 跑一个任务

