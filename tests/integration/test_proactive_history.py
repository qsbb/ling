"""主动联系历史由实际门控和投递回执组成，读取不得产生新任务。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from ling.domains.identity.service import IdentityService
from ling.domains.proactive.dispatch import dispatch_pending
from ling.infra.db import session_scope
from ling.infra.models import (
    DeliveryReceiptRow,
    IdempotencyKey,
    Operation,
    ProactiveOpportunity,
    Window,
)
from tests.integration.conftest import TestEnv
from tests.integration.test_proactive_dispatch import _Connector

pytestmark = pytest.mark.w0_web
NOW = datetime(2030, 6, 1, 4, 0, tzinfo=UTC)


class _Config:
    async def effective(self):  # noqa: ANN202
        return SimpleNamespace(dialogue=SimpleNamespace(
            proactive_delivery_enabled=True,
            proactive_daily_limit=20,
            quiet_start=(23, 0),
            quiet_end=(8, 0),
        ))


def _candidate(oid: str, subject: str, *, reason: str, **kwargs) -> ProactiveOpportunity:
    return ProactiveOpportunity(
        id=oid,
        actor_id="actor_ling",
        subject_id=subject,
        kind="check_in",
        reason=reason,
        state="candidate",
        expires_at=NOW + timedelta(hours=2),
        created_at=NOW,
        source_event_id=f"intent:{oid}",
        reason_code="intent_due",
        draft_text="合成草稿",
        **kwargs,
    )


async def _dispatch(connector: _Connector, timezone: str = "Asia/Shanghai") -> dict:
    async with session_scope() as session:
        return await dispatch_pending(
            session, actor_id="actor_ling", connector=connector, config_reader=_Config(),
            subject_timezone=timezone, now=NOW,
        )


async def _records(env: TestEnv, **params: str) -> dict[str, dict]:
    response = await env.client.get("/v1/proactive", params=params)
    assert response.status_code == 200, response.text
    return {row["opportunity_id"]: row for row in response.json()["recent"]}


async def test_actual_gates_and_receipts_are_recorded_and_read_only(logged_in: TestEnv) -> None:
    subject = logged_in.subject_id
    async with session_scope() as session:
        session.add(Window(
            id="win_proactive_history", actor_id="actor_ling", persona_id="persona_ling",
            subject_id=subject, title="onebot-demo:private:100200300", channel="onebot",
            visibility="subject_private", archived=False, last_message_at=NOW,
        ))
        session.add(_candidate("opp_history_success", subject, reason="合成来源甲"))

    success = _Connector(state="delivered")
    assert (await _dispatch(success))["sent"] == 1
    async with session_scope() as session:
        session.add(_candidate("opp_history_failure", subject, reason="合成来源乙"))
    failure = _Connector(state="failed")
    assert (await _dispatch(failure))["failed"] == 1
    async with session_scope() as session:
        session.add(_candidate("opp_history_blocked", subject, reason="合成来源丙"))
    blocked = _Connector()
    await _dispatch(blocked, "America/New_York")
    assert blocked.sent == []

    async with session_scope() as session:
        opportunities_before = (await session.execute(
            select(ProactiveOpportunity.id, ProactiveOpportunity.state)
        )).all()
        claims_before = (await session.execute(select(IdempotencyKey.id))).scalars().all()
        operations_before = (await session.execute(select(Operation.id))).scalars().all()

    records = await _records(logged_in)
    assert records == await _records(logged_in) == await _records(logged_in)
    sent = records["opp_history_success"]
    failed = records["opp_history_failure"]
    waiting = records["opp_history_blocked"]
    assert sent["reason"] == "合成来源甲" and sent["reason_code"] == "intent_due"
    assert sent["has_source"] is True and sent["gate"]["allowed"] is True
    assert sent["receipt"]["state"] == "delivered"
    assert failed["reason"] == "合成来源乙" and failed["gate"]["allowed"] is True
    assert failed["receipt"]["state"] == "failed"
    assert waiting["reason"] == "合成来源丙" and waiting["gate"]["allowed"] is False
    assert "勿扰" in waiting["gate"]["reasons"][0]
    assert waiting["receipt"] is None and waiting["state"] == "candidate"
    assert waiting["gate"]["checked_at"] is not None

    async with session_scope() as session:
        for record in (sent, failed):
            opportunity = await session.get(ProactiveOpportunity, record["opportunity_id"])
            receipt = await session.scalar(select(DeliveryReceiptRow).where(
                DeliveryReceiptRow.operation_id == opportunity.delivery_operation_id
            ))
            assert receipt is not None and receipt.state == record["receipt"]["state"]
        assert (await session.execute(
            select(ProactiveOpportunity.id, ProactiveOpportunity.state)
        )).all() == opportunities_before
        assert (await session.execute(select(IdempotencyKey.id))).scalars().all() == claims_before
        assert (await session.execute(select(Operation.id))).scalars().all() == operations_before


async def test_scope_voice_and_missing_or_unrelated_receipts(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        other = await IdentityService(session).create_user(
            username="history-member", password="synthetic-password", role="member"
        )
        voice = _candidate("opp_history_voice", logged_in.subject_id, reason="语音来源")
        voice.output_modality = "voice"
        voice.draft_text = "合成语音正文专用标记"
        ungenerated = _candidate("opp_history_ungenerated", logged_in.subject_id, reason="未生成来源")
        ungenerated.output_modality = "voice"
        ungenerated.draft_text = None
        other_row = _candidate("opp_history_other", other.subject_id, reason="另一位来源标记")
        private = _candidate("opp_history_private", logged_in.subject_id, reason="角色私有标记")
        private.visibility = "actor_private"
        missing = _candidate("opp_history_missing", logged_in.subject_id, reason="旧记录")
        missing.state = "sent"
        missing.delivery_operation_id = "operation_missing"
        unrelated = _candidate("opp_history_unrelated", logged_in.subject_id, reason="关联异常")
        unrelated.state = "failed"
        unrelated.delivery_operation_id = "operation_unrelated"
        unrelated.rejected_reason = "不应暴露的原始平台错误"
        session.add_all([voice, ungenerated, other_row, private, missing, unrelated, Operation(
            id="operation_unrelated", kind="delivery", actor_id="actor_ling",
            subject_id=logged_in.subject_id, state="succeeded",
            payload={"source": "other", "opportunity_id": unrelated.id},
        ), DeliveryReceiptRow(
            id="receipt_unrelated", operation_id="operation_unrelated", channel="fake",
            state="delivered", observed_at=NOW,
        )])

    participant = await logged_in.client.get("/v1/proactive")
    assert participant.status_code == 200
    assert "合成语音正文专用标记" not in participant.text
    assert "另一位来源标记" not in participant.text
    assert "角色私有标记" not in participant.text
    assert "不应暴露的原始平台错误" not in participant.text
    pending = {row["opportunity_id"]: row for row in participant.json()["pending"]}
    assert pending["opp_history_voice"]["draft_available"] is True
    assert pending["opp_history_ungenerated"]["draft_available"] is False
    records = {r["opportunity_id"]: r for r in participant.json()["recent"]}
    assert records["opp_history_missing"]["gate"]["allowed"] is None
    assert records["opp_history_missing"]["receipt"] is None
    assert records["opp_history_unrelated"]["receipt"] is None

    operator = await logged_in.client.get("/v1/proactive", params={"view": "operator"})
    assert operator.status_code == 200
    assert "合成语音正文专用标记" in operator.text
    assert "角色私有标记" in operator.text
    assert "另一位来源标记" not in operator.text

    login = await logged_in.client.post(
        "/v1/auth/login", json={"username": "history-member", "password": "synthetic-password"}
    )
    assert login.status_code == 200
    member = await logged_in.client.get("/v1/proactive", params={"subject_id": logged_in.subject_id})
    assert member.status_code == 200
    assert "另一位来源标记" in member.text
    assert "语音来源" not in member.text and "角色私有标记" not in member.text
    denied = await logged_in.client.get("/v1/proactive", params={"view": "operator"})
    assert denied.status_code == 403


async def test_malformed_receipt_source_is_skipped_without_hiding_valid_receipt(
    logged_in: TestEnv,
) -> None:
    subject = logged_in.subject_id
    async with session_scope() as session:
        valid = _candidate("opp_valid_receipt", subject, reason="有效回执")
        valid.delivery_operation_id = "op_valid_receipt"
        malformed = _candidate("opp_bad_receipt", subject, reason="异常回执")
        malformed.delivery_operation_id = "op_bad_receipt"
        session.add_all([
            valid,
            malformed,
            Operation(
                id="op_valid_receipt", kind="delivery", actor_id="actor_ling",
                subject_id=subject, state="succeeded",
                payload={"source": "proactive", "opportunity_id": valid.id},
            ),
            Operation(
                id="op_bad_receipt", kind="delivery", actor_id="actor_ling",
                subject_id=subject, state="succeeded",
                payload={"source": "proactive", "opportunity_id": [malformed.id]},
            ),
            DeliveryReceiptRow(
                id="receipt_valid", operation_id="op_valid_receipt", channel="fake",
                state="delivered", observed_at=NOW,
            ),
            DeliveryReceiptRow(
                id="receipt_bad", operation_id="op_bad_receipt", channel="fake",
                state="delivered", observed_at=NOW,
            ),
        ])

    records = await _records(logged_in)
    assert records["opp_valid_receipt"]["receipt"]["state"] == "delivered"
    assert records["opp_bad_receipt"]["receipt"] is None
