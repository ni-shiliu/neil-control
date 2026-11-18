# Neil Assistant

一个面向个人自动化的 `Loop Runtime`。

你输入目标，系统负责解析、调度、执行、记录、通知，并在运行中维护受控记忆。

架构设计、链路图、运行时边界请看 [ARCHITECTURE.md](./architecture/RUNTIME.md)。

## 概览

核心能力：

- 自然语言创建目标
- 支持 `cron` / `goal` / `event` 三种触发模式
- 统一的 `LoopEngine` 执行链路
- 统一的 Effect 提交与幂等控制
- 统一的 `run_records` 运行记录
- 分层 memory，自动压缩
- CLI 历史、补全、自动建议

## 内置 Loops

| Loop | 用途 | 触发模式 | 主要工具 | 输出 / Effect |
|---|---|---|---|---|
| `daily_briefing_loop` | 生成并发送每日简报 | `cron` | `claude`, `telegram` | HTML output, `send_telegram_document` |
| `email_loop` | 处理未读邮件，自动回复或存草稿 | `cron`, `goal`, `event` | `imap`, `smtp`, `claude`, `telegram` | `mark_read`, `send_email_and_mark_read`, `save_draft_and_mark_read` |

## 内置工具 / 能力

Neil Assistant 将能力分成两类：

- **Loop Catalog**：业务闭环，可创建 goal、可调度、有 run record / memory / verify / report。
- **Tool Catalog**：原子能力，不负责业务闭环，可被 loop 通过 `required_tools` 依赖；部分低风险 tool 也可以被聊天按需调用。

启动和每轮聊天只注入轻量能力目录，不注入完整实现细节；当用户输入命中某个 tool 时，才按需加载该 tool 的详细 schema / executor。

Loop 可以通过 `required_tools` 声明运行时依赖，Engine 会在 `ctx.tools` 中按需注入。

| 工具 | 用途 | Loop 中的声明 | 运行时入口 |
|---|---|---|---|
| `browser` | 控制本机 Chrome：打开页面、读取页面状态、按文本点击元素、输入文本、等待页面变化 | `required_tools = ["browser"]` | `ctx.tools.browser` |
| `claude` | 调用 Claude 完成生成、分析、结构化判断 | `required_tools = ["claude"]` | `ctx.tools.claude` |
| `imap` | 读取邮箱未读邮件 | `required_tools = ["imap"]` | `ctx.tools.imap` |
| `smtp` | 发送邮件或保存草稿 | `required_tools = ["smtp"]` | `ctx.tools.smtp` |
| `telegram` | 发送 Telegram 消息或文档 | `required_tools = ["telegram"]` | `ctx.tools.telegram` |

浏览器能力当前基于 macOS Chrome + Apple Events，适合作为后续网页自动化 loop 的底层能力；业务 loop 只应依赖 `ctx.tools.browser`，不要直接依赖具体 Chrome 实现。

聊天侧按需暴露的浏览器 action tools：

- `browser_open_url`
- `browser_observe`
- `browser_click_text`
- `browser_type`
- `browser_wait`
- `browser_diagnostic`

## 快速开始

推荐使用 `python3.12`。

### 1. 创建虚拟环境

```bash
cd assistant
python3.12 -m venv .venv312
source .venv312/bin/activate
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
cp .env.example .env
```

常用环境变量：

| 变量 | 说明 |
|---|---|
| `ANTHROPIC_BASE_URL` | Claude 代理地址，可选 |
| `ANTHROPIC_AUTH_TOKEN` | 代理鉴权 token，可选 |
| `ANTHROPIC_API_KEY` | 官方 API key，可选 |
| `ANTHROPIC_MODEL` | 模型名 |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token |
| `TELEGRAM_CHAT_ID` | Telegram Chat ID |
| `EMAIL_USER` | 163 邮箱账号 |
| `EMAIL_PASS` | 163 IMAP/SMTP 授权码 |
| `EMAIL_AUTO_SEND` | 是否允许低风险邮件自动发送 |
| `EMAIL_AUTO_SEND_CONFIDENCE` | 自动发送置信度阈值 |
| `SMTP_LOCAL_HOSTNAME` | SMTP `EHLO` 主机名 |
| `QWEATHER_API_KEY` | 和风天气 key |
| `QWEATHER_API_HOST` | 和风天气 host |
| `WEATHER_CITY_ID` | 城市 ID |

说明：

- 代理模式通常使用 `ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN`
- 官方模式通常使用 `ANTHROPIC_API_KEY`
- 邮件链路依赖 163 IMAP/SMTP 已开启，且使用授权码

## 启动

```bash
python3.12 main.py
```

启动后会：

- 加载 `.env`
- 恢复 `goals.json` 中的 active goals
- 启动调度器
- 进入 CLI

## 常用命令

### 创建目标

直接输入自然语言：

```text
> 每天早上8点给我发每日简报
> 有新邮件时立刻处理
> 保持收件箱零未读
```

或使用 `add` 显式指定参数：

```text
> add 每天早上8点给我发每日简报
> add --dry-run 每天早上8点给我发每日简报
> add --retry-after 10 --max-retries 5 --backoff 2 --retry-max 120 每天早上10点帮我处理邮件
```

支持参数：

- `--dry-run`
- `--retry-after <分钟>`
- `--max-retries <次数>`
- `--backoff <倍率>`
- `--retry-max <分钟上限>`

### 查看和管理目标

```text
> list
> goal goal_xxxxxx
> pause goal_xxxxxx
> resume goal_xxxxxx
> delete goal_xxxxxx
```

### 立即执行

```text
> rerun goal_xxxxxx
> rerun --dry-run goal_xxxxxx
```

### 查看运行记录

```text
> runs
> runs 20
> run 4919d24d6483
```

### 查看记忆

```text
> loopmem daily_briefing_loop
> loopmem email_loop
> goalmem goal_xxxxxx
```

### 浏览器能力诊断

Neil Assistant 提供本机 Chrome 浏览器能力，用于后续 loop 打开页面、读取页面状态、点击元素和输入文本。

首次使用前，请在 Chrome 菜单中开启：

```text
Chrome -> View -> Developer -> Allow JavaScript from Apple Events
```

然后运行诊断：

```text
> browser doctor
```

也可以在命令行直接运行：

```bash
python3.12 -m harness.agents.tools.browser.diagnostics
```

如果诊断中 `javascript_from_apple_events` 为 `disabled`，浏览器能力只能读取 URL/title，不能稳定读取 DOM、点击元素或输入文本。

### 初始化可选文件

```text
> init
> init --force
> init loop my_loop
> init loop my_loop --with-doc
> init loops
```

会用到的文件：

- `assistant/RUNTIME.md`
- `assistant/loops/<loop_name>.py`
- `assistant/loops/<loop_name>.md`
- `assistant/tests/test_<loop_name>.py`

### 帮助与退出

```text
> help
> exit
> quit
```

## CLI 体验

CLI 基于 `prompt_toolkit`：

- 支持命令历史
- 支持历史建议
- 支持自动补全
- 默认关闭鼠标支持，减少中英文输入时的光标错位问题

补全项包括：

- 命令名
- `goal_id`
- `loop_name`
- 常用参数

历史文件：

- `assistant/.cli_history`

## 运行时文件

| 路径 | 说明 |
|---|---|
| `goals.json` | 目标定义 |
| `memory/loops/*.json` | loop 级 memory |
| `memory/goals/*.json` | goal 级 memory |
| `run_records/<goal_id>_<loop_name>_<YYYY-MM-DD>.json` | 每日运行记录 |
| `harness/tasks/task_store/tasks/<task_id>/` | 内部复杂 Task 的 Plan、Run、Artifact 与 checkpoint |
| `harness/memory/memory_store/conversations/` | 新 Harness 按租户、用户与日期分区的 append-only JSONL 会话记录；thread 仅作读取过滤 |
| `harness/memory/memory_store/user/<tenant>/<user>.md` | 新 Harness 的当前用户记忆；同 key 直接覆盖，不保留候选或版本文件 |
| `harness/memory/memory_store/project/<tenant>/<project>.md` | 新 Harness 的当前项目记忆；仅 project Agent + project_id 可用 |
| `harness/memory/memory_store/user/<tenant>/<user>.md` | 用户可直接编辑的用户记忆文档（key / kind / value） |
| `harness/config/personal_store/` | 新 Harness 的用户个人配置（仅由用户或宿主写入） |
| `effect_history.json` | effect 幂等历史 |
| `assistant.log` | 运行日志 |

排障优先顺序建议：

1. `assistant.log`
2. `run_records/*.json`
3. `memory/*.json`

## 测试

### 每日简报

```bash
python3.12 tests/test_briefing.py
```

### 邮件处理

```bash
python3.12 tests/test_email.py
python3.12 tests/test_email.py --max 1
python3.12 tests/test_email.py --full-auto
```

建议顺序：

1. 先跑 `--max 1`
2. 再在主程序里 `rerun --dry-run`
3. 最后再测真实发送

## 常见问题

### Telegram 没收到消息

检查：

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- 网络访问 Telegram 是否正常
- `run_records` 中的 `committed_effects`

### 邮件没有真正发出去

检查：

- `EMAIL_AUTO_SEND`
- `EMAIL_AUTO_SEND_CONFIDENCE`
- SMTP 授权码
- 163 SMTP / IMAP 是否开启
- `committed_effects` 是否失败

### `dry_run` 为什么还能看到 effect

这是正常行为。

`dry_run` 会：

- 生成 effect
- 记录到 `planned_effects`
- 在 `committed_effects` 中标记为 `dry_run`

但不会真正提交副作用。

## 开发新 Loop

最小骨架：

```python
from loops.base import BaseLoop


class MyLoop(BaseLoop):
    name = "my_loop"
    description = "这个 loop 是做什么的"
    required_tools = []

    def plan(self, goal, ctx=None):
        return {}

    def execute(self, context, ctx=None):
        return {}

    def verify(self, result):
        return True, ""

    def fix(self, result, issues, ctx=None):
        return result

    def report(self, result):
        return "完成"
```

复杂 Loop 再考虑补：

- `loops/<loop_name>.md`
- `extract_memory()`
- `extract_goal_memory()`

详细开发步骤请看 [ARCHITECTURE.md](./architecture/RUNTIME.md)。

## 文档

- [README.md](./README.md)
- [ARCHITECTURE.md](./architecture/RUNTIME.md)
