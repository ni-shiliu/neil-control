"""
调度引擎。基于 APScheduler BackgroundScheduler，与主循环并行运行。

支持三种触发模式：
  - cron  : 固定时间触发（现有行为）
  - goal  : 首次由 cron 触发，Engine 根据 is_goal_met 动态 reschedule
  - event : IMAP IDLE 监听新邮件（由 IMAPTool 注册，不走 APScheduler）
"""

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

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
        from engine.engine import get_engine
        engine = get_engine()
        engine._scheduler = _scheduler_proxy()
        run_result = engine.run(loop, goal)
        goals_mod.update_last_run(goal_id, run_result.summary, meta=run_result.result)
        notifier.notify("助手完成任务", run_result.summary)
    except Exception as e:
        msg = f"执行失败: {e}"
        log.error(f"goal={goal_id} {msg}")
        notifier.notify("助手任务失败", msg)


class _scheduler_proxy:
    """给 LoopEngine 注入的调度代理，避免循环依赖。"""
    def reschedule(self, goal_id: str, delay: timedelta) -> None:
        reschedule(goal_id, delay)


def start() -> None:
    _scheduler.start()
    for goal in goals_mod.list_all():
        if goal["status"] == "active":
            _register(goal)
    log.info("调度器已启动")


def _register(goal: dict) -> None:
    mode = goal.get("trigger_mode", "cron")

    if mode == "event":
        # event 模式由 IMAPTool.idle_listen() 注册，不走 APScheduler
        log.info(f"goal {goal['id']} 为 event 模式，跳过 cron 注册")
        _register_event(goal)
        return

    # cron 和 goal 模式都先用 cron 触发首次执行
    cron = goal.get("schedule", "").strip()
    if not cron:
        log.error(f"goal {goal['id']} 缺少 schedule 字段")
        return

    parts = cron.split()
    if len(parts) == 6:
        log.warning(f"cron 多了一段（秒），自动丢弃: {cron}")
        parts = parts[1:]
    if len(parts) != 5:
        log.error(f"无效 cron 表达式: {cron}")
        return

    minute, hour, day, month, dow = parts
    trigger = CronTrigger(
        minute=minute, hour=hour, day=day,
        month=month, day_of_week=dow,
        timezone="Asia/Shanghai",
    )
    _scheduler.add_job(
        _run_goal, trigger=trigger, args=[goal["id"]],
        id=goal["id"], replace_existing=True,
    )
    log.info(f"已注册 goal {goal['id']} [{mode}]: {cron}")


def _register_event(goal: dict) -> None:
    """event 模式：启动 IMAP IDLE 监听线程。"""
    try:
        from engine.tools.imap_tool import IMAPTool
        imap = IMAPTool()
        imap.idle_listen(lambda: _run_goal(goal["id"]))
        log.info(f"已启动 IMAP IDLE 监听 goal={goal['id']}")
    except Exception as e:
        log.error(f"启动 IMAP IDLE 失败: {e}")


def reschedule(goal_id: str, delay: timedelta) -> None:
    """LoopEngine 调用：在 delay 后重新触发该 goal。"""
    run_date = datetime.now() + delay
    try:
        _scheduler.reschedule_job(
            goal_id,
            trigger=DateTrigger(run_date=run_date, timezone="Asia/Shanghai"),
        )
        log.info(f"已重调度 goal={goal_id}，将在 {run_date.strftime('%H:%M:%S')} 触发")
    except Exception as e:
        # job 不存在时重新 add
        log.warning(f"reschedule_job 失败，改用 add_job: {e}")
        _scheduler.add_job(
            _run_goal,
            trigger=DateTrigger(run_date=run_date, timezone="Asia/Shanghai"),
            args=[goal_id], id=goal_id, replace_existing=True,
        )


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
