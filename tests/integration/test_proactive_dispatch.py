"""主动投递：真的发出去，但**默认关闭**。

投递是这条链路里唯一会真的打扰人的一步。前面几步（收集、门控、生成草稿）
都能在影子模式下验证，投递不行——一旦发生就是"她突然找你"。

所以默认 `proactive_delivery_enabled=False`，只生成不发送。

四条硬规则：

1. **默认关**，用户确认过草稿再自己打开
2. **只发私聊**：主动找你时往群里发一句"在吗"是不可接受的
3. **只有真正的送达才标 sent**（`DeliveryOutcome.delivered`）；
   结果不确定标 `uncertain` 等对账（`failed` 只留给"确定没发出去"），
   **不盲目重发**——重复发比没发更糟
4. **门控没过就留着**，不标 rejected：那只是"现在还不是时候"
5. **异常也要就地结算**：投递可能"平台已收到、然后才抛异常"，此时若机会
   仍留在 candidate，下一轮就会把一条不可撤回的消息**再发一遍**
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from ling.domains.proactive.dispatch import (
    PROACTIVE_DISPATCH_SCOPE,
    STALE_CLAIM_SECONDS,
    dispatch_pending,
)
from ling.infra.db import session_scope
from ling.infra.models import (
    DeliveryReceiptRow,
    IdempotencyKey,
    Operation,
    ProactiveOpportunity,
    Window,
)
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"
SUBJ = "subj_dispatch_test"
# UTC 04:00 = 上海 12:00：稳稳在勿扰时段（23:00-08:00）之外，
# 测试结果不该依赖"现在几点"。
SAFE_NOW = datetime(2030, 6, 1, 4, 0, tzinfo=UTC)
TZ = "Asia/Shanghai"


class _Caps:
    enabled = True
    supports_group = True
    supports_private = True
    supports_proactive = True
    supports_media = False
    max_text_length = 4000


class _Connector:
    """只记录发送，不发网络请求。"""

    connector_id = "fake"
    instance_id = "fake:test"

    def __init__(self, *, state: str = "delivered") -> None:
        from ling.contracts.v1 import DeliveryState

        self.sent: list[object] = []
        self._state = DeliveryState(state)

    def capabilities(self):  # noqa: ANN202
        return _Caps()

    async def send(self, message):  # noqa: ANN001, ANN202
        from ling.contracts.v1 import DeliveryReceipt

        self.sent.append(message)
        return DeliveryReceipt(
            receipt_id=f"rcpt_{len(self.sent)}",
            operation_id=message.operation_id,
            channel=self.connector_id,
            state=self._state,
            platform_message_id=f"pm_{len(self.sent)}",
            observed_at=datetime.now(UTC),
            detail=None,
        )

    async def verify_inbound(self, headers, body):  # noqa: ANN001, ANN202
        return True


class _VoiceCaps(_Caps):
    supports_media = True

    def media_supported(self, modality):  # noqa: ANN001, ANN202
        return True


class _VoiceConnector(_Connector):
    def capabilities(self):  # noqa: ANN202
        return _VoiceCaps()


class _Config:
    def __init__(self, *, enabled: bool = False) -> None:
        self._enabled = enabled

    async def effective(self):  # noqa: ANN202
        return SimpleNamespace(
            dialogue=SimpleNamespace(
                proactive_delivery_enabled=self._enabled,
                proactive_daily_limit=2,
                quiet_start=(23, 0),
                quiet_end=(8, 0),
            )
        )


async def _clean() -> None:
    from sqlalchemy import delete

    async with session_scope() as session:
        await session.execute(
            delete(ProactiveOpportunity).where(ProactiveOpportunity.actor_id == ACTOR)
        )
        await session.execute(delete(Window).where(Window.subject_id == SUBJ))


@pytest.fixture(autouse=True)
async def _reset(logged_in: TestEnv) -> None:  # noqa: ARG001
    await _clean()
    yield
    await _clean()


async def _window(*, visibility: str = "subject_private", ref: str = "onebot-x:private:9001") -> str:
    async with session_scope() as session:
        wid = f"win_disp_{visibility[:6]}_{abs(hash(ref)) % 9999}"
        session.add(
            Window(
                id=wid,
                actor_id=ACTOR,
                persona_id="persona_ling",
                subject_id=SUBJ,
                title=ref,
                channel="onebot",
                visibility=visibility,
                archived=False,
                last_message_at=datetime.now(UTC),
            )
        )
    return wid


async def _candidate(
    *,
    draft: str | None = "在吗，问个事。",
    oid: str | None = None,
    created_at: datetime | None = None,
) -> str:
    oid = oid or f"opp_disp_{abs(hash(draft or 'none')) % 9999}"
    async with session_scope() as session:
        session.add(
            ProactiveOpportunity(
                id=oid,
                actor_id=ACTOR,
                subject_id=SUBJ,
                kind="reach_out",
                reason="想问对方一件事",
                state="candidate",
                expires_at=SAFE_NOW + timedelta(hours=2),
                created_at=created_at or SAFE_NOW,
                draft_text=draft,
            )
        )
    return oid


async def _state(oid: str) -> tuple[str, datetime | None]:
    async with session_scope() as session:
        row = await session.get(ProactiveOpportunity, oid)
        assert row is not None
        return row.state, row.sent_at


async def _settlement(oid: str) -> tuple[str, datetime | None, str | None, str | None]:
    """结算字段：状态、送达时间、本地 operation ID、投递状态。"""
    async with session_scope() as session:
        row = await session.get(ProactiveOpportunity, oid)
        assert row is not None
        return (
            row.state,
            row.sent_at,
            row.delivery_operation_id,
            row.delivery_state,
        )


async def _operation_state(operation_id: str) -> str | None:
    """本地是否真的存在这次投递的 operation（None = 查无此行）。"""
    async with session_scope() as session:
        row = await session.get(Operation, operation_id)
        return None if row is None else str(row.state)


async def _receipts(operation_id: str) -> list[DeliveryReceiptRow]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(DeliveryReceiptRow).where(
                        DeliveryReceiptRow.operation_id == operation_id
                    )
                )
            ).scalars()
        )


# ---------------------------------------------------------------- 默认关


async def test_disabled_by_default_sends_nothing(logged_in: TestEnv) -> None:  # noqa: ARG001
    """默认不发送——这是最重要的那条。"""
    await _window()
    oid = await _candidate()
    conn = _Connector()

    async with session_scope() as session:
        stats = await dispatch_pending(
            session,
            actor_id=ACTOR,
            connector=conn,
            config_reader=_Config(enabled=False),
            subject_timezone=TZ,
            now=SAFE_NOW,
        )
    assert stats["skipped"] == "disabled"
    assert conn.sent == [], "默认关却发了"
    assert (await _state(oid))[0] == "candidate", "没发却改了状态"


# ---------------------------------------------------------------- 正常发送


async def test_enabled_sends_and_marks_sent(logged_in: TestEnv) -> None:  # noqa: ARG001
    await _window()
    oid = await _candidate()
    conn = _Connector()

    async with session_scope() as session:
        stats = await dispatch_pending(
            session,
            actor_id=ACTOR,
            connector=conn,
            config_reader=_Config(enabled=True),
            subject_timezone=TZ,
            now=SAFE_NOW,
        )
    assert stats["sent"] == 1, stats
    assert len(conn.sent) == 1
    assert conn.sent[0].text == "在吗，问个事。"
    state, sent_at, operation_id, delivery_state = await _settlement(oid)
    assert state == "sent"
    assert sent_at is not None, "sent 必须记送达时间（限额按它计日）"
    # 成功路径的结算字段必须**指得回去**：能查到这次 operation 和回执，
    # 否则"发过了"只是一句话，对账时无从查证。
    assert delivery_state in {"sent", "delivered"}
    assert operation_id, "送达也要记下本地 operation ID（成功路径可回查 operation/回执行）"
    assert await _operation_state(operation_id) == "succeeded"
    assert [r.state for r in await _receipts(operation_id)] in (
        ["delivered"],
        ["sent"],
    )


async def test_candidate_without_draft_is_not_sent(logged_in: TestEnv) -> None:  # noqa: ARG001
    """没草稿不发——那是"还没想好说什么"，不是"有话要说"。"""
    await _window()
    await _candidate(draft=None)
    conn = _Connector()

    async with session_scope() as session:
        stats = await dispatch_pending(
            session,
            actor_id=ACTOR,
            connector=conn,
            config_reader=_Config(enabled=True),
            subject_timezone=TZ,
            now=SAFE_NOW,
        )
    assert stats["considered"] == 0
    assert conn.sent == []


# ---------------------------------------------------------------- 只发私聊


async def test_group_window_is_never_used(logged_in: TestEnv) -> None:  # noqa: ARG001
    """只有群窗口时**不发**并标 rejected。

    主动找你时往群里发一句"在吗"是不可接受的。
    """
    await _window(visibility="group_scope", ref="onebot-x:group:8888")
    oid = await _candidate()
    conn = _Connector()

    async with session_scope() as session:
        stats = await dispatch_pending(
            session,
            actor_id=ACTOR,
            connector=conn,
            config_reader=_Config(enabled=True),
            subject_timezone=TZ,
            now=SAFE_NOW,
        )
    assert stats["rejected"] == 1
    assert conn.sent == [], "往群里发了"
    state, _ = await _state(oid)
    assert state == "rejected"
    async with session_scope() as session:
        row = await session.get(ProactiveOpportunity, oid)
        assert "私聊" in (row.rejected_reason or "")


async def test_no_window_marks_rejected(logged_in: TestEnv) -> None:  # noqa: ARG001
    oid = await _candidate(draft="在吗")
    conn = _Connector()
    async with session_scope() as session:
        stats = await dispatch_pending(
            session,
            actor_id=ACTOR,
            connector=conn,
            config_reader=_Config(enabled=True),
            subject_timezone=TZ,
            now=SAFE_NOW,
        )
    assert stats["rejected"] == 1
    assert (await _state(oid))[0] == "rejected"


# ---------------------------------------------------------------- 门控


async def test_gate_blocked_leaves_candidate_alone(logged_in: TestEnv) -> None:  # noqa: ARG001
    """门控没过就**留着**，不标 rejected——那只是"现在还不是时候"。"""
    await _window()
    oid = await _candidate()
    conn = _Connector()

    # 用**勿扰时段**制造"门控没过"：SAFE_NOW = UTC 04:00，纽约当地 00:00
    # 落在 23:00-08:00 里。
    #
    # 这里原来靠"时区未知 → gate 直接拦下"，那条门已经删掉了：默认时区
    # （Asia/Shanghai）让缺省也有值，否则"没配一个环境变量"就等于她永远
    # 不主动开口（见 `ling/settings/user_preferences.py`）。
    async with session_scope() as session:
        await dispatch_pending(
            session,
            actor_id=ACTOR,
            connector=conn,
            config_reader=_Config(enabled=True),
            subject_timezone="America/New_York",
            now=SAFE_NOW,
        )
    assert conn.sent == [], "门控没过却发了"
    assert (await _state(oid))[0] == "candidate", "门控没过却被标成 rejected"


# ---------------------------------------------------------------- 失败


async def test_failed_delivery_is_not_marked_sent(logged_in: TestEnv) -> None:  # noqa: ARG001
    """投递失败不能标 sent——那会让限额算错，而且用户什么都没收到。"""
    await _window()
    oid = await _candidate()
    conn = _Connector(state="failed")

    async with session_scope() as session:
        stats = await dispatch_pending(
            session,
            actor_id=ACTOR,
            connector=conn,
            config_reader=_Config(enabled=True),
            subject_timezone=TZ,
            now=SAFE_NOW,
        )
    assert stats["failed"] == 1
    state, sent_at = await _state(oid)
    assert state == "failed"
    assert sent_at is None, "没送到却记了送达时间"


async def test_uncertain_is_not_retried_as_success(logged_in: TestEnv) -> None:  # noqa: ARG001
    """回执不确定时标 uncertain 等对账，不当作成功，也不盲目重发。

    `uncertain` 与 `failed` 分开记：前者是"平台可能已经收下"，后者是
    "确定没发出去"。两者都不重发，但对账时要查的东西不一样。
    """
    await _window()
    oid = await _candidate()
    conn = _Connector(state="uncertain")

    async with session_scope() as session:
        stats = await dispatch_pending(
            session,
            actor_id=ACTOR,
            connector=conn,
            config_reader=_Config(enabled=True),
            subject_timezone=TZ,
            now=SAFE_NOW,
        )
    state, sent_at, operation_id, delivery_state = await _settlement(oid)
    assert state == "uncertain", "不确定被当成成功了"
    assert delivery_state == "uncertain"
    assert operation_id, "不确定的这次投递也要留下本地 operation ID"
    assert sent_at is None
    assert len(conn.sent) == 1, "不该重发"
    assert stats["uncertain"] == 1, stats


# ---------------------------------------------------------------- 异常（不是"回执说失败"）

# 上面两条测的是"连接器正常返回了一个失败/不确定的回执"。真正危险的是
# **抛异常**：`DeliveryService.deliver` 抛异常时不会把 operation 交回调用方，
# 而异常完全可能发生在"平台已经收下这条消息"之后（网关断流、写回执时数据库
# 报错）。原来这里只 `failed += 1` 就把机会留在 candidate——下一轮
# `dispatch_pending` 会把它再挑出来**重发一遍**。发消息是不可撤回的，
# 所以这里要的是 fail-closed：宁可少发，也不能重复打扰同一个人。


class _AcceptedThenRaised(_Connector):
    """平台**已经收到**消息，连接器才在返回回执前抛异常。

    这是最难判的一种失败：本地看到的是异常，平台看到的是"发成功了"。
    """

    async def send(self, message):  # noqa: ANN001, ANN202
        self.sent.append(message)
        raise ConnectionResetError("gateway dropped after accept")


class _OverlongReceiptId(_Connector):
    """平台已送达，但回执里的平台消息 id 超过列宽——写回执时数据库报错。

    模拟"发送成功、落库失败"：抛的是数据库异常，整个事务会进入 aborted，
    不处理的话连后面 `service.mark` 都会失败。
    """

    async def send(self, message):  # noqa: ANN001, ANN202
        receipt = await super().send(message)
        # `model_copy(update=...)` 绕过 pydantic 的长度校验（回执契约上限
        # 128），让这个非法值真的落到列上（delivery_receipts 也是 VARCHAR(128)）。
        return receipt.model_copy(update={"platform_message_id": "pm_" + "x" * 200})


class _FirstReceiptTooLong(_OverlongReceiptId):
    """只让**第一条**回执超宽：模拟"第一次写回执炸了、整轮还得继续"。"""

    def __init__(self) -> None:
        super().__init__()
        self._failed_once = False

    async def send(self, message):  # noqa: ANN001, ANN202
        if self._failed_once:
            return await _Connector.send(self, message)
        self._failed_once = True
        return await super().send(message)


class _CapsBoom(_Connector):
    """能力自检就炸了：**根本没走到平台**。"""

    def capabilities(self):  # noqa: ANN202
        raise RuntimeError("capability probe blew up")


async def _dispatch(connector, *, enabled: bool = True) -> dict:  # noqa: ANN001
    async with session_scope() as session:
        return await dispatch_pending(
            session,
            actor_id=ACTOR,
            connector=connector,
            config_reader=_Config(enabled=enabled),
            subject_timezone=TZ,
            now=SAFE_NOW,
        )


class _BarrierConfig(_Config):
    def __init__(self, barrier: asyncio.Barrier) -> None:
        super().__init__(enabled=True)
        self._barrier = barrier

    async def effective(self):  # noqa: ANN202
        await self._barrier.wait()
        return await super().effective()


class _PausedSend(_Connector):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, message):  # noqa: ANN001, ANN202
        from ling.contracts.v1 import DeliveryReceipt

        self.sent.append(message)
        self.entered.set()
        await self.release.wait()
        return DeliveryReceipt(
            receipt_id=f"rcpt_{len(self.sent)}",
            operation_id=message.operation_id,
            channel=self.connector_id,
            state=self._state,
            platform_message_id=f"pm_{len(self.sent)}",
            observed_at=datetime.now(UTC),
            detail=None,
        )


async def _dispatch_with_config(
    connector: object,
    config_reader: object,
    *,
    voice_dispatch: Callable[
        [ProactiveOpportunity, Window, object | None], Awaitable[str]
    ]
    | None = None,
) -> dict[str, int | str]:
    async with session_scope() as session:
        return await dispatch_pending(
            session,
            actor_id=ACTOR,
            connector=connector,
            config_reader=config_reader,
            subject_timezone=TZ,
            now=SAFE_NOW,
            voice_dispatch=voice_dispatch,
        )


async def test_concurrent_dispatchers_send_one_opportunity_at_most_once(
    logged_in: TestEnv,  # noqa: ARG001
) -> None:
    """独立 session 同时竞争时，同一机会最多一次进入发送边界。"""
    await _window()
    oid = await _candidate(oid=f"opp_dispatch_concurrent_{uuid4().hex}")
    connector = _PausedSend()
    config = _BarrierConfig(asyncio.Barrier(2))

    first = asyncio.create_task(_dispatch_with_config(connector, config))
    second = asyncio.create_task(_dispatch_with_config(connector, config))
    try:
        await asyncio.wait_for(connector.entered.wait(), timeout=5)
        # 第二个 dispatch 已穿过配置屏障。让它在第一个发送仍暂停时竞争机会。
        await asyncio.sleep(0.25)
    finally:
        connector.release.set()
        await asyncio.wait_for(
            asyncio.gather(first, second, return_exceptions=True), timeout=10
        )

    first_stats, second_stats = first.result(), second.result()
    assert len(connector.sent) == 1, (
        f"同一机会从两个独立 session 发送了 {len(connector.sent)} 次: "
        f"{first_stats}, {second_stats}"
    )
    state, _ = await _state(oid)
    assert state == "sent", "并发保护不能以两边都不发送作为通过结果"


async def test_stale_dispatch_claim_recovers_as_uncertain_without_resend(
    logged_in: TestEnv,  # noqa: ARG001
) -> None:
    """超过声明阈值的旧 claim 从新 session 回收为 uncertain，且两轮都不重发。"""
    await _window()
    oid = await _candidate(oid=f"opp_dispatch_stale_{uuid4().hex}")
    claim_id = f"idem_dispatch_stale_{uuid4().hex}"
    async with session_scope() as session:
        opportunity = await session.get(ProactiveOpportunity, oid)
        assert opportunity is not None
        opportunity.state = "dispatching"
        session.add(
            IdempotencyKey(
                id=claim_id,
                scope=PROACTIVE_DISPATCH_SCOPE,
                key=oid,
                result_ref="v1:in_progress",
                created_at=SAFE_NOW - timedelta(seconds=STALE_CLAIM_SECONDS + 1),
            )
        )

    current_sweep = _Connector()
    current_stats = await _dispatch(current_sweep)
    assert current_sweep.sent == [], "陈旧 claim 回收时不能重发"
    assert current_stats["uncertain"] == 1, current_stats
    state, sent_at, _operation_id, delivery_state = await _settlement(oid)
    assert state == "uncertain"
    assert delivery_state == "uncertain"
    assert sent_at is None

    later_sweep = _Connector()
    await _dispatch(later_sweep)
    assert later_sweep.sent == [], "后续新 session 不能重试已回收的 uncertain 机会"
    assert (await _settlement(oid))[0] == "uncertain"


async def test_concurrent_voice_dispatchers_call_voice_handler_once(
    logged_in: TestEnv,  # noqa: ARG001
) -> None:
    """voice 模式也必须经过同一机会 claim，并实际进入一次语音回调。"""
    await _window()
    oid = await _candidate(oid=f"opp_dispatch_voice_claim_{uuid4().hex}")
    async with session_scope() as session:
        opportunity = await session.get(ProactiveOpportunity, oid)
        assert opportunity is not None
        opportunity.output_modality = "voice"

    connector = _VoiceConnector()
    config = _BarrierConfig(asyncio.Barrier(2))
    entered = asyncio.Event()
    release = asyncio.Event()
    callback_opportunities: list[str] = []

    async def voice_dispatch(
        opportunity: ProactiveOpportunity, window: Window, selected: object | None
    ) -> str:
        callback_opportunities.append(opportunity.id)
        assert window.subject_id == SUBJ
        assert selected is connector
        entered.set()
        await release.wait()
        return "delivered"

    first = asyncio.create_task(
        _dispatch_with_config(connector, config, voice_dispatch=voice_dispatch)
    )
    second = asyncio.create_task(
        _dispatch_with_config(connector, config, voice_dispatch=voice_dispatch)
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await asyncio.sleep(0.25)
    finally:
        release.set()
        await asyncio.wait_for(
            asyncio.gather(first, second, return_exceptions=True), timeout=10
        )

    first_stats, second_stats = first.result(), second.result()
    assert callback_opportunities == [oid], (
        f"voice callback 次数或机会不正确: {callback_opportunities}"
    )
    assert first_stats["sent"] + second_stats["sent"] == 1, (
        f"voice 正向投递没有恰好一次完成: {first_stats}, {second_stats}"
    )
    assert connector.sent == [], "voice 分支应调用注入的回调而非文本发送器"
    assert (await _state(oid))[0] == "sent"


class _AcceptedThenBlocked(_Connector):
    """在可能已被平台接受的发送入口等待，供取消尝试使用。"""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    async def send(self, message):  # noqa: ANN001, ANN202
        self.sent.append(message)
        self.entered.set()
        await asyncio.Event().wait()


async def test_cancelled_possibly_sent_attempt_is_durable_and_not_retried(
    logged_in: TestEnv,  # noqa: ARG001
) -> None:
    """发送入口后的取消必须跨新 session 持久结算 uncertain，且不可重发。"""
    await _window()
    oid = await _candidate(oid=f"opp_dispatch_cancelled_{uuid4().hex}")
    connector = _AcceptedThenBlocked()

    task = asyncio.create_task(_dispatch(connector))
    await asyncio.wait_for(connector.entered.wait(), timeout=5)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    state, sent_at, _operation_id, delivery_state = await _settlement(oid)
    assert len(connector.sent) == 1, "取消前必须确实越过过发送边界"
    assert state == "uncertain", f"可能已送达的取消结果被记为 {state}"
    assert delivery_state == "uncertain"
    assert sent_at is None

    retry = _Connector()
    await _dispatch(retry)
    assert retry.sent == [], "新 session 对 uncertain 机会又发起了外部调用"
    assert (await _settlement(oid))[0] == "uncertain"


async def test_exception_after_send_is_never_resent(logged_in: TestEnv) -> None:  # noqa: ARG001
    """平台已收到、随后抛异常：机会必须离开 candidate，且绝不重发。

    这就是要在修复前先红的那条：原实现里机会仍是 candidate，
    第二轮的 `considered` 又是 1，同一条"在吗"被发了两遍。
    """
    await _window()
    oid = await _candidate()
    conn = _AcceptedThenRaised()

    stats = await _dispatch(conn)
    assert len(conn.sent) == 1, "第一轮应当真的尝试过一次"
    assert stats["failed"] == 1, stats
    assert stats.get("uncertain") == 1, "结果未知必须单独计数，不能记成确定失败"

    state, sent_at, operation_id, delivery_state = await _settlement(oid)
    assert state == "uncertain", f"平台可能已收到却仍是 {state}：下一轮会重发"
    assert sent_at is None, "结果未知不能记送达时间（限额会算错）"
    assert delivery_state == "uncertain"
    assert operation_id, "没记下本地 operation ID：异常后本地行已回滚，只留下这条线索"
    # 钉住当前取舍（已知限制，见 dispatch.py 的「已知限制」）：连接器抛异常时
    # 这次投递的 operation / 回执行随 SAVEPOINT 一起回滚，本地查不到。机会上
    # 的这个 ID 只是**本地 operation ID 的残留**，只能和同一轮那条 warning
    # 日志对上；它既不是平台侧幂等键（connector.send 不向外透传），也不能用来
    # 向平台去重/对账。别把它当成"现在可对账"。
    assert await _operation_state(operation_id) is None, (
        "operation 行还在？那 SAVEPOINT 边界/回滚语义变了，需要重新核对已知限制"
    )
    assert await _receipts(operation_id) == [], "回执行不该在回滚后还留着"

    # 关键：再跑一轮。候选里不能再有它，连接器也不能再收到同一条。
    again = await _dispatch(conn)
    assert again["considered"] == 0, "不确定的机会被重新挑出来重发了"
    assert len(conn.sent) == 1, "同一条不可撤回的消息被发了两次"


async def test_database_error_after_send_keeps_round_alive(logged_in: TestEnv) -> None:  # noqa: ARG001
    """发送成功、写回执时数据库报错：整轮不能崩，按回执记送达。

    数据库异常会让事务进入 aborted。不把每次投递包在 SAVEPOINT 里，
    异常之后的 `mark` 也会失败——那一轮已经发出去的记录一起回滚，
    下一轮就会重发。这里同时验证"事务没被毒死"和"按已知回执结算"。

    一轮里放**两条**候选：第一条回执写库失败，第二条必须照常发出并结算。
    只测一条的话，"整轮继续"只是名字；第二条真的发出去，才证明 SAVEPOINT
    只圈住炸掉的那一次投递。
    """
    await _window()
    first = await _candidate(created_at=SAFE_NOW)
    second = await _candidate(
        draft="在吗，第二件事。",
        oid="opp_disp_second",
        created_at=SAFE_NOW + timedelta(seconds=1),
    )
    conn = _FirstReceiptTooLong()

    stats = await _dispatch(conn)
    assert stats["considered"] == 2, "两条候选都应在同一轮被处理"
    assert len(conn.sent) == 2, "第一条炸了之后，第二条也必须真的发出去"
    assert [m.text for m in conn.sent] == ["在吗，问个事。", "在吗，第二件事。"]
    assert stats["sent"] == 2, f"两条回执都到了，都应算送达：{stats}"
    assert stats["failed"] == 0

    # 第一条：回执已到达、但落库失败 → 仍按 sent 结算；本地 operation /
    # 回执行随 SAVEPOINT 回滚，机会上只剩本地 operation ID 的残留。
    state, sent_at, operation_id, delivery_state = await _settlement(first)
    assert state == "sent", "平台确实收到了，不该记成失败"
    assert sent_at is not None
    assert delivery_state in {"sent", "delivered"}
    assert operation_id == conn.sent[0].operation_id
    # 已知限制：回执写库失败时 operation / 回执行都被 SAVEPOINT 撤掉，本地
    # 没有这次投递的证据，只有 ID 能和同一轮那条 warning 日志对上。别把这两个
    # 断言当成"正确"，它们钉住的是当前取舍。
    assert await _operation_state(operation_id) is None
    assert await _receipts(operation_id) == [], "回执行不该在回滚后还留着"

    # 第二条：同一轮的后续投递没有被前一条的数据库异常毒死，operation 行
    # 真实存在——这才是"整轮继续"的正面证据。
    state, sent_at, operation_id, delivery_state = await _settlement(second)
    assert state == "sent"
    assert sent_at is not None
    assert delivery_state in {"sent", "delivered"}
    assert operation_id == conn.sent[1].operation_id
    assert await _operation_state(operation_id) == "succeeded"
    assert [r.state for r in await _receipts(operation_id)] in (["delivered"], ["sent"])

    again = await _dispatch(conn)
    assert again["considered"] == 0, "已送达的机会又被重发了"
    assert len(conn.sent) == 2


async def test_failure_before_send_is_fail_closed(logged_in: TestEnv) -> None:  # noqa: ARG001
    """没走到平台的异常：确定没发，标 failed 并记账。

    fail-closed 的取舍：这种失败其实可以安全重试，但"自动重试"和
    "重复打扰"只差一次错误的判断，所以这里不重试，只如实记下来。
    """
    await _window()
    oid = await _candidate()
    conn = _CapsBoom()

    stats = await _dispatch(conn)
    assert conn.sent == [], "能力自检都炸了，消息不该发出去"
    assert stats["failed"] == 1, stats
    assert stats.get("uncertain", 0) == 0, "确定没发出去，不能记成不确定"

    state, sent_at, operation_id, delivery_state = await _settlement(oid)
    assert state == "failed"
    assert sent_at is None
    assert delivery_state == "failed"
    assert operation_id is None, "根本没建 operation，不该编一个 id 出来"

    again = await _dispatch(conn)
    assert again["considered"] == 0
