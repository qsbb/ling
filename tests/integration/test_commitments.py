"""承诺：创建 / 改期 / 履约 / 不可达。

状态文档里"履约/改期专项"一直未做。这些逻辑不需要任何外部账号，
用合成承诺就能完整验证。关键不变量：

- 改期后仍是**同一条**承诺（保留证据链，不新建）
- 只有真正 sent/delivered 才算履行；计划不等于完成
- unsupported（渠道不支持）与 missed（尝试后失败）必须区分——
  前者是"没能尝试"，后者是"尝试了但没送达"
- 已取消的承诺不能靠改期复活
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from ling.domains.goals.service import GoalsService
from ling.infra.db import session_scope
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def _create(
    env: TestEnv, *, due_in: timedelta = timedelta(hours=1), text: str = "晚上提醒吃药"
) -> object:
    async with session_scope() as session:
        return await GoalsService(session).create_commitment(
            subject_id=env.subject_id,
            text=text,
            due_at=datetime.now(UTC) + due_in,
            source_event_ids=["evt_1"],
        )


async def test_create_requires_timezone(logged_in: TestEnv) -> None:
    """无时区的到期时间必须拒绝——否则"晚上八点"在哪个时区都说不清。"""
    with pytest.raises(ValueError, match="时区"):
        async with session_scope() as session:
            await GoalsService(session).create_commitment(
                subject_id=logged_in.subject_id,
                text="x",
                due_at=datetime(2030, 1, 1, 20, 0),  # naive
            )


async def test_create_is_active(logged_in: TestEnv) -> None:
    view = await _create(logged_in)
    assert view.state == "active"


async def test_reschedule_keeps_same_commitment(logged_in: TestEnv) -> None:
    """改期不新建承诺——否则证据链断裂，"他答应了什么"就查不到。"""
    original = await _create(logged_in)
    new_due = datetime.now(UTC) + timedelta(days=2)
    async with session_scope() as session:
        updated = await GoalsService(session).reschedule(original.commitment_id, new_due)
    assert updated.commitment_id == original.commitment_id
    assert updated.due_at == new_due
    assert updated.state == "active"

    async with session_scope() as session:
        all_of_them = await GoalsService(session).list_commitments(logged_in.subject_id)
    assert len(all_of_them) == 1, "改期产生了新承诺记录"


async def test_reschedule_clears_previous_delivery(logged_in: TestEnv) -> None:
    """改期后旧的投递结果必须清空，否则会被当成"已送达"。"""
    original = await _create(logged_in)
    async with session_scope() as session:
        await GoalsService(session).mark_delivery(
            original.commitment_id, operation_id="op_1", delivery_state="delivered"
        )
    async with session_scope() as session:
        view = await GoalsService(session).reschedule(
            original.commitment_id, datetime.now(UTC) + timedelta(days=3)
        )
    assert view.state == "active"
    assert view.delivery_operation_id is None
    assert view.delivery_state is None


async def test_reschedule_rejects_naive_time(logged_in: TestEnv) -> None:
    original = await _create(logged_in)
    with pytest.raises(ValueError, match="时区"):
        async with session_scope() as session:
            await GoalsService(session).reschedule(
                original.commitment_id, datetime(2030, 1, 1, 9, 0)
            )


async def test_cancel_commitment(logged_in: TestEnv) -> None:
    """取消承诺：此前 cancelled 是条**死状态**——声明在
    COMMITMENT_STATES 里、reschedule 也在检查它，却没有任何路径能设置。

    用户因此无法撤回自己许下的承诺。
    """
    original = await _create(logged_in)
    async with session_scope() as session:
        view = await GoalsService(session).cancel(
            original.commitment_id, reason="计划变了"
        )
    assert view.state == "cancelled"
    assert view.cancel_reason == "计划变了"


async def test_cancel_is_idempotent(logged_in: TestEnv) -> None:
    original = await _create(logged_in)
    async with session_scope() as session:
        await GoalsService(session).cancel(original.commitment_id, reason="第一次")
    async with session_scope() as session:
        again = await GoalsService(session).cancel(original.commitment_id)
    assert again.state == "cancelled"


async def test_cancelled_commitment_cannot_be_rescheduled(logged_in: TestEnv) -> None:
    """取消是终态，不能靠改期复活。"""
    original = await _create(logged_in)
    async with session_scope() as session:
        await GoalsService(session).cancel(original.commitment_id)
    with pytest.raises(ValueError, match="取消"):
        async with session_scope() as session:
            await GoalsService(session).reschedule(
                original.commitment_id, datetime.now(UTC) + timedelta(days=1)
            )


async def test_delivered_commitment_cannot_be_cancelled(logged_in: TestEnv) -> None:
    """已经做到的承诺不能"取消"——那会抹掉事实。"""
    original = await _create(logged_in)
    async with session_scope() as session:
        await GoalsService(session).mark_delivery(
            original.commitment_id, operation_id="op_1", delivery_state="delivered"
        )
    with pytest.raises(ValueError, match="已履行"):
        async with session_scope() as session:
            await GoalsService(session).cancel(original.commitment_id)


async def test_cancelled_not_returned_as_due(logged_in: TestEnv) -> None:
    """取消后不再被调度（否则会给用户发一条已撤回的提醒）。"""
    original = await _create(logged_in, due_in=timedelta(minutes=-5))
    async with session_scope() as session:
        await GoalsService(session).cancel(original.commitment_id)
    async with session_scope() as session:
        due = await GoalsService(session).due_commitments()
    assert original.commitment_id not in {c.commitment_id for c in due}


async def test_reschedule_unknown_commitment_raises(logged_in: TestEnv) -> None:
    with pytest.raises(KeyError):
        async with session_scope() as session:
            await GoalsService(session).reschedule(
                "cmt_does_not_exist", datetime.now(UTC) + timedelta(days=1)
            )


@pytest.mark.parametrize(
    ("delivery_state", "expected_state"),
    [
        ("sent", "delivered"),
        ("delivered", "delivered"),
        ("unsupported", "undeliverable"),
        ("failed", "missed"),
    ],
)
async def test_delivery_state_mapping(
    logged_in: TestEnv, delivery_state: str, expected_state: str
) -> None:
    """投递结果 → 承诺状态。不可达与错过必须区分：
    前者是"渠道根本不支持"（不是角色的错），后者是"尝试了但没送达"。
    """
    original = await _create(logged_in)
    async with session_scope() as session:
        view = await GoalsService(session).mark_delivery(
            original.commitment_id,
            operation_id="op_x",
            delivery_state=delivery_state,
        )
    assert view.state == expected_state


async def test_planned_is_not_done(logged_in: TestEnv) -> None:
    """计划不等于完成：没投递结果就还是 active。"""
    original = await _create(logged_in)
    assert original.state == "active"
    assert original.delivery_state is None


async def test_due_commitments_only_returns_active_and_due(logged_in: TestEnv) -> None:
    past = await _create(logged_in, due_in=timedelta(minutes=-5), text="已到期")
    future = await _create(logged_in, due_in=timedelta(days=3), text="还没到")
    delivered = await _create(logged_in, due_in=timedelta(minutes=-5), text="已履行")
    async with session_scope() as session:
        await GoalsService(session).mark_delivery(
            delivered.commitment_id, operation_id="op_d", delivery_state="delivered"
        )

    async with session_scope() as session:
        due = await GoalsService(session).due_commitments()
    ids = {c.commitment_id for c in due}
    assert past.commitment_id in ids, "到期的 active 承诺未被调度"
    assert future.commitment_id not in ids, "未到期的承诺被提前调度"
    assert delivered.commitment_id not in ids, "已履行的承诺被重复调度"


async def test_due_commitments_within_window(logged_in: TestEnv) -> None:
    """提前量：快到期的也要能被提前看到（用于准备，不是提前发）。"""
    soon = await _create(logged_in, due_in=timedelta(minutes=30))
    async with session_scope() as session:
        within_hour = await GoalsService(session).due_commitments(
            within=timedelta(hours=1)
        )
    assert soon.commitment_id in {c.commitment_id for c in within_hour}


async def test_cancel_endpoint(logged_in: TestEnv) -> None:
    """HTTP 端点：取消承诺并返回原因。"""
    original = await _create(logged_in)
    response = await logged_in.client.post(
        f"/v1/commitments/{original.commitment_id}/cancel",
        json={"reason": "改主意了"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "cancelled"
    assert body["cancel_reason"] == "改主意了"


async def test_cancel_endpoint_404_for_unknown(logged_in: TestEnv) -> None:
    response = await logged_in.client.post(
        "/v1/commitments/cmt_nope/cancel", json={"reason": "x"}
    )
    assert response.status_code == 404


async def test_cancel_endpoint_422_for_delivered(logged_in: TestEnv) -> None:
    """已履行的承诺取消 → 422（不是 500）。"""
    original = await _create(logged_in)
    async with session_scope() as session:
        await GoalsService(session).mark_delivery(
            original.commitment_id, operation_id="op_1", delivery_state="delivered"
        )
    response = await logged_in.client.post(
        f"/v1/commitments/{original.commitment_id}/cancel", json={}
    )
    assert response.status_code == 422, response.text


# ------------------------------------------------------------------ 到期投递（此前实现的缺口）


async def test_due_commitment_gets_dispatch_state(logged_in: TestEnv) -> None:
    """到期的承诺必须被推进状态，不能永远停在 active。

    此前实现的真实缺口：due_commitments() 存在，但 worker 从没调用它，
    承诺能创建/查询/取消，**却永远不会触发**——用户当提醒器用就会静默失灵。
    """
    from ling.worker import handle_commitment_dispatch

    original = await _create(logged_in, due_in=timedelta(minutes=-5))

    async with session_scope() as session:
        before = await GoalsService(session).current_commitment(original.commitment_id)
    assert before.state == "active", "前提：初始应为 active"

    await handle_commitment_dispatch({})

    async with session_scope() as session:
        after = await GoalsService(session).current_commitment(original.commitment_id)
    assert after.state != "active", "到期承诺没有被推进（静默失灵）"


async def test_no_proactive_channel_marks_undeliverable(logged_in: TestEnv) -> None:
    """没有主动推送渠道时应标 undeliverable，而不是假装发了。

    `undeliverable` 与 `missed` 必须区分：
    前者是"根本没能力发"（不是角色的错），后者是"尝试了但没送达"。
    """
    from ling.worker import handle_commitment_dispatch

    original = await _create(logged_in, due_in=timedelta(minutes=-1))
    await handle_commitment_dispatch({})  # 不传 proactive_channel

    async with session_scope() as session:
        view = await GoalsService(session).current_commitment(original.commitment_id)
    assert view.state == "undeliverable"
    assert view.delivery_state == "unsupported"


async def test_channel_without_connector_is_not_marked_sent(logged_in: TestEnv) -> None:
    """只是"传了渠道名"不能算已发送。

    原实现只要 payload 里有 `proactive_channel` 就直接 `delivery_state="sent"`，
    却**没有真的发任何消息**——违反项目自己的硬规则"没有回执不声称已发送"，
    用户会以为提醒到了，实际什么都没发生（而且静默）。

    现在只有真实投递成功才记 sent；没有可用连接器时如实标 unsupported。
    """
    from ling.worker import handle_commitment_dispatch

    original = await _create(logged_in, due_in=timedelta(minutes=-1))
    await handle_commitment_dispatch({"proactive_channel": "qq"})

    async with session_scope() as session:
        view = await GoalsService(session).current_commitment(original.commitment_id)
    assert view.delivery_state != "sent", "没有真实投递却记成了已发送"
    assert view.state in {"undeliverable", "failed"}


async def test_future_commitment_untouched(logged_in: TestEnv) -> None:
    """未到期的不动（不能提前发）。"""
    from ling.worker import handle_commitment_dispatch

    original = await _create(logged_in, due_in=timedelta(days=3))
    await handle_commitment_dispatch({})
    async with session_scope() as session:
        view = await GoalsService(session).current_commitment(original.commitment_id)
    assert view.state == "active"


async def test_dispatch_is_scheduled(logged_in: TestEnv) -> None:
    """维护循环必须真的投递 commitment.dispatch，否则 handler 是摆设。"""
    from sqlalchemy import delete

    from ling.infra.models import OutboxEvent
    from ling.worker import enqueue_maintenance

    async with session_scope() as session:
        await session.execute(
            delete(OutboxEvent).where(OutboxEvent.topic == "commitment.dispatch")
        )
    await enqueue_maintenance()
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.topic == "commitment.dispatch")
                )
            ).scalars()
        )
    assert rows, "维护循环没有投递承诺投递任务"


async def test_dispatch_sends_through_connector(logged_in: TestEnv) -> None:
    """有可用连接器时必须**真的发**，并只在成功后记 sent。

    这是"没有回执不声称已发送"的正面验证——之前只测了没渠道的情况，
    有渠道的分支直接写 sent 而没有发送动作，测试也没发现。
    """
    import httpx
    from connectors.onebot.connector import OneBotV11Connector
    from connectors.onebot.settings import OneBotConfig

    from ling.worker import handle_commitment_dispatch

    sent: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        import json

        sent.append(json.loads(request.content))
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": 1}}
        )

    connector = OneBotV11Connector(
        OneBotConfig(
            api_base="http://onebot.test:5700",
            access_token="t",
            self_id="1",
            instance_id="commit-test",
            supports_proactive=True,
        ),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    import ling.capabilities.channels as channels_mod

    original = channels_mod.proactive_connector
    channels_mod.proactive_connector = lambda: connector  # type: ignore[assignment]
    try:
        commitment = await _create(logged_in, due_in=timedelta(minutes=-1))
        await handle_commitment_dispatch(
            {"proactive_window_ref": "commit-test:private:12345"}
        )
    finally:
        channels_mod.proactive_connector = original  # type: ignore[assignment]

    assert sent, "承诺到期却没有任何实际发送"
    assert sent[0]["message"], "发送内容为空"

    async with session_scope() as session:
        view = await GoalsService(session).current_commitment(commitment.commitment_id)
    assert view.delivery_state == "sent"
    assert view.state == "delivered"
