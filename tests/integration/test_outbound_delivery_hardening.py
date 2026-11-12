"""出站投递加固（P0-1/P0-2/P0-3/P1）的端到端验收。

对应用户任务里的每一条，用假 OneBot（httpx MockTransport）+ 真实 PG：

1. **段级幂等**：部分送达之后重投同一个事件，**第 1 段不许重发**，
   而且不能为了续发再调一次模型；
2. **uncertain 对账三态**：查到 → succeeded；查不到 → 仍 uncertain
   并留 attempts/下次重试；平台给两个否证 → failed；
3. **周期任务真的被调度**：`delivery.reconcile` 在 `PERIODIC_JOBS` 里，
   且调它能把 uncertain 收敛；
4. **180 秒超时不丢正文**：超时后不投递、正文留给续发、用户消息不记 replied；
   续发任务把正文发出且只发一次；
5. **引用用真 id**：上下文里有 `[msg_id:…]`；引用真实 id → 带 reply 段；
   引用编造的 id → **只丢引用段**，正文照发；
6. **retcode 1200 → uncertain**：不重发、不记 replied。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from connectors.onebot.connector import OneBotV11Connector
from connectors.onebot.settings import OneBotConfig
from sqlalchemy import select

from ling.capabilities.chunking import plan_parts
from ling.capabilities.delivery import DeliveryService
from ling.contracts.v1 import DeliveryState, OperationState
from ling.domains.identity.service import IdentityService
from ling.infra.db import session_scope
from ling.infra.models import DeliveryReceiptRow, IdempotencyKey, Message, Operation, Window
from ling.kernel.turn import TurnStreamEvent
from ling.ports.channel import (
    CapabilityDescriptor,
    ChannelConnector,
    DeliveryReceipt,
    OutboundMessage,
    ReceiptQuery,
)
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

CFG = OneBotConfig(
    api_base="http://onebot.test:5700",
    access_token="tok",
    secret="sec",
    self_id="10001",
    instance_id="onebot-hardening",
)
QQ_USER = "100200300"
WINDOW_REF = f"onebot-hardening:private:{QQ_USER}"

_LONG_REPLY = (
    "第一句：今天风很大，我把窗关上了。\n"
    "第二句：你要是出门记得穿厚一点。\n"
    "第三句：回来我给你煮点热的。\n"
)


class _FakeOneBot:
    """记录出站请求；``fail_sends`` 里的第 N 次发送返回 retcode=100。"""

    def __init__(self, *, fail_sends: set[int] | None = None, retcode: int = 100) -> None:
        self.sent: list[dict] = []
        self._fail = set(fail_sends or set())
        self._retcode = retcode

    async def handler(self, request: httpx.Request) -> httpx.Response:
        action = str(request.url).rsplit("/", 1)[-1]
        body = json.loads(request.content or b"")
        if action in {"send_private_msg", "send_group_msg"}:
            self.sent.append({"action": action, "body": body})
            if len(self.sent) in self._fail:
                return httpx.Response(
                    200,
                    json={"status": "failed", "retcode": self._retcode, "data": None},
                )
            return httpx.Response(
                200,
                json={"status": "ok", "retcode": 0, "data": {"message_id": 9000 + len(self.sent)}},
            )
        if action == "get_msg":
            return httpx.Response(
                200, json={"status": "failed", "retcode": 200, "wording": "消息不存在"}
            )
        if action in {"get_friend_msg_history", "get_group_msg_history"}:
            return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {"messages": []}})
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {}})

    def texts(self) -> list[str]:
        out: list[str] = []
        for call in self.sent:
            out.append(
                "".join(
                    seg["data"].get("text", "")
                    for seg in call["body"].get("message") or []
                    if seg.get("type") == "text"
                )
            )
        return out

    def reply_segments(self) -> list[list[dict]]:
        return [
            [seg for seg in call["body"].get("message") or [] if seg.get("type") == "reply"]
            for call in self.sent
        ]


def _install(env: TestEnv, fake: _FakeOneBot) -> OneBotV11Connector:
    connector = OneBotV11Connector(
        CFG, http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    )
    env.app.state.onebot_connector = connector
    return connector


async def _bind(env: TestEnv) -> None:
    async with session_scope() as session:
        await IdentityService(session).bind_account(
            subject_id=env.subject_id,
            connector_instance=CFG.instance_id,
            external_account_id=QQ_USER,
            proof="proven",
        )


def _event_body(message: str, *, message_id: int = 777) -> bytes:
    payload = {
        "time": int(datetime.now(UTC).timestamp()),
        "self_id": 10001,
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": message_id,
        "user_id": int(QQ_USER),
        "message": message,
        "raw_message": message,
        "font": 0,
        "sender": {"nickname": "小不点"},
    }
    return json.dumps(payload).encode()


async def _post(env: TestEnv, body: bytes) -> dict:
    sig = hmac.new(b"sec", body, hashlib.sha1).hexdigest()
    response = await env.client.post(
        "/v1/onebot/events",
        content=body,
        headers={
            "X-Signature": f"sha1={sig}",
            "X-Self-ID": "10001",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _is_choice_call(messages) -> bool:  # noqa: ANN001
    return bool(messages) and "只输出一个 JSON 对象" in (messages[0].content or "")


class _TextRouter:
    """决策交给真实 router；生成换成**固定文本**（可顺便记下提示词）。"""

    def __init__(self, inner, text: str) -> None:  # noqa: ANN001
        self._inner = inner
        self._text = text
        self.generation_calls = 0
        self.prompts: list[list] = []

    async def stream(self, config, *, profile, messages, **kwargs):  # noqa: ANN001, ANN003
        from ling.models.router import StreamOutcome
        from ling.ports.model import ModelChunk

        if _is_choice_call(messages):
            async for item in self._inner.stream(
                config, profile=profile, messages=messages, **kwargs
            ):
                yield item
            return
        self.generation_calls += 1
        self.prompts.append(list(messages))
        for index in range(0, len(self._text), 6):
            yield ModelChunk(delta=self._text[index : index + 6]), StreamOutcome()
        yield ModelChunk(done=True, finish_reason="stop"), StreamOutcome(text=self._text)


async def _post_with_text(env: TestEnv, text: str, message: str, *, message_id: int = 777) -> tuple[dict, _TextRouter]:
    stub = _TextRouter(env.app.state.router, text)
    env.app.state.router = stub
    try:
        body = await _post(env, _event_body(message, message_id=message_id))
    finally:
        env.app.state.router = stub._inner
    return body, stub


# ---------------------------------------------------------------- P0-1 段级幂等


async def test_redelivery_does_not_resend_first_part(logged_in: TestEnv) -> None:
    """第 2 段被平台拒收 → 重投同一事件时**第 1 段不许再发一次**。

    修之前：`plan_id` 是随机 uuid，重投等于"重新规划 + 从第 1 段再发一遍"，
    用户会看到第一句重复两遍。
    """
    fake = _FakeOneBot(fail_sends={2})  # 第 2 次发送失败 → 部分送达
    _install(logged_in, fake)
    await _bind(logged_in)

    body, stub = await _post_with_text(logged_in, _LONG_REPLY, "我出门了", message_id=801)
    parts = plan_parts(_LONG_REPLY, is_group=False)
    assert len(parts) >= 2
    assert body["parts_sent"] == 1, body
    assert body["delivered"] is False, body
    assert len(fake.sent) == 2, fake.texts()

    # OneBot 超时重投同一个事件（部分送达会释放认领，这是既有行为）
    again = await _post(logged_in, _event_body("我出门了", message_id=801))
    assert again.get("duplicate") is not True, again

    texts = fake.texts()
    assert texts.count(parts[0]) == 1, f"第 1 段被重发了：{texts}"
    assert parts[1] in texts[1:] or any(parts[1] in t for t in texts[1:]), (
        f"重投没有把剩下的段续发出去：{texts}"
    )
    assert stub.generation_calls == 1, "续发不该再调一次模型（会变成另一版内容）"
    assert again["delivered"] is True, again


async def test_all_parts_delivered_is_not_resent_on_redelivery(logged_in: TestEnv) -> None:
    """全部送达之后重投：由事件认领挡住，一次都不该再发。"""
    fake = _FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    body = _event_body("在吗", message_id=802)
    first = await _post(logged_in, body)
    assert first["delivered"] is True, first
    sent_before = len(fake.sent)
    second = await _post(logged_in, body)
    assert second.get("duplicate") is True
    assert len(fake.sent) == sent_before


# ---------------------------------------------------------------- P0-4 retcode


async def test_retcode_1200_is_uncertain_and_never_resent(logged_in: TestEnv) -> None:
    """NapCat 的 1200 = 动作内部抛异常（可能是**已经发出去了**的超时）。

    所以：不重发、不记 replied，进对账。
    """
    fake = _FakeOneBot(fail_sends={1}, retcode=1200)
    connector = _install(logged_in, fake)
    await _bind(logged_in)

    body, _stub = await _post_with_text(logged_in, "这一句发不发得出去", "在吗", message_id=803)

    assert body["delivered"] is False, body
    assert body["parts_sent"] == 0, body
    assert len(fake.sent) == 1, f"uncertain 被重发了：{fake.texts()}"
    # 恰好有一条 uncertain 的 delivery operation
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(Operation).where(
                        Operation.kind == "delivery",
                        Operation.state == OperationState.uncertain.value,
                    )
                )
            ).scalars()
        )
    assert len(rows) == 1, [r.state for r in rows]
    assert rows[0].payload.get("text_digest"), "对账要靠内容摘要，不能只靠 id"
    assert connector.instance_id == CFG.instance_id


# ---------------------------------------------------------------- P0-2 对账三态


class _FakeReconcileConnector(ChannelConnector):
    """只用来对账的假连接器（不真的发）。"""

    connector_id = "onebot"
    instance_id = "onebot-hardening"

    def __init__(self, state: DeliveryState) -> None:
        self._state = state
        self.queried: list[ReceiptQuery] = []

    def capabilities(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            capability_id="onebot",
            version="11",
            supports_private=True,
            supports_group=True,
            max_text_length=4000,
            receipt_reliability="best_effort",
            enabled=True,
        )

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:  # pragma: no cover
        raise AssertionError("对账不该发送")

    async def query_receipt(self, query: ReceiptQuery) -> DeliveryReceipt:
        self.queried.append(query)
        return DeliveryReceipt(
            receipt_id="rcpt_q",
            operation_id=query.operation_id,
            channel="onebot",
            state=self._state,
            platform_message_id="pm_q" if self._state is DeliveryState.delivered else None,
            observed_at=datetime.now(UTC),
            detail="对账结果",
        )

    async def verify_inbound(self, headers, body) -> bool:  # noqa: ANN001
        return True

    def normalize_inbound(self, payload: dict):  # noqa: ANN001
        raise NotImplementedError

    async def fetch_media(self, media_ref: str) -> tuple[bytes, str]:  # pragma: no cover
        return b"", ""

    def render_output(self, text: str, *, modalities) -> list[str]:  # noqa: ANN001
        return [text]

    async def health(self) -> tuple[bool, str]:
        return True, "ok"


async def _uncertain_operation(
    env: TestEnv, connector: ChannelConnector, *, text: str = "落在我手里的一句"
) -> str:
    """造一条 uncertain 的投递（模拟"发送结果未知"）。"""
    from ling.ports.channel import ChannelError, ChannelErrorKind

    class _Unknown(ChannelConnector):
        connector_id = "onebot"
        instance_id = "onebot-hardening"

        def capabilities(self):  # noqa: ANN201
            return connector.capabilities()

        async def send(self, message: OutboundMessage) -> DeliveryReceipt:
            raise ChannelError(ChannelErrorKind.uncertain, "发送超时，未知是否到达")

        async def query_receipt(self, query: ReceiptQuery) -> DeliveryReceipt:  # pragma: no cover
            raise AssertionError

        async def verify_inbound(self, headers, body) -> bool:  # noqa: ANN001
            return True

        def normalize_inbound(self, payload: dict):  # noqa: ANN001
            raise NotImplementedError

        async def fetch_media(self, media_ref: str):  # pragma: no cover
            return b"", ""

        def render_output(self, text: str, *, modalities) -> list[str]:  # noqa: ANN001
            return [text]

        async def health(self) -> tuple[bool, str]:
            return True, "ok"

    async with session_scope() as session:
        outcome = await DeliveryService(session).deliver(
            connector=_Unknown(),
            message=OutboundMessage(
                operation_id="op_unknown",
                window_ref=WINDOW_REF,
                text=text,
                is_group=False,
            ),
            actor_id=env.settings.actor_id,
            subject_id=env.subject_id,
        )
    assert outcome.state is OperationState.uncertain, outcome.detail
    return outcome.operation_id


async def test_reconcile_three_states(logged_in: TestEnv) -> None:
    """查到 → succeeded；查不到 → 仍 uncertain（带 attempts）；否证 → failed。"""
    env = logged_in

    # ① 查到 → succeeded
    op_id = await _uncertain_operation(env, _FakeReconcileConnector(DeliveryState.delivered))
    async with session_scope() as session:
        outcome = await DeliveryService(session).reconcile(
            _FakeReconcileConnector(DeliveryState.delivered), op_id
        )
    assert outcome.state is OperationState.succeeded, outcome.detail

    # ② 仍不确定 → 保持 uncertain，留下 attempts 与下次重试时间
    op_id = await _uncertain_operation(env, _FakeReconcileConnector(DeliveryState.uncertain))
    async with session_scope() as session:
        outcome = await DeliveryService(session).reconcile(
            _FakeReconcileConnector(DeliveryState.uncertain), op_id
        )
    assert outcome.state is OperationState.uncertain, "查不到不许改成 failed"
    async with session_scope() as session:
        row = await session.get(Operation, op_id)
        payload = dict(row.payload or {})
    assert payload["reconcile_attempts"] == 1
    assert "reconcile_next_at" in payload, "没有下次重试时间就会每轮都打一遍平台"

    # ③ 平台给出明确否证 → failed（但**不重发**：连接器只在两个否证时给 failed）
    op_id = await _uncertain_operation(env, _FakeReconcileConnector(DeliveryState.failed))
    async with session_scope() as session:
        outcome = await DeliveryService(session).reconcile(
            _FakeReconcileConnector(DeliveryState.failed), op_id
        )
    assert outcome.state is OperationState.failed


async def test_reconcile_job_is_scheduled_and_converges(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`delivery.reconcile` 必须是周期任务，且真的能把 uncertain 收敛。"""
    from ling.worker import PERIODIC_JOBS, handle_delivery_reconcile

    topics = {job.topic for job in PERIODIC_JOBS}
    assert "delivery.reconcile" in topics, "uncertain 又没有调用方 = 永久悬空"

    connector = _FakeReconcileConnector(DeliveryState.delivered)
    op_id = await _uncertain_operation(logged_in, connector)
    monkeypatch.setattr(
        "ling.worker._group_reply_connector",
        AsyncMock(side_effect=lambda channel, window_ref: connector if channel == "onebot" else None),
    )
    import ling.domains.goals.delivery_reconcile as reconcile_module

    async def no_pause(seconds: float) -> None:  # noqa: ARG001
        return None

    monkeypatch.setattr(reconcile_module, "_pause", no_pause)
    await handle_delivery_reconcile({})

    async with session_scope() as session:
        row = await session.get(Operation, op_id)
    assert row.state == OperationState.succeeded.value, row.detail
    assert connector.queried, "周期任务没有真的去问平台"


# ---------------------------------------------------------------- P1 超时


class _HangingCoordinator:
    """先吐一段 delta，然后**永不结束**（模拟模型侧卡住）。"""

    def __init__(self) -> None:
        self.calls = 0
        self.stop_requests: list[str] = []

    async def run_turn(self, **kwargs):  # noqa: ANN003
        self.calls += 1
        queue: asyncio.Queue = asyncio.Queue()
        await queue.put(
            TurnStreamEvent(
                event="delta",
                data={"text": "我先说一半", "turn_id": kwargs.get("turn_id")},
            )
        )
        return queue

    def request_stop(self, turn_id: str) -> bool:
        self.stop_requests.append(turn_id)
        return True

    def partial_text(self, turn_id: str) -> str:  # noqa: ARG002
        return "我先说一半"


async def test_timeout_keeps_body_and_never_claims_replied(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """180 秒超时：不投递、正文留给续发、用户消息**不记 replied**。"""
    import ling.kernel.bridge as bridge_module

    fake = _FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    coordinator = _HangingCoordinator()
    logged_in.app.state.bridge_coordinator = coordinator
    monkeypatch.setattr(bridge_module, "TURN_WAIT_TIMEOUT_SECONDS", 0.2)

    body = await _post(logged_in, _event_body("你在吗", message_id=804))

    assert body["accepted"] is True, body
    assert body["delivered"] is False, body
    assert body["handled_as"] == "failed", body
    assert "超时" in str(body["reason"]), body
    assert "已回复" not in str(body["reason"]), body
    assert not fake.sent, "超时后不该发出任何东西（正文还没生成完）"
    assert coordinator.stop_requests, "没有请求停止：那一轮会一直烧额度"

    # 正文不能丢：收尾路径会把已生成的部分写进 messages（这里直接模拟）
    turn_id = body["turn_id"]
    async with session_scope() as session:
        user_row = (
            await session.execute(select(Message).where(Message.turn_id == turn_id, Message.role == "user"))
        ).scalars().first()
        assert user_row is not None
        assert user_row.handled_as == "failed", user_row.handled_as
        actor = Message(
            id="msg_partial_timeout",
            window_id=user_row.window_id,
            turn_id=turn_id,
            role="actor",
            text="我先说一半，剩下的等会儿再说。",
            state="stopped",
            origin="model_inference",
            sequence=user_row.sequence + 1,
        )
        session.add(actor)
        pending = list(
            (
                await session.execute(
                    select(Operation).where(Operation.kind == "delivery.pending")
                )
            ).scalars()
        )
    assert pending, "超时没有留下 delivery.pending：进程一重启就彻底丢了"
    assert pending[0].payload.get("reason") == "generation_timeout"


async def test_pending_delivery_is_resumed_once(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超时留下的 pending 由周期任务续发，而且**只发一次**。"""
    from ling.worker import handle_delivery_reconcile

    fake = _FakeOneBot()
    connector = _install(logged_in, fake)
    await _bind(logged_in)
    monkeypatch.setattr(
        "ling.worker._group_reply_connector",
        AsyncMock(side_effect=lambda channel, window_ref: connector if channel == "onebot" else None),
    )
    import ling.domains.goals.delivery_reconcile as reconcile_module

    async def no_pause(seconds: float) -> None:  # noqa: ARG001
        return None

    monkeypatch.setattr(reconcile_module, "_pause", no_pause)

    # 造一条 pending + 对应的"正文已落库"
    async with session_scope() as session:
        window = Window(
            id="win_pending",
            actor_id=logged_in.settings.actor_id,
            persona_id=logged_in.settings.persona_id,
            subject_id=logged_in.subject_id,
            title=WINDOW_REF,
            channel="onebot",
            visibility="subject_private",
        )
        session.add(window)
        # 必须先 flush：`messages.window_id` 是外键，同一事务里不 flush
        # 就没有 windows 行可引用（PostgreSQL 立即检查）。
        await session.flush()
        session.add(
            Message(
                id="msg_pending_actor",
                window_id=window.id,
                turn_id="turn_pending_1",
                role="actor",
                text="这段是超时之后才落库的正文。",
                state="stopped",
                origin="model_inference",
                sequence=1,
            )
        )
        pending = await DeliveryService(session).record_pending(
            connector_id="onebot",
            actor_id=logged_in.settings.actor_id,
            subject_id=logged_in.subject_id,
            window_ref=WINDOW_REF,
            is_group=False,
            plan_id="plan_turn_pending_1",
            turn_id="turn_pending_1",
            reason="generation_timeout",
            detail="测试造出来的 pending",
        )
        pending_id = pending.id

    await handle_delivery_reconcile({})
    assert fake.texts() == ["这段是超时之后才落库的正文。"], fake.texts()
    async with session_scope() as session:
        row = await session.get(Operation, pending_id)
    assert row.state == OperationState.succeeded.value, row.detail

    # 再跑一轮：pending 已收敛，不许再发一次
    await handle_delivery_reconcile({})
    assert len(fake.texts()) == 1, f"续发重复了：{fake.texts()}"


# ---------------------------------------------------------------- P0-3 真 id 引用


async def test_context_gives_real_message_id(logged_in: TestEnv) -> None:
    """上下文里必须出现 `[msg_id:777]`——否则模型只能瞎编引用 id。"""
    fake = _FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    _body, stub = await _post_with_text(logged_in, "嗯，我在。", "你在吗", message_id=777)
    assert stub.prompts, "没有抓到生成用的提示词"
    system = "\n".join(str(m.content) for m in stub.prompts[0])
    assert "[quote:" in system, f"上下文里没有引用语法教学：{system[-800:]}"
    assert "[msg_id:777]" in system, f"上下文里没有真 id：{system[-800:]}"


async def test_history_messages_carry_their_own_ids(logged_in: TestEnv) -> None:
    """历史里的每条入站消息也要带自己的 id，否则"回一条较早的"就没法引用。"""
    fake = _FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    first, _ = await _post_with_text(logged_in, "第一句回你。", "第一句", message_id=771)
    assert first["delivered"] is True, first
    _body, stub = await _post_with_text(logged_in, "第二句回你。", "第二句", message_id=772)

    prompt = "\n".join(str(m.content) for m in stub.prompts[0])
    assert "[msg_id:771]" in prompt, "历史消息没有 id，回较早的那条就只能瞎编"
    assert "[msg_id:772]" in prompt, "当前这句也没有 id"


async def test_quote_of_fake_id_degrades_to_plain_message(logged_in: TestEnv) -> None:
    """编造的引用 id → **不发引用段**，正文照发，不整条失败。"""
    fake = _FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    body, _stub = await _post_with_text(
        logged_in, "[quote:999999] 这句话照发，只是不带引用。", "你在吗", message_id=778
    )
    assert body["delivered"] is True, body
    assert len(fake.sent) == 1, "降级不该多打一次平台"
    assert all(not segs for segs in fake.reply_segments()), fake.sent
    assert any("这句话照发" in text for text in fake.texts()), fake.texts()


async def test_quote_of_real_id_is_sent(logged_in: TestEnv) -> None:
    """引用**真实** id → 消息段里带 `{"type":"reply","data":{"id":"777"}}`。"""
    fake = _FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    body, _stub = await _post_with_text(
        logged_in, "[quote:777] 我回一下你刚才那句。", "你在吗", message_id=777
    )
    assert body["delivered"] is True, body
    assert fake.reply_segments()[0] == [{"type": "reply", "data": {"id": "777"}}], fake.sent


async def test_platform_message_id_is_recorded_for_context(logged_in: TestEnv) -> None:
    """本地消息 id → 平台 id 的映射必须落库，否则历史消息永远没有 id。"""
    fake = _FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    await _post_with_text(logged_in, "嗯。", "在吗", message_id=779)

    async with session_scope() as session:
        user_row = (
            await session.execute(select(Message).where(Message.text == "在吗"))
        ).scalars().first()
        mapping = (
            await session.execute(
                select(IdempotencyKey).where(IdempotencyKey.key == user_row.id)
            )
        ).scalar_one_or_none()
    assert mapping is not None and mapping.result_ref == "779", mapping


# ---------------------------------------------------------------- 回执与操作记录


async def test_uncertain_operation_has_receipt_row(logged_in: TestEnv) -> None:
    """uncertain 也要有回执行，否则对账不知道上次查到什么。"""
    fake = _FakeOneBot(fail_sends={1}, retcode=1200)
    _install(logged_in, fake)
    await _bind(logged_in)
    await _post_with_text(logged_in, "一句可能已发出的话", "在吗", message_id=780)

    async with session_scope() as session:
        op = (
            await session.execute(select(Operation).where(Operation.kind == "delivery"))
        ).scalars().first()
        rows = list(
            (
                await session.execute(
                    select(DeliveryReceiptRow).where(
                        DeliveryReceiptRow.operation_id == op.id
                    )
                )
            ).scalars()
        )
    assert rows, "没有回执行，对账与运维都无据可查"
    assert rows[-1].state == DeliveryState.uncertain.value
