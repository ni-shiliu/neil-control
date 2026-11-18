import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.engine import LoopEngine
from engine.memory import MemoryStore
from engine.records import RunRecorder
from loops.base import BaseLoop


class _NativeLoop(BaseLoop):
    name = "native_test_loop"
    description = "native loop runtime test"

    def plan(self, goal: dict, ctx=None) -> dict:
        return {"value": goal.get("value", 1)}

    def execute(self, context: dict, ctx=None) -> dict:
        return {"value": context["value"], "executed": True}

    def verify(self, result: dict) -> tuple[bool, str]:
        return True, ""

    def fix(self, result: dict, issues: str, ctx=None) -> dict:
        return result

    def report(self, result: dict) -> str:
        return f"value={result['value']}"


class _FailingLoop(BaseLoop):
    name = "failing_test_loop"
    description = "failing loop runtime test"

    def plan(self, goal: dict, ctx=None) -> dict:
        return {"value": 1}

    def execute(self, context: dict, ctx=None) -> dict:
        raise RuntimeError("boom")

    def verify(self, result: dict) -> tuple[bool, str]:
        return True, ""

    def fix(self, result: dict, issues: str, ctx=None) -> dict:
        return result

    def report(self, result: dict) -> str:
        return "never"


def test_loop_engine_runs_loop_runtime(tmp_path: Path) -> None:
    memory_store = MemoryStore(base_dir=tmp_path / "memory")
    recorder = RunRecorder(base_dir=tmp_path / "run_records")
    engine = LoopEngine(memory_store=memory_store, recorder=recorder)

    run_result = engine.run(_NativeLoop(), {"id": "goal_native", "value": 7, "dry_run": False})

    assert run_result.success is True
    assert run_result.result["executed"] is True
    assert run_result.record.phase_data["runtime"]["type"] == "loop_template"
    assert run_result.record.phase_data["execute"]["mode"] == "loop_template"


def test_loop_engine_does_not_settle_business_state_on_failure(tmp_path: Path) -> None:
    memory_store = MemoryStore(base_dir=tmp_path / "memory")
    recorder = RunRecorder(base_dir=tmp_path / "run_records")
    engine = LoopEngine(memory_store=memory_store, recorder=recorder)

    run_result = engine.run(_FailingLoop(), {"id": "goal_fail", "dry_run": False})

    assert run_result.success is False
    assert run_result.record.notifications == []
    assert run_result.record.memory_before == {}
    assert run_result.record.memory_after == {}
    assert run_result.record.goal_memory_before == {}
    assert run_result.record.goal_memory_after == {}
