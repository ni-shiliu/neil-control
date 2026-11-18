"""
独立测试 DailyBriefingLoop，通过 LoopEngine 运行。
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

from loops.daily_briefing_loop import DailyBriefingLoop
from engine.engine import LoopEngine
from engine.memory import MemoryStore

if __name__ == "__main__":
    loop = DailyBriefingLoop()
    goal = {"id": "test_briefing_001", "raw": "测试每日简报"}

    print("=== 开始测试 DailyBriefingLoop（via LoopEngine）===\n")

    memory_store = MemoryStore()
    engine = LoopEngine(memory_store=memory_store)
    run_result = engine.run(loop, goal)

    print(f"\n=== 执行结果 ===\n{run_result.summary}")

    import json
    mem = memory_store.load(loop.name)
    print(f"\n=== memory/loops/daily_briefing_loop.json ===")
    print(json.dumps(mem, ensure_ascii=False, indent=2))
