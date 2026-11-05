"""A1 回归：停止信号属于「这一轮（attempt）」，不属于 turn_id。

## 被测缺陷（实测复现）

用户「停止 → 重试」时，重试出来的新一轮**立刻被旧停止标记停掉**，
重试多少次都一样。成因链：

1. ``TurnControlStore.clear()`` 当时**没有任何生产调用点**，也没有过期逻辑
   → 上一轮留下的 ``stop_requested_at`` 永久留在 ``turn_controls``；
2. retry 复用同一个 ``turn_id``（``bump_attempt`` 只把 attempt 加一），
   随后 ``run_turn(turn_id=turn_id, attempt=new_attempt)``；
3. ``run_turn`` 每轮新建 ``StopToken`` 并 ``create_task(control.watch(turn_id))``，
   而 watch 是 ``while True: await asyncio.sleep(0.5)``（先睡再查）；
4. 首次检查（约 0.5s）命中旧标记 → ``token.stopped=True`` →
   ``turn.py`` 在编译阶段就收敛成 stopped。

唯一逃生口是新一轮 <0.5s 跑完——真实模型场景不可能。

## 修法

停止信号的作用域改为 ``(turn_id, attempt)``（``turn_controls`` 主键加 attempt）。
旧标记只对旧 attempt 生效，新一轮查自己那一行，天然不命中；旧轮次（还在跑）
仍然轮询自己的 attempt，所以**照样停得下来**。

## 为什么这些测试能抓到回退

断言的是「新一轮真的跑完并产生了回复」（``turn.completed`` + 非空正文 +
库里 actor 消息 + turn 状态 completed），不是「接口返回 200」。
旧实现下这些用例全部收敛成 ``turn.stopped``，正文为空。
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from sqlalchemy import select

from ling.infra.coordination import TurnControlStore
from ling.infra.db import session_scope
from ling.infra.models import Message, Turn
from ling.kernel.turn import TurnCoordinator
from ling.models.registry import ProviderRegistry
from ling.models.router import ModelRouter
from ling.ports.config import EffectiveConfig, ProfileBinding, ProviderConfig
from tests.integration.conftest import TestEnv, read_sse

pytestmark = pytest.mark.w0_web

# fake 回复约 24 段，0.25s/段 ≈ 6s —— 远超 watch 的 0.5s 首次轮询。
# 必须这么慢，否则"新一轮在首次检查前就跑完"会掩盖缺陷（这正是旧测试
# 只断言 200 时漏掉的原因）。
SLOW_CHUNK_DELAY = 0.25
POLL = 0.05


def _slow_config() -> EffectiveConfig:
    return EffectiveConfig(
        revision=1,
        providers={
            "fake": ProviderConfig(
                provider_id="fake",
                kind="fake",
                enabled=True,
                extra={"chunk_delay": SLOW_CHUNK_DELAY},
            )
        },
        profiles={
            "dialogue.fast": ProfileBinding(
                profile="dialogue.fast",
                provider_id="fake",
                model_id="deterministic",
                timeout_seconds=60.0,
            )
        },
        test_mode=True,
    )


async def _new_session(client) -> str:
    response = await client.post("/v1/sessions", json={"title": "停止信号作用域"})
    assert response.status_code == 201
    return response.json()["session_id"]


async def _publish_slow_provider(client) -> None:
    """让 HTTP 路径（retry 路由）也用慢速 fake provider。"""
    draft = await client.put(
        "/v1/settings/draft",
        json={
            "providers": [
                {
                    "provider_id": "fake",
                    "kind": "fake",
                    "enabled": True,
                    "extra": {"chunk_delay": SLOW_CHUNK_DELAY},
                }
            ],
            "profiles": [
                {
                    "profile": "dialogue.fast",
                    "provider_id": "fake",
                    "model_id": "deterministic",
                }
            ],
            "test_mode": True,
        },
    )
    assert draft.status_code == 200, draft.text
    published = await client.post(
        "/v1/settings/publish", json={"revision": draft.json()["revision"]}
    )
    assert published.status_code == 200, published.text


async def _make_turn(window_id: str, text: str) -> str:
    """在数据库里建一条真实轮次（``_finalize`` 需要真实行）。"""
    from ling.domains.conversation.service import ConversationService

    async with session_scope() as session:
        conv = ConversationService(session)
        turn = await conv.create_turn(
            window_id, client_turn_id=f"ct-{uuid.uuid4().hex[:8]}", config_revision=1
        )
        await conv.add_message(window_id, role="user", text=text, turn_id=turn.id)
        return turn.id


async def _bump_attempt(turn_id: str) -> int:
    """等价于 retry 路由里的 ``bump_attempt``（只加 attempt，不删停止标记）。"""
    from ling.domains.conversation.service import ConversationService

    async with session_scope() as session:
        conv = ConversationService(session)
        bumped = await conv.bump_attempt(turn_id)
        return int(bumped.attempt or 1)


def _coordinator(env: TestEnv, control: TurnControlStore) -> TurnCoordinator:
    registry = ProviderRegistry(lambda _pid: None)
    return TurnCoordinator(
        router=ModelRouter(registry),
        actor_id="actor_ling",
        persona_id="persona_ling",
        cancels=env.app.state.cancels,
        stream_log=env.app.state.stream_log,
        turn_control=control,
    )


async def _drain(queue) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    while True:
        item = await asyncio.wait_for(queue.get(), timeout=60)
        if item is None:
            return events
        events.append((item.event, item.data))


async def _run(env: TestEnv, coordinator: TurnCoordinator, turn_id: str, *, attempt: int,
               window_id: str, text: str):
    return await coordinator.run_turn(
        window_id=window_id,
        subject_id=env.subject_id,
        turn_id=turn_id,
        user_text=text,
        config=_slow_config(),
        attempt=attempt,
    )


async def _wait_until(predicate, *, timeout: float = 10.0, interval: float = 0.01) -> bool:
    """轮询等待条件成立；超时返回 False（不抛，交给调用方的断言给消息）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def _wait_generating(env: TestEnv, turn_id: str, queue) -> None:
    """等这一轮**真的**进入运行态并已吐出第一段。

    原来是 ``await asyncio.sleep(SLOW_CHUNK_DELAY * 3)``（盲等 0.75s）。盲等的
    问题不是"睡太短"，而是**它承诺的东西和实际状态无关**：生成比预期快、调度
    有延迟、机器被别的测试压住时，轮次可能已经结束，随后的
    ``cancels.is_running(...) is True`` 就变成看运气（全量跑时偶发红）。

    改成等条件，两个都要：

    - ``cancels.is_running(turn_id)``：任务还活着——"旧轮次停得下来"的前提；
    - ``queue.qsize() > 0``：第一段 delta 已产生——证明真的在生成，而不是卡在
      上下文编译阶段就被停止（那种情况下停止测不到"生成中被停"的语义）。

    只读 ``qsize`` 不消费队列，``_drain`` 之后照常读到**全量**事件。
    """
    ok = await _wait_until(
        lambda: env.app.state.cancels.is_running(turn_id) and queue.qsize() > 0
    )
    assert ok, f"轮次 {turn_id} 未在超时内进入生成态（未登记运行或首段迟迟未到）"


async def _turn_row(turn_id: str) -> Turn:
    async with session_scope() as session:
        row = (await session.execute(select(Turn).where(Turn.id == turn_id))).scalar_one()
        session.expunge(row)
        return row


async def _actor_messages(turn_id: str) -> list[Message]:
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(Message).where(
                        Message.turn_id == turn_id, Message.role == "actor"
                    )
                )
            ).scalars()
        )
        for row in rows:
            session.expunge(row)
        return rows


def _completed_payload(events: list[tuple[str, dict]]) -> dict:
    completed = [data for name, data in events if name == "turn.completed"]
    assert completed, f"新一轮没有完成，实际事件: {[n for n, _ in events]}"
    return completed[-1]


def _assert_reply(events: list[tuple[str, dict]], turn_id: str) -> str:
    """新一轮必须**真的产出回复**：完成事件里带非空正文。"""
    payload = _completed_payload(events)
    assert payload["state"] == "completed", payload
    message = payload.get("message") or {}
    text = str(message.get("text") or "")
    assert text, f"新一轮完成了却没有正文（被旧停止标记吃掉了）: {payload}"
    return text


# ---------------------------------------------------------------- 时序 A


async def test_stop_then_retry_completes_new_round(logged_in: TestEnv) -> None:
    """时序 A：「stop → 等旧轮次终结 → retry」，新一轮必须正常跑完。

    旧实现下这一轮会在首次 0.5s 轮询时命中旧标记，直接收敛成 stopped，
    正文为空——用户的观感是"重试永远没回复"。
    """
    control = TurnControlStore(poll_seconds=POLL)
    coordinator = _coordinator(logged_in, control)
    session_id = await _new_session(logged_in.client)
    turn_id = await _make_turn(session_id, "讲个故事")

    # 第 1 轮：跑到一半用户点停止（"跑到一半"由条件等待保证，不是盲等定时）
    queue = await _run(
        logged_in, coordinator, turn_id, attempt=1, window_id=session_id, text="讲个故事"
    )
    await _wait_generating(logged_in, turn_id, queue)
    await control.request_stop(turn_id, 1)
    events_1 = await _drain(queue)
    assert [n for n, _ in events_1][-1] == "turn.stopped", events_1
    assert await _turn_row(turn_id) is not None
    assert (await _turn_row(turn_id)).state == "stopped"
    # 落终态后本 attempt 的标记没有读者了：应被清掉（不是根因，但不留垃圾）
    assert await control.is_stop_requested(turn_id, 1) is False, "终结后标记没有被清理"

    # 用户点重试：retry 路由只 bump attempt，复用同一个 turn_id
    new_attempt = await _bump_attempt(turn_id)
    assert new_attempt == 2
    queue2 = await _run(
        logged_in, coordinator, turn_id, attempt=new_attempt, window_id=session_id,
        text="讲个故事",
    )
    events_2 = await _drain(queue2)
    text = _assert_reply(events_2, turn_id)

    assert (await _turn_row(turn_id)).state == "completed"
    actors = await _actor_messages(turn_id)
    assert any(m.attempt == 2 and m.text.strip() for m in actors), (
        f"重试轮没有落库正文: {[(m.attempt, m.text[:20]) for m in actors]}"
    )
    assert text.strip()


async def test_stale_marker_from_previous_attempt_does_not_block_retry(
    logged_in: TestEnv,
) -> None:
    """停过的那一轮留下标记，HTTP retry 仍必须跑完并产生回复。

    这是最直接对应用户报障的用例：stop 之后（那一轮已终结）点重试。
    旧实现把信号绑在 turn_id 上，所以这个标记对新一轮依然生效。
    """
    await _publish_slow_provider(logged_in.client)
    session_id = await _new_session(logged_in.client)
    turn_id = await _make_turn(session_id, "原始输入")

    from ling.contracts.v1 import TurnState
    from ling.domains.conversation.service import ConversationService

    # 模拟"用户在生成中点过停止、那一轮已落 stopped、标记还在库里"
    control = TurnControlStore(poll_seconds=POLL)
    await control.request_stop(turn_id, 1)
    async with session_scope() as session:
        conv = ConversationService(session)
        await conv.set_turn_state(turn_id, TurnState.stopped)

    retry = await logged_in.client.post(f"/v1/turns/{turn_id}/retry")
    assert retry.status_code == 200, retry.text[:300]
    events = await read_sse(retry)
    _assert_reply(events, turn_id)

    assert (await _turn_row(turn_id)).state == "completed"
    actors = await _actor_messages(turn_id)
    assert any(m.attempt == 2 and m.text.strip() for m in actors)


# ---------------------------------------------------------------- 时序 B


async def test_immediate_retry_while_old_turn_running(logged_in: TestEnv) -> None:
    """时序 B：「stop → 立即 retry（旧轮次还没终结）」。

    正确行为有两半，缺一不可：

    1. 旧标记仍然只作用于旧 attempt → **旧轮次照样停得下来**（不能为了让
       重试能跑就把标记清掉，那是方案 3 的坑：旧轮次会永远停不下来）；
    2. 新一轮的 attempt 行不受旧标记污染 → 旧轮次终结后重试能正常跑完。

    另外，旧轮次仍在收尾时 retry 路由应给明确的 409（"正在停止，请稍候"），
    而不是默默接受一个注定被旧标记吃掉的新轮次。
    """
    control = TurnControlStore(poll_seconds=POLL)
    coordinator = _coordinator(logged_in, control)
    session_id = await _new_session(logged_in.client)
    turn_id = await _make_turn(session_id, "慢慢说")

    queue = await _run(
        logged_in, coordinator, turn_id, attempt=1, window_id=session_id, text="慢慢说"
    )
    await _wait_generating(logged_in, turn_id, queue)
    # 先确认旧轮次**确实还在跑**，再点停止：
    #   原来是在"停止之后"才断言 is_running，而停止一旦被 watch 轮询到，
    #   轮次随时可能收敛（下一个 chunk 边界即退出），这条断言就成了看运气的
    #   时序断言——全量跑时偶发红。
    assert logged_in.app.state.cancels.is_running(turn_id) is True
    await control.request_stop(turn_id, 1)

    # 旧轮次还在跑：标记在、旧轮次可被停
    assert await control.is_stop_requested(turn_id, 1) is True

    # 立即 retry：attempt 变成 2，但新一轮**不被旧标记污染**
    new_attempt = await _bump_attempt(turn_id)
    assert new_attempt == 2
    assert await control.is_stop_requested(turn_id, new_attempt) is False, (
        "旧 attempt 的停止标记泄漏到了新一轮"
    )
    # 旧标记仍在（旧轮次还没终结），否则旧轮次会停不下来
    assert await control.is_stop_requested(turn_id, 1) is True, (
        "清理过度：旧轮次还在跑就把它的停止标记删了，它会永远停不下来"
    )

    early = await logged_in.client.post(f"/v1/turns/{turn_id}/retry")
    assert early.status_code == 409, (
        f"旧轮次还在收尾时应明确拒绝并提示稍候，实际 {early.status_code}: {early.text[:200]}"
    )

    events_1 = await _drain(queue)
    assert [n for n, _ in events_1][-1] == "turn.stopped", (
        f"旧轮次没有停下来（标记被误清）: {[n for n, _ in events_1]}"
    )
    assert (await _turn_row(turn_id)).state == "stopped"

    # 旧轮次终结后重试：必须真的跑完
    queue2 = await _run(
        logged_in, coordinator, turn_id, attempt=new_attempt, window_id=session_id,
        text="慢慢说",
    )
    events_2 = await _drain(queue2)
    _assert_reply(events_2, turn_id)
    assert (await _turn_row(turn_id)).state == "completed"


# ---------------------------------------------------------------- 连续重试


async def test_repeated_retries_each_complete(logged_in: TestEnv) -> None:
    """连续 retry 多次：每一次都必须跑完并产生回复。

    每轮重试前都**人为留下上一 attempt 的标记**（模拟"标记从不清"的历史
    状态），确保这不是"只对第一次重试有效"的修复。
    """
    control = TurnControlStore(poll_seconds=POLL)
    coordinator = _coordinator(logged_in, control)
    session_id = await _new_session(logged_in.client)
    turn_id = await _make_turn(session_id, "再讲一次")

    # attempt 1：用户中途停止（等它真的在生成，而不是盲等固定时长）
    queue = await _run(
        logged_in, coordinator, turn_id, attempt=1, window_id=session_id, text="再讲一次"
    )
    await _wait_generating(logged_in, turn_id, queue)
    await control.request_stop(turn_id, 1)
    events = await _drain(queue)
    assert [n for n, _ in events][-1] == "turn.stopped"

    texts: list[str] = []
    for expected_attempt in (2, 3, 4):
        # 残留标记：上一个 attempt 的停止信号（历史实现里它从不清除）
        await control.request_stop(turn_id, expected_attempt - 1)
        attempt = await _bump_attempt(turn_id)
        assert attempt == expected_attempt
        queue = await _run(
            logged_in, coordinator, turn_id, attempt=attempt, window_id=session_id,
            text="再讲一次",
        )
        events = await _drain(queue)
        texts.append(_assert_reply(events, turn_id))

    assert len(texts) == 3
    assert (await _turn_row(turn_id)).state == "completed"
    actors = await _actor_messages(turn_id)
    completed_attempts = {m.attempt for m in actors if m.text.strip()}
    assert {2, 3, 4} <= completed_attempts, (
        f"多次重试里有轮次没有产出正文: {sorted(completed_attempts)}"
    )
