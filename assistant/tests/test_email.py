"""
独立测试 EmailLoop，不启动完整助手。

跑前请确认：
  1. .env 已配置 EMAIL_USER / EMAIL_PASS / ANTHROPIC_*
  2. 收件箱有至少 1 封未读邮件
  3. 第一次跑建议 agent_mode='semi_auto'（不会真发出去，只存草稿）

用法：
  python3 tests/test_email.py                # semi_auto，存草稿
  python3 tests/test_email.py --full-auto    # full_auto，可能真发邮件
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("test_email")

from loops.email_loop import EmailLoop, BODY_LIMIT_FOR_CLAUDE, AUTO_SEND_CONFIDENCE


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--full-auto", action="store_true",
                   help="full_auto 模式：低风险邮件可能真发出去，慎用")
    p.add_argument("--max", type=int, default=3, help="最多处理几封（默认3）")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    mode = "full_auto" if args.full_auto else "semi_auto"

    print(f"=== EmailLoop 测试 ===")
    print(f"模式: {mode}")
    print(f"阈值: confidence >= {AUTO_SEND_CONFIDENCE} 才会自动发送")
    print(f"上下文截断: 前 {BODY_LIMIT_FOR_CLAUDE} 字符")
    print(f"上限: {args.max} 封\n")

    loop = EmailLoop(agent_mode=mode, max_emails=args.max)
    goal = {"id": "test_email_001", "raw": "测试邮件处理"}

    print(">>> 通过 LoopEngine 运行（记忆 + 工具注入）...")
    from engine.engine import LoopEngine
    from engine.memory import MemoryStore

    memory_store = MemoryStore()
    engine = LoopEngine(memory_store=memory_store)
    run_result = engine.run(loop, goal)

    result = run_result.result
    print(f"\n=== 执行结果 ===")
    print(f"已发送: {len(result.get('sent', []))}")
    for s in result.get("sent", []):
        print(f"  - {s['subject']}")
    print(f"存草稿: {len(result.get('drafted', []))}")
    for d in result.get("drafted", []):
        print(f"  - {d['subject']}  原因: {d.get('reason', '-')}")
    print(f"跳过:   {len(result.get('skipped', []))}")
    for s in result.get("skipped", []):
        print(f"  - {s['subject']}  原因: {s.get('reason', '-')}")
    print(f"失败:   {len(result.get('failed', []))}")
    for f in result.get("failed", []):
        print(f"  - {f['subject']}  错误: {f.get('error', '-')}")

    print(f"\n>>> summary: {run_result.summary}")

    # 验证记忆是否写入
    mem = memory_store.load(loop.name)
    print(f"\n>>> memory/email_loop.json:")
    import json
    print(json.dumps(mem, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
