"""把聊天消息转给手机上的 dsh-go，再把结果发回聊天。

用法（QQ / 任意平台）：

    /dsh 帮我看看 /root/workspace/notes.md 写了什么
    /dshgo 查一下今天有什么科技新闻
    /dsh 状态 / /dsh 新对话 / /dsh 停止 / /dsh 帮助

不加指令时，聊天模型也能自己调用 ``dsh_go`` 工具把任务转过去。

这个文件只做 AstrBot 适配：解析消息、管理会话映射、发进度消息。
真正跑任务的是 dsh_client.py（ACP 协议，常驻子进程）。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from typing import Any

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.core.star.filter.command import GreedyStr

from . import plugin_installer, safety
from .audit import AuditLog, default_audit_dir
from .store import StateStore
from .bridge_client import BridgeClient, BridgeError
from .dsh_client import (
    DshAcpClient,
    DshError,
    DshNotRunningError,
    PromptResult,
    ToolTrace,
    build_command,
)

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "dsh_bin": "dsh",
    "profile": "acp",
    "patch_files": ["~/.dsh/profiles/web/cordis.patch.yml"],
    "extra_args": [],
    "work_dir": "~",
    "permission_mode": "danger-full-access",
    "env_api_keys": "",
    "model": "",
    "auto_approve": True,
    "enable_llm_tool": True,
    "admin_only": True,
    "allowed_users": "",
    "allowed_groups": "",
    "session_scope": "chat",
    "show_progress": True,
    "max_reply_chars": 3500,
    "timeout_seconds": 420,
    "idle_shutdown_minutes": 20,
    "keep_context": True,
    "bridge_url": "",
    "bridge_token": "",
    "enable_install": False,
    "install_max_mb": 16,
}

HELP_TEXT = """dsh-go 桥接

/dsh <指令>   把指令交给手机上的 dsh-go 执行
/dsh 状态     看引擎连没连上
/dsh 新对话   换一个干净的 dsh 上下文
/dsh 停止     打断正在跑的任务
/dsh 后台 <任务>  后台跑（适合大项目），跑完自动把结果发过来
/dsh 任务    看还在跑的后台任务
/dsh 历史    最近几次任务做了些什么
/dsh 装插件 <需求>  让 dsh-go 写个 AstrBot 插件并自动装上
/dsh 帮助     这条说明

也能不说指令，让聊天模型自己调用 dsh_go 工具转过去。"""

_STATUS_FACTS = ("状态", "status")
_NEW_FACTS = ("新对话", "新会话", "新话题", "new", "reset")
_STOP_FACTS = ("停止", "stop", "取消", "cancel", "中断")
_HELP_FACTS = ("帮助", "help", "?", "？")
_INSTALL_FACTS = ("装插件", "安装插件", "install")
# 自然语言触发：用 dsh / 让 dsh / 用 dsh 后台 …
_NL_PATTERN = (
    r"(?i)^\s*(?:请|麻烦)?\s*(?:帮我|帮忙)?\s*"
    r"(?:用|让|叫|通过)\s*dsh(?:-?go)?\s*"
    r"(?P<bg>后台|bg)?\s*(?:帮我|帮忙|给我|去|来|干|做)?\s*[：:，,、]?\s*(?P<task>.+)$"
)

_BG_FACTS = ("后台", "bg", "background")
_TASKS_FACTS = ("任务", "tasks", "后台任务")
_HISTORY_FACTS = ("历史", "history", "记录")

# 让 dsh-go 写插件的固定工作目录与产物路径（两边约定好的）
_INSTALL_DIR = "/root/workspace/_plugins"
_INSTALL_ZIP = _INSTALL_DIR + "/out.zip"


class DshGoPlugin(star.Star):
    """把聊天指令转给手机上的 dsh-go，并把结果发回聊天。"""

    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.cfg: dict[str, Any] = dict(DEFAULT_CONFIG)
        if config:
            try:
                self.cfg.update(dict(config))
            except Exception:
                self.logger.warning("[dsh-go] 插件配置读取失败，使用默认值")

        self._bridge: BridgeClient | None = None
        self._client: DshAcpClient | None = None
        self._client_lock = asyncio.Lock()
        data_dir = default_audit_dir()
        self._store = StateStore(data_dir, max_sessions=int(self._conf("max_sessions", 50) or 50))
        self._audit = AuditLog(str(self._store.base_dir / "audit.jsonl")) if data_dir else None
        self._sessions: dict[str, str] = self._store.load_sessions()
        self._pending_note = self._store.take_pending() if self._store else None
        self._busy: set[str] = set()   # 哪个聊天正在跑（用于去重提示）
        self._inflight = 0             # 真正在跑的任务数（用于空闲回收判断）
        self._bg: dict[str, asyncio.Task] = {}  # 后台任务：聊天键 -> task
        self._nl_re = None
        try:
            import re as _re

            self._nl_re = _re.compile(_NL_PATTERN)
        except Exception:  # pragma: no cover
            self.logger.warning("[dsh-go] 自然语言触发正则编译失败，已禁用")
        self._idle_task: asyncio.Task | None = None
        self._starting: asyncio.Task | None = None
        self._closed = False

    # ------------------------------------------------------------------ 配置

    def _conf(self, key: str, default: Any = None) -> Any:
        value = self.cfg.get(key, DEFAULT_CONFIG.get(key, default))
        return default if value is None else value

    def _patch_list(self) -> list[str]:
        raw = self._conf("patch_files", [])
        if isinstance(raw, str):
            raw = [x for x in raw.replace("\n", ",").split(",") if x.strip()]
        return [os.path.expanduser(str(x)) for x in (raw or []) if str(x).strip()]

    def _extra_args(self) -> list[str]:
        raw = self._conf("extra_args", [])
        if isinstance(raw, str):
            raw = raw.split()
        return [str(x) for x in (raw or []) if str(x).strip()]

    def _work_dir(self) -> str:
        return os.path.expanduser(str(self._conf("work_dir", "~") or "~"))

    def _dsh_bin(self) -> str:
        raw = str(self._conf("dsh_bin", "dsh") or "dsh").strip()
        if os.path.isabs(raw):
            return raw
        found = shutil.which(raw)
        if found:
            return found
        for guess in ("/usr/local/bin/dsh", "/usr/bin/dsh", "/opt/node/bin/dsh"):
            if os.path.exists(guess):
                return guess
        return raw

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        mode = str(self._conf("permission_mode", "danger-full-access") or "").strip()
        if mode:
            env["DSH_PERMISSION_MODE"] = mode
        raw = str(self._conf("env_api_keys", "") or "")
        for line in raw.replace(",", "\n").replace(";", "\n").splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if key and value:
                env[key] = value
        return env

    def _allow(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """检查这个用户/群能不能用 dsh-go。"""
        if not self._conf("enabled", True):
            return False, "dsh-go 桥接已关闭（插件配置 enabled 设为 false）。"

        allow_users = _split_ids(self._conf("allowed_users", ""))
        allow_groups = _split_ids(self._conf("allowed_groups", ""))
        sender = _sender_id(event)
        group = _group_id(event)

        if allow_users:
            if sender in allow_users:
                return True, ""
            return False, "你这个账号不在插件的 allowed_users 白名单里。"
        if allow_groups and group:
            if group in allow_groups:
                return True, ""
            return False, "这个群不在插件的 allowed_groups 白名单里。"
        if self._conf("admin_only", True):
            try:
                if event.is_admin():
                    return True, ""
            except Exception:
                self.logger.debug("[dsh-go] 判断管理员身份失败", exc_info=True)
            return False, "这个指令只允许 AstrBot 管理员使用（可在插件配置里关掉 admin_only）。"
        return True, ""

    def _scope_key(self, event: AstrMessageEvent) -> str:
        """聊天会话的键：默认按会话（群/私聊）分，也可以按人分。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            try:
                umo = str(event.get_session_id())
            except Exception:
                umo = "unknown"
        if str(self._conf("session_scope", "chat")).lower() == "user":
            return f"{umo}::{_sender_id(event)}"
        return umo

    # ------------------------------------------------------------------ 引擎

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

    def _save_sessions(self) -> None:
        """把会话映射落盘（失败也不影响任务）。"""
        try:
            self._store.save_sessions(self._sessions)
        except Exception:
            self.logger.debug("[dsh-go] 保存会话映射失败", exc_info=True)

    def _audit_write(self, **fields) -> None:
        """写一条审计记录（失败静默）。"""
        if self._audit is None:
            return
        try:
            self._audit.write(**fields)
        except Exception:
            pass

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
            if self._inflight > 0 or self._busy:
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
        for key in list(self._bg):
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

    def _guard(self, task: str) -> str | None:
        """危险操作拦截。返回提示文案表示已拦下，返回 None 表示放行。"""
        if not self._conf("danger_guard", True):
            return None
        hit = safety.check(task)
        if hit is None:
            return None
        self._audit_write(
            kind="guard", blocked=True, reason=hit.name, task=task, ok=False
        )
        self.logger.warning("[dsh-go] 拦截危险指令：%s", hit.name)
        return hit.message()

    async def _run_task(
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

    def _format(self, result: PromptResult) -> str:
        limit = int(self._conf("max_reply_chars", 3500) or 3500)
        body = (result.text or "").strip()
        if result.error:
            if result.timed_out:
                hint = f"dsh-go 跑了 {result.elapsed:.0f} 秒还没结束，我先不等了。"
                if body:
                    return f"{body}\n\n（{hint}可以发 /dsh 停止，或把 timeout_seconds 调大）"[:limit]
                return (
                    f"{hint}任务可能还在后台跑，发 /dsh 停止 可以中断；"
                    f"也可以把插件配置 timeout_seconds 调大。"
                )[:limit]
            if body:
                return f"{body}\n\n（dsh-go 没正常收尾：{result.error}）"[:limit]
            return f"dsh-go 这边出错了：{result.error}"[:limit]
        if not body:
            tools = result.tool_summary()
            return (
                "dsh-go 跑完了，但没有文字回复。"
                + (f"\n用到的工具：{tools}" if tools else "")
            )[:limit]
        if len(body) > limit:
            body = body[: max(0, limit - 20)].rstrip() + "\n…（结果太长，已截断）"
        return body

    # ------------------------------------------------------------------ 指令

    @filter.command("dsh", alias={"dshgo"})
    async def dsh_command(self, event: AstrMessageEvent, arg: GreedyStr):
        """把指令交给手机上的 dsh-go 执行。"""
        async for item in self._handle_command(event, str(arg or "")):
            yield item

    @filter.regex(_NL_PATTERN)
    async def nl_trigger(self, event: AstrMessageEvent):
        """自然对话触发：「用 dsh 帮我…」「让 dsh 后台…」。

        正则过滤器不受唤醒前缀约束，所以这里必须自己检查是不是被叫到了，
        否则群里随便谁提一句 dsh 都会触发。
        """
        if self._nl_re is None or not self._conf("nl_trigger", True):
            return
        if not getattr(event, "is_at_or_wake_command", False):
            return
        m = self._nl_re.match((event.message_str or "").strip())
        if not m:
            return
        # 权限不够就静默，别在群里刷拒绝提示
        allowed, _reason = self._allow(event)
        if not allowed:
            return

        task = (m.group("task") or "").strip()
        if not task:
            yield event.plain_result("想让 dsh 做什么？说具体一点，例如：用 dsh 帮我看看下载目录有多满")
            event.stop_event()
            return

        key = self._scope_key(event)
        is_bg = bool(m.group("bg"))
        if is_bg:
            async for item in self._handle_background(event, key, task):
                yield item
        else:
            async for item in self._handle_command(event, task):
                yield item
        event.stop_event()

    async def _handle_command(self, event: AstrMessageEvent, arg: str):
        allowed, reason = self._allow(event)
        if not allowed:
            yield event.plain_result(reason)
            event.stop_event()
            return

        arg = (arg or "").strip()
        fact = arg.lower()

        if not arg or fact in _HELP_FACTS:
            yield event.plain_result(HELP_TEXT)
            event.stop_event()
            return

        key = self._scope_key(event)

        if self._pending_note:
            note, self._pending_note = self._pending_note, None
            yield event.plain_result(
                f"上次有个后台任务没跑完（{note.get('started', '时间未知')}）：\n"
                f"{note.get('task', '（没记下内容）')}\n"
                "重启已经把它中断了，需要的话重发一次。"
            )

        if fact in _HISTORY_FACTS:
            yield event.plain_result(self._history_text())
            event.stop_event()
            return

        if fact in _STATUS_FACTS:
            yield event.plain_result(await self._status_text())
            event.stop_event()
            return

        if fact in _NEW_FACTS:
            self._sessions.pop(key, None)
            self._save_sessions()
            if self._use_bridge:
                try:
                    await self._get_bridge().reset(key)
                except BridgeError as e:
                    yield event.plain_result(f"新建会话失败：{e}")
                    event.stop_event()
                    return
                yield event.plain_result("好，开了个新对话，前面那段上下文不带过去了。")
                event.stop_event()
                return
            if self._client is not None and self._client.alive:
                try:
                    sid = await self._client.new_session(self._work_dir())
                    self._sessions[key] = sid
                    await self._apply_model(self._client, sid)
                except DshError as e:
                    yield event.plain_result(f"新建会话失败：{e}")
                    event.stop_event()
                    return
            yield event.plain_result("好，开了个新对话，前面那段上下文不带过去了。")
            event.stop_event()
            return

        if fact in _STOP_FACTS:
            if await self._cancel_background(key):
                yield event.plain_result("后台任务已停。")
                event.stop_event()
                return
            if self._use_bridge:
                try:
                    stopped = await self._get_bridge().stop(key)
                except BridgeError as e:
                    yield event.plain_result(f"停不了：{e}")
                    event.stop_event()
                    return
                yield event.plain_result(
                    "已经让它停下了。" if stopped else "现在没有在跑的任务。"
                )
                event.stop_event()
                return
            sid = self._sessions.get(key)
            if self._client is not None and sid:
                await self._client.cancel(sid)
                yield event.plain_result("已经让它停下了。")
            else:
                yield event.plain_result("现在没有在跑的任务。")
            event.stop_event()
            return

        bg_req = _match_prefix(arg, _BG_FACTS)
        if bg_req is not None:
            async for item in self._handle_background(event, key, bg_req):
                yield item
            event.stop_event()
            return

        if fact in _TASKS_FACTS:
            yield event.plain_result(self._tasks_text(key))
            event.stop_event()
            return

        install_req = _match_install(arg)
        if install_req is not None:
            async for item in self._handle_install(event, key, install_req):
                yield item
            event.stop_event()
            return

        if key in self._busy:
            yield event.plain_result("上一条还在跑，等它结束，或者发 /dsh 停止。")
            event.stop_event()
            return

        blocked = self._guard(arg)
        if blocked:
            yield event.plain_result(blocked)
            event.stop_event()
            return
        task_text = safety.strip_confirm(arg)

        self._busy.add(key)
        started = time.time()
        try:
            yield event.plain_result("收到，交给 dsh-go 了…")
            try:
                result = await self._run_task(event, task_text, key)
            except DshNotRunningError as e:
                yield event.plain_result(f"连不上手机上的 dsh-go：{e}")
                event.stop_event()
                return
            except DshError as e:
                yield event.plain_result(f"dsh-go 处理失败：{e}")
                event.stop_event()
                return
            except Exception as e:
                self.logger.exception("[dsh-go] 执行任务异常")
                yield event.plain_result(f"dsh-go 这边异常了：{e}")
                event.stop_event()
                return
            self._audit_write(
                kind="task",
                mode="inline",
                chat=self._scope_key(event),
                user=_sender_id(event),
                task=task_text,
                ok=bool(result.ok),
                elapsed=round(time.time() - started, 1),
                tools=result.tool_summary(),
                error=result.error,
            )
            yield event.plain_result(self._format(result))
        finally:
            self._busy.discard(key)
        event.stop_event()

    # ------------------------------------------------------------------ 后台任务

    async def _handle_background(self, event: AstrMessageEvent, key: str, task: str):
        task = (task or "").strip()
        if not task:
            yield event.plain_result(
                "要后台跑什么？例如：\n"
                "/dsh 后台 把 /root/workspace/我的项目 的测试跑一遍，失败的修到全过"
            )
            return
        if key in self._busy or key in self._bg:
            yield event.plain_result("这个聊天已经有任务在跑了，先 /dsh 停止 或等它结束。")
            return

        blocked = self._guard(task)
        if blocked:
            yield event.plain_result(blocked)
            return
        task = safety.strip_confirm(task)

        umo = str(getattr(event, "unified_msg_origin", "") or "")
        timeout = float(self._conf("timeout_seconds", 420) or 0)
        note = "不限时" if timeout <= 0 else f"最长 {timeout / 60:.0f} 分钟"
        yield event.plain_result(
            f"好，后台跑起来了（{note}）。\n"
            "跑完我会直接把结果发到这个聊天，期间你可以正常聊天。\n"
            "中途想停就发 /dsh 停止。"
        )

        self._busy.add(key)
        self._store.set_pending(task, key)
        self._bg[key] = asyncio.create_task(
            self._run_background(key, task, umo), name=f"dsh-go-bg-{key}"
        )

    async def _run_background(self, key: str, task: str, umo: str) -> None:
        """后台跑一个任务，结束后主动把结果发到聊天。"""
        ok = False
        elapsed = 0.0
        error = ""
        try:
            result = await self._run_task(None, task, key, progress=False)
            text = self._format(result)
            ok = bool(result.ok)
            elapsed = round(float(result.elapsed or 0), 1)
            error = result.error or ""
        except DshNotRunningError as e:
            text = f"后台任务连不上 dsh-go：{e}"
            error = str(e)
        except DshError as e:
            text = f"后台任务失败：{e}"
            error = str(e)
        except asyncio.CancelledError:
            self._audit_write(
                kind="task", mode="background", chat=key, task=task,
                ok=False, error="cancelled",
            )
            raise
        except Exception as e:  # pragma: no cover
            self.logger.exception("[dsh-go] 后台任务异常")
            text = f"后台任务异常：{e}"
            error = str(e)
        finally:
            self._busy.discard(key)
            self._bg.pop(key, None)
            self._store.clear_pending()

        self._audit_write(
            kind="task",
            mode="background",
            chat=key,
            task=task,
            ok=ok,
            elapsed=elapsed,
            error=error,
        )
        if umo:
            try:
                await self.context.send_message(umo, MessageChain([Plain(text)]))
            except Exception:
                self.logger.warning("[dsh-go] 后台结果发送失败", exc_info=True)

    def _history_text(self, count: int = 5) -> str:
        """最近几次任务摘要（来自审计日志）。"""
        if self._audit is None:
            return "审计日志没开，看不到历史。"
        records = [r for r in self._audit.tail(40) if r.get("kind") == "task"][:count]
        if not records:
            return "还没有任务记录。"
        lines = [f"最近 {len(records)} 次任务："]
        for r in records:
            mode = {"inline": "前台", "background": "后台", "tool": "模型自动", "install": "装插件"}.get(
                str(r.get("mode") or ""), "任务"
            )
            status = "✅" if r.get("ok") else "❌"
            ts = str(r.get("ts") or "")[5:16]
            task = str(r.get("task") or "").replace("\n", " ")
            if len(task) > 40:
                task = task[:40] + "…"
            extra = f" {r.get('elapsed', 0)}s" if r.get("elapsed") else ""
            lines.append(f"{status} {ts} [{mode}] {task}{extra}")
        return "\n".join(lines)

    def _tasks_text(self, key: str) -> str:
        running = "在跑" if key in self._bg else "没有"
        lines = [f"后台任务：{running}"]
        if len(self._bg) > 1:
            lines.append(f"（其它聊天还有 {len(self._bg) - 1} 个在跑）")
        timeout = float(self._conf("timeout_seconds", 420) or 0)
        lines.append(
            "单任务超时：" + ("不限时" if timeout <= 0 else f"{timeout / 60:.0f} 分钟")
        )
        return "\n".join(lines)

    async def _cancel_background(self, key: str) -> bool:
        task = self._bg.get(key)
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await asyncio.gather(task, return_exceptions=True)
        except Exception:
            pass
        return True

    # ------------------------------------------------------------------ 装插件

    async def _handle_install(self, event: AstrMessageEvent, key: str, requirement: str):
        """让 dsh-go 写一个插件，取回 zip 并装进 AstrBot。"""
        if not self._conf("enable_install", False):
            yield event.plain_result(
                "自动装插件没开。如果确实需要，在插件配置里把 enable_install 打开\n"
                "（它会把 dsh-go 写好的插件直接装进 AstrBot，风险自负）"
            )
            return
        requirement = (requirement or "").strip()
        if not requirement:
            yield event.plain_result(
                "要写什么样的插件？例如：\n/dsh 装插件 一个 /echo 指令，把用户说的内容重复一遍"
            )
            return
        if key in self._busy:
            yield event.plain_result("上一条还在跑，等它结束再来。")
            return

        self._busy.add(key)
        try:
            yield event.plain_result("好，让 dsh-go 写插件去了，写完会自动装上…")
            blocked = self._guard(requirement)
            if blocked:
                yield event.plain_result(blocked)
                return
            prompt = _INSTALL_PROMPT.format(
                requirement=requirement,
                dir=_INSTALL_DIR,
                zip=_INSTALL_ZIP,
                author=str(self._conf("install_author", "dsh-go") or "dsh-go"),
            )
            try:
                result = await self._run_task(event, prompt, key)
            except DshNotRunningError as e:
                yield event.plain_result(f"连不上手机上的 dsh-go：{e}")
                return
            except DshError as e:
                yield event.plain_result(f"dsh-go 处理失败：{e}")
                return
            if result.error and not result.text:
                yield event.plain_result(f"dsh-go 出错了：{result.error}")
                return

            # 取回 zip：桥接模式走 HTTP，本机模式直接读磁盘
            try:
                data = await self._fetch_install_zip()
            except (BridgeError, DshError, OSError) as e:
                yield event.plain_result(f"没拿到插件包：{e}")
                return

            limit = int(float(self._conf("install_max_mb", 16) or 16) * 1024 * 1024)
            if len(data) > limit:
                yield event.plain_result(
                    f"插件包太大了（{len(data) / 1024 / 1024:.1f} MB，上限 {limit / 1024 / 1024:.0f} MB），没有安装。"
                )
                return
            if not plugin_installer.is_zip(plugin_installer.coerce_zip(data)):
                yield event.plain_result("dsh-go 给的产物不是 zip，没有安装。")
                return

            plugins_dir = plugin_installer.default_plugins_dir()
            if not plugins_dir:
                yield event.plain_result("拿不到 AstrBot 的插件目录，没法安装。")
                return
            overwrite = "覆盖" in requirement or "替换" in requirement
            try:
                name = plugin_installer.install_plugin_zip(
                    plugin_installer.coerce_zip(data),
                    plugins_dir,
                    overwrite=overwrite,
                )
            except plugin_installer.InstallError as e:
                yield event.plain_result(f"安装失败：{e}")
                return

            self._audit_write(
                kind="task", mode="install", chat=key, user=_sender_id(event),
                task=requirement, ok=True, plugin=name,
            )
            yield event.plain_result(
                f"装好了：{name}\n"
                f"位置：{plugins_dir}/{name}\n\n"
                "最后一步：**重启 AstrBot**（不是点重载——新插件还没被扫描到，"
                "列表里没有它，也就没有可点的重载按钮）。重启后它就会出现在插件管理里。"
            )
        finally:
            self._busy.discard(key)

    async def _fetch_install_zip(self) -> bytes:
        """把 dsh-go 侧生成的 zip 取回来。"""
        if self._use_bridge:
            data, _ = await self._get_bridge().download(_INSTALL_ZIP)
            return data
        # 本机模式：同一个文件系统，直接读
        with open(_INSTALL_ZIP, "rb") as f:
            return f.read()

    async def _status_text(self) -> str:
        lines = ["dsh-go 状态", ""]
        if self._use_bridge:
            url = self._bridge_url()
            lines.append(f"模式：桥接（{url}）")
            try:
                info = await self._get_bridge().health()
                engine = info.get("engine")
                lines.append(
                    "引擎："
                    + ("已连接" if engine == "connected" else "空闲（下一条指令会自动拉起）")
                )
                lines.append(f"会话数：{info.get('sessions', 0)}")
            except BridgeError as e:
                lines.append(f"桥接服务：连不上（{e}）")
            lines.append(
                f"模型自动调用：{'开' if self._conf('enable_llm_tool', True) else '关'}"
            )
            return "\n".join(lines)

        lines.append("模式：本机")
        client = self._client
        if client is None or not client.alive:
            lines.append("引擎：没启动（下一条指令会自动拉起）")
        else:
            lines.append(f"引擎：已连接，空闲 {client.idle_seconds():.0f} 秒")
            models = client.models
            if models:
                lines.append(f"可选模型：{len(models)} 个")
        lines.append(f"dsh：{self._dsh_bin()}")
        lines.append(f"工作目录：{self._work_dir()}")
        lines.append(f"权限模式：{self._conf('permission_mode', 'danger-full-access')}")
        lines.append(f"记住的会话：{len(self._sessions)} 个")
        lines.append(
            f"模型自动调用：{'开' if self._conf('enable_llm_tool', True) else '关'}"
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------ LLM 工具

    @filter.llm_tool(name="dsh_go")
    async def dsh_go_tool(
        self, event: AstrMessageEvent, task: str, background: bool = False
    ):
        """把任务交给手机上更强的 dsh-go 智能体去执行，用于当前聊天机器人做不到的事：读写文件、执行命令、查网页、看代码仓库。用户说「用 dsh」「让 dsh 帮我」时优先调用它。

        Args:
            task(string): 要完整交给 dsh-go 执行的任务，写清目标、涉及的文件路径和约束，它会独立完成后返回结果
            background(boolean): 任务预计要跑很久（大项目、跑测试、批量处理）时设为 true，会转到后台执行，跑完自动把结果发到聊天；普通小任务留空即可
        """
        if not self._conf("enable_llm_tool", True):
            return "dsh_go 工具已在插件配置里关闭。"
        allowed, reason = self._allow(event)
        if not allowed:
            return f"无法调用 dsh_go：{reason}"
        task = (task or "").strip()
        if not task:
            return "无法调用 dsh_go：任务内容为空。"

        blocked = self._guard(task)
        if blocked:
            return blocked.replace("\n\n", " ").replace("\n", " ")
        task = safety.strip_confirm(task)

        key = self._scope_key(event)
        if background:
            if key in self._busy or key in self._bg:
                return "这个聊天已经有任务在跑，等它结束或先停止。"
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            self._busy.add(key)
            self._store.set_pending(task, key)
            self._bg[key] = asyncio.create_task(
                self._run_background(key, task, umo), name=f"dsh-go-bg-{key}"
            )
            return "已转到后台执行，跑完会自动把结果发到这个聊天。"
        if key in self._busy:
            return "dsh-go 正在处理上一条任务，请稍后再调用。"
        self._busy.add(key)
        try:
            result = await self._run_task(event, task, key, progress=False)
        except DshNotRunningError as e:
            return f"连不上手机上的 dsh-go：{e}"
        except DshError as e:
            return f"dsh-go 处理失败：{e}"
        except Exception as e:
            self.logger.exception("[dsh-go] 工具调用异常")
            return f"dsh-go 这边异常了：{e}"
        finally:
            self._busy.discard(key)

        self._audit_write(
            kind="task",
            mode="tool",
            chat=key,
            user=_sender_id(event),
            task=task,
            ok=bool(result.ok),
            elapsed=round(float(result.elapsed or 0), 1),
            tools=result.tool_summary(),
            error=result.error,
        )
        if result.error and not result.text:
            return f"dsh-go 出错了：{result.error}"
        out = (result.text or "").strip() or "（dsh-go 跑完了，但没有给出文字内容）"
        tools = result.tool_summary()
        if tools:
            out += f"\n\n[用到的工具：{tools}]"
        return out

    # ------------------------------------------------------------------ 清理

    @filter.on_plugin_unloaded()
    async def _on_unloaded(self, metadata=None):
        # 只处理自己：别的插件被卸载时不该关掉我的引擎。
        # 用模块路径判断最可靠（实例的 name 由 loader 注入，测试里可能没有）。
        my_module = type(self).__module__
        other_module = getattr(metadata, "module_path", None)
        if other_module and my_module and str(other_module) != my_module:
            return
        other_name = getattr(metadata, "name", None)
        my_name = getattr(self, "name", None)
        if not other_module and other_name and my_name and other_name != my_name:
            return
        await self._shutdown()


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


_INSTALL_PROMPT = """你现在的任务是：写一个 AstrBot 插件，并打包成 zip。

用户想要的功能：
{requirement}

硬性要求（不满足就算失败）：
1. 插件目录固定放在 {dir}/<插件目录名>/，目录名用 astrbot_plugin_ 开头的小写英文
2. 目录里必须有 metadata.yaml，至少包含这些字段：
   name（必须和目录名一致）、display_name、short_desc、desc、version（1.0.0）、
   author、repo、astrbot_version
3. 代码要能在 AstrBot 4.x 加载：
   - 插件类继承 astrbot.api.star.Star；**不要**用已废弃的 @register 装饰器（新版会自动识别 Star 子类）
   - 用 from astrbot.api.event import filter, AstrMessageEvent 注册指令
   - 日志用 self.logger（Star 自带）或 from astrbot.api import logger，禁止用 logging.getLogger
   - 顺手写一个简短的 README.md
   - 不要执行危险操作，不要联网拉未知内容
4. 写完后把插件目录打包成 {zip}：
   zip 根目录直接是插件文件（metadata.yaml 在最外层），不要再套一层目录
5. 打包完成后，只回复一行：DONE <插件目录名>

注意：{zip} 这个文件必须真实存在，AstrBot 会来取。"""


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
