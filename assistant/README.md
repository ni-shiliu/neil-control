# Neil Assistant

一个面向个人自动化的 `Loop Runtime`。

你输入目标，系统负责解析、调度、执行、记录、通知，并在运行中维护受控记忆。

架构设计、链路图、运行时边界请看 [ARCHITECTURE.md](./ARCHITECTURE.md)。

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

详细开发步骤请看 [ARCHITECTURE.md](./ARCHITECTURE.md)。

## 文档

- [README.md](./README.md)
- [ARCHITECTURE.md](./ARCHITECTURE.md)
