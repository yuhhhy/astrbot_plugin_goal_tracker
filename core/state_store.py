"""按用户隔离的目标状态存储。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from .constants import (
    DEFAULT_MAX_RECORDS,
    MAX_MAX_RECORDS,
    MIN_MAX_RECORDS,
    MODE_CHECKIN,
    MODE_TIMER,
    VALID_MODES,
)

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent


class GoalStateStore:
    """封装 KV 读写、数据归一化和每用户事务锁。"""

    def __init__(
        self,
        *,
        get_kv_data: Callable[..., Awaitable[Any]],
        put_kv_data: Callable[..., Awaitable[None]],
        delete_kv_data: Callable[[str], Awaitable[None]],
        config_value: Callable[[str, Any], Any],
    ) -> None:
        self._get_kv_data = get_kv_data
        self._put_kv_data = put_kv_data
        self._delete_kv_data = delete_kv_data
        self._config_value = config_value
        self._locks: dict[str, asyncio.Lock] = {}

    def state_key(self, event: AstrMessageEvent) -> str:
        """使用平台与发送者身份的哈希作为隐私友好存储键。"""
        sender = event.get_sender_id() or event.unified_msg_origin
        identity = f"{event.get_platform_id()}:{sender}"
        digest = hashlib.sha256(identity.encode()).hexdigest()[:32]
        return f"user:{digest}"

    @asynccontextmanager
    async def locked(self, event: AstrMessageEvent):
        async with self.locked_by_key(self.state_key(event)):
            yield

    @asynccontextmanager
    async def locked_by_key(self, key: str):
        async with self._locks.setdefault(key, asyncio.Lock()):
            yield

    async def load(self, event: AstrMessageEvent) -> dict[str, Any]:
        return await self.load_by_key(self.state_key(event))

    async def load_by_key(self, key: str) -> dict[str, Any]:
        raw = await self._get_kv_data(key, {})
        return self._normalize(raw)

    def _normalize(self, raw: Any) -> dict[str, Any]:
        state = raw if isinstance(raw, dict) else {}
        goals = state.get("goals") if isinstance(state.get("goals"), list) else []
        clean_goals: list[dict[str, Any]] = []
        for item in goals:
            if (
                not isinstance(item, dict)
                or not item.get("id")
                or not item.get("title")
            ):
                continue
            mode = item.get("mode")
            if mode not in VALID_MODES:
                mode = MODE_CHECKIN
            try:
                target = max(1, int(item.get("daily_target", 1)))
            except (TypeError, ValueError):
                target = 1
            reminder = item.get("reminder")
            reminder = reminder if isinstance(reminder, dict) else {}
            clean_goals.append(
                {
                    **item,
                    "mode": mode,
                    "daily_target": target,
                    "archived": bool(item.get("archived")),
                    "reminder": {
                        "enabled": bool(reminder.get("enabled")),
                        "time": reminder.get("time"),
                        "job_id": reminder.get("job_id"),
                        "umo": reminder.get("umo"),
                        "last_sent_date": reminder.get("last_sent_date"),
                    },
                }
            )
        records = state.get("records") if isinstance(state.get("records"), list) else []
        active_timers = (
            state.get("active_timers")
            if isinstance(state.get("active_timers"), dict)
            else {}
        )
        pending = state.get("pending_image")
        if not isinstance(pending, dict):
            pending = None
        image_messages = state.get("image_messages")
        if not isinstance(image_messages, list):
            image_messages = []
        return {
            "goals": clean_goals,
            "records": [record for record in records if isinstance(record, dict)],
            "active_timers": {
                str(goal_id): started
                for goal_id, started in active_timers.items()
                if isinstance(started, str)
            },
            "pending_image": pending,
            "image_messages": [str(value) for value in image_messages[-100:]],
        }

    def _record_limit(self) -> int:
        try:
            value = int(self._config_value("max_records", DEFAULT_MAX_RECORDS))
        except (TypeError, ValueError):
            value = DEFAULT_MAX_RECORDS
        return max(MIN_MAX_RECORDS, min(MAX_MAX_RECORDS, value))

    async def save(self, event: AstrMessageEvent, state: dict[str, Any]) -> None:
        await self.save_by_key(self.state_key(event), state)

    async def save_by_key(self, key: str, state: dict[str, Any]) -> None:
        records = state.get("records")
        if isinstance(records, list):
            state["records"] = records[-self._record_limit() :]
        image_messages = state.get("image_messages")
        if isinstance(image_messages, list):
            state["image_messages"] = image_messages[-100:]
        await self._put_kv_data(key, state)

    async def update(
        self,
        event: AstrMessageEvent,
        mutator: Callable[[dict[str, Any]], Any],
    ) -> tuple[dict[str, Any], Any]:
        async with self.locked(event):
            state = await self.load(event)
            result = mutator(state)
            if asyncio.iscoroutine(result):
                result = await result
            await self.save(event, state)
            return state, result

    async def clear(self, event: AstrMessageEvent) -> None:
        await self._delete_kv_data(self.state_key(event))

    @staticmethod
    def find_goal(
        state: dict[str, Any], selector: str, *, include_archived: bool = False
    ) -> tuple[dict[str, Any] | None, str | None]:
        """按 ID、完整标题或唯一子串定位目标。"""
        text = (selector or "").strip()
        candidates = [
            goal
            for goal in state["goals"]
            if include_archived or not goal.get("archived")
        ]
        if not text:
            return None, "请指定目标名称。"
        exact_id = [goal for goal in candidates if goal.get("id") == text]
        if exact_id:
            return exact_id[0], None
        exact_title = [goal for goal in candidates if goal.get("title") == text]
        if len(exact_title) == 1:
            return exact_title[0], None
        partial = [goal for goal in candidates if text in str(goal.get("title", ""))]
        if len(partial) == 1:
            return partial[0], None
        if not partial:
            return None, f"没有找到目标“{text}”。"
        names = "、".join(str(goal["title"]) for goal in partial[:5])
        return None, f"“{text}”匹配到多个目标：{names}，请说完整名称。"

    @staticmethod
    def daily_progress(state: dict[str, Any], goal: dict[str, Any], date: str) -> int:
        """返回指定日期的完成次数或分钟数。"""
        goal_id = goal["id"]
        if goal["mode"] == MODE_TIMER:
            return sum(
                int(record.get("minutes", 0))
                for record in state["records"]
                if record.get("goal_id") == goal_id
                and record.get("date") == date
                and record.get("kind") == MODE_TIMER
            )
        return sum(
            int(record.get("count", 0))
            for record in state["records"]
            if record.get("goal_id") == goal_id
            and record.get("date") == date
            and record.get("kind") == MODE_CHECKIN
        )

    def close(self) -> None:
        self._locks.clear()
