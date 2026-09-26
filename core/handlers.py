"""明确指令的业务委托与回复封装。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import goals

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent


def result(event: AstrMessageEvent, text: str):
    return event.make_result().message(text)


async def help_command(event: AstrMessageEvent):
    text = (
        "🎯 目标跟踪助手\n"
        "你可以直接说：“帮我创建每天阅读 30 分钟的目标”、"
        "“健身打卡两次”、“开始背单词计时”。\n\n"
        "明确指令：\n"
        "/目标 新增 <名称> <打卡|计时> <每日次数|分钟>\n"
        "/目标 列表｜/目标 今日｜/目标 统计 [天数]\n"
        "/目标 打卡 <名称> [次数]\n"
        "/目标 计时 <名称> <分钟>\n"
        "/目标 开始 <名称>｜/目标 结束 <名称>\n"
        "/目标 提醒 <名称> 开启 <HH:MM>\n"
        "/目标 提醒 <名称> <关闭|状态>\n"
        "/目标 归档 <名称>｜/目标 恢复 <名称>｜/目标 删除 <名称>"
    )
    return result(event, text)


async def create_command(
    event: AstrMessageEvent,
    *,
    store: Any,
    now: Any,
    title: str,
    mode: str,
    target: int,
):
    text = await goals.create_goal(
        event,
        store=store,
        now=now,
        title=title,
        mode=mode,
        daily_target=target,
    )
    return result(event, text)


async def list_command(event: AstrMessageEvent, *, store: Any, now: Any):
    return result(event, await goals.list_goals(event, store=store, now=now))


async def stats_command(event: AstrMessageEvent, *, store: Any, now: Any, days: int):
    return result(
        event,
        await goals.progress_summary(event, store=store, now=now, days=days),
    )


async def checkin_command(
    event: AstrMessageEvent,
    *,
    store: Any,
    now: Any,
    selector: str,
    count: int,
):
    return result(
        event,
        await goals.record_checkin(
            event, store=store, now=now, selector=selector, count=count
        ),
    )


async def duration_command(
    event: AstrMessageEvent,
    *,
    store: Any,
    now: Any,
    selector: str,
    minutes: int,
):
    return result(
        event,
        await goals.log_duration(
            event,
            store=store,
            now=now,
            selector=selector,
            minutes=minutes,
        ),
    )


async def start_command(
    event: AstrMessageEvent, *, store: Any, now: Any, selector: str
):
    return result(
        event,
        await goals.start_timer(event, store=store, now=now, selector=selector),
    )


async def stop_command(event: AstrMessageEvent, *, store: Any, now: Any, selector: str):
    return result(
        event,
        await goals.stop_timer(event, store=store, now=now, selector=selector),
    )


async def reminder_command(
    event: AstrMessageEvent,
    *,
    reminder_service: Any,
    selector: str,
    action: str,
    time_str: str,
):
    action = (action or "状态").strip()
    if action in {"开启", "开", "on"}:
        text = await reminder_service.configure(
            event, selector=selector, enabled=True, time_str=time_str
        )
    elif action in {"关闭", "关", "off"}:
        text = await reminder_service.configure(event, selector=selector, enabled=False)
    elif action in {"状态", "status"}:
        text = await reminder_service.status(event, selector)
    else:
        text = "操作必须是开启、关闭或状态。"
    return result(event, text)


async def archive_command(
    event: AstrMessageEvent,
    *,
    store: Any,
    reminder_service: Any,
    selector: str,
):
    return result(
        event,
        await goals.set_archived(
            event,
            store=store,
            reminder_service=reminder_service,
            selector=selector,
            archived=True,
        ),
    )


async def restore_command(
    event: AstrMessageEvent,
    *,
    store: Any,
    reminder_service: Any,
    selector: str,
):
    return result(
        event,
        await goals.set_archived(
            event,
            store=store,
            reminder_service=reminder_service,
            selector=selector,
            archived=False,
        ),
    )


async def delete_command(
    event: AstrMessageEvent,
    *,
    store: Any,
    reminder_service: Any,
    selector: str,
):
    return result(
        event,
        await goals.delete_goal(
            event,
            store=store,
            reminder_service=reminder_service,
            selector=selector,
        ),
    )


async def clear_pending_image(event: AstrMessageEvent, *, store: Any) -> str:
    await store.update(event, lambda state: state.update(pending_image=None))
    return "已取消这次图片记录。"


async def initialize(*, reminder_service: Any) -> None:
    await reminder_service.restore_jobs()


async def terminate(*, store: Any) -> None:
    store.close()
