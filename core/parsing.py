"""输入与模型 JSON 的容错解析。"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any

from .constants import MODE_CHECKIN, MODE_TIMER


def coerce_int(value: Any, *, field: str = "数值") -> int:
    """容错转换整数，拒绝布尔值和非整数小数。"""
    if isinstance(value, bool):
        raise ValueError(f"{field}必须是整数")  # noqa: TRY004
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value.strip())
    raise ValueError(f"{field}必须是整数")


def normalize_mode(value: str) -> str:
    """把中英文模式名统一为内部枚举。"""
    text = (value or "").strip().lower()
    if text in {"打卡", "每日打卡", "次数", "checkin", "check-in", "count"}:
        return MODE_CHECKIN
    if text in {"计时", "每日计时", "时长", "timer", "time", "duration"}:
        return MODE_TIMER
    raise ValueError("模式必须是“打卡”或“计时”")


def resolve_date(spec: str, current: datetime) -> str:
    """解析今天、昨天、前天或 ISO 日期。"""
    text = (spec or "").strip().lower()
    if text in {"", "today", "今天"}:
        return current.date().isoformat()
    if text in {"yesterday", "昨天"}:
        return (current - timedelta(days=1)).date().isoformat()
    if text == "前天":
        return (current - timedelta(days=2)).date().isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            pass
    raise ValueError("日期格式无法识别")


def parse_clock_time(spec: str) -> str:
    """解析 24 小时制 HH:MM 时间。"""
    text = (spec or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        raise ValueError("时间格式应为 HH:MM，例如 21:00")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("时间需在 00:00～23:59 之间")
    return f"{hour:02d}:{minute:02d}"


def extract_json_object(text: str) -> dict[str, Any]:
    """从模型回复中提取第一个 JSON 对象。"""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        raise ValueError("模型未返回 JSON 对象")
    payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("模型未返回 JSON 对象")  # noqa: TRY004
    return payload
