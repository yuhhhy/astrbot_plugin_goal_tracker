"""每目标每日提醒的注册、恢复与投递。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.api.event import MessageChain

from .constants import MODE_TIMER, PLUGIN_ID
from .parsing import parse_clock_time

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

    from .state_store import GoalStateStore


def cron_expression(time_str: str) -> str:
    hour, minute = time_str.split(":")
    return f"{int(minute)} {int(hour)} * * *"


def cron_timezone_name(offset: int) -> str | None:
    if 0 <= offset <= 14:
        return f"Etc/GMT-{offset}"
    if -12 <= offset < 0:
        return f"Etc/GMT+{-offset}"
    return None


class ReminderService:
    """管理 AstrBot basic cron 任务与主动消息。"""

    def __init__(
        self,
        *,
        context: Any,
        store: GoalStateStore,
        config_value: Any,
        now: Any,
    ) -> None:
        self._context = context
        self._store = store
        self._config_value = config_value
        self._now = now

    def _timezone(self) -> str | None:
        try:
            offset = int(self._config_value("timezone_offset", 8))
        except (TypeError, ValueError):
            offset = 8
        return cron_timezone_name(max(-12, min(14, offset)))

    async def register_job(
        self, *, state_key: str, goal_id: str, umo: str, time_str: str
    ) -> str | None:
        payload = {
            "plugin": PLUGIN_ID,
            "state_key": state_key,
            "goal_id": goal_id,
            "umo": umo,
        }
        kwargs = {
            "name": f"{PLUGIN_ID}:{state_key[-8:]}:{goal_id}",
            "cron_expression": cron_expression(time_str),
            "handler": self.push_scheduled,
            "description": f"目标每日提醒 {time_str}",
            "payload": payload,
            "persistent": True,
        }
        try:
            try:
                job = await self._context.cron_manager.add_basic_job(
                    timezone=self._timezone(), **kwargs
                )
            except Exception:  # noqa: BLE001 - cron 后端异常类型不统一
                job = await self._context.cron_manager.add_basic_job(
                    timezone=None, **kwargs
                )
            return job.job_id
        except Exception as exc:  # noqa: BLE001 - cron 后端异常类型不统一
            logger.exception("注册目标提醒失败：%s", exc)
            return None

    async def remove_job(self, job_id: str | None) -> None:
        if not job_id:
            return
        try:
            await self._context.cron_manager.delete_job(job_id)
        except Exception as exc:  # noqa: BLE001 - cron 后端异常类型不统一
            logger.warning("删除目标提醒任务 %s 失败：%s", job_id, exc)

    async def configure(
        self,
        event: AstrMessageEvent,
        *,
        selector: str,
        enabled: bool,
        time_str: str = "",
    ) -> str:
        """开启或关闭某个目标的提醒。"""
        state = await self._store.load(event)
        goal, error = self._store.find_goal(state, selector)
        if error:
            return error
        assert goal is not None
        old_job_id = goal["reminder"].get("job_id")
        if not enabled:
            await self.remove_job(old_job_id)

            def disable(fresh: dict[str, Any]) -> None:
                target, _ = self._store.find_goal(fresh, goal["id"])
                if target:
                    target["reminder"]["enabled"] = False
                    target["reminder"]["job_id"] = None

            await self._store.update(event, disable)
            return f"已关闭“{goal['title']}”的每日提醒。"

        if not (time_str or "").strip():
            return "请先设置每日提醒时间，例如 21:00；未设置时提醒不会生效。"
        try:
            normalized_time = parse_clock_time(time_str)
        except ValueError as exc:
            return str(exc)
        new_job_id = await self.register_job(
            state_key=self._store.state_key(event),
            goal_id=goal["id"],
            umo=event.unified_msg_origin,
            time_str=normalized_time,
        )
        if not new_job_id:
            return "提醒任务创建失败，未修改原设置。"
        await self.remove_job(old_job_id)

        def enable(fresh: dict[str, Any]) -> None:
            target, _ = self._store.find_goal(fresh, goal["id"])
            if target:
                target["reminder"].update(
                    {
                        "enabled": True,
                        "time": normalized_time,
                        "job_id": new_job_id,
                        "umo": event.unified_msg_origin,
                        "last_sent_date": None,
                    }
                )

        await self._store.update(event, enable)
        global_text = (
            "当前管理员总开关已开启，提醒会正常发送。"
            if self._config_value("enable_daily_reminders", False)
            else "已保存；管理员总开关目前关闭，开启后才会发送。"
        )
        return f"已设置“{goal['title']}”每天 {normalized_time} 提醒。{global_text}"

    async def status(self, event: AstrMessageEvent, selector: str) -> str:
        state = await self._store.load(event)
        goal, error = self._store.find_goal(state, selector)
        if error:
            return error
        assert goal is not None
        reminder = goal["reminder"]
        if not reminder.get("enabled"):
            return f"“{goal['title']}”的每日提醒已关闭。"
        global_enabled = bool(self._config_value("enable_daily_reminders", False))
        global_text = "总开关已开启" if global_enabled else "总开关已关闭，暂不发送"
        return f"“{goal['title']}”每天 {reminder.get('time')} 提醒；{global_text}。"

    async def push_scheduled(self, state_key: str, goal_id: str, umo: str) -> None:
        await self.deliver(state_key=state_key, goal_id=goal_id, umo=umo)

    async def deliver(
        self,
        *,
        state_key: str,
        goal_id: str,
        umo: str,
        event: AstrMessageEvent | None = None,
    ) -> bool:
        """投递一条目标提醒，成功后标记当日已发送。"""
        if not self._config_value("enable_daily_reminders", False):
            return False
        async with self._store.locked_by_key(state_key):
            state = await self._store.load_by_key(state_key)
            goal, _ = self._store.find_goal(state, goal_id)
            if goal is None or not goal["reminder"].get("enabled"):
                return False
            today = self._now().date().isoformat()
            if goal["reminder"].get("last_sent_date") == today:
                return False
            progress = self._store.daily_progress(state, goal, today)
            target = int(goal["daily_target"])
            unit = "分钟" if goal["mode"] == MODE_TIMER else "次"
            if progress >= target:
                body = f"今天已完成 {progress}/{target} {unit}，很棒！"
            else:
                body = f"今天已完成 {progress}/{target} {unit}，还差 {target - progress} {unit}。"
            text = f"⏰ 目标提醒：{goal['title']}\n{body}"
            try:
                if event is not None:
                    await event.send(event.make_result().message(text))
                else:
                    await self._context.send_message(umo, MessageChain().message(text))
            except Exception as exc:  # noqa: BLE001 - 消息平台异常类型不统一
                logger.warning("目标提醒投递失败（%s）：%s", goal["title"], exc)
                return False
            goal["reminder"]["last_sent_date"] = today
            await self._store.save_by_key(state_key, state)
            return True

    async def deliver_due_for_event(self, event: AstrMessageEvent) -> None:
        """用户发言时补发已到时但主动投递失败的提醒。"""
        if not self._config_value("enable_daily_reminders", False):
            return
        state = await self._store.load(event)
        current = self._now()
        current_clock = (current.hour, current.minute)
        for goal in state["goals"]:
            reminder = goal["reminder"]
            if goal.get("archived") or not reminder.get("enabled"):
                continue
            try:
                hour, minute = (int(part) for part in reminder["time"].split(":"))
            except (AttributeError, TypeError, ValueError):
                continue
            if current_clock >= (hour, minute):
                await self.deliver(
                    state_key=self._store.state_key(event),
                    goal_id=goal["id"],
                    umo=event.unified_msg_origin,
                    event=event,
                )

    async def restore_jobs(self) -> int:
        """重建持久化 basic 任务丢失的 Python handler。"""
        try:
            jobs = await self._context.cron_manager.list_jobs("basic")
        except Exception as exc:  # noqa: BLE001 - cron 后端异常类型不统一
            logger.warning("读取目标提醒任务失败：%s", exc)
            return 0
        restored = 0
        for job in jobs:
            payload = getattr(job, "payload", None) or {}
            if payload.get("plugin") != PLUGIN_ID:
                continue
            state_key = payload.get("state_key")
            goal_id = payload.get("goal_id")
            umo = payload.get("umo")
            if not state_key or not goal_id or not umo:
                continue
            state = await self._store.load_by_key(state_key)
            goal, _ = self._store.find_goal(state, goal_id)
            if goal is None or not goal["reminder"].get("enabled"):
                await self.remove_job(job.job_id)
                continue
            try:
                await self._context.cron_manager.delete_job(job.job_id)
                reminder_time = goal["reminder"].get("time")
                if not reminder_time:
                    await self.remove_job(job.job_id)
                    async with self._store.locked_by_key(state_key):
                        fresh = await self._store.load_by_key(state_key)
                        target, _ = self._store.find_goal(fresh, goal_id)
                        if target:
                            target["reminder"]["enabled"] = False
                            target["reminder"]["job_id"] = None
                            await self._store.save_by_key(state_key, fresh)
                    continue
                new_id = await self.register_job(
                    state_key=state_key,
                    goal_id=goal_id,
                    umo=umo,
                    time_str=reminder_time,
                )
                if not new_id:
                    continue
                async with self._store.locked_by_key(state_key):
                    fresh = await self._store.load_by_key(state_key)
                    target, _ = self._store.find_goal(fresh, goal_id)
                    if target:
                        target["reminder"]["job_id"] = new_id
                        await self._store.save_by_key(state_key, fresh)
                restored += 1
            except Exception as exc:  # noqa: BLE001 - cron 后端异常类型不统一
                logger.exception("恢复目标提醒失败：%s", exc)
        if restored:
            logger.info("目标跟踪助手：已恢复 %d 个每日提醒", restored)
        return restored
