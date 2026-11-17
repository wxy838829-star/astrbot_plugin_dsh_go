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
| `model` | 空 | 指定 `渠道/模型`，留空用默认 |
| `auto_approve` | 开 | 自动同意 dsh-go 的权限请求 |
| `enable_llm_tool` | 开 | 允许聊天模型自动调用 |
| `admin_only` | 开 | 只允许 AstrBot 管理员 |
| `allowed_users` / `allowed_groups` | 空 | 白名单，填了优先于 admin_only |
| `session_scope` | `chat` | `chat` 一个群共享上下文，`user` 每人一段 |
| `show_progress` | 开 | 执行中播报「正在用 xxx」 |
| `max_reply_chars` | 3500 | 回聊天的最长字数 |
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
