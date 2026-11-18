import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import goals
import scheduler


def test_run_goal_now_refuses_paused_goal(tmp_path: Path) -> None:
    original_file = goals.GOALS_FILE
    goals.GOALS_FILE = tmp_path / "goals.json"
    try:
        goal = goals.add(
            raw="test paused goal",
            schedule="0 8 * * *",
            loop="daily_briefing_loop",
        )
        goals.pause(goal["id"])

        assert scheduler.run_goal_now(goal["id"]) is None
    finally:
        goals.GOALS_FILE = original_file
