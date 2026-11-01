"""API 集成：`/v1/dashboard` 的「今天的安排」与 `/v1/commitments` 的默认可见集。

修前实测（dev 库，2026-10-31）：
- dashboard.agenda.plans 返回 2 条，start_at 都在 10-30（不是今天）
- dashboard.agenda.commitments 返回 2 条，state 分别为
  undeliverable / cancelled（都不是待履行）
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ling.domains.agenda.service import AgendaService
from ling.domains.goals.service import GoalsService
from ling.infra.db import session_scope
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

SHANGHAI = "Asia/Shanghai"


def _use_timezone(env: TestEnv, timezone: str) -> None:
    """替换 app 的进程级配置（路由通过 request.app.state.settings 读取）。"""
    env.app.state.settings = replace(env.settings, user_timezone=timezone)  # type: ignore[attr-defined]


async def _plan(title: str, start: datetime) -> str:
    async with session_scope() as session:
        view = await AgendaService(session, actor_id="actor_ling").propose_plan(
            kind="reading",
            title=title,
            start_at=start,
            end_at=start + timedelta(minutes=30),
        )
    assert view is not None
    return view.plan_id


async def _commitment(env: TestEnv, text: str) -> str:
    async with session_scope() as session:
        view = await GoalsService(session).create_commitment(
            subject_id=env.subject_id,
            text=text,
            due_at=datetime.now(UTC) + timedelta(hours=1),
        )
    return view.commitment_id


async def _agenda_section(env: TestEnv) -> dict:
    response = await env.client.get("/v1/dashboard")
    assert response.status_code == 200, response.text
    return response.json()["sections"]["agenda"]


async def test_dashboard_only_returns_today_plans(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「今天的安排」只含用户本地时区当天的日程；start_at 字段仍在。"""
    _use_timezone(logged_in, SHANGHAI)
    monkeypatch.setattr("ling.api.routes.dashboard.local_today", lambda tz: date(2026, 10, 31))
    today_a = await _plan("今天上午", datetime(2026, 10, 31, 1, 0, tzinfo=UTC))
    today_b = await _plan("今天晚上", datetime(2026, 10, 31, 12, 0, tzinfo=UTC))
    yesterday = await _plan("昨天", datetime(2026, 10, 30, 12, 0, tzinfo=UTC))
    tomorrow = await _plan("明天", datetime(2026, 11, 1, 12, 0, tzinfo=UTC))

    response = await logged_in.client.get("/v1/dashboard")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["timezone"] == SHANGHAI
    section = body["sections"]["agenda"]
    assert section["status"] == "ok", section
    plans = section["data"]["plans"]
    got = {p["plan_id"] for p in plans}
    assert got == {today_a, today_b}
    assert yesterday not in got and tomorrow not in got
    # 前端靠 start_at 显示日期并判断"不是今天"；字段与语义必须保持。
    for plan in plans:
        parsed = datetime.fromisoformat(plan["start_at"])
        assert parsed.tzinfo is not None
        assert parsed.astimezone(ZoneInfo(SHANGHAI)).date() == date(2026, 10, 31)
        assert plan["end_at"]


async def test_dashboard_hides_inactive_commitments(logged_in: TestEnv) -> None:
    """dashboard 的承诺只含 active；已取消/送不出去不再冒充待履行。"""
    active = await _commitment(logged_in, "还欠着的事")
    cancelled = await _commitment(logged_in, "已取消的事")
    async with session_scope() as session:
        await GoalsService(session).cancel(cancelled)
    undeliverable = await _commitment(logged_in, "送不出去的事")
    async with session_scope() as session:
        await GoalsService(session).mark_delivery(
            undeliverable, operation_id=None, delivery_state="unsupported"
        )

    section = await _agenda_section(logged_in)
    assert section["status"] == "ok", section
    got = {c["commitment_id"] for c in section["data"]["commitments"]}
    assert got == {active}
    assert all(c["state"] == "active" for c in section["data"]["commitments"])


async def test_dashboard_agenda_is_empty_when_only_history(logged_in: TestEnv) -> None:
    """今天没有日程、没有待履行承诺、没有目标 → empty，而不是拿历史凑数。"""
    cancelled = await _commitment(logged_in, "已取消的事")
    async with session_scope() as session:
        await GoalsService(session).cancel(cancelled)

    section = await _agenda_section(logged_in)
    assert section["status"] == "empty", section


async def test_commitments_api_filters_and_exposes_state_filter(logged_in: TestEnv) -> None:
    active = await _commitment(logged_in, "待履行")
    cancelled = await _commitment(logged_in, "已取消")
    async with session_scope() as session:
        await GoalsService(session).cancel(cancelled)
    undeliverable = await _commitment(logged_in, "送不出去")
    async with session_scope() as session:
        await GoalsService(session).mark_delivery(
            undeliverable, operation_id=None, delivery_state="unsupported"
        )

    default = await logged_in.client.get("/v1/commitments")
    assert default.status_code == 200, default.text
    body = default.json()
    assert body["state_filter"] == "active"
    assert [c["commitment_id"] for c in body["commitments"]] == [active]

    history = await logged_in.client.get("/v1/commitments?state=cancelled")
    assert history.status_code == 200, history.text
    body = history.json()
    assert body["state_filter"] == "cancelled"
    assert [c["commitment_id"] for c in body["commitments"]] == [cancelled]

    everything = await logged_in.client.get("/v1/commitments?state=all")
    assert everything.status_code == 200, everything.text
    body = everything.json()
    assert body["state_filter"] == "all"
    assert {c["commitment_id"] for c in body["commitments"]} == {
        active,
        cancelled,
        undeliverable,
    }


async def test_commitments_api_rejects_unknown_state(logged_in: TestEnv) -> None:
    response = await logged_in.client.get("/v1/commitments?state=canceled")
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["code"] == "invalid_request", body
