from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from ling.config import Settings
from ling.domains.identity.service import IdentityService
from ling.infra.db import session_scope
from ling.infra.models import ConnectorInstance, OutboxEvent, utcnow
from ling.infra.security import SecretBox, new_id
from ling.kernel.live.events import CUSTOM_SCOPE
from ling.kernel.live.room import LiveConflict, LiveForbidden, active_session, open_session
from ling.models.router import ModelRouter
from ling.ports.model import ChatMessageIn
from ling.settings.service import ConfigService

logger = logging.getLogger(__name__)


def authorization(connector: ConnectorInstance) -> dict | None:
    config = connector.custom_config or {}
    window = config.get("autonomous_window")
    if not (
        connector.enabled and config.get("obs_control") and config.get("stage_token_hash")
        and window and datetime.fromisoformat(window["starts_at"]) <= utcnow()
        < datetime.fromisoformat(window["ends_at"])
    ):
        return None
    return window


def prepare_autonomy(
    session: AsyncSession, connector: ConnectorInstance, *, device_id: str, broadcast_ready: bool,
) -> str:
    window = authorization(connector)
    config = connector.custom_config or {}
    if window is None or config.get("autonomous_state"):
        return "hold"
    if not broadcast_ready:
        return "prepare"
    offered_at = utcnow().isoformat()
    connector.custom_config = {**config, "autonomous_state": "deciding", "autonomous_offer": offered_at}
    session.add(OutboxEvent(
        id=new_id("evt"), topic="livestream.decide",
        payload={"instance_id": connector.instance_id, "device_id": device_id, "offer": offered_at},
    ))
    return "hold"


def _offered(connector: ConnectorInstance | None, offer: str) -> bool:
    return bool(
        connector is not None and authorization(connector)
        and connector.custom_config.get("autonomous_state") == "deciding"
        and connector.custom_config.get("autonomous_offer") == offer
    )


async def consider_start(
    settings: Settings, router: ModelRouter | None, *, instance_id: str, device_id: str, offer: str,
) -> None:
    should_start = False
    decision = "wait"
    key = (CUSTOM_SCOPE, instance_id)
    try:
        async with asyncio.timeout(12), session_scope() as session:
            connector = await session.get(ConnectorInstance, key)
            if not _offered(connector, offer):
                return
            if router is not None and await active_session(session, settings.actor_id) is None:
                config = ConfigService(
                    session, SecretBox(settings), test_mode_default=settings.test_mode,
                    persona_id=settings.persona_id,
                )
                result = []
                async for chunk, outcome in router.stream(
                    await config.effective(),
                    profile="dialogue.fast",
                    messages=[
                        ChatMessageIn(
                            role="system",
                            content="你是直播主持人。现在是所有者预先授权的开播时段。"
                            "按公开主题决定是否想开场，只回复「开播」或「等待」。",
                        ),
                        ChatMessageIn(role="user", content=connector.custom_config["public_topic"]),
                    ],
                ):
                    if outcome.error is not None:
                        raise outcome.error
                    if chunk.delta:
                        result.append(chunk.delta)
                should_start = "".join(result).strip(" \n\t。") == "开播"
    except Exception as exc:
        logger.warning("自主开播决策失败，类型=%s", type(exc).__name__)
        decision = "unavailable"

    async with session_scope() as session:
        connector = await session.get(ConnectorInstance, key, with_for_update=True)
        if not _offered(connector, offer):
            return
        if should_start and utcnow() - datetime.fromisoformat(offer) <= timedelta(seconds=30):
            owner = await IdentityService(session).owner_subject_id()
            if owner and await active_session(session, settings.actor_id) is None:
                try:
                    run = await open_session(
                        session, connector, actor_id=settings.actor_id, persona_id=settings.persona_id,
                        subject_id=owner, mode="broadcast", device_id=device_id,
                        ends_at=datetime.fromisoformat(authorization(connector)["ends_at"]),
                    )
                except (LiveConflict, LiveForbidden):
                    decision = "wait"
                else:
                    run.runtime = {**run.runtime, "autonomous": True}
                    decision = "started"
        connector.custom_config = {**connector.custom_config, "autonomous_state": decision}
