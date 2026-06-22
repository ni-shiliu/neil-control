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

    print(">>> plan 阶段：拉取未读邮件...")
    context = loop.plan(goal)
    emails = context.get("emails", [])
    print(f"  拉到 {len(emails)} 封")
    for i, em in enumerate(emails, 1):
        print(f"  [{i}] uid={em['uid']} from={em['sender']} subject={em['subject']!r}")
        snippet = em['body'][:60].replace('\n', ' ')
        print(f"      body: {snippet}...")

    if not emails:
        print("\n收件箱没有未读邮件，先发一封再来跑测试。")
        return

    print(f"\n>>> execute 阶段：Claude 生成回复 + Maker-Checker 验证...")
    result = loop.execute(context)

    print(f"\n=== 执行结果 ===")
    print(f"已发送: {len(result['sent'])}")
    for s in result["sent"]:
        print(f"  - {s['subject']}")
    print(f"存草稿: {len(result['drafted'])}")
    for d in result["drafted"]:
        print(f"  - {d['subject']}  原因: {d.get('reason', '-')}")
    print(f"跳过:   {len(result.get('skipped', []))}")
    for s in result.get("skipped", []):
        print(f"  - {s['subject']}  原因: {s.get('reason', '-')}")
    print(f"失败:   {len(result['failed'])}")
    for f in result["failed"]:
        print(f"  - {f['subject']}  错误: {f.get('error', '-')}")

    print(f"\n>>> verify 阶段...")
    ok, issues = loop.verify(result)
    print(f"  ok={ok} issues={issues!r}")

    if not ok:
        print(f"\n>>> fix 阶段...")
        fixed = loop.fix(result, issues)
        print(f"  fix 后 failed={len(fixed.get('failed', []))} drafted={len(fixed.get('drafted', []))}")

    print(f"\n>>> report 阶段...")
    summary = loop.report(result)
    print(f"  {summary}")


if __name__ == "__main__":
    main()
