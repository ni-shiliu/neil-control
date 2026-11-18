import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.context import RunContext, ToolRegistry
from engine.memory import MemoryStore
from loops.daily_briefing_loop import DailyBriefingLoop
from loops.email_loop import EmailLoop


def test_goal_memory_preferences_deep_merge() -> None:
    base_dir = Path(tempfile.mkdtemp())
    memory = MemoryStore(base_dir=base_dir)

    memory.save_goal_memory(
        "goal_x",
        {"preferences": {"content": {"topic_bias": ["AI"]}}},
    )
    merged = memory.merge_save_goal_memory(
        "goal_x",
        {"preferences": {"format": {"title_lang": "en"}}},
    )

    assert merged["preferences"]["content"]["topic_bias"] == ["AI"]
    assert merged["preferences"]["format"]["title_lang"] == "en"


def test_daily_briefing_resolves_goal_preferences_over_loop_preferences() -> None:
    loop = DailyBriefingLoop()
    ctx = RunContext(
        goal={"id": "goal_x"},
        memory={
            "preferences": {
                "content": {"topic_bias": ["AI"]},
                "format": {"title_lang": "zh"},
            }
        },
        goal_memory={
            "preferences": {
                "format": {"title_lang": "en"},
                "content": {"include_english_phrase": False},
            }
        },
        recent_runs={},
        tools=ToolRegistry(),
    )

    resolved = loop._resolved_preferences(ctx)

    assert resolved["content"]["topic_bias"] == ["AI"]
    assert resolved["content"]["include_english_phrase"] is False
    assert resolved["format"]["title_lang"] == "en"


def test_daily_briefing_delivery_preference_can_skip_telegram() -> None:
    loop = DailyBriefingLoop()
    ctx = RunContext(
        goal={"id": "goal_x"},
        memory={"preferences": {"delivery": {"channels": ["none"]}}},
        goal_memory={},
        recent_runs={},
        tools=ToolRegistry(),
    )
    result = {
        "outputs": [
            {
                "output_type": "briefing_html",
                "name": "briefing.html",
                "content": "<html></html>",
            }
        ]
    }

    loop._queue_delivery_effect(result, "2026-06-23", ctx=ctx)

    assert len(ctx.effects) == 0


def test_email_loop_prefers_draft_when_preference_enabled() -> None:
    loop = EmailLoop()
    ctx = RunContext(
        goal={"id": "goal_x"},
        memory={"preferences": {"behavior": {"draft_first": True}}},
        goal_memory={},
        recent_runs={},
        tools=ToolRegistry(),
    )

    resolved = loop._resolved_preferences(ctx)

    assert resolved["behavior"]["draft_first"] is True
