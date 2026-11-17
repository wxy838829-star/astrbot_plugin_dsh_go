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
from dataclasses import dataclass
from typing import Any

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.core.star.filter.command import GreedyStr

from . import errors as errmsg
from . import formatting, plugin_installer, safety
from .audit import AuditLog, default_audit_dir
from .store import StateStore
from .bridge_client import BridgeClient, BridgeError
from .constants import _NL_PATTERN
from .helpers import _group_id, _match_install, _match_prefix, _sender_id, _split_ids
from .commands import CommandsMixin
from .engine import EngineMixin
from .tasks import TasksMixin
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


@dataclass
class RunningTask:
    """一条正在跑的任务。"""

    kind: str            # inline / background
    text: str = ""
    task: asyncio.Task | None = None
    started: float = 0.0


class DshGoPlugin(CommandsMixin, TasksMixin, EngineMixin, star.Star):
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
        # 统一的任务登记表：key -> RunningTask(kind, task, started, text)
        # 以前用 _busy + _bg 两套状态，容易不一致（改空闲回收时踩过），现在合成一份
        self._running: dict[str, RunningTask] = {}
        max_parallel = int(self._conf("max_parallel", 2) or 0)
        self._sem: asyncio.Semaphore | None = (
            asyncio.Semaphore(max_parallel) if max_parallel > 0 else None
        )
        self._inflight = 0             # 真正在跑的任务数（用于空闲回收判断）
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

    def _is_running(self, key: str) -> bool:
        return key in self._running

    def _begin_task(self, key: str, kind: str, text: str = "", task=None) -> None:
        """登记一个正在跑的任务（调用方保证不会重复登记）。"""
        self._running[key] = RunningTask(kind=kind, text=text, task=task, started=time.time())

    def _end_task(self, key: str) -> None:
        self._running.pop(key, None)

    def _background_tasks(self) -> list[tuple[str, RunningTask]]:
        return [(k, v) for k, v in self._running.items() if v.kind == "background" and v.task]

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

    @staticmethod
    def _err_text(exc: Exception) -> str:
        """把异常翻译成人话（带原始信息尾巴）。"""
        return errmsg.friendly_with_detail(str(exc))

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

    # 这两个入口必须定义在主模块里：AstrBot 按 handler 的 __module__
    # 与插件 module_path 匹配来决定绑哪个实例，放在 Mixin 里会拿不到 self。
    @filter.command("dsh", alias={"dshgo"})
    async def dsh_command(self, event: AstrMessageEvent, arg: GreedyStr):
        """把指令交给手机上的 dsh-go 执行。"""
        async for item in self.run_command(event, str(arg or "")):
            yield item

    @filter.regex(_NL_PATTERN)
    async def nl_trigger(self, event: AstrMessageEvent):
        """自然对话触发：「用 dsh 帮我…」「让 dsh 后台…」。"""
        async for item in self.run_nl_trigger(event):
            yield item

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
            if self._is_running(key):
                return "这个聊天已经有任务在跑，等它结束或先停止。"
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            bg_task = asyncio.create_task(
                self._run_background(key, task, umo), name=f"dsh-go-bg-{key}"
            )
            self._begin_task(key, "background", task, task=bg_task)
            self._store.set_pending(task, key)
            return "已转到后台执行，跑完会自动把结果发到这个聊天。"
        if self._is_running(key):
            return "dsh-go 正在处理上一条任务，请稍后再调用。"
        self._begin_task(key, "inline", task)
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
            self._end_task(key)

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
        out = formatting.to_plain((result.text or "").strip()) or "（dsh-go 跑完了，但没有给出文字内容）"
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


