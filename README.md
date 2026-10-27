# wereply

通过本机网页配置微信自动回复，微信数据读取与桌面发送由独立的 `wecli` 提供。

## 安装

当前版本：**0.1.0**，初始版本标签：`v0.1.0`。支持 Linux，Python 3.10+。

```bash
git clone git@github.com:Staaaaaaaaar/wereply.git
cd wereply
git checkout v0.1.0
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
wereply --version
```

如系统缺少 `venv` 或 `pip`，请先通过发行版包管理器安装对应组件。
开发时可用 `python -m pip install -e .`。运行时没有第三方 Python 依赖；
安装构建需要 setuptools。`wecli` 是单独安装和初始化的外部程序，
不会随 wereply 自动安装。请先在同一桌面用户会话中确认其 `doctor --format json`
能返回匹配账号与读取能力，再启动本服务。不要使用 sudo 启动 wereply。

## 启动

Python 3.10+，运行时不需要第三方 Python 包。在能够使用 `wecli` 的桌面用户会话中运行：

```bash
python3 -m wereply.app --wecli /绝对路径/wecli
```

打开 http://127.0.0.1:8765 。也可以指定解释器与配置：

```bash
python3 -m wereply.app \
  --wecli '/绝对路径/python -m wecli' \
  --wecli-config /绝对路径/config.json
```

在网页按昵称、备注或内部 ID 搜索联系人，点击结果后自动填写对象，再设置固定回复或 LLM 提示词。
联系人可修改的微信号（alias）不等于内部会话 ID；保存时会通过 wecli 再次精确核对 ID。
等待历史基线完成后开启自动回复。搜索最多返回 30 项，结果较多时请缩小搜索范围。
同一联系人再次保存会更新规则；取消勾选“启用此规则”并保存可关闭该规则，也可以直接删除。
修改、关闭或删除规则会取消该对象等待中的旧任务；关闭或删除已有无效规则不要求联系人查询成功。

默认状态库：`~/.local/share/wereply/state.db`。可以使用 `--db` 指定独立位置。
一个状态库只允许一个服务实例。Ctrl+C 停止，每次重新启动默认暂停实际发送。
停止时等待正在执行的子进程退出；停止请求后不会再开始新的发送。
`--wecli` 也支持只填写 Python 解释器路径，程序会自动追加 `-m wecli`。
独立 Conda 环境可以直接指定已有环境的解释器，无需在 wereply 环境重复安装 wecli。

## 命令参数

安装后使用 `wereply`，或在源码目录使用 `python3 -m wereply.app`，参数相同。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--version` | — | 显示版本后退出 |
| `--help` | — | 显示命令帮助 |
| `--port` | `8765` | 本地端口，范围 1–65535 |
| `--db` | `~/.local/share/wereply/state.db` | SQLite 状态库路径 |
| `--wecli` | `wecli` | 外部命令或 Python 解释器路径 |
| `--wecli-config` | 未指定 | 传给 wecli 的配置文件路径 |
| `--interval` | `3` | 读到消息末尾后的轮询秒数，至少 1 秒 |

路径包含空格时，对整个参数加引号；如果 `--wecli` 内含多个命令参数，
其中带空格的路径也应保留引号，例如 `--wecli '"/path with spaces/python" -m wecli'`。

## LLM 回复

支持返回 `choices[0].message.content` 的 chat-completions HTTP 接口。
环境变量指定完整接口地址、模型及可选密钥，然后在网页选择“LLM 生成”，填写提示词：

```bash
export WEREPLY_LLM_URL='https://你的服务地址/v1/chat/completions'
export WEREPLY_LLM_MODEL='你的模型名称'
read -rs WEREPLY_LLM_KEY
export WEREPLY_LLM_KEY
python3 -m wereply.app --wecli /绝对路径/wecli
```

仅接受 HTTPS 或 localhost/127.0.0.1 的 HTTP 地址。LLM 会收到此会话截至触发消息的最近
12 条本地文本记录，每条最多 4000 字符；发送前请确认所选服务适合接收这些聊天内容。
生成超时 45 秒，输出最多 4000 字符。生成失败标记为失败，不自动发送兜底文本或重试。
密钥仅从进程环境读取，不存入网页或状态库。

## 消息和发送语义

- 首次运行按每页最多 5000 条扫描历史，只建立基线，不创建回复任务；扫描期间到达的消息也可能纳入基线。
- 网页显示已扫描条数；有下一页时立即继续，读到末尾后才等待轮询间隔（默认 3 秒）。重启从已保存游标续读。
- 当前每页仍启动独立 wecli 进程并建立解密快照，大型消息库可能耗时较长；尚未使用当前位置游标快速初始化。
- 消息、任务、账号绑定和游标在同一 SQLite 事务内保存，重放消息不重复创建任务。
- 暂停期间继续接收记录，不创建回复任务；暂停取消等待中的任务。
- 仅回复启用规则对应的私聊纯文本。本人、发送者未知、群聊及非文本消息被跳过。
- 每条符合规则的消息创建一个任务；当前没有连续消息合并、冷却时间或人工接管逻辑。
- 生成与发送使用单个 worker 顺序执行；生成完成、进入发送前，在写事务中再次检查开关、规则及任务状态。
- LLM 生成期间暂停、删除或修改规则会阻止旧回复发送。已经进入发送阶段的操作无法撤销。
- 每次发送前持久化 UUID 与正文，然后调用 `wecli send --operation-id ...`。
- 结果未知或服务在发送中退出时，不自动重发；网页“核对发送结果”调用 `send-status --reconcile`，不发送。
- “本地已确认”不代表服务器送达或对方已读。已经进入发送阶段的操作无法通过暂停撤销。
- 启动时取消之前等待中的任务，避免重启后突然回复旧消息。

## 运行状态

网页显示环境检查、历史扫描、消息读取、LLM 生成与发送阶段，以及等待和结果未知的任务数。
历史扫描显示已读取条数和最近一页耗时；不预估未知的历史总量。
最近非空批次按重复、历史基线、暂停、本人/群聊/非文本、未匹配规则及已创建任务分类计数。
这组计数反映最近一批消息，不是某位联系人的全部处理历史。
网页刷新不会清除保存错误提示，也不会重复发起尚未结束的状态查询。

## 环境和运行边界

需要本文对应的 `wecli` 服务集成契约：逐条 `new-messages`、结构化 `history`、
UUID 发送、`send-status --reconcile` 与结构化 `doctor`。
运行账号匹配且数据读取可用后才采集；发送还要求 `send_available=true`。
账号变化时停止处理，切勿直接清空游标来规避错误。切换账号应使用独立状态库。

网页仅监听 IPv4 回环地址，使用来源检查、Host 检查与请求令牌，不提供远程访问功能。
聊天正文、LLM 提示词与发送结果保存在本地状态库；尚无自动清理策略，长期运行需关注磁盘占用。

当前发送继承 `wecli` 的 Linux X11、桌面解锁及窗口可操作要求。
本项目自动化测试使用模拟适配器，不代表真实微信发送验收通过。
LLM 真实服务和真实桌面发送仍需在你的环境单独验收。

## 验证

```bash
python3 -m unittest discover -s tests -v
```


## 故障排查与备份

- 找不到命令或返回非 JSON：检查 `--wecli` 指向的解释器和模块，在同一会话手动运行该命令的 `doctor --format json`。
- 读取失败或账号不匹配：检查 wecli 初始化、微信登录和配置；切换账号使用新的 `--db`，保留旧库。
- 无法发送：确认桌面为 X11、已解锁且微信窗口可操作，并检查网页环境状态。
- 网页打不开：使用启动日志中的 `http://127.0.0.1:端口`，检查端口是否被占用；同一状态库不可重复启动。
- 任务结果未知：在网页核对结果，避免直接手动重发造成重复回复。

备份前用 Ctrl+C 停止服务并等待退出，再复制状态库；恢复时停服替换，
使用相同微信账号。状态库包含聊天内容和规则，应保存在私人目录，勿提交到 Git。
卸载可运行 `python -m pip uninstall wereply`；本地状态库不会随卸载删除。

## 项目结构与开发

- `wereply/app.py`：命令入口、本地 HTTP 服务与 worker 生命周期。
- `wereply/core.py`：wecli 适配、SQLite 状态、消息处理和回复任务。
- `wereply/static/`：控制台网页资源，随 Python 包分发。
- `tests/`：模拟 wecli 的核心、适配器和本地 HTTP 集成测试。
- `docs/`：早期可行性与上游源码调研，内容为当时快照；当前使用以本 README 为准。

测试需在允许本地 socket 的环境运行，不依赖真实微信或 LLM。
构建分发包可安装 `build` 后运行 `python -m build`。

版本变更记录见 [CHANGELOG.md](CHANGELOG.md)。
