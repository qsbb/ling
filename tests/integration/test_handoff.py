"""共享胶囊：授权门、同窗口排除、过期、撤销、TTL 边界。

设计文档 §6.2：HandoffCapsule 是"跨端续接"用的短时摘要，
必须 (1) 先有 memory.cross_device 授权，(2) 短 TTL，(3) 可撤销。
状态文档里"共享胶囊过期"是缺口。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from ling.infra.db import session_scope
from ling.infra.models import HandoffCapsuleRow
from ling.kernel.handoff import MAX_TTL_MINUTES, HandoffService
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

SOURCE = "win_source"
TARGET = "win_target"


async def _grant_cross_device(env: TestEnv) -> None:
    response = await env.client.put(
        "/v1/consents", json={"scope": "memory.cross_device", "granted": True}
    )
    assert response.status_code == 200


async def _create(env: TestEnv, **kwargs: object):
    async with session_scope() as session:
        return await HandoffService(session).create(
            subject_id=env.subject_id,
            source_window_id=SOURCE,
            recent_topic="在聊旅行计划",
            **kwargs,  # type: ignore[arg-type]
        )


async def test_create_requires_consent(logged_in: TestEnv) -> None:
    """没有跨端授权就不能生成胶囊。

    否则会出现"偷偷准备好可以跨端读的东西"——用户没同意，内容却已经
    躺在能被别的设备读到的地方。
    """
    with pytest.raises(PermissionError, match="memory.cross_device"):
        await _create(logged_in)


async def test_create_with_consent(logged_in: TestEnv) -> None:
    await _grant_cross_device(logged_in)
    capsule = await _create(logged_in)
    assert capsule.source_window_id == SOURCE
    assert capsule.recent_topic == "在聊旅行计划"


async def test_read_from_other_window(logged_in: TestEnv) -> None:
    await _grant_cross_device(logged_in)
    await _create(logged_in)
    async with session_scope() as session:
        found = await HandoffService(session).read(
            subject_id=logged_in.subject_id, current_window_id=TARGET
        )
    assert found is not None
    assert found.recent_topic == "在聊旅行计划"


async def test_read_from_same_window_returns_none(logged_in: TestEnv) -> None:
    """同一窗口不需要"续接"——返回自己刚说的话没有意义，
    而且会污染上下文（把摘要再喂一遍）。
    """
    await _grant_cross_device(logged_in)
    await _create(logged_in)
    async with session_scope() as session:
        found = await HandoffService(session).read(
            subject_id=logged_in.subject_id, current_window_id=SOURCE
        )
    assert found is None


async def test_read_without_consent_returns_none(logged_in: TestEnv) -> None:
    """授权被撤回后，已存在的胶囊也不能读。"""
    await _grant_cross_device(logged_in)
    await _create(logged_in)
    # 撤回授权
    await logged_in.client.put(
        "/v1/consents", json={"scope": "memory.cross_device", "granted": False}
    )
    async with session_scope() as session:
        found = await HandoffService(session).read(
            subject_id=logged_in.subject_id, current_window_id=TARGET
        )
    assert found is None


async def test_expired_capsule_not_readable(logged_in: TestEnv) -> None:
    """TTL 到期后不再可读（短时摘要不能变成长期记忆）。"""
    await _grant_cross_device(logged_in)
    capsule = await _create(logged_in, ttl_minutes=1)

    # 直接把过期时间推到过去，模拟时间流逝
    async with session_scope() as session:
        await session.execute(
            update(HandoffCapsuleRow)
            .where(HandoffCapsuleRow.id == capsule.capsule_id)
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )

    async with session_scope() as session:
        found = await HandoffService(session).read(
            subject_id=logged_in.subject_id, current_window_id=TARGET
        )
    assert found is None, "过期胶囊仍可读"


async def test_revoke_makes_unreadable_immediately(logged_in: TestEnv) -> None:
    await _grant_cross_device(logged_in)
    capsule = await _create(logged_in)
    async with session_scope() as session:
        assert await HandoffService(session).revoke(capsule.capsule_id) is True

    async with session_scope() as session:
        found = await HandoffService(session).read(
            subject_id=logged_in.subject_id, current_window_id=TARGET
        )
    assert found is None, "撤销后仍可读"


async def test_revoke_unknown_returns_false(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        assert await HandoffService(session).revoke("cap_nope") is False


async def test_ttl_is_clamped_to_max(logged_in: TestEnv) -> None:
    """超过上限的 TTL 会被压回上限——防止有人传一年。"""
    await _grant_cross_device(logged_in)
    capsule = await _create(logged_in, ttl_minutes=MAX_TTL_MINUTES * 100)
    delta = capsule.expires_at - datetime.now(UTC)
    assert delta <= timedelta(minutes=MAX_TTL_MINUTES, seconds=5)


async def test_ttl_zero_becomes_one_minute(logged_in: TestEnv) -> None:
    """0 或负数 TTL 至少保留 1 分钟（不能立即失效成死数据）。"""
    await _grant_cross_device(logged_in)
    capsule = await _create(logged_in, ttl_minutes=0)
    assert capsule.expires_at > datetime.now(UTC)


async def test_capsule_does_not_cross_subjects(logged_in: TestEnv) -> None:
    """别人的胶囊不能被我读到。"""
    await _grant_cross_device(logged_in)
    await _create(logged_in)
    async with session_scope() as session:
        found = await HandoffService(session).read(
            subject_id="subj_someone_else", current_window_id=TARGET
        )
    assert found is None
