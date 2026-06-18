# Neil Assistant — 个人助手框架

## 是什么

一个真正的个人助手，而不是定制化脚本。

你通过命令行自然语言告诉它目标，它自己记住、调度、执行、通知结果。
邮件处理和每日简报只是它能做的事情，后续可以扩展任意 Loop。

```
> 每天早上8点给我发每日简报
✓ 已添加目标 goal_3a1b2c：每天 08:00 执行每日简报
  调度: 0 8 * * * | Loop: daily_briefing_loop

> 每天早上10点帮我处理邮件
✓ 已添加目标 goal_9f2e4d：每天 10:00 执行邮件处理
  调度: 0 10 * * * | Loop: email_loop

> list
ID              状态     Loop                  原始描述
----------------------------------------------------------------------
goal_3a1b2c     ✓ active daily_briefing_loop   每天早上8点给我发每日简报
goal_9f2e4d     ✓ active email_loop            每天早上10点帮我处理邮件
```

---

## 架构

```
assistant/
├── main.py                    # 命令行入口：自然语言对话 + 管理命令
├── goals.py                   # 目标管理：增删改查，持久化到 goals.json
├── scheduler.py               # 调度引擎：APScheduler，到时触发对应 Loop
├── notifier.py                # 通知：macOS 系统通知 + Telegram Bot
├── goals.json                 # 持久化目标（运行时自动生成）
├── assistant.log              # 运行日志（运行时自动生成）
├── tests/
│   └── test_briefing.py       # DailyBriefingLoop 独立测试
└── loops/
    ├── base.py                # Loop 基类：规划→执行→验证→修复→汇报
    ├── email_loop.py          # 邮件 Loop：读信 + Claude 回复 + 发送
    └── daily_briefing_loop.py # 每日简报 Loop：天气+热点+HTML → Telegram
```

### Loop 六阶段模型

每个 Loop 都遵循统一流程，框架统一驱动：

```
规划(plan) → 执行(execute) → 验证(verify) → 修复(fix) → 汇报(report)
```

子类只需实现这五个方法，调度和通知由 `BaseLoop` + `scheduler` 统一处理。

### Loop vs Skill

| | Loop | Skill（`../skills/`）|
|---|---|---|
| 职责 | 流程框架 + 业务逻辑 | 可复用的纯业务能力 |
| 当前状态 | 两个 Loop 已实现 | 预留，暂为空 |

---

## 当前支持的 Loop

| Loop | 触发示例 | 做什么 |
|---|---|---|
| `daily_briefing_loop` | 每天早上8点给我发每日简报 | 并发抓取天气+头条+GitHub+HN+36kr，Claude 动态生成 HTML，Telegram 发送 |
| `email_loop` | 每天10点帮我处理邮件 | IMAP 读取未读邮件，Claude Maker-Checker 生成回复，SMTP 发送或存草稿 |

### 每日简报内容

- 今日重点（可点击跳转原文）
- 每日一句（根据当天基调，励志/幽默/神评）
- 今日天气（含穿衣建议）
- 今日头条热榜
- GitHub 今日推荐（附推荐理由）
- Hacker News 精选
- 36kr 快讯
- 今日一问
- 每日英文（英文原句 / 中文翻译 / 使用场景 / 例句）

HTML 风格每天动态变化，由 Claude 根据当天内容基调决定。

---

## 快速上手

### 1. 安装依赖

```bash
cd assistant
pip install -r requirements.txt
```

### 2. 配置密钥

```bash
cp .env.example .env
```

编辑 `.env`，填入以下配置：

| 变量 | 说明 | 获取方式 |
|---|---|---|
| `EMAIL_USER` | 163 邮箱账号 | — |
| `EMAIL_PASS` | 163 授权码 | 163网页版 → 设置 → IMAP → 生成授权码 |
| `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN` | 官方 API Key 或公司代理 Token | 二选一 |
| `ANTHROPIC_BASE_URL` | 公司代理地址 | 走代理时填，走官方 API 留空 |
| `TELEGRAM_BOT_TOKEN` | Bot Token | Telegram 找 @BotFather 发 /newbot |
| `TELEGRAM_CHAT_ID` | 你的 Chat ID | Telegram 找 @userinfobot 发任意消息 |
| `QWEATHER_API_KEY` | 和风天气 Key | dev.qweather.com 免费注册 |
| `QWEATHER_API_HOST` | 免费版 API Host | 项目管理页面的 API Host 字段 |
| `WEATHER_CITY_ID` | 城市 ID | 默认 101010100（北京），[查询列表](https://github.com/qwd/LocationList) |

### 3. 启动

```bash
python main.py
```

### 4. 添加目标（自然语言）

```
> 每天早上8点给我发每日简报
> 每天早上10点帮我处理邮件
```

### 5. 管理目标

```
> list                    查看所有目标
> pause goal_3a1b2c       暂停
> resume goal_3a1b2c      恢复
> delete goal_3a1b2c      删除
> help                    帮助
> exit                    退出
```

### 6. 单独测试每日简报

```bash
python tests/test_briefing.py
```

---

## 扩展新 Loop

Loop 框架是自发现的——**只需在 `loops/` 下新建一个文件，框架启动时自动注册**。

`loops/` 下新建 `my_loop.py`：

```python
from loops.base import BaseLoop

class MyLoop(BaseLoop):
    name = "my_loop"
    description = "描述这个 loop 能做什么场景（供 Claude 解析自然语言时挑选）"

    def plan(self, goal: dict) -> dict: ...
    def execute(self, context: dict) -> dict: ...
    def verify(self, result: dict) -> tuple[bool, str]: ...
    def fix(self, result: dict, issues: str) -> dict: ...
    def report(self, result: dict) -> str: ...
```

`main.py` 和 `scheduler.py` **不需要改**。`loops/__init__.py` 的 `discover()` 会扫到新类，把它注册到 Claude 的能力清单和调度器实例表中。`description` 是必填项，启动时会校验。

---

## 注意事项

- `goals.json` 随进程重启自动恢复，调度器启动时重新注册所有 active 目标
- 163 邮箱对 SMTP 有频率限制，`max_emails` 默认 10 封/次
- 每日简报 HTML 由 Telegram `sendDocument` 发送，手机端点开即可查看
- 和风天气免费版每天 1000 次调用，足够使用
