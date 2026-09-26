"""目标跟踪助手插件入口。

AstrBot 需在插件类定义时注册 handler，因此装饰器方法保留在本文件；
所有业务处理均单行委托给 ``core/`` 模块。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .core import goals, handlers, image_flow
from .core.constants import PLUGIN_ID
from .core.reminders import ReminderService
from .core.state_store import GoalStateStore


@register(
    PLUGIN_ID,
    "yuhhhy",
    "用自然语言、图片识别和每日提醒管理次数或计时目标",
    "0.1.0",
)
class GoalTrackerPlugin(Star):
    """按用户隔离的每日目标跟踪插件。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        self.store = GoalStateStore(
            get_kv_data=self.get_kv_data,
            put_kv_data=self.put_kv_data,
            delete_kv_data=self.delete_kv_data,
            config_value=self._config_value,
        )
        self.reminders = ReminderService(
            context=context,
            store=self.store,
            config_value=self._config_value,
            now=self._now,
        )

    def _config_value(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except AttributeError:
            return default
        return default if value is None else value

    def _now(self) -> datetime:
        try:
            offset = int(self._config_value("timezone_offset", 8))
        except (TypeError, ValueError):
            offset = 8
        return datetime.now(timezone(timedelta(hours=max(-12, min(14, offset)))))

    @filter.command_group("目标")
    def goal_group():
        """管理每日次数打卡或计时目标。"""

    @goal_group.command("帮助")
    async def help(self, event: AstrMessageEvent):
        yield await handlers.help_command(event)

    @goal_group.command("新增")
    async def create(
        self,
        event: AstrMessageEvent,
        title: str,
        mode: str = "打卡",
        target: int = 1,
    ):
        yield await handlers.create_command(
            event,
            store=self.store,
            now=self._now,
            title=title,
            mode=mode,
            target=target,
        )

    @goal_group.command("列表")
    async def list_command(self, event: AstrMessageEvent):
        yield await handlers.list_command(event, store=self.store, now=self._now)

    @goal_group.command("今日")
    async def today(self, event: AstrMessageEvent):
        yield await handlers.list_command(event, store=self.store, now=self._now)

    @goal_group.command("统计")
    async def stats(self, event: AstrMessageEvent, days: int = 7):
        yield await handlers.stats_command(
            event, store=self.store, now=self._now, days=days
        )

    @goal_group.command("打卡")
    async def checkin(self, event: AstrMessageEvent, goal: str, count: int = 1):
        yield await handlers.checkin_command(
            event, store=self.store, now=self._now, selector=goal, count=count
        )

    @goal_group.command("计时")
    async def duration(self, event: AstrMessageEvent, goal: str, minutes: int):
        yield await handlers.duration_command(
            event, store=self.store, now=self._now, selector=goal, minutes=minutes
        )

    @goal_group.command("开始")
    async def start(self, event: AstrMessageEvent, goal: str):
        yield await handlers.start_command(
            event, store=self.store, now=self._now, selector=goal
        )

    @goal_group.command("结束")
    async def stop(self, event: AstrMessageEvent, goal: str):
        yield await handlers.stop_command(
            event, store=self.store, now=self._now, selector=goal
        )

    @goal_group.command("提醒")
    async def reminder(
        self,
        event: AstrMessageEvent,
        goal: str,
        action: str = "状态",
        time: str = "",
    ):
        yield await handlers.reminder_command(
            event,
            reminder_service=self.reminders,
            selector=goal,
            action=action,
            time_str=time,
        )

    @goal_group.command("归档")
    async def archive(self, event: AstrMessageEvent, goal: str):
        yield await handlers.archive_command(
            event, store=self.store, reminder_service=self.reminders, selector=goal
        )

    @goal_group.command("恢复")
    async def restore(self, event: AstrMessageEvent, goal: str):
        yield await handlers.restore_command(
            event,
            store=self.store,
            reminder_service=self.reminders,
            selector=goal,
        )

    @goal_group.command("删除")
    async def delete(self, event: AstrMessageEvent, goal: str):
        yield await handlers.delete_command(
            event, store=self.store, reminder_service=self.reminders, selector=goal
        )

    @filter.llm_tool(name="create_goal_tracker_goal")
    async def create_goal_tool(
        self,
        event: AstrMessageEvent,
        title: str,
        mode: str,
        daily_target: int,
        description: str = "",
    ) -> str:
        """当用户想新增长期或每日目标时调用。

        次数型目标的 daily_target 是每天次数；计时型是每天分钟数。
        若用户没说清模式或目标值，应先询问，不要臆测。

        Args:
            title(string): 简短、唯一的目标名称。
            mode(string): checkin（次数打卡）或 timer（计时）。
            daily_target(number): 每日目标次数或分钟数。
            description(string): 用户对目标的补充描述，可留空。
        """
        return await goals.create_goal(
            event,
            store=self.store,
            now=self._now,
            title=title,
            mode=mode,
            daily_target=daily_target,
            description=description,
        )

    @filter.llm_tool(name="update_goal_tracker_goal")
    async def update_goal_tool(
        self,
        event: AstrMessageEvent,
        goal: str,
        title: str = "",
        mode: str = "",
        daily_target: int = 0,
        description: str = "",
    ) -> str:
        """修改已有目标的名称、模式、每日目标值或描述。

        Args:
            goal(string): 现有目标名称或 ID。
            title(string): 新名称；不修改时留空。
            mode(string): checkin 或 timer；不修改时留空。
            daily_target(number): 新的每日次数或分钟；不修改时填 0。
            description(string): 新描述；不修改时留空。
        """
        return await goals.update_goal(
            event,
            store=self.store,
            now=self._now,
            selector=goal,
            title=title,
            mode=mode,
            daily_target=daily_target,
            description=description,
        )

    @filter.llm_tool(name="list_goal_tracker_goals")
    async def list_goals_tool(
        self, event: AstrMessageEvent, include_archived: bool = False
    ) -> str:
        """查看用户的目标、今日进度、计时与提醒状态。

        Args:
            include_archived(boolean): 是否一并列出已归档目标。
        """
        return await goals.list_goals(
            event, store=self.store, now=self._now, include_archived=include_archived
        )

    @filter.llm_tool(name="record_goal_checkin")
    async def record_checkin_tool(
        self,
        event: AstrMessageEvent,
        goal: str,
        count: int = 1,
        date: str = "today",
        note: str = "",
    ) -> str:
        """用户明确表示已完成某个次数打卡目标时记录。

        用户说“做了三组”等明确次数时将 count 设为对应数字。
        仅探讨、计划或疑问时不要调用。

        Args:
            goal(string): 目标名称或 ID。
            count(number): 本次增加的打卡次数，默认 1。
            date(string): today、yesterday、前天或 YYYY-MM-DD。
            note(string): 可选备注。
        """
        return await goals.record_checkin(
            event,
            store=self.store,
            now=self._now,
            selector=goal,
            count=count,
            date=date,
            note=note,
        )

    @filter.llm_tool(name="log_goal_duration")
    async def log_duration_tool(
        self,
        event: AstrMessageEvent,
        minutes: int,
        goal: str = "",
        date: str = "today",
        note: str = "",
    ) -> str:
        """用户明确说明某计时目标已做了多久时补记分钟。

        若这是对图片匹配询问的直接回复，且只有一个候选目标，goal 可留空。

        Args:
            minutes(number): 本次实际用时，单位分钟。
            goal(string): 目标名称或 ID；单个图片候选时可留空。
            date(string): today、yesterday、前天或 YYYY-MM-DD。
            note(string): 可选备注。
        """
        return await goals.log_duration(
            event,
            store=self.store,
            now=self._now,
            selector=goal,
            minutes=minutes,
            date=date,
            note=note,
        )

    @filter.llm_tool(name="start_goal_timer")
    async def start_timer_tool(self, event: AstrMessageEvent, goal: str) -> str:
        """当用户要从现在开始为计时目标计时时调用。

        Args:
            goal(string): 计时目标名称或 ID。
        """
        return await goals.start_timer(
            event, store=self.store, now=self._now, selector=goal
        )

    @filter.llm_tool(name="stop_goal_timer")
    async def stop_timer_tool(self, event: AstrMessageEvent, goal: str) -> str:
        """当用户要结束某个已开始的计时时调用，自动计算并记录时长。

        Args:
            goal(string): 计时目标名称或 ID。
        """
        return await goals.stop_timer(
            event, store=self.store, now=self._now, selector=goal
        )

    @filter.llm_tool(name="get_goal_tracker_stats")
    async def stats_tool(self, event: AstrMessageEvent, days: int = 7) -> str:
        """查询各目标的累计坚持、当前连续天数和近期达标进度。

        Args:
            days(number): 近期达标和进度的统计天数，默认 7；不影响全部历史的坚持与连续天数。
        """
        return await goals.progress_summary(
            event, store=self.store, now=self._now, days=days
        )

    @filter.llm_tool(name="configure_goal_reminder")
    async def reminder_tool(
        self,
        event: AstrMessageEvent,
        goal: str,
        enabled: bool,
        time: str = "",
    ) -> str:
        """根据用户明确请求，为某个目标开启或关闭每日提醒。

        若用户没有说明提醒时间，必须先询问具体时间，不要调用本工具开启提醒。

        Args:
            goal(string): 目标名称或 ID。
            enabled(boolean): true 表示开启，false 表示关闭。
            time(string): 开启时必填的 HH:MM 时间；关闭时可留空。
        """
        return await self.reminders.configure(
            event, selector=goal, enabled=enabled, time_str=time
        )

    @filter.llm_tool(name="archive_goal_tracker_goal")
    async def archive_goal_tool(self, event: AstrMessageEvent, goal: str) -> str:
        """用户明确要暂停或归档目标时调用，会同时停止该目标提醒。

        Args:
            goal(string): 目标名称或 ID。
        """
        return await goals.set_archived(
            event,
            store=self.store,
            reminder_service=self.reminders,
            selector=goal,
            archived=True,
        )

    @filter.llm_tool(name="delete_goal_tracker_goal")
    async def delete_goal_tool(self, event: AstrMessageEvent, goal: str) -> str:
        """仅当用户明确要求永久删除目标及其记录时调用。

        “暂停”、“不再提醒”或意图不明时不得删除，应使用归档或关闭提醒。

        Args:
            goal(string): 目标名称或 ID。
        """
        return await goals.delete_goal(
            event, store=self.store, reminder_service=self.reminders, selector=goal
        )

    @filter.llm_tool(name="restore_goal_tracker_goal")
    async def restore_goal_tool(self, event: AstrMessageEvent, goal: str) -> str:
        """用户明确要恢复一个已归档目标时调用。恢复后提醒仍保持关闭。

        Args:
            goal(string): 已归档目标的完整名称或 ID。
        """
        return await goals.set_archived(
            event,
            store=self.store,
            reminder_service=self.reminders,
            selector=goal,
            archived=False,
        )

    @filter.llm_tool(name="dismiss_goal_image_suggestion")
    async def dismiss_image_tool(self, event: AstrMessageEvent) -> str:
        """用户对图片记录询问回复“不用”、“取消”时调用。"""
        return await handlers.clear_pending_image(event, store=self.store)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def listen_for_goal_images(self, event: AstrMessageEvent):
        await image_flow.handle_images(
            event,
            context=self.context,
            store=self.store,
            config_value=self._config_value,
            now=self._now,
        )

    @filter.event_message_type(filter.EventMessageType.ALL, priority=20)
    async def deliver_due_reminders(self, event: AstrMessageEvent):
        await self.reminders.deliver_due_for_event(event)

    async def initialize(self) -> None:
        await handlers.initialize(reminder_service=self.reminders)

    async def terminate(self) -> None:
        await handlers.terminate(store=self.store)
