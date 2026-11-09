"""媒体沉降：看过的视频过期后，她还能记得。

## 用户原话

「然后看过的视频完整的解析留存一小段时间，过了一段时间之后，他就总结出简单
的内容，存放到记忆或者知识库里（经过筛选，走记忆入库流程）」

## 这个文件在钉什么

媒体描述只有最近 3 条能进上下文（`context.py:749`），而且 `expires_at` 一到
整行都读不到。所以不沉降 = 三轮之后她彻底忘了自己看过什么。

下面每条用例对应需求里"经过筛选"的一个落点：
- **她真的看过**才算（模型推断的"这是一只猫"不能变成她记得的事）
- **有来源**才写（没有来源的记忆不该存在）
- **有归属主体**才写（她自产素材不是"关于某个人的记忆"）
- **幂等**（反复跑不会长出第二条）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from ling.domains.media.sediment import SEDIMENT_SLOT, sediment_expired_evidence
from ling.infra.db import session_scope
from ling.infra.models import MediaEvidenceRow, MemoryRow
from ling.infra.security import new_id
from tests.integration.conftest import TestEnv, logged_in, web_env  # noqa: F401 - fixtures

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 10, 4, 0, tzinfo=UTC)


async def _clean(subject_id: str) -> None:
    from sqlalchemy import delete

    async with session_scope() as session:
        await session.execute(
            delete(MemoryRow).where(
                MemoryRow.subject_id == subject_id,
                MemoryRow.slot == SEDIMENT_SLOT,
            )
        )
        await session.execute(
            delete(MediaEvidenceRow).where(MediaEvidenceRow.subject_id == subject_id)
        )


async def _add_evidence(
    *,
    subject_id: str,
    window_id: str,
    description: str | None,
    description_origin: str | None = "human",
    kind: str = "video",
    expires_in_days: int | None = 0,
    created_days_ago: int = 1,
    source_event_id: str | None = "evt_media_1",
    own_asset: bool = False,
) -> str:
    """造一条媒体证据。`expires_in_days=0` = 已经/即将过期。"""
    evidence_id = new_id("mev")
    created = NOW - timedelta(days=created_days_ago)
    expires = (
        None if expires_in_days is None else NOW + timedelta(days=expires_in_days)
    )
    async with session_scope() as session:
        session.add(
            MediaEvidenceRow(
                id=evidence_id,
                actor_id="actor_ling",
                subject_id=None if own_asset else subject_id,
                window_id=window_id,
                kind=kind,
                media_ref=f"s3://bucket/{evidence_id}",
                origin="user_upload",
                visibility="window_private",
                description=description,
                description_origin=description_origin,
                source_event_id=source_event_id,
                dedupe_key=f"key_{evidence_id}",
                expires_at=expires,
                created_at=created,
                updated_at=created,
            )
        )
    return evidence_id


async def _sedimented(subject_id: str) -> list[MemoryRow]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(MemoryRow).where(
                        MemoryRow.subject_id == subject_id,
                        MemoryRow.slot == SEDIMENT_SLOT,
                    )
                )
            ).scalars()
        )


async def test_watched_video_becomes_a_memory(logged_in: TestEnv) -> None:  # noqa: F811
    """她真的看过、且描述将要过期 → 沉降成一条记忆。"""
    await _clean(logged_in.subject_id)
    await _add_evidence(
        subject_id=logged_in.subject_id,
        window_id="win_sed_1",
        description="一只猫从桌上跳下去，然后镜头转向窗外",
        expires_in_days=0,
    )

    async with session_scope() as session:
        outcome = await sediment_expired_evidence(
            session, actor_id=logged_in.settings.actor_id, now=NOW
        )
    assert outcome.written == 1, outcome

    rows = await _sedimented(logged_in.subject_id)
    assert len(rows) == 1
    row = rows[0]
    assert "猫从桌上跳下去" in row.text
    assert "视频" in row.text, "记忆里要保留媒体类型，那是后续对话的线索"
    # **外部观察**，不是"用户亲口说的"：她看过一个视频 ≠ 用户告诉过她
    assert row.origin == "external_observation"
    assert row.source_event_ids, "没有来源的记忆不该存在"


async def test_model_inference_is_never_sedimented(logged_in: TestEnv) -> None:  # noqa: F811
    """模型推断的描述**不能**变成她记得的事。

    这是 `fact_eligible=False` 的同一条铁律：模型看图说"这是一只猫"只是猜测。
    把它沉降成记忆，等于让模型的猜测变成她的经历。
    """
    await _clean(logged_in.subject_id)
    await _add_evidence(
        subject_id=logged_in.subject_id,
        window_id="win_sed_2",
        description="（推断）桌上好像有一只猫",
        description_origin="model_inference",
        expires_in_days=0,
    )

    async with session_scope() as session:
        outcome = await sediment_expired_evidence(
            session, actor_id=logged_in.settings.actor_id, now=NOW
        )
    assert outcome.written == 0, outcome
    assert await _sedimented(logged_in.subject_id) == []


async def test_fresh_evidence_is_left_for_the_live_path(logged_in: TestEnv) -> None:  # noqa: F811
    """还新鲜的证据不沉降：继续走实时通道。

    提前沉降会让她**提前**说"我以前看过"——而她其实刚看过、还记得。
    """
    await _clean(logged_in.subject_id)
    await _add_evidence(
        subject_id=logged_in.subject_id,
        window_id="win_sed_3",
        description="窗外的雪下得很大",
        expires_in_days=30,
        created_days_ago=1,
    )

    async with session_scope() as session:
        outcome = await sediment_expired_evidence(
            session, actor_id=logged_in.settings.actor_id, now=NOW
        )
    assert outcome.written == 0
    assert await _sedimented(logged_in.subject_id) == []


async def test_evidence_without_source_is_skipped(logged_in: TestEnv) -> None:  # noqa: F811
    """没有来源的证据不写：没有来源的记忆不该存在。"""
    await _clean(logged_in.subject_id)
    await _add_evidence(
        subject_id=logged_in.subject_id,
        window_id="win_sed_4",
        description="街角有人在弹吉他",
        expires_in_days=0,
        source_event_id=None,
    )

    async with session_scope() as session:
        outcome = await sediment_expired_evidence(
            session, actor_id=logged_in.settings.actor_id, now=NOW
        )
    assert outcome.written == 0
    assert outcome.skipped == 1, "跳过的条数要如实计数，不能假装扫到了"


async def test_own_asset_is_not_sedimented(logged_in: TestEnv) -> None:  # noqa: F811
    """她自己产生的素材不沉降——那不是"关于某个人的记忆"。"""
    await _clean(logged_in.subject_id)
    await _add_evidence(
        subject_id=logged_in.subject_id,
        window_id="win_sed_5",
        description="她自己画的一张图",
        expires_in_days=0,
        own_asset=True,
    )

    async with session_scope() as session:
        outcome = await sediment_expired_evidence(
            session, actor_id=logged_in.settings.actor_id, now=NOW
        )
    assert outcome.written == 0


async def test_running_twice_does_not_duplicate(logged_in: TestEnv) -> None:  # noqa: F811
    """反复跑不长出第二条：幂等靠 `media:{evidence_id}` 这个 turn_id 键。"""
    await _clean(logged_in.subject_id)
    await _add_evidence(
        subject_id=logged_in.subject_id,
        window_id="win_sed_6",
        description="地铁上有人在看书",
        expires_in_days=0,
    )

    async with session_scope() as session:
        await sediment_expired_evidence(
            session, actor_id=logged_in.settings.actor_id, now=NOW
        )
    async with session_scope() as session:
        second = await sediment_expired_evidence(
            session, actor_id=logged_in.settings.actor_id, now=NOW
        )

    rows = await _sedimented(logged_in.subject_id)
    assert len(rows) == 1, f"重复沉降长出了 {len(rows)} 条"
    assert second.written == 0


async def test_other_subjects_evidence_is_untouched(logged_in: TestEnv) -> None:  # noqa: F811
    """只沉降本 actor 的证据，别人的不动。"""
    other = "subj_sediment_other"
    await _clean(logged_in.subject_id)
    await _clean(other)
    await _add_evidence(
        subject_id=other,
        window_id="win_sed_other",
        description="别人的视频",
        expires_in_days=0,
    )

    async with session_scope() as session:
        outcome = await sediment_expired_evidence(
            session, actor_id=logged_in.settings.actor_id, now=NOW
        )
    # 同 actor 下别人 subject 的证据仍然可以被沉降（记忆是按 subject 存的），
    # 但绝不能串到本主体名下
    assert await _sedimented(logged_in.subject_id) == []
    assert outcome.written <= 1
