"""投递与对账：能力校验、错误分类、幂等键、真实回执。

这块状态文档一直标"已实现未测试"，但核心逻辑**不需要真实平台账号**——
用一个假连接器就能完整验证。真正的平台联调（QQ/微信）才需要账号。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ling.capabilities.delivery import DeliveryService
from ling.contracts.v1 import CapabilityDescriptor, DeliveryState, OperationState
from ling.infra.db import session_scope
from ling.ports.channel import (
    ChannelConnector,
    ChannelError,
    ChannelErrorKind,
    DeliveryReceipt,
    OutboundMessage,
    ReceiptQuery,
)
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


class FakeConnector(ChannelConnector):
    """可配置的假连接器：只记录调用，不发网络请求。"""

    connector_id = "fake"
    instance_id = "fake:test"

    def __init__(
        self,
        *,
        enabled: bool = True,
        supports_group: bool = True,
        supports_private: bool = True,
        supports_proactive: bool = False,
        supports_media: bool = False,
        max_text_length: int = 100,
        raise_error: ChannelError | None = None,
        receipt_state: DeliveryState = DeliveryState.delivered,
    ) -> None:
        self._caps = CapabilityDescriptor(
            capability_id="fake",
            version="1",
            supports_private=supports_private,
            supports_group=supports_group,
            supports_proactive=supports_proactive,
            supports_media=supports_media,
            max_text_length=max_text_length,
            enabled=enabled,
        )
        self._raise = raise_error
        self._receipt_state = receipt_state
        self.sent: list[OutboundMessage] = []
        self.queried: list[str] = []

    def capabilities(self) -> CapabilityDescriptor:
        return self._caps

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        self.sent.append(message)
        if self._raise is not None:
            raise self._raise
        return DeliveryReceipt(
            receipt_id=f"rcpt_{len(self.sent)}",
            operation_id=message.operation_id,
            channel=self.connector_id,
            state=self._receipt_state,
            platform_message_id=f"pm_{len(self.sent)}",
            observed_at=datetime.now(UTC),
        )

    async def query_receipt(self, query: ReceiptQuery) -> DeliveryReceipt:
        self.queried.append(query.operation_id)
        return DeliveryReceipt(
            receipt_id="rcpt_q",
            operation_id=query.operation_id,
            channel=self.connector_id,
            state=self._receipt_state,
            platform_message_id="pm_q",
            observed_at=datetime.now(UTC),
        )

    async def verify_inbound(self, headers: dict[str, str], body: bytes) -> bool:
        return True

    def normalize_inbound(self, payload: dict):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def fetch_media(self, media_ref: str) -> tuple[bytes, str]:
        return b"", ""

    def render_output(self, text: str, *, modalities: list) -> list[str]:  # type: ignore[type-arg]
        return [text]

    async def health(self) -> tuple[bool, str]:
        return True, "ok"


def _msg(text: str = "你好", *, is_group: bool = False) -> OutboundMessage:
    return OutboundMessage(
        operation_id=f"op_{text[:6]}_{is_group}",
        window_ref="win_test",
        text=text,
        is_group=is_group,
    )


async def _deliver(connector: FakeConnector, message: OutboundMessage, **kwargs):
    async with session_scope() as session:
        return await DeliveryService(session).deliver(
            connector=connector,
            message=message,
            actor_id="actor_ling",
            subject_id=kwargs.pop("subject_id", None),
            **kwargs,
        )


# ------------------------------------------------------------------ 能力校验先行


async def test_disabled_channel_never_sends(logged_in: TestEnv) -> None:
    connector = FakeConnector(enabled=False)
    outcome = await _deliver(connector, _msg())
    assert outcome.state is OperationState.failed
    assert connector.sent == [], "渠道未启用却发出了请求"
    assert "未启用" in outcome.detail


async def test_group_message_to_nongroup_channel_rejected(logged_in: TestEnv) -> None:
    connector = FakeConnector(supports_group=False)
    outcome = await _deliver(connector, _msg(is_group=True))
    assert outcome.state is OperationState.failed
    assert connector.sent == []
    assert "群聊" in outcome.detail


async def test_private_message_to_noprivate_channel_rejected(logged_in: TestEnv) -> None:
    connector = FakeConnector(supports_private=False)
    outcome = await _deliver(connector, _msg())
    assert outcome.state is OperationState.failed
    assert connector.sent == []


async def test_proactive_on_nonsupporting_channel_rejected(logged_in: TestEnv) -> None:
    connector = FakeConnector(supports_proactive=False)
    outcome = await _deliver(connector, _msg(), require_proactive=True)
    assert outcome.state is OperationState.failed
    assert connector.sent == []
    assert "主动推送" in outcome.detail


async def test_text_over_channel_limit_rejected(logged_in: TestEnv) -> None:
    """超长文本必须在**发之前**拦住，而不是发出去被平台截断。"""
    connector = FakeConnector(max_text_length=10)
    outcome = await _deliver(connector, _msg("这是一段超过十个字的文本"))
    assert outcome.state is OperationState.failed
    assert connector.sent == []
    assert "超出渠道上限" in outcome.detail


# ------------------------------------------------------------------ 正常投递


async def test_successful_delivery_records_receipt(logged_in: TestEnv) -> None:
    connector = FakeConnector()
    outcome = await _deliver(connector, _msg("短"))
    assert outcome.state is OperationState.succeeded
    assert len(connector.sent) == 1
    assert outcome.receipt is not None
    assert outcome.receipt.state == DeliveryState.delivered.value


# ------------------------------------------------------------------ 错误分类


async def test_uncertain_error_marks_uncertain_not_failed(logged_in: TestEnv) -> None:
    """uncertain ≠ failed：消息可能已经到达平台。

    标成 failed 会导致重发，用户收到两条重复消息。
    必须留 uncertain 供对账确认。
    """
    connector = FakeConnector(
        raise_error=ChannelError(ChannelErrorKind.uncertain, "超时，未知是否送达")
    )
    outcome = await _deliver(connector, _msg())
    assert outcome.state is OperationState.uncertain, (
        f"不确定的结果被标成 {outcome.state}，会导致重复发送"
    )


async def test_transient_error_marks_failed(logged_in: TestEnv) -> None:
    connector = FakeConnector(
        raise_error=ChannelError(ChannelErrorKind.unreachable, "网关 502", retriable=True)
    )
    outcome = await _deliver(connector, _msg())
    assert outcome.state is OperationState.failed
    assert outcome.state is OperationState.failed


# ------------------------------------------------------------------ 对账


async def test_reconcile_queries_platform(logged_in: TestEnv) -> None:
    """对账必须真的向平台查询，而不是凭本地状态猜。"""
    connector = FakeConnector()
    async with session_scope() as session:
        service = DeliveryService(session)
        outcome = await service.deliver(
            connector=connector,
            message=_msg("对账"),
            actor_id="actor_ling",
            subject_id=None,
        )
        assert outcome.operation_id

    async with session_scope() as session:
        reconciled = await DeliveryService(session).reconcile(
            connector, outcome.operation_id
        )
    assert connector.queried == [outcome.operation_id], "对账没有向平台查询"
    assert reconciled.state is OperationState.succeeded


async def test_operation_is_recorded_for_audit(logged_in: TestEnv) -> None:
    """每次投递都要留 Operation 记录（谁、发给谁、多少字）。"""
    from sqlalchemy import select

    from ling.infra.models import Operation

    connector = FakeConnector()
    outcome = await _deliver(connector, _msg("审计"))
    async with session_scope() as session:
        row = (
            await session.execute(
                select(Operation).where(Operation.id == outcome.operation_id)
            )
        ).scalar_one()
    assert row.kind == "delivery"
    assert row.payload["channel"] == "fake"
    assert row.payload["chars"] == len("审计")


async def test_idempotency_key_is_carried(logged_in: TestEnv) -> None:
    """operation_id 必须传给平台层作为幂等键。"""
    connector = FakeConnector()
    message = _msg("幂等")
    outcome = await _deliver(connector, message)
    assert connector.sent[0].operation_id == outcome.operation_id


# ------------------------------------------------------------------ 出站媒体


async def test_media_is_not_silently_dropped(logged_in: TestEnv) -> None:
    """渠道不支持媒体时**拒绝发送**，而不是发文字丢图还报成功。

    这是本项目最不能容忍的那类错误：没有真实结果却留下成功记录。
    原实现里 OutboundMessage.media_refs 存在但连接器完全忽略它——
    一旦有人填了，文字会发出去、图被静默丢弃、回执照样拿到。
    """
    connector = FakeConnector(supports_media=False)
    message = OutboundMessage(
        operation_id="",
        window_ref="fake:private:u1",
        text="给你看张图",
        media_refs=["a.jpg"],
    )
    async with session_scope() as session:
        outcome = await DeliveryService(session).deliver(
            connector=connector,
            message=message,
            actor_id="actor_ling",
            subject_id=logged_in.subject_id,
        )

    # _unsupported 的语义：operation=failed + error_code=unsupported_capability，
    # 回执状态记 unsupported。不是"成功"，也不是"不确定"。
    assert outcome.state is OperationState.failed, outcome.detail
    assert "不支持发送媒体" in outcome.detail
    assert connector.sent == [], "渠道不支持媒体却仍然发了"


async def test_media_is_sent_when_supported(logged_in: TestEnv) -> None:
    connector = FakeConnector(supports_media=True)
    message = OutboundMessage(
        operation_id="",
        window_ref="fake:private:u1",
        text="给你看张图",
        media_refs=["a.jpg", "b.jpg"],
    )
    async with session_scope() as session:
        outcome = await DeliveryService(session).deliver(
            connector=connector,
            message=message,
            actor_id="actor_ling",
            subject_id=logged_in.subject_id,
        )

    assert outcome.state is OperationState.succeeded, outcome.detail
    assert len(connector.sent) == 1
    assert connector.sent[0].media_refs == ["a.jpg", "b.jpg"], "媒体没送到渠道"


async def test_operation_records_media_intent(logged_in: TestEnv) -> None:
    """operation 的 payload 要记下"本应发几个媒体"。

    事后对账时才能区分"压根没想发媒体"和"想发但失败了"。
    """
    from ling.infra.models import Operation

    connector = FakeConnector(supports_media=True)
    message = OutboundMessage(
        operation_id="",
        window_ref="fake:private:u1",
        text="三张",
        media_refs=["a.jpg", "b.jpg", "c.jpg"],
    )
    async with session_scope() as session:
        outcome = await DeliveryService(session).deliver(
            connector=connector,
            message=message,
            actor_id="actor_ling",
            subject_id=logged_in.subject_id,
        )
        row = await session.get(Operation, outcome.operation_id)
        assert row is not None
        assert row.payload["media_count"] == 3
        assert row.payload["media_refs"] == ["a.jpg", "b.jpg", "c.jpg"]


async def test_text_only_still_sends(logged_in: TestEnv) -> None:
    """没有媒体时行为不变——这是"纯增益改动"的证据。"""
    connector = FakeConnector(supports_media=False)
    message = OutboundMessage(
        operation_id="", window_ref="fake:private:u1", text="只有文字"
    )
    async with session_scope() as session:
        outcome = await DeliveryService(session).deliver(
            connector=connector,
            message=message,
            actor_id="actor_ling",
            subject_id=logged_in.subject_id,
        )
    assert outcome.state is OperationState.succeeded, outcome.detail
    assert connector.sent[0].media_refs == []


async def test_media_delivery_still_needs_receipt(logged_in: TestEnv) -> None:
    """媒体发出去但没有真实回执 → uncertain，不记成送达。

    这是"生成成功但渠道失败不记成送达"的直接验证。
    """
    connector = FakeConnector(
        supports_media=True, receipt_state=DeliveryState.uncertain
    )
    message = OutboundMessage(
        operation_id="", window_ref="fake:private:u1", text="图", media_refs=["a.jpg"]
    )
    async with session_scope() as session:
        outcome = await DeliveryService(session).deliver(
            connector=connector,
            message=message,
            actor_id="actor_ling",
            subject_id=logged_in.subject_id,
        )
    assert outcome.state is OperationState.uncertain, outcome.detail
    assert outcome.state is not OperationState.succeeded


# ---------------------------------------------------------------- 送达判据唯一


@pytest.mark.parametrize(
    ("receipt_state", "expect_delivered"),
    [
        (DeliveryState.delivered, True),
        (DeliveryState.sent, True),
        (DeliveryState.uncertain, False),
        (DeliveryState.failed, False),
    ],
)
async def test_delivered_property_matches_receipt_state(
    logged_in: TestEnv,  # noqa: ARG001
    receipt_state: DeliveryState,
    expect_delivered: bool,
) -> None:
    """`DeliveryOutcome.delivered` 必须与 `deliver()` 的映射一致。

    原来两个调用点各写一种拼法：bridge 判
    `receipt.state in {"delivered","sent"}`，worker 判 `state == "succeeded"`。
    两者当前等价，但那是**两份定义同一个事实**——将来给 deliver 加一个新成功
    状态时，只有一处会被记得改，另一处会静默把成功当失败。

    这条测试锁住映射关系：改 deliver 的映射而不同步属性，这里就红。
    """
    from ling.infra.db import session_scope

    connector = FakeConnector(receipt_state=receipt_state)
    async with session_scope() as session:
        outcome = await DeliveryService(session).deliver(
            connector=connector,
            message=OutboundMessage(
                operation_id="op_x", window_ref="win_x", text="hi", is_group=False
            ),
            actor_id="actor_ling",
            subject_id="subj_delivered",
        )
    assert outcome.receipt is not None
    # receipt.state 是数据库里的**字符串**，不是枚举
    assert DeliveryState(outcome.receipt.state) is receipt_state
    assert outcome.delivered is expect_delivered, (
        f"回执 {receipt_state} 时 delivered 判成了 {outcome.delivered}"
    )


async def test_unsupported_outcome_is_not_delivered(logged_in: TestEnv) -> None:  # noqa: ARG001
    """能力自检没过时 delivered 必须为 False。

    注意：这种情况**仍然有回执**（`state=failed` / `receipt.state=unsupported`），
    不是 receipt=None。我原来在属性注释里写反了，这条测试顺手验证真实行为。
    """
    from ling.infra.db import session_scope

    connector = FakeConnector(enabled=False)
    async with session_scope() as session:
        outcome = await DeliveryService(session).deliver(
            connector=connector,
            message=OutboundMessage(
                operation_id="op_y", window_ref="win_y", text="hi", is_group=False
            ),
            actor_id="actor_ling",
            subject_id="subj_unsupported",
        )
    assert outcome.receipt is not None, "能力不通过也要留下回执"
    assert DeliveryState(outcome.receipt.state) is DeliveryState.unsupported
    assert outcome.state is OperationState.failed
    assert outcome.delivered is False
    assert connector.sent == [], "能力未通过却仍然发了"
