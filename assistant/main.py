"""
命令行入口。

自然语言输入 → Claude 解析为 goal（schedule + loop 类型）→ 存储 + 注册调度
管理命令：list / pause <id> / resume <id> / delete <id> / help
"""

import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
from claude_client import get_client, get_model

import goals as goals_mod
import scheduler
import notifier
from loops import discover

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "assistant.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

def _call_claude(prompt: str, max_tokens: int = 512) -> str:
    msg = get_client().messages.create(
        model=get_model(),
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _parse_goal(user_input: str) -> dict | None:
    """让 Claude 把自然语言解析成结构化 goal，失败返回 None。"""
    loops = discover()
    loop_lines = "\n".join(f"- {n}：{l.description}" for n, l in loops.items())
    prompt = f"""用户说："{user_input}"

请判断这是否是一个需要执行的任务目标，并解析成结构化数据。

支持的 loop 类型（从下列中选最匹配的一个，或判断为非任务目标）：
{loop_lines}

触发模式说明：
- cron  : 固定时间触发，需要 schedule（如"每天早上8点"）
- goal  : 目标驱动，持续运行直到目标达成（如"保持收件箱零未读"），可搭配初始 schedule
- event : 事件驱动，实时响应（如"有新邮件时立刻处理"），不需要 schedule

如果是任务目标，输出 JSON：
{{
  "is_goal": true,
  "trigger_mode": "cron 或 goal 或 event",
  "schedule": "5字段cron（仅 cron/goal 模式需要，event 填 null）。例：每天10点='0 10 * * *'",
  "goal_condition": "goal 模式时填达成条件描述，其他填 null",
  "loop": "loop类型",
  "summary": "一句话描述这个目标"
}}

如果不是任务目标（比如闲聊、问问题），输出：
{{"is_goal": false, "reply": "你的回复"}}

只输出 JSON，不要其他内容。"""

    msg = get_client().messages.create(
        model=get_model(),
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw
    return json.loads(raw)


def _cmd_list() -> None:
    goals = goals_mod.list_all()
    if not goals:
        print("暂无目标。")
        return
    print(f"\n{'ID':<15} {'状态':<8} {'Loop':<20} {'Cron':<15} {'原始描述'}")
    print("-" * 90)
    for g in goals:
        status_icon = "✓" if g["status"] == "active" else "⏸"
        print(f"{g['id']:<15} {status_icon} {g['status']:<6} {g['loop']:<20} {g['schedule']:<15} {g['raw']}")
        if g["last_run"]:
            print(f"  └─ 上次执行: {g['last_run']}  结果: {g['last_result']}")
    print()


def _cmd_pause(goal_id: str) -> None:
    if goals_mod.pause(goal_id):
        scheduler.pause_goal(goal_id)
        print(f"已暂停 {goal_id}")
    else:
        print(f"未找到目标 {goal_id}")


def _cmd_resume(goal_id: str) -> None:
    if goals_mod.resume(goal_id):
        scheduler.resume_goal(goal_id)
        print(f"已恢复 {goal_id}")
    else:
        print(f"未找到目标 {goal_id}")


def _cmd_delete(goal_id: str) -> None:
    confirm = input(f"确认删除 {goal_id}？(y/N) ").strip().lower()
    if confirm != "y":
        print("已取消")
        return
    if goals_mod.delete(goal_id):
        scheduler.remove_goal(goal_id)
        print(f"已删除 {goal_id}")
    else:
        print(f"未找到目标 {goal_id}")


def _cmd_help() -> None:
    print("""
命令列表：
  list                  查看所有目标
  pause <goal_id>       暂停目标
  resume <goal_id>      恢复目标
  delete <goal_id>      删除目标
  help                  显示帮助
  exit / quit           退出

直接输入自然语言来添加新目标，例如：
  每天早上10点帮我处理邮件
  每天11点半提醒我该去接水了
""")


def _handle_input(user_input: str) -> None:
    parts = user_input.strip().split()
    if not parts:
        return

    cmd = parts[0].lower()

    if cmd == "list":
        _cmd_list()
    elif cmd == "pause" and len(parts) >= 2:
        _cmd_pause(parts[1])
    elif cmd == "resume" and len(parts) >= 2:
        _cmd_resume(parts[1])
    elif cmd == "delete" and len(parts) >= 2:
        _cmd_delete(parts[1])
    elif cmd in ("help", "?"):
        _cmd_help()
    elif cmd in ("exit", "quit"):
        print("再见！")
        scheduler.stop()
        sys.exit(0)
    else:
        # 自然语言，交给 Claude 解析
        print("解析中...")
        try:
            result = _parse_goal(user_input)
        except Exception as e:
            print(f"解析失败: {e}")
            return

        if not result.get("is_goal"):
            print(f"助手：{result.get('reply', '我不太理解，请重新描述。')}")
            return

        loop = result.get("loop", "")
        if loop not in discover():
            print(f"暂不支持的 loop 类型：{loop}，当前支持：{', '.join(discover())}")
            return

        trigger_mode = result.get("trigger_mode", "cron")
        goal = goals_mod.add(
            raw=user_input,
            schedule=result.get("schedule"),
            loop=loop,
            trigger_mode=trigger_mode,
            goal_condition=result.get("goal_condition"),
        )
        scheduler.add_goal(goal)
        mode_label = {"cron": "定时", "goal": "目标驱动", "event": "事件驱动"}.get(trigger_mode, trigger_mode)
        print(f"✓ 已添加目标 {goal['id']}：{result['summary']}")
        print(f"  触发模式: {mode_label} | Loop: {goal['loop']}")
        print(f"  调度: {goal['schedule']} | Loop: {goal['loop']}")


def main() -> None:
    print("Neil 助手已启动，输入 help 查看帮助。")
    scheduler.start()

    try:
        while True:
            try:
                user_input = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见！")
                break
            if user_input:
                _handle_input(user_input)
    finally:
        scheduler.stop()


if __name__ == "__main__":
    main()
