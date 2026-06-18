"""
调度引擎。基于 APScheduler BackgroundScheduler，与主循环并行运行。
每个 active goal 注册一个 cron job，触发时执行对应 Loop。
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import goals as goals_mod
import notifier
from loops import discover

log = logging.getLogger(__name__)

_scheduler = BackgroundScheduler(timezone="Asia/Shanghai")


def _run_goal(goal_id: str) -> None:
    goal = goals_mod.get(goal_id)
    if not goal or goal["status"] != "active":
        return

    loop_name = goal.get("loop", "")
    loop = discover().get(loop_name)
    if not loop:
        log.error(f"未知 loop 类型: {loop_name}")
        return

    log.info(f"触发目标 {goal_id}: {goal['raw']}")
    try:
        result = loop.run(goal)
        goals_mod.update_last_run(goal_id, result)
        notifier.notify("助手完成任务", result)
    except Exception as e:
        msg = f"执行失败: {e}"
        log.error(f"goal={goal_id} {msg}")
        notifier.notify("助手任务失败", msg)


def start() -> None:
    _scheduler.start()
    # 加载所有 active goal
    for goal in goals_mod.list_all():
        if goal["status"] == "active":
            _register(goal)
    log.info("调度器已启动")


def _register(goal: dict) -> None:
    cron = goal["schedule"].strip()
    parts = cron.split()
    if len(parts) == 6:
        # 6 段格式是 (秒 分 时 日 月 周)，丢第一段得到标准 5 段
        log.warning(f"cron 表达式多了一段（秒），自动丢弃: {cron} -> {' '.join(parts[1:])}")
        parts = parts[1:]
    if len(parts) != 5:
        log.error(f"无效 cron 表达式: {cron}")
        return
    minute, hour, day, month, day_of_week = parts
    trigger = CronTrigger(
        minute=minute, hour=hour, day=day,
        month=month, day_of_week=day_of_week,
        timezone="Asia/Shanghai",
    )
    _scheduler.add_job(
        _run_goal,
        trigger=trigger,
        args=[goal["id"]],
        id=goal["id"],
        replace_existing=True,
    )
    log.info(f"已注册目标 {goal['id']}: {cron}")


def add_goal(goal: dict) -> None:
    _register(goal)


def remove_goal(goal_id: str) -> None:
    try:
        _scheduler.remove_job(goal_id)
    except Exception:
        pass


def pause_goal(goal_id: str) -> None:
    try:
        _scheduler.pause_job(goal_id)
    except Exception:
        pass


def resume_goal(goal_id: str) -> None:
    try:
        _scheduler.resume_job(goal_id)
    except Exception:
        pass


def stop() -> None:
    _scheduler.shutdown(wait=False)
