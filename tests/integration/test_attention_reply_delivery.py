"""群聊「决定回了」之后必须真的发出去。

## 为什么要这个文件

`handle_attention_tick` 曾经只做一件事：

```python
if result.decision == "replied":
    replied += 1
```

**只计数、不发送**。调度、租约、游标、防刷屏上限、决策与理由全都在，
唯独缺了「构造上下文 → 生成 → 投递」这一段。后果是群里她**永远不会开口**，
而日志里却写着 `decision=replied`——用户看到"她决定回了"，群里什么都没发生。

本文件钉住四条事实：

1. `decision == "replied"` 且配置项 `group_reply_delivery_enabled` 打开 →
   **真的调用了投递**（fake connector 收到消息）
2. 投递失败 → **不标 replied**：消息 `handled_as` 变 `failed`、轮询日志变
   `reply_failed`、tick 汇总里 `replied=0`
3. 配置项默认关闭 → 只决策、不生成、不发送；**payload 里的 `shadow` 不再
   能决定发不发**（否则又是一个改不动行为的假开关）
4. 周期任务的 payload 里不再有 `shadow`：开关只有一个来源——已发布配置
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from ling.infra.db import session_scope
from ling.infra.models import AttentionPollLogRow, Message, Window
from tests.integration.conftest import TestEnv
from tests.integration.test_attention_polling import _seed_unseen

pytestmark = pytest.mark.w0_web

GROUP_REF = "onebot:group:88888"
GROUP_REPLY = "好，我在。"


@pytest.fixture(autouse=True)
async def _clean_cursors(logged_in: TestEnv) -> None:  # noqa: ARG001
    """每个用例前清空调度游标。

    `due_windows` 是全局查询：不清的话，别的用例留下的到期窗口会挤进来，
    `handle_attention_tick` 就会处理到本用例没准备的那个窗口。
    """
    from sqlalchemy import delete

    from ling.infra.models import AttentionCursorRow

    async with session_scope() as session:
        await session.execute(delete(AttentionCursorRow))
    yield
    async with session_scope() as session:
        await session.execute(delete(AttentionCursorRow))


class _TwinProfileRouter:
    """同一个 router 要服务两种职责：先决策、再生成。

    真实部署里两者都走 `ModelRouter`（按 profile 选模型）；这里按 profile
    分开返回，避免"决策返回 JSON、生成也返回 JSON"这种假通过。
    """

    def __init__(self, *, reply: str = GROUP_REPLY) -> None:
        self.reply = reply
        self.decision_calls = 0
        self.reply_calls = 0

    async def stream(self, config, *, profile, messages, **kwargs):  # noqa: ANN001, ANN003
        from ling.models.router import StreamOutcome
        from ling.ports.model import ModelChunk

        if profile == "dialogue.group":
            self.decision_calls += 1
            payload = '{"respond": true, "reason": "群里这话我能接"}'
        else:
            self.reply_calls += 1
            payload = self.reply
        yield ModelChunk(delta=payload), StreamOutcome()
        yield ModelChunk(done=True), StreamOutcome(text=payload)


class _FakeConnector:
    """最小的渠道连接器：记录收到的消息，可选地明确失败。"""

    connector_id = "onebot"
    instance_id = "onebot-attention-test"

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[object] = []
        self.attempted = 0
        self._fail = fail

    def capabilities(self):  # noqa: ANN201
        from ling.contracts.v1 import CapabilityDescriptor

        return CapabilityDescriptor(
            capability_id="fake-attention",
            version="1",
            supports_private=True,
            supports_group=True,
            max_text_length=4000,
            enabled=True,
        )

    async def send(self, message):  # noqa: ANN001, ANN201
        from ling.contracts.v1 import DeliveryReceipt, DeliveryState
        from ling.infra.security import new_id
        from ling.ports.channel import ChannelError, ChannelErrorKind

        self.attempted += 1
        if self._fail:
            # 平台明确拒收 = 确定没发出去（不是 uncertain）
            raise ChannelError(ChannelErrorKind.rejected, "平台拒收：群已解散")
        self.sent.append(message)
        return DeliveryReceipt(
            receipt_id=new_id("rcpt"),
            operation_id=message.operation_id,
            channel=self.connector_id,
            state=DeliveryState.delivered,
            platform_message_id="mid_attention_test",
            observed_at=datetime.now(UTC),
        )


async def _group_window(
    env: TestEnv, *, title: str = GROUP_REF, channel: str = "onebot"
) -> str:
    """建一个真实的群窗口：visibility=group_scope、挂在 onebot 渠道上。"""
    r = await env.client.post("/v1/sessions", json={"title": "群"})
    session_id = r.json()["session_id"]
    async with session_scope() as session:
        row = await session.get(Window, session_id)
        assert row is not None
        row.visibility = "group_scope"
        row.channel = channel
        row.title = title
        row.subject_id = env.subject_id
    return session_id


def _install(
    monkeypatch: pytest.MonkeyPatch, *, reply: str = GROUP_REPLY, fail: bool = False
) -> tuple[_TwinProfileRouter, _FakeConnector]:
    from ling import worker

    router = _TwinProfileRouter(reply=reply)
    connector = _FakeConnector(fail=fail)
    monkeypatch.setattr(worker, "_MEMORY_ROUTER", router)
    # 渠道选择是本用例要钉的边界：群回复必须走**窗口所属渠道**，
    # 不能拿 proactive_connector() 顶替。这里用一个可断言的假连接器。
    monkeypatch.setattr(
        worker,
        "_group_reply_connector",
        lambda channel: connector if channel == "onebot" else None,
    )
    return router, connector


async def _set_group_reply_delivery(env: TestEnv, *, enabled: bool) -> None:
    """发布配置：群聊回复是否真的发出去。

    走的是和设置页同一条路（草稿 → 发布）。直接在库里改修订行会让本用例
    绕开校验与 `_dialogue_to_dict`，测不到用户真正能改到的那个开关。
    """
    draft = await env.client.put(
        "/v1/settings/draft",
        json={"dialogue": {"group_reply_delivery_enabled": enabled}},
    )
    assert draft.status_code == 200, draft.text
    published = await env.client.post(
        "/v1/settings/publish", json={"revision": draft.json()["revision"]}
    )
    assert published.status_code == 200, published.text


async def test_replied_decision_really_delivers(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置打开 → 真的生成了、真的调用投递、消息真的到了渠道。

    这条是修复的核心验收：回退 `if result.decision == "replied": replied += 1`
    那一版（只计数不发送），或把配置读取改回硬编码关，本用例必须失败。

    这里刻意传 `{"shadow": True}`：配置已经打开，payload 不应该再有任何
    决定权——如果哪天又变成"从 payload 读 shadow"，本用例会红。
    """
    from ling import worker

    router, connector = _install(monkeypatch)
    window_id = await _group_window(logged_in)
    ids = await _seed_unseen(window_id, ["今晚有人一起吃饭吗"])
    await _set_group_reply_delivery(logged_in, enabled=True)

    await worker.handle_attention_tick({"shadow": True})

    assert connector.sent, "决定回应了，却没有任何消息投递出去"
    assert len(connector.sent) == 1
    message = connector.sent[0]
    assert message.text == GROUP_REPLY
    assert message.is_group is True, "群窗口的回复必须按群消息投递"
    assert message.window_ref == GROUP_REF, "投递目标不是这个群的平台标识"
    assert router.decision_calls == 1, "决策调用次数变了"
    assert router.reply_calls >= 1, "决定回应后没有真的生成内容"

    async with session_scope() as session:
        # 消息的处置结果是**投递成功之后**的 replied
        target = await session.get(Message, ids[-1])
        assert target is not None and target.handled_as == "replied", target.handled_as
        # 她的话也要落库（Web 面板与审计都能看到）
        actor_rows = list(
            (
                await session.execute(
                    select(Message).where(
                        Message.window_id == window_id, Message.role == "actor"
                    )
                )
            ).scalars()
        )
        assert actor_rows and actor_rows[-1].text == GROUP_REPLY
        # 轮询日志同样记 replied（这次是真的发出去了）
        log = (
            await session.execute(
                select(AttentionPollLogRow)
                .where(AttentionPollLogRow.window_id == window_id)
                .order_by(AttentionPollLogRow.polled_at.desc())
                .limit(1)
            )
        ).scalar_one()
        assert log.decision == "replied", log.decision


async def test_delivery_failure_is_not_marked_replied(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """投递失败 → **不标 replied**，状态如实反映"决定回但没发出去"。

    只把计数器减一是不够的：消息的 `handled_as` 和轮询日志的 `decision`
    都写在 `poll_window` 里（那时还不知道投递结果），失败路径必须改回来，
    否则运维会把"她根本没发出去"读成"她回了但没人看见"。
    """
    from ling import worker

    router, connector = _install(monkeypatch, fail=True)
    window_id = await _group_window(logged_in)
    ids = await _seed_unseen(window_id, ["在吗，帮我看看这个"])
    await _set_group_reply_delivery(logged_in, enabled=True)

    with caplog.at_level(logging.INFO):
        await worker.handle_attention_tick({})

    assert connector.attempted == 1, "没有尝试投递，失败不是投递造成的"
    assert connector.sent == []
    assert router.reply_calls >= 1, "没有生成就去投递了"

    async with session_scope() as session:
        target = await session.get(Message, ids[-1])
        assert target is not None
        assert target.handled_as != "replied", "投递失败却标了 replied"
        assert target.handled_as == "failed", target.handled_as
        assert "没发出去" in (target.handled_reason or "")
        log = (
            await session.execute(
                select(AttentionPollLogRow)
                .where(AttentionPollLogRow.window_id == window_id)
                .order_by(AttentionPollLogRow.polled_at.desc())
                .limit(1)
            )
        ).scalar_one()
        assert log.decision == "reply_failed", log.decision

    # tick 汇总不能把它算成"回了"
    completed = [
        record
        for record in caplog.records
        if getattr(record, "event_name", "") == "attention_tick_completed"
    ]
    assert completed, "没有 tick 汇总事件"
    attrs = getattr(completed[-1], "attributes", {})
    assert attrs.get("replied") == 0, attrs
    assert attrs.get("reply_failed") == 1, attrs

    window_events = [
        record
        for record in caplog.records
        if getattr(record, "event_name", "") == "attention_window_polled"
    ]
    assert window_events, "没有窗口级事件"
    window_attrs = getattr(window_events[-1], "attributes", {})
    assert window_attrs.get("decision") == "reply_failed", window_attrs
    assert window_attrs.get("reply_failure"), "失败原因被吞掉了"


async def test_default_config_only_counts_reply_without_delivery(
    logged_in: TestEnv,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """配置默认关闭 → 只决策、不生成、不发送，只计数。

    默认关闭是安全默认：群里说错话收不回来。`decision=replied` 在这里
    只表示"她决定回"，不等于"已经发出去了"。

    这里刻意传 `{"shadow": False}`：payload 已经不是开关（旧版本靠它
    才会真发）。把配置读取改回硬编码、或退回从 payload 读 shadow，
    本用例都必须红。
    """
    from ling import worker

    router, connector = _install(monkeypatch)
    window_id = await _group_window(logged_in)
    ids = await _seed_unseen(window_id, ["随便聊聊"])

    with caplog.at_level(logging.INFO):
        await worker.handle_attention_tick({"shadow": False})

    assert connector.attempted == 0, "配置默认关，却擅自投递了"
    assert connector.sent == []
    assert router.decision_calls == 1
    assert router.reply_calls == 0, "配置没开就不该花生成调用"

    async with session_scope() as session:
        target = await session.get(Message, ids[-1])
        assert target is not None and target.handled_as == "replied"
        log = (
            await session.execute(
                select(AttentionPollLogRow)
                .where(AttentionPollLogRow.window_id == window_id)
                .order_by(AttentionPollLogRow.polled_at.desc())
                .limit(1)
            )
        ).scalar_one()
        assert log.decision == "replied", log.decision

    # tick 汇总：算作"决定回了"，且没有失败尝试
    completed = [
        record
        for record in caplog.records
        if getattr(record, "event_name", "") == "attention_tick_completed"
    ]
    assert completed, "没有 tick 汇总事件"
    attrs = getattr(completed[-1], "attributes", {})
    assert attrs.get("replied") == 1, attrs
    assert attrs.get("reply_failed") == 0, attrs
    assert attrs.get("reply_delivery_enabled") is False, attrs


async def test_attention_tick_periodic_payload_has_no_shadow() -> None:
    """周期任务不该再用 payload 里的 shadow 决定发不发。

    开关只有一个来源：已发布配置 `group_reply_delivery_enabled`。
    写死 `{"shadow": True}`（"决定回了"永远停在日志里）和写死
    `{"shadow": False}`（这次的临时修法）是同一类毛病——硬编码，
    用户没有开关可选。
    """
    from ling.worker import PERIODIC_JOBS

    job = next(j for j in PERIODIC_JOBS if j.topic == "attention.tick")
    assert job.payload == {}, f"attention.tick 的 payload 又有硬编码开关：{job.payload}"


async def test_proactive_collect_periodic_payload_has_no_shadow() -> None:
    """`proactive.collect` 的 payload 里不该再有装饰性的 shadow。

    它只登记候选；发不发由 `proactive.dispatch` + `proactive_delivery_enabled`
    决定。留着一个改不动任何行为的开关只会误导人。
    """
    from ling.worker import PERIODIC_JOBS

    job = next(j for j in PERIODIC_JOBS if j.topic == "proactive.collect")
    assert "shadow" not in job.payload, job.payload

