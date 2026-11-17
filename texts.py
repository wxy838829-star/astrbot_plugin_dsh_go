"""给用户看的固定文案（放一起方便改）。"""

from __future__ import annotations

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
