"""命令词、目录约定、给模型看的固定提示词。"""

from __future__ import annotations

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
