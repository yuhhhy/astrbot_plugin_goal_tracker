"""图片与已有目标的多模态匹配流程。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import astrbot.api.message_components as Comp
from astrbot.api import logger

from .constants import MODE_CHECKIN, MODE_LABELS
from .goals import record_checkin
from .parsing import extract_json_object

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

    from .state_store import GoalStateStore


def _parse_matches(
    response_text: str, goals_by_id: dict[str, dict[str, Any]]
) -> list[dict[str, str]]:
    payload = extract_json_object(response_text)
    raw_matches = payload.get("matches")
    if not isinstance(raw_matches, list):
        return []
    matches: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_matches:
        if not isinstance(item, dict):
            continue
        goal_id = str(item.get("goal_id") or "")
        confidence = str(item.get("confidence") or "").lower()
        if goal_id not in goals_by_id or goal_id in seen or confidence != "high":
            continue
        seen.add(goal_id)
        matches.append(
            {
                "goal_id": goal_id,
                "evidence": str(item.get("evidence") or "")[:200],
            }
        )
    return matches


async def handle_images(
    event: AstrMessageEvent,
    *,
    context: Any,
    store: GoalStateStore,
    config_value: Any,
    now: Any,
) -> None:
    """识别图片；单个打卡目标自动记录，计时目标则询问。"""
    if not config_value("enable_image_recognition", True):
        return
    images = [
        component
        for component in event.get_messages()
        if isinstance(component, Comp.Image)
    ]
    if not images:
        return
    state = await store.load(event)
    goals = [goal for goal in state["goals"] if not goal.get("archived")]
    if not goals:
        return
    message_id = str(getattr(event.message_obj, "message_id", ""))
    if message_id and message_id in state["image_messages"]:
        event.stop_event()
        return
    try:
        provider_id = await context.get_current_chat_provider_id(
            event.unified_msg_origin
        )
        image_paths = [await image.convert_to_file_path() for image in images]
        compact_goals = [
            {
                "id": goal["id"],
                "title": goal["title"],
                "description": goal.get("description", ""),
                "mode": goal["mode"],
            }
            for goal in goals
        ]
        response = await context.llm_generate(
            chat_provider_id=provider_id,
            image_urls=image_paths,
            prompt=(
                "判断图片是否能明确证明发送者正在或已经执行下列某个目标。"
                "只能根据图片中可见证据匹配；纯文字海报、网图、成果不明确、"
                "或无法确定是发送者行为时不要匹配。仅输出 JSON，格式："
                '{"matches":[{"goal_id":"abc123","confidence":"high|medium|low",'
                '"evidence":"可见证据简述"}]}。'
                "只有证据直接、几乎无歧义时才使用 high；无匹配时 matches 为空数组。"
                f"\n用户附带文字：{(event.message_str or '')[:500]}"
                f"\n活跃目标：{json.dumps(compact_goals, ensure_ascii=False)}"
            ),
        )
        goals_by_id = {goal["id"]: goal for goal in goals}
        matches = _parse_matches(response.completion_text, goals_by_id)
    except Exception as exc:  # noqa: BLE001 - 模型/平台异常类型不统一
        logger.warning("目标图片识别失败，已跳过：%s", exc)
        return

    if not matches:
        if message_id:
            await store.update(
                event,
                lambda fresh: fresh["image_messages"].append(message_id),
            )
        return

    event.stop_event()
    matched_goals = [goals_by_id[item["goal_id"]] for item in matches]
    if len(matched_goals) == 1 and matched_goals[0]["mode"] == MODE_CHECKIN:
        goal = matched_goals[0]
        result = await record_checkin(
            event,
            store=store,
            now=now,
            selector=goal["id"],
            count=1,
            note=f"图片识别：{matches[0]['evidence']}",
            source="image",
            source_message_id=message_id,
        )
        await event.send(event.make_result().message(f"📷 {result}"))
        return

    def save_pending(fresh: dict[str, Any]) -> None:
        fresh["pending_image"] = {
            "goal_ids": [goal["id"] for goal in matched_goals],
            "created_at": now().isoformat(),
            "source_message_id": message_id or None,
        }
        if message_id:
            fresh["image_messages"].append(message_id)

    await store.update(event, save_pending)
    if len(matched_goals) == 1:
        goal = matched_goals[0]
        await event.send(
            event.make_result().message(
                f"📷 图片看起来与计时目标“{goal['title']}”相关。"
                "要帮你记录吗？请告诉我时长，例如“记录 30 分钟”。"
            )
        )
        return
    options = "\n".join(
        f"{index}. {goal['title']}（{MODE_LABELS[goal['mode']]}）"
        for index, goal in enumerate(matched_goals, start=1)
    )
    await event.send(
        event.make_result().message(
            f"📷 图片可能与多个目标相关：\n{options}\n"
            "请说明要记录哪个目标；计时目标请同时告诉我时长。"
        )
    )
