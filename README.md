# astrbot_plugin_dsh_go

> 在 AstrBot 4.29.4 上实测通过。

在 QQ（或任何 AstrBot 平台）里遥控手机上的 **dsh-go**。

你发一句话 → 插件交给手机上的 dsh-go → dsh-go 用它的工具（读写文件、跑命令、查网页、看仓库）干活 → 结果回到聊天。

这样，聊天机器人本身做不到的事，也能直接在聊天里完成。

---

## 它怎么工作

```
QQ 消息
  ↓
AstrBot（插件解析 /dsh 指令，或聊天模型自己调用 dsh_go 工具）
  ↓  ACP 协议（JSON-RPC over stdio）
dsh --profile acp     ← 手机 dsh-go 的本机引擎，常驻一个子进程
  ↓
bash / 读写文件 / 网页搜索 / 网页抓取 / 子代理 …
  ↓
结果文本 → 发回聊天
```

- **常驻子进程**：引擎只起一次，之后每条指令 1~4 秒返回；空闲 20 分钟自动关掉。
- **会话延续**：同一个群/私聊对应同一段 dsh 上下文；进程重启会自动 `session/resume` 接上。
- **自动同意授权**：dsh-go 需要授权时插件自动同意（可关），否则任务会卡住等人点。
- **超时自愈**：等太久会先中断再报错，不会把后续指令一直堵住。

## 安装

整目录放进 AstrBot 的插件文件夹：

```
<AstrBot>/data/plugins/astrbot_plugin_dsh_go/
├── main.py
├── dsh_client.py
├── _conf_schema.json
├── metadata.yaml
├── __init__.py
└── README.md
```

然后重启 AstrBot，或在 WebUI「插件管理」里重载。

前提：**AstrBot 和 dsh-go 在同一台机器上**，并且 `dsh` 命令能用：

```bash
dsh --version
```

## 两种连接方式

插件支持两种模式，看 AstrBot 和 dsh-go 是不是同一个 Linux 环境：

### 模式 A：本机模式（默认）
AstrBot 和 dsh-go 装在**同一台机器**、同一个 Linux 环境里，插件直接调用 `dsh`。
什么都不用配，`bridge_url` 留空即可。

### 模式 B：桥接模式
AstrBot 装在**另一个环境**（典型例子：安卓「泡泡版」App，它自带一套独立 Linux，
里面没有 dsh）。这时在 dsh-go 那一侧跑一个桥接服务，两边通过本机回环通信。

**第一步：在 dsh-go 这边装好并启动桥接服务**

桥接服务在另一个仓库：[dsh-go-astrbot-bridge](https://github.com/wxy838829-star/dsh-go-astrbot-bridge)

```sh
git clone https://github.com/wxy838829-star/dsh-go-astrbot-bridge.git /root/dsh-go-bridge
cd /root/dsh-go-bridge
sh 启动.sh
```

它会打印地址和口令，例如：

```
地址：http://127.0.0.1:6799
口令：uemX-GW-nCde4XvWN0gEtVu_
```

口令存在 `/root/.dsh-bridge-token`，重复执行启动脚本不会变。

**第二步：在 AstrBot 插件配置里填两项**

| 配置项 | 填什么 |
| --- | --- |
| `bridge_url` | `http://127.0.0.1:6799` |
| `bridge_token` | 启动脚本打印的那个口令 |

填完保存，发 `/dsh 状态`，显示「模式：桥接」就通了。

> 其余配置项（dsh_bin、patch_files、permission_mode 等）在桥接模式下**不生效**，
> 它们由桥接服务那边的 `dsh_bridge.py` 负责。

**可选：让桥接服务随 DSH 启动**

桥接是个后台进程。想让它在 DSH 引擎启动时自动拉起，把桥接仓库里的
`autostart.mjs` 挂到 profile 即可（详见
[dsh-go-astrbot-bridge](https://github.com/wxy838829-star/dsh-go-astrbot-bridge) 的 README）：

```yaml
# ~/.dsh/profiles/web/cordis.patch.yml
- insert:
    - id: dsh-go-bridge-autostart
      name: /root/dsh-go-bridge/autostart.mjs
```

没配自启的话，手机重启后手动跑一次 `sh /root/dsh-go-bridge/启动.sh` 就行。

## 使用

| 输入 | 效果 |
| --- | --- |
| `/dsh 帮我看看 ~/notes.md 写了什么` | 交给 dsh-go 执行并回结果 |
| `/dshgo 查一下今天的科技新闻` | 同上（别名） |
| `/dsh 状态` | 引擎连没连上、工作目录、权限模式 |
| `/dsh 新对话` | 换一段干净的上下文 |
| `/dsh 停止` | 打断正在跑的任务 |
| `/dsh 后台 <任务>` | 后台跑（适合大项目），跑完自动把结果发到聊天 |
| `/dsh 任务` | 看当前有没有后台任务在跑 |
| `/dsh 历史` | 最近几次任务做了些什么（来自审计日志） |
| `/dsh 装插件 <需求>` | 让 dsh-go 现场写一个 AstrBot 插件并自动装上（需开启 `enable_install`） |
| `/dsh 帮助` | 命令说明 |

### 也可以不用指令，直接说话

只要消息以这些说法开头，就等同于 `/dsh`：

```
用dsh 查一下今天的新闻
用 dsh 帮我看看下载目录
让dsh帮我整理一下文件
帮我用dsh 搜一下这个报错
用dsh后台 把测试跑一遍
```

规则：

- 必须**先叫到机器人**（@ 或唤醒词）才会响应，群里闲聊提到 dsh 不会触发
- 支持 `用/让/叫/通过` + `dsh`/`dsh-go`，前面可带 `帮我`、后面可带 `帮我/给我/去/来`
- 带「后台」或 `bg` 就走后台模式
- 权限不够时**静默忽略**，不会在群里刷拒绝提示
- 可以在配置里用 `nl_trigger` 关掉这个能力

不带指令也可以：聊天模型会自己判断要不要调用 `dsh_go` 工具（可在配置里关掉）。

## 配置（WebUI 插件设置里有中文说明）

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `dsh_bin` | `dsh` | dsh 可执行文件；找不到就填绝对路径 |
| `patch_files` | `~/.dsh/profiles/web/cordis.patch.yml` | 复用手机 App 的模型渠道配置 |
| `work_dir` | `~` | dsh-go 的工作目录 |
| `permission_mode` | `danger-full-access` | 手机 proot 无沙箱后端，只能是这个 |
| `env_api_keys` | 空 | 补 `KEY=VALUE`，每行一个 |
| `model` | 空 | 指定 `渠道/模型`，如 `custom-worldcode/deepseek-v4.1-flash-1017`。**留空会用 dsh 的默认模型**，可能是 deepseek 官方，余额不足会失败 |
| `api_key` | 空 | 模型渠道的 API 密钥。留空则用 dsh 自己配好的 |
| `api_key_env` | `CUSTOM_WORLDCODE_API_KEY` | 上面密钥对应的环境变量名（要和 dsh 渠道配置里的一致） |
| `auto_approve` | 开 | 自动同意 dsh-go 的权限请求 |
| `enable_llm_tool` | 开 | 允许聊天模型自动调用 |
| `admin_only` | 开 | 只允许 AstrBot 管理员 |
| `allowed_users` / `allowed_groups` | 空 | 白名单，填了优先于 admin_only |
| `session_scope` | `chat` | `chat` 一个群共享上下文，`user` 每人一段 |
| `show_progress` | 开 | 执行中播报「正在用 xxx」 |
| `max_reply_chars` | 1800 | 单条消息字数上限；超出按段落切成多条，不硬截断 |
| `max_parallel` | 2 | 跨所有聊天同时最多跑几个任务，0 = 不限 |
| `ack_after_seconds` | 5 | 任务超过多少秒才回一句「还在跑」，0 = 从不提示 |
| `danger_guard` | 开 | 危险操作拦截：命中删根/格式化/刷盘/关机等要求带「确认执行」 |
| `max_sessions` | 50 | 最多记住多少段聊天的上下文 |
| `nl_trigger` | 开 | 允许「用 dsh …」这类自然语言直接触发 |
| `enable_install` | 关 | 允许 `/dsh 装插件`（会让 dsh-go 写代码并装进 AstrBot，风险自负） |
| `install_max_mb` | 16 | 自动装插件时插件包大小上限 |
| `install_author` | `dsh-go` | 自动生成插件里填的作者名 |
| `bridge_url` | 空 | 桥接模式地址；填了就走桥接，留空走本机 |
| `bridge_token` | 空 | 桥接服务口令 |
| `timeout_seconds` | 420 | 单任务超时；**设 0 = 不限时**（写大项目用） |
| `idle_shutdown_minutes` | 20 | 空闲多久关引擎，0 表示常开 |
| `keep_context` | 开 | 保留上下文 |

### 为什么权限必须是 danger-full-access

手机上的 DSH 跑在 proot 里，没有 bubblewrap / Landlock，`workspace-write` 沙箱后端不可用，命令会被直接拒绝：

```
sandbox mode "workspace-write" is requested but no sandbox backend is usable on this host
```

所以默认 `permission_mode = danger-full-access`，同时靠 `admin_only` + 白名单限制谁能用。

## ⚠️ 安全提醒

**开了这个插件，就等于在聊天里开放了「能执行命令」的能力。**

默认配置（`permission_mode=danger-full-access` + `auto_approve=true`）意味着
被授权的人可以通过聊天让手机执行任意命令。请务必保持：

- **`admin_only: true`（默认就是开的，不要关）** —— 只允许 AstrBot 管理员使用
- **不要把这个指令开放到公共群**，尤其是有其他管理员的大群
- 想收得更紧，就把 `allowed_users` 填成你自己的账号 ID，这样连其他管理员也用不了
- 桥接模式下不要把服务监听地址改成 `0.0.0.0`，默认只监听 `127.0.0.1`

白名单优先级：`allowed_users` > `allowed_groups` > `admin_only`。

## 排障

| 现象 | 处理 |
| --- | --- |
| `连不上手机上的 dsh-go` | 检查 `dsh --version`，必要时把 `dsh_bin` 写成绝对路径 |
| 一直没反应 | `/dsh 状态`；AstrBot 日志搜 `[dsh-go]` |
| 回复慢 | 首次启动 6~8 秒，之后 1~4 秒；别重复发同一条 |
| 说没权限 | `permission_mode` 改成 `danger-full-access` |
| 模型报错 | `patch_files` 指向手机 App 的模型配置，或填 `env_api_keys` |
| 任务超时 | 调大 `timeout_seconds`；或 `/dsh 停止` 后重发 |

日志关键词：`[dsh-go]`。

## 进阶：让它自己写插件并装上

开启 `enable_install` 后：

```
/dsh 装插件 一个 /hello 指令，随机夸我一句
```

流程是：

1. dsh-go 按需求写插件，放到它那边的 `/root/workspace/_plugins/`，打包成 `out.zip`
2. 插件把 zip 取回来（桥接模式走 HTTP，本机模式直接读磁盘）
3. 校验并解压到 `data/plugins/<插件名>/`
4. **重启 AstrBot**，新插件才会被扫描到并出现在插件管理里
   （不能点重载——还没加载的插件在列表里没有条目）

安全设计：

- 默认**关闭**，要手动打开
- 压缩包拒绝绝对路径和 `..`（防 zip slip）
- 必须有 `metadata.yaml`，插件名只允许字母数字 `_.-`
- 同名目录已存在时不覆盖，除非指令里带「覆盖」

> 它装的是 dsh-go 现写的代码。你等于在给一个 AI 往自己 AstrBot 里塞插件的权限，
> 请自己判断需求描述是否安全。

## 跑大项目：去掉超时 + 后台跑

默认单个任务 7 分钟就超时。写大项目时有两个办法：

### 1. 把超时改成不限

插件配置里：

```
timeout_seconds = 0        # 0 表示不限时
```

桥接模式下插件会把这个值随请求一起传给桥接服务，两边都放开，
不用再去改 `dsh_bridge.py` 的配置。

### 2. 用后台模式（推荐）

```
/dsh 后台 把 /root/workspace/我的项目 的测试跑一遍，失败的修到全过
```

特点：

- **立刻返回**，不会让你的聊天卡住等结果
- 跑完它**主动把结果发到这个聊天**
- 期间你可以正常聊天、发别的指令
- `/dsh 任务` 看还在不在跑
- `/dsh 停止` 随时中断
- 每个聊天同一时间只跑一个后台任务，避免互相干扰

> 后台任务靠内存里的 asyncio task 维持。**AstrBot 重启会丢**，
> 所以超长任务（几小时）建议拆成几个阶段，一段一段跑。

## 模型与密钥（重要）

插件支持在配置里直接指定模型和 API 密钥，**不用去改 dsh 的配置文件**。

| 配置项 | 例子 | 说明 |
| --- | --- | --- |
| `model` | `custom-worldcode/deepseek-v4.1-flash-1017` | 渠道名/模型名 |
| `api_key_env` | `CUSTOM_WORLDCODE_API_KEY` | 环境变量名，要和 dsh 渠道配置对得上 |
| `api_key` | `sk-xxxx…` | 实际密钥 |

工作方式：

- **本机模式**：插件把密钥注入给 dsh 子进程的环境变量
- **桥接模式**：插件把模型和密钥随请求带给桥接服务，桥接发现密钥变了就
  **重建 dsh 引擎**（进程环境变量建好后就改不了），再注入新密钥并设置模型

> ⚠️ **为什么一定要配 `model`**
>
> dsh 的 `acp` profile 默认走 `deepseek-official`，这个渠道如果没充值，
> 每条指令都会报 `Insufficient Balance`。即使你在 dsh 的 web 配置里设了别的
> 默认模型，走 ACP 时也不一定生效——**所以这里显式指定最稳**。

## 安全与可靠性

### 危险操作拦截（`danger_guard`）

插件把聊天消息变成命令执行，所以有一道闸。命中下面这类模式时**不会执行**，
会让你在指令里带「确认执行」再重发：

- 删根目录 / 删家目录（`rm -rf /`、`rm -rf ~`）
- 格式化、分区（`mkfs`、`fdisk`、`parted`）
- 直接写块设备（`dd of=/dev/...`、`> /dev/sda`）
- 递归改根权限（`chmod -R 777 /`）
- fork 炸弹
- 关机 / 重启
- `curl ... | bash` 这类下载即执行

正常操作不受影响：`rm -rf /sdcard/Download/tmp` 这种带具体路径的照常执行。
觉得误报了可以在配置里关掉 `danger_guard`。

### 审计日志

每个任务都会在
`<AstrBot>/data/plugin_data/astrbot_plugin_dsh_go/audit.jsonl` 记一行：

```json
{"ts": "2026-11-17 10:10:28", "kind": "task", "mode": "inline",
 "chat": "...", "user": "42", "task": "只回复三个字：审计中",
 "ok": true, "elapsed": 1.3, "tools": ""}
```

- 被拦下的危险指令记为 `kind: "guard"`
- 超过 1 MB 自动轮转为 `audit.jsonl.1`
- 随时用 `/dsh 历史` 在聊天里看最近几次

### 状态持久化

- **会话映射**存 `<AstrBot>/data/plugin_data/astrbot_plugin_dsh_go/sessions.json`，
  AstrBot 重启后上下文还能接上
- **后台任务**开始时写一个 `pending.json` 标记，跑完删除。如果重启打断了，
  下次发指令时会提醒你「上次那个后台任务没跑完」并说明内容

## 输出与交互（1.1.1）

### Markdown 自动转纯文本

QQ 不渲染 Markdown，所以插件会先整理再发：

| dsh-go 输出 | 聊天里看到 |
| --- | --- |
| `**加粗**` / `*斜体*` | 加粗 / 斜体（去掉记号） |
| `## 标题` | 【标题】 |
| `- 项目` | · 项目 |
| `> 引用` | ┆ 引用 |
| `\| a \| b \|` 表格 | · a ｜ b |
| `[文字](链接)` | 文字（链接） |
| 代码块 | **原样保留**，连 `**` 都不动 |

### 长内容自动分条

超过 `max_reply_chars`（默认 1800）不会硬截断，而是按「段落 → 行」切开分多条发。
**代码块要么完整进一条，要么在行边界拆开并各自补上围栏**，不会出现半个代码块。

### 快任务不再刷「收到」

以前每条指令都先回一句「收到，交给 dsh-go 了…」。现在只在任务超过
`ack_after_seconds`（默认 5 秒）还没结束时才补一句「还在跑」。
```

### 并发有上限

`max_parallel`（默认 2）限制**跨所有聊天**同时跑的任务数，超出的排队等待。
手机不是服务器，多个群同时用会把它拖垮。

### 任务状态统一

以前「哪个聊天在跑」存在两处（`_busy` 集合 + `_bg` 字典），改代码时容易漏判。
现在统一成一张登记表，`/dsh 任务` 和空闲回收都读同一份。

## 代码结构

按职责拆成多个文件（原来 `main.py` 一个文件 1300 行）：

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `main.py` | ~360 | 插件类、配置、handler 注册（**必须在主模块注册**，见下） |
| `commands.py` | ~510 | 聊天侧入口：指令、自然语言、后台任务、装插件 |
| `engine.py` | ~246 | 引擎与会话：启动子进程、恢复会话、空闲回收 |
| `tasks.py` | ~212 | 任务执行：跑指令、并发控制、进度播报、结果格式化 |
| `dsh_client.py` | ~690 | ACP 协议客户端 |
| `bridge_client.py` | ~253 | 桥接模式 HTTP 客户端 |
| `formatting.py` | ~209 | Markdown → 纯文本 + 长文本切片 |
| `plugin_installer.py` | ~174 | 插件包安装与 zip slip 防护 |
| `store.py` | ~116 | 会话映射与后台任务标记持久化 |
| `audit.py` | ~96 | 任务审计日志 |
| `errors.py` | ~81 | 异常翻译成人话 |
| `safety.py` | ~75 | 危险命令拦截 |
| `constants.py` | ~56 | 命令词与固定提示词 |
| `helpers.py` | ~50 | 小工具函数 |
| `texts.py` | ~17 | 给用户看的文案 |

`commands.py` / `engine.py` / `tasks.py` 是 Mixin，由 `DshGoPlugin` 多继承。

> ⚠️ **一个容易踩的坑**：AstrBot 按 handler 的 `__module__` 与插件的
> `module_path` 匹配来决定绑哪个实例。所以带 `@filter.command` / `@filter.regex`
> 的入口**必须定义在 main.py 里**，放进 Mixin 会拿不到 `self`，调用时直接报错。
> Mixin 里只放普通方法（如 `run_command`），主模块里放薄壳注册。

## 开发与测试

```sh
pip install pytest
python -m pytest tests -q
```

测试**不需要 AstrBot 本体，也不需要真实的 dsh 引擎**，几十秒跑完：

| 文件 | 覆盖 |
| --- | --- |
| `test_formatting.py` | Markdown 转换、代码块保护、切片边界 |
| `test_safety.py` | 11 类危险指令拦截 + 5 类正常指令放行 |
| `test_errors.py` | 异常到人话的映射 |
| `test_store.py` | 会话持久化、上限淘汰、pending 标记 |
| `test_audit.py` | 追加、截断、轮转、坏行容错 |
| `test_installer.py` | zip slip、缺 metadata、非法插件名、覆盖策略 |
| `test_bridge_client.py` | 失败退避、成功清零 |

推代码会自动跑（`.github/workflows/test.yml`）。

命令级的端到端测试（`/dsh` 全流程、桥接真实请求）需要 AstrBot 环境和可用的 dsh，
放在本地手动跑，不进 CI。

---

## 更新日志

### 1.3.0

**新增**
- 可在插件配置里直接指定模型和 API 密钥（`model` / `api_key_env` / `api_key`），
  不用再改 dsh 的配置文件
- 桥接模式支持随请求传递密钥：密钥变化时自动重建引擎并注入

**修复**
- **`model` 配置项一直没生效**：`set_model_for` 生成的 JSON 带空格
  （`["p", "m"]`），而引擎按字符串精确匹配 `["p","m"]`，直接报
  `unknown model option`。改用紧凑分隔符后正常
- 同步修复桥接目录里残留的旧版 `dsh_client.py`

### 1.2.2

**修复**
- 任务失败时的错误信息没有走中文翻译（余额不足、连不上、缺 API Key 仍显示英文原文），已修复

### 1.2.0

**重构**
- `main.py` 从 1321 行拆成 6 个模块（Mixin 方案），按职责分离：指令 / 引擎 / 任务 / 常量
- 补充余额不足、限流、上下文超长三类错误的中文提示

**修复**
- 拆分时发现的关键问题：带 `@filter.*` 的入口必须定义在 `main.py`，
  放 Mixin 里会导致 AstrBot 绑不到实例（指令静默失效），已修复并记录在文档里

### 1.1.2

**新增**
- 桥接失败退避：失败后 5s→10s→20s…最多 300s 内不再重试，恢复后自动清零
- `errors.py`：异常翻译成「发生了什么 + 怎么办」，保留原始信息便于排查
- `tests/` 68 个用例 + GitHub Actions CI

### 1.1.1

**新增**
- `formatting.py`：Markdown 自动转纯文本，代码块内容原样保留
- 长内容按语义边界分条发送，代码围栏保证成对，不再硬截断
- `max_parallel`：跨聊天并发上限（默认 2），超出的排队
- 快任务不再回「收到」，超过 `ack_after_seconds` 才提示「还在跑」

**改进**
- `_busy` + `_bg` 两套状态合并成 `_running` 登记表，避免判定不一致

### 1.1.0

**安全**
- `safety.py`：拦截删根目录、格式化磁盘、写块设备、递归改根权限、
  fork 炸弹、关机、`curl | bash` 等不可逆操作，需在指令里带「确认执行」才放行

**可追溯**
- `audit.py`：每个任务写一行 JSONL（时间/用户/指令/结果/耗时/工具），超 1MB 轮转
- 新增 `/dsh 历史` 查看最近任务

**可靠性**
- `store.py`：会话映射落盘，重启后上下文可恢复
- 后台任务写 `pending.json`，被重启打断后下次对话会提醒

### 1.0.6

**新增**
- 自然语言触发：`用dsh …`、`让dsh …`、`帮我用dsh …`、`用dsh后台 …`
- 必须先叫到机器人才响应；权限不足时静默，不在群里刷拒绝提示
- `dsh_go` 工具增加 `background` 参数，模型可自行判断转后台

### 1.0.5

**新增**
- `timeout_seconds = 0` 表示不限时，适合写大项目
- `/dsh 后台 <任务>`：立刻返回，跑完主动把结果发到聊天
- `/dsh 任务`：查看后台任务状态

### 1.0.4

**新增**
- `/dsh 装插件 <需求>`：dsh-go 现场写插件 → 打包 → 自动装进 AstrBot
- `plugin_installer.py`：zip slip 防护、metadata 校验、插件名白名单
- 配置 `enable_install`（默认关）、`install_max_mb`、`install_author`

### 1.0.3

- 插件描述改为纯文本（插件市场页面不渲染 Markdown）

### 1.0.2

- 补充 tags（dsh / 桥接 / 文件操作 / 远程控制 / 工具）

### 1.0.1

- 修复审核指出的问题：`logging.getLogger("astrbot")` 改为 `from astrbot.api import logger`
- 桥接服务独立运行时自动退化为 stderr 输出
- README 安全提醒加强

### 1.0.0

- 首个版本：`/dsh` 指令、`dsh_go` 工具、本机/桥接双模式、会话延续
