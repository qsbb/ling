"""审计日志与安全日志：谁改了什么、谁被拒了。

两类日志分开命名空间，字段固定，且**绝不携带内容**。
"""

from __future__ import annotations

import logging

import pytest

from ling.audit import audit_event, hash_account_ref, security_event

pytestmark = pytest.mark.w0_web

CANARY = "这段内容绝不该出现在审计日志里"


def _by_event(caplog: pytest.LogCaptureFixture, name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event_name", "") == name]


# ---------------------------------------------------------------- 纯函数层


def test_account_ref_is_hashed_not_plain() -> None:
    """外部账号标识只写哈希——安全日志长期留存，不该成为泄漏源。"""
    hashed = hash_account_ref("1483904397")
    assert hashed and len(hashed) == 16
    assert "1483904397" not in hashed
    # 同一个值稳定：两次算出来一致，才能用来"指代同一个人"
    assert hash_account_ref("1483904397") == hashed
    # 不同的值不撞
    assert hash_account_ref("1483904398") != hashed


def test_audit_event_shape(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="ling.audit"):
        audit_event(
            "memory_retracted",
            actor="user_1",
            target_type="memory",
            target_id="mem_1",
            reason_code="user_request",
        )
    record = _by_event(caplog, "audit_memory_retracted")[-1]
    attrs = record.attributes
    assert attrs["log_kind"] == "audit"
    assert attrs["action"] == "memory_retracted"
    assert attrs["outcome"] == "ok"
    assert attrs["actor"] == "user_1"
    assert attrs["target_id"] == "mem_1"


def test_security_event_hashes_the_account(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="ling.security"):
        security_event("inbound_denied", account_ref="1483904397", reason_code="not_in_whitelist")
    record = _by_event(caplog, "security_inbound_denied")[-1]
    attrs = record.attributes
    assert attrs["log_kind"] == "security"
    assert attrs["outcome"] == "denied"
    assert "account_ref_hash" in attrs
    # 原值不能出现
    assert "1483904397" not in str(attrs)


def test_audit_never_carries_content(caplog: pytest.LogCaptureFixture) -> None:
    """把内容塞进 attributes 也会被 Formatter 拦掉，但正确用法是压根不传。"""
    with caplog.at_level(logging.INFO, logger="ling.audit"):
        audit_event(
            "memory_forgotten",
            actor="user_1",
            target_type="subject",
            attributes={"retracted_count": 3, "content": CANARY},
        )
    from ling.logging_setup import JsonFormatter

    record = _by_event(caplog, "audit_memory_forgotten")[-1]
    line = JsonFormatter(service="ling", version="t", role="api").format(record)
    assert CANARY not in line
    assert '"retracted_count": 3' in line


# ---------------------------------------------------------------- 接线层


async def test_publishing_config_is_audited(logged_in, caplog) -> None:
    """发布配置要留痕：谁、哪个修订。"""
    draft = await logged_in.client.put("/v1/settings/draft", json={})
    revision = draft.json().get("revision")
    assert revision is not None

    with caplog.at_level(logging.INFO, logger="ling.audit"):
        published = await logged_in.client.post(
            "/v1/settings/publish", json={"revision": revision}
        )
    assert published.status_code == 200, published.text

    events = _by_event(caplog, "audit_config_published")
    assert events, [r.getMessage() for r in caplog.records]
    assert events[-1].attributes["target_id"]


async def test_retracting_memory_is_audited(logged_in, caplog) -> None:
    """撤回记忆要留痕，但只记 ID 与原因，不记内容。"""
    from ling.infra.db import session_scope
    from ling.infra.models import MemoryRow
    from ling.infra.security import new_id

    async with session_scope() as session:
        row = MemoryRow(
            id=new_id("mem"),
            subject_id=logged_in.subject_id,
            kind="assertion",
            text=CANARY,
            origin="user_statement",
            visibility="subject_private",
            confidence=1.0,
        )
        session.add(row)
        await session.commit()
        memory_id = row.id

    with caplog.at_level(logging.INFO, logger="ling.audit"):
        response = await logged_in.client.post(
            f"/v1/memory/{memory_id}/retract", json={"reason": "test"}
        )
    assert response.status_code == 200, response.text

    events = _by_event(caplog, "audit_memory_retracted")
    assert events
    dumped = str(events[-1].attributes)
    assert memory_id in dumped
    assert CANARY not in dumped


async def test_failed_login_is_a_security_event(web_env, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="ling.security"):
        response = await web_env.client.post(
            "/v1/auth/login", json={"username": "tester", "password": "wrong-password"}
        )
    assert response.status_code == 401

    events = _by_event(caplog, "security_auth_failed")
    assert events
    attrs = events[-1].attributes
    assert attrs["reason_code"] == "bad_credentials"
    # 口令绝不能出现在安全日志里
    assert "wrong-password" not in str(attrs)
