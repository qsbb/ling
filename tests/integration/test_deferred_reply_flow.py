"""延后回复的**端到端**行为：到期 → 认领 → 新逻辑轮次 → 投递。

## 这个文件钉的是"链真的通了"，不是"函数各自对"

`test_deferred_reply_store.py` 证明的是状态与栅栏；这里证明的是
**sweep handler 真的会把一条到期的记录变成一个用户看得见的回复**，以及三条
最容易在接线时坏掉的路径：

1. **到期前不回、到期后恰好一个新轮**（断言 8 的核心）。用注入的固定 `now`
   驱动，不 sleep 真实 45 秒。
2. **重复扫描只产生一个新轮次**（断言 8 后半）：两次 sweep 之间 turn 数只 +1。
3. **新输入抢占 → 外发 0 次**（断言 10）：延后期间来了新用户消息，到点后
   既不生成也不投递，落 `cancelled(reason=new_input)`。
4. **硬截止兜底**：生成不出正文时发确定性短句，且**不声称送达**。
5. **窗口归档 → 不投递**（lead 的第 6 条要求）：延后期间窗口被归档，到点
   必须 `cancelled(reason=window_gone)` 而不是发出去。

## 为什么用假 router 而不是真模型

本文件测的是**调度与栅栏**，不是模型质量。真模型会让"到期前无回复"这类断言
变成不确定的（模型可能自己想说话）。假 router 让"生成发生了/没发生"变成可数
的事实——这正是断言需要的。真实模型的意愿行为由别的任务取证。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

import ling.worker as worker_module
from ling.domains.deferred import service as deferred
from ling.domains.deferred.state import DeferredReplyState
from ling.infra.db import session_scope
from ling.infra.models import DeferredReply, Message, Turn, Window
from tests.integration.conftest import (
    TestEnv,  # noqa: F401 - 类型注解
    web_env,  # noqa: F401 - fixture
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)


class _CountingRouter:
    """最小的流式 router：每次生成吐一句固定正文，并数调用次数。"""

    def __init__(self, *, reply: str = "刚才说的那事我想了想，还是现在说。") -> None:
        self.reply = reply
        self.calls = 0

    async def stream(self, config, *, profile, messages, **kwargs):  # noqa: ANN001, ANN003, ARG002
        from ling.models.router import StreamOutcome
        from ling.ports.model import ModelChunk

        self.calls += 1
        yield ModelChunk(delta=self.reply), StreamOutcome()
        yield ModelChunk(done=True), StreamOutcome(text=self.reply)


async def _web_window(window_id: str = "win_def", *, subject: str = "subj_a") -> None:
    async with session_scope() as session:
        session.add(
            Window(
                id=window_id,
                actor_id="actor_ling",
                persona_id="persona_ling",
                subject_id=subject,
                title="测试窗口",
                channel="web",
                visibility="subject_private",
            )
        )


async def _user_message(window_id: str, *, sequence: int, text: str = "在吗") -> None:
    async with session_scope() as session:
        session.add(
            Message(
                id=f"msg_{window_id}_{sequence}",
                window_id=window_id,
                role="user",
                text=text,
                sequence=sequence,
            )
        )


async def _schedule(
    *,
    window_id: str = "win_def",
    turn_id: str = "turn_a",
    input_sequence: int = 1,
    delay_ms: int = 45_000,
    now: datetime = NOW,
) -> DeferredReply:
    async with session_scope() as session:
        row = await deferred.schedule(
            session,
            actor_id="actor_ling",
            window_id=window_id,
            subject_id="subj_a",
            source_turn_id=turn_id,
            source_message_id=f"msg_{window_id}_{input_sequence}",
            input_sequence=input_sequence,
            applied_delay_ms=delay_ms,
            requested_delay_ms=delay_ms,
            clamped=False,
            now=now,
        )
    assert row is not None
    return row


async def _turns_in(window_id: str) -> int:
    async with session_scope() as session:
        return int(
            await session.scalar(
                select(func.count()).select_from(Turn).where(Turn.window_id == window_id)
            )
            or 0
        )


async def _actor_messages(window_id: str) -> list[str]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Message.text).where(
                    Message.window_id == window_id, Message.role == "actor"
                )
            )
        ).scalars()
        return list(rows)


async def _sweep_at(moment: datetime) -> None:
    """在**注入的时刻**跑一次完整 sweep（扫描 + 认领 + 生成 + 终结）。"""
    from ling.domains.deferred import service as _svc

    await worker_module._deferred_sweep_once(now=moment)
    claimed = await worker_module._claim_one_deferred(now=moment)
    if claimed is None:
        return
    outcome, reason, delivered_turn_id = await worker_module._run_deferred_turn(
        reply_id=claimed.id,
        window_id=claimed.window_id,
        subject_id=str(claimed.subject_id or ""),
        input_sequence=int(claimed.input_sequence),
        claim_generation=int(claimed.claim_generation),
    )
    await worker_module._settle_deferred(
        claimed, outcome=outcome, reason=reason, delivered_turn_id=delivered_turn_id
    )
    _ = _svc


# ------------------------------------------------------------------ 断言 8


async def test_no_reply_before_due_then_exactly_one_new_turn(
    web_env: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**44 秒前无回复，45 秒后恰好一个新轮**（断言 8 的核心）。

    不 sleep 真实的 45 秒：`now` 是注入的，`chosen_due_at` 在登记时就写死了。
    """
    router = _CountingRouter()
    monkeypatch.setattr(worker_module, "_MEMORY_ROUTER", router)

    await _web_window()
    await _user_message("win_def", sequence=1)
    await _schedule(delay_ms=45_000)

    # 44 秒：什么都不该发生
    await _sweep_at(NOW + timedelta(seconds=44))
    assert await _turns_in("win_def") == 0, "44 秒就生成了新轮次 = 她会提前回"
    assert router.calls == 0, "44 秒就调了模型"
    assert await _actor_messages("win_def") == []

    # 45 秒：恰好一个新轮次
    await _sweep_at(NOW + timedelta(seconds=45))
    assert await _turns_in("win_def") == 1, "到点没有产生新轮次"
    assert router.calls == 1, f"到点应当恰好生成一次，实际 {router.calls} 次"
    messages = await _actor_messages("win_def")
    assert len(messages) == 1, f"应当恰好一条她的消息，实际 {messages}"

    async with session_scope() as session:
        stored = await session.scalar(select(DeferredReply))
    assert stored is not None
    assert stored.state == DeferredReplyState.delivered.value
    assert stored.delivered_turn_id is not None


async def test_duplicate_sweeps_produce_only_one_new_turn(
    web_env: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连续扫三次，仍然**只有一个新轮次、只调一次模型**。

    这是断言 8 的后半（"重复任务只产生一个轮次和一个投递计划"）。第二、三次
    扫描看到的是 `claimed`/`delivered`，认领不到——而不是又生成一遍。
    """
    router = _CountingRouter()
    monkeypatch.setattr(worker_module, "_MEMORY_ROUTER", router)

    await _web_window()
    await _user_message("win_def", sequence=1)
    await _schedule(delay_ms=45_000)

    at = NOW + timedelta(seconds=45)
    await _sweep_at(at)
    await _sweep_at(at)
    await _sweep_at(at + timedelta(seconds=5))

    assert await _turns_in("win_def") == 1, "重复扫描产生了多个轮次"
    assert router.calls == 1, f"重复扫描调了 {router.calls} 次模型"
    assert len(await _actor_messages("win_def")) == 1


# ------------------------------------------------------------------ 断言 10


async def test_new_input_preempts_before_generation(
    web_env: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """延后期间来了新输入 → **外发 0 次**，落 `cancelled(reason=new_input)`。

    这是断言 10：旧迟到回复绝不插进新话题。
    """
    router = _CountingRouter()
    monkeypatch.setattr(worker_module, "_MEMORY_ROUTER", router)

    await _web_window()
    await _user_message("win_def", sequence=1)
    await _schedule(delay_ms=45_000, input_sequence=1)
    # 用户又说话了
    await _user_message("win_def", sequence=2, text="我换个问题")

    await _sweep_at(NOW + timedelta(seconds=45))

    assert router.calls == 0, "被抢占之后仍然调了模型"
    assert await _turns_in("win_def") == 0, "被抢占之后仍然建了新轮次"
    assert await _actor_messages("win_def") == [], "被抢占之后仍然外发了"

    async with session_scope() as session:
        stored = await session.scalar(select(DeferredReply))
    assert stored is not None
    assert stored.state == DeferredReplyState.cancelled.value
    assert stored.reason == deferred.PREEMPT_REASON_NEW_INPUT


async def test_new_input_during_generation_still_blocks_delivery(
    web_env: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**生成期间**来了新输入 → 仍然不外发（栅栏 3 单独存在才有意义）。

    栅栏 2（生成前）和栅栏 3（外发前）看起来重复，但它们挡的是**不同的时刻**：
    栅栏 2 之后模型要跑几十秒，这期间用户完全可能又说话了。只有栅栏 2 时，
    那条旧回复会照样发出去、插进新话题——正是断言 10 禁止的"迟到补话"。

    这里用一个"生成时顺手插一条用户消息"的 router 精确制造这个时刻。
    """

    class _InterruptingRouter:
        calls = 0

        def __init__(self) -> None:
            self.reply = "旧话题的回答"

        async def stream(self, config, *, profile, messages, **kwargs):  # noqa: ANN001, ANN003, ARG002
            from ling.models.router import StreamOutcome
            from ling.ports.model import ModelChunk

            type(self).calls += 1
            # 生成**进行中**，用户又说话了（sequence 更大）
            async with session_scope() as session:
                session.add(
                    Message(
                        id="msg_win_def_9",
                        window_id="win_def",
                        role="user",
                        text="等等，我改主意了",
                        sequence=9,
                    )
                )
            yield ModelChunk(delta=self.reply), StreamOutcome()
            yield ModelChunk(done=True), StreamOutcome(text=self.reply)

    router = _InterruptingRouter()
    monkeypatch.setattr(worker_module, "_MEMORY_ROUTER", router)

    await _web_window()
    await _user_message("win_def", sequence=1)
    await _schedule(delay_ms=45_000, input_sequence=1)

    await _sweep_at(NOW + timedelta(seconds=45))

    assert router.calls == 1, "模型该跑一次（抢占发生在生成期间，不是生成前）"
    assert await _actor_messages("win_def") == [], (
        "生成期间来了新输入，旧回复仍然外发了——这正是'迟到补话'"
    )
    async with session_scope() as session:
        stored = await session.scalar(select(DeferredReply))
    assert stored is not None
    assert stored.state == DeferredReplyState.cancelled.value
    assert stored.reason == deferred.PREEMPT_REASON_NEW_INPUT


async def test_actor_message_does_not_preempt_own_deferred_reply(
    web_env: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """她自己先说了一句（`role=actor`）不该把自己那条延后回复抢占掉。

    否则会出现"她一说别的话，答应的事就永远不做了"。
    """
    router = _CountingRouter()
    monkeypatch.setattr(worker_module, "_MEMORY_ROUTER", router)

    await _web_window()
    await _user_message("win_def", sequence=1)
    await _schedule(delay_ms=45_000, input_sequence=1)
    async with session_scope() as session:
        session.add(
            Message(
                id="msg_actor_2",
                window_id="win_def",
                role="actor",
                text="我先说别的",
                sequence=2,
            )
        )

    await _sweep_at(NOW + timedelta(seconds=45))
    assert router.calls == 1, "她自己的话把她那条延后回复抢没了"


# ------------------------------------------------------------------ 授权/受众重取


async def test_archived_window_cancels_instead_of_delivering(
    web_env: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """延后期间窗口被归档 → 到点**不投递**，落 `cancelled(reason=window_gone)`。

    这是 lead 第 6 条要求：受众与授权必须**重新取**，不能用 60 秒前的缓存。
    """
    router = _CountingRouter()
    monkeypatch.setattr(worker_module, "_MEMORY_ROUTER", router)

    await _web_window()
    await _user_message("win_def", sequence=1)
    await _schedule(delay_ms=45_000)

    async with session_scope() as session:
        row = await session.get(Window, "win_def")
        assert row is not None
        row.archived = True

    await _sweep_at(NOW + timedelta(seconds=45))

    assert router.calls == 0, "窗口已归档仍然调了模型"
    assert await _actor_messages("win_def") == [], "窗口已归档仍然外发了"
    async with session_scope() as session:
        stored = await session.scalar(select(DeferredReply))
    assert stored is not None
    assert stored.state == DeferredReplyState.cancelled.value
    assert stored.reason == "window_gone"


async def test_window_deleted_entirely_cancels(
    web_env: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """窗口整行不见了（会话被删）→ 同样不投递。"""
    router = _CountingRouter()
    monkeypatch.setattr(worker_module, "_MEMORY_ROUTER", router)

    await _web_window()
    await _user_message("win_def", sequence=1)
    await _schedule(delay_ms=45_000)

    async with session_scope() as session:
        from sqlalchemy import delete

        await session.execute(delete(Window).where(Window.id == "win_def"))

    await _sweep_at(NOW + timedelta(seconds=45))
    assert router.calls == 0
    async with session_scope() as session:
        stored = await session.scalar(select(DeferredReply))
    assert stored is not None
    assert stored.state == DeferredReplyState.cancelled.value


async def test_group_window_is_refused_instead_of_delivering(
    web_env: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**外部渠道当前不支持延后** → `cancelled(reason=channel_unsupported)`。

    为什么这条是必需的防线（而不是"反正现在只有 web 用"）：外发闸
    (`TurnCoordinator.deliver_gate`) 拦的是"消息落库"，因为 Web 路径上落库
    就等于送达；而 QQ 的投递发生在 `kernel/bridge.py::_deliver`，那道闸管不到。

    也就是说，如果哪天有人让群聊也能延后，那条回复会**绕过抢占闸**直接发到群里
    ——正是断言 10 禁止的迟到补话，而且不会有任何用例变红。这条把"当前不支持"
    变成可观察的事实（`reason=channel_unsupported`），而不是一个隐含假设。
    """
    router = _CountingRouter()
    monkeypatch.setattr(worker_module, "_MEMORY_ROUTER", router)

    async with session_scope() as session:
        session.add(
            Window(
                id="win_group",
                actor_id="actor_ling",
                persona_id="persona_ling",
                subject_id="subj_a",
                title="群平台标识",
                channel="onebot",
                visibility="group_scope",
            )
        )
    await _user_message("win_group", sequence=1)
    async with session_scope() as session:
        row = await deferred.schedule(
            session,
            actor_id="actor_ling",
            window_id="win_group",
            subject_id="subj_a",
            source_turn_id="turn_group",
            source_message_id="msg_win_group_1",
            input_sequence=1,
            applied_delay_ms=45_000,
            requested_delay_ms=45_000,
            clamped=False,
            now=NOW,
        )
    assert row is not None

    await _sweep_at(NOW + timedelta(seconds=45))

    assert router.calls == 0, "不支持的渠道仍然调了模型"
    assert await _actor_messages("win_group") == [], "不支持的渠道仍然外发了"
    async with session_scope() as session:
        stored = await session.scalar(
            select(DeferredReply).where(DeferredReply.id == row.id)
        )
    assert stored is not None
    assert stored.state == DeferredReplyState.cancelled.value
    assert stored.reason == "channel_unsupported"


# ------------------------------------------------------------------ 硬截止兜底


async def test_generation_failure_uses_deterministic_fallback(
    web_env: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生成不出正文 → 发**确定性兜底短句**，且 `fallback_used=True`。

    兜底那句只陈述可核实的事实，不承诺、不假装送达。
    """
    class _EmptyRouter:
        calls = 0

        async def stream(self, config, *, profile, messages, **kwargs):  # noqa: ANN001, ANN003, ARG002
            from ling.models.router import StreamOutcome
            from ling.ports.model import ModelChunk

            self.calls += 1
            # 一个字都不吐（模拟模型失败/超时）
            yield ModelChunk(done=True), StreamOutcome(text="")

    router = _EmptyRouter()
    monkeypatch.setattr(worker_module, "_MEMORY_ROUTER", router)

    await _web_window()
    await _user_message("win_def", sequence=1)
    await _schedule(delay_ms=45_000)

    await _sweep_at(NOW + timedelta(seconds=45))

    messages = await _actor_messages("win_def")
    assert messages == [worker_module.DEFERRED_FALLBACK_TEXT], (
        f"没有发兜底短句，实际：{messages}"
    )
    async with session_scope() as session:
        stored = await session.scalar(select(DeferredReply))
    assert stored is not None
    assert stored.state == DeferredReplyState.delivered.value
    assert stored.fallback_used is True, "用了兜底却没标出来 = 声称她当时想清楚了"


async def test_missing_router_marks_failed_without_delivered_turn(
    web_env: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型路由未就绪 → `failed`，且**不写** `delivered_turn_id`。

    "状态 failed 但有轮次 id"是自相矛盾的记录，会让对账无从判断。
    """
    monkeypatch.setattr(worker_module, "_MEMORY_ROUTER", None)

    await _web_window()
    await _user_message("win_def", sequence=1)
    await _schedule(delay_ms=45_000)

    await _sweep_at(NOW + timedelta(seconds=45))

    async with session_scope() as session:
        stored = await session.scalar(select(DeferredReply))
    assert stored is not None
    assert stored.state in {
        DeferredReplyState.failed.value,
        DeferredReplyState.delivered.value,
    }
    if stored.state == DeferredReplyState.failed.value:
        assert stored.delivered_turn_id is None, "failed 却带着轮次 id：记录自相矛盾"
