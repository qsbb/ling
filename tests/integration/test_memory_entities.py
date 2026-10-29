"""实体链接：沿关系定位，而不只靠文本命中。

参照 MaiBot A-Memorix 的图谱关系召回与 Mem0 的 entity linking。

**核心价值场景**：用户问「我妈提过的那个人后来怎样」——
字面可能与记忆毫无重叠，但沿实体关系可以找到相关记忆。

**别名归并是前提**：没有它，"妈 / 母亲 / 老妈"会被当成三个对象，
换个叫法仍然串不起来。
"""

from __future__ import annotations

import pytest

from ling.contracts.v1 import MemoryProposal, Origin, Visibility
from ling.domains.memory.entities import EntityService, canonical_name
from ling.domains.memory.service import MemoryService
from ling.infra.db import session_scope
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


# ------------------------------------------------------------------ 别名归并


def test_kinship_aliases_collapse() -> None:
    assert canonical_name("母亲") == canonical_name("妈") == canonical_name("老妈")
    assert canonical_name("父亲") == canonical_name("爸")


def test_unrelated_names_stay_distinct() -> None:
    """宁可少归并，也不能把两个不同的人合成一个。"""
    assert canonical_name("阿澄") != canonical_name("阿明")


# ------------------------------------------------------------------ 写入与关联


async def test_upsert_dedupes_by_canonical_name(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        service = EntityService(session)
        a = await service.upsert(subject_id=logged_in.subject_id, name="母亲", kind="person")
        b = await service.upsert(subject_id=logged_in.subject_id, name="老妈", kind="person")
    assert a.id == b.id, "同义别名没有归并到同一实体"


async def test_aliases_accumulate(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        service = EntityService(session)
        await service.upsert(subject_id=logged_in.subject_id, name="妈", kind="person")
        row = await service.upsert(
            subject_id=logged_in.subject_id, name="老妈", kind="person", aliases=["母亲"]
        )
    assert canonical_name("母亲") in {canonical_name(a) for a in row.aliases}


async def test_resolve_matches_alias_in_text(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        service = EntityService(session)
        entity = await service.upsert(
            subject_id=logged_in.subject_id, name="妈", kind="person", aliases=["母亲"]
        )
    async with session_scope() as session:
        found = await EntityService(session).resolve(
            subject_id=logged_in.subject_id, text="我妈提过的那个人"
        )
    assert any(f.id == entity.id for f in found), "别名没有在文本里被识别"


async def test_resolve_returns_empty_for_unrelated(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        await EntityService(session).upsert(
            subject_id=logged_in.subject_id, name="豆豆", kind="pet"
        )
    async with session_scope() as session:
        found = await EntityService(session).resolve(
            subject_id=logged_in.subject_id, text="今天天气不错"
        )
    assert found == []


async def test_entities_are_subject_scoped(logged_in: TestEnv) -> None:
    """实体不能跨主体串（否则又一次数据归属事故）。"""
    from ling.domains.identity.service import IdentityService

    async with session_scope() as session:
        other = await IdentityService(session).create_user(
            username="ent_other", password="pw-not-used"
        )
        other_subject = other.subject_id
    async with session_scope() as session:
        await EntityService(session).upsert(
            subject_id=logged_in.subject_id, name="妈", kind="person"
        )
    async with session_scope() as session:
        found = await EntityService(session).resolve(
            subject_id=other_subject, text="我妈"
        )
    assert found == [], "实体跨主体泄漏"


# ------------------------------------------------------------------ 关联与召回


async def test_link_and_related_ids(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        entity = await EntityService(session).upsert(
            subject_id=logged_in.subject_id, name="豆豆", kind="pet"
        )
        memory_id = await MemoryService(session).propose(
            MemoryProposal(
                proposal_id="en1", kind="assertion", text="用户养了豆豆",
                origin=Origin.user_statement, source_event_ids=["e"],
                visibility=Visibility.subject_private, confidence=0.9,
            ),
            subject_id=logged_in.subject_id, turn_id="enT1", slot="has_pet", value="豆豆",
        )
        await EntityService(session).link(memory_id=memory_id or "", entity_id=entity.id)
        related = await EntityService(session).related_memory_ids([entity.id])
    assert memory_id in related


async def test_link_is_idempotent(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        entity = await EntityService(session).upsert(
            subject_id=logged_in.subject_id, name="甲", kind="other"
        )
        service = EntityService(session)
        assert await service.link(memory_id="m1", entity_id=entity.id) is True
        assert await service.link(memory_id="m1", entity_id=entity.id) is False


async def test_entity_path_respects_visibility(logged_in: TestEnv) -> None:
    """实体关联不能绕过权限过滤：只在已授权候选里标命中。"""

    async with session_scope() as session:
        entity = await EntityService(session).upsert(
            subject_id=logged_in.subject_id, name="秘密项目", kind="project"
        )
        memory_id = await MemoryService(session).propose(
            MemoryProposal(
                proposal_id="en2", kind="assertion", text="用户在做秘密项目",
                origin=Origin.user_statement, source_event_ids=["e"],
                visibility=Visibility.actor_private,  # 私密
                confidence=0.9,
            ),
            subject_id=logged_in.subject_id, turn_id="enT2",
            slot="ongoing_project", value="秘密项目",
        )
        await EntityService(session).link(memory_id=memory_id or "", entity_id=entity.id)

    # 只用 subject_private 权限召回：不该看到 actor_private 的内容
    async with session_scope() as session:
        result = await MemoryService(session).recall(
            subject_id=logged_in.subject_id,
            query="秘密项目",
            visibility_allow={Visibility.subject_private},
            limit=5,
        )
    assert all(h.memory_id != memory_id for h in result.hits), (
        "实体关联绕过了权限过滤"
    )


async def test_entity_path_rescues_zero_literal_hit(logged_in: TestEnv) -> None:
    """实体路必须能救回**零字面重叠**的记忆。

    这是它存在的唯一理由。原实现先按 `bm25 > 0` 定候选、再算实体命中，
    于是"我妈提过的那个人"与记忆字面零重叠 → 早被滤掉 → 实体关联无处加分。
    此前实现的问题这一点，注释里写的"沿实体找回零字面重叠内容"当时并不成立。
    """
    from ling.contracts.v1 import MemoryProposal, Origin, Visibility
    from ling.domains.memory.service import MemoryService
    from ling.infra.db import session_scope

    async with session_scope() as session:
        entity = await EntityService(session).upsert(
            subject_id=logged_in.subject_id, name="妈", kind="person", aliases=["母亲"]
        )
        # 记忆文本刻意不含"我妈/母亲"字样，只通过实体关联指向"妈"
        memory_id = await MemoryService(session).propose(
            MemoryProposal(
                proposal_id="rescue1",
                kind="assertion",
                text="那个人后来搬去了外地工作",
                origin=Origin.user_statement,
                source_event_ids=["e"],
                visibility=Visibility.subject_private,
                confidence=0.9,
            ),
            subject_id=logged_in.subject_id,
            turn_id="rescueT1",
            slot="family",
            value="妈",
        )
        await EntityService(session).link(
            memory_id=memory_id or "", entity_id=entity.id
        )

    async with session_scope() as session:
        # 无 embedding（走 BM25 路径），查询与记忆字面零重叠
        result = await MemoryService(session, embedder=None, reranker=None).recall(
            subject_id=logged_in.subject_id,
            query="我妈提过的那个人",
            visibility_allow={Visibility.subject_private},
            limit=5,
        )
    assert any(h.memory_id == memory_id for h in result.hits), (
        f"实体路没有救回零字面命中的记忆: {[h.text for h in result.hits]}"
    )
