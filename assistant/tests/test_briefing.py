"""
独立测试 DailyBriefingLoop，不启动完整助手。
"""

import logging
import sys
from pathlib import Path

# 让 Python 找到 assistant/ 下的模块
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

from loops.daily_briefing_loop import DailyBriefingLoop

if __name__ == "__main__":
    loop = DailyBriefingLoop()
    goal = {"id": "test_001", "raw": "测试每日简报"}

    print("=== 开始测试 DailyBriefingLoop ===\n")
    result = loop.run(goal)
    print(f"\n=== 执行结果 ===\n{result}")
