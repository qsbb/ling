"""同平台标识下的撤回只能落在可信来源实例的窗口。"""

from __future__ import annotations

import asyncio

import pytest

from ling.domains.conversation.service import ConversationService
from ling.infra.db import session_scope
from ling.infra.models import ConnectorInstance, IdempotencyKey, Message
from ling.kernel.context import PLATFORM_MESSAGE_ID_SCOPE
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def test_concurrent_event_claims_are_atomic_and_instance_scoped(
    logged_in: TestEnv,
) -> None:
    async def claim(scope: str) -> str | None:
        async with session_scope() as session:
            return await ConversationService(
                session, actor_id=logged_in.settings.actor_id
            ).claim_event(scope=scope, key="shared-event-id")

    alpha_scope = "connector:onebot:alpha"
    results = await asyncio.gather(claim(alpha_scope), claim(alpha_scope))
    assert sorted(result is None for result in results) == [False, True]
    assert await claim("connector:onebot:bravo") is None


async def test_recall_selects_window_by_stable_connector_instance(
    logged_in: TestEnv,
) -> None:
    reference = "onebot-shared:private:target"
    platform_message_id = "same-platform-message-id"
    async with session_scope() as session:
        for instance_id in ("alpha", "bravo"):
            session.add(
                ConnectorInstance(
                    scope="onebot",
                    instance_id=instance_id,
                    display_name=instance_id,
                    enabled=True,
                )
            )
        await session.flush()

        conversation = ConversationService(session, actor_id=logged_in.settings.actor_id)
        windows = {}
        messages = {}
        for instance_id in ("alpha", "bravo"):
            window = await conversation.create_window(
                subject_id=logged_in.subject_id,
                title=reference,
                channel="onebot",
            )
            window.connector_window_ref = reference
            window.origin_scope = "onebot"
            window.origin_instance_id = instance_id
            window.origin_status = "known"
            messages[instance_id] = await conversation.add_message(
                window.id, role="user", text=f"窗口 {instance_id} 的消息"
            )
            windows[instance_id] = window.id
            session.add(
                IdempotencyKey(
                    id=f"idem_{instance_id}",
                    scope=PLATFORM_MESSAGE_ID_SCOPE,
                    key=messages[instance_id].message_id,
                    result_ref=platform_message_id,
                )
            )

    async with session_scope() as session:
        outcome = await ConversationService(
            session, actor_id=logged_in.settings.actor_id
        ).retract_message(
            window_ref=reference,
            platform_message_id=platform_message_id,
            trusted_instance_key=("onebot", "alpha"),
        )
        assert outcome.status == "retracted"

    async with session_scope() as session:
        alpha = await session.get(Message, messages["alpha"].message_id)
        bravo = await session.get(Message, messages["bravo"].message_id)
        assert alpha is not None and alpha.retracted_at is not None
        assert bravo is not None and bravo.retracted_at is None
        assert alpha.window_id == windows["alpha"]
        assert bravo.window_id == windows["bravo"]
