# huohuoer/wechat-cli 源码调研与终端工具实现建议

日期：2026-10-25。

目标：以个人微信既有好友会话为对象，在 Linux 终端查询、接收与发送消息，后续再接自动回复。

重点仓库：[huohuoer/wechat-cli](https://github.com/huohuoer/wechat-cli)。检查快照：[a378923](https://github.com/huohuoer/wechat-cli/tree/a3789232d4f79bf0b30634d9dadbce71e4acd601)，提交日期 2026-04-06；代码版本 `0.2.4`。本次该仓库可读取，Releases API 返回空列表。

范围：读取源码、配置与分发脚本，并用合成 SQLite 数据核对增量查询行为。没有安装到用户环境，没有访问真实聊天数据库，没有提取微信进程密钥、登录账号或发送消息。

## 判断

**适合作为读取本地微信数据的参考或初始模块，不是可以直接满足终端双向收发需求的完整客户端。**

Linux 支持存在实际代码，并非只在 README 写一个平台名称。但发送、持续监听和交互式聊天界面均缺失；现有 `new-messages` 也不能直接作为完整消息事件流。

如果基于它开发，建议复用数据库定位、密钥验证、联系人解析和历史查询，重做结构化消息事件、游标与缓存隔离，另外实现发送后端。这样可以继续使用官方 Linux 微信负责登录与联网，无需从零实现私人协议。

## 当前具备与缺失的能力

| 能力 | 源码核查结果 | 对项目的意义 |
| --- | --- | --- |
| 最近会话、未读 | 有对应命令 | 可用作会话发现与列表展示 |
| 历史查询、搜索 | 有实现 | 读取本机已同步的数据，不是服务端全量历史接口 |
| 联系人、群成员、收藏、统计、导出 | 有对应命令 | 可参考命令组织与数据解析 |
| Linux 数据目录定位 | 有实现 | 支持部分约定路径与显式指定路径 |
| Linux 密钥提取 | 有实现 | 读取 `/proc/<pid>/maps` 与 `/proc/<pid>/mem` |
| `new-messages` | 会话摘要差异查询 | 不能保证逐条接收 |
| 文本、文件发送 | 没有命令或对应发送模块 | 必须另外实现 |
| 登录、退出官方微信、切换窗口 | 不提供相应控制命令 | `init` 是数据访问初始化，不是微信登录 |
| TUI / 交互聊天 | 没有 | 当前是一次一条命令的查询 CLI |
| daemon / 持续事件订阅 | 没有对应入口 | 跨调用复用的是磁盘缓存，不是常驻收消息服务 |

依据：[命令注册](https://github.com/huohuoer/wechat-cli/blob/a3789232d4f79bf0b30634d9dadbce71e4acd601/wechat_cli/main.py)、[项目依赖](https://github.com/huohuoer/wechat-cli/blob/a3789232d4f79bf0b30634d9dadbce71e4acd601/pyproject.toml)。

实现语言是 Python，CLI 使用 Click，依赖 PyCryptodome 和 zstandard。npm 是 Python 打包产物的启动包装，不是 Node.js 实现的微信协议库。

## 数据接入机制

```text
已登录的官方 Linux 微信
        │
        ├── 进程内存 ──→ 初始化时提取数据库密钥
        │
        └── 加密数据库与 WAL
                    ↓
            解密为本地 SQLite 缓存
                    ↓
            查询 / 名称解析 / 格式化
                    ↓
              JSON 或文本输出
```

源码通过进程名称、可执行文件路径等筛选微信进程，然后扫描特定格式的密钥候选，并验证数据库页。它不是调用腾讯公开的个人消息 API，也不是通过 OCR 读取聊天。

权限检查要求 root 或进程具备 `CAP_SYS_PTRACE`。这不等于普通用户运行全部命令都需要 root；密钥初始化与后续数据查询应分开处理。但在本机是否能提取、是否匹配微信 `4.1.1.4`，尚未实际验证。

上游报错建议包含给通用 `python3` 设置 `CAP_SYS_PTRACE` 的命令，不建议直接照搬为本项目默认配置。这会扩大该解释器的进程读取权限；开发时应把提权范围限制在必要初始化步骤。

此外，状态目录由当前进程的 `~` 推导。通过 sudo 初始化时，需要检查配置、密钥的实际落点与文件归属，避免初始化写入 root 家目录而普通用户随后找不到。

依据：[Linux 扫描器](https://github.com/huohuoer/wechat-cli/blob/a3789232d4f79bf0b30634d9dadbce71e4acd601/wechat_cli/keys/scanner_linux.py)、[密钥验证与保存](https://github.com/huohuoer/wechat-cli/blob/a3789232d4f79bf0b30634d9dadbce71e4acd601/wechat_cli/keys/common.py)、[目录配置](https://github.com/huohuoer/wechat-cli/blob/a3789232d4f79bf0b30634d9dadbce71e4acd601/wechat_cli/core/config.py)。

## 最影响选型的四项限制

### 1. `new-messages` 只返回会话最后一条摘要

实现读取 `session.db` 的 `SessionTable`，比较每个会话的 `last_timestamp`，输出该会话的 `summary`。它没有据此进一步扫描消息表，找出期间所有新增消息。

例如好友在两次轮询之间依次发送“明天有空吗”和“下午三点见”，它只能返回最新摘要“下午三点见”。同一个时间戳内摘要继续变化，严格大于旧时间戳的条件也可能漏掉变化。

本次从上游源码提取原函数，仅移除 Click 装饰器，给它接入合成 `SessionTable` 与临时状态目录，得到：

```json
{
  "burst_unread": 2,
  "burst_emitted_count": 1,
  "burst_emitted_texts": ["second of two messages"],
  "same_timestamp_changed_summary_emitted_count": 0
}
```

这是离线逻辑验证，不是本机微信实测。它证明不能仅通过缩短轮询周期来把当前命令变成可靠收件流。

还应注意：首次调用返回未读会话摘要，后续调用返回会话时间变化；这些变化没有可靠的 `is_self` 字段，不能默认全部来自好友。

依据：[new_messages.py](https://github.com/huohuoer/wechat-cli/blob/a3789232d4f79bf0b30634d9dadbce71e4acd601/wechat_cli/commands/new_messages.py)。

### 2. JSON 外壳内的历史消息仍是格式化字符串

`history --format json` 的 `messages` 列表由已经格式化的历史文本组成。查询读到了 `local_id` 等字段，但输出没有完整保留稳定消息标识、原始时间、发送者身份和方向。

这适合人工查看或摘要分析；用于终端聊天和自动回复时，需要新增结构化消息对象，而不是再从显示字符串反解析。

建议内部至少保留账号 ID、会话 ID、来源分片/表、local ID、可用时的 server ID、发送者 ID、方向、原始时间戳、消息类型与正文。跨分片的 local ID 不应假定全局唯一。

依据：[history 命令输出](https://github.com/huohuoer/wechat-cli/blob/a3789232d4f79bf0b30634d9dadbce71e4acd601/wechat_cli/commands/history.py)、[消息查询和格式化](https://github.com/huohuoer/wechat-cli/blob/a3789232d4f79bf0b30634d9dadbce71e4acd601/wechat_cli/core/messages.py)。

### 3. 缓存与游标缺少账号、并发和消费端隔离

`DBCache` 使用固定临时目录 `wechat_cli_cache`，缓存文件名仅由数据库相对路径计算。两个账号都有 `message/message_0.db` 时，会得到同一个缓存文件路径，存在覆盖或并发干扰风险。此处是源码推导，没有用真实账号复现数据串读。

`last_check.json` 也固定在全局状态目录。不同配置或两个消费程序共享同一游标，并且状态写入没有原子替换、锁或消费确认。程序写入游标后、输出被管道中断时，下一次查询可能不再返回相应变化。

缓存保存的是解密后的数据库；初始化与缓存模块没有在这些写入路径上显式统一设置私有权限，实际权限受 umask 等影响。做成常驻服务前，需要按账号建立私有目录，并使用锁、原子替换和明确的缓存生命周期。

依据：[缓存实现](https://github.com/huohuoer/wechat-cli/blob/a3789232d4f79bf0b30634d9dadbce71e4acd601/wechat_cli/core/db_cache.py)、[增量状态文件](https://github.com/huohuoer/wechat-cli/blob/a3789232d4f79bf0b30634d9dadbce71e4acd601/wechat_cli/commands/new_messages.py)。

### 4. 源码跨平台不等于安装分发已覆盖 Linux

目录中有 Linux 平台包描述与启动映射，但 npm 主包 `optionalDependencies` 只声明了 macOS arm64 平台包。启动包装不会因此自动获得 Linux 二进制，错误提示还残留不同包名。

README 的 npm / pip 命令指向包注册表，不会自动固定到 `huohuoer` 的当前源码。多个文档和包元数据链接也仍指向其他仓库名。因此，验证这个仓库应优先从固定提交在隔离 Python 环境中安装，不能把安装同名包当作验证了该源码。

本次没有查询 npm/PyPI 当前发布内容，也没有验证任何安装包。项目树未发现测试目录或 CI 验收配置，Releases API 返回空列表；这些限制不证明代码不可用，但不能据此承诺即装即用。

依据：[npm 主包](https://github.com/huohuoer/wechat-cli/blob/a3789232d4f79bf0b30634d9dadbce71e4acd601/npm/wechat-cli/package.json)、[启动包装](https://github.com/huohuoer/wechat-cli/blob/a3789232d4f79bf0b30634d9dadbce71e4acd601/npm/wechat-cli/bin/wechat-cli.js)。

## 用它开发 wereply 的建议

首版可以保留 Python 和命令式接口，优先完成终端手动收发，不必同时加入 AI 或完整 TUI。

```text
wereply CLI / 后续 TUI
            │
            ▼
    本地 daemon：账号、事件、任务、游标
          │                   │
          ▼                   ▼
   Reader：只读数据库     Sender：AT-SPI / X11
          │                   │
          └──── 官方 Linux 微信 ────┘
```

这里是设计建议，不代表原仓库已有 daemon。它让官方微信继续负责登录和网络协议，终端成为操作入口。后台仍需要微信图形进程；部署到服务器时可以使用虚拟显示器。

可以参考并改造 `core/config.py`、`keys/`、`core/messages.py` 和联系人相关代码。仓库标注 Apache-2.0 许可，实际纳入代码时应检查许可文件和来源声明。

必须补齐的部分：

1. **完整接收：** 从真实消息表增量读取，采用账号、分片、表和消息标识构成游标；时间仅作辅助过滤，处理同秒多条、分片变化与自己发出的消息。
2. **持久事件：** 收到消息先落本项目收件箱，再推进扫描状态。每个监听端使用独立消费游标；重启可恢复。
3. **受控发送：** 根据明确的会话 ID 定位目标，核对后输入，发送操作串行。用本地消息记录核对结果；这不是对方送达回执。超时保留不确定状态，不能自动反复回车。
4. **CLI 契约：** 人工输出易读，机器模式 JSON/JSONL，日志走 stderr；ID 保持字符串；管道中断不丢待消费事件。
5. **TUI：** 后续只订阅同一 daemon，支持会话列表、历史、草稿和输入框，不另外再启动一个竞争扫描状态的循环。

可设计如下命令；这些尚未实现：

```bash
wereply doctor
wereply account status
wereply chats list
wereply contacts find "张三"
wereply messages list --chat wxid_example --limit 30
wereply listen --chat wxid_example --jsonl
wereply send --to wxid_example --text "你好"
wereply send --to wxid_example --text-file message.txt
wereply send status SEND_ID
wereply chat wxid_example
```

查询命令不应默认标记微信会话已读。发送必须明确指定接收目标，不使用“最近活跃联系人”作为脚本默认收件人。展示外来消息时需要转义终端控制字符，保留可读文本。

## 其他候选对照

| 项目 | 当前最有价值之处 | 采用限制 |
| --- | --- | --- |
| huohuoer/wechat-cli | Python 本地数据读取基础 | 缺发送、可靠事件流、daemon 和 TUI |
| agent-wechat v0.16.0 | 已有容器微信控制 CLI，包含收发路径 | 数据库密钥读取 + UI 发送，需锁定版本验证 |
| agent-wechat v1.0.0-beta.13 | 独立原生 daemon/CLI，发布于 2026-10-24 | 协议路线、profile 范围有限，要求 glibc 2.36+ |
| kevmo314/wechat-cli | 已有原生协议 CLI 与 TUI，最接近完整终端聊天形态 | 非常新，账号兼容与真实验证范围有限，历史只覆盖其归档 |
| gih10012/wechatcopilot | 官方客户端容器 + Unix socket 控制架构 | README 明确尚无真实客户端完成端到端及重启验收，消息读取仅可见区域 |
| leeguooooo/wechat-use | 查询、发送和 TUI 的产品形态参考 | 主要完整支持 macOS，不能直接当 Linux 后端 |
| iLink 类 CLI | 易于脚本化的 Bot 收发 | 不能直接等同于个人号既有好友会话接管 |

本机另已确认：x86_64、glibc 2.35。因此 `agent-wechat` 原生 beta 的 Linux 预构建产物不在其声明的本机运行基线内；不要为此直接替换系统 glibc，可以另用匹配运行时的隔离环境验证。

`kevmo314/wechat-cli` 本次快照为 [3162883](https://github.com/kevmo314/wechat-cli/tree/316288343500c1bfd741e17707f357a0778a2f0f)，创建于 2026-10-21，无 GitHub Release。当前 README 声称完成测试账号的桌面/平板登录、同步和一条平板文本发送的服务端接受；引用回复、实际改备注与置顶等仍有未完成的实测。其“history”是自身归档，不导入官方桌面数据库。仓库顶层没有发现 LICENSE，API 元数据也未提供明确许可，因此不能默认按常见开源许可复制代码。

`djasdh/wechat-bridge-linux` 虽然 README 声称完整文本收发，但进一步检查发现内置提取路径输出 `key_placeholder`、联系人/会话存在空实现，发送路径没有按目标会话完成选择与确认，因此没有列为可直接采用的基础。

来源：[agent-wechat 稳定 CLI](https://github.com/thisnick/agent-wechat/blob/v0.16.0/packages/cli/README.md)、[原生 beta 发布](https://github.com/thisnick/agent-wechat/releases/tag/v1.0.0-beta.13)、[原生运行基线](https://github.com/thisnick/agent-wechat/blob/v1.0.0-beta.13/packages/native-client-rust/README.md)、[kevmo314 CLI](https://github.com/kevmo314/wechat-cli/blob/316288343500c1bfd741e17707f357a0778a2f0f/README.md)、[wechatcopilot](https://github.com/gih10012/wechatcopilot)、[wechat-use](https://github.com/leeguooooo/wechat-use)、[桥接项目源码](https://github.com/djasdh/wechat-bridge-linux/blob/main/src/db_watcher.py)。

## 推荐的下一步

以你给出的仓库作为读取层参考，先对本机微信完成一次受控只读验证：密钥提取是否成功、会话与联系人是否对应、消息分片是否覆盖、连续多条消息是否完整。这一步验证通过，再开发 `listen` 和独立的文本 `send`；完整 TUI 与自动回复放在之后。

如果你要求不依赖任何官方微信进程，则转入原生协议客户端评估；那是另一种维护范围，不应在给现有查询库加命令时顺带混入。
