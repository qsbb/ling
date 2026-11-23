"""Offer one authorized Windows writing activity to the character."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from ling.config import Settings
from ling.domains.agenda.timeline import (
    BoundarySegment,
    BoundaryView,
    boundary_view,
    build_persona_projection,
)
from ling.domains.desktop import service as desktop
from ling.infra.db import session_scope
from ling.infra.models import OutboxEvent, Persona
from ling.infra.security import SecretBox
from ling.models.router import ModelRouter
from ling.ports.config import EffectiveConfig
from ling.ports.model import ChatMessageIn, ModelError
from ling.settings.service import ConfigService

AUTONOMY_TOPIC = "desktop.activity.decide"
_WRITE_SLOTS = frozenset({"free", "hobby", "writing", "write"})
_PROMPT = (
    "现在可以在已授权的 Windows 记事本写一条笔记，也可以休息。"
    "根据你的人设和眼下的安排，选你现在想做的事。"
    '只返回 JSON 对象 {"action":"write"} 或 {"action":"rest"}。'
)


def _resting(view: BoundaryView) -> bool:
    return view.sleep is not None or (view.active_leaf is not None and view.active_leaf.kind == "rest")


def _writing_slot(view: BoundaryView) -> BoundarySegment | None:
    leaf = view.active_leaf
    if _resting(view) or view.commitment is not None or leaf is None or leaf.kind not in _WRITE_SLOTS:
        return None
    return leaf


async def offer_activity(
    session: AsyncSession, *, actor_id: str, view: BoundaryView, now: datetime,
) -> None:
    resting = _resting(view)
    slot = _writing_slot(view)
    if not resting and slot is None:
        return
    device = await desktop.lock_device(session, actor_id)
    if device is None or not device.token_hash or device.paused:
        return
    grant = desktop.current_grant(device, now)
    if grant is None or now < grant.start_at:
        return
    if resting and grant.decision in {"pending", "deciding"}:
        grant.decision = "rest"
        desktop.store_grant(device, grant)
    if (
        slot is None or grant.decision != "pending" or not desktop.ready(device, now)
        or await desktop.active_task(session, device, now) is not None or device.paused
    ):
        return

    event_id = f"evt_desktop_{grant.request_id.hex}"
    if await session.get(OutboxEvent, event_id) is not None:
        grant.decision = "unavailable"
        desktop.store_grant(device, grant)
        return
    grant.decision = "deciding"
    desktop.store_grant(device, grant)
    session.add(OutboxEvent(
        id=event_id, topic=AUTONOMY_TOPIC, available_at=now, created_at=now,
        payload={"request_id": str(grant.request_id), "segment_id": slot.segment_id},
    ))


async def _choose(
    router: ModelRouter, config: EffectiveConfig, context: str,
) -> Literal["write", "rest", "unavailable"]:
    text = ""
    try:
        async with asyncio.timeout(8):
            async for chunk, outcome in router.stream(
                config, profile="dialogue.fast", allow_fallback=False,
                messages=[
                    ChatMessageIn(role="system", content=_PROMPT),
                    ChatMessageIn(role="user", content=context),
                ],
            ):
                if outcome.error is not None or len(text) + len(chunk.delta or "") > 512:
                    return "unavailable"
                text += chunk.delta or ""
        result = json.loads(text)
    except (ModelError, TimeoutError, ValueError, RecursionError):
        return "unavailable"
    action = result.get("action") if isinstance(result, dict) else None
    if action == "write":
        return "write"
    return "rest" if action == "rest" else "unavailable"


async def decide_activity(
    settings: Settings, router: ModelRouter | None, *, request_id: str, segment_id: str,
    clock: Callable[[], datetime],
) -> None:
    actor_id = settings.actor_id
    async with session_scope() as session:
        now = clock()
        device = await desktop.lock_device(session, actor_id)
        if device is None:
            return
        grant = desktop.current_grant(device, now)
        if grant is None or str(grant.request_id) != request_id or grant.decision != "deciding":
            return
        slot = _writing_slot(await boundary_view(session, actor_id=actor_id, now=now))
        config: EffectiveConfig | None = None
        context = ""
        if (
            router is not None and slot is not None and slot.segment_id == segment_id
            and not device.paused and desktop.ready(device, now)
        ):
            config_service = ConfigService(
                session, SecretBox(settings), test_mode_default=settings.test_mode, persona_id=settings.persona_id,
            )
            config = await config_service.effective()
            persona = await session.get(Persona, config.persona_id)
            traits = await build_persona_projection(session, config.persona_id, actor_id=actor_id, now=now)
            context = json.dumps({
                "时段": slot.title,
                "人设": {
                    "角色设定": (persona.system_prompt or "")[:1200] if persona else "",
                    "性格": traits.personality, "兴趣": traits.interests, "约束": traits.boundaries,
                },
            }, ensure_ascii=False)

    choice = await _choose(router, config, context) if router is not None and config is not None else "unavailable"

    async with session_scope() as session:
        now = clock()
        device = await desktop.lock_device(session, actor_id)
        if device is None:
            return
        grant = desktop.current_grant(device, now)
        if grant is None or str(grant.request_id) != request_id or grant.decision != "deciding":
            return
        view = await boundary_view(session, actor_id=actor_id, now=now)
        slot = _writing_slot(view)
        if _resting(view) or choice == "rest":
            grant.decision = "rest"
        elif choice == "write" and slot is not None and slot.segment_id == segment_id:
            await desktop.queue_granted_write(
                session, device, grant, now=now, segment_id=slot.segment_id, segment_end=slot.end_at,
            )
            return
        else:
            grant.decision = "unavailable"
        desktop.store_grant(device, grant)
