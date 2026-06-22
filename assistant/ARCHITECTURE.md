# LoopEngine 架构设计

## 背景

当前 `assistant/` 实现的是 **Harness 层**，不是真正的 Loop Engineering：

| 问题 | 现状 | 目标 |
|---|---|---|
| 触发方式 | 死 cron，到点就跑 | 三种模式：cron / goal / event |
| 记忆 | `last_result` 只存字符串 | 结构化记忆，跨 run 学习 |
| 工具耦合 | IMAP/Claude 直接 hardcode 在 Loop 里 | 依赖注入，Loop 只声明需要什么 |
| 通知 | 散落在各 Loop 的 `report()` 里 | Engine 统一发，Loop 只返回数据 |
| 子 Agent | Maker-Checker 同一上下文串行调用 | 真正隔离的两个独立调用 |

---

## 新文件结构

```
assistant/
├── engine/
│   ├── __init__.py
│   ├── engine.py            # LoopEngine 主类
│   ├── context.py           # RunContext + ToolRegistry
│   ├── memory.py            # MemoryStore（持久记忆读写）
│   ├── trigger.py           # TriggerMode + 自适应调度
│   └── tools/
│       ├── __init__.py
│       ├── claude_tool.py   # ClaudeTool
│       ├── imap_tool.py     # IMAPTool（含 IMAP IDLE）
│       ├── smtp_tool.py     # SMTPTool
│       └── telegram_tool.py # TelegramTool
├── loops/
│   ├── base.py              # BaseLoop（加 ctx 参数 + 新钩子）
│   ├── email_loop.py        # 只保留业务逻辑
│   ├── daily_briefing_loop.py
│   └── __init__.py
├── memory/                  # 运行时自动生成
│   └── email_loop.json      # 各 Loop 的持久记忆
├── scheduler.py             # 改为动态调度，支持 reschedule
├── goals.py                 # 扩展 trigger_mode 等字段
└── main.py                  # 基本不变
```

---

## 核心概念

### 三种触发模式

| 模式 | 触发方式 | 适用场景 | 示例 |
|---|---|---|---|
| `cron` | 固定时间，APScheduler | 周期性任务 | 每天8点发简报 |
| `goal` | 目标达成才停，Engine 动态 reschedule | 需要反复确认完成 | 收件箱清零 |
| `event` | 外部事件驱动，IMAP IDLE | 实时响应 | 新邮件到达立刻处理 |

---

### goals.json 新增字段

```json
{
  "id": "goal_xxx",
  "raw": "用户原始描述",
  "loop": "email_loop",
  "status": "active",
  "trigger_mode": "goal",
  "schedule": "0 10 * * *",
  "goal_condition": "收件箱零未读",
  "retry_after_minutes": 30,
  "last_run_meta": {
    "unread_count": 0,
    "sent": 2,
    "skipped": 5
  }
}
```

---

### RunContext — 工具注入容器

```python
class RunContext:
    goal: dict           # 当前 goal 配置
    memory: dict         # 从 MemoryStore 加载的历史记忆
    tools: ToolRegistry  # 工具访问入口

class ToolRegistry:
    claude: ClaudeTool
    imap: IMAPTool
    smtp: SMTPTool
    telegram: TelegramTool
```

Loop 通过 `ctx.tools.imap.fetch_unseen()` 操作，不再直接调 `imaplib`。

---

### BaseLoop 新增钩子

```python
class BaseLoop(ABC):
    name: str
    description: str
    required_tools: list[str] = []   # 声明依赖

    # 原有方法签名加 ctx 参数（默认 None，向后兼容）
    def plan(self, goal, ctx=None) -> dict: ...
    def execute(self, context, ctx=None) -> dict: ...
    def fix(self, result, issues, ctx=None) -> dict: ...
    def report(self, result) -> str: ...  # 只返回字符串，不发通知

    # 新增：目标模式
    def is_goal_met(self, result, memory) -> bool:
        return True  # 默认：cron 模式，每次运行后视为完成

    def next_trigger(self, result) -> timedelta | None:
        return None  # 默认：不自动重触发

    # 新增：记忆沉淀
    def extract_memory(self, result, old_memory) -> dict:
        return old_memory  # 默认：不更新记忆
```

---

### LoopEngine 执行流程

```
LoopEngine.run(loop, goal)
  │
  ├─ 1. 加载记忆       memory = MemoryStore.load(loop.name)
  ├─ 2. 注入工具       ctx = RunContext(goal, memory, tools)
  ├─ 3. 执行阶段       plan → execute → verify → fix → report
  ├─ 4. 统一通知       Notifier.send(loop.report(result))
  ├─ 5. 沉淀记忆       MemoryStore.save(loop.extract_memory(result))
  └─ 6. 目标判断       is_goal_met? → 否 → reschedule(next_trigger())
```

---

### EmailLoop 重构后

```python
class EmailLoop(BaseLoop):
    name = "email_loop"
    required_tools = ["imap", "smtp", "claude"]

    def is_goal_met(self, result, memory):
        return result.get("unread_count", 0) == 0

    def next_trigger(self, result):
        if result.get("unread_count", 0) > 0:
            return timedelta(minutes=30)   # 没清完，30分钟后重试
        return None                        # 清完了，等 IMAP IDLE 事件

    def plan(self, goal, ctx):
        # 历史 pattern 从记忆读，不再硬编码
        known_patterns = ctx.memory.get("skip_patterns", [])
        emails = ctx.tools.imap.fetch_unseen()
        return {"emails": emails, "known_patterns": known_patterns}

    def extract_memory(self, result, old_memory):
        patterns = old_memory.get("skip_patterns", [])
        for s in result.get("skipped", []):
            patterns.append({"sender": s["sender"], "reason": s["reason"]})
        return {
            **old_memory,
            "skip_patterns": patterns[-100:],
            "unread_count": result.get("unread_count", 0),
        }
```

---

### IMAP IDLE（event 模式）

```python
class IMAPTool:
    def idle_listen(self, callback: Callable) -> None:
        """后台线程监听新邮件，到达时调用 callback(goal)"""
        # 使用 IMAP IDLE 命令，163 支持
        # 新邮件 → callback → LoopEngine.run()
```

`scheduler.py` 注册 `trigger_mode=event` 的 goal 时，启动 IDLE 监听线程而非 cron job。

---

## 实施步骤

### Step 1 — engine/ 框架层
- `engine/memory.py` MemoryStore
- `engine/context.py` RunContext + ToolRegistry（空壳）
- `engine/engine.py` LoopEngine.run()，通知从 report 剥离
- 改 `scheduler.py`：`_run_goal` 调 LoopEngine，加 `reschedule()`
- 改 `goals.py`：加新字段，旧数据 fallback 到 `trigger_mode=cron`
- 改 `BaseLoop`：加默认钩子，ctx 参数向后兼容

### Step 2 — Tool 层
- `engine/tools/claude_tool.py`
- `engine/tools/imap_tool.py`（含 IDLE）
- `engine/tools/smtp_tool.py`
- `engine/tools/telegram_tool.py`

### Step 3 — 迁移 EmailLoop
- 方法签名加 ctx
- 工具调用替换为 ctx.tools.*
- report 只返回字符串
- 实现 is_goal_met / next_trigger / extract_memory

### Step 4 — 迁移 DailyBriefingLoop
- 同 Step 3，较简单

### Step 5 — IMAP IDLE（event 模式）
- IMAPTool.idle_listen()
- scheduler 注册 event goal

---

## 向后兼容策略

- `plan/execute/fix` 的 `ctx` 参数默认 `None`，旧 Loop 不需要改
- `goals.json` 新字段有默认值，旧 goal 自动 fallback 到 `trigger_mode=cron`
- `report()` 过渡期：Engine 检测 Loop 是否已迁移，未迁移的继续用旧 report 发通知

---

## 验证

1. `python3 tests/test_email.py` 结果与重构前一致
2. 设置 `trigger_mode=goal`，处理后有未读 → 30分钟后自动重触发
3. 设置 `trigger_mode=event`，发一封邮件 → IDLE 立刻触发处理
4. `memory/email_loop.json` 记录跳过的发件人 pattern
5. Telegram 收到汇总通知格式不变
