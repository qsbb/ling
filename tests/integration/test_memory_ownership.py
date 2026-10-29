"""记忆纠正接口的归属校验。

真实缺陷：`retract` / `supersede` 只按 memory_id 操作，不校验归属。
任何已登录用户拿到别人的 memory_id 就能撤回或改写别人的记忆。

返回 404 而不是 403：403 会泄漏"这个 id 存在"。
"""

from __future__ import annotations

import pytest

from ling.contracts.v1 import MemoryProposal, Origin, Visibility
from ling.domains.identity.service import IdentityService
from ling.domains.memory.service import MemoryService
from ling.infra.db import session_scope
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def _seed_for(env: TestEnv, subject_id: str) -> str:
    async with session_scope() as session:
        return (
            await MemoryService(session).propose(
                MemoryProposal(
                    proposal_id="own1",
                    kind="assertion",
                    text="用户喜欢下雨天",
                    origin=Origin.user_statement,
                    source_event_ids=["e"],
                    visibility=Visibility.subject_private,
                    confidence=0.9,
                ),
                subject_id=subject_id,
                turn_id="ownT1",
                slot="likes",
                value="下雨天",
            )
        ) or ""


async def _other_subject() -> str:
    import uuid

    async with session_scope() as session:
        view = await IdentityService(session).create_user(
            username=f"other_{uuid.uuid4().hex[:8]}", password="pw-not-used"
        )
        return view.subject_id


async def test_cannot_retract_other_subject_memory(logged_in: TestEnv) -> None:
    other = await _other_subject()
    memory_id = await _seed_for(logged_in, other)

    response = await logged_in.client.post(
        f"/v1/memory/{memory_id}/retract", json={"reason": "越权试试"}
    )
    assert response.status_code == 404, response.text

    # 确认真的没被撤回
    from ling.infra.models import MemoryRow

    async with session_scope() as session:
        row = await session.get(MemoryRow, memory_id)
    assert row is not None and row.retracted is False


async def test_cannot_supersede_other_subject_memory(logged_in: TestEnv) -> None:
    other = await _other_subject()
    memory_id = await _seed_for(logged_in, other)

    response = await logged_in.client.post(
        f"/v1/memory/{memory_id}/supersede",
        json={"text": "用户住在火星", "reason": "越权试试"},
    )
    assert response.status_code == 404


async def test_own_memory_can_be_retracted(logged_in: TestEnv) -> None:
    """自己的记忆要能正常撤回（不能为了安全把功能锁死）。"""
    memory_id = await _seed_for(logged_in, logged_in.subject_id)
    response = await logged_in.client.post(
        f"/v1/memory/{memory_id}/retract", json={"reason": "用户纠正"}
    )
    assert response.status_code == 200
    assert response.json()["retracted"] is True


async def test_unknown_id_is_404(logged_in: TestEnv) -> None:
    response = await logged_in.client.post(
        "/v1/memory/mem_nope/retract", json={"reason": "x"}
    )
    assert response.status_code == 404
