"""目标、打卡、计时与统计的核心业务逻辑。"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .constants import (
    BACKFILL_MAX_DAYS,
    MAX_DAILY_CHECKINS,
    MAX_DESCRIPTION_LENGTH,
    MAX_DURATION_MINUTES,
    MAX_GOALS_PER_USER,
    MAX_NOTE_LENGTH,
    MAX_TITLE_LENGTH,
    MODE_CHECKIN,
    MODE_LABELS,
    MODE_TIMER,
    PENDING_IMAGE_HOURS,
)
from .parsing import coerce_int, normalize_mode, resolve_date

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

    from .state_store import GoalStateStore

Now = Callable[[], datetime]


def _target_text(goal: dict[str, Any]) -> str:
    unit = "分钟" if goal["mode"] == MODE_TIMER else "次"
    return f"{goal['daily_target']} {unit}/天"


def _progress_text(goal: dict[str, Any], progress: int) -> str:
    target = int(goal["daily_target"])
    unit = "分钟" if goal["mode"] == MODE_TIMER else "次"
    status = (
        "✅ 今日已完成" if progress >= target else f"还差 {target - progress} {unit}"
    )
    return f"{progress}/{target} {unit}，{status}"


def _validated_date(spec: str, current: datetime) -> tuple[str | None, str | None]:
    try:
        date = resolve_date(spec, current)
    except ValueError:
        return None, "日期无法识别，请使用今天、昨天、前天或 YYYY-MM-DD。"
    days_ago = (current.date() - datetime.fromisoformat(date).date()).days
    if days_ago < 0:
        return None, "不能记录未来日期的进度。"
    if days_ago > BACKFILL_MAX_DAYS:
        return None, f"只支持补记最近 {BACKFILL_MAX_DAYS} 天。"
    return date, None


async def create_goal(
    event: AstrMessageEvent,
    *,
    store: GoalStateStore,
    now: Now,
    title: str,
    mode: str,
    daily_target: Any,
    description: str = "",
) -> str:
    """创建目标。"""
    title = (title or "").strip()[:MAX_TITLE_LENGTH]
    if not title:
        return "请告诉我目标名称。"
    try:
        normalized_mode = normalize_mode(mode)
    except ValueError as exc:
        return str(exc)
    try:
        target = coerce_int(daily_target, field="每日目标")
    except ValueError as exc:
        return str(exc)
    maximum = (
        MAX_DURATION_MINUTES if normalized_mode == MODE_TIMER else MAX_DAILY_CHECKINS
    )
    if not 1 <= target <= maximum:
        unit = "分钟" if normalized_mode == MODE_TIMER else "次"
        return f"每日目标需在 1～{maximum} {unit}之间。"

    outcome: dict[str, Any] = {}

    def mutate(state: dict[str, Any]) -> None:
        active = [goal for goal in state["goals"] if not goal.get("archived")]
        if len(active) >= MAX_GOALS_PER_USER:
            outcome["error"] = f"活跃目标已达上限 {MAX_GOALS_PER_USER} 个。"
            return
        if any(goal["title"] == title for goal in active):
            outcome["error"] = f"已有同名目标“{title}”。"
            return
        current = now()
        goal = {
            "id": uuid.uuid4().hex[:8],
            "title": title,
            "description": (description or "").strip()[:MAX_DESCRIPTION_LENGTH],
            "mode": normalized_mode,
            "daily_target": target,
            "archived": False,
            "created_at": current.isoformat(),
            "updated_at": current.isoformat(),
            "reminder": {
                "enabled": False,
                "time": None,
                "job_id": None,
                "umo": None,
                "last_sent_date": None,
            },
        }
        state["goals"].append(goal)
        outcome["goal"] = goal

    await store.update(event, mutate)
    if outcome.get("error"):
        return str(outcome["error"])
    goal = outcome["goal"]
    return (
        f"已创建目标“{goal['title']}”：{MODE_LABELS[goal['mode']]}，"
        f"每日目标 {_target_text(goal)}。提醒默认关闭。"
    )


async def update_goal(
    event: AstrMessageEvent,
    *,
    store: GoalStateStore,
    now: Now,
    selector: str,
    title: str = "",
    mode: str = "",
    daily_target: Any = 0,
    description: str = "",
) -> str:
    """更新目标的非提醒属性。"""
    outcome: dict[str, Any] = {}

    def mutate(state: dict[str, Any]) -> None:
        goal, error = store.find_goal(state, selector)
        if error:
            outcome["error"] = error
            return
        assert goal is not None
        if title:
            clean_title = title.strip()[:MAX_TITLE_LENGTH]
            if any(
                item["id"] != goal["id"]
                and not item.get("archived")
                and item["title"] == clean_title
                for item in state["goals"]
            ):
                outcome["error"] = f"已有同名目标“{clean_title}”。"
                return
            goal["title"] = clean_title
        if mode:
            try:
                goal["mode"] = normalize_mode(mode)
            except ValueError as exc:
                outcome["error"] = str(exc)
                return
            state["active_timers"].pop(goal["id"], None)
        if daily_target not in {0, "0", "", None}:
            try:
                target = coerce_int(daily_target, field="每日目标")
            except ValueError as exc:
                outcome["error"] = str(exc)
                return
            maximum = (
                MAX_DURATION_MINUTES
                if goal["mode"] == MODE_TIMER
                else MAX_DAILY_CHECKINS
            )
            if not 1 <= target <= maximum:
                outcome["error"] = f"每日目标需在 1～{maximum} 之间。"
                return
            goal["daily_target"] = target
        if description:
            goal["description"] = description.strip()[:MAX_DESCRIPTION_LENGTH]
        goal["updated_at"] = now().isoformat()
        outcome["goal"] = goal

    await store.update(event, mutate)
    if outcome.get("error"):
        return str(outcome["error"])
    goal = outcome["goal"]
    return (
        f"已更新“{goal['title']}”：{MODE_LABELS[goal['mode']]}，"
        f"每日目标 {_target_text(goal)}。"
    )


async def record_checkin(
    event: AstrMessageEvent,
    *,
    store: GoalStateStore,
    now: Now,
    selector: str,
    count: Any = 1,
    date: str = "today",
    note: str = "",
    source: str = "text",
    source_message_id: str = "",
) -> str:
    """为打卡目标记录一次或多次进度。"""
    try:
        count_value = coerce_int(count, field="打卡次数")
    except ValueError as exc:
        return str(exc)
    if not 1 <= count_value <= MAX_DAILY_CHECKINS:
        return f"打卡次数需在 1～{MAX_DAILY_CHECKINS} 之间。"
    current = now()
    entry_date, error = _validated_date(date, current)
    if error:
        return error
    outcome: dict[str, Any] = {}

    def mutate(state: dict[str, Any]) -> None:
        goal, find_error = store.find_goal(state, selector)
        if find_error:
            outcome["error"] = find_error
            return
        assert goal is not None and entry_date is not None
        if goal["mode"] != MODE_CHECKIN:
            outcome["error"] = f"“{goal['title']}”是计时目标，请记录时长。"
            return
        if source_message_id and any(
            record.get("source_message_id") == source_message_id
            for record in state["records"]
        ):
            outcome["error"] = "这张图片已经记录过，未重复打卡。"
            return
        record = {
            "id": uuid.uuid4().hex[:8],
            "goal_id": goal["id"],
            "date": entry_date,
            "created_at": current.isoformat(),
            "kind": MODE_CHECKIN,
            "count": count_value,
            "note": (note or "").strip()[:MAX_NOTE_LENGTH],
            "source": source,
            "source_message_id": source_message_id or None,
        }
        state["records"].append(record)
        if source_message_id and source_message_id not in state["image_messages"]:
            state["image_messages"].append(source_message_id)
        state["pending_image"] = None
        outcome["goal"] = goal
        outcome["progress"] = store.daily_progress(state, goal, entry_date)

    await store.update(event, mutate)
    if outcome.get("error"):
        return str(outcome["error"])
    goal = outcome["goal"]
    return (
        f"已为“{goal['title']}”记录 {count_value} 次打卡。"
        f"{entry_date} 进度：{_progress_text(goal, outcome['progress'])}。"
    )


async def log_duration(
    event: AstrMessageEvent,
    *,
    store: GoalStateStore,
    now: Now,
    selector: str,
    minutes: Any,
    date: str = "today",
    note: str = "",
    source: str = "text",
) -> str:
    """为计时目标补记一段时长。"""
    try:
        minutes_value = coerce_int(minutes, field="时长")
    except ValueError as exc:
        return str(exc)
    if not 1 <= minutes_value <= MAX_DURATION_MINUTES:
        return f"单次时长需在 1～{MAX_DURATION_MINUTES} 分钟之间。"
    current = now()
    entry_date, error = _validated_date(date, current)
    if error:
        return error
    outcome: dict[str, Any] = {}

    def mutate(state: dict[str, Any]) -> None:
        selected = selector
        if not selected and state.get("pending_image"):
            pending = state["pending_image"]
            try:
                created_at = datetime.fromisoformat(pending.get("created_at", ""))
                is_fresh = current - created_at <= timedelta(hours=PENDING_IMAGE_HOURS)
            except (TypeError, ValueError):
                is_fresh = False
            if is_fresh:
                ids = pending.get("goal_ids") or []
                if len(ids) == 1:
                    selected = ids[0]
            else:
                state["pending_image"] = None
        goal, find_error = store.find_goal(state, selected)
        if find_error:
            outcome["error"] = find_error
            return
        assert goal is not None and entry_date is not None
        if goal["mode"] != MODE_TIMER:
            outcome["error"] = f"“{goal['title']}”是次数打卡目标。"
            return
        state["records"].append(
            {
                "id": uuid.uuid4().hex[:8],
                "goal_id": goal["id"],
                "date": entry_date,
                "created_at": current.isoformat(),
                "kind": MODE_TIMER,
                "minutes": minutes_value,
                "note": (note or "").strip()[:MAX_NOTE_LENGTH],
                "source": source,
            }
        )
        state["pending_image"] = None
        outcome["goal"] = goal
        outcome["progress"] = store.daily_progress(state, goal, entry_date)

    await store.update(event, mutate)
    if outcome.get("error"):
        return str(outcome["error"])
    goal = outcome["goal"]
    return (
        f"已为“{goal['title']}”记录 {minutes_value} 分钟。"
        f"{entry_date} 进度：{_progress_text(goal, outcome['progress'])}。"
    )


async def start_timer(
    event: AstrMessageEvent, *, store: GoalStateStore, now: Now, selector: str
) -> str:
    """开始目标计时。"""
    outcome: dict[str, Any] = {}

    def mutate(state: dict[str, Any]) -> None:
        goal, error = store.find_goal(state, selector)
        if error:
            outcome["error"] = error
            return
        assert goal is not None
        if goal["mode"] != MODE_TIMER:
            outcome["error"] = f"“{goal['title']}”是次数打卡目标。"
            return
        if goal["id"] in state["active_timers"]:
            outcome["error"] = f"“{goal['title']}”已在计时中。"
            return
        started = now()
        state["active_timers"][goal["id"]] = started.isoformat()
        outcome["goal"] = goal
        outcome["started"] = started

    await store.update(event, mutate)
    if outcome.get("error"):
        return str(outcome["error"])
    return f"已开始为“{outcome['goal']['title']}”计时。"


async def stop_timer(
    event: AstrMessageEvent, *, store: GoalStateStore, now: Now, selector: str
) -> str:
    """结束计时并记录时长，不足一分钟按一分钟记。"""
    outcome: dict[str, Any] = {}

    def mutate(state: dict[str, Any]) -> None:
        goal, error = store.find_goal(state, selector)
        if error:
            outcome["error"] = error
            return
        assert goal is not None
        started_raw = state["active_timers"].get(goal["id"])
        if not started_raw:
            outcome["error"] = f"“{goal['title']}”当前没有进行中的计时。"
            return
        current = now()
        try:
            started = datetime.fromisoformat(started_raw)
            elapsed = max(1, round((current - started).total_seconds() / 60))
        except (TypeError, ValueError):
            elapsed = 1
        elapsed = min(elapsed, MAX_DURATION_MINUTES)
        state["active_timers"].pop(goal["id"], None)
        state["records"].append(
            {
                "id": uuid.uuid4().hex[:8],
                "goal_id": goal["id"],
                "date": current.date().isoformat(),
                "created_at": current.isoformat(),
                "kind": MODE_TIMER,
                "minutes": elapsed,
                "note": "开始/结束计时",
                "source": "timer",
            }
        )
        outcome["goal"] = goal
        outcome["minutes"] = elapsed
        outcome["progress"] = store.daily_progress(
            state, goal, current.date().isoformat()
        )

    await store.update(event, mutate)
    if outcome.get("error"):
        return str(outcome["error"])
    goal = outcome["goal"]
    return (
        f"“{goal['title']}”计时已结束，本次 {outcome['minutes']} 分钟。"
        f"今日进度：{_progress_text(goal, outcome['progress'])}。"
    )


async def list_goals(
    event: AstrMessageEvent,
    *,
    store: GoalStateStore,
    now: Now,
    include_archived: bool = False,
) -> str:
    """列出目标及今日进度。"""
    state = await store.load(event)
    goals = [
        goal for goal in state["goals"] if include_archived or not goal.get("archived")
    ]
    if not goals:
        return "还没有目标。你可以说：“帮我创建每天阅读 30 分钟的目标”。"
    today = now().date().isoformat()
    lines = [f"🎯 目标列表（{today}）"]
    for index, goal in enumerate(goals, start=1):
        progress = store.daily_progress(state, goal, today)
        reminder = goal.get("reminder") or {}
        reminder_text = (
            f"｜提醒 {reminder.get('time')}"
            if reminder.get("enabled")
            else "｜提醒关闭"
        )
        archived = "｜已归档" if goal.get("archived") else ""
        timer = "｜计时中" if goal["id"] in state["active_timers"] else ""
        lines.append(
            f"{index}. {goal['title']}｜{MODE_LABELS[goal['mode']]}｜"
            f"{_progress_text(goal, progress)}{reminder_text}{timer}{archived}"
        )
    return "\n".join(lines)


async def progress_summary(
    event: AstrMessageEvent,
    *,
    store: GoalStateStore,
    now: Now,
    days: Any = 7,
) -> str:
    """统计最近若干天的目标完成情况。"""
    try:
        days_value = max(1, min(90, coerce_int(days, field="统计天数")))
    except ValueError as exc:
        return str(exc)
    state = await store.load(event)
    goals = [goal for goal in state["goals"] if not goal.get("archived")]
    if not goals:
        return "还没有活跃目标。"
    from datetime import timedelta

    end = now().date()
    dates = [(end - timedelta(days=offset)).isoformat() for offset in range(days_value)]
    lines = [f"📊 最近 {days_value} 天目标统计"]
    for goal in goals:
        values = [store.daily_progress(state, goal, date) for date in dates]
        target = int(goal["daily_target"])
        completed = sum(value >= target for value in values)
        unit = "分钟" if goal["mode"] == MODE_TIMER else "次"
        lines.append(
            f"• {goal['title']}：达标 {completed}/{days_value} 天，"
            f"累计 {sum(values)} {unit}"
        )
    return "\n".join(lines)


async def set_archived(
    event: AstrMessageEvent,
    *,
    store: GoalStateStore,
    reminder_service: Any,
    selector: str,
    archived: bool,
) -> str:
    """归档或恢复目标。"""
    state = await store.load(event)
    goal, error = store.find_goal(state, selector, include_archived=True)
    if error:
        return error
    assert goal is not None
    if archived and goal.get("reminder", {}).get("job_id"):
        await reminder_service.remove_job(goal["reminder"]["job_id"])

    def mutate(fresh: dict[str, Any]) -> None:
        target, _ = store.find_goal(fresh, goal["id"], include_archived=True)
        if target is None:
            return
        target["archived"] = archived
        if archived:
            target["reminder"]["enabled"] = False
            target["reminder"]["job_id"] = None
            fresh["active_timers"].pop(target["id"], None)

    await store.update(event, mutate)
    action = "归档" if archived else "恢复"
    return f"已{action}目标“{goal['title']}”。"


async def delete_goal(
    event: AstrMessageEvent,
    *,
    store: GoalStateStore,
    reminder_service: Any,
    selector: str,
) -> str:
    """删除目标及其全部记录。"""
    state = await store.load(event)
    goal, error = store.find_goal(state, selector, include_archived=True)
    if error:
        return error
    assert goal is not None
    await reminder_service.remove_job(goal.get("reminder", {}).get("job_id"))

    def mutate(fresh: dict[str, Any]) -> None:
        fresh["goals"] = [item for item in fresh["goals"] if item["id"] != goal["id"]]
        fresh["records"] = [
            record for record in fresh["records"] if record.get("goal_id") != goal["id"]
        ]
        fresh["active_timers"].pop(goal["id"], None)
        pending = fresh.get("pending_image") or {}
        if goal["id"] in (pending.get("goal_ids") or []):
            fresh["pending_image"] = None

    await store.update(event, mutate)
    return f"已删除目标“{goal['title']}”及其全部进度记录。"
