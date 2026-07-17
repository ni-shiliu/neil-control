"""init 命令用的模板生成 + 写盘辅助。

从 main.py 原样抽出。纯函数（除 write_template 落盘外无副作用）。
"""

from __future__ import annotations

from pathlib import Path


def snake_to_camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_") if part)


def runtime_doc_template() -> str:
    return """# Runtime Preferences

## 目标

- 这是当前用户自己的可选运行偏好文件
- 只有在用户主动创建后，runtime 才会加载它
- 这里更适合写个人偏好、全局约束、长期习惯，而不是某次运行的临时状态

## 全局原则

- 运行时状态放在 JSON：goal memory、loop memory、run records
- 用户自定义的长期偏好可以放在 Markdown
- Loop 只声明 effect，不直接做不可回放的副作用
- Output 是产物，Effect 是动作，两者分开

## 可选文档层

### 用户级偏好（可选）

- 当前文件是用户自己的全局偏好入口
- 不同用户可以完全不同，也可以不存在

### Loop 级长期知识（可选）

- 只给复杂 loop 单独建 `loops/<loop_name>.md`
- 简单 loop 优先只依赖代码注释，必要时再补文档
- loop 文档记录该 loop 的目标、边界、长期策略、人工确认规则

### 运行时记忆

- `memory/loops/*.json`：跨 goal 的长期状态
- `memory/goals/*.json`：goal 级短期状态
- `run_records/*.json`：结构化运行记录

## 开发约束

- 如果用户需要全局偏好文件，可以执行 `init`
- 新增 loop 时优先执行 `init loop <loop_name>`
- 只有复杂 loop 才建议补单独的 `.md`
- 修改规则时，优先更新对应的 Markdown，再调整代码
"""


def loop_doc_template(loop_name: str, loop=None) -> str:
    description = getattr(loop, "description", "一句话描述这个 loop 负责什么")
    required_tools = getattr(loop, "required_tools", [])
    trigger_modes = getattr(loop, "supported_trigger_modes", ("cron", "goal"))
    tools_text = ", ".join(required_tools) if required_tools else "无"
    trigger_text = ", ".join(trigger_modes) if trigger_modes else "未声明"
    return f"""# {loop_name}

## 目标

- {description}

## 触发方式

- 支持模式：{trigger_text}

## 工具依赖

- {tools_text}

## 输入输出

- 输入：goal 配置、ctx.memory、ctx.goal_memory、ctx.recent_runs
- 输出：结构化 result，可选 `result.outputs`

## 长期规则

- 在这里记录这个 loop 的长期稳定策略
- 只写人工确认过的规则，不写一次性运行结果

## Memory 边界

- loop memory：跨 goal 共用的稳定经验
- goal memory：某个 goal 的短期状态
- recent runs：最近运行轨迹，只做辅助上下文

## 注意事项

- 副作用通过 effect 提交
- 不要把高频临时数据写进 Markdown
"""


def loop_py_template(loop_name: str) -> str:
    class_name = snake_to_camel(loop_name)
    return f'''from loops.base import BaseLoop


class {class_name}(BaseLoop):
    name = "{loop_name}"
    description = "一句话描述这个 loop 的职责"
    required_tools = []
    supported_trigger_modes = ("cron", "goal")

    def plan(self, goal, ctx=None):
        return {{}}

    def execute(self, context, ctx=None):
        return {{}}

    def verify(self, result):
        return True, ""

    def fix(self, result, issues, ctx=None):
        return result

    def report(self, result):
        return "完成"
'''


def loop_test_template(loop_name: str) -> str:
    class_name = snake_to_camel(loop_name)
    return f'''from engine.engine import LoopEngine
from engine.memory import MemoryStore
from loops.{loop_name} import {class_name}


def test_{loop_name}_runs():
    loop = {class_name}()
    engine = LoopEngine(memory_store=MemoryStore())
    goal = {{
        "id": "test_{loop_name}",
        "raw": "test {loop_name}",
        "loop": "{loop_name}",
        "trigger_mode": "cron",
        "schedule": "0 8 * * *",
        "dry_run": True,
    }}

    result = engine.run(loop, goal)

    assert result.record.loop_name == "{loop_name}"
'''


def write_template(path: Path, content: str, *, force: bool = False) -> str:
    existed = path.exists()
    if existed and not force:
        return "skipped"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return "updated" if existed else "created"
