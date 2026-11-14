"""Exercise malformed historical receipts through the real proactive HTTP view."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from ling.domains.identity.service import IdentityService
from ling.infra.db import session_scope
from ling.infra.models import DeliveryReceiptRow, Operation, ProactiveOpportunity
from tests.integration.test_proactive_history import NOW, _candidate, _records

pytestmark = pytest.mark.w0_web


async def test_receipt_provenance_and_malformed_values_keep_valid_receipt(logged_in):
    subject_id = logged_in.subject_id
    invalid_payloads = {
        "missing": {"source": "proactive"},
        "null": {"source": "proactive", "opportunity_id": None},
        "empty": {"source": "proactive", "opportunity_id": ""},
        "list": {"source": "proactive", "opportunity_id": ["opp_ci_list"]},
        "object": {"source": "proactive", "opportunity_id": {"id": "opp_ci_object"}},
        "boolean": {"source": "proactive", "opportunity_id": True},
        "integer": {"source": "proactive", "opportunity_id": 42},
        "unknown": {"source": "proactive", "opportunity_id": "opp_does_not_exist"},
        "wrong_source": {"source": "other", "opportunity_id": "opp_ci_wrong_source"},
        "hijack": {"source": "proactive", "opportunity_id": "opp_ci_valid"},
        "non_object": ["legacy", "payload"],
    }
    async with session_scope() as session:
        other = await IdentityService(session).create_user(
            username="ci-history-other", password="synthetic-password", role="member"
        )
        cases = {"valid": {"source": "proactive", "opportunity_id": "opp_ci_valid"}}
        cases.update(invalid_payloads)
        cases.update({
            "other_subject": {"source": "proactive", "opportunity_id": "opp_ci_other_subject"},
            "other_actor": {"source": "proactive", "opportunity_id": "opp_ci_other_actor"},
            "wrong_kind": {"source": "proactive", "opportunity_id": "opp_ci_wrong_kind"},
        })
        for name, payload in cases.items():
            opportunity = _candidate(f"opp_ci_{name}", subject_id, reason=f"synthetic-{name}")
            opportunity.delivery_operation_id = f"op_ci_{name}"
            session.add(opportunity)
            session.add(Operation(
                id=f"op_ci_{name}",
                kind="other" if name == "wrong_kind" else "delivery",
                actor_id="actor_other" if name == "other_actor" else "actor_ling",
                subject_id=other.subject_id if name == "other_subject" else subject_id,
                state="succeeded",
                payload=payload,
            ))
            session.add(DeliveryReceiptRow(
                id=f"receipt_ci_{name}", operation_id=f"op_ci_{name}",
                channel="expected" if name == "valid" else "wrong",
                state="delivered", observed_at=NOW if name == "valid" else NOW + timedelta(hours=1),
            ))
        session.add(_candidate("opp_ci_private_other", other.subject_id, reason="other-private-marker"))
    async with session_scope() as session:
        before = (
            (await session.execute(select(ProactiveOpportunity.id, ProactiveOpportunity.state))).all(),
            (await session.execute(select(Operation.id, Operation.payload))).all(),
            (await session.execute(select(DeliveryReceiptRow.id, DeliveryReceiptRow.state))).all(),
        )
    for params in ({}, {"view": "operator"}):
        records = await _records(logged_in, **params)
        assert records["opp_ci_valid"]["receipt"]["channel"] == "expected"
        for name in cases:
            if name != "valid":
                assert records[f"opp_ci_{name}"]["receipt"] is None, name
        assert "opp_ci_private_other" not in records
    async with session_scope() as session:
        after = (
            (await session.execute(select(ProactiveOpportunity.id, ProactiveOpportunity.state))).all(),
            (await session.execute(select(Operation.id, Operation.payload))).all(),
            (await session.execute(select(DeliveryReceiptRow.id, DeliveryReceiptRow.state))).all(),
        )
    assert after == before
