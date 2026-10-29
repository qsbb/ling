"""群聊轮询：刷到才看，一次轮询最多一次决策调用。

用户需求：「群聊可以以一定频率轮询（模拟人），如果"设定"她目前在看这个群聊，
轮询频率加快（实时聊天但又不是每条都触发 llm 节省成本）」。

本文件逐条钉住验收标准：

1. 无未读 → **0 次调用**
2. 同一窗口 20 条未读 → **1 次调用**（不是 20 次）
3. 双 worker 不重复处理同一窗口（租约）
4. 失败**不推进游标、不标已看**
5. 调用下降率可复核（20 → 1）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ling.domains.attention.service import (
    MAX_BATCH,
    GroupAttentionScheduler,
    build_batch_text,
)
from ling.domains.conversation.service import ConversationService
from ling.infra.db import session_scope
from ling.infra.models import Message, Window
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"


class _Chooser:
    """记录调用次数；可配置为抛错。"""

    def __init__(self, *, respond: bool = False, reason: str = "刷了一下", boom: bool = False) -> None:
        self.calls: list[str] = []
        # 群聊决策会带上"最近说话的人"，记下来供断言
        self.speakers: list[str | None] = []
        self._respond = respond
        self._reason = reason
        self._boom = boom

    async def __call__(self, batch_text: str, speaker_subject_id: str | None = None):  # noqa: ANN204
        self.calls.append(batch_text)
        self.speakers.append(speaker_subject_id)
        if self._boom:
            raise RuntimeError("模型挂了")
        return SimpleNamespace(respond=self._respond, reason=self._reason)


@pytest.fixture(autouse=True)
async def _clean_cursors(logged_in: TestEnv) -> None:  # noqa: ARG001
    """每个用例前清空调度游标。

    这些测试都会建游标，而 `due_windows` 是全局查询——不清的话
    上一个用例留下的聚焦窗口会排到前面，顺序断言就会指向错误的窗口。
    """
    from sqlalchemy import delete

    from ling.infra.models import AttentionCursorRow

    async with session_scope() as session:
        await session.execute(delete(AttentionCursorRow))
    yield
    async with session_scope() as session:
        await session.execute(delete(AttentionCursorRow))


async def _group_window(env: TestEnv, title: str = "群") -> str:
    from sqlalchemy import update

    r = await env.client.post("/v1/sessions", json={"title": title})
    session_id = r.json()["session_id"]
    async with session_scope() as session:
        await session.execute(
            update(Window).where(Window.id == session_id).values(visibility="group_scope")
        )
    return session_id


async def _seed_unseen(
    window_id: str,
    texts: list[str],
    *,
    start_seq: int = 1,
    senders: list[str | None] | None = None,
) -> list[str]:
    """直接写未读消息。跳过 HTTP 入站是为了单独验证调度器。"""
    ids: list[str] = []
    async with session_scope() as session:
        for i, text in enumerate(texts):
            mid = f"msg_poll_{window_id[-6:]}_{start_seq + i}"
            session.add(
                Message(
                    id=mid,
                    window_id=window_id,
                    role="user",
                    text=text,
                    sequence=start_seq + i,
                    state="completed",
                    attention_state="unseen",
                    sender_subject_id=(
                        senders[i] if senders and i < len(senders) else None
                    ),
                )
            )
            ids.append(mid)
    return ids


# ---------------------------------------------------------------- 1 & 2


async def test_empty_window_costs_zero_calls(logged_in: TestEnv) -> None:  # noqa: ARG001
    """没有未读就不该调模型。轮询本身是免费的。"""
    window_id = await _group_window(logged_in, "空群")
    chooser = _Chooser()

    async with session_scope() as session:
        scheduler = GroupAttentionScheduler(session, actor_id=ACTOR)
        result = await scheduler.poll_window(window_id, chooser=chooser)

    assert result.unseen_count == 0
    assert result.llm_calls == 0
    assert chooser.calls == [], "没有未读却调了模型"


async def test_twenty_messages_cost_one_call(logged_in: TestEnv) -> None:  # noqa: ARG001
    """20 条未读 → **1 次**决策调用。这是省钱的唯一来源。"""
    window_id = await _group_window(logged_in, "热闹群")
    await _seed_unseen(window_id, [f"群友第{i}句话" for i in range(20)])
    chooser = _Chooser()

    async with session_scope() as session:
        scheduler = GroupAttentionScheduler(session, actor_id=ACTOR)
        result = await scheduler.poll_window(window_id, chooser=chooser)

    assert result.unseen_count == 20
    assert result.llm_calls == 1, f"实际调了 {result.llm_calls} 次"
    assert result.baseline_calls == 20, "影子基线应是每条一次"
    assert len(chooser.calls) == 1
    # 验收第 5 条：下降率可复核
    assert result.saved_calls == 19


async def test_more_than_max_batch_is_capped(logged_in: TestEnv) -> None:  # noqa: ARG001
    """一次最多看 MAX_BATCH 条，剩下的留到下一轮。"""
    window_id = await _group_window(logged_in, "刷屏群")
    await _seed_unseen(window_id, [f"消息{i}" for i in range(MAX_BATCH + 5)])
    chooser = _Chooser()

    async with session_scope() as session:
        scheduler = GroupAttentionScheduler(session, actor_id=ACTOR)
        result = await scheduler.poll_window(window_id, chooser=chooser)

    assert result.unseen_count == MAX_BATCH
    assert result.llm_calls == 1


# ---------------------------------------------------------------- 3 双 worker


async def test_lease_prevents_double_processing(logged_in: TestEnv) -> None:  # noqa: ARG001
    """两个 worker 同时取到期窗口，不该拿到同一个。

    `SKIP LOCKED` + 租约。重复处理 = 重复调模型 = 白花钱。
    """
    a = await _group_window(logged_in, "群A")
    b = await _group_window(logged_in, "群B")

    now = datetime.now(UTC)
    async with session_scope() as session:
        s1 = GroupAttentionScheduler(session, actor_id=ACTOR)
        await s1.ensure_cursor(a, now=now)
        await s1.ensure_cursor(b, now=now)

    async with session_scope() as session:
        first = await GroupAttentionScheduler(session, actor_id=ACTOR).due_windows(now=now, limit=1)
    async with session_scope() as session:
        second = await GroupAttentionScheduler(session, actor_id=ACTOR).due_windows(now=now, limit=10)

    assert len(first) == 1
    assert first[0] not in second, f"同一窗口被两个 worker 取了：{first} / {second}"


async def test_lease_expires_so_window_can_be_retried(logged_in: TestEnv) -> None:  # noqa: ARG001
    """租约过期后可以重取——否则 worker 崩了窗口就永远卡住。"""
    window_id = await _group_window(logged_in, "卡住群")
    now = datetime.now(UTC)
    async with session_scope() as session:
        await GroupAttentionScheduler(session, actor_id=ACTOR).ensure_cursor(window_id, now=now)
    async with session_scope() as session:
        got = await GroupAttentionScheduler(session, actor_id=ACTOR).due_windows(now=now)
    assert window_id in got

    # 租约到期之后
    later = now + timedelta(minutes=5)
    async with session_scope() as session:
        again = await GroupAttentionScheduler(session, actor_id=ACTOR).due_windows(now=later)
    assert window_id in again, "租约到期后窗口仍取不到，会永久卡住"


# ---------------------------------------------------------------- 4 失败不写"看过"


async def test_failure_does_not_mark_seen(logged_in: TestEnv) -> None:  # noqa: ARG001
    """模型失败时**绝不**标已看。

    标了就等于"她看过但没回"，那批消息再也不会被看到，而且没人知道。
    """
    window_id = await _group_window(logged_in, "失败群")
    ids = await _seed_unseen(window_id, ["消息一", "消息二"])
    chooser = _Chooser(boom=True)

    async with session_scope() as session:
        scheduler = GroupAttentionScheduler(session, actor_id=ACTOR)
        result = await scheduler.poll_window(window_id, chooser=chooser)

    assert result.decision == "failed"
    assert result.llm_calls == 1

    async with session_scope() as session:
        rows = (
            await session.execute(Message.__table__.select().where(Message.id.in_(ids)))
        ).all()
        for row in rows:
            assert row.attention_state == "unseen", "失败却标了已看"
        svc = ConversationService(session, actor_id=ACTOR)
        assert len(await svc.unseen_messages(window_id)) == 2


# ---------------------------------------------------------------- 决策结果


async def test_reply_marks_only_target_as_replied(logged_in: TestEnv) -> None:  # noqa: ARG001
    """决定回应时：批次里只有**最后一条**记 replied，其余记 ignored。

    回复针对的是最近那句（话题落点），不是把整批都算成"回过"。
    """
    window_id = await _group_window(logged_in, "回应群")
    ids = await _seed_unseen(window_id, ["第一句", "第二句", "第三句"])
    chooser = _Chooser(respond=True, reason="接一句")

    async with session_scope() as session:
        scheduler = GroupAttentionScheduler(session, actor_id=ACTOR)
        result = await scheduler.poll_window(window_id, chooser=chooser)

    assert result.decision == "replied"

    async with session_scope() as session:
        rows = (
            await session.execute(Message.__table__.select().where(Message.id.in_(ids)))
        ).all()
        by_id = {r.id: r for r in rows}
        assert by_id[ids[-1]].handled_as == "replied"
        for mid in ids[:-1]:
            assert by_id[mid].handled_as == "ignored"
        for mid in ids:
            assert by_id[mid].attention_state == "seen"


async def test_silence_marks_all_ignored(logged_in: TestEnv) -> None:  # noqa: ARG001
    window_id = await _group_window(logged_in, "沉默群")
    ids = await _seed_unseen(window_id, ["闲聊一", "闲聊二"])
    chooser = _Chooser(respond=False, reason="不值得接")

    async with session_scope() as session:
        scheduler = GroupAttentionScheduler(session, actor_id=ACTOR)
        result = await scheduler.poll_window(window_id, chooser=chooser)

    assert result.decision == "ignored"
    async with session_scope() as session:
        rows = (
            await session.execute(Message.__table__.select().where(Message.id.in_(ids)))
        ).all()
        assert all(r.handled_as == "ignored" for r in rows)
        assert all(r.attention_state == "seen" for r in rows)


# ---------------------------------------------------------------- 冷却


async def test_cooldown_blocks_second_call(logged_in: TestEnv) -> None:  # noqa: ARG001
    """冷却期内再次轮询**不调模型**。

    这是"聚焦加速不退化成逐条调用"的关键：轮询可以 8 秒一次，
    但决策受最小间隔（默认 15 秒）约束。
    """
    window_id = await _group_window(logged_in, "冷却群")
    await _seed_unseen(window_id, ["第一波"])
    chooser = _Chooser()

    now = datetime.now(UTC)
    async with session_scope() as session:
        scheduler = GroupAttentionScheduler(session, actor_id=ACTOR)
        first = await scheduler.poll_window(window_id, chooser=chooser, now=now)
    assert first.llm_calls == 1

    # 5 秒后又来一条（在冷却期内）
    await _seed_unseen(window_id, ["第二波"], start_seq=100)
    async with session_scope() as session:
        scheduler = GroupAttentionScheduler(session, actor_id=ACTOR)
        second = await scheduler.poll_window(
            window_id, chooser=chooser, now=now + timedelta(seconds=5)
        )
    assert second.decision == "skipped"
    assert second.llm_calls == 0
    assert len(chooser.calls) == 1, "冷却期内又调了一次模型"

    # 过了冷却期就能调
    async with session_scope() as session:
        scheduler = GroupAttentionScheduler(session, actor_id=ACTOR)
        third = await scheduler.poll_window(
            window_id, chooser=chooser, now=now + timedelta(seconds=60)
        )
    assert third.llm_calls == 1


# ---------------------------------------------------------------- 批量文本


def test_batch_text_keeps_head_and_tail() -> None:
    """超长批次保留**首 2 + 末 12**。

    只留尾部会丢掉"最早说的重要事实"——`_rollup_summary` 已经踩过这个坑。
    """
    texts = [f"第{i}句" for i in range(30)]
    out = build_batch_text(texts)
    assert "第0句" in out, "最早的内容被丢了"
    assert "第1句" in out
    assert "第29句" in out
    assert "中间还有" in out
    assert "第15句" not in out, "中间的不该全塞进去"


def test_batch_text_short_batch_passes_through() -> None:
    assert build_batch_text(["a", "b"]) == "a\nb"
    assert build_batch_text(["  ", ""]) == ""


# ---------------------------------------------------------------- 聚焦


async def test_focus_makes_polling_faster(logged_in: TestEnv) -> None:  # noqa: ARG001
    """在看的群，下次轮询时间更近。"""
    window_id = await _group_window(logged_in, "聚焦群")
    now = datetime.now(UTC)

    async with session_scope() as session:
        scheduler = GroupAttentionScheduler(
            session, actor_id=ACTOR, base_interval=30, focus_interval=8
        )
        await scheduler.poll_window(window_id, chooser=_Chooser(), now=now)
        from ling.infra.models import AttentionCursorRow

        row = await session.get(AttentionCursorRow, window_id)
        assert row is not None
        from ling.domains.attention.service import JITTER_SPAN_SECONDS

        base_gap = (row.next_due_at - now).total_seconds()
        # 30 秒基础间隔 + 0..JITTER_SPAN 的固定抖动（去同拍用）
        assert 30 <= base_gap <= 30 + JITTER_SPAN_SECONDS, (
            f"未聚焦时应约 30~{30 + JITTER_SPAN_SECONDS} 秒，实际 {base_gap}"
        )

        await scheduler.focus(window_id, now=now)
        await scheduler.poll_window(window_id, chooser=_Chooser(), now=now + timedelta(minutes=1))
        row = await session.get(AttentionCursorRow, window_id)
        await session.refresh(row)
        focus_gap = (row.next_due_at - (now + timedelta(minutes=1))).total_seconds()
        assert 8 <= focus_gap <= 8 + JITTER_SPAN_SECONDS, (
            f"聚焦时应约 8~{8 + JITTER_SPAN_SECONDS} 秒，实际 {focus_gap}"
        )


# ---------------------------------------------------------------- 防刷屏上限


async def _add_actor_messages(window_id: str, n: int) -> None:
    """往窗口里塞 n 条她自己的发言（5 分钟内）。"""
    async with session_scope() as session:
        for i in range(n):
            session.add(
                Message(
                    id=f"msg_actor_{window_id[-6:]}_{i}",
                    window_id=window_id,
                    role="actor",
                    text="她说过的话",
                    sequence=80000 + i,
                    state="completed",
                )
            )


async def test_recent_actor_turns_counts_actor_messages(logged_in: TestEnv) -> None:  # noqa: ARG001
    from ling.domains.attention.service import recent_actor_turns

    window_id = await _group_window(logged_in, "计数群")
    async with session_scope() as session:
        assert await recent_actor_turns(session, window_id) == 0
    await _add_actor_messages(window_id, 3)
    async with session_scope() as session:
        assert await recent_actor_turns(session, window_id) == 3


async def test_polling_respects_group_budget(logged_in: TestEnv) -> None:  # noqa: ARG001
    """轮询路径**必须**守防刷屏上限。

    修之前 worker 里硬编码 `recent_actor_turns=0`，于是 L0 检查永远不触发——
    热门群她能每 15 秒回一次，正是这条上限要拦的事。
    """
    from types import SimpleNamespace

    from ling.domains.attention.service import make_group_chooser

    window_id = await _group_window(logged_in, "刷屏群")
    await _add_actor_messages(window_id, 3)  # 已经说了 3 次
    await _seed_unseen(window_id, ["群里又有人说话了"])

    class _Cfg:
        async def effective(self):  # noqa: ANN202
            return SimpleNamespace(
                dialogue=SimpleNamespace(
                    group_turn_budget=3,
                    private_may_stay_silent=True,
                    group_mention_requires_reply=True,
                    rest_may_skip=True,
                )
            )

    class _Router:
        """模型说"要回"——但如果上限生效，它根本不该被问到。"""

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, config, *, profile, messages, **kw):  # noqa: ANN001, ANN003
            from ling.models.router import StreamOutcome
            from ling.ports.model import ModelChunk

            self.calls += 1
            payload = '{"respond": true, "reason": "想插话"}'
            yield ModelChunk(delta=payload), StreamOutcome()
            yield ModelChunk(done=True), StreamOutcome(text=payload)

    router = _Router()
    async with session_scope() as session:
        chooser = make_group_chooser(
            session=session,
            actor_id=ACTOR,
            window_id=window_id,
            router=router,
            config_reader=_Cfg(),
        )
        choice = await chooser("群里又有人说话了")

    assert choice.respond is False, "已达上限却仍然回应"
    assert "上限" in choice.reason, choice.reason
    assert router.calls == 0, "被 L0 挡住时不该浪费一次模型调用"


# ---------------------------------------------------------------- 跨群调度


async def test_focused_window_is_polled_first(logged_in: TestEnv) -> None:  # noqa: ARG001
    """正在看的群优先——她此刻就盯着它。"""
    from ling.domains.attention.service import GroupAttentionScheduler

    a = await _group_window(logged_in, "群A")
    b = await _group_window(logged_in, "群B")
    now = datetime.now(UTC)

    async with session_scope() as session:
        s = GroupAttentionScheduler(session, actor_id=ACTOR)
        # B 先到期，但 A 在聚焦
        await s.ensure_cursor(b, now=now - timedelta(minutes=5))
        await s.ensure_cursor(a, now=now)
        await s.focus(a, now=now)

    async with session_scope() as session:
        order = await GroupAttentionScheduler(session, actor_id=ACTOR).due_windows(now=now)
        assert a in order and b in order, order
        assert order.index(a) < order.index(b), f"聚焦的群没被优先：{order}"


async def test_mention_pending_beats_plain_unseen(logged_in: TestEnv) -> None:  # noqa: ARG001
    """有人在等回应的群优先于普通未读——@ 是有人叫她。"""
    from ling.domains.attention.service import GroupAttentionScheduler

    plain = await _group_window(logged_in, "普通群")
    mentioned = await _group_window(logged_in, "被叫群")
    now = datetime.now(UTC)

    async with session_scope() as session:
        s = GroupAttentionScheduler(session, actor_id=ACTOR)
        await s.ensure_cursor(plain, now=now - timedelta(minutes=5))
        await s.ensure_cursor(mentioned, now=now)
        await s.await_supplement(mentioned, now=now)

    # 注意：`await_supplement` 会把 next_due_at 推到 8 秒后（等补充），
    # 所以此刻它**本来就不该到期**。要查 now+10s 才看得到两者。
    later = now + timedelta(seconds=10)
    async with session_scope() as session:
        order = await GroupAttentionScheduler(session, actor_id=ACTOR).due_windows(now=later)
        assert mentioned in order and plain in order, order
        assert order.index(mentioned) < order.index(plain), (
            f"@ 待处理的群没被优先：{order}"
        )


async def test_jitter_is_deterministic_and_desyncs(logged_in: TestEnv) -> None:  # noqa: ARG001
    """抖动必须是**固定**的：同一窗口每次都一样，不同窗口不一样。

    用随机数会让"为什么先看这个群"变成不可复现的事。
    """
    from ling.domains.attention.service import JITTER_SPAN_SECONDS, _jitter_seconds

    windows = [await _group_window(logged_in, f"抖动群{i}") for i in range(6)]
    values = [_jitter_seconds(w) for w in windows]

    # 固定：再算一次结果一致
    assert [_jitter_seconds(w) for w in windows] == values
    # 有界
    assert all(0 <= v <= JITTER_SPAN_SECONDS for v in values)
    # 确实打散了（6 个窗口不该全同拍）
    assert len(set(values)) > 1, f"所有窗口抖动相同，等于没抖动：{values}"


async def test_windows_per_tick_is_capped(logged_in: TestEnv) -> None:  # noqa: ARG001
    """单次 tick 最多处理固定个窗口，防止一次串行打很多次模型。"""
    from ling.domains.attention.service import MAX_WINDOWS_PER_TICK, GroupAttentionScheduler

    now = datetime.now(UTC)
    for i in range(MAX_WINDOWS_PER_TICK + 3):
        w = await _group_window(logged_in, f"排队群{i}")
        async with session_scope() as session:
            await GroupAttentionScheduler(session, actor_id=ACTOR).ensure_cursor(w, now=now)

    async with session_scope() as session:
        got = await GroupAttentionScheduler(session, actor_id=ACTOR).due_windows(
            now=now, limit=999
        )
    assert len(got) <= MAX_WINDOWS_PER_TICK


# ---------------------------------------------------------------- 错过（时效）


async def _seed_unseen_aged(window_id: str, texts: list[str], *, minutes_ago: int,
                            start_seq: int = 500) -> list[str]:
    """写一批**指定年龄**的未读消息。"""
    from datetime import timedelta as _td

    ids: list[str] = []
    created = datetime.now(UTC) - _td(minutes=minutes_ago)
    async with session_scope() as session:
        for i, text in enumerate(texts):
            mid = f"msg_aged_{window_id[-6:]}_{start_seq + i}"
            session.add(
                Message(
                    id=mid, window_id=window_id, role="user", text=text,
                    sequence=start_seq + i, state="completed",
                    attention_state="unseen", created_at=created,
                )
            )
            ids.append(mid)
    return ids


async def test_stale_messages_are_missed_not_answered(logged_in: TestEnv) -> None:  # noqa: ARG001
    """过了时效就不再去回，标成**错过**，且不花模型调用。

    她忙了一下午，不该现在去回那条 5 小时前的"下午三点开会"。
    晚到的回复比没有回复更像故障。
    """
    from ling.domains.attention.service import RELEVANCE_TTL_MINUTES

    window_id = await _group_window(logged_in, "过时群")
    ids = await _seed_unseen_aged(
        window_id, ["下午三点开会", "别忘了"],
        minutes_ago=RELEVANCE_TTL_MINUTES + 30,
    )
    chooser = _Chooser(respond=True, reason="想接话")

    async with session_scope() as session:
        scheduler = GroupAttentionScheduler(session, actor_id=ACTOR)
        result = await scheduler.poll_window(window_id, chooser=chooser)

    assert result.decision == "missed", result
    assert result.llm_calls == 0, "过期的消息不该花模型调用"
    assert chooser.calls == [], "过期的消息不该进模型"

    async with session_scope() as session:
        rows = (
            await session.execute(Message.__table__.select().where(Message.id.in_(ids)))
        ).all()
        assert all(r.attention_state == "missed" for r in rows), "应标成 missed"
        assert all(r.handled_as == "missed" for r in rows)


async def test_only_fresh_messages_reach_the_model(logged_in: TestEnv) -> None:  # noqa: ARG001
    """过期的丢掉，新鲜的照常判断——不能因为混着就整批放弃。"""
    from ling.domains.attention.service import RELEVANCE_TTL_MINUTES

    window_id = await _group_window(logged_in, "混合群")
    await _seed_unseen_aged(
        window_id, ["很久以前的话"], minutes_ago=RELEVANCE_TTL_MINUTES + 60,
        start_seq=600,
    )
    await _seed_unseen_aged(window_id, ["刚刚说的话"], minutes_ago=1, start_seq=700)
    chooser = _Chooser(respond=False, reason="看看")

    async with session_scope() as session:
        scheduler = GroupAttentionScheduler(session, actor_id=ACTOR)
        result = await scheduler.poll_window(window_id, chooser=chooser)

    assert result.decision == "ignored", result
    assert result.llm_calls == 1
    assert chooser.calls and "刚刚说的话" in chooser.calls[0]
    assert "很久以前的话" not in chooser.calls[0], "过期内容进了模型"


async def test_missing_timestamp_is_treated_as_fresh(logged_in: TestEnv) -> None:  # noqa: ARG001
    """读不到时间戳就**当作新鲜**。

    宁可多看一眼，也不要因为读不到时间戳就把消息判成错过——
    那等于静默吞掉她的消息，而用户明确要求"紧急/重要不能被静默吞掉"。
    """
    window_id = await _group_window(logged_in, "无时间戳群")
    await _seed_unseen(window_id, ["没有时间戳的消息"])
    # 把 created_at 清成 NULL 不可行（非空约束），改为验证 _split_stale
    from ling.domains.attention.service import GroupAttentionScheduler

    class _NoTs:
        message_id = "m"
        sequence = 1
        created_at = None

    async with session_scope() as session:
        s = GroupAttentionScheduler(session, actor_id=ACTOR)
        fresh, stale = s._split_stale([_NoTs()], datetime.now(UTC))  # noqa: SLF001
    assert len(fresh) == 1 and not stale


# ---------------------------------------------------------------- 群里的"谁说的"


async def test_group_batch_labels_who_said_what(logged_in: TestEnv) -> None:
    """群里的一批消息要标上是谁说的。

    群窗口是多个人的。不标发言人，她只能看到一堆没主的话——
    既没法回指（"你刚才问的"），也没法按人分亲疏。
    """
    from ling.infra.db import session_scope
    from ling.infra.models import Subject

    window_id = await _group_window(logged_in, "带发言人")
    async with session_scope() as session:
        for sid, name in (("subj_poll_a", "小明"), ("subj_poll_b", "小红")):
            if await session.get(Subject, sid) is None:
                session.add(Subject(id=sid, display_name=name, trust_level="known"))

    await _seed_unseen(
        window_id,
        ["今天好累", "我也是"],
        senders=["subj_poll_a", "subj_poll_b"],
    )
    chooser = _Chooser()
    async with session_scope() as session:
        await GroupAttentionScheduler(session, actor_id=ACTOR).poll_window(
            window_id, chooser=chooser
        )

    assert chooser.calls, "没问模型"
    batch = chooser.calls[0]
    assert "小明：" in batch, f"没标发言人：{batch!r}"
    assert "小红：" in batch, f"没标发言人：{batch!r}"


async def test_group_decision_uses_latest_speaker(logged_in: TestEnv) -> None:
    """「要不要接」看的是**最近开口的那个人**，不是群窗口的 subject。

    群窗口的 subject 是主人（这个群属于谁），拿它当"对谁"是错误归属。
    """
    from ling.infra.db import session_scope
    from ling.infra.models import Subject

    window_id = await _group_window(logged_in, "最近发言人")
    async with session_scope() as session:
        for sid, name in (("subj_poll_c", "甲"), ("subj_poll_d", "乙")):
            if await session.get(Subject, sid) is None:
                session.add(Subject(id=sid, display_name=name, trust_level="known"))

    await _seed_unseen(
        window_id,
        ["我先说", "我后说"],
        senders=["subj_poll_c", "subj_poll_d"],
    )
    chooser = _Chooser()
    async with session_scope() as session:
        await GroupAttentionScheduler(session, actor_id=ACTOR).poll_window(
            window_id, chooser=chooser
        )

    assert chooser.speakers, "决策没收到发言人"
    assert chooser.speakers[0] == "subj_poll_d", (
        f"用的是 {chooser.speakers[0]}，应该是最新说话的人"
    )


async def test_group_relationship_reaches_the_prompt(logged_in: TestEnv) -> None:
    """群聊决策的提示词里要出现「我和这个人的关系」。

    这是本轮的目的：原来群里刻意不带关系（怕把"对主人的熟悉度"
    当成"对说话人的熟悉度"）。现在每个成员都有自己的 subject，
    按**最近说话的人**取关系，就既能用又不会错误归属。
    """
    import json as _json

    from ling.domains.attention.service import make_group_chooser
    from ling.domains.relationship.service import RelationshipService
    from ling.infra.db import session_scope
    from ling.infra.models import Subject
    from ling.infra.security import SecretBox
    from ling.settings.service import ConfigService

    window_id = await _group_window(logged_in, "关系进群聊")
    async with session_scope() as session:
        if await session.get(Subject, "subj_poll_e") is None:
            session.add(Subject(id="subj_poll_e", display_name="熟人", trust_level="known"))
        await session.flush()
        rel = RelationshipService(session, actor_id=ACTOR)
        state = await rel.state("subj_poll_e")
        state.familiarity = 0.9

    captured: dict = {}

    class _Router:
        async def stream(self, config, *, profile, messages, **kw):  # noqa: ANN001, ANN003, ARG002
            from ling.models.router import StreamOutcome
            from ling.ports.model import ModelChunk

            captured["prompt"] = "\n".join(m.content for m in messages)
            payload = _json.dumps({"respond": False, "reason": "先忙"})
            yield ModelChunk(delta=payload), StreamOutcome()
            yield ModelChunk(done=True), StreamOutcome(text=payload)

    settings = logged_in.settings

    async with session_scope() as session:
        cfg = ConfigService(
            session, SecretBox(settings), test_mode_default=settings.test_mode,
            persona_id=settings.persona_id,
        )
        chooser = make_group_chooser(
            session=session, actor_id=ACTOR, window_id=window_id,
            router=_Router(), config_reader=cfg, persona_id=settings.persona_id,
        )
        await chooser("熟人：在吗", speaker_subject_id="subj_poll_e")

    prompt = captured.get("prompt", "")
    assert "我和这个人的关系" in prompt, f"关系没进群聊决策：{prompt[:400]}"
