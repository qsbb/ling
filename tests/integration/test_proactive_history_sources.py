"""Independent integration coverage for proactive history and its sources."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from ling.domains.identity.service import IdentityService
from ling.domains.proactive.dispatch import dispatch_pending
from ling.infra.db import session_scope
from ling.infra.models import ProactiveOpportunity, Window
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web
NOW = datetime(2030, 6, 1, 4, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


class Config:
    async def effective(self):
        return SimpleNamespace(dialogue=SimpleNamespace(
            proactive_delivery_enabled=True,
            proactive_daily_limit=20,
            quiet_start=(23, 0), quiet_end=(8, 0),
        ))


def opportunity(identifier, subject, reason, reason_code="intent_due", *, draft="开场白"):
    return ProactiveOpportunity(
        id=identifier, actor_id="actor_ling", subject_id=subject,
        kind="check_in", reason=reason, reason_code=reason_code,
        source_event_id=f"intent:{identifier}", state="candidate",
        expires_at=NOW + timedelta(hours=2), created_at=NOW, draft_text=draft,
    )


async def insert(*objects):
    async with session_scope() as session:
        session.add_all(objects)


async def rows(client):
    response = await client.get("/v1/proactive")
    assert response.status_code == 200, response.text
    return {item["opportunity_id"]: item for item in response.json()["recent"]}


async def test_actual_sources_collection_and_web_delivery(logged_in: TestEnv):
    from ling.domains.goals.service import GoalsService
    from ling.domains.intent.service import IntentService
    from ling.domains.proactive.service import ProactivityService
    from ling.infra.models import Message

    now = datetime.now(UTC)
    subject = logged_in.subject_id
    async with session_scope() as session:
        commitment = await GoalsService(session, actor_id="actor_ling").create_commitment(
            subject_id=subject, text="验收到期承诺", due_at=now - timedelta(minutes=1),
            timezone="Asia/Shanghai",
        )
        intent = await IntentService(session, actor_id="actor_ling").propose(
            goal="验收到期意图", kind="reach_out", source_kind="message",
            source_ref="acceptance_turn", subject_id=subject,
            window_id="accept_source_window", not_before=now - timedelta(minutes=5),
            expires_at=now + timedelta(hours=3),
        )
    async with session_scope() as session:
        first = await ProactivityService(session, actor_id="actor_ling").collect_candidates(now=now)
    async with session_scope() as session:
        second = await ProactivityService(session, actor_id="actor_ling").collect_candidates(now=now)
    assert first["created"] == 2
    assert second["created"] == 0 and second["skipped_duplicate"] == 2
    async with session_scope() as session:
        stored = list((await session.scalars(select(ProactiveOpportunity))).all())
        by_source = {row.source_event_id: row for row in stored}
        assert len(by_source) == 2
        commitment_row = by_source[f"commitment:{commitment.commitment_id}"]
        intent_row = by_source[f"intent:{intent.intent_id}"]
        commitment_id = commitment_row.id
        intent_id = intent_row.id
        assert commitment_row.reason_code == "commitment_due"
        assert intent_row.reason_code == "intent_due"
        await ProactivityService(session, actor_id="actor_ling").set_draft(
            intent_id, text="验收 Web 消息正文"
        )
        session.add(Window(
            id="accept_source_window", actor_id="actor_ling", persona_id="persona_ling",
            subject_id=subject, title="验收 Web 会话", channel="web",
            visibility="subject_private", archived=False, last_message_at=now,
        ))
    initial = await rows(logged_in.client)
    assert initial[commitment_id]["reason_code"] == "commitment_due"
    assert initial[intent_id]["reason_code"] == "intent_due"
    assert "验收到期承诺" in initial[commitment_id]["reason"]
    assert initial[intent_id]["reason"] == "验收到期意图"
    assert initial[intent_id]["receipt"] is None
    assert initial[intent_id]["state"] == "candidate"

    class NoQuietConfig:
        async def effective(self):
            return SimpleNamespace(dialogue=SimpleNamespace(
                proactive_delivery_enabled=True, proactive_daily_limit=20,
                quiet_start=(0, 0), quiet_end=(0, 0),
            ))

    async with session_scope() as session:
        result = await dispatch_pending(
            session, actor_id="actor_ling", connector=None, config_reader=NoQuietConfig(),
            subject_timezone="Asia/Shanghai", now=now, deliver_to_web=True,
        )
    assert result["sent"] == 1
    delivered = await rows(logged_in.client)
    assert delivered[intent_id]["state"] == "sent"
    assert delivered[intent_id]["web_saved"] is True
    assert delivered[intent_id]["receipt"] is None
    async with session_scope() as session:
        messages = list((await session.scalars(select(Message).where(
            Message.window_id == "accept_source_window"
        ))).all())
        assert len(messages) == 1 and messages[0].text == "验收 Web 消息正文"
        message_id = messages[0].id
    for _ in range(3):
        await rows(logged_in.client)
    async with session_scope() as session:
        messages = list((await session.scalars(select(Message).where(
            Message.window_id == "accept_source_window"
        ))).all())
        assert [message.id for message in messages] == [message_id]
    print("REAL_SOURCE_COLLECTION", first, second, "web_message", message_id)


async def test_revision_0072_preserves_previous_rows(logged_in: TestEnv):
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    revision_path = ROOT / "migrations/versions/0072_proactive_gate_snapshot.py"
    spec = importlib.util.spec_from_file_location("acceptance_revision_0072", revision_path)
    assert spec is not None and spec.loader is not None
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)

    def run_revision(connection, action):
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            getattr(revision, action)()

    def schema_state(connection):
        inspector = inspect(connection)
        columns = {item["name"] for item in inspector.get_columns("proactive_opportunities")}
        indexes = {item["name"] for item in inspector.get_indexes("proactive_opportunities")}
        return columns, indexes

    async with session_scope() as session:
        session.add(opportunity("accept_pre_migration", logged_in.subject_id, "历史记录不丢失"))
        await session.flush()
        connection = await session.connection()
        await connection.run_sync(run_revision, "downgrade")
        columns, indexes = await connection.run_sync(schema_state)
        assert "last_gate_allowed" not in columns
        assert "ix_proactive_actor_subject_created" not in indexes
        assert (await session.execute(text(
            "SELECT reason FROM proactive_opportunities WHERE id='accept_pre_migration'"
        ))).scalar_one() == "历史记录不丢失"
        await connection.run_sync(run_revision, "upgrade")
        columns, indexes = await connection.run_sync(schema_state)
        assert {"last_gate_allowed", "last_gate_reasons", "last_gate_at"} <= columns
        assert "ix_proactive_actor_subject_created" in indexes
        row = (await session.execute(text(
            "SELECT reason, last_gate_allowed, last_gate_reasons, last_gate_at "
            "FROM proactive_opportunities WHERE id='accept_pre_migration'"
        ))).one()
        assert tuple(row) == ("历史记录不丢失", None, None, None)
        print("MIGRATION_0072", "downgrade/upgrade", tuple(row))


async def test_two_users_have_distinct_persisted_gate_decisions(logged_in: TestEnv):
    async with session_scope() as session:
        other = await IdentityService(session).create_user(
            username="different-zone-member", password="synthetic-password", role="member"
        )
    await insert(
        opportunity("accept_zone_shanghai", logged_in.subject_id, "上海时区候选"),
        opportunity("accept_zone_newyork", other.subject_id, "纽约时区候选"),
    )
    async with session_scope() as session:
        result = await dispatch_pending(
            session, actor_id="actor_ling", connector=None, config_reader=Config(),
            subject_timezone="Asia/Shanghai",
            subject_timezones={
                logged_in.subject_id: "Asia/Shanghai",
                other.subject_id: "America/New_York",
            }, now=NOW,
        )
    assert result["considered"] == 2 and result["sent"] == 0
    own = await rows(logged_in.client)
    assert own["accept_zone_shanghai"]["gate"]["allowed"] is True
    assert "accept_zone_newyork" not in own
    async with AsyncClient(transport=ASGITransport(app=logged_in.app), base_url="http://testserver") as member:
        login = await member.post("/v1/auth/login", json={
            "username": "different-zone-member", "password": "synthetic-password"
        })
        assert login.status_code == 200
        theirs = await rows(member)
    assert theirs["accept_zone_newyork"]["gate"]["allowed"] is False
    assert "勿扰" in theirs["accept_zone_newyork"]["gate"]["reasons"][0]
    assert "accept_zone_shanghai" not in theirs
    print("PER_USER_GATE", own["accept_zone_shanghai"]["gate"], theirs["accept_zone_newyork"]["gate"])
