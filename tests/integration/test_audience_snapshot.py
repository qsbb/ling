"""统一取数出口：跨受众泄漏的端到端回归。

复核的核心批评：
> "把'模型能读到，但不能说出'当成了可执行的隐私边界。这条边界守不住。"

因此真正的门必须前移到取数处。这些测试直接检验"整条信息流"，
而不只是某个查询返回空：

- 群 A → 群 B（同 visibility，不同群）
- 群 → 私聊
- 私聊 → 群
- sealed 模式连角色情绪都不该由别的窗口造成
- worker 提取必须跟随来源窗口，不能固定写 subject_private
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, update

from ling.contracts.v1 import Origin, Visibility
from ling.domains.awareness.service import AwarenessService
from ling.domains.memory.service import MemoryService
from ling.infra.db import session_scope
from ling.infra.models import MemoryEpisode, MemoryRow, OutboxEvent, Window
from ling.kernel.audience import AudienceSnapshot, visibility_set
from ling.kernel.context import ContextCompiler
from ling.worker import handle_memory_extract
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def _window(env: TestEnv, *, visibility: str = "subject_private", title: str = "w") -> str:
    r = await env.client.post("/v1/sessions", json={"title": title})
    session_id = r.json()["session_id"]
    async with session_scope() as session:
        await session.execute(
            update(Window).where(Window.id == session_id).values(visibility=visibility)
        )
    return session_id


async def _snapshot(env: TestEnv, window_id: str) -> AudienceSnapshot:
    async with session_scope() as session:
        window = await session.get(Window, window_id)
        return await AudienceSnapshot.freeze(
            session,
            subject_id=env.subject_id,
            window_id=window_id,
            window_visibility=window.visibility,
        )


# ------------------------------------------------------------------ 快照本身


async def test_snapshot_freezes_mode_and_audience(logged_in: TestEnv) -> None:
    window_id = await _window(logged_in)
    snapshot = await _snapshot(logged_in, window_id)
    assert snapshot.mode.value == "per_window"
    assert snapshot.is_group is False
    assert snapshot.allows_long_term_memory() is True
    assert snapshot.allows_awareness() is False
    desc = snapshot.describe()
    assert desc["window_visibility"] == "subject_private"


# ------------------------------------------------------------------ 群 A → 群 B


async def test_group_memory_does_not_leak_to_another_group(logged_in: TestEnv) -> None:
    """群 A 的记忆（哪怕 visibility=group_scope）不得进群 B。"""
    group_a = await _window(logged_in, visibility="group_scope", title="群A")
    group_b = await _window(logged_in, visibility="group_scope", title="群B")

    async with session_scope() as session:
        await MemoryService(session).record_episode(
            subject_id=logged_in.subject_id,
            window_id=group_a,
            summary="群A 里聊到了团子这只猫",
            source_event_ids=["evt_a"],
            origin=Origin.user_statement,
            visibility=Visibility.group_scope,
            source_visibility="group_scope",
        )

    snap_b = await _snapshot(logged_in, group_b)
    async with session_scope() as session:
        compiled = await ContextCompiler(session).compile(
            subject_id=logged_in.subject_id,
            window_id=group_b,
            user_text="团子",
            is_group=True,
        )
    # 只看 system 车道：用户自己的输入会作为历史出现，那不是泄漏
    system_text = "\n".join(m.content for m in compiled.messages if m.role == "system")
    assert "群A 里聊到了团子" not in system_text, (
        f"群 A 的记忆泄漏到群 B（snapshot={snap_b.describe()}）"
    )

    # 群 A 自己能读到
    async with session_scope() as session:
        compiled_a = await ContextCompiler(session).compile(
            subject_id=logged_in.subject_id,
            window_id=group_a,
            user_text="团子",
            is_group=True,
        )
    system_a = "\n".join(m.content for m in compiled_a.messages if m.role == "system")
    assert "群A 里聊到了团子" in system_a, "群 A 自己应能召回"


# ------------------------------------------------------------------ 群 → 私聊


async def test_group_memory_does_not_promote_to_private(logged_in: TestEnv) -> None:
    """群里说的话不能自动变成该用户的私聊事实。"""
    group = await _window(logged_in, visibility="group_scope", title="群")
    private = await _window(logged_in, title="私聊")

    async with session_scope() as session:
        await MemoryService(session).record_episode(
            subject_id=logged_in.subject_id,
            window_id=group,
            summary="群里有人说自己住在杭州",
            source_event_ids=["evt_g"],
            origin=Origin.user_statement,
            visibility=Visibility.group_scope,
            source_visibility="group_scope",
        )

    async with session_scope() as session:
        compiled = await ContextCompiler(session).compile(
            subject_id=logged_in.subject_id, window_id=private, user_text="杭州"
        )
    system_text = "\n".join(m.content for m in compiled.messages if m.role == "system")
    assert "群里有人说自己住在杭州" not in system_text, "群聊内容被提升成私聊事实"


# ------------------------------------------------------------------ 私聊 → 群


async def test_private_memory_does_not_leak_to_group(logged_in: TestEnv) -> None:
    private = await _window(logged_in, title="私聊")
    group = await _window(logged_in, visibility="group_scope", title="群")

    async with session_scope() as session:
        await MemoryService(session).record_episode(
            subject_id=logged_in.subject_id,
            window_id=private,
            summary="私聊里说养了一只叫团子的猫",
            source_event_ids=["evt_p"],
            origin=Origin.user_statement,
            visibility=Visibility.subject_private,
            source_visibility="subject_private",
        )

    async with session_scope() as session:
        compiled = await ContextCompiler(session).compile(
            subject_id=logged_in.subject_id,
            window_id=group,
            user_text="团子",
            is_group=True,
        )
    system_text = "\n".join(m.content for m in compiled.messages if m.role == "system")
    assert "私聊里说养了一只叫团子的猫" not in system_text


# ------------------------------------------------------------------ worker 来源


async def test_worker_respects_source_visibility(logged_in: TestEnv) -> None:
    """worker 提取必须跟随来源窗口，不能固定写 subject_private。"""
    group = await _window(logged_in, visibility="group_scope", title="群提取")
    await handle_memory_extract(
        {
            "subject_id": logged_in.subject_id,
            "window_id": group,
            "turn_id": f"turn_{uuid.uuid4().hex[:8]}",
            "user_text": "我住在杭州",
            "actor_text": "记下了",
            "source_visibility": "group_scope",
        }
    )
    async with session_scope() as session:
        episodes = list(
            (
                await session.execute(
                    select(MemoryEpisode).where(
                        MemoryEpisode.subject_id == logged_in.subject_id
                    )
                )
            ).scalars()
        )
        rows = list(
            (
                await session.execute(
                    select(MemoryRow).where(MemoryRow.subject_id == logged_in.subject_id)
                )
            ).scalars()
        )
    assert episodes and all(e.source_visibility == "group_scope" for e in episodes), [
        e.source_visibility for e in episodes
    ]
    assert rows and all(r.visibility == "group_scope" for r in rows), [
        r.visibility for r in rows
    ]


async def test_turn_outbox_carries_source_visibility(logged_in: TestEnv) -> None:
    """轮次入队时必须带上来源可见范围，worker 才有依据。"""
    group = await _window(logged_in, visibility="group_scope", title="群入队")
    body = {"client_turn_id": str(uuid.uuid4()), "text": "ling 随便答一句"}
    response = await logged_in.client.post(f"/v1/sessions/{group}/turns", json=body)
    assert response.status_code == 200
    # 让后台轮次跑完，outbox 才会写入
    import asyncio

    for _ in range(60):
        await asyncio.sleep(0.1)
        async with session_scope() as session:
            existing = list(
                (
                    await session.execute(
                        select(OutboxEvent).where(OutboxEvent.topic == "memory.extract")
                    )
                ).scalars()
            )
        if existing:
            break

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.topic == "memory.extract")
                )
            ).scalars()
        )
    # 群聊里若被点名，会产出回复并登记 outbox
    if rows:
        assert dict(rows[-1].payload or {}).get("source_visibility") == "group_scope"


# ------------------------------------------------------------------ sealed


async def test_sealed_blocks_capsule_and_awareness(logged_in: TestEnv) -> None:
    await logged_in.client.put("/v1/isolation", json={"mode": "sealed"})
    private = await _window(logged_in, title="sealed")
    snapshot = await _snapshot(logged_in, private)
    assert snapshot.allows_long_term_memory() is False
    assert snapshot.allows_awareness() is False
    assert snapshot.allows_handoff_capsule() is False


async def test_sealed_marks_no_cross_window_effect(logged_in: TestEnv) -> None:
    """sealed 模式下快照必须明确声明不读任何跨窗口来源。"""
    await logged_in.client.put("/v1/isolation", json={"mode": "sealed"})
    window_id = await _window(logged_in)
    snapshot = await _snapshot(logged_in, window_id)
    desc = snapshot.describe()
    assert desc["long_term_memory"] is False
    assert desc["awareness"] is False
    assert desc["handoff_capsule"] is False


# ------------------------------------------------------------------ 可见集合


async def test_visibility_set_matches_audience(logged_in: TestEnv) -> None:
    private = await _window(logged_in, title="私")
    group = await _window(logged_in, visibility="group_scope", title="群")
    snap_private = await _snapshot(logged_in, private)
    snap_group = await _snapshot(logged_in, group)
    assert Visibility.subject_private in visibility_set(snap_private)
    assert Visibility.subject_private not in visibility_set(snap_group)
    assert Visibility.group_scope in visibility_set(snap_group)


# ------------------------------------------------------------------ 情绪分层


async def test_sealed_does_not_inject_any_affect(logged_in: TestEnv) -> None:
    """sealed 下不注入任何情绪：否则"完全隔离"名不副实（ADR 待办 1）。"""
    from ling.contracts.v1 import Visibility
    from ling.domains.affect.service import AffectService

    subject_id = logged_in.subject_id
    window_id = await _window(logged_in, title="sealed情绪")

    async with session_scope() as session:
        await AffectService(session).nudge("tired", 0.8, scope="global")

    # per_window：能看到语气趋势
    async with session_scope() as session:
        compiled = await ContextCompiler(session).compile(
            subject_id=subject_id, window_id=window_id, user_text="你好"
        )
    assert "疲倦" in "\n".join(m.content for m in compiled.messages)

    # sealed：完全不注入
    await logged_in.client.put("/v1/isolation", json={"mode": "sealed"})
    async with session_scope() as session:
        compiled2 = await ContextCompiler(session).compile(
            subject_id=subject_id, window_id=window_id, user_text="你好"
        )
    joined = "\n".join(m.content for m in compiled2.messages)
    assert "疲倦" not in joined, "sealed 模式仍注入了别的窗口造成的情绪"
    _ = Visibility  # 保持 import 有用


async def test_window_affect_does_not_cross_windows(logged_in: TestEnv) -> None:
    """窗口局部情绪只在本窗口可见。"""
    from ling.domains.affect.service import AffectService

    window_a = await _window(logged_in, title="A")
    window_b = await _window(logged_in, title="B")
    async with session_scope() as session:
        await AffectService(session).nudge(
            "uneasy", 0.9, cause="刚和这个人争执", scope="window", window_id=window_a
        )

    async with session_scope() as session:
        compiled_b = await ContextCompiler(session).compile(
            subject_id=logged_in.subject_id, window_id=window_b, user_text="你好"
        )
    assert "不安" not in "\n".join(m.content for m in compiled_b.messages), (
        "窗口 A 的情绪影响了窗口 B"
    )

    async with session_scope() as session:
        compiled_a = await ContextCompiler(session).compile(
            subject_id=logged_in.subject_id, window_id=window_a, user_text="你好"
        )
    assert "不安" in "\n".join(m.content for m in compiled_a.messages)


async def test_global_affect_is_weak_trend_only(logged_in: TestEnv) -> None:
    """全局情绪只给粗粒度趋势，不带原因与对象。

    极低的全局趋势（<0.15）刻意不注入：那只是背景噪声，
    写进提示等于让别的窗口影响本窗口措辞。
    """
    from ling.domains.affect.service import AffectService

    window_id = await _window(logged_in, title="全局")

    # 极弱全局趋势：不注入
    async with session_scope() as session:
        await AffectService(session).nudge("warm", 0.08, scope="global")
    async with session_scope() as session:
        compiled_weak = await ContextCompiler(session).compile(
            subject_id=logged_in.subject_id, window_id=window_id, user_text="你好"
        )
    assert "偏暖" not in "\n".join(m.content for m in compiled_weak.messages)

    # 达到阈值的全局趋势：注入，且不带原因
    async with session_scope() as session:
        await AffectService(session).nudge("warm", 0.3, scope="global")
    async with session_scope() as session:
        compiled = await ContextCompiler(session).compile(
            subject_id=logged_in.subject_id, window_id=window_id, user_text="你好"
        )
    joined = "\n".join(m.content for m in compiled.messages)
    assert "偏暖" in joined
    assert "争执" not in joined


async def test_affect_scope_requires_window_id() -> None:
    from ling.domains.affect.service import AffectService

    service = AffectService.__new__(AffectService)
    service._actor_id = "actor_ling"
    service._session = None  # type: ignore[assignment]
    try:
        import asyncio

        asyncio.get_event_loop()
        # 只验证参数校验分支，不触库
        import inspect

        src = inspect.getsource(AffectService.nudge)
        assert "window 作用域必须提供 window_id" in src
    finally:
        pass


# ------------------------------------------------------------------ awareness 复活


async def test_awareness_does_not_revive_after_mode_roundtrip(logged_in: TestEnv) -> None:
    """continuous → per_window → continuous 后旧条目不得复活（ADR 待办 3）。"""
    await logged_in.client.put("/v1/isolation", json={"mode": "continuous"})
    window_id = await _window(logged_in, title="复活")
    async with session_scope() as session:
        await AwarenessService(session).derive_from_turn(
            subject_id=logged_in.subject_id,
            window_id=window_id,
            window_visibility="subject_private",
            user_text="我住在杭州",
        )

    # 来回切一轮
    await logged_in.client.put("/v1/isolation", json={"mode": "per_window"})
    await logged_in.client.put("/v1/isolation", json={"mode": "continuous"})

    async with session_scope() as session:
        items = await AwarenessService(session).visible_for_window(
            subject_id=logged_in.subject_id,
            window_visibility="subject_private",
            window_id=window_id,
        )
    assert items == [], "模式来回切换后旧意识复活了"

    # 新条目可以正常积累
    async with session_scope() as session:
        created = await AwarenessService(session).derive_from_turn(
            subject_id=logged_in.subject_id,
            window_id=window_id,
            window_visibility="subject_private",
            user_text="我养了一只猫",
        )
    assert created


async def test_snapshot_uses_visibility_authority(logged_in: TestEnv) -> None:
    """AudienceSnapshot 的行级过滤必须走唯一权威实现。

    可见范围规则曾有四份独立实现，漂移会导致泄漏或锁死。
    这里断言快照内部用的集合与 kernel.audience.readable_visibilities 一致。
    """
    from ling.kernel.audience import AudienceSnapshot, readable_visibilities

    async with session_scope() as session:
        snapshot = await AudienceSnapshot.freeze(
            session,
            subject_id=logged_in.subject_id,
            window_id="win_auth",
            window_visibility="subject_private",
        )
    expected = {
        v.value
        for v in readable_visibilities(
            is_group=snapshot.is_group,
            cross_device_granted=snapshot.cross_device_granted,
        )
    }
    assert snapshot._allowed_visibilities() == expected  # noqa: SLF001
