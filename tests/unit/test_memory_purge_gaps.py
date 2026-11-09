"""记忆真删：三个已实测确认缺陷的回归（历史事件 / 失败状态 / 遗漏的表）。

这个文件与 `test_memory_purge.py` 分开写，理由是它钉的是**具体的缺陷**，
不是"这个模块整体对不对"：

1. **历史 outbox 事件**：`outbox_events.subject_id` 是随真删一起新增的列，
   历史行全是 NULL。只按列过滤，它们的 payload 原文留在库里，pending 的
   还会被消费、把内容重新写成记忆。开发库实测：`memory.extract` 407 条 done
   + 24 条 pending，`count(subject_id)` 是 **0**。
2. **失败状态被回滚吃掉**：`purge_subject` 在 except 里写 `failed` 后抛出，
   调用方 `session_scope` 回滚，写入被丢弃——墓碑停在 `running`，"失败可查
   可重试"实际不成立。
3. **遗漏的表**：`media_evidence.description` / `knowledge_items.claim` /
   `prompt_snapshots.payload` / `relationship_events.note` 里都有用户原文或
   可推断出原文的内容，原实现一条都没删。

跑在 sqlite 内存库上：验证的是"哪些行被删、哪些字段被清"，与方言无关。
真实 PostgreSQL 上的并发竞态在 `tests/integration/test_memory_purge_races.py`。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ling.domains.memory.purge import (
    MemoryPurgeError,
    purge_subject,
    request_purge,
)
from ling.infra.db import Base
from ling.infra.models import (
    KnowledgeItem,
    MediaEvidenceRow,
    MemoryPurgeTombstone,
    MemoryRow,
    OutboxEvent,
    PromptSnapshot,
    RelationshipEvent,
    Subject,
    Window,
)

# 一望即知的哨兵串：它出现在任何**保留下来**的地方，用例就直接指出来。
CANARY = "我对花生过敏，千万别给我吃"


@pytest.fixture()
async def session_factory():
    """建好全部表的 sqlite 内存库 + 会话工厂（与主测试文件同构）。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_minimal(factory) -> dict[str, str]:  # noqa: ANN001 - 测试夹具
    """最小主体：一行记忆 + 一个窗口（提示词快照按窗口归属）。"""
    async with factory() as session:
        session.add(Subject(id="subj_gap", display_name="测试用户", forget_generation=0))
        session.add(
            Window(
                id="win_gap",
                actor_id="actor_ling",
                persona_id="persona_ling",
                subject_id="subj_gap",
            )
        )
        session.add(
            MemoryRow(
                id="mem_gap",
                subject_id="subj_gap",
                kind="assertion",
                text=CANARY,
                origin="user_statement",
                visibility="subject_private",
            )
        )
        await session.commit()
    return {"subject_id": "subj_gap", "window_id": "win_gap"}


# ------------------------------------------------- 缺陷 1：历史事件 subject_id 为 NULL


async def test_request_purge_cancels_legacy_event_matched_by_payload(
    session_factory,
) -> None:
    """`subject_id` 列为 NULL、payload 里带主体的历史事件，也必须被取消并脱敏。

    这条是缺陷 1 的**前半**：只按列过滤时，这样的历史 pending 事件一条都
    选不中——它会在删除之后被消费，把 `user_text` 里的原文重新写成记忆。
    """
    ids = await _seed_minimal(session_factory)
    async with session_factory() as session:
        session.add(
            OutboxEvent(
                id="obx_legacy_pending",
                topic="memory.extract",
                state="pending",
                # 关键：列留空，模拟 0060 迁移之前入队的行。
                subject_id=None,
                forget_generation=0,
                payload={
                    "subject_id": ids["subject_id"],
                    "window_id": ids["window_id"],
                    "turn_id": "turn_legacy",
                    "user_text": CANARY,
                    "actor_text": f"她回的：{CANARY}",
                },
            )
        )
        await session.commit()

    async with session_factory() as session:
        request = await request_purge(session, subject_id=ids["subject_id"])
        await session.commit()

    assert request.events_cancelled == 1, (
        "历史事件（subject_id 列为 NULL）没被选中：它会在删除之后被消费，"
        "把 payload 里的原文重新写成记忆"
    )

    async with session_factory() as session:
        event = await session.get(OutboxEvent, "obx_legacy_pending")
        assert event is not None
        assert event.state == "cancelled"
        assert event.payload == {"purged": True}, "payload 里的原文还在库里"
        assert CANARY not in str(event.payload)


async def test_purge_subject_scrubs_legacy_done_event_payload(session_factory) -> None:
    """`done` 状态的历史事件（列 NULL）在清理阶段也必须被脱敏。

    这条是缺陷 1 的**后半**：它们不再被消费，但原文还躺在
    `outbox_events.payload` 里——那同样违背"原文从库里清掉"。
    """
    ids = await _seed_minimal(session_factory)
    async with session_factory() as session:
        session.add(
            OutboxEvent(
                id="obx_legacy_done",
                topic="memory.extract",
                state="done",
                subject_id=None,
                forget_generation=0,
                payload={
                    "subject_id": ids["subject_id"],
                    "user_text": CANARY,
                    "actor_text": f"她回的：{CANARY}",
                },
            )
        )
        await session.commit()

    async with session_factory() as session:
        request = await request_purge(session, subject_id=ids["subject_id"])
        await session.commit()
    async with session_factory() as session:
        result = await purge_subject(session, tombstone_id=request.tombstone_id)
        await session.commit()

    assert result.events_scrubbed == 1

    async with session_factory() as session:
        event = await session.get(OutboxEvent, "obx_legacy_done")
        assert event is not None
        assert event.state == "done", "状态不该被改写：它已经发生过了"
        assert event.payload == {"purged": True}
        assert CANARY not in str(event.payload), "历史 done 事件的原文没被擦掉"


async def test_events_of_other_subjects_are_untouched(session_factory) -> None:
    """按 payload 兜底**不能误伤别人**：删除 A 的记忆不该动 B 的事件。"""
    ids = await _seed_minimal(session_factory)
    async with session_factory() as session:
        session.add(Subject(id="subj_other", display_name="另一个人", forget_generation=0))
        session.add(
            OutboxEvent(
                id="obx_other",
                topic="memory.extract",
                state="pending",
                subject_id=None,
                forget_generation=0,
                payload={"subject_id": "subj_other", "user_text": "别人的原话"},
            )
        )
        await session.commit()

    async with session_factory() as session:
        await request_purge(session, subject_id=ids["subject_id"])
        await session.commit()

    async with session_factory() as session:
        other = await session.get(OutboxEvent, "obx_other")
        assert other is not None
        assert other.state == "pending", "删 A 的记忆把 B 的事件也取消了"
        assert other.payload["user_text"] == "别人的原话"


# ------------------------------------------------- 缺陷 2：失败状态必须落库


async def test_failed_state_survives_the_rollback(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清理 SQL 失败时，墓碑在库里**真的是** `failed`，且 `error_code` 非空。

    原实现把 `tombstone.state = "failed"` 写在当前事务里然后抛异常，而调用方
    `session_scope` 遇到异常会 `rollback()`——这次写入被一起丢掉。结果是墓碑
    停在 `running`，前端永远显示"清理中"，而 `memory.purge.sweep` 虽然会重试
    `running`，但"失败可查"这件事彻底不成立（查不到失败，也看不出失败原因）。

    这里用**新的 session 重读**，而不是复用刚才那个：只有新 session 读到
    `failed`，才证明它真的提交进了库，而不是留在某个未结束的事务里。
    """
    ids = await _seed_minimal(session_factory)
    async with session_factory() as session:
        request = await request_purge(session, subject_id=ids["subject_id"])
        await session.commit()

    def _boom(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("模拟删除失败")

    # 让删除语句炸掉。用 `delete` 而不是更靠后的步骤：要证明的是"删除过程中
    # 途失败"这条路径，而不是"什么都没做就失败"。
    monkeypatch.setattr("ling.domains.memory.purge.delete", _boom, raising=True)

    with pytest.raises(MemoryPurgeError) as excinfo:
        async with session_factory() as session:
            await purge_subject(session, tombstone_id=request.tombstone_id)
            await session.commit()
    assert excinfo.value.code == "purge_failed"

    # 用一个**全新的 session** 重读：证明状态真的提交了。
    async with session_factory() as session:
        tombstone = await session.get(MemoryPurgeTombstone, request.tombstone_id)
        assert tombstone is not None
        assert tombstone.state == "failed", (
            "失败状态被事务回滚吃掉了：墓碑停在 "
            f"{tombstone.state!r}，用户会一直看到「清理中」"
        )
        assert tombstone.error_code, "失败原因码没落库，排查时看不出为什么失败"
        assert "RuntimeError" in tombstone.error_code


async def test_failed_tombstone_is_pickable_by_the_sweeper(session_factory) -> None:
    """`failed` 的墓碑必须能被 `pending_tombstones()` 扫到（否则不会重试）。"""
    from ling.domains.memory.purge import pending_tombstones

    ids = await _seed_minimal(session_factory)
    async with session_factory() as session:
        request = await request_purge(session, subject_id=ids["subject_id"])
        await session.commit()
    async with session_factory() as session:
        tombstone = await session.get(MemoryPurgeTombstone, request.tombstone_id)
        assert tombstone is not None
        tombstone.state = "failed"
        tombstone.error_code = "purge_failed"
        await session.commit()

    async with session_factory() as session:
        assert await pending_tombstones(session) == [request.tombstone_id]


# ------------------------------------------------- 缺陷 3 的补充：跨表覆盖


async def test_media_evidence_descriptions_are_deleted(session_factory) -> None:
    """`media_evidence.description` 是"用户发来那张图里有什么"，必须删。

    只删该用户上传的（`subject_id` 指向他）；她自产素材（`subject_id` 为空）
    是**她的**东西，不属于"这个人的记忆"。
    """
    ids = await _seed_minimal(session_factory)
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(
            MediaEvidenceRow(
                id="media_user",
                actor_id="actor_ling",
                subject_id=ids["subject_id"],
                window_id=ids["window_id"],
                kind="image",
                media_ref="file:uploads/cat.png",
                origin="user_upload",
                description=f"照片里：{CANARY}",
                description_origin="model_inference",
                dedupe_key="dk_user",
                expires_at=now + timedelta(days=7),
            )
        )
        session.add(
            MediaEvidenceRow(
                id="media_actor",
                actor_id="actor_ling",
                # 她自拍的素材：没有上传者
                subject_id=None,
                window_id=ids["window_id"],
                kind="image",
                media_ref="file:uploads/selfie.png",
                origin="model_generated",
                description="她自己的一张自拍",
                dedupe_key="dk_actor",
                expires_at=now + timedelta(days=7),
            )
        )
        await session.commit()

    async with session_factory() as session:
        request = await request_purge(session, subject_id=ids["subject_id"])
        await session.commit()
    async with session_factory() as session:
        await purge_subject(session, tombstone_id=request.tombstone_id)
        await session.commit()

    async with session_factory() as session:
        assert await session.get(MediaEvidenceRow, "media_user") is None, (
            "用户上传图片的描述还在库里——她下一轮就能把它读出来复述"
        )
        kept = await session.get(MediaEvidenceRow, "media_actor")
        assert kept is not None, "把她自己的素材也删了：那不是这个人的记忆"


async def test_private_knowledge_is_deleted_but_public_is_kept(session_factory) -> None:
    """`knowledge_items` 只删 `owner_subject_id == 主体` 的私有条目。

    `source_kind=user_statement` 的 `claim` 就是用户自述（"我住在杭州"），
    属于原文；而 `owner_subject_id IS NULL` 是公共知识（后台录入、来源可核验、
    任何人可读），删它等于因为一个人退出而清空全站知识库。
    """
    ids = await _seed_minimal(session_factory)
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(
            KnowledgeItem(
                id="kn_private",
                topic="居住地",
                claim=f"用户自述：{CANARY}",
                source_kind="user_statement",
                observed_at=now,
                valid_until=now + timedelta(days=30),
                confidence=0.8,
                owner_subject_id=ids["subject_id"],
                visibility="subject_private",
            )
        )
        session.add(
            KnowledgeItem(
                id="kn_public",
                topic="水的沸点",
                claim="标准大气压下 100 摄氏度",
                source_url="https://example.invalid/boiling",
                source_kind="verified_source",
                observed_at=now,
                valid_until=now + timedelta(days=30),
                confidence=0.99,
                owner_subject_id=None,
                visibility="public",
            )
        )
        await session.commit()

    async with session_factory() as session:
        request = await request_purge(session, subject_id=ids["subject_id"])
        await session.commit()
    async with session_factory() as session:
        await purge_subject(session, tombstone_id=request.tombstone_id)
        await session.commit()

    async with session_factory() as session:
        assert await session.get(KnowledgeItem, "kn_private") is None, (
            "用户自述的知识条目还在库里"
        )
        assert await session.get(KnowledgeItem, "kn_public") is not None, (
            "公共知识被误删了：那不属于任何一个人的记忆"
        )


async def test_prompt_snapshots_of_the_subject_windows_are_deleted(
    session_factory,
) -> None:
    """`prompt_snapshots.payload` 是整份提示词，**必然含记忆正文**，必须删。

    这张表没有 subject 列（快照是旁路观测，写的时候只知道窗口），所以按
    `window_id ∈ 该主体的窗口` 删。
    """
    ids = await _seed_minimal(session_factory)
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(Subject(id="subj_other", display_name="另一个人", forget_generation=0))
        session.add(
            Window(
                id="win_other",
                actor_id="actor_ling",
                persona_id="persona_ling",
                subject_id="subj_other",
            )
        )
        session.add(
            PromptSnapshot(
                id="psnap_mine",
                actor_id="actor_ling",
                turn_id="turn_mine",
                window_id=ids["window_id"],
                created_at=now,
                expires_at=now + timedelta(days=7),
                payload={
                    "messages": [
                        {"role": "system", "content": f"你记得：{CANARY}"},
                        {"role": "user", "content": "你记得我过敏吗"},
                    ],
                    "token_total": 42,
                },
            )
        )
        session.add(
            PromptSnapshot(
                id="psnap_other",
                actor_id="actor_ling",
                turn_id="turn_other",
                window_id="win_other",
                created_at=now,
                expires_at=now + timedelta(days=7),
                payload={"messages": [{"role": "user", "content": "别人的话"}]},
            )
        )
        await session.commit()

    async with session_factory() as session:
        request = await request_purge(session, subject_id=ids["subject_id"])
        await session.commit()
    async with session_factory() as session:
        result = await purge_subject(session, tombstone_id=request.tombstone_id)
        await session.commit()

    assert result.details.get("prompt_snapshots_deleted") == 1

    async with session_factory() as session:
        assert await session.get(PromptSnapshot, "psnap_mine") is None, (
            "提示词快照还在库里：它含完整记忆正文，是最完整的一份原文副本"
        )
        assert await session.get(PromptSnapshot, "psnap_other") is not None, (
            "删 A 的快照把 B 的一起删了"
        )


async def test_relationship_note_is_scrubbed_but_event_is_kept(session_factory) -> None:
    """`relationship_events.note` 是用户原话，置 NULL；但**行要留着**。

    为什么不能整行删：这张表是 `relationship_states`（熟悉度/信任度）的
    可重算来源，删行之后投影再也算不回来。而 `kind/weight/origin/occurred_at`
    这些是纯结构化事实，不含内容。
    """
    ids = await _seed_minimal(session_factory)
    async with session_factory() as session:
        session.add(
            RelationshipEvent(
                id="relev_1",
                actor_id="actor_ling",
                subject_id=ids["subject_id"],
                turn_id="turn_rel",
                kind="warmth",
                weight=1.0,
                origin="user_statement",
                source_event_ids=["turn:turn_rel"],
                note=CANARY[:200],
            )
        )
        await session.commit()

    async with session_factory() as session:
        request = await request_purge(session, subject_id=ids["subject_id"])
        await session.commit()
    async with session_factory() as session:
        result = await purge_subject(session, tombstone_id=request.tombstone_id)
        await session.commit()

    assert result.details.get("relationship_notes_scrubbed") == 1

    async with session_factory() as session:
        row = await session.get(RelationshipEvent, "relev_1")
        assert row is not None, "关系事件被整行删了：关系投影再也算不回来"
        assert row.note is None, "关系事件的 note 里还留着用户原话"
        assert row.kind == "warmth"
        assert row.weight == 1.0


async def test_nothing_holding_the_canary_survives_the_purge(session_factory) -> None:
    """总闸：清理之后，全库扫描一遍，CANARY 不该出现在任何一张表里。

    逐表断言容易被"以后新加一张表"绕过；这条把"原文彻底不存在"当成一个
    整体性质来验证（保留的 `messages` 在本用例里本来就没有该文本）。
    """
    ids = await _seed_minimal(session_factory)
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(
            MediaEvidenceRow(
                id="media_scan",
                actor_id="actor_ling",
                subject_id=ids["subject_id"],
                window_id=ids["window_id"],
                kind="image",
                media_ref="file:uploads/x.png",
                origin="user_upload",
                description=CANARY,
                dedupe_key="dk_scan",
                expires_at=now + timedelta(days=7),
            )
        )
        session.add(
            KnowledgeItem(
                id="kn_scan",
                topic="过敏",
                claim=CANARY,
                source_kind="user_statement",
                observed_at=now,
                valid_until=now + timedelta(days=30),
                owner_subject_id=ids["subject_id"],
            )
        )
        session.add(
            PromptSnapshot(
                id="psnap_scan",
                actor_id="actor_ling",
                turn_id="turn_scan",
                window_id=ids["window_id"],
                created_at=now,
                expires_at=now + timedelta(days=7),
                payload={"messages": [{"role": "system", "content": CANARY}]},
            )
        )
        session.add(
            RelationshipEvent(
                id="relev_scan",
                actor_id="actor_ling",
                subject_id=ids["subject_id"],
                turn_id="turn_scan",
                kind="disclosure",
                weight=1.0,
                origin="user_statement",
                source_event_ids=["turn:turn_scan"],
                note=CANARY,
            )
        )
        session.add(
            OutboxEvent(
                id="obx_scan",
                topic="memory.extract",
                state="done",
                subject_id=None,
                payload={"subject_id": ids["subject_id"], "user_text": CANARY},
            )
        )
        await session.commit()

    async with session_factory() as session:
        request = await request_purge(session, subject_id=ids["subject_id"])
        await session.commit()
    async with session_factory() as session:
        await purge_subject(session, tombstone_id=request.tombstone_id)
        await session.commit()

    # 逐表把该主体的行读出来，拼成一个大字符串扫描。
    async with session_factory() as session:
        blobs: list[str] = []
        for model in (
            MemoryRow,
            MediaEvidenceRow,
            KnowledgeItem,
            PromptSnapshot,
            RelationshipEvent,
            OutboxEvent,
        ):
            rows = list((await session.execute(select(model))).scalars())
            blobs.append(repr([row.__dict__ for row in rows]))
        blob = "\n".join(blobs)

    assert CANARY not in blob, "清理之后库里仍能搜到被删的原文"
    assert "花生过敏" not in blob, "清理之后库里仍能搜到从原文派生的值"
