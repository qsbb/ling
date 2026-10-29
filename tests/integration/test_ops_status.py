"""运维状态端点：把"可能出问题的地方"变成可执行的告警项。

无人值守时"有日志、有 healthcheck"不足以保证本人会第一时间知道故障。
这里只报**可行动**的问题，带 severity 与建议动作——个人服务器没人天天看仪表盘。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ling.infra.db import session_scope
from ling.infra.models import OutboxEvent
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def _ops(env: TestEnv) -> dict:
    response = await env.client.get("/v1/health/ops")
    assert response.status_code == 200, response.text
    return response.json()


async def test_ops_reports_shape(logged_in: TestEnv) -> None:
    body = await _ops(logged_in)
    assert body["status"] in {"ok", "attention"}
    names = {i["name"] for i in body["items"]}
    assert {
        "outbox_pending",
        "outbox_dead",
        "stuck_turns",
        "backup_age_hours",
        "disk_free_gb",
    } <= names


async def test_clean_state_is_ok_or_only_backup_warn(logged_in: TestEnv) -> None:
    """没积压/没死信/没卡住轮次时，不该报这些项。"""
    body = await _ops(logged_in)
    by_name = {i["name"]: i for i in body["items"]}
    assert by_name["outbox_pending"]["severity"] == "ok"
    assert by_name["outbox_dead"]["severity"] == "ok"
    assert by_name["stuck_turns"]["severity"] == "ok"


async def test_dead_letter_raises_warning(logged_in: TestEnv) -> None:
    """有 dead 事件必须报警——否则派生记忆会一直缺席而没人知道。"""
    async with session_scope() as session:
        session.add(
            OutboxEvent(
                id="obx_dead_ops",
                topic="memory.extract",
                payload={},
                state="dead",
                attempts=5,
                last_error="模拟连续失败",
            )
        )
    body = await _ops(logged_in)
    item = next(i for i in body["items"] if i["name"] == "outbox_dead")
    assert item["severity"] == "warn"
    assert item["value"] >= 1
    assert "dead" in (item["action"] or "")


async def test_outbox_backlog_raises_warning(logged_in: TestEnv) -> None:
    from ling.api.routes.ops import OUTBOX_PENDING_WARN

    async with session_scope() as session:
        for i in range(OUTBOX_PENDING_WARN + 1):
            session.add(
                OutboxEvent(
                    id=f"obx_backlog_{i}",
                    topic="memory.extract",
                    payload={},
                    state="pending",
                )
            )
    body = await _ops(logged_in)
    item = next(i for i in body["items"] if i["name"] == "outbox_pending")
    assert item["severity"] == "warn"
    assert item["value"] > OUTBOX_PENDING_WARN


async def test_old_pending_raises_warning(logged_in: TestEnv) -> None:
    """积压时间也要看：数量不多但很久没动，说明 worker 停了。"""
    from sqlalchemy import update

    async with session_scope() as session:
        session.add(
            OutboxEvent(
                id="obx_old", topic="memory.extract", payload={}, state="pending"
            )
        )
    async with session_scope() as session:
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == "obx_old")
            .values(created_at=datetime.now(UTC) - timedelta(hours=3))
        )
    body = await _ops(logged_in)
    item = next(i for i in body["items"] if i["name"] == "outbox_pending")
    assert item["severity"] == "warn"
    assert (item["oldest_minutes"] or 0) > 30


async def test_missing_backup_dir_warns(logged_in: TestEnv) -> None:
    """没配备份目录要报警：个人服务器只有一份数据源。"""
    body = await _ops(logged_in)
    item = next(i for i in body["items"] if i["name"] == "backup_age_hours")
    # 测试环境通常没设 LING_BACKUP_DIR → 应为 warn
    assert item["severity"] == "warn"
    assert "备份" in (item["action"] or "")


async def test_disk_reported(logged_in: TestEnv) -> None:
    body = await _ops(logged_in)
    item = next(i for i in body["items"] if i["name"] == "disk_free_gb")
    assert item["value"] is None or item["value"] > 0


async def test_stats_endpoint(logged_in: TestEnv) -> None:
    response = await logged_in.client.get("/v1/health/stats")
    assert response.status_code == 200
    body = response.json()
    assert {"memories_active", "memories_archived", "episodes_active"} <= set(body)


async def test_ops_requires_auth(web_env: TestEnv) -> None:
    response = await web_env.client.get("/v1/health/ops")
    assert response.status_code == 401


async def test_stats_expose_shadow_candidates(logged_in: TestEnv) -> None:
    """影子候选必须能被看到——否则没法评估质量、也没法决定要不要开真发送。"""
    from datetime import UTC, datetime, timedelta

    from ling.domains.goals.service import GoalsService
    from ling.domains.proactive.service import ProactivityService

    async with session_scope() as session:
        await GoalsService(session, actor_id="actor_ling").create_commitment(
            subject_id=logged_in.subject_id,
            window_id="win_ops_pro",
            text="提醒我交材料",
            due_at=datetime.now(UTC) - timedelta(minutes=1),
            timezone="Asia/Shanghai",
        )
    async with session_scope() as session:
        await ProactivityService(session, actor_id="actor_ling").collect_candidates()

    response = await logged_in.client.get("/v1/health/stats")
    body = response.json()
    assert body.get("proactive_candidates", 0) >= 1
    assert "commitment_due" in (body.get("proactive_candidates_by_reason") or {})
