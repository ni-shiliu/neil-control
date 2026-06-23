# Neil Assistant

一个围绕 `Loop` 组织的个人助手 runtime。

它不是“脚本集合”，而是一套持续运行的目标执行系统：你给它目标，它负责解析、调度、执行、记录、通知，并在运行中沉淀可控的记忆。

当前已经落地的两个典型场景：

- `daily_briefing_loop`：生成每日简报并通过 Telegram 发送
- `email_loop`：读取未读邮件，判断是否需要回复，自动回复或存草稿

如果你想看实现原理、记忆设计、Effect / Output 分层，请直接看 [ARCHITECTURE.md](/Users/nihao/myProjects/neil-control/assistant/ARCHITECTURE.md)。

## 1. 这套 Assistant 能做什么

这套 runtime 的核心能力：

- 用自然语言创建目标
- 支持 `cron` / `goal` / `event` 三种触发模式
- 所有 Loop 统一走 `LoopEngine`
- 所有副作用统一走 Effect 提交
- 所有运行都有结构化 `run_records`
- 有分层记忆，但记忆体积受控，会自动压缩
- CLI 可直接使用，支持历史记录、自动建议、基础补全

一句话理解：

- `goal` 是“要做什么”
- `loop` 是“这一类事情怎么做”
- `engine` 是“运行时怎么把这件事安全执行完”

## 2. 快速开始

推荐使用 `python3.12`。

### 2.1 创建虚拟环境

```bash
cd assistant
python3.12 -m venv .venv312
source .venv312/bin/activate
```

### 2.2 安装依赖

```bash
pip install -r requirements.txt
```

### 2.3 配置环境变量

```bash
cp .env.example .env
```

常用环境变量：

| 变量 | 说明 |
|---|---|
| `EMAIL_USER` | 163 邮箱账号 |
| `EMAIL_PASS` | 163 SMTP/IMAP 授权码，不是登录密码 |
| `EMAIL_AUTO_SEND` | `true/false`，是否允许低风险邮件自动发送 |
| `EMAIL_AUTO_SEND_CONFIDENCE` | 自动发送置信度阈值 |
| `ANTHROPIC_BASE_URL` | Claude 公司代理地址，可选 |
| `ANTHROPIC_AUTH_TOKEN` | 代理鉴权 token，可选 |
| `ANTHROPIC_API_KEY` | 官方 API key，可选 |
| `ANTHROPIC_MODEL` | 使用的模型名 |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token |
| `TELEGRAM_CHAT_ID` | Telegram Chat ID |
| `QWEATHER_API_KEY` | 和风天气 key |
| `QWEATHER_API_HOST` | 和风天气 host |
| `WEATHER_CITY_ID` | 城市 ID |
| `SMTP_LOCAL_HOSTNAME` | SMTP `EHLO` 主机名，默认 `localhost` |

说明：

- 走公司代理时，通常用 `ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN`
- 走官方 API 时，通常用 `ANTHROPIC_API_KEY`
- 邮件测试前先确认 163 IMAP/SMTP 已开启，且用的是授权码

## 3. 启动

```bash
python3.12 main.py
```

启动后会：

- 加载 `.env`
- 恢复 `goals.json` 中的 active 目标
- 启动 APScheduler
- 进入 CLI 交互

启动示例：

```text
Neil 助手已启动，输入 help 查看帮助。
```

## 4. 日常使用

这一节是最常用的。

### 4.1 直接输入自然语言创建目标

例如：

```text
> 每天早上8点给我发每日简报
> 每天早上10点帮我处理邮件
> 有新邮件时立刻处理
> 保持收件箱零未读
```

系统会自动解析出：

- 用哪个 loop
- 触发模式是 `cron` / `goal` / `event`
- 是否需要 schedule
- 是否是 dry run
- 重试参数

### 4.2 用 `add` 显式指定参数

当你想覆盖默认重试策略或强制 dry run 时，使用 `add`：

```text
> add 每天早上8点给我发每日简报
> add --dry-run 每天早上8点给我发每日简报
> add --retry-after 10 --max-retries 5 --backoff 2 --retry-max 120 每天早上10点帮我处理邮件
```

支持的参数：

- `--dry-run`
- `--retry-after <分钟>`
- `--max-retries <次数>`
- `--backoff <倍率>`
- `--retry-max <分钟上限>`

### 4.3 查看目标

查看所有目标：

```text
> list
```

查看单个目标详情：

```text
> goal goal_xxxxxx
```

常用管理命令：

```text
> pause goal_xxxxxx
> resume goal_xxxxxx
> delete goal_xxxxxx
```

### 4.4 立即手动执行某个目标

```text
> rerun goal_xxxxxx
> rerun --dry-run goal_xxxxxx
```

适合：

- 新增目标后立刻验证
- 排障时手动重跑
- 用 `--dry-run` 验证 effect 是否正确生成但不真正提交

### 4.5 查看运行记录

查看最近运行：

```text
> runs
> runs 20
```

查看某次运行详情：

```text
> run 4919d24d6483
```

运行详情里会看到：

- `goal_id`
- `loop_name`
- `status`
- `summary`
- `outputs`
- `planned_effects`
- `committed_effects`
- `notifications`
- `error`

### 4.6 查看记忆

查看某个 loop 的长期状态：

```text
> loopmem email_loop
> loopmem daily_briefing_loop
```

查看某个 goal 的短期状态：

```text
> goalmem goal_xxxxxx
```

适合用来排查：

- 为什么某个 goal 最近行为变了
- 为什么某类邮件被跳过
- 某个 loop 的长期状态是否被异常污染

### 4.7 初始化可选文档和 Loop 骨架

初始化用户自己的可选全局偏好文件：

```text
> init
> init --force
```

这会创建：

- [`assistant/RUNTIME.md`](/Users/nihao/myProjects/neil-control/assistant/RUNTIME.md)

这个文件不是必须存在，但如果存在，runtime 会在执行时加载它，并注入到 `ctx.runtime_doc`。

初始化单个 Loop 骨架：

```text
> init loop my_loop
> init loop my_loop --with-doc
> init loop my_loop --force
```

会生成：

- `loops/my_loop.py`
- `tests/test_my_loop.py`
- 可选的 `loops/my_loop.md`

给已有复杂 loop 批量补说明：

```text
> init loops
```

### 4.8 帮助和退出

```text
> help
> exit
> quit
```

## 5. CLI 体验

当前 CLI 已经接入 `prompt_toolkit`，不再只是原生 `input()`：

- 支持命令历史
- 支持历史建议
- 支持基础自动补全
- 默认关闭鼠标支持，减少中英文输入时的光标错位问题

补全内容包括：

- 命令名
- `goal_id`
- `loop_name`
- 常见参数，例如 `--dry-run`

历史文件默认保存在：

- [`assistant/.cli_history`](/Users/nihao/myProjects/neil-control/assistant/.cli_history)

## 6. 运行时产物

### 6.1 `goals.json`

保存所有目标定义。

### 6.2 `memory/loops/*.json`

Loop 级运行时状态。

适合保存：

- 跨 goal 的长期聚合统计
- 稳定 pattern
- 程序下次运行要直接读取的轻量状态

### 6.3 `memory/goals/*.json`

Goal 级短期状态。

适合保存：

- 某个具体 goal 最近一次状态
- 最近几次处理结果的精简信息
- 某个 goal 专属的局部规则

### 6.4 `run_records/<goal_id>_<loop_name>_<YYYY-MM-DD>.json`

最重要的排障数据。

每个文件表示“某一天某个 goal 在某个 loop 下的运行日志”，文件里包含该 goal 当天的 `runs` 列表。

每条 run 会记录：

- `run_id`、`goal_id`、`loop_name`
- `phase_data`
- `result.outputs`
- `planned_effects`
- `committed_effects`
- `notifications`
- `memory_before / memory_after`
- `goal_memory_before / goal_memory_after`
- `error`

### 6.5 `effect_history.json`

已成功提交的 effect 幂等记录，用于避免重复提交。

### 6.6 `assistant.log`

完整日志输出。

遇到这些问题时，优先看这里：

- 网络
- 鉴权
- SMTP
- IMAP
- Telegram

## 7. 记忆系统怎么理解

README 只讲使用视角，详细原理请看 [ARCHITECTURE.md](/Users/nihao/myProjects/neil-control/assistant/ARCHITECTURE.md)。这里先给一个最实用的心智模型。

### 7.1 规则、状态、事实是分开的

- `RUNTIME.md` / `loops/*.md`
  - 可选规则层
- `memory/*.json`
  - 运行时状态层
- `run_records/*.json`
  - 历史事实层

不要把这三层混着理解。

### 7.2 `recent_runs` 不是新的存储层

每次运行前，Engine 会额外注入几组轻量上下文：

- `recent_runs.goal_recent_runs`：当前 goal 最近 `5` 次运行
- `recent_runs.loop_recent_runs`：当前 loop 最近 `5` 次运行
- `runtime_doc`：用户自定义 `RUNTIME.md` 内容，不存在时为空
- `loop_doc`：当前 loop 的 `.md` 内容，不存在时为空

这部分的作用是：

- 去重
- 补短期上下文
- 避免重复生成

但它不替代正式 memory。

### 7.3 Memory 会自动压缩

系统不会让 memory 无限制增长。

当前默认限制：

- `goal memory`：`8 KB`
- `loop memory`：`16 KB`

超过限制后，`MemoryStore` 会自动做：

- 列表裁剪
- 去重
- 长文本截断
- 超限后的二次收缩
- 必要时保留最小字段集

所以如果你看到 memory 比你返回的原始结构更短，这是正常现象。

## 8. 当前内置 Loop

### 8.1 `daily_briefing_loop`

做的事情：

- 抓取天气
- 抓取头条 / GitHub / Hacker News / 36kr
- 调 Claude 生成日报 HTML
- 通过 Telegram 发送文件

特点：

- 会生成 output
- 会声明 `send_telegram_document` effect
- 会利用最近运行上下文避免每日英文重复

### 8.2 `email_loop`

做的事情：

- IMAP 拉取未读邮件
- 判断是否需要回复
- 低风险时自动发送，否则存草稿
- 对失败邮件做兜底草稿

特点：

- 使用 `imap` / `smtp` / `claude` / `telegram`
- 会沉淀 `skip_patterns`
- 支持 `mark_read` / `send_email_and_mark_read` / `save_draft_and_mark_read`
- 支持 `cron` / `goal` / `event`

## 9. 测试方式

### 9.1 测试每日简报

```bash
python3.12 tests/test_briefing.py
```

说明：

- 通过 `LoopEngine` 直接运行 `DailyBriefingLoop`
- 会打印 summary
- 会落 `memory` 和 `run_records`

### 9.2 测试邮件 Loop

```bash
python3.12 tests/test_email.py
python3.12 tests/test_email.py --max 1
python3.12 tests/test_email.py --full-auto
```

说明：

- 默认是 `semi_auto`
- `--full-auto` 会开启更激进的自动处理，真实环境下请谨慎
- 测试前最好先往邮箱发一封明确需要回复的未读邮件

建议顺序：

1. 先用 `--max 1` 验证基础链路
2. 再在主程序里用 `rerun --dry-run`
3. 最后再测试真实发送

## 10. 常见问题

### 10.1 Telegram 没收到消息

优先检查：

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- 网络是否可访问 `api.telegram.org`
- 该次 run 的 `committed_effects` 和 `notifications` 是否成功

### 10.2 邮件没有真正发出去

优先检查：

- `EMAIL_AUTO_SEND` 是否为 `true`
- `EMAIL_AUTO_SEND_CONFIDENCE` 是否过高
- SMTP 授权码是否正确
- 163 SMTP 是否开启
- `run_records` 里 `committed_effects` 的状态是不是 `failed`

### 10.3 `dry_run` 为什么仍然看到 effect

这是正常的。

`dry_run` 会：

- 生成 effect
- 记录到 `planned_effects`
- 在 `committed_effects` 里标记为 `dry_run`

但不会真的提交副作用。

### 10.4 为什么某些邮件被自动跳过

先看：

```text
> loopmem email_loop
> goalmem goal_xxxxxx
```

通常原因有两类：

- 命中静态白名单
- 命中该 goal 沉淀出来的 `skip_patterns`

### 10.5 为什么 memory 看起来比原始结果短

因为 memory 会在落盘前自动压缩。

系统会优先保留：

- 最近状态
- 稳定 pattern
- 必要聚合字段

而不会无限保存所有历史。

## 11. 开发新 Loop

如果你要加新 Loop，详细步骤看 [ARCHITECTURE.md](/Users/nihao/myProjects/neil-control/assistant/ARCHITECTURE.md)。

最小骨架如下：

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

如果这是一个简单 loop，通常只需要：

- `.py`
- 测试

如果这是一个复杂 loop，再考虑补：

- `.md` 文档
- 更完整的 `extract_memory()`
- 更完整的 `extract_goal_memory()`

## 12. 相关文档

- [README.md](/Users/nihao/myProjects/neil-control/assistant/README.md)：使用说明
- [ARCHITECTURE.md](/Users/nihao/myProjects/neil-control/assistant/ARCHITECTURE.md)：架构设计与实现原理
