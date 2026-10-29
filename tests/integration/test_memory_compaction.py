"""压缩级联：把同槽位的多条低价值记忆并成一条高层摘要。

参照实现：「低价值、长期未访问的记忆可在睡眠维护中压缩或归档」+
「将即将衰退的一组记忆压缩为更高层摘要」。

不压缩的后果很具体：用几个月后证据车道被一堆「用户喜欢过 X」的碎片占满，
真正重要的事实挤不进去。

关键约束：
- 只处理**有 value** 的行（没值就无法可靠合并，不猜）
- 合并后原行**归档并指向摘要**（保留 superseded 链，不物理删除）
- 高置信/高访问/近期的不参与衰减
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from ling.contracts.v1 import MemoryProposal, Origin, Visibility
from ling.domains.memory.service import MemoryService
from ling.infra.db import session_scope
from ling.infra.models import MemoryRow
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def _seed(
    env: TestEnv,
    *,
    slot: str,
    value: str,
    days_old: int,
    confidence: float = 0.6,
    access: int = 0,
) -> str:
    async with session_scope() as session:
        service = MemoryService(session)
        # 用 propose 走正常写入路径，保证 value/slot 都正确落库
        text = f"{value}"
        memory_id = await service.propose(
            MemoryProposal(
                proposal_id=f"p_{slot}_{value}",
                kind="assertion",
                text=text,
                origin=Origin.user_statement,
                source_event_ids=["evt"],
                visibility=Visibility.subject_private,
                confidence=confidence,
            ),
            subject_id=env.subject_id,
            turn_id=f"t_{slot}_{value}",
            slot=slot,
            value=value,
        )
    async with session_scope() as session:
        await session.execute(
            update(MemoryRow)
            .where(MemoryRow.id == memory_id)
            .values(
                created_at=datetime.now(UTC) - timedelta(days=days_old),
                access_count=access,
            )
        )
    return memory_id or ""


async def _rows(env: TestEnv) -> list[MemoryRow]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(MemoryRow).where(MemoryRow.subject_id == env.subject_id)
                )
            ).scalars()
        )


# ------------------------------------------------------------------ 重要性


async def test_importance_prefers_high_confidence_and_access(logged_in: TestEnv) -> None:
    low = await _seed(logged_in, slot="likes", value="喝茶", days_old=10, confidence=0.3)
    high = await _seed(
        logged_in, slot="likes", value="咖啡", days_old=10, confidence=0.9, access=20
    )
    async with session_scope() as session:
        service = MemoryService(session)
        now = datetime.now(UTC)
        a = await session.get(MemoryRow, low)
        b = await session.get(MemoryRow, high)
        assert service.importance(a, now=now) < service.importance(b, now=now)


async def test_importance_decays_with_age(logged_in: TestEnv) -> None:
    fresh = await _seed(logged_in, slot="likes", value="新", days_old=1)
    old = await _seed(logged_in, slot="likes", value="旧", days_old=300)
    async with session_scope() as session:
        service = MemoryService(session)
        now = datetime.now(UTC)
        a = await session.get(MemoryRow, fresh)
        b = await session.get(MemoryRow, old)
        assert service.importance(a, now=now) > service.importance(b, now=now)


# ------------------------------------------------------------------ 压缩


async def test_compaction_merges_low_value_group(logged_in: TestEnv) -> None:
    ids = [
        await _seed(logged_in, slot="likes", value=v, days_old=90, confidence=0.3)
        for v in ("喝茶", "咖啡", "散步")
    ]
    async with session_scope() as session:
        result = await MemoryService(session).compact_low_value()
    assert result["groups"] >= 1
    assert result["archived"] >= 3

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(MemoryRow).where(MemoryRow.subject_id == logged_in.subject_id)
                )
            ).scalars()
        )
    # 原行被归档并指向摘要
    originals = [r for r in rows if r.id in ids]
    assert all(r.archived_at is not None for r in originals)
    assert all(r.superseded_by for r in originals)
    # 摘要存在且含全部值
    summaries = [r for r in rows if r.id not in ids and r.archived_at is None]
    assert summaries, "没有生成摘要"
    text = summaries[0].text
    assert "喝茶" in text and "咖啡" in text and "散步" in text


async def test_compaction_preserves_supersede_chain(logged_in: TestEnv) -> None:
    """原行不能物理删除：superseded 链断掉就无法审计"这条从哪来"。"""
    ids = [
        await _seed(logged_in, slot="likes", value=v, days_old=90, confidence=0.3)
        for v in ("甲", "乙", "丙")
    ]
    async with session_scope() as session:
        await MemoryService(session).compact_low_value()
    rows = await _rows(logged_in)
    originals = [r for r in rows if r.id in ids]
    assert len(originals) == 3, "原行被物理删除了"
    summary_id = originals[0].superseded_by
    assert summary_id and any(r.id == summary_id for r in rows)


async def test_compaction_skips_recent(logged_in: TestEnv) -> None:
    """近期记忆不参与衰减（参照实现：近期默认更不易衰减）。"""
    ids = [
        await _seed(logged_in, slot="likes", value=v, days_old=2, confidence=0.3)
        for v in ("甲", "乙", "丙")
    ]
    async with session_scope() as session:
        await MemoryService(session).compact_low_value()
    rows = await _rows(logged_in)
    assert all(r.archived_at is None for r in rows if r.id in ids)


async def test_compaction_skips_high_importance(logged_in: TestEnv) -> None:
    """高访问频率/高置信的记忆不该被压缩掉。"""
    ids = [
        await _seed(
            logged_in, slot="likes", value=v, days_old=90, confidence=0.95, access=50
        )
        for v in ("甲", "乙", "丙")
    ]
    async with session_scope() as session:
        await MemoryService(session).compact_low_value()
    rows = await _rows(logged_in)
    assert all(r.archived_at is None for r in rows if r.id in ids)


async def test_compaction_requires_min_group(logged_in: TestEnv) -> None:
    """少于 3 条不合并——并了反而丢信息。"""
    await _seed(logged_in, slot="likes", value="甲", days_old=90, confidence=0.3)
    await _seed(logged_in, slot="likes", value="乙", days_old=90, confidence=0.3)
    async with session_scope() as session:
        result = await MemoryService(session).compact_low_value()
    assert result["groups"] == 0


async def test_compaction_skips_rows_without_value(logged_in: TestEnv) -> None:
    """没有 value 的行不猜（参照 LivingMemory 的教训：不凭摘要补造证据）。"""
    async with session_scope() as session:
        for i in range(4):
            session.add(
                MemoryRow(
                    id=f"mem_noval_{i}",
                    subject_id=logged_in.subject_id,
                    kind="assertion",
                    text=f"以前的记忆 {i}",
                    origin=Origin.user_statement.value,
                    visibility=Visibility.subject_private.value,
                    confidence=0.3,
                    source_event_ids=["e"],
                    source_visibility="subject_private",
                    slot="likes",
                    value=None,
                    created_at=datetime.now(UTC) - timedelta(days=90),
                )
            )
    async with session_scope() as session:
        result = await MemoryService(session).compact_low_value()
    assert result["groups"] == 0, "没有 value 的行被误合并"


async def test_compaction_skips_retracted(logged_in: TestEnv) -> None:
    """已撤回的不参与压缩（撤回是更强的失效）。"""
    ids = [
        await _seed(logged_in, slot="likes", value=v, days_old=90, confidence=0.3)
        for v in ("甲", "乙", "丙")
    ]
    async with session_scope() as session:
        await MemoryService(session).retract(ids[0], reason="用户纠正")
    async with session_scope() as session:
        result = await MemoryService(session).compact_low_value()
    rows = await _rows(logged_in)
    retracted = next(r for r in rows if r.id == ids[0])
    assert retracted.archived_at is None
    assert result["groups"] == 0, "含撤回行的组不该被合并"


async def test_compaction_runs_in_maintenance(logged_in: TestEnv) -> None:
    """维护任务必须真的调用压缩，否则逻辑是死的。"""
    from ling.worker import handle_memory_archive

    for v in ("甲", "乙", "丙"):
        await _seed(logged_in, slot="likes", value=v, days_old=90, confidence=0.3)
    await handle_memory_archive({})
    rows = await _rows(logged_in)
    assert any(r.archive_reason and "compacted" in r.archive_reason for r in rows), (
        "维护任务没有执行压缩级联"
    )


async def test_compaction_preserves_origin(logged_in: TestEnv) -> None:
    """合并摘要必须沿用组内真实来源，不能一律标成 user_statement。

    混来源合并会**伪造证据等级**：把"外部观察"和"用户亲口说"并成一条
    标成用户陈述，等于给推断出来的信息盖了用户的章。
    """
    async with session_scope() as session:
        service = MemoryService(session)
        for i, v in enumerate(("甲", "乙", "丙")):
            await service.propose(
                MemoryProposal(
                    proposal_id=f"ext{i}", kind="assertion", text=f"用户喜欢{v}",
                    origin=Origin.external_observation, source_event_ids=["e"],
                    visibility=Visibility.subject_private, confidence=0.3,
                ),
                subject_id=logged_in.subject_id, turn_id=f"extT{i}",
                slot="likes", value=v,
            )
    async with session_scope() as session:
        await session.execute(
            update(MemoryRow)
            .where(MemoryRow.origin == Origin.external_observation.value)
            .values(created_at=datetime.now(UTC) - timedelta(days=90))
        )
    async with session_scope() as session:
        await MemoryService(session).compact_low_value()
    rows = await _rows(logged_in)
    summaries = [r for r in rows if r.value is None and r.archived_at is None]
    assert summaries, "没有生成摘要"
    assert summaries[0].origin == Origin.external_observation.value, (
        f"摘要来源被改成 {summaries[0].origin}，伪造了证据等级"
    )


async def test_compaction_does_not_mix_origins(logged_in: TestEnv) -> None:
    """不同来源的同槽位记忆不该合并到一起。"""
    async with session_scope() as session:
        service = MemoryService(session)
        for i, (v, origin) in enumerate([
            ("甲", Origin.user_statement), ("乙", Origin.user_statement),
            ("丙", Origin.external_observation), ("丁", Origin.external_observation),
        ]):
            await service.propose(
                MemoryProposal(
                    proposal_id=f"mix{i}", kind="assertion", text=f"用户喜欢{v}",
                    origin=origin, source_event_ids=["e"],
                    visibility=Visibility.subject_private, confidence=0.3,
                ),
                subject_id=logged_in.subject_id, turn_id=f"mixT{i}",
                slot="likes", value=v,
            )
    async with session_scope() as session:
        await session.execute(
            update(MemoryRow).values(created_at=datetime.now(UTC) - timedelta(days=90))
        )
    async with session_scope() as session:
        result = await MemoryService(session).compact_low_value()
    # 每种来源只有 2 条 < min_group=3，都不该合并
    assert result["groups"] == 0, "不同来源被混在一起合并了"


# ------------------------------------------------------------------ 隐私边界（严重）


async def test_compaction_never_mixes_subjects(logged_in: TestEnv) -> None:
    """压缩**绝不能**跨用户合并。

    这是隐私事故而不是"记得差一点"：把 B 的偏好并进 A 的摘要，
    A 的记忆列表里就会出现 B 的隐私。
    用「Alice 1 条 + Bob 2 条」复现过。
    """
    from ling.domains.identity.service import IdentityService

    async with session_scope() as session:
        other = await IdentityService(session).create_user(
            username="bob_compact", password="pw-not-used"
        )
        bob = other.subject_id

    # Alice 1 条、Bob 2 条，同槽位同来源、都很旧很低价值
    await _seed(logged_in, slot="likes", value="甲", days_old=90, confidence=0.3)
    async with session_scope() as session:
        service = MemoryService(session)
        for v in ("乙", "丙"):
            await service.propose(
                MemoryProposal(
                    proposal_id=f"bob_{v}", kind="assertion", text=f"用户喜欢{v}",
                    origin=Origin.user_statement, source_event_ids=["e"],
                    visibility=Visibility.subject_private, confidence=0.3,
                ),
                subject_id=bob, turn_id=f"bobT_{v}", slot="likes", value=v,
            )
    async with session_scope() as session:
        await session.execute(
            update(MemoryRow).values(created_at=datetime.now(UTC) - timedelta(days=90))
        )

    async with session_scope() as session:
        await MemoryService(session).compact_low_value()

    async with session_scope() as session:
        alice_rows = list(
            (
                await session.execute(
                    select(MemoryRow).where(MemoryRow.subject_id == logged_in.subject_id)
                )
            ).scalars()
        )
    # Alice 侧不该出现 Bob 的值，也不该因为凑够 2 条就合并出摘要
    for row in alice_rows:
        text = row.text or ""
        assert "乙" not in text and "丙" not in text, (
            f"Bob 的记忆进了 Alice 的档案: {text}"
        )
    # Alice 只有 1 条 → 达不到 min_group，不该产生摘要
    assert not [r for r in alice_rows if r.value is None], "跨用户合并生成了摘要"


async def test_compaction_never_mixes_visibility(logged_in: TestEnv) -> None:
    """不同可见范围也不能混合并（群内可见与私聊可见是不同受众）。"""
    from ling.infra.models import Window  # noqa: F401

    async with session_scope() as session:
        service = MemoryService(session)
        for i, (v, vis) in enumerate([
            ("甲", "subject_private"), ("乙", "subject_private"),
            ("丙", "group_scope"), ("丁", "group_scope"), ("戊", "group_scope"),
        ]):
            await service.propose(
                MemoryProposal(
                    proposal_id=f"vis{i}", kind="assertion", text=f"用户喜欢{v}",
                    origin=Origin.user_statement, source_event_ids=["e"],
                    visibility=Visibility(vis), confidence=0.3,
                ),
                subject_id=logged_in.subject_id, turn_id=f"visT{i}",
                slot="likes", value=v,
            )
    async with session_scope() as session:
        await session.execute(
            update(MemoryRow).values(created_at=datetime.now(UTC) - timedelta(days=90))
        )
    async with session_scope() as session:
        await MemoryService(session).compact_low_value()
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(MemoryRow).where(MemoryRow.subject_id == logged_in.subject_id)
                )
            ).scalars()
        )
    summaries = [r for r in rows if r.value is None and r.archived_at is None]
    # 同为 group_scope 的三条（丙丁戊）合并是**正确**的；
    # 要拦的是把私聊值（甲乙）和群值混进同一条摘要。
    for s in summaries:
        text = s.text or ""
        has_private = "甲" in text or "乙" in text
        has_group = "丙" in text or "丁" in text or "戊" in text
        assert not (has_private and has_group), f"不同可见范围被混合并: {text}"


# ------------------------------------------------------------------ 过期事实不能复活


async def test_compaction_skips_expired_facts(logged_in: TestEnv) -> None:
    """已过期（valid_until 已过）的事实不能参与合并。

    合并出的新摘要没有 valid_until，等于把过期事实复活成"现行事实"。
    实测：三条过期的居住地压缩后变成可召回的"用户住在：北京、上海、广州"。
    """
    async with session_scope() as session:
        service = MemoryService(session)
        for i, v in enumerate(("北京", "上海", "广州")):
            await service.propose(
                MemoryProposal(
                    proposal_id=f"exp{i}", kind="assertion", text=f"用户住在{v}",
                    origin=Origin.user_statement, source_event_ids=["e"],
                    visibility=Visibility.subject_private, confidence=0.3,
                ),
                subject_id=logged_in.subject_id, turn_id=f"expT{i}",
                slot="lives_in", value=v,
            )
    async with session_scope() as session:
        await session.execute(
            update(MemoryRow).values(
                created_at=datetime.now(UTC) - timedelta(days=90),
                valid_until=datetime.now(UTC) - timedelta(days=1),  # 已过期
            )
        )
    async with session_scope() as session:
        result = await MemoryService(session).compact_low_value()
    assert result["groups"] == 0, "过期事实被合并复活了"

    # 库里不该出现能召回的"用户住在：北京、上海、广州"
    rows = await _rows(logged_in)
    for r in rows:
        if r.archived_at is None and r.value is None:
            assert "北京、上海" not in (r.text or ""), f"过期事实被复活: {r.text}"


# ------------------------------------------------------------------ 核心记忆与 forgetting


async def test_forget_subject_also_clears_core_memory(logged_in: TestEnv) -> None:
    """"忘记我"必须连核心记忆一起清。

    核心记忆是**每轮常驻**的，不清掉的话用户说了"忘记"之后，
    角色仍会每轮把它念出来——这是最直接的失信。
    """
    from ling.domains.memory.core import CoreMemoryService

    async with session_scope() as session:
        await CoreMemoryService(session).upsert(
            subject_id=logged_in.subject_id, label="隐私", text="他在吃抗抑郁药"
        )
    async with session_scope() as session:
        await MemoryService(session).forget_subject(logged_in.subject_id)
    async with session_scope() as session:
        items = await CoreMemoryService(session).list_active(logged_in.subject_id)
    assert items == [], f"核心记忆未被清除: {[i.text for i in items]}"
