"""Web 主动投递：没有外部渠道时，她也能主动开口。

部署实例上 qqbot 拿不到主动推送权限、OneBot 没启用——如果没有这条路，
"主动联系"永远只是代码里的空转，她永远不会先说话。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.w0_web


class _Gate:
    allowed = True
    reason = ""


class _Dialogue:
    proactive_delivery_enabled = True


class _Effective:
    dialogue = _Dialogue()


class _Config:
    async def effective(self):  # noqa: ANN201
        return _Effective()


async def _seed(web_env, *, draft: str = "在忙吗？刚试了个新摊子") -> str:
    from ling.infra.db import session_scope
    from ling.infra.models import ProactiveOpportunity
    from ling.infra.security import new_id

    async with session_scope() as session:
        opportunity_id = new_id("opp")
        session.add(
            ProactiveOpportunity(
                id=opportunity_id,
                actor_id=web_env.settings.actor_id,
                subject_id=web_env.subject_id,
                kind="check_in",
                reason="connector_of_thought",
                state="candidate",
                expires_at=datetime.now(UTC) + timedelta(hours=2),
                draft_text=draft,
            )
        )
        await session.commit()
    return opportunity_id


async def test_proactive_message_lands_in_web_session(web_env, logged_in, monkeypatch) -> None:
    """草稿要真的变成她的一条消息。"""
    from ling.domains.proactive import dispatch as dispatch_mod
    from ling.domains.proactive.service import ProactivityService

    async def _allow(self, **_kwargs):  # noqa: ANN001, ANN202
        return _Gate()

    monkeypatch.setattr(ProactivityService, "gate", _allow)

    # **先有会话**：投递目标是"最近的私聊窗口"，没有窗口就只能 rejected。
    await logged_in.client.post("/v1/sessions", json={"title": "主动投递"})
    opportunity_id = await _seed(web_env)

    from ling.infra.db import session_scope

    async with session_scope() as session:
        stats = await dispatch_mod.dispatch_pending(
            session,
            actor_id=web_env.settings.actor_id,
            connector=None,
            config_reader=_Config(),
            deliver_to_web=True,
        )
    assert stats["sent"] == 1, stats

    # 她的话真的落在 Web 会话里
    sessions = (await logged_in.client.get("/v1/sessions")).json()["sessions"]
    bodies = []
    for s in sessions:
        msgs = (
            await logged_in.client.get(f"/v1/sessions/{s['session_id']}/messages")
        ).json()["messages"]
        bodies.extend(m["text"] for m in msgs)
    assert any("新摊子" in b for b in bodies), bodies

    # 候选状态要推进，避免下一轮重复投递
    from sqlalchemy import select

    from ling.infra.models import ProactiveOpportunity

    async with session_scope() as session:
        row = (
            await session.execute(
                select(ProactiveOpportunity).where(
                    ProactiveOpportunity.id == opportunity_id
                )
            )
        ).scalars().first()
    assert row is not None and row.state == "sent", row.state if row else None


async def test_web_delivery_is_off_unless_asked(web_env, logged_in, monkeypatch) -> None:
    """不给 deliver_to_web 时保持原样：没有渠道就什么都不做。"""
    from ling.domains.proactive import dispatch as dispatch_mod
    from ling.domains.proactive.service import ProactivityService
    from ling.infra.db import session_scope

    async def _allow(self, **_kwargs):  # noqa: ANN001, ANN202
        return _Gate()

    monkeypatch.setattr(ProactivityService, "gate", _allow)
    await logged_in.client.post("/v1/sessions", json={"title": "不该投递"})
    await _seed(web_env, draft="不该出现的消息")

    async with session_scope() as session:
        stats = await dispatch_mod.dispatch_pending(
            session,
            actor_id=web_env.settings.actor_id,
            connector=None,
            config_reader=_Config(),
        )
    assert stats["sent"] == 0
