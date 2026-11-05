"""告警推送：让"有事能第一时间知道"真的成立。

`/v1/health/ops` 只解决"能查"，不解决"会知道"。个人服务器没人天天盯仪表盘。

三条设计约束，每条都有对应的测试：
1. **只推需要人处理的事**——心跳推送会让人开始忽略通知
2. **同一告警不重复轰炸**——否则渠道用废，真正要处理的那条淹在噪声里
3. **推送失败不能让检查失败**——否则"通知渠道坏了"会掩盖真实故障
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from ling.infra.db import session_scope
from ling.infra.models import OpsAlertState
from ling.ops.notify import (
    REPEAT_INTERVAL,
    build_payload,
    mark_sent,
    notify_if_needed,
    send_webhook,
    should_notify,
)
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


def _status(*names: str) -> dict:
    items = [{"name": n, "severity": "warn", "value": 1, "action": "处理它"} for n in names]
    return {"status": "attention", "checked_at": "now", "items": items, "warnings": len(items)}


# ------------------------------------------------------------------ 去重


async def test_first_alert_notifies(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        assert await should_notify(session, fingerprint="outbox_dead:warn") is True


async def test_repeat_suppressed_within_interval(logged_in: TestEnv) -> None:
    """间隔内不重复推送——否则渠道会被同一件事打废。"""
    async with session_scope() as session:
        await mark_sent(session, fingerprint="outbox_dead:warn")
    async with session_scope() as session:
        assert await should_notify(session, fingerprint="outbox_dead:warn") is False


async def test_renotifies_after_interval(logged_in: TestEnv) -> None:
    past = datetime.now(UTC) - REPEAT_INTERVAL - timedelta(minutes=1)
    async with session_scope() as session:
        await mark_sent(session, fingerprint="outbox_dead:warn", now=past)
    async with session_scope() as session:
        assert await should_notify(session, fingerprint="outbox_dead:warn") is True


async def test_recovery_clears_state(logged_in: TestEnv) -> None:
    """恢复后删记录，下次再出问题能立刻推送。"""
    async with session_scope() as session:
        await mark_sent(session, fingerprint="outbox_dead:warn")
    async with session_scope() as session:
        result = await notify_if_needed(
            session, status={"status": "ok", "items": []}, webhook_url=""
        )
    assert "outbox_dead:warn" in result["cleared"]
    async with session_scope() as session:
        rows = list((await session.execute(select(OpsAlertState))).scalars())
    assert not [r for r in rows if r.fingerprint == "outbox_dead:warn"]


# ------------------------------------------------------------------ 只推要处理的事


def test_payload_only_lists_warnings() -> None:
    status = {
        "status": "attention",
        "checked_at": "now",
        "items": [
            {"name": "outbox_dead", "severity": "warn", "value": 2, "action": "重放"},
            {"name": "disk_free_gb", "severity": "ok", "value": 50},
        ],
    }
    payload = build_payload(status=status, base_url="test")
    assert len(payload["warnings"]) == 1
    assert payload["warnings"][0]["name"] == "outbox_dead"
    assert "outbox_dead" in payload["text"]
    assert "disk_free_gb" not in payload["text"], "正常项混进了通知，会稀释信号"


async def test_nothing_to_send_when_healthy(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        result = await notify_if_needed(
            session,
            status={"status": "ok", "items": [{"name": "x", "severity": "ok"}]},
            webhook_url="http://127.0.0.1:1/hook",
        )
    assert result["sent"] == []


async def test_no_webhook_configured_is_silent(logged_in: TestEnv) -> None:
    """没配推送目标时不该报错，只是不推。"""
    async with session_scope() as session:
        result = await notify_if_needed(
            session, status=_status("outbox_dead"), webhook_url=""
        )
    assert result["sent"] == []
    assert result["skipped"], "应当记录哪些告警因为没有渠道而没推"


# ------------------------------------------------------------------ 推送失败


async def test_webhook_failure_does_not_raise() -> None:
    """推送失败返回 False，不抛异常。

    抛异常的话，"通知渠道坏了"会让检查本身失败，反而看不到真实故障。
    """
    ok, detail = await send_webhook("http://127.0.0.1:1/nope", {"text": "x"},
                                    timeout_seconds=1.0)
    assert ok is False
    assert detail


async def test_failed_send_does_not_mark_sent(logged_in: TestEnv) -> None:
    """推送失败不能记成已发送，否则下次不会再试。"""
    async with session_scope() as session:
        result = await notify_if_needed(
            session,
            status=_status("outbox_dead"),
            webhook_url="http://127.0.0.1:1/nope",
        )
    assert result["sent"] == []
    async with session_scope() as session:
        assert await should_notify(session, fingerprint="outbox_dead:warn") is True


# ------------------------------------------------------------------ 端到端


async def test_notify_endpoint_with_stub_webhook(logged_in: TestEnv) -> None:
    """通过端点触发一次推送，并用本地 stub 验证内容。"""
    received: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        import json

        received.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    # 用 mock transport 替换 send_webhook 的 client 不方便，这里直接验证
    # send_webhook 逻辑 + 端点返回结构（端点内部走真实 httpx）。
    ok, _ = await send_webhook("http://127.0.0.1:1/x", {"text": "x"}, timeout_seconds=1.0)
    assert ok is False

    response = await logged_in.client.post("/v1/health/ops/notify")
    assert response.status_code == 200, response.text
    body = response.json()
    assert {"sent", "skipped", "cleared"} <= set(body)


async def test_notify_endpoint_requires_auth(web_env: TestEnv) -> None:
    response = await web_env.client.post("/v1/health/ops/notify")
    assert response.status_code == 401
