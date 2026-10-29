"""G0 契约基线测试：版本字段、未知字段失败关闭、ID 语义与幂等键唯一性。

这些是契约测试（不是功能测试），任何阶段都可以运行。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from ling.contracts import SCHEMA_VERSION, v1

ROOT = Path(__file__).resolve().parents[2]


def test_schema_version_is_v1() -> None:
    assert SCHEMA_VERSION == "v1"


def test_runtime_scope_requires_ids() -> None:
    scope = v1.RuntimeScope(
        actor_id="actor_ling",
        persona_id="persona_ling",
        subject_id="subj_1",
        window_id="win_1",
    )
    assert scope.visibility is v1.Visibility.subject_private
    assert scope.schema_version == "v1"


def test_unknown_field_fails_closed() -> None:
    with pytest.raises(ValidationError):
        v1.RuntimeScope(
            actor_id="a",
            persona_id="p",
            subject_id="s",
            window_id="w",
            injected_privilege="admin",
        )


def test_interaction_envelope_needs_parts() -> None:
    with pytest.raises(ValidationError):
        v1.InteractionEnvelope(
            connector_instance="web",
            external_event_id="e1",
            window_ref="w1",
            parts=[],
        )


def test_origin_values_are_distinct_sources() -> None:
    # 六种来源必须互不混写；这是“经历有来源”不变量的契约侧保证。
    assert {o.value for o in v1.Origin} == {
        "simulated",
        "user_statement",
        "external_observation",
        "verified_source",
        "tool_receipt",
        "model_inference",
    }


def test_visibility_set_frozen() -> None:
    assert {v.value for v in v1.Visibility} == {
        "actor_private",
        "subject_private",
        "window_private",
        "group_scope",
        "public",
        "admin_only",
    }


def test_delivery_state_has_uncertain_and_unsupported() -> None:
    states = {s.value for s in v1.DeliveryState}
    assert "uncertain" in states and "unsupported" in states


def test_memory_proposal_requires_evidence() -> None:
    with pytest.raises(ValidationError):
        v1.MemoryProposal(
            proposal_id="mp_1",
            kind="assertion",
            text="用户喜欢下雨天",
            origin=v1.Origin.user_statement,
            source_event_ids=[],
            visibility=v1.Visibility.subject_private,
        )


def test_error_body_retriable_is_explicit() -> None:
    body = v1.ErrorBody(code=v1.ErrorCode.provider_timeout, message="超时", retriable=True)
    assert body.retriable is True
    # 版本号只在最外层 ErrorResponse 出现一次，避免响应体里重复嵌套
    response = v1.ErrorResponse(error=body)
    assert response.schema_version == "v1"
    assert "schema_version" not in body.model_dump()


def test_session_and_message_serialize_roundtrip() -> None:
    now = datetime.now(UTC)
    msg = v1.ChatMessage(
        message_id="msg_1",
        turn_id="turn_1",
        role="actor",
        text="嗯，我在。",
        created_at=now,
        sequence=2,
    )
    restored = v1.ChatMessage.model_validate_json(msg.model_dump_json())
    assert restored == msg


def test_exported_schemas_match_source() -> None:
    """导出的 JSON Schema 必须与契约源码同步（防止手改 schema 漂移）。"""
    index_path = ROOT / "contracts" / "v1" / "index.json"
    if not index_path.exists():
        pytest.skip("contracts/v1 尚未导出")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert index["schema_version"] == SCHEMA_VERSION
    for name in index["contracts"]:
        model = getattr(v1, name)
        on_disk = json.loads(
            (ROOT / "contracts" / "v1" / index["contracts"][name]).read_text(encoding="utf-8")
        )
        expected = model.model_json_schema()
        expected["$id"] = on_disk["$id"]
        expected["x-ling-schema-version"] = SCHEMA_VERSION
        assert on_disk == expected, f"{name} schema 已漂移，请运行 scripts/export_contracts.py"
