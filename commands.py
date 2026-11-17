"""聊天侧入口：/dsh 指令、自然语言触发、后台任务、装插件、状态与历史。"""

from __future__ import annotations

import asyncio
import time

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain

from . import errors as errmsg
from .constants import (
    _BG_FACTS,
    _HELP_FACTS,
    _HISTORY_FACTS,
    _INSTALL_DIR,
    _INSTALL_FACTS,
    _INSTALL_PROMPT,
    _INSTALL_ZIP,
    _NEW_FACTS,
    _NL_PATTERN,
    _STATUS_FACTS,
    _STOP_FACTS,
    _TASKS_FACTS,
)
from .helpers import _group_id, _match_install, _match_prefix, _sender_id, _split_ids
from .texts import HELP_TEXT
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


class CommandsMixin:
    async def run_command(self, event: AstrMessageEvent, arg: str):
        """指令入口的实现；带装饰器的注册在 main.py（必须在主模块里注册，
        否则 AstrBot 按 module_path 绑定时找不到，会拿不到 self）。"""
        async for item in self._handle_command(event, arg or ""):
            yield item

    async def run_nl_trigger(self, event: AstrMessageEvent):
        """自然对话触发的实现；注册在 main.py。

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

        if self._is_running(key):
            yield event.plain_result("上一条还在跑，等它结束，或者发 /dsh 停止。")
            event.stop_event()
            return

        blocked = self._guard(arg)
        if blocked:
            yield event.plain_result(blocked)
            event.stop_event()
            return
        task_text = safety.strip_confirm(arg)

        self._begin_task(key, "inline", task_text)
        started = time.time()
        ack = self._start_ack(event, key)
        try:
            try:
                result = await self._run_task(event, task_text, key)
            except DshNotRunningError as e:
                yield event.plain_result(f"连不上手机上的 dsh-go：{self._err_text(e)}")
                event.stop_event()
                return
            except DshError as e:
                yield event.plain_result(f"dsh-go 处理失败：{self._err_text(e)}")
                event.stop_event()
                return
            except Exception as e:
                self.logger.exception("[dsh-go] 执行任务异常")
                yield event.plain_result(f"dsh-go 这边异常了：{self._err_text(e)}")
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
            for chunk in self._format(result):
                yield event.plain_result(chunk)
        finally:
            await self._stop_ack(ack)
            self._end_task(key)
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
        if self._is_running(key):
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

        bg_task = asyncio.create_task(
            self._run_background(key, task, umo), name=f"dsh-go-bg-{key}"
        )
        self._begin_task(key, "background", task, task=bg_task)
        self._store.set_pending(task, key)

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
            text = [f"后台任务连不上 dsh-go：{self._err_text(e)}"]
            error = str(e)
        except DshError as e:
            text = [f"后台任务失败：{self._err_text(e)}"]
            error = str(e)
        except asyncio.CancelledError:
            self._audit_write(
                kind="task", mode="background", chat=key, task=task,
                ok=False, error="cancelled",
            )
            raise
        except Exception as e:  # pragma: no cover
            self.logger.exception("[dsh-go] 后台任务异常")
            text = [f"后台任务异常：{self._err_text(e)}"]
            error = str(e)
        finally:
            self._end_task(key)
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
            for chunk in text:
                try:
                    await self.context.send_message(umo, MessageChain([Plain(chunk)]))
                except Exception:
                    self.logger.warning("[dsh-go] 后台结果发送失败", exc_info=True)
                    break

    def _tasks_text(self, key: str) -> str:
        running = "在跑" if self._is_running(key) else "没有"
        lines = [f"后台任务：{running}"]
        if len(self._running) > 1:
            lines.append(f"（其它聊天还有 {len(self._running) - 1} 个在跑）")
        timeout = float(self._conf("timeout_seconds", 420) or 0)
        lines.append(
            "单任务超时：" + ("不限时" if timeout <= 0 else f"{timeout / 60:.0f} 分钟")
        )
        return "\n".join(lines)

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

    async def _cancel_background(self, key: str) -> bool:
        entry = self._running.get(key)
        if entry is None or entry.kind != "background" or entry.task is None:
            return False
        task = entry.task
        if task.done():
            return False
        task.cancel()
        try:
            await asyncio.gather(task, return_exceptions=True)
        except Exception:
            pass
        # 本地取消了还不够：桥接那边也要明确停掉，否则会话一直占着，
        # 下一条指令会收到「上一条任务还在跑」。
        if self._use_bridge:
            try:
                await self._get_bridge().stop(key)
            except Exception:
                self.logger.debug("[dsh-go] 通知桥接停止任务失败", exc_info=True)
        return True

    # ------------------------------------------------------------------ 装插件

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
        if self._is_running(key):
            yield event.plain_result("上一条还在跑，等它结束再来。")
            return

        self._begin_task(key, "inline", requirement)
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
                yield event.plain_result(f"连不上手机上的 dsh-go：{self._err_text(e)}")
                return
            except DshError as e:
                yield event.plain_result(f"dsh-go 处理失败：{self._err_text(e)}")
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
            self._end_task(key)

    async def _fetch_install_zip(self) -> bytes:
        """把 dsh-go 侧生成的 zip 取回来。"""
        if self._use_bridge:
            data, _ = await self._get_bridge().download(_INSTALL_ZIP)
            return data
        # 本机模式：同一个文件系统，直接读
        with open(_INSTALL_ZIP, "rb") as f:
            return f.read()

