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
from conversation_records import ConversationRecorder
from engine.records import RunRecorder
from loops import discover

log = logging.getLogger(__name__)

_scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
_run_recorder = RunRecorder()
_conversation_recorder = ConversationRecorder()
_RUN_RECORDS_CLEANUP_JOB_ID = "__cleanup_run_records__"
_CONVERSATION_RECORDS_CLEANUP_JOB_ID = "__cleanup_conversation_records__"


def _safe_increment_failure(goal_id: str) -> int:
    try:
        return goals_mod.increment_failure(goal_id)
    except Exception as e:
        log.error(f"goal={goal_id} 记录失败次数失败: {e}")
        return 0


def _safe_update_last_run(goal_id: str, summary: str, meta: dict | None = None) -> None:
    try:
        goals_mod.update_last_run(goal_id, summary, meta=meta)
    except Exception as e:
        log.error(f"goal={goal_id} 更新 last_run 失败: {e}")


def _safe_mark_success(goal_id: str) -> None:
    try:
        goals_mod.mark_success(goal_id)
    except Exception as e:
        log.error(f"goal={goal_id} 标记成功失败: {e}")


def _cleanup_run_records() -> None:
    try:
        deleted = _run_recorder.cleanup_old_files()
        if deleted:
            log.info(f"run_records 定时清理完成，删除 {deleted} 个历史文件")
    except Exception as e:
        log.error(f"run_records 定时清理失败: {e}")


def _cleanup_conversation_records() -> None:
    try:
        deleted = _conversation_recorder.cleanup_old_files()
        if deleted:
            log.info(f"conversation_records 定时清理完成，删除 {deleted} 个历史文件")
    except Exception as e:
        log.error(f"conversation_records 定时清理失败: {e}")


def _compute_failure_delay(goal: dict, failure_count: int) -> timedelta:
    base_minutes = max(1, int(goal.get("retry_after_minutes", 30)))
    factor = max(1, int(goal.get("retry_backoff_factor", 2)))
    max_minutes = max(base_minutes, int(goal.get("retry_max_minutes", 240)))

    delay_minutes = base_minutes * (factor ** max(0, failure_count - 1))
    delay_minutes = min(delay_minutes, max_minutes)
    return timedelta(minutes=delay_minutes)


def _schedule_failure_retry(goal: dict, failure_count: int, reason: str) -> None:
    max_retries = max(0, int(goal.get("max_retries", 3)))
    if failure_count > max_retries:
        log.error(f"goal={goal['id']} 已超过最大失败重试次数 {max_retries}，停止自动重试")
        return

    delay = _compute_failure_delay(goal, failure_count)
    reschedule(goal["id"], delay)
    log.warning(
        f"goal={goal['id']} 执行失败，将在 {int(delay.total_seconds() // 60)} 分钟后重试 "
        f"(failure_count={failure_count}/{max_retries}) | {reason}"
    )


def _execute_goal(
    goal: dict,
    *,
    notify_result: bool = True,
    allow_retry_schedule: bool = True,
    dry_run_override: bool | None = None,
):
    goal_id = goal["id"]
    effective_goal = dict(goal)
    if dry_run_override is not None:
        effective_goal["dry_run"] = dry_run_override

    loop_name = goal.get("loop", "")
    loop = discover().get(loop_name)
    if not loop:
        log.error(f"未知 loop 类型: {loop_name}")
        return None

    log.info(f"触发目标 {goal_id}: {goal['raw']} | dry_run={effective_goal.get('dry_run', False)}")
    try:
        from engine.engine import get_engine
        engine = get_engine()
        engine._scheduler = _scheduler_proxy()
        run_result = engine.run(loop, effective_goal)
        _safe_update_last_run(goal_id, run_result.summary, meta=run_result.result)
        if run_result.success:
            _safe_mark_success(goal_id)
            if notify_result:
                notifier.notify("助手完成任务", run_result.summary)
            return run_result

        failure_count = _safe_increment_failure(goal_id)
        if notify_result:
            notifier.notify("助手任务失败", run_result.summary)
        if allow_retry_schedule and goal.get("status") == "active":
            _schedule_failure_retry(goal, failure_count, run_result.summary)
        return run_result
    except Exception as e:
        msg = f"执行失败: {e}"
        log.error(f"goal={goal_id} {msg}")
        failure_count = _safe_increment_failure(goal_id)
        if notify_result:
            notifier.notify("助手任务失败", msg)
        if allow_retry_schedule and goal.get("status") == "active":
            _schedule_failure_retry(goal, failure_count, msg)
        return None


def _run_goal(goal_id: str) -> None:
    goal = goals_mod.get(goal_id)
    if not goal or goal["status"] != "active":
        return
    _execute_goal(goal, notify_result=True, allow_retry_schedule=True)


def run_goal_now(goal_id: str, *, dry_run_override: bool | None = None):
    """手动立即执行某个 active goal。paused goal 不允许通过命令绕过状态。"""
    goal = goals_mod.get(goal_id)
    if not goal:
        return None
    if goal.get("status") != "active":
        log.warning(f"goal={goal_id} 当前状态为 {goal.get('status')}，拒绝手动执行")
        return None
    return _execute_goal(
        goal,
        notify_result=True,
        allow_retry_schedule=True,
        dry_run_override=dry_run_override,
    )


class _scheduler_proxy:
    """给 LoopEngine 注入的调度代理，避免循环依赖。"""
    def reschedule(self, goal_id: str, delay: timedelta) -> None:
        reschedule(goal_id, delay)


def start() -> None:
    _scheduler.start()
    _scheduler.add_job(
        _cleanup_run_records,
        trigger=CronTrigger(hour=3, minute=0, timezone="Asia/Shanghai"),
        id=_RUN_RECORDS_CLEANUP_JOB_ID,
        replace_existing=True,
    )
    _scheduler.add_job(
        _cleanup_conversation_records,
        trigger=CronTrigger(hour=3, minute=5, timezone="Asia/Shanghai"),
        id=_CONVERSATION_RECORDS_CLEANUP_JOB_ID,
        replace_existing=True,
    )
    _cleanup_run_records()
    _cleanup_conversation_records()
    for goal in goals_mod.list_all():
        if goal["status"] == "active":
            _register(goal)
    log.info("调度器已启动")


def _register(goal: dict) -> None:
    mode = goal.get("trigger_mode", "cron")
    loop = discover().get(goal.get("loop", ""))
    if not loop:
        log.error(f"goal {goal.get('id')} 对应的 loop 不存在: {goal.get('loop')}")
        return
    supported_modes = tuple(getattr(loop, "supported_trigger_modes", ("cron", "goal")))
    if mode not in supported_modes:
        log.error(
            f"goal {goal['id']} 的触发模式 {mode} 不受 loop={goal['loop']} 支持，"
            f"支持：{supported_modes}"
        )
        return

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
    if not _scheduler.running:
        return
    _scheduler.shutdown(wait=False)
