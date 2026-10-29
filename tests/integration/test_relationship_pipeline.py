"""关系与情绪的自动演化（ 指出的"只进提示、从不更新"缺口）。

原先 record_event 只在手动 API 里调用、AffectService.nudge 从未被调用，
关系永远不变、情绪永远是 calm——即使它们会进入提示，也不是活的上下文。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from ling.infra.db import session_scope
from ling.infra.models import AffectState, OutboxEvent, RelationshipEvent
from ling.worker import handle_relationship_update
from tests.integration.conftest import TestEnv, read_sse

pytestmark = pytest.mark.w0_web


async def _send(client, session_id: str, text: str):
    body = {"client_turn_id": str(uuid.uuid4()), "text": text}
    response = await client.post(f"/v1/sessions/{session_id}/turns", json=body)
    return await read_sse(response)


async def test_completed_turn_enqueues_relationship_update(logged_in: TestEnv) -> None:
    session_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "关系链路"})
    ).json()["session_id"]
    await _send(logged_in.client, session_id, "谢谢你陪我聊天")

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.topic == "relationship.update")
                )
            ).scalars()
        )
    assert rows, "完成的轮次应登记 relationship.update"
    assert dict(rows[-1].payload or {}).get("user_text") == "谢谢你陪我聊天"


@pytest.mark.parametrize(
    ("text", "expected_kind"),
    [
        ("谢谢你，今天聊得很开心", "warmth"),
        ("不要叫我小名，我不喜欢这样", "boundary"),
        ("我叫阿澄，我住在杭州", "disclosure"),
        ("你错了，这让我很失望", "conflict"),
    ],
)
async def test_handler_classifies_interaction(
    logged_in: TestEnv, text: str, expected_kind: str
) -> None:
    turn_id = f"turn_{uuid.uuid4().hex[:8]}"
    await handle_relationship_update(
        {"subject_id": logged_in.subject_id, "turn_id": turn_id, "user_text": text}
    )
    async with session_scope() as session:
        events = list(
            (
                await session.execute(
                    select(RelationshipEvent).where(
                        RelationshipEvent.subject_id == logged_in.subject_id
                    )
                )
            ).scalars()
        )
    assert any(e.kind == expected_kind for e in events), [e.kind for e in events]
    # 每个事件都必须有证据
    assert all(e.source_event_ids for e in events)


async def test_neutral_text_changes_nothing(logged_in: TestEnv) -> None:
    """中性闲聊不改变关系投影——避免"说一句话关系就变"的假拟人。"""
    await handle_relationship_update(
        {
            "subject_id": logged_in.subject_id,
            "turn_id": "turn_neutral",
            "user_text": "今天天气不错",
        }
    )
    async with session_scope() as session:
        events = list(
            (
                await session.execute(
                    select(RelationshipEvent).where(
                        RelationshipEvent.subject_id == logged_in.subject_id
                    )
                )
            ).scalars()
        )
    assert events == []


async def test_warmth_raises_familiarity_and_touches_affect(logged_in: TestEnv) -> None:
    from ling.domains.affect.service import AffectService
    from ling.domains.relationship.service import RelationshipService

    await handle_relationship_update(
        {
            "subject_id": logged_in.subject_id,
            "turn_id": "turn_warm",
            "user_text": "谢谢你，有你真好",
        }
    )
    async with session_scope() as session:
        rel = RelationshipService(session)
        view = rel.view(await rel.state(logged_in.subject_id))
        affect = await AffectService(session).current()
    assert view.familiarity > 0, view.familiarity
    assert affect.intensity > 0, "善意互动应带来短时情绪"

    async with session_scope() as session:
        rows = list((await session.execute(select(AffectState))).scalars())
    assert any(r.emotion == "warm" for r in rows)


async def test_conflict_does_not_grant_anything(logged_in: TestEnv) -> None:
    """情绪与关系只影响措辞，不能放宽权限（设计文档 §6.3）。"""
    from ling.contracts.v1 import ActionIntent, Visibility
    from ling.domains.affect.service import AffectService
    from ling.kernel.policy import PolicyContext, PolicyGate

    await handle_relationship_update(
        {
            "subject_id": logged_in.subject_id,
            "turn_id": "turn_conflict",
            "user_text": "你错了，我很生气",
        }
    )
    async with session_scope() as session:
        affect = await AffectService(session).current()
    assert affect.emotion in {"uneasy", "calm"}

    # 无论情绪如何，未授权的相机仍然被拒
    decision = PolicyGate().evaluate(
        ActionIntent(intent_id="i", kind="perception.camera"),
        PolicyContext(
            subject_id=logged_in.subject_id,
            window_id="win",
            visibility=Visibility.subject_private,
            is_group=False,
        ),
    )
    assert decision.allowed is False


async def test_handler_ignores_empty_payload() -> None:
    await handle_relationship_update({})
    await handle_relationship_update({"subject_id": "s"})


async def test_conflict_signal_takes_priority_over_boundary(logged_in: TestEnv) -> None:
    """"你错了，别这样" 同时含冲突与边界信号：冲突应优先。

    否则会丢掉"信任下降"这个更重要的事实（实测暴露）。
    """
    await handle_relationship_update(
        {
            "subject_id": logged_in.subject_id,
            "window_id": "win_conflict",
            "turn_id": "turn_mixed",
            "user_text": "你错了，我很失望，别再这样了",
        }
    )
    async with session_scope() as session:
        events = list(
            (
                await session.execute(
                    select(RelationshipEvent).where(
                        RelationshipEvent.subject_id == logged_in.subject_id
                    )
                )
            ).scalars()
        )
    kinds = [e.kind for e in events]
    assert "conflict" in kinds, f"混合信号应判为 conflict，实际 {kinds}"
    assert "boundary" not in kinds


async def test_window_affect_written_with_cause(logged_in: TestEnv) -> None:
    """有窗口上下文时写 window 作用域（带原因），另加极弱全局趋势。"""
    from ling.infra.models import AffectState

    await handle_relationship_update(
        {
            "subject_id": logged_in.subject_id,
            "window_id": "win_affect",
            "turn_id": "turn_affect",
            "user_text": "谢谢你，有你真好",
        }
    )
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(AffectState).where(AffectState.actor_id == "actor_ling")
                )
            ).scalars()
        )
    window_rows = [r for r in rows if r.scope == "window"]
    global_rows = [r for r in rows if r.scope == "global"]
    assert window_rows, "应写入窗口局部情绪"
    assert all(r.window_id == "win_affect" for r in window_rows)
    assert all(r.cause for r in window_rows), "窗口局部情绪应带原因"
    assert global_rows, "应同时写入极弱全局趋势"
    assert all(r.intensity <= 0.1 for r in global_rows), "全局趋势必须很弱"
