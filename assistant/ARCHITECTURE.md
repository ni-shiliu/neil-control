# Assistant Architecture

本文档描述 `assistant/` 当前已经落地的运行时架构。

它不是历史草图，也不是未来方案，而是对“现在这套系统为什么这么设计、核心亮点是什么、代码里到底怎么实现”的一份说明。

如果你要做这些事，先读这份文档：

- 理解这个项目现在怎么跑
- 排查某个 Loop 为什么成功或失败
- 理解记忆系统为什么这样分层
- 新增一个 Loop
- 给现有 Loop 扩展工具、通知或触发方式

## 1. 这套 Runtime 在解决什么问题

这个项目的目标，不是把几个自动化脚本堆在一起，而是把“目标驱动的个人助手”抽成一套统一 runtime。

它想解决的是这几个典型问题：

- 用户说的是“目标”，不是“调用哪个函数”
- 同一类任务应该有统一的执行、记录、重试、通知方式
- 业务逻辑不应该和副作用提交、调度、记忆读写耦合在一起
- 每次运行都应该可回看、可排障、可解释
- 新增 Loop 时，尽量不改主框架

换句话说，这套系统的核心不是“自动化”，而是“把自动化运行时本身产品化”。

## 2. 架构亮点

这一节不是罗列模块，而是先讲最值得看的设计点。

### 2.1 Goal 驱动，而不是函数驱动

用户输入的是：

- “每天给我发简报”
- “保持收件箱零未读”
- “有新邮件时立刻处理”

系统先把这类自然语言解析成结构化 `goal`，再交给 runtime 去执行。

这意味着：

- CLI 层只负责把人类意图变成结构化目标
- 调度器只关心 goal 何时触发
- Engine 只关心 goal 的一次 run 怎么完成
- Loop 只关心业务逻辑

这是整个项目最重要的抽象边界。

### 2.2 Loop 只管业务，Engine 接管运行时

每个 Loop 不直接做这些事情：

- 不直接发通知
- 不直接做幂等判断
- 不直接做运行日志归档
- 不直接做 memory 落盘
- 不直接做调度

这些能力都由 `LoopEngine` 统一接管。

这样做的价值是：

- 各个 Loop 的代码更纯
- dry run 更容易做
- 运行记录更统一
- verify/fix 阶段不会把副作用逻辑写得到处都是
- 后续扩展新的 Loop 不容易失控

### 2.3 Effect 和 Output 分离

这套 runtime 明确把“运行产物”和“运行动作”拆开：

- `output`：这次生成了什么
- `effect`：这次想对外部世界做什么

例如每日简报：

- HTML 简报本身是 `output`
- 把 HTML 发到 Telegram 是 `effect`

这层分离很关键，因为它让系统同时获得：

- 可回看性：可以直接看当时生成的产物
- 可重放性：可以只重做 effect，不一定要重生成
- 可调试性：可以知道“产物生成成功，但发送失败”

### 2.4 记忆不是一层，而是分层系统

这个项目的记忆不是一个大字典，也不是一份长 Markdown，而是分成几层不同职责：

- `loop memory`：跨 goal 的长期状态
- `goal memory`：某个具体 goal 的短期状态
- `recent_runs`：从 run records 派生出来的短期运行轨迹
- `RUNTIME.md`：用户自定义的全局偏好，存在才加载
- `loops/<loop>.md`：复杂 loop 的长期规则文档，存在才加载

这套分层的核心思想是：

- 规则和状态分开
- 长期经验和短期上下文分开
- 事实日志和提炼后的记忆分开

### 2.5 Memory 有自动压缩，不允许无限增长

这是当前实现里非常值得注意的一点。

系统不是“把所有历史都塞进 memory”，而是明确限制 memory 的体积和长度：

- `goal memory` 上限：`8 KB`
- `loop memory` 上限：`16 KB`
- `recent_runs` 默认只注入最近 `5` 条

超过限制后，`MemoryStore` 会自动压缩，而不是继续堆数据。

这让 memory 更像“可复用的工作记忆”，而不是“无边界的历史仓库”。

### 2.6 运行记录是唯一事实源

`run_records/` 保存的是每次 run 的结构化事实。

memory 不负责归档全部历史，memory 只保留“下次执行还值得带着走的状态”。

这意味着：

- 想看发生过什么，去查 `run_records`
- 想看下一次执行要带什么状态，去看 `memory`
- 想看长期规则，去看 Markdown

这三个层次职责非常清楚。

## 3. 总体分层

```text
用户输入 / 定时触发 / 事件触发
            │
            ▼
         main.py
            │
            ▼
        goals.py
            │
            ▼
      scheduler.py
            │
            ▼
    engine.engine.LoopEngine
            │
   ┌────────┼──────────────┬─────────────┐
   ▼        ▼              ▼             ▼
memory   effects       run records    notifications
            │
            ▼
          Loop
            │
            ▼
     engine.tools.*
```

职责拆分：

- `main.py`：CLI 入口，自然语言解析、命令路由、交互体验
- `goals.py`：goal 持久化
- `scheduler.py`：触发、恢复 active goals、失败重试
- `engine/`：运行时内核
- `loops/`：业务逻辑实现
- `engine/tools/`：Claude、IMAP、SMTP、Telegram 等工具封装

## 4. 目录结构

```text
assistant/
├── main.py
├── goals.py
├── scheduler.py
├── notifier.py
├── claude_client.py
├── engine/
│   ├── __init__.py
│   ├── engine.py
│   ├── context.py
│   ├── memory.py
│   ├── records.py
│   ├── effects.py
│   └── tools/
│       ├── __init__.py
│       ├── claude_tool.py
│       ├── imap_tool.py
│       ├── smtp_tool.py
│       └── telegram_tool.py
├── loops/
│   ├── __init__.py
│   ├── base.py
│   ├── daily_briefing_loop.py
│   ├── email_loop.py
│   └── *.md                  # 复杂 loop 的可选规则文档
├── memory/
│   ├── loops/
│   └── goals/
├── run_records/
├── tests/
│   ├── test_briefing.py
│   └── test_email.py
├── goals.json
├── effect_history.json
├── assistant.log
└── RUNTIME.md                # 用户自定义的可选全局偏好文件
```

## 5. 核心概念

### 5.1 Goal

Goal 是“要持续执行的任务定义”。

典型字段：

```json
{
  "id": "goal_9c7dab",
  "raw": "每天给我发送简报",
  "loop": "daily_briefing_loop",
  "status": "active",
  "schedule": "0 8 * * *",
  "trigger_mode": "cron",
  "goal_condition": null,
  "dry_run": false,
  "retry_after_minutes": 30,
  "max_retries": 3,
  "retry_backoff_factor": 2,
  "retry_max_minutes": 240,
  "failure_count": 0,
  "last_run": null,
  "last_result": null,
  "last_run_meta": {}
}
```

关键含义：

- `raw`：用户原始描述
- `loop`：交给哪个 Loop 执行
- `trigger_mode`：`cron` / `goal` / `event`
- `dry_run`：是否只演练，不提交副作用
- `goal_condition`：goal 模式下的达成条件
- `last_run_meta`：结构化的上次运行信息

### 5.2 Loop

Loop 是“某一类任务的业务逻辑实现”。

例如：

- `daily_briefing_loop`
- `email_loop`

Loop 只回答业务问题：

- 这次需要收集什么上下文
- 怎么执行业务逻辑
- 什么算成功
- 验证失败后怎么补救
- 本次应该沉淀哪些记忆

Loop 不直接回答运行时问题：

- 怎么调度
- 怎么落盘
- 怎么幂等
- 怎么通知

### 5.3 Run

Run 是某个 Goal 的一次实际执行。

每次 run 都会生成一条结构化记录，落盘到 `run_records/`。

当前文件粒度是：

- 每天
- 每个 goal
- 每个 loop

文件名格式：

```text
run_records/<goal_id>_<loop_name>_<YYYY-MM-DD>.json
```

### 5.4 Effect

Effect 是“Loop 想做的副作用”。

Loop 不直接做，而是声明意图，由 Engine 统一提交。

当前已落地的 effect 类型：

- `mark_read`
- `send_email_and_mark_read`
- `save_draft_and_mark_read`
- `send_telegram_document`

### 5.5 Output

Output 是运行产出的结构化内容。

例如：

- 生成好的 briefing HTML
- 后续可扩展为 Markdown、截图、报表等

Output 直接写入该次 run 的 `result.outputs`，并随 `run_records` 一起落盘。

### 5.6 Memory

当前有两层运行时记忆：

- `loop memory`：跨 goal 的长期状态
- `goal memory`：某个具体 goal 的短期状态

另外还有两层可选 Markdown 上下文：

- `RUNTIME.md`：用户自定义的全局偏好
- `loops/<loop_name>.md`：复杂 loop 的长期规则文档

以及一层短期运行轨迹：

- `recent_runs.goal_recent_runs`
- `recent_runs.loop_recent_runs`

## 6. 触发模式

### 6.1 `cron`

固定时间执行。

适合：

- 每日简报
- 周报
- 定时巡检

### 6.2 `goal`

目标未达成时，由 Engine 决定是否重调度。

适合：

- 收件箱清零
- 重试直到成功
- 检查某个条件是否满足

Loop 通过两个钩子参与控制：

- `is_goal_met(result, memory)`
- `next_trigger(result)`

### 6.3 `event`

由外部事件触发，不依赖 cron。

当前实现里，邮件场景通过 IMAP IDLE 支持这一模式。

## 7. LoopEngine

`engine/engine.py` 里的 `LoopEngine` 是整个 runtime 的核心。

### 7.1 主流程

`LoopEngine.run(loop, goal)` 的核心步骤：

```text
1. 加载 loop memory / goal memory
2. 加载可选 Markdown 上下文（RUNTIME.md / loop_doc）
3. 注入 recent_runs
4. 构建 RunContext
5. 执行 plan -> execute
6. 提交 effect
7. verify -> fix（必要时多轮）
8. build_notifications 并发送
9. 保存 memory
10. 生成 RunRecord 并落盘
11. 如有需要，通知 scheduler 重调度
```

### 7.2 RunContext

`RunContext` 是 Loop 的运行时容器：

```python
class RunContext:
    goal: dict
    run_id: str
    memory: dict
    goal_memory: dict
    recent_runs: dict
    runtime_doc: str
    loop_doc: str
    tools: ToolRegistry
    effects: EffectCollector
```

Loop 不需要自己创建工具，也不应该直接写文件做运行时状态管理。

其中：

- `memory`：loop 级长期状态
- `goal_memory`：goal 级短期状态
- `recent_runs.goal_recent_runs`：当前 goal 最近 `5` 次运行
- `recent_runs.loop_recent_runs`：当前 loop 最近 `5` 次运行
- `runtime_doc`：用户自定义 `RUNTIME.md`，不存在时为空字符串
- `loop_doc`：当前 loop 的 Markdown 文档，存在时加载，不存在为空

这里刻意把“正式记忆”和“短期轨迹”分开：

- 正式记忆负责沉淀状态
- 短期轨迹负责补上下文

### 7.3 ToolRegistry

Engine 根据 `required_tools` 按需注入：

```python
class ToolRegistry:
    claude
    imap
    smtp
    telegram
```

例如 `email_loop`：

```python
required_tools = ["imap", "smtp", "claude", "telegram"]
```

### 7.4 verify / fix 机制

Engine 默认最多执行 3 轮：

- 第 1 次正常执行
- 最多 2 次修复重试

如果 `verify()` 失败：

1. 记录本轮问题
2. 调 `fix(result, issues, ctx)`
3. 再次 commit effects / after_effects
4. 再次 verify

这意味着：

- verify 可以把“结果质量”纳入 runtime
- fix 可以做结构化补救
- 补救路径也会完整留下运行痕迹

### 7.5 Run Record

每次运行都会生成 `RunRecord`，包含：

- 基本元数据
- `result`
- `phase_data`
- `planned_effects`
- `committed_effects`
- `memory_before / memory_after`
- `goal_memory_before / goal_memory_after`
- `notifications`
- `error`

其中：

- `result.outputs`、effect 提交结果、通知结果都会跟着本次 run 一起保存
- 这也是 CLI 里 `runs` 和 `run <id>` 的直接数据来源

## 8. Effect 机制

### 8.1 为什么要有 Effect

如果 Loop 在 `execute()` 里直接发邮件、直接发 Telegram、直接改 IMAP 状态，会有几个问题：

- dry run 难做
- 幂等不好做
- run record 不好记账
- verify/fix 阶段容易重复提交

因此 Loop 只声明 effect，Engine 统一提交。

### 8.2 当前 Effect 提交流程

```text
Loop execute/fix
    ↓
ctx.effects.add(...)
    ↓
Engine drain effects
    ↓
记录 planned_effects
    ↓
检查 dry_run
    ↓
检查 effect_history 幂等
    ↓
真正提交
    ↓
记录 committed_effects
```

### 8.3 幂等

每个 effect 都有 `idempotency_key`。

如果同一个 key 已经成功提交过：

- 本次不会重复提交
- 状态会记为 `duplicate_skipped`

### 8.4 Effect 提交状态

当前常见状态：

- `committed`
- `dry_run`
- `duplicate_skipped`
- `failed`

## 9. Output 机制

Output 用于保存“运行产物”，而不是“副作用”。

例如每日简报：

- 产物是 HTML
- 动作是发送 Telegram 文件

这样做的好处：

- 可以从 run record 里直接回看当时生成了什么
- 可以把“产物生成”和“产物发送”彻底解耦
- 后续更容易做回放、导出、二次处理

## 10. Memory 设计

这一节是本项目最有特色、也最值得仔细看的部分。

### 10.1 为什么不能只用一个大 memory

如果只用一个大 memory，会立刻遇到几个问题：

- 跨 goal 的经验和某个 goal 的状态会混在一起
- 历史运行和长期规则会混在一起
- 一次测试、一次异常、一次错误输入，很容易污染长期记忆
- memory 会越积越大，最后变成不可控上下文

所以当前设计明确拆成：

- 规则层
- 状态层
- 事实层
- 短期轨迹层

### 10.2 规则层、状态层、事实层分别是什么

规则层：

- `RUNTIME.md`
- `loops/<loop_name>.md`

特点：

- 可选
- 给人写
- 更偏长期规则和偏好
- 不适合高频结构化更新

状态层：

- `memory/loops/*.json`
- `memory/goals/*.json`

特点：

- 给程序读写
- 保存下次运行还要带着走的状态
- 允许自动裁剪和压缩

事实层：

- `run_records/*.json`

特点：

- 保存每次 run 的真实发生情况
- 用于回看、排障、追溯
- 不直接承担“长期记忆”职责

短期轨迹层：

- `recent_runs`

特点：

- 每次运行时从 run records 派生
- 默认只取最近 5 条
- 不单独持久化为新的 memory 文件

### 10.3 loop memory 存什么

路径：

```text
memory/loops/<loop_name>.json
```

它适合存：

- 跨 goal 共用的聚合统计
- 稳定 pattern
- 长期偏好
- 程序下次执行必须直接读取的少量字段

它不适合存：

- 全量历史
- 大段解释性文本
- 某次 run 的详细结果
- 可以从 `run_records` 反查出来的明细

当前例子：

- `daily_briefing_loop` 的 `totals`
- `email_loop` 的跨 goal 聚合统计

### 10.4 goal memory 存什么

路径：

```text
memory/goals/<goal_id>.json
```

它适合存：

- 某个 goal 的最近状态
- 最近处理结果的精简摘要
- 某个 goal 专属的局部规则
- 最近几次运行的状态性信息

它不适合存：

- 所有历史
- 所有产物
- 大量长文本
- 完整运行归档

当前例子：

- `daily_briefing_loop` 的最近短语、最近简报状态
- `email_loop` 的 `skip_patterns`、`last_counts`、`recent_activity`

### 10.5 recent_runs 是什么，为什么不直接放进 memory

`recent_runs` 是“短期运行轨迹”，不是长期状态。

当前会注入两类：

- `goal_recent_runs`
- `loop_recent_runs`

它们默认都只取最近 `5` 条。

为什么不直接放进 memory：

- 它本质上来自 `run_records`
- 它更像“本次执行前的短期参考”
- 它不应该不断污染长期状态文件

这是一个很重要的边界：

- `run_records` 保存事实
- `recent_runs` 提供短期上下文
- `memory` 保存提炼后的状态

### 10.6 Markdown 记忆是怎么加载的

运行时支持两类可选 Markdown：

- `RUNTIME.md`
- `loops/<loop_name>.md`

它们的规则都是：

- 文件存在：加载字符串内容到 `ctx`
- 文件不存在：返回空字符串

也就是说：

- 它们是可选能力
- 不是项目运行的硬依赖
- 更适合让用户或复杂 loop 补充人类规则

### 10.7 Memory 压缩的原理

这是当前实现里最重要的 memory 保护机制。

#### 目标

压缩机制的目标不是“把所有东西都删掉”，而是：

- 保住最有价值的状态
- 去掉可重建、可推导、可回看的冗余内容
- 防止 memory 越积越大

#### 当前阈值

- `goal memory`：`8 KB`
- `loop memory`：`16 KB`

#### 压缩入口

所有 memory 都不是直接原样写盘，而是统一经过 `MemoryStore`：

- `save_goal_memory()`
- `save_loop_memory()`

这意味着：

- 各个 Loop 可以专注于“返回什么状态”
- 最终落盘前一定会被统一收口

#### 压缩步骤

当前压缩逻辑大致分三步：

1. 轻压缩
2. 超限后二次收缩
3. 兜底最小保留集

#### 第一步：轻压缩

轻压缩会做这些事：

- 删除空值：`None`、空字符串、空列表、空字典
- 长文本裁剪
- 列表按字段类型裁剪
- `recent_english_phrases` 去重
- `skip_patterns` 按 sender 去重

这一步的目标是：

- 保留结构
- 去掉明显冗余

#### 第二步：超限后二次收缩

如果轻压缩后仍然超过大小上限，会进一步缩小：

对 `goal memory`：

- `recent_activity` 缩到最近 `3`
- `recent_briefings` 缩到最近 `3`
- `recent_runs` 缩到最近 `3`
- `recent_failures` 缩到最近 `3`
- `recent_english_phrases` 缩到最近 `3`
- `skip_patterns` 缩到最近 `20`

对 `loop memory`：

- `skip_patterns` 缩到最近 `20`
- `recent_activity` / `recent_briefings` 缩到最近 `3`

#### 第三步：最小保留集

如果二次收缩后仍然太大，就只保留最关键字段。

例如 `goal memory` 的最小保留集主要包括：

- `last_run_id`
- `last_status`
- `last_summary`
- `last_result_keys`
- `last_updated_at`
- `last_today`
- `last_output_name`
- `last_delivery_count`
- `last_english_phrase`
- `unread_count`
- `last_counts`
- `last_subjects`
- `skip_patterns`

这一步的意义是：

- 再极端的情况下，也不会把 memory 写成不可控大文件
- 同时仍然尽量保留下一次执行最需要的状态

### 10.8 为什么这套记忆设计是合理的

因为它同时满足了四件事：

1. 运行时可用
2. 长期可控
3. 历史可追溯
4. 上下文不过载

这是它最接近现代 agent runtime 的地方。

## 11. Scheduler

`scheduler.py` 负责：

- 启动 `BackgroundScheduler`
- 恢复 active goals
- 注册 cron/date/event 触发
- 调用 `LoopEngine.run()`
- 失败后按指数退避重试

### 11.1 重试机制

失败后使用：

- `retry_after_minutes`
- `retry_backoff_factor`
- `retry_max_minutes`
- `max_retries`

计算下一次重试时间。

### 11.2 event 模式

`event` 模式不走 cron，而是：

1. 由 `_register_event()` 启动 IMAP IDLE
2. 收到事件后触发 `_run_goal(goal_id)`

## 12. CLI 和运行时关系

### 12.1 自然语言建 goal

`main.py` 会：

1. 调 Claude 解析用户输入
2. 判断是否是任务目标
3. 生成结构化 goal
4. 保存到 `goals.json`
5. 交给 `scheduler.add_goal()`

### 12.2 管理命令

当前主要命令：

- `init`
- `init loop <name>`
- `init loops`
- `list`
- `goal <id>`
- `pause <id>`
- `resume <id>`
- `delete <id>`
- `runs [N]`
- `run <run_id>`
- `loopmem <loop_name>`
- `goalmem <goal_id>`
- `rerun [--dry-run] <goal_id>`

### 12.3 CLI 体验层

当前 CLI 已经不是纯 `input()`：

- 使用 `prompt_toolkit`
- 支持历史命令
- 支持自动建议
- 支持基础补全
- 默认关闭鼠标支持，减少中英文输入时的光标错位问题

这部分不影响 runtime 核心逻辑，但对实际可用性很重要。

## 13. 现有 Loop 实现

### 13.1 `daily_briefing_loop`

职责：

- 聚合多个外部信息源
- 调 Claude 生成 HTML 简报
- 生成 briefing output
- 声明 `send_telegram_document` effect

特点：

- 通知主要通过 Telegram 文件发送
- 结果适合通过 output 回看
- 会利用短期运行上下文避免每日英文重复

### 13.2 `email_loop`

职责：

- 拉取未读邮件
- 自动跳过通知类邮件
- 判断是否需要回复
- 生成回复
- 验证回复质量
- 自动发送或存草稿
- 对失败邮件生成兜底草稿

特点：

- `execute()` 和 `fix()` 都会声明 effect
- `after_effects()` 会刷新未读数
- `extract_goal_memory()` 会沉淀目标专属的 `skip_patterns`
- 支持 `cron` / `goal` / `event`

## 14. 新增 Loop 的开发步骤

### Step 1：先定义边界

先回答四个问题：

1. 这个 Loop 解决什么问题
2. 它需要哪些工具
3. 它有哪些副作用
4. 它的长期状态和短期状态分别是什么

建议先写出：

- `name`
- `description`
- `required_tools`
- `supported_trigger_modes`

### Step 2：初始化骨架

推荐先执行：

```text
init loop my_loop
```

这会生成：

- `loops/my_loop.py`
- `tests/test_my_loop.py`

如果这是复杂 loop，需要文档，再执行：

```text
init loop my_loop --with-doc
```

这时会额外生成：

- `loops/my_loop.md`

如果是给已有复杂 loop 批量补说明：

```text
init loops
```

### Step 3：优先用工具注入

推荐：

```python
ctx.tools.claude
ctx.tools.imap
ctx.tools.smtp
ctx.tools.telegram
```

不推荐：

- 在 Loop 里直接 new `requests.Session()`
- 直接在业务里硬编码 SMTP/IMAP 连接
- 直接在 Loop 里读写运行时状态文件

### Step 4：副作用走 Effect

推荐：

```python
ctx.effects.add(
    "some_effect",
    {"foo": "bar"},
    {"success_bucket": "done", "success_item": {...}},
    idempotency_key="xxx",
)
```

不推荐：

- 在 `execute()` 里直接发通知
- 在 `execute()` 里直接发邮件
- 在 `fix()` 里重复做不可回放副作用

### Step 5：有产物就写进 `result.outputs`

例如：

```python
result["outputs"] = [{
    "output_type": "html",
    "name": "briefing_html",
    "content": html,
    "meta": {"source": "daily_briefing"},
}]
```

### Step 6：设计 Verify / Fix

至少想清楚：

- 什么算失败
- 能不能自动补救
- 补救后要不要重新提交 effect

建议：

- `verify()` 只判断
- `fix()` 做可解释、可回放的补救

### Step 7：设计 Memory

至少想清楚：

- 哪些经验值得跨 run 复用
- 哪些状态只属于某个 goal
- 哪些内容其实应该留在 `run_records`

然后实现：

- `extract_memory()`
- `extract_goal_memory()`

### Step 8：补独立测试脚本

推荐通过 `LoopEngine` 跑，而不是直接调 `loop.run()`：

```python
from engine.engine import LoopEngine
from engine.memory import MemoryStore

engine = LoopEngine(memory_store=MemoryStore())
run_result = engine.run(loop, goal)
```

这样能一起验证：

- ctx 注入
- effect 提交
- output 落盘
- memory 落盘
- run record 生成

### Step 9：做三类验证

至少验证：

1. 正常路径
2. dry run 路径
3. 失败补救路径

如果 Loop 有外部副作用，再加：

4. 幂等路径
5. 重复运行路径

## 15. 开发约定

### 15.1 尽量返回结构化结果

推荐：

```python
{
  "sent": [],
  "drafted": [],
  "failed": [],
}
```

不推荐只返回一句话字符串。

### 15.2 `report()` 只做摘要

不要在 `report()` 里发通知、写文件、改数据库。

### 15.3 谨慎沉淀长期记忆

写入 `loop memory` 的字段一定要尽量稳，避免把测试噪音、异常输入、一次性数据写进长期状态。

### 15.4 优先把历史留给 run records

只要某段信息是“用于追溯历史”，优先放 `run_records`，不要轻易塞进 memory。

### 15.5 尽量给 effect 设计稳定 idempotency_key

这会直接影响重复运行时是否安全。

## 16. 后续演进方向

基于当前实现，后续可以继续升级：

- 更细粒度的 Tool 抽象
- 更多通知渠道
- 更强的 event trigger
- 更完善的 run record 检索/回放工具
- 更丰富的 output 浏览能力
- 更清晰的 semi_auto / full_auto 行为分层

如果要看更偏设计草图的版本，可以再参考 `RUNTIME_V2.md`。
