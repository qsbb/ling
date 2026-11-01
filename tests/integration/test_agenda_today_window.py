"""`AgendaService.upcoming(on_date=...)`：按本地日历日切片的服务层测试。

覆盖：
- 左闭右开边界（当天 00:00 在内、次日 00:00 在外）
- 跨午夜按 `start_at` 归日
- DST 23 小时 / 25 小时的两天
- 不带 `on_date` 时仍保持"接下来 N 件"的旧语义（worker / 自我视图依赖它）
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from ling.domains.agenda.service import AgendaService
from ling.infra.db import session_scope
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"


def _svc(session):  # noqa: ANN001, ANN202
    return AgendaService(session, actor_id=ACTOR)


async def _plan(title: str, start: datetime) -> str:
    async with session_scope() as session:
        view = await _svc(session).propose_plan(
            kind="reading",
            title=title,
            start_at=start,
            end_at=start + timedelta(minutes=30),
        )
    assert view is not None, "计划数超过 max_live_plans，测试数据准备失败"
    return view.plan_id


async def test_on_date_is_left_closed_right_open(logged_in: TestEnv) -> None:
    """上海 10-31 只含 [10-30T16:00Z, 10-31T16:00Z) 的开始时间。"""
    day = date(2026, 10, 31)
    first = await _plan("当天零点整", datetime(2026, 10, 30, 16, 0, tzinfo=UTC))
    last = await _plan("当天最后一秒", datetime(2026, 10, 31, 15, 59, 59, tzinfo=UTC))
    before = await _plan("前一天最后一秒", datetime(2026, 10, 30, 15, 59, 59, tzinfo=UTC))
    after = await _plan("次日零点整", datetime(2026, 10, 31, 16, 0, tzinfo=UTC))

    async with session_scope() as session:
        got = await _svc(session).upcoming(on_date=day, timezone="Asia/Shanghai")
    ids = {p.plan_id for p in got}
    assert ids == {first, last}
    assert before not in ids and after not in ids


async def test_cross_midnight_is_attributed_to_start_day(logged_in: TestEnv) -> None:
    """23:30 开始、次日 00:30 结束属于开始那天；昨天开始的不算今天。"""
    day = date(2026, 10, 31)
    crosses_into_tomorrow = await _plan(
        "今晚跨午夜",
        datetime(2026, 10, 31, 15, 30, tzinfo=UTC),  # 上海 10-31 23:30
    )
    crossed_from_yesterday = await _plan(
        "昨晚跨午夜",
        datetime(2026, 10, 30, 15, 30, tzinfo=UTC),  # 上海 10-30 23:30
    )

    async with session_scope() as session:
        got = await _svc(session).upcoming(on_date=day, timezone="Asia/Shanghai")
    ids = {p.plan_id for p in got}
    assert crosses_into_tomorrow in ids
    assert crossed_from_yesterday not in ids


async def test_dst_spring_forward_23_hour_day(logged_in: TestEnv) -> None:
    """纽约 2026-03-08：日界是 UTC 05:00 到次日 04:00。"""
    day = date(2026, 3, 8)
    first = await _plan("DST 当天零点", datetime(2026, 3, 8, 5, 0, tzinfo=UTC))
    last = await _plan("DST 当天最后一秒", datetime(2026, 3, 9, 3, 59, 59, tzinfo=UTC))
    before = await _plan("DST 前一天最后一秒", datetime(2026, 3, 8, 4, 59, 59, tzinfo=UTC))
    after = await _plan("DST 次日零点", datetime(2026, 3, 9, 4, 0, tzinfo=UTC))

    async with session_scope() as session:
        got = await _svc(session).upcoming(on_date=day, timezone="America/New_York")
    ids = {p.plan_id for p in got}
    assert ids == {first, last}
    assert before not in ids and after not in ids


async def test_dst_fall_back_25_hour_day(logged_in: TestEnv) -> None:
    """纽约 2026-11-01：日界是 UTC 04:00 到次日 05:00，重复的 01:xx 都算今天。"""
    day = date(2026, 11, 1)
    first_pass = await _plan("DST 第一个 01:30", datetime(2026, 11, 1, 5, 30, tzinfo=UTC))
    second_pass = await _plan("DST 第二个 01:30", datetime(2026, 11, 1, 6, 30, tzinfo=UTC))
    before = await _plan("DST 前一天最后一秒", datetime(2026, 11, 1, 3, 59, 59, tzinfo=UTC))
    after = await _plan("DST 次日零点", datetime(2026, 11, 2, 5, 0, tzinfo=UTC))

    async with session_scope() as session:
        got = await _svc(session).upcoming(on_date=day, timezone="America/New_York")
    ids = {p.plan_id for p in got}
    assert ids == {first_pass, second_pass}
    assert before not in ids and after not in ids


async def test_without_on_date_keeps_unbounded_semantics(logged_in: TestEnv) -> None:
    """不带 on_date 仍是"接下来 N 件"：worker / 自我视图靠它跨日看计划。"""
    yesterday = await _plan("昨天没做完的", datetime(2026, 10, 30, 1, 0, tzinfo=UTC))
    tomorrow = await _plan("明天要做的", datetime(2026, 11, 1, 1, 0, tzinfo=UTC))

    async with session_scope() as session:
        got = await _svc(session).upcoming()
    ids = {p.plan_id for p in got}
    assert {yesterday, tomorrow} <= ids


async def test_invalid_configured_timezone_falls_back_to_utc(logged_in: TestEnv) -> None:
    """时区配错时按 UTC 切日，而不是让整块 agenda 变成 unavailable。"""
    day = date(2026, 10, 31)
    in_utc_day = await _plan("UTC 当天的计划", datetime(2026, 10, 31, 12, 0, tzinfo=UTC))
    not_utc_day = await _plan("UTC 次日的计划", datetime(2026, 11, 1, 1, 0, tzinfo=UTC))

    async with session_scope() as session:
        got = await _svc(session).upcoming(on_date=day, timezone="Not/AZone")
    ids = {p.plan_id for p in got}
    assert in_utc_day in ids
    assert not_utc_day not in ids
