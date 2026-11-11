"""W2 模块的**领域规则**单元测试（不依赖数据库、不依赖设备）。

这些测试验证的是纯逻辑不变量，可以在任何阶段运行——它们不触碰 QQ/微信/
手机/具身的外部依赖，也不执行模拟平台或真实设备联调。

标记为 ``w0_web`` 之外的普通测试，因此默认会被收集执行；
凡是需要外部前提的用例都在 tests/w3_pending/ 里。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ling.capabilities.embodiment import (
    EmbodimentError,
    EmbodimentGateway,
    PresenceKind,
)
from ling.capabilities.expression import ExpressionService
from ling.capabilities.publishing import PublishError, PublishingService
from ling.capabilities.tools import ToolOutcome, ToolRegistry, ToolRisk, ToolSpec
from ling.contracts.v1 import ActionIntent, Origin, Visibility
from ling.kernel.policy import PolicyContext, PolicyGate
from ling.models.fake import TEST_BANNER, FakeProvider, build_fake_reply
from ling.ports.model import ChatMessageIn, ModelRequest

# ------------------------------------------------------------------ fake provider


@pytest.mark.asyncio
async def test_fake_provider_is_deterministic_and_marked() -> None:
    provider = FakeProvider(chunk_delay=0)
    request = ModelRequest(
        model_id="deterministic",
        messages=[ChatMessageIn(role="user", content="你好")],
    )
    first = "".join([c.delta async for c in provider.stream(request)])
    second = "".join([c.delta async for c in provider.stream(request)])
    assert first == second, "fake provider 必须确定性"
    assert TEST_BANNER in first, "假回复必须带测试标记"


def test_fake_reply_differs_by_input() -> None:
    a = build_fake_reply([ChatMessageIn(role="user", content="A")], model_id="m")
    b = build_fake_reply([ChatMessageIn(role="user", content="B")], model_id="m")
    assert a != b


# ------------------------------------------------------------------ 表达式


def test_expression_splits_and_preserves_text() -> None:
    # ExpressionService 有 64 字符下限（避免把一句话切得太碎），因此用长文本
    service = ExpressionService(max_text_length=64)
    text = "第一句话。第二句话！第三句话？" * 8
    plan = service.plan_text(text)
    assert len(plan.chunks) > 1
    assert plan.full_text == text, "切分不得丢字"
    assert [c.index for c in plan.chunks] == list(range(len(plan.chunks)))


def test_expression_degrades_speech_to_text() -> None:
    service = ExpressionService(supports_speech=False)
    plan = service.plan_speech("你好")
    assert plan.degraded is True
    assert "降级" in (plan.degrade_reason or "")
    assert plan.full_text == "你好"


# ------------------------------------------------------------------ 策略门


def _ctx(**overrides) -> PolicyContext:
    base = {
        "subject_id": "subj_1",
        "window_id": "win_1",
        "visibility": Visibility.subject_private,
        "is_group": False,
    }
    base.update(overrides)
    return PolicyContext(**base)  # type: ignore[arg-type]


def _intent(kind: str, **kw) -> ActionIntent:
    return ActionIntent(intent_id="i1", kind=kind, **kw)


def test_policy_allows_plain_reply() -> None:
    decision = PolicyGate().evaluate(_intent("reply.text"), _ctx())
    assert decision.allowed is True


def test_policy_rejects_unregistered_action() -> None:
    decision = PolicyGate().evaluate(_intent("some.new.action"), _ctx())
    assert decision.allowed is False
    assert "未登记" in decision.reasons[0]


def test_policy_rejects_forbidden_action_even_with_grants() -> None:
    decision = PolicyGate().evaluate(
        _intent("identity.impersonate"),
        _ctx(camera_granted=True, location_granted=True, publish_granted=True),
    )
    assert decision.allowed is False


def test_policy_blocks_camera_and_location_by_default() -> None:
    gate = PolicyGate()
    assert gate.evaluate(_intent("perception.camera"), _ctx()).allowed is False
    assert gate.evaluate(_intent("perception.location"), _ctx()).allowed is False
    assert (
        gate.evaluate(_intent("perception.camera"), _ctx(camera_granted=True)).allowed is True
    )


def test_policy_blocks_private_content_in_group() -> None:
    decision = PolicyGate().evaluate(
        _intent("reply.text"),
        _ctx(is_group=True, visibility=Visibility.subject_private, channel_supports_group=True),
    )
    assert decision.allowed is False
    assert any("群聊" in r for r in decision.reasons)


def test_policy_requires_confirmation_for_publish() -> None:
    decision = PolicyGate().evaluate(
        _intent("publish.public"), _ctx(publish_granted=True)
    )
    assert decision.allowed is True
    assert decision.requires_confirmation is True


def test_policy_blocks_proactive_without_channel_support() -> None:
    decision = PolicyGate().evaluate(
        _intent("proactive.contact"),
        _ctx(channel_supports_proactive=False),
    )
    assert decision.allowed is False


def test_policy_blocks_when_budget_exhausted() -> None:
    decision = PolicyGate().evaluate(_intent("reply.text"), _ctx(budget_remaining=0.0))
    assert decision.allowed is False


def test_visible_memory_scopes_exclude_private_in_group() -> None:
    scopes = PolicyGate().visible_memory_scopes(_ctx(is_group=True))
    assert Visibility.subject_private not in scopes
    assert Visibility.group_scope in scopes


# ------------------------------------------------------------------ 工具


@pytest.mark.asyncio
async def test_tool_requires_confirmation_and_blocks_irreversible_by_default() -> None:
    registry = ToolRegistry()

    async def handler(args):  # noqa: ANN001
        return "done"

    with pytest.raises(PermissionError):
        registry.register(
            ToolSpec(
                tool_id="danger",
                version="1",
                description="不可逆",
                risk=ToolRisk.irreversible,
                requires_confirmation=True,
                reversible=False,
            ),
            handler,
        )


@pytest.mark.asyncio
async def test_unregistered_tool_returns_unsupported() -> None:
    registry = ToolRegistry()
    result = await registry.call("nope", {})
    assert result.outcome is ToolOutcome.unsupported
    assert result.untrusted is True


@pytest.mark.asyncio
async def test_readonly_shell_rejects_metacharacters() -> None:
    from ling.capabilities.tools import build_default_registry

    registry = build_default_registry()
    result = await registry.call("shell.readonly", {"command": "date; rm -rf /"})
    assert result.outcome is ToolOutcome.failed
    assert "元字符" in (result.detail or "")


@pytest.mark.asyncio
async def test_readonly_shell_rejects_non_whitelisted() -> None:
    from ling.capabilities.tools import build_default_registry

    registry = build_default_registry()
    result = await registry.call("shell.readonly", {"command": "curl http://example.com"})
    assert result.outcome is ToolOutcome.failed


# ------------------------------------------------------------------ 具身


def test_perception_defaults_off() -> None:
    gateway = EmbodimentGateway()
    session = gateway.start_session(
        subject_id="subj_1", device_id="dev_1", presence=PresenceKind.phone_foreground
    )
    assert session.perception_active("microphone") is False
    assert session.perception_active("camera") is False
    assert session.presence_valid() is True


def test_perception_grant_expires() -> None:
    gateway = EmbodimentGateway()
    session = gateway.start_session(
        subject_id="subj_1", device_id="dev_1", presence=PresenceKind.phone_foreground
    )
    gateway.grant_perception(session.session_id, "microphone", ttl_minutes=30)
    assert session.perception_active("microphone") is True
    future = datetime.now(UTC) + timedelta(hours=1)
    assert session.perception_active("microphone", now=future) is False


def test_offline_invalidates_presence_and_perception() -> None:
    gateway = EmbodimentGateway()
    session = gateway.start_session(
        subject_id="subj_1", device_id="dev_1", presence=PresenceKind.phone_foreground
    )
    gateway.grant_perception(session.session_id, "microphone")
    gateway.set_presence(session.session_id, PresenceKind.offline)
    assert session.presence_valid() is False
    assert session.perception_active("microphone") is False
    with pytest.raises(EmbodimentError):
        gateway.begin_turn(session.session_id)


def test_avatar_action_whitelist() -> None:
    gateway = EmbodimentGateway()
    session = gateway.start_session(
        subject_id="subj_1", device_id="dev_1", presence=PresenceKind.vr
    )
    intent = gateway.avatar_intent(session_id=session.session_id, action="nod")
    assert intent["action"] == "nod"
    with pytest.raises(EmbodimentError):
        gateway.avatar_intent(session_id=session.session_id, action="launch_rocket")


def test_late_audio_rejected_after_interrupt() -> None:
    gateway = EmbodimentGateway()
    session = gateway.start_session(
        subject_id="subj_1", device_id="dev_1", presence=PresenceKind.phone_foreground
    )
    gateway.grant_perception(session.session_id, "microphone")
    turn_id, _ = gateway.begin_turn(session.session_id)
    assert gateway.accept_audio(
        session_id=session.session_id, turn_id=turn_id, sequence=0, is_final=False
    )
    assert gateway.interrupt_turn(session.session_id, turn_id) is True
    # 旧轮的迟到音频必须被丢弃
    assert (
        gateway.accept_audio(
            session_id=session.session_id, turn_id=turn_id, sequence=1, is_final=True
        )
        is False
    )


def test_spatial_observation_filters_to_low_sensitivity_fields() -> None:
    gateway = EmbodimentGateway()
    session = gateway.start_session(
        subject_id="subj_1", device_id="dev_1", presence=PresenceKind.vr
    )
    with pytest.raises(EmbodimentError):
        gateway.accept_spatial_observation(
            session_id=session.session_id, semantics={"room_kind": "living"}
        )
    gateway.grant_perception(session.session_id, "spatial")
    obs = gateway.accept_spatial_observation(
        session_id=session.session_id,
        semantics={"room_kind": "living", "raw_pointcloud": "b64..."},
    )
    assert "room_kind" in obs["semantics"]
    assert "raw_pointcloud" not in obs["semantics"]


# ------------------------------------------------------------------ 发布


class _StubConsent:
    """替身：只回答"这个 scope 授予了没有"，不碰数据库。"""

    def __init__(self, granted: bool) -> None:
        self._granted = granted
        self.seen: list[tuple[str, str]] = []

    async def is_active(self, subject_id: str, scope: str) -> bool:
        self.seen.append((subject_id, scope))
        return self._granted


@pytest.mark.asyncio
async def test_publishing_capability_off_by_default() -> None:
    """宿主能力默认关；即使主体已授权也发不出去。"""
    service = PublishingService(capability_enabled=False)
    with pytest.raises(PublishError, match="宿主未启用"):
        await service.create_draft(
            subject_id="s", channel="web", title="t", body="b",
            consent=_StubConsent(granted=True),
        )


@pytest.mark.asyncio
async def test_publishing_host_capability_alone_is_not_authorization() -> None:
    """这是原来的缺陷：env=1 就绕过了一切授权记录。宿主有能力 != 主体已授权。"""
    service = PublishingService(capability_enabled=True)
    stub = _StubConsent(granted=False)
    with pytest.raises(PublishError, match="public_publish"):
        await service.create_draft(
            subject_id="s", channel="web", title="t", body="b", consent=stub,
        )
    # 必须真的去查了授权，而不是只看布尔
    assert stub.seen == [("s", "public_publish")]


@pytest.mark.asyncio
async def test_publishing_without_consent_object_is_denied() -> None:
    """没有授权来源时失败关闭，不是失败放行。"""
    service = PublishingService(capability_enabled=True)
    with pytest.raises(PublishError):
        await service.create_draft(
            subject_id="s", channel="web", title="t", body="b", consent=None,
        )


@pytest.mark.asyncio
async def test_publishing_requires_real_receipt() -> None:
    service = PublishingService(capability_enabled=True)
    draft = await service.create_draft(
        subject_id="s", channel="web", title="t", body="b",
        consent=_StubConsent(granted=True),
    )
    service.submit_for_approval(draft.draft_id)
    with pytest.raises(PublishError):
        service.mark_published(draft.draft_id, platform_ref="x")  # 未批准
    service.approve(draft.draft_id)
    with pytest.raises(PublishError):
        service.mark_published(draft.draft_id, platform_ref="")
    published = service.mark_published(draft.draft_id, platform_ref="post-123")
    assert published.platform_ref == "post-123"


# ------------------------------------------------------------------ 记忆规则（纯函数）


def test_memory_origin_model_inference_not_promoted() -> None:
    """模型推测不得写入长期记忆：这一条在 MemoryService.propose 里硬编码。"""
    import inspect

    from ling.domains.memory.service import MemoryService

    source = inspect.getsource(MemoryService.propose)
    assert "model_inference" in source
    assert "return None" in source


def test_quiet_hours_helper_crosses_midnight() -> None:
    from datetime import time

    from ling.domains.proactive.service import _in_quiet_hours

    assert _in_quiet_hours(time(23, 30), time(23, 0), time(8, 0)) is True
    assert _in_quiet_hours(time(3, 0), time(23, 0), time(8, 0)) is True
    assert _in_quiet_hours(time(12, 0), time(23, 0), time(8, 0)) is False


def test_activity_state_machine_blocks_completed_without_evidence() -> None:
    from ling.domains.activity.service import TRANSITIONS, VALID_STATES

    assert "completed" not in TRANSITIONS["proposed"]
    assert {
        "proposed",
        "running",
        "paused",
        "completed",
        "failed",
        "cancelled",
    } == VALID_STATES


def test_relationship_event_effects_cover_known_kinds() -> None:
    from ling.domains.relationship.service import EVENT_EFFECTS

    assert set(EVENT_EFFECTS) == {
        "warmth",
        "shared_experience",
        "conflict",
        "repair",
        "boundary",
        "disclosure",
    }


def test_origin_enum_has_six_sources() -> None:
    assert len(list(Origin)) == 6


# ------------------------------------------------------------------ 发言仲裁


def test_arbiter_allows_one_speaker_and_queues_others() -> None:
    """同一角色同一时刻只在一个窗口说话；其他窗口排队而不是被拒。"""
    from ling.kernel.arbiter import SpeakerArbiter

    arbiter = SpeakerArbiter()
    first = arbiter.acquire(window_id="win_a", turn_id="t1")
    assert first.acquired is True
    assert arbiter.holder() == "win_a"

    second = arbiter.acquire(window_id="win_b", turn_id="t2")
    assert second.acquired is False
    assert second.holder_window_id == "win_a"
    assert second.queue_position == 1
    assert "排队" in (second.reason or "")

    third = arbiter.acquire(window_id="win_c", turn_id="t3")
    assert third.acquired is False
    assert third.queue_position == 2


def test_arbiter_same_turn_renews_but_new_turn_queues() -> None:
    """同一轮续约；同窗口的**不同轮次**必须排队（此前实现的缺陷）。"""
    from ling.kernel.arbiter import SpeakerArbiter

    arbiter = SpeakerArbiter()
    arbiter.acquire(window_id="win_a", turn_id="t1")
    # 同一轮：续约
    same = arbiter.acquire(window_id="win_a", turn_id="t1")
    assert same.acquired is True
    assert arbiter.holder() == "win_a"
    # 同窗口的新轮次：排队，不能直接替换占用
    new_turn = arbiter.acquire(window_id="win_a", turn_id="t2")
    assert new_turn.acquired is False
    assert "同一窗口" in (new_turn.reason or "")
    assert arbiter.queue() == ["win_a#t2"]


def test_arbiter_release_promotes_next_waiter() -> None:
    """释放后**自动提升**队首（注意：原先排队者永远拿不到）。"""
    from ling.kernel.arbiter import SpeakerArbiter

    arbiter = SpeakerArbiter()
    arbiter.acquire(window_id="win_a", turn_id="t1")
    arbiter.acquire(window_id="win_b", turn_id="t2")
    assert arbiter.queue() == ["win_b#t2"]

    assert arbiter.release(window_id="win_a", turn_id="t1") is True
    # 队首被提升为持有者
    assert arbiter.holder() == "win_b"
    snap = arbiter.snapshot()
    assert snap["holder_turn_id"] == "t2"
    assert snap["queue"] == []


def test_arbiter_lease_expires_so_crash_does_not_lock() -> None:
    """进程崩溃没释放时，租约超时后自动可用，不会永久锁死。"""
    from datetime import UTC, datetime, timedelta

    from ling.kernel.arbiter import SpeakerArbiter

    arbiter = SpeakerArbiter(default_lease_seconds=10)
    lease = arbiter.acquire(window_id="win_a", turn_id="t1").lease
    assert lease is not None
    # 手工把租约改成已过期
    lease.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert arbiter.holder() is None
    got = arbiter.acquire(window_id="win_b", turn_id="t2")
    assert got.acquired is True


def test_arbiter_extend_keeps_long_reply_alive() -> None:
    from ling.kernel.arbiter import SpeakerArbiter

    arbiter = SpeakerArbiter(default_lease_seconds=10)
    arbiter.acquire(window_id="win_a", turn_id="t1")
    assert arbiter.extend(window_id="win_a", turn_id="t1") is True
    # 别的窗口/别的轮次不能续约
    assert arbiter.extend(window_id="win_b", turn_id="t1") is False
    assert arbiter.extend(window_id="win_a", turn_id="other") is False


# ------------------------------------------------------------------ 流式续传


def test_stream_log_returns_only_new_chunks() -> None:
    from ling.kernel.stream_log import StreamLog

    log = StreamLog()
    log.append(turn_id="t1", attempt=1, text="你")
    log.append(turn_id="t1", attempt=1, text="好")
    log.append(turn_id="t1", attempt=1, text="呀")

    chunks, complete = log.since(turn_id="t1", attempt=1, last_sequence=0)
    assert complete is True
    assert [c.text for c in chunks] == ["你", "好", "呀"]

    # 从游标 2 续传，只拿到第 3 段
    chunks2, complete2 = log.since(turn_id="t1", attempt=1, last_sequence=2)
    assert complete2 is True
    assert [c.text for c in chunks2] == ["呀"]

    assert log.full_text(turn_id="t1", attempt=1) == "你好呀"


def test_stream_log_reports_incomplete_for_unknown_turn() -> None:
    """游标指向不存在的轮次时必须明确"接不上"，而不是静默返回空。"""
    from ling.kernel.stream_log import StreamLog

    log = StreamLog()
    chunks, complete = log.since(turn_id="nope", attempt=1, last_sequence=0)
    assert chunks == []
    assert complete is False


def test_stream_log_separates_attempts() -> None:
    from ling.kernel.stream_log import StreamLog

    log = StreamLog()
    log.append(turn_id="t1", attempt=1, text="旧")
    log.append(turn_id="t1", attempt=2, text="新")
    a1, _ = log.since(turn_id="t1", attempt=1, last_sequence=0)
    a2, _ = log.since(turn_id="t1", attempt=2, last_sequence=0)
    assert [c.text for c in a1] == ["旧"]
    assert [c.text for c in a2] == ["新"]


def test_stream_log_caps_chunks_but_keeps_monotonic_sequence() -> None:
    from ling.kernel.stream_log import MAX_CHUNKS_PER_TURN, StreamLog

    log = StreamLog()
    for i in range(MAX_CHUNKS_PER_TURN + 50):
        log.append(turn_id="t1", attempt=1, text=str(i))
    chunks, _ = log.since(turn_id="t1", attempt=1, last_sequence=0)
    seqs = [c.sequence for c in chunks]
    assert len(chunks) <= MAX_CHUNKS_PER_TURN
    assert seqs == sorted(seqs), "sequence 必须单调"


def test_stream_log_clear() -> None:
    from ling.kernel.stream_log import StreamLog

    log = StreamLog()
    log.append(turn_id="t1", attempt=1, text="x")
    log.clear(turn_id="t1")
    _, complete = log.since(turn_id="t1", attempt=1, last_sequence=0)
    assert complete is False


# ------------------------------------------------------------------ 接话决策


def _dctx(**kw):
    from ling.kernel.decision import DecisionContext

    base = {"is_group": False}
    base.update(kw)
    return DecisionContext(**base)  # type: ignore[arg-type]


def test_private_chat_always_replies() -> None:
    """私聊里用户直接对角色说话，沉默会被当成坏了。"""
    from ling.kernel.decision import DecisionEngine, TurnDecision

    engine = DecisionEngine()
    assert engine.decide(_dctx(is_group=False)).decision is TurnDecision.reply
    assert engine.decide(_dctx(is_group=False, is_question=True)).decision is TurnDecision.reply


def test_private_empty_message_asks_for_clarification() -> None:
    from ling.kernel.decision import DecisionEngine, TurnDecision

    result = DecisionEngine().decide(_dctx(is_group=False, has_meaningful_text=False))
    assert result.decision is TurnDecision.clarify


def test_group_plain_chat_is_hard_silence() -> None:
    from ling.kernel.decision import DecisionEngine, TurnDecision

    result = DecisionEngine().decide(_dctx(is_group=True))
    assert result.decision is TurnDecision.silence
    assert result.hard is True
    assert result.silent_ack == "react", "沉默时应有无声存在感"


def test_group_mention_forces_reply_and_bypasses_budget() -> None:
    """被点名必须答，且不受群内频率限制。"""
    from ling.kernel.decision import DecisionEngine, TurnDecision

    engine = DecisionEngine()
    result = engine.decide(
        _dctx(is_group=True, mentioned=True, recent_actor_turns=99, group_turn_budget=3)
    )
    assert result.decision is TurnDecision.reply
    assert result.hard is True


def test_group_flood_is_hard_silence() -> None:
    """群内已刷屏时硬性不答，避免变成噪音源。"""
    from ling.kernel.decision import DecisionEngine, TurnDecision

    result = DecisionEngine().decide(
        _dctx(is_group=True, is_question=True, recent_actor_turns=3, group_turn_budget=3)
    )
    assert result.decision is TurnDecision.silence
    assert result.hard is True


def test_group_generic_question_is_soft_decision() -> None:
    """泛问句未指向角色：默认不接，但允许模型判断（hard=False）。"""
    from ling.kernel.decision import DecisionEngine

    result = DecisionEngine().decide(_dctx(is_group=True, is_question=True))
    assert result.hard is False


def test_group_continuation_is_soft_decision() -> None:
    from ling.kernel.decision import DecisionEngine

    result = DecisionEngine().decide(_dctx(is_group=True, addressed_to_actor=True))
    assert result.hard is False


def test_group_emoji_only_is_silent() -> None:
    from ling.kernel.decision import DecisionEngine, TurnDecision

    result = DecisionEngine().decide(_dctx(is_group=True, has_meaningful_text=False))
    assert result.decision is TurnDecision.silence


def test_classify_text_detects_question_and_mention() -> None:
    from ling.kernel.decision import classify_text

    assert classify_text("今天天气怎么样？")[0] is True
    assert classify_text("ling 你怎么看")[1] is True
    assert classify_text("ling 你说的对")[2] is True
    assert classify_text("嗯嗯")[0] is False


def test_group_budget_counts_within_window() -> None:
    from ling.kernel.decision import GroupBudgetRegistry

    registry = GroupBudgetRegistry(window_seconds=300, limit=3)
    budget = registry.budget("win_g")
    assert budget.count(now=1000.0) == 0
    budget.mark(1000.0)
    budget.mark(1010.0)
    assert budget.count(now=1015.0) == 2
    # 超出窗口后过期
    assert budget.count(now=1400.0) == 0


def test_has_meaningful_text() -> None:
    from ling.kernel.decision import DecisionEngine

    assert DecisionEngine.has_meaningful_text("你好") is True
    assert DecisionEngine.has_meaningful_text("   ") is False
    assert DecisionEngine.has_meaningful_text("😀😀") is False


# ------------------------------------------------------------------ 分段规划


def test_plan_parts_never_loses_text() -> None:
    """各段按序拼接必须等于原文——**可机械检查的硬约束**。

    分段过程丢字比"一次说完"严重得多，所以规划器自己就验证这一条，
    不满足时退回单条。
    """
    from ling.capabilities.chunking import plan_parts

    text = (
        "第一件事说完了，讲的是老城区怎么长起来的。"
        "第二件事是说那本书我顺手也拿了，等看完可以聊聊。"
        "第三件是晚上做番茄牛腩，炖久一点更入味。"
        "最后还有一点，你之前说的事我觉得先别急。"
    )
    parts = plan_parts(text, is_group=False)
    assert "".join(parts) == text


def test_plan_parts_keeps_code_block_whole() -> None:
    """代码块绝不能被切开——切开会把可执行内容变成不可执行。"""
    from ling.capabilities.chunking import plan_parts

    text = (
        "先看这段代码，这是核心逻辑：\n"
        "```python\n"
        "def f():\n"
        "    return 1\n"
        "```\n"
        "下面这些是配置说明，可以按需调整顺序和位置，不影响运行结果。"
        "另外还有一节关于部署的注意事项，也一并写在这里方便一起看。"
    )
    parts = plan_parts(text, is_group=False)
    # 内容级比较：分段会重新编排换行与首尾空白（那是分段本身的一部分），
    # 但**一个字都不许增删**。严格比字符串会在"按换行分段"下必然失败。
    import re as _re

    squash = lambda s: _re.sub(r"\s+", "", s)  # noqa: E731
    assert squash("".join(parts)) == squash(text), "分段丢字了"
    holders = [p for p in parts if "```python" in p]
    assert len(holders) == 1, f"代码块被切开了：{parts}"
    assert "return 1" in holders[0], "代码块内容被切断"


def test_plan_parts_group_is_single() -> None:
    """群聊恒为一条：拆成多条会绕过防刷屏上限。"""
    from ling.capabilities.chunking import plan_parts

    long_text = "。".join(f"这是第{i}句比较长的话用来撑长度" for i in range(20)) + "。"
    assert len(plan_parts(long_text, is_group=True)) == 1


def test_plan_parts_overflow_merges_instead_of_dropping() -> None:
    """超出段数上限的部分要**并回最后一段**，不能丢。

    这一条专门盯"截断"：分段算法最容易犯的错是 `segments[:3]` 直接把
    后面的丢掉。丢字比"一次说完"严重得多，所以规划器必须自己验证
    「拼回去等于原文」。
    """
    from ling.capabilities.chunking import MAX_PARTS_PRIVATE, plan_parts

    # 必须真的超过上限才会走到溢出分支：12 句只产生 3 段（正好等于上限），
    # 20 句才产生 4 段。造少了这条测试就变成了假绿。
    text = "。".join(f"这是第{i}句话，用来把长度撑到需要分很多段" for i in range(20)) + "。"
    parts = plan_parts(text, is_group=False)

    assert 1 <= len(parts) <= MAX_PARTS_PRIVATE, f"段数超上限：{len(parts)}"
    assert "".join(parts) == text, "分段落字了"
    assert parts[-1].endswith("。"), "最后一段被截断"


# ---------------------------------------------------------------- 调度间隔一致


def test_attention_tick_is_faster_than_poll_intervals() -> None:
    """调度间隔必须**小于**轮询器自己的间隔。

    这条曾经被违反：轮询器挂在 300 秒的收集器块里，而它自己的间隔是
    基础 30 秒 / 聚焦 8 秒——于是"聚焦 8 秒"永远不会发生，
    那些数字只是纸面参数，功能形同不存在。

    这是个很容易再犯的错：加一个新 handler 时顺手塞进已有的定时块。
    """
    from ling.domains.attention.service import (
        BASE_INTERVAL_SECONDS,
        FOCUS_INTERVAL_SECONDS,
    )
    from ling.worker import ATTENTION_TICK_SECONDS

    assert ATTENTION_TICK_SECONDS < BASE_INTERVAL_SECONDS, (
        f"调度 {ATTENTION_TICK_SECONDS}s 慢于基础轮询 {BASE_INTERVAL_SECONDS}s，"
        "轮询会被调度拖后腿"
    )
    assert ATTENTION_TICK_SECONDS < FOCUS_INTERVAL_SECONDS, (
        f"调度 {ATTENTION_TICK_SECONDS}s 慢于聚焦轮询 {FOCUS_INTERVAL_SECONDS}s，"
        "聚焦加速形同不存在"
    )


async def test_guarded_isolates_failures() -> None:
    """后台任务之间**不该有隐式依赖**：一件失败不能吞掉后面几件。

    原来四件事共用一个 try，收集器一炸后面全跳过。
    """
    from ling.worker import _guarded

    called: list[str] = []

    async def boom(_payload: dict) -> None:
        raise RuntimeError("炸了")

    async def fine(_payload: dict) -> None:
        called.append("ok")

    await _guarded("会炸的", boom, {})
    await _guarded("正常的", fine, {})
    assert called == ["ok"], "前一个任务失败后，后面的任务没有继续执行"


# ---------------------------------------------------------------- traits 必须生效


def test_format_traits_renders_key_values() -> None:
    from ling.domains.persona.service import format_traits

    out = format_traits({"tone": "冷淡", "curiosity": 0.2})
    assert "tone：冷淡" in out
    assert "curiosity：0.2" in out


def test_persona_is_empty_judge() -> None:
    """「人设是否为空」只有一个判据，三列**全空**才算空。

    引子：没有默认人设了（2026-11-11），空人设什么都不注入。所以"空"的判断
    必须精确——判宽了会把用户写的东西丢掉，判窄了会让新装用户被当成"有人设"，
    「暂无人设 + 新建人设」的空状态就永远出不来。

    **display_name 不参与判据**：它只是名字（时间线/日程/剥自我前缀用它），
    新装行里本来就是 `"ling"`；算进来会让每个新装用户都被判成"有人设"。
    """
    from ling.domains.persona.service import PersonaView, persona_is_empty, view_is_empty

    # 三列全空 → 空
    assert persona_is_empty(system_prompt="", traits={}, boundaries=[]) is True
    assert persona_is_empty() is True
    # 全空白也算空（空白不是设定，注入它只会让模型收到一段空指令）
    assert persona_is_empty(system_prompt="   \n\t") is True
    # 有键但值全空：format_traits 渲染就是空串，所以也算空（两边判断必须一致）
    assert persona_is_empty(traits={"a": "", "b": None}) is True
    # boundaries 里全是空白串 → 空
    assert persona_is_empty(boundaries=["", "   "]) is True

    # 任一列有真内容 → 非空
    assert persona_is_empty(system_prompt="你是小满。") is False
    assert persona_is_empty(traits={"tone": "冷淡"}) is False
    assert persona_is_empty(boundaries=["不编造现实履历"]) is False

    # 历史脏数据：类型不对 / NULL 一律按空处理，不抛异常
    assert persona_is_empty(system_prompt=None, traits=None, boundaries=None) is True
    assert persona_is_empty(traits=[], boundaries="不是列表") is True  # type: ignore[arg-type]

    def _view(**kwargs: object) -> PersonaView:
        base: dict = {
            "persona_id": "p",
            "revision": 1,
            "name": "ling",
            "display_name": "ling",
            "system_prompt": "",
            "traits": {},
            "boundaries": [],
        }
        base.update(kwargs)
        return PersonaView(**base)

    # view 版本与裸字段版本同一个判据
    assert view_is_empty(_view()) is True
    assert view_is_empty(_view(system_prompt="你是小满。")) is False
    # display_name 不影响结论：名字是"ling"、其余全空，仍然是空人设
    assert view_is_empty(_view(display_name="ling")) is True


def test_persona_view_is_returned_as_stored() -> None:
    """`_to_view` **原样**返回：空就是空，绝不回落成任何默认值。

    这条防的是上一版踩过的坑：读取时回落 → 前端输入框显示默认值 → 用户一点
    保存 → 默认值被写进库、从此冻结，于是"空 = 跟随默认"这个设计根本存不下来。
    """
    from ling.domains.persona.service import PersonaService
    from ling.infra.models import Persona

    row = Persona(
        id="p",
        revision=1,
        name="ling",
        display_name="ling",
        system_prompt="",
        traits={},
        boundaries=[],
        scenario_prompts={},
        published=True,
    )
    view = PersonaService._to_view(row)
    assert view.system_prompt == ""
    assert view.traits == {}
    assert view.boundaries == []
    # 形状兜底（NULL / 脏数据）不得把"空"变成"有内容"
    row.system_prompt = None  # type: ignore[assignment]
    row.traits = None  # type: ignore[assignment]
    row.boundaries = None  # type: ignore[assignment]
    view = PersonaService._to_view(row)
    assert view.system_prompt == ""
    assert view.traits == {}
    assert view.boundaries == []


def test_no_default_persona_constants_exist() -> None:
    """**没有默认人设**：这三个常量必须彻底不存在。

    为什么不是"改成空值"：`DEFAULT_*` 这个名字本身就在说"这里有一份默认人设"。
    留空壳常量，下一个人会往里填内容，而且每个引用点都得写
    `if DEFAULT_SYSTEM_PROMPT:` 这种恒假判断。所以这里断言的是"名字不存在"：
    有人想把默认人设加回来，必须先改掉这条用例并解释原因。
    """
    import ling.domains.persona as pkg
    import ling.domains.persona.service as svc

    for name in ("DEFAULT_SYSTEM_PROMPT", "DEFAULT_TRAITS", "DEFAULT_BOUNDARIES"):
        assert not hasattr(svc, name), f"{name} 又回来了——默认人设不该存在"
    # 上一版的读取时回落函数同样必须不存在
    for name in ("effective_prompt", "effective_traits", "effective_boundaries"):
        assert not hasattr(svc, name), f"{name} 又回来了——回落会让空人设存不下来"
    assert not hasattr(pkg, "DEFAULT_SYSTEM_PROMPT")


def test_empty_persona_renders_nothing_to_send() -> None:
    """三列都为空时，人设车道**渲染不出可发送的内容**——不能出现 `content=""`。

    这是从 `tests/integration/test_persona_injection.py` 搬过来的保护：
    那条集成用例原来靠"库里清空两列"制造空人设，但回落语义落地后，库里空
    读出来就是默认人设了——**"空"只能由渲染结果为空**来构造。

    两点分工写清楚，免得以后有人以为下面那条白名单是有意保留的行为：

    1. `format_traits` 渲染为空（键存在但值全空）→ 拼出来的文本本来就是 `""`；
    2. `system_prompt` 只有空白（`"  \n"`）→ 拼出来的文本是空白串，**不是** `""`。
       这一点由 `ContextCompiler` 的末道过滤兜住：所有贡献统一
       ``[c for c in contributions if c.text.strip()]``（见 `kernel/context.py`
       里"空文本的车道整条不发"那段注释），所以空白串同样不发。
       这里断言的是**不发**这个结果，而不是"拼接结果为 `""`"。

    真正会触发它的情形仍在：老数据/手工改库把 `system_prompt` 写成空白、
    且 `traits` 里全是空值。
    """
    from ling.domains.persona.service import format_traits

    for prompt, traits in (
        ("", {}),
        ("", None),
        ("", {"a": "", "b": None}),  # 有键但渲染为空
        ("   \n", {"a": "   "}),  # 只有空白：靠 `kernel/context.py` 的 strip 过滤兜住
    ):
        # 与 `kernel/context.py` 里人设车道的拼装表达式一致
        persona_text = "\n\n".join(
            part for part in (prompt, format_traits(traits)) if part
        )
        # 与 `ContextCompiler` 的末道过滤一致：空白也不占消息位
        assert not persona_text.strip(), f"人设车道会发出一条空消息：{persona_text!r}"


def test_format_traits_empty_returns_nothing() -> None:
    """空 traits 不能产出半句空话——那只是白占预算。"""
    from ling.domains.persona.service import format_traits

    assert format_traits(None) == ""
    assert format_traits({}) == ""
    assert format_traits({"a": "", "b": None}) == ""
    assert format_traits({"a": "   "}) == ""


def test_format_traits_is_bounded() -> None:
    """traits 是自由 dict，不能让它把上下文预算吃光。

    上限本身放宽过（12 条/120 字/600 字 → 40 条/1200 字/4000 字），因为
    原来那个量级会把用户真写的设定切掉，而**切掉时不声不响**。所以这里断言
    的是"有界且留痕"，不是某个具体的小数字。
    """
    from ling.domains.persona.service import (
        _MAX_TRAIT_CHARS,
        _MAX_TRAIT_ITEMS,
        format_traits,
    )

    huge = {f"k{i}": "x" * 5000 for i in range(40)}
    out = format_traits(huge)
    # 人设车道是 10000 token ≈ 20000 字符，traits 只占其中一块
    assert len(out) <= _MAX_TRAIT_CHARS + 200, f"渲染太长了：{len(out)}"
    assert out.count("\n- ") <= _MAX_TRAIT_ITEMS, "没限制条目数"
    # **截断必须留痕**：不声不响地切掉等于"写了等于没写"
    assert "…（" in out, "超限时没有留下任何痕迹"


def test_format_traits_marks_every_truncation() -> None:
    """三种截断（单条过长 / 合计过长 / 条目过多）都要能看出来。

    上限随人设预算一起放宽过（40 条/1200 字/4000 字 → 400 条/4000 字/120000 字），
    所以这里用**常量本身**造超限数据，而不是写死一个数字——否则上限一改这条
    测试就会假绿（造的数据根本没超限）。
    """
    from ling.domains.persona.service import (
        _MAX_TRAIT_CHARS,
        _MAX_TRAIT_ITEM_CHARS,
        _MAX_TRAIT_ITEMS,
        format_traits,
    )

    # 单条过长：逐条留痕
    single = format_traits({"说话方式": "设定" * (_MAX_TRAIT_ITEM_CHARS // 2 + 100)})
    assert "这条太长" in single, "单条超限没留痕"

    # 合计过长：末尾留痕
    huge = {f"k{i}": "设" * (_MAX_TRAIT_ITEM_CHARS - 10) for i in range(40)}
    assert len(format_traits(huge)) > _MAX_TRAIT_CHARS
    assert "没放进提示词" in format_traits(huge), "合计超限没留痕"

    # 条目过多：末尾给总账，且明确还有几条没进来
    many = {f"k{i}": f"第{i}条" for i in range(_MAX_TRAIT_ITEMS + 20)}
    out = format_traits(many)
    assert "条性格特征没放进提示词" in out

    # 没有超限时不该出现任何"没放进"的字样（不能狼来了）
    normal = format_traits({"tone": "冷淡"})
    assert "没放进" not in normal


def test_format_traits_does_not_assume_known_keys() -> None:
    """代码不该假定 traits 有哪些键。

    traits 是使用者的自由字段——想让她在群里话多，写个头就生效，
    不需要改代码。这正是"行为归人设"的落点。
    """
    from ling.domains.persona.service import format_traits

    out = format_traits({"group_chat": "群里很爱搭话，看到感兴趣的就接一句"})
    assert "group_chat" in out
    assert "爱搭话" in out


# ---------------------------------------------------------------- 上下文预算


def test_total_budget_follows_the_persona_budget() -> None:
    """总预算 = 车道和 + 最近原文层；人设提到 10 万后合计约 149k。

    原来合计只有约 8.3k——紧到人格剩 3.6k、最近对话只留 400。后来是 50k，
    2026-11-09 用户拍板「人设上限 10 万 token」，于是人设 10k → 100k，
    **其余车道一个都没动**（仍是 28600）。

    这条测试钉的是**结构关系**（总预算由车道和推出），不是某个具体数字：
    数字变了要改的是 `DEFAULT_BUDGETS`，不是这里。
    """
    from ling.kernel.context import DEFAULT_BUDGETS, HISTORY_TOKEN_BUDGET

    lanes = sum(DEFAULT_BUDGETS.values())
    total = lanes + HISTORY_TOKEN_BUDGET
    assert DEFAULT_BUDGETS["persona"] == 100_000
    assert lanes - DEFAULT_BUDGETS["persona"] == 28_600, "其余车道被动过了"
    assert total == lanes + 20_000


def test_persona_budget_fits_a_full_character_sheet() -> None:
    """人格预算要能装下一份完整人设（实测一份完整人设约 6200 字符）。

    截掉一半人设比提示长更糟：模型只剩世界观，没有说话方式和禁区，
    角色会变成"设定对、说话不像"。
    """
    from ling.kernel.context import DEFAULT_BUDGETS

    chars_allowed = DEFAULT_BUDGETS["persona"] * 2
    assert chars_allowed >= 12000, f"人格只允许 {chars_allowed} 字符，装不下完整人设"


def test_history_budget_is_not_the_bottleneck() -> None:
    """最近原文层的上限必须够宽——车道预算再宽也救不了它。

    原来 HISTORY_TOKEN_BUDGET 只有 1600，约装 20~30 条就滚动摘要，
    细节全丢。那才是"接不上话"的真正原因。
    """
    from ling.kernel.context import HISTORY_TOKEN_BUDGET

    assert HISTORY_TOKEN_BUDGET >= 10000, (
        f"最近原文层只有 {HISTORY_TOKEN_BUDGET} token，聊几轮就开始丢细节"
    )


def _src_root() -> str:
    """给子进程用的 ``PYTHONPATH``：``ling`` 包所在目录的父目录（即 ``src/``）。

    从**已导入的包**反推，而不是拼 ``cwd/src``：用例可能在仓库根、
    也可能在 ``tests/`` 下被调用，写死相对路径会在其中一种下静默失败
    （子进程 import 不到 ling，报的错还和缩放无关）。
    """
    import os.path

    import ling

    existing = os.environ.get("PYTHONPATH", "")
    parts = [os.path.dirname(os.path.dirname(os.path.abspath(ling.__file__)))]
    if existing:
        parts.append(existing)
    return os.pathsep.join(parts)


def test_budget_scale_env_is_honored(monkeypatch) -> None:  # noqa: ANN001
    """`LING_CONTEXT_BUDGET_SCALE` 要能整体缩放——模型窗口小的时候用得上。

    **不要用 ``importlib.reload`` 来测这个**（曾经是这么写的，已改）：
    reload 会造出**第二个** ``ContextCompiler`` 类，而 ``turn.py:58`` 的
    ``from ... import ContextCompiler`` 在首次导入时已把旧类钉死，
    reload 修不回来。结果是 ``monkeypatch.setattr(context.ContextCompiler, ...)``
    patch 到新类、生产代码走旧类，spy 静默地永不触发——表现为
    ``test_display_name_chain.py`` 单独跑全绿、跟在本文之后跑必挂。
    见 ``docs/evidence/test-pollution-reload.md``。

    改成两层测（都不 reload）：系数取值在本进程直接调 ``_budget_scale()``
    （它调用期读 ``os.environ``）；"系数真的作用到预算表上"那一步发生在
    导入期，放到 :func:`test_budget_scale_actually_resizes_the_tables`
    用子进程验。
    """
    import ling.kernel.context as ctx

    assert ctx._budget_scale() == 1.0, "没设环境变量时应当是 1.0"

    monkeypatch.setenv("LING_CONTEXT_BUDGET_SCALE", "0.5")
    assert ctx._budget_scale() == 0.5

    # 超范围要夹紧，而不是让预算炸掉或归零
    monkeypatch.setenv("LING_CONTEXT_BUDGET_SCALE", "99")
    assert ctx._budget_scale() == 5.0
    monkeypatch.setenv("LING_CONTEXT_BUDGET_SCALE", "0")
    assert ctx._budget_scale() == 0.1


def _import_context_in_subprocess(scale: str) -> dict:
    """在**干净解释器**里按给定 ``LING_CONTEXT_BUDGET_SCALE`` 导入 context，取结果。

    为什么必须开子进程：缩放发生在 ``context.py`` 的**导入期**（``_SCALE =
    _budget_scale()`` 之后重算两张预算表）。本进程早已导入完，除了
    ``importlib.reload`` 没有别的办法再看一次"换环境变量重新导入会怎样"——
    而 reload 正是本文要根除的东西（它分裂类身份，污染别的测试文件）。
    """
    import json
    import os
    import subprocess
    import sys

    script = (
        "import json;"
        "import ling.kernel.context as c;"
        "print(json.dumps({"
        "'scale': c._SCALE,"
        "'persona': c.DEFAULT_BUDGETS['persona'],"
        "'history': c.HISTORY_TOKEN_BUDGET,"
        "'total': sum(c.DEFAULT_BUDGETS.values()) + c.HISTORY_TOKEN_BUDGET}))"
    )
    completed = subprocess.run(  # noqa: S603 - 固定脚本，无外部输入
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, "LING_CONTEXT_BUDGET_SCALE": scale, "PYTHONPATH": _src_root()},
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_budget_scale_actually_resizes_the_tables() -> None:
    """系数必须真的作用到两张预算表上，而不只是被读出来。

    断言子进程里**真实导入结果**，不复算缩放公式——公式抄到测试里就成了
    "两处各说一套"，改了一边另一边还绿。
    """
    data = _import_context_in_subprocess("0.5")

    assert data["scale"] == 0.5, data
    # 0.5 倍：人设 100k → 50k，历史 20k → 10k，车道和 128600 → 64300
    assert data["persona"] == 50_000, data
    assert data["history"] == 10_000, data
    assert data["total"] == 74_300, data


def test_budget_scale_garbage_does_not_zero_the_budgets() -> None:
    """环境变量写错不能把预算变成 0 —— 那会让所有车道都截成 80 字符。"""
    data = _import_context_in_subprocess("不是数字")

    assert data["scale"] == 1.0, f"垃圾值应当退回 1.0，实际 {data['scale']}"
    assert data["total"] > 30_000, f"垃圾值把预算压没了：{data['total']}"


# ---------------------------------------------------------------- 出入许可


def test_whitelist_allows_only_listed() -> None:
    from ling.kernel.access import check_access

    allowed = frozenset({"10001"})
    assert check_access("10001", mode="whitelist", allow_list=allowed).allowed
    assert not check_access("999999", mode="whitelist", allow_list=allowed).allowed


def test_blacklist_blocks_only_listed() -> None:
    from ling.kernel.access import check_access

    blocked = frozenset({"999999"})
    assert not check_access("999999", mode="blacklist", allow_list=blocked).allowed
    assert check_access("10001", mode="blacklist", allow_list=blocked).allowed


def test_off_mode_allows_everyone() -> None:
    from ling.kernel.access import check_access

    assert check_access("anyone", mode="off", allow_list=frozenset()).allowed


def test_qq_id_is_compared_as_string() -> None:
    """QQ 号按**字符串**比较。

    转 int 会丢掉前导零，于是"加了白名单却对不上"——这种错只在
    某些号上出现，最难查。
    """
    from ling.kernel.access import check_access

    allowed = frozenset({"0123456"})
    assert check_access("0123456", mode="whitelist", allow_list=allowed).allowed
    assert check_access("123456", mode="whitelist", allow_list=allowed).allowed is False


def test_unknown_mode_fails_closed() -> None:
    """配置写错时按最严处理。

    宁可漏接，也不要因为一个拼写错误把所有陌生人放进来。
    """
    from ling.kernel.access import check_access

    d = check_access("10001", mode="white-list", allow_list=frozenset({"10001"}))
    assert d.allowed is False
    assert "未知" in d.reason


def test_missing_sender_is_rejected_in_whitelist() -> None:
    """不知道是谁就不放行。"""
    from ling.kernel.access import check_access

    assert not check_access(None, mode="whitelist", allow_list=frozenset({"1"})).allowed
    assert not check_access("", mode="whitelist", allow_list=frozenset({"1"})).allowed


# ---------------------------------------------------------------- 上下文总预算


def _c(lane: str, n: int):
    from ling.contracts.v1 import ContextContribution, Origin, Visibility

    return ContextContribution(
        lane=lane, text="x" * n, origin=Origin.user_statement,
        visibility=Visibility.subject_private, token_budget=100,
    )


def test_total_budget_drops_lowest_priority_lanes_first() -> None:
    """超总预算时按优先级**整条丢弃**，不是把人设截一半。

    为什么整条丢而不是截断：半条记忆比没有记忆更糟——模型会基于残缺内容
    下结论。整条丢至少是"这次没想起来"。
    """
    from ling.kernel.context import _apply_total_budget, _total_soft_hard

    soft, _ = _total_soft_hard()
    contribs = [
        _c("persona", soft // 3),
        _c("constraints", 2000),
        _c("capabilities", soft // 2),
        _c("goals", soft // 2),
    ]
    meta: dict = {}
    kept = _apply_total_budget(contribs, meta)
    lanes = [c.lane for c in kept]
    assert "capabilities" not in lanes, "最不重要的车道没被先牺牲"
    assert "persona" in lanes, "人格被丢了——那是「她是谁」，不能丢"
    assert "constraints" in lanes
    assert meta["budget_state"] in {"over_soft", "over_hard"}


def test_protected_lanes_are_never_dropped() -> None:
    """persona / constraints / short_memory 永不整条丢弃。

    人格被截断比提示长更糟：模型只剩世界观，没有说话方式和禁区，
    角色会变成"设定对、说话不像"。
    """
    from ling.kernel.context import _apply_total_budget, _total_soft_hard

    # **写死期望保护的车道**，不要用 PROTECTED_LANES 生成数据——
    # 常量被清空时循环会变成空的，测试就静默通过了（第一次就是这样）。
    must_survive = ("persona", "constraints", "short_memory")
    soft, _ = _total_soft_hard()
    contribs = [_c(lane, soft) for lane in must_survive]
    contribs += [_c("knowledge", soft), _c("media", soft)]
    meta: dict = {}
    kept = _apply_total_budget(contribs, meta)
    kept_lanes = {c.lane for c in kept}
    for lane in must_survive:
        assert lane in kept_lanes, f"受保护车道 {lane} 被丢了"


def test_within_budget_is_untouched() -> None:
    """正常范围内一个字都不改——这道闸不该在平时产生任何影响。"""
    from ling.kernel.context import _apply_total_budget

    contribs = [_c("persona", 1000), _c("knowledge", 500)]
    meta: dict = {}
    kept = _apply_total_budget(contribs, meta)
    assert [c.text for c in kept] == [c.text for c in contribs]
    assert meta["budget_state"] == "within"


def test_total_budget_env_override(monkeypatch) -> None:  # noqa: ANN001
    """总预算可用环境变量覆盖，便于按模型窗口调。"""
    from ling.kernel.context import _total_soft_hard

    monkeypatch.setenv("LING_CONTEXT_SOFT_CHARS", "5000")
    monkeypatch.setenv("LING_CONTEXT_HARD_CHARS", "8000")
    soft, hard = _total_soft_hard()
    assert (soft, hard) == (5000, 8000)


# ---------------------------------------------------------------- 记忆冲突优先级


def test_evidence_render_states_conflict_priority() -> None:
    """记忆渲染必须说明**冲突时以用户本轮原话为准**。

    没有这条时，用户说"我搬去成都了"而记忆写着"住在杭州"，
    模型会拿记忆去反驳对方——把记忆当成比当事人更权威的证据。

    这条参考了 同类实现 的「核心记忆证据权限」：
    光把记忆分类还不够，还得说清冲突时的优先级。
    """
    from types import SimpleNamespace

    from ling.contracts.v1 import Origin
    from ling.domains.memory.service import render_evidence

    hit = SimpleNamespace(
        text="用户住在杭州", origin=Origin.user_statement, confidence=0.9,
        source_event_ids=["e1"], occurred_at=None, valid_until=None,
    )
    text = render_evidence([hit])
    assert "以对方现在说的为准" in text, f"没说冲突优先级：{text}"
    assert "不要拿记忆去反驳" in text


def test_evidence_render_omits_contract_when_empty() -> None:
    """没有记忆时不能凭空多出一段"以上都是你记得的事"。"""
    from ling.domains.memory.service import render_evidence

    assert render_evidence([]) == ""


# ---------------------------------------------------------------- 多用户身份


def test_identity_is_never_resolved_by_name() -> None:
    """身份解析**永远**按账号，不按名字。

    同名不同人不是理论风险：QQ 群昵称可以随便改，也可以完全重名。
    现在不混，是因为所有路径碰巧都用 subject_id 当键——不是因为有防护。
    这条测试把"目前恰好没写错"变成"以后也不许写错"。

    允许 `User.username`（登录名，是账号本身），禁止任何针对
    `display_name` / `nickname` 的**查找/过滤/合并**。
    """
    import re
    from pathlib import Path

    root = Path("src/ling/domains/identity")
    assert root.exists(), "身份域目录不存在——测试路径写错了会静默失效"

    # 查找类操作的标志：where(...) 里出现名字字段
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        src = path.read_text()
        for match in re.finditer(
            r"(where|filter_by|filter)\s*\([^)]*(display_name|nickname)", src
        ):
            line = src[: match.start()].count("\n") + 1
            offenders.append(f"{path}:{line}")
    assert not offenders, (
        "出现了按名字找人的查询——同名不同人会混：\n" + "\n".join(offenders)
    )


def test_identity_service_exposes_no_name_lookup() -> None:
    """公开接口里不能有 `find_by_name` 这类方法。

    有这个方法就一定会有人用；用了就必然在某个群昵称重名时出错。
    """
    from ling.domains.identity.service import IdentityService

    names = {n for n in dir(IdentityService) if not n.startswith("_")}
    forbidden = {n for n in names if "by_name" in n or "by_nickname" in n}
    assert not forbidden, f"出现了按名字查找的接口：{forbidden}"


def test_same_nickname_does_not_merge_subjects() -> None:
    """两个不同账号、同一个昵称 → 必须仍然是**两个** subject。

    这里只验身份键的构成：subject 由账号决定，不由昵称决定。
    """
    import inspect

    from ling.domains.identity.service import IdentityService

    src = inspect.getsource(IdentityService.resolve_account)
    assert "external_account_id" in src, "身份解析没用到账号——那用什么？"
    assert "display_name" not in src, "身份解析里出现了名字，可能被误用为键"
    assert "nickname" not in src, "身份解析里出现了昵称，可能被误用为键"


# ---------------------------------------------------------------- 动图与媒体去重


def _gif_bytes() -> bytes:
    import io

    from PIL import Image

    frames = [
        Image.new("RGB", (16, 16), color)
        for color in [(220, 40, 40), (40, 200, 60), (40, 60, 220)]
    ]
    buffer = io.BytesIO()
    frames[0].save(
        buffer, format="GIF", save_all=True, append_images=frames[1:], duration=100
    )
    return buffer.getvalue()


def test_animated_gif_is_expanded_to_frames() -> None:
    """动图必须拆成多帧再交给视觉模型。

    视觉接口对 GIF 基本只取**第一帧**，而第一帧经常是片头、空白或黑场——
    不拆帧就会出现"她看了动图说「这是一张黑色图片」"。
    """
    from ling.domains.media.describe import expand_animation

    frames = expand_animation(_gif_bytes(), "image/gif")
    assert len(frames) >= 2, f"动图没拆帧，只有 {len(frames)} 帧"
    assert all(m == "image/png" for _, m in frames), "拆出来的帧应该是 PNG"


def test_static_image_is_returned_unchanged() -> None:
    """静态图原样返回，不做无谓的重编码。"""
    import io

    from PIL import Image

    from ling.domains.media.describe import expand_animation

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buffer, format="PNG")
    data = buffer.getvalue()
    frames = expand_animation(data, "image/png")
    assert frames == [(data, "image/png")]


def test_broken_image_falls_back_to_single_frame() -> None:
    """拆帧失败不能连描述都不做——原样返回，让单帧路径继续。"""
    from ling.domains.media.describe import expand_animation

    junk = b"not really an image"
    assert expand_animation(junk, "image/gif") == [(junk, "image/gif")]


async def test_vision_actually_sends_multiple_frames() -> None:
    """`_ask_vision` 必须真的把多帧发出去——测函数本身不够。

    第一版只测 `expand_animation`，于是把调用处改成"只送第一帧"测试照样过
    （负例没响）。这里用假 router 抓实际发出的 images。
    """
    from ling.domains.media.describe import MediaDescriber

    captured: dict = {}

    class _FakeRouter:
        async def stream(self, config, *, profile, messages, **kw):  # noqa: ANN001, ANN003, ARG002
            import json as _json

            from ling.models.router import StreamOutcome
            from ling.ports.model import ModelChunk

            captured["images"] = list(messages[0].images or ())
            captured["content"] = messages[0].content
            payload = _json.dumps({"ok": 1})
            yield ModelChunk(delta=payload), StreamOutcome()
            yield ModelChunk(done=True), StreamOutcome(text=payload)

    class _Cfg:
        async def effective(self):  # noqa: ANN202
            return object()

    describer = MediaDescriber(
        service=None,  # type: ignore[arg-type]
        fetch_media=None,  # type: ignore[arg-type]
        router=_FakeRouter(),
        config_reader=_Cfg(),
    )
    # 职责不按 kind 自动切换（视频要由调用方显式指定），这里就是默认路径。
    await describer._ask_vision(_gif_bytes(), "image/gif")
    assert len(captured.get("images") or []) >= 2, (
        f"只送了 {len(captured.get('images') or [])} 帧——动图被当成单帧处理了"
    )
    assert "关键帧" in captured.get("content", ""), "多帧时没告诉模型这是同一段动图"


async def test_media_describer_never_switches_profile_by_kind() -> None:
    """职责只来自构造参数，**不按 evidence.kind 偷换**。

    用户的要求是「不是每个视频都去读，只有她自己主动想读那个视频时才读」
    （逐条自动描述白烧 token）。所以 `MediaDescriber` 默认用 `profile` 参数
    （识图），视频要由那一侧的调用方显式传 `VIDEO_PROFILE`。

    这条防的是"顺手把 kind=video 自动路由到 perception.video"：那样队列里
    每个视频都会被读一遍，而且用户以为"她还没想看"的时候 token 已经烧掉了。
    """
    from types import SimpleNamespace

    from ling.domains.media.describe import MediaDescriber
    from ling.domains.media.service import EvidenceView
    from ling.models.router import StreamOutcome
    from ling.ports.model import ModelChunk

    seen: list[str] = []

    class _Router:
        async def stream(self, config, *, profile, messages, **kw):  # noqa: ANN001, ANN003, ARG002
            seen.append(profile)
            yield ModelChunk(delta="一段视频"), StreamOutcome()
            yield ModelChunk(done=True), StreamOutcome(text="一段视频")

    class _Svc:
        async def describe(self, evidence_id, *, window_id, description, description_origin):  # noqa: ANN001, ARG002
            return SimpleNamespace(description=description)

    class _Cfg:
        async def effective(self):  # noqa: ANN202
            return object()

    async def _fetch(_ref: str) -> tuple[bytes, str]:
        return b"fake-mp4", "video/mp4"

    evidence = EvidenceView(
        evidence_id="media_1",
        window_id="win_1",
        kind="video",
        media_ref="obj/clip.mp4",
        origin="user_upload",
        visibility="window_private",
        description=None,
        description_origin=None,
        expires_at=None,
    )
    describer = MediaDescriber(
        service=_Svc(),  # type: ignore[arg-type]
        fetch_media=_fetch,
        router=_Router(),
        config_reader=_Cfg(),
    )
    await describer.describe_one(evidence)

    assert seen == ["perception.vision"], (
        f"职责被按 kind 自动切换了：{seen}。视频必须由调用方显式指定职责。"
    )


# ---------------------------------------------------------------- 表达方式由她选


def test_expression_markers_are_parsed_and_stripped() -> None:
    """她选的表达方式要被解析出来，并**从正文里剥掉**。

    不剥的话，发出去的话里会残留"[at:123]"这种说明书。
    """
    from ling.capabilities.expression_choice import parse_expression

    text, d = parse_expression("[at:770140] 我在呢，你说")
    assert text == "我在呢，你说", text
    assert d.at == ["770140"]

    text, d = parse_expression("[quote:123456] 这条我回一下")
    assert text == "这条我回一下"
    assert d.quote == "123456"

    text, d = parse_expression("[poke:770140]")
    # 只有标记 → 保留原文，否则会发出一条空消息
    assert text == "[poke:770140]"
    assert d.empty is True


def test_expression_without_markers_is_untouched() -> None:
    """没用标记时一个字都不改——绝大多数回复都是这种情况。"""
    from ling.capabilities.expression_choice import parse_expression

    plain = "今天好累啊\n想早点睡"
    text, d = parse_expression(plain)
    assert text == plain
    assert d.empty


def test_expression_targets_are_capped() -> None:
    """@ 目标有上限，防止她一次点名一屏人。"""
    from ling.capabilities.expression_choice import MAX_TARGETS, parse_expression

    raw = "".join(f"[at:{i}] " for i in range(10)) + "大家好"
    _, d = parse_expression(raw)
    assert len(d.at) == MAX_TARGETS


def test_expression_help_lists_only_what_channel_supports() -> None:
    """只列真的能做到的。

    列了做不到的，她会选一个做不出来的动作，然后"选择了却没发生"——
    比不给选项更糟。
    """
    from ling.capabilities.expression_choice import render_expression_help

    group = render_expression_help(is_group=True, supports_poke=True)
    assert "[at:" in group and "[quote:" in group and "[poke:" in group

    private_no_poke = render_expression_help(is_group=False, supports_poke=False)
    assert "[at:" not in private_no_poke, "私聊不该提供 @"
    assert "[poke:" not in private_no_poke, "不支持拍一拍却列了出来"


# ---------------------------------------------------------------- 主动推送为什么不可用


def test_proactive_disabled_reason_names_the_switch(monkeypatch) -> None:  # noqa: ANN001
    """不能主动推送时，理由要**指名道姓**。

    原来只有一句「需账号权限探测通过」——用户会去找一个根本不存在的平台权限，
    而真实原因八成是 `LING_ONEBOT_PROACTIVE=0` 没打开。
    """
    from types import SimpleNamespace

    import ling.capabilities.channels as channels

    fake = SimpleNamespace(
        capabilities=lambda: SimpleNamespace(enabled=True, supports_proactive=False),
        connector_id="onebot",
    )
    monkeypatch.setattr(channels, "build_onebot_connector", lambda: fake)
    conn, why = channels.proactive_connector_status()
    assert conn is None
    assert why and "LING_ONEBOT_PROACTIVE" in why, why


def test_proactive_disabled_reason_distinguishes_not_configured(monkeypatch) -> None:  # noqa: ANN001
    """「没配连接器」和「没声明能力」是两件事，要去改的地方不同。"""
    from types import SimpleNamespace

    import ling.capabilities.channels as channels

    fake = SimpleNamespace(
        capabilities=lambda: SimpleNamespace(enabled=False, supports_proactive=False),
        connector_id="onebot",
    )
    monkeypatch.setattr(channels, "build_onebot_connector", lambda: fake)
    _, why = channels.proactive_connector_status()
    assert why and "LING_QQ_ENABLED" in why, why


async def test_gate_uses_the_precise_channel_reason() -> None:
    """门控要把调用方给的精确原因原样呈现，而不是换成笼统一句。"""
    from ling.domains.proactive.service import ProactivityService

    class _Result:
        # 门控会数"今天已经发了几条"：scalar_one 与 scalar 都会用到
        def scalar(self) -> int:
            return 0

        def scalar_one(self) -> int:
            return 0

    class _S:
        async def execute(self, *_a, **_k):  # noqa: ANN002, ANN003
            return _Result()

    svc = ProactivityService(_S(), actor_id="actor_ling")  # type: ignore[arg-type]
    decision = await svc.gate(
        subject_id="subj_x",
        subject_timezone="Asia/Shanghai",
        channel_can_proactive=False,
        channel_proactive_reason="LING_ONEBOT_PROACTIVE=0 的说明",
    )
    assert any("LING_ONEBOT_PROACTIVE" in r for r in decision.reasons), decision.reasons


# ---------------------------------------------------------------- 草稿截断


def test_draft_trims_at_sentence_boundary() -> None:
    """超长草稿要在**句子边界**截断，不能切在半句话上。

    原来直接 `text[:200]`，会把"我路过你家楼下那家店，想起你说想吃"截成
    "…想起你说想"，像话没说完被掐掉。宁可少说一句，也不要说半句。
    """
    from ling.domains.proactive.draft import trim_to_sentence

    # 30 字会**切在第二句中间**：硬截断得到"…进去吃了碗面，味"这种半句，
    # 这正是要防的。长度必须选在句子中间，否则测试是空跑的（踩过一次）。
    long = "今天路过那家店。进去吃了碗面，味道还行。" * 20
    out = trim_to_sentence(long, 30)
    assert len(out) <= 30
    assert out.endswith("。"), f"没落在句末：{out!r}"
    # 对照：硬截断在同样长度上确实会切在半句，证明这个测试不是空跑
    assert not long[:30].endswith("。"), "测试选的长度恰好落在句末，失去意义"


def test_draft_trim_keeps_short_text_untouched() -> None:
    """没超长就一个字都不改。"""
    from ling.domains.proactive.draft import trim_to_sentence

    assert trim_to_sentence("在吗？", 200) == "在吗？"


def test_draft_trim_falls_back_when_no_punctuation() -> None:
    """整段没有标点时退回硬截断——总比不截好。"""
    from ling.domains.proactive.draft import trim_to_sentence

    out = trim_to_sentence("啊" * 500, 20)
    assert len(out) == 20


async def test_draft_for_actually_uses_the_sentence_trim() -> None:
    """`draft_for` 必须真的走句界截断——测 helper 不等于测接线。

    前一版只测 `trim_to_sentence`，把 `draft_for` 改回硬截断照样绿
    （负例没响就是证据）。这里换成假 router，断言最终落库的文本。
    """
    from types import SimpleNamespace

    from ling.domains.proactive.draft import draft_for

    # 句子长度必须**不整除** MAX_DRAFT_CHARS，否则硬截断也刚好落在句末，
    # 测试就空跑了（这个坑我踩了两次：20 字 × 200 正好整除）。
    long_text = "路过那家店。" * 100  # 6 字一句，200 会切在句子中间
    assert not long_text[:200].endswith("。"), "长度选得不对，测试会空跑"

    class _Router:
        async def stream(self, *_a, **_k):  # noqa: ANN002, ANN003
            from ling.models.router import StreamOutcome
            from ling.ports.model import ModelChunk

            yield ModelChunk(delta=long_text), StreamOutcome()
            yield ModelChunk(done=True), StreamOutcome(text=long_text)

    class _Cfg:
        async def effective(self):  # noqa: ANN202
            return object()

    opportunity = SimpleNamespace(reason="想说点日常", expires_at=None)
    out = await draft_for(
        None,  # type: ignore[arg-type]
        opportunity=opportunity,
        router=_Router(),
        config_reader=_Cfg(),
    )
    assert out is not None
    assert len(out) <= 200
    assert out.endswith("。"), f"没走句界截断：{out[-8:]!r}"


# ---------------------------------------------------------------- 部署变量传递


def test_deploy_compose_passes_feature_env_into_containers() -> None:
    """`x-app-env` 是**逐项白名单**：没列的变量即使在 .env 里配好也进不了容器。

    这个坑已经踩过两次——OneBot 的变量当初没列，"配了却连不上"且日志里
    一句异常都没有；QQ 官方机器人也一样。所以把必须传进去的变量钉住。
    """
    from pathlib import Path

    import yaml

    compose = Path("deploy/compose.yaml")
    assert compose.exists(), "部署文件不见了"
    data = yaml.safe_load(compose.read_text())
    passed = set(data["x-app-env"])

    # 这几组是"配了必须生效"的：渠道连接、出入许可、时区、语义检索
    required = {
        "LING_QQBOT_ENABLED", "LING_QQBOT_APP_ID", "LING_QQBOT_CLIENT_SECRET",
        "LING_QQBOT_INSTANCE_ID", "LING_QQBOT_SANDBOX", "LING_QQBOT_PROACTIVE",
        "LING_ACCESS_MODE", "LING_ACCESS_LIST", "LING_USER_TIMEZONE",
        "LING_ONEBOT_API_BASE", "LING_ONEBOT_SELF_ID", "LING_ONEBOT_INSTANCE_ID",
        "LING_ONEBOT_SECRET",
        # 反向 WS 服务端三件套：原来**一项都没列**，于是设了也进不了容器，
        # 表现为"配了 WS 端口却永远没监听"，日志里一句异常都没有。
        "LING_ONEBOT_WS_PORT", "LING_ONEBOT_WS_TOKEN", "LING_ONEBOT_WS_HOST",
        # 反向 WS **客户端**（ling 连出去）：原来也完全没列，设了进不了容器，
        # 表现为"配了 WS 地址，容器里一次都没连过"。
        "LING_ONEBOT_WS_URL",
        # 媒体域名白名单：不传进容器就只能用内置默认值，改不动。
        "LING_ONEBOT_MEDIA_HOSTS",
        "LING_EMBEDDING_API_BASE",
    }
    missing = sorted(required - passed)
    assert not missing, f"这些变量没传进容器，配了也不会生效：{missing}"


def test_deploy_compose_does_not_publish_ws_port_by_default() -> None:
    """**不配 WS 的部署不该多出一个对外端口。**

    反向 WS 服务端要监听一个端口，但基础 compose 里 api 的 `ports` 只有
    `${LING_PORT:-6520}:6520`：WS 端口靠用户追加 override
    （`deploy/compose.ws.example.yaml`）来发布。

    为什么不是条件发布：Compose 无法按变量条件发布端口——变量留空时
    那条映射会变成只有冒号的非法条目，`docker compose up` 直接报错；
    写成固定端口则等于**所有部署**都发布它。`profiles` 只能条件启用服务，
    不能条件启用某个服务的 `ports`。

    这条测试防的是两件相反的事：
    1. 有人"顺手"在基础 compose 里写死 WS 端口 → 不配 WS 的部署也开端口；
    2. 有人把那份 override 示例删了 → 用户没有可执行的步骤。
    """
    from pathlib import Path

    import yaml

    compose = Path("deploy/compose.yaml")
    data = yaml.safe_load(compose.read_text())
    api_ports = [str(item) for item in (data["services"]["api"].get("ports") or [])]
    assert api_ports == ["${LING_PORT:-6520}:6520"], (
        f"基础 compose 的 api ports 不该多出条目：{api_ports}"
    )

    example = Path("deploy/compose.ws.example.yaml")
    assert example.exists(), "WS 端口发布的示例 override 不见了（用户没法自己加）"
    override = yaml.safe_load(example.read_text())
    ws_ports = [str(item) for item in (override["services"]["api"].get("ports") or [])]
    assert len(ws_ports) == 1, f"示例 override 应只加一条端口映射：{ws_ports}"
    assert ws_ports[0].startswith("${LING_ONEBOT_WS_PORT:?"), (
        f"WS 端口必须来自 LING_ONEBOT_WS_PORT（且漏配时解析阶段就报错）：{ws_ports}"
    )


def test_deploy_env_example_documents_them_too() -> None:
    """env.example 里也要有——不然部署的人根本不知道要配什么。"""
    from pathlib import Path

    text = Path("deploy/env.example").read_text()
    for key in ("LING_QQBOT_APP_ID", "LING_QQBOT_CLIENT_SECRET", "LING_ACCESS_LIST",
                "LING_USER_TIMEZONE", "LING_ONEBOT_SECRET",
                "LING_ONEBOT_WS_PORT", "LING_ONEBOT_WS_TOKEN", "LING_ONEBOT_WS_HOST",
                "LING_ONEBOT_WS_URL"):
        assert key in text, f"env.example 缺 {key}"


def test_deploy_env_example_names_the_napcat_field_for_secret() -> None:
    """`LING_ONEBOT_SECRET` 的注释必须点名 **NapCat 的哪个字段**。

    `SECRET` 与 `ACCESS_TOKEN` 是两回事，但 NapCat 的 `httpClients` 里
    **没有独立 secret 字段**——`token` 一个字段同时当 API 口令和 HMAC 密钥
    （见 napcat-onebot/network/http-client.ts 用 `this.config.token` 当 key）。
    注释只写"与 OneBot 一致"是不够的：用户把 SECRET 填成与 ACCESS_TOKEN
    不同的值，NapCat 侧只填了一个 token，验签必然失败 ⇒ 仍然全 403，
    而日志只看到"签名不匹配"。

    所以这里钉住三件事：必填、对应 `httpClients[].token`、与 ACCESS_TOKEN 的区别。
    """
    from pathlib import Path

    text = Path("deploy/env.example").read_text()
    # 找到 LING_ONEBOT_SECRET= 那一行往上的注释块
    lines = text.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("LING_ONEBOT_SECRET="))
    comment = "\n".join(lines[max(0, index - 25) : index])
    assert "必填" in comment, f"env.example 没写 SECRET 是必填：{comment}"
    assert "httpClients[].token" in comment, (
        f"env.example 没写 SECRET 对应 NapCat 的 httpClients[].token：{comment}"
    )
    assert "ACCESS_TOKEN" in comment, (
        f"env.example 没写清 SECRET 与 ACCESS_TOKEN 的区别：{comment}"
    )


# ---------------------------------------------------------------- 日志入口


def test_both_entrypoints_configure_logging() -> None:
    """API 有两条启动路径，**两条都要配日志**。

    开发用 `python -m ling`（走 __main__.py），容器用
    `uvicorn ling.app:app`（走 entrypoint.sh，不经过 __main__）。
    `uvicorn.run(log_level=...)` 只调 uvicorn 自己的 logger，不碰 root logger——
    所以只在一处配的话，另一条路的 ling 日志会**一条都不出现**。

    实测踩过：容器部署后 qqbot 网关启没启动、为什么没启动，日志里一个字没有。
    """
    import inspect
    from pathlib import Path

    from ling import app as app_module
    from ling import logging_setup

    # app.py 在**模块级**就调了（uvicorn import 它的那一刻生效）
    src = inspect.getsource(app_module)
    top_level = [
        line for line in src.splitlines() if line.startswith("setup_logging(")
    ]
    assert top_level, "app.py 没有在模块级调用 setup_logging()"

    # 另外两个入口也要调：开发路径与 worker 进程
    for entry in ("src/ling/__main__.py", "src/ling/worker.py"):
        entry_src = Path(entry).read_text()
        assert "setup_logging(" in entry_src, f"{entry} 没配日志"

    # worker 曾经自己调 basicConfig，格式与 api 不一致——这条防它回来
    worker_src = Path("src/ling/worker.py").read_text()
    assert "basicConfig" not in worker_src, "worker 又自己配了一套日志"

    # 配置函数本身要幂等且尊重 LING_LOG_LEVEL
    assert callable(logging_setup.setup_logging)


def test_dockerfile_ships_connectors_package() -> None:
    """镜像必须带上 `connectors`。

    `pyproject` 声明的 wheel 包含 `connectors`（渠道连接器都在这儿）。Dockerfile
    当初只 `COPY src`，于是镜像里没有这个包——`import connectors.qqbot` 直接
    ModuleNotFoundError，被上层 `except Exception` 吞成"缺少凭据"。

    后果是**所有渠道在容器里都连不上**，而 dev 里一切正常（cwd 下就有
    connectors/）。这是最典型的假完整感：功能写了、测试绿了、容器里是死的。
    """
    from pathlib import Path

    text = Path("deploy/Dockerfile.api").read_text()
    copies = [
        line for line in text.splitlines()
        if "COPY" in line and "connectors" in line
    ]
    assert len(copies) >= 2, (
        f"builder 与 runtime 两个阶段都要 COPY connectors，实际只找到 {copies}"
    )
    # pyproject 里也要声明，否则打了 wheel 也没它
    pyproject = Path("pyproject.toml").read_text()
    assert "connectors" in pyproject, "pyproject 没把 connectors 打进 wheel"


def test_worker_container_has_network_egress() -> None:
    """worker 必须能出网。

    它要调用模型（记忆提炼、关系更新、主动消息生成）。`internal` 网络是
    `internal: true`，**没有外网**；worker 只挂它时，所有模型调用都失败，
    而且不报错——只会打出"模型提炼不可用，降级为正则"，能力悄悄变差。
    实测在部署实例上就是这样：api 正常、worker 静默降级好几天没人发现。

    这类"配置看着齐全、跑起来也不报错、但功能全废"的故障，必须有测试钉住。
    """
    from pathlib import Path

    import yaml

    data = yaml.safe_load(Path("deploy/compose.yaml").read_text())
    networks = data["services"]["worker"].get("networks") or []
    # 出网口叫 `egress`（原来叫 `edge`，随 nginx 容器一起改名）：它是**普通
    # bridge**，才有外网。改名时最容易犯的错是把 api/worker 只留在 `internal`
    # 上——那会让它们彻底失去外网，而且不报错。
    assert "egress" in networks, f"worker 没有出网能力：networks={networks}"
    # internal 仍然要保留：数据库只在内网
    assert "internal" in networks, f"worker 丢了内网访问：networks={networks}"
    # internal 必须是 internal: true（否则 db 会被暴露到宿主网络）
    assert data["networks"]["internal"].get("internal") is True
    # egress **绝对不能**是 internal: true，否则等于没有出网口。
    # （compose 里 `egress:` 后面不写值 → YAML 解析成 None，也算合法默认）
    egress_def = data["networks"].get("egress") or {}
    assert egress_def.get("internal") is not True, (
        "egress 被设成 internal: true —— api/worker 会失去全部外网访问"
        "（QQ、embedding、LLM 全废），而且不会报错"
    )


def test_deploy_build_can_survive_slow_pypi() -> None:
    """镜像构建必须能扛住 PyPI 慢/超时。

    实测在 NAS 上整次部署因 `ReadTimeoutError` 失败，报错是
    "No matching distribution found for build"——看起来像缺包，
    实际是网络。所以三件事都要在：

    1. Dockerfile 有 `--retries/--timeout`
    2. Dockerfile 支持 `PIP_INDEX_URL` 换镜像源
    3. compose **两个服务**都把 `PIP_INDEX_URL` 透传进构建
       （漏一个，那个镜像仍会失败）
    """
    from pathlib import Path

    import yaml

    dockerfile = Path("deploy/Dockerfile.api").read_text()
    assert "--retries" in dockerfile, "构建没有重试，慢网络必挂"
    assert "PIP_INDEX_URL" in dockerfile, "构建不支持换镜像源"

    data = yaml.safe_load(Path("deploy/compose.yaml").read_text())
    for svc in ("api", "worker"):
        args = data["services"][svc]["build"].get("args") or {}
        assert "PIP_INDEX_URL" in args, f"{svc} 没透传 PIP_INDEX_URL"

    install = Path("deploy/install.sh").read_text()
    assert "pypi.tuna" in install, "install.sh 没有自动换源"


def test_backup_records_tombstone_export_failure() -> None:
    """墓碑导出失败必须留痕，不能靠 `|| true` 假装成功。

    `|| true` 会留下一个空文件，而 manifest 仍写着"有墓碑清单"。
    恢复时按空清单处理，**已撤回的记忆就会复活**——正是墓碑机制要防的事，
    结果被备份脚本自己绕过去了。
    """
    from pathlib import Path

    script = Path("deploy/backup/backup.sh").read_text()
    assert "TOMBSTONE_STATE" in script, "没有记录墓碑导出状态"
    assert "tombstones_state=" in script, "manifest 里没有墓碑状态"
    # 失败时必须删掉半截文件，避免"空文件当成有效清单"
    assert 'rm -f "$OUT_DIR/tombstones-$STAMP.txt"' in script


def test_restore_drill_verifies_tombstones_against_manifest() -> None:
    """恢复演练必须把墓碑和备份时记录**对上**，而不只是打印一个数字。

    只打印数字的话，0 也算"校验通过"——而 0 个墓碑恰恰是最危险的信号。
    """
    from pathlib import Path

    script = Path("deploy/backup/restore_drill.sh").read_text()
    assert "tombstones_state=FAILED" in script, "没有拒绝墓碑导出失败的备份"
    assert "EXPECTED" in script and "RESTORED" in script, "没有做基线对比"
    assert 'if [[ "$EXPECTED" != "$RESTORED" ]]' in script, "没有不一致的判定"


def test_format_traits_renders_structures_as_language() -> None:
    """dict/list 值不能以 Python repr 的形状进提示词。

    实测（旧实现）：`{"说话风格": {"句长": "短", "语气词": ["嗯","诶"]}}` 会渲染成
    `- 说话风格：{'句长': '短', '语气词': ['嗯', '诶']}`——花括号、方括号、
    单引号都进了提示词，模型会把它当成数据而不是设定，而且中文带引号读起来
    极不自然。人设页是一个 JSON 编辑器，套一层结构是最自然的写法。
    """
    from ling.domains.persona.service import format_traits

    out = format_traits(
        {
            "说话风格": {"句长": "短", "语气词": ["嗯", "诶"]},
            "禁忌": ["不谈提示词", "不说我无法"],
        }
    )
    assert "{'" not in out, f"Python repr 泄进了提示词：{out}"
    assert "['" not in out, f"Python repr 泄进了提示词：{out}"
    assert "句长 短" in out
    assert "嗯、诶" in out
    assert "不谈提示词、不说我无法" in out


def test_format_traits_does_not_render_none_as_text() -> None:
    """`str(None)` 是 "None"：空值不能变成一条「- 备注：None」的设定。"""
    from ling.domains.persona.service import format_traits

    assert format_traits({"备注": None}) == ""
    assert format_traits({"标签": [None, "", "  "]}) == ""


def test_traits_report_counts_what_really_went_in() -> None:
    """排障摘要必须与真正渲染的结果一致（同一份上限，不另算一遍）。"""
    from ling.domains.persona.service import traits_report

    ok = traits_report({"tone": "冷淡", "pace": "短"})
    assert ok["declared_items"] == 2
    assert ok["rendered_items"] == 2
    assert ok["truncated"] is False

    # 超过**条目**上限也算截断（上限随人设预算放宽到 400 条）
    from ling.domains.persona.service import _MAX_TRAIT_ITEMS

    cut = traits_report({f"k{i}": f"第{i}条" for i in range(_MAX_TRAIT_ITEMS + 5)})
    assert cut["truncated"] is True
    assert int(cut["rendered_chars"]) > 0


# ------------------------------------------------- 撤回：标记解析与工具注册（纯函数）


def test_recall_marker_parse_and_strip() -> None:
    """`<<撤回 rXXXX>>` 解析、剥离、幂等；无参形式 = 撤最近一条。"""
    from ling.domains.conversation.recall import (
        finalize_recall_text,
        parse_recall_markers,
        strip_recall_markers,
    )

    cleaned, refs = parse_recall_markers("说错了<<撤回 r3f2a1>>，当没听见")
    assert refs == ["r3f2a1"]
    assert "<<撤回" not in cleaned
    assert "说错了" in cleaned and "当没听见" in cleaned

    # 幂等
    assert strip_recall_markers(cleaned) == cleaned
    assert parse_recall_markers(cleaned)[1] == []

    # 无参
    assert parse_recall_markers("<<撤回>>好")[1] == [""]

    # 宽容排版：多余空格也认
    assert parse_recall_markers("<<  撤回   r3f2a1  >>")[1] == ["r3f2a1"]

    # 半截标记也要清掉（模型被打断的残片不能落库/外发）
    assert "<<" not in finalize_recall_text("说错了<<撤回 r3")
    assert finalize_recall_text("正常的句子") == "正常的句子"


def test_recall_marker_does_not_collide_with_watch_marker() -> None:
    """撤回标记与看视频标记**互不吞**（两套正则各认各的）。"""
    from ling.domains.conversation.recall import parse_recall_markers
    from ling.domains.media.video import parse_watch_markers

    both = "<<看视频 m3f2a1>>我看了<<撤回 r9b2c3>>"
    watch_cleaned, watch_refs = parse_watch_markers(both)
    assert watch_refs == ["m3f2a1"]
    # 撤回标记仍在（看视频那条不该顺手把它吃掉）
    assert "<<撤回 r9b2c3>>" in watch_cleaned

    recall_cleaned, recall_refs = parse_recall_markers(both)
    assert recall_refs == ["r9b2c3"]
    assert "<<看视频 m3f2a1>>" in recall_cleaned


def test_recall_ref_is_derived_from_message_id() -> None:
    """短号由 id 唯一决定（同一句话在相邻两轮里拿到同一个短号）。"""
    from ling.domains.conversation.recall import recall_ref_for

    assert recall_ref_for("m3f2a1") == "r3f2a1"
    assert recall_ref_for("abc123") == "rabc123"
    assert recall_ref_for("") == ""
    # 同一个输入永远同一个输出
    assert recall_ref_for("mdeadbe") == recall_ref_for("mdeadbe")


@pytest.mark.asyncio
async def test_recall_tool_is_registered_irreversible_and_needs_confirmation() -> None:
    """`message.recall` 在默认注册表里，且不可逆 + 必须确认。"""
    from ling.capabilities.tools import (
        RECALL_TOOL_ID,
        ToolOutcome,
        ToolRisk,
        build_default_registry,
    )

    registry = build_default_registry()
    spec = next(s for s in registry.list_specs() if s.tool_id == RECALL_TOOL_ID)
    assert spec.risk is ToolRisk.irreversible
    assert spec.requires_confirmation is True
    assert spec.reversible is False
    assert spec.idempotent is True
    assert "message_id" in spec.input_schema.get("required", [])

    # 不确认 → needs_confirmation，**handler 根本不进**
    result = await registry.call(
        RECALL_TOOL_ID, {"message_id": "msg_x", "window_id": "win_x"}
    )
    assert result.outcome is ToolOutcome.needs_confirmation
    assert result.output == ""
    assert result.call_id, "没有 call_id，审计对不上"


def test_per_registration_authorization_does_not_open_the_gate() -> None:
    """逐次授权只对那一次生效：默认注册表仍然拒绝别的不可逆工具。"""
    from ling.capabilities.tools import ToolRegistry, ToolRisk, ToolSpec

    async def handler(args):  # noqa: ANN001
        return "x"

    spec = ToolSpec(
        tool_id="other.danger",
        version="1",
        description="别的不可逆工具",
        risk=ToolRisk.irreversible,
        requires_confirmation=True,
        reversible=False,
    )
    # 不传 allow_irreversible → 拒绝
    with pytest.raises(PermissionError):
        ToolRegistry().register(spec, handler)
    # 显式传 True → 允许（这就是撤回那一行的写法）
    registry = ToolRegistry()
    registry.register(spec, handler, allow_irreversible=True)
    assert [s.tool_id for s in registry.list_specs()] == ["other.danger"]


def test_irreversible_tool_without_confirmation_is_rejected() -> None:
    """不可逆 + 不要确认 = 直接拒。确认这一条是硬要求，不能被授权开关绕过。"""
    from ling.capabilities.tools import ToolRegistry, ToolRisk, ToolSpec

    async def handler(args):  # noqa: ANN001
        return "x"

    with pytest.raises(ValueError, match="必须要求确认"):
        ToolRegistry().register(
            ToolSpec(
                tool_id="sneaky",
                version="1",
                description="不可逆但不要确认",
                risk=ToolRisk.irreversible,
                requires_confirmation=False,
                reversible=False,
            ),
            handler,
            allow_irreversible=True,
        )
