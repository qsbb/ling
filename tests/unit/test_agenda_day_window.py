"""「今天」的日界：本地时区、跨午夜、DST、左闭右开边界（纯逻辑，无数据库）。

这些函数决定仪表盘「今天的安排」到底包含哪些日程。它们很容易写成
"UTC 日期"或"start + 24h"——前者在东八区会把 00:00-08:00 算成昨天，
后者在 DST 当天会把日界挪到 01:00/23:00。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from ling.domains.agenda.service import (
    local_day_bounds,
    local_today,
    resolve_timezone,
)


def test_invalid_timezone_falls_back_to_utc() -> None:
    """非法/缺失时区退回 UTC，绝不猜服务器本地时区。"""
    assert str(resolve_timezone(None)) == "UTC"
    assert str(resolve_timezone("Not/AZone")) == "UTC"
    assert str(resolve_timezone("Asia/Shanghai")) == "Asia/Shanghai"


def test_shanghai_day_bounds_are_utc_shifted() -> None:
    """上海 10-31 的一天 = UTC [10-30T16:00, 10-31T16:00)。"""
    start, end = local_day_bounds(date(2026, 10, 31), "Asia/Shanghai")
    assert start == datetime(2026, 10, 30, 16, 0, tzinfo=UTC)
    assert end == datetime(2026, 10, 31, 16, 0, tzinfo=UTC)


def test_utc_is_used_only_when_no_timezone_configured() -> None:
    start, end = local_day_bounds(date(2026, 10, 31), None)
    assert start == datetime(2026, 10, 31, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 11, 1, 0, 0, tzinfo=UTC)


def test_interval_is_left_closed_right_open() -> None:
    """当天 00:00:00 属于今天，次日 00:00:00 属于明天，边界只落一次。"""
    day = date(2026, 10, 31)
    start, end = local_day_bounds(day, "Asia/Shanghai")
    inside_first = start
    inside_last = end.replace(microsecond=0) - timedelta(seconds=1)
    assert start <= inside_first < end
    assert start <= inside_last < end
    assert not (start <= start - timedelta(microseconds=1) < end)
    assert not (start <= end < end)


def test_cross_midnight_belongs_to_start_day() -> None:
    """23:30 开始、次日 00:30 结束的计划属于**开始那天**。"""
    start, end = local_day_bounds(date(2026, 10, 31), "Asia/Shanghai")
    starts_today = datetime(2026, 10, 31, 15, 30, tzinfo=UTC)  # 上海 10-31 23:30
    starts_yesterday = datetime(2026, 10, 30, 15, 30, tzinfo=UTC)  # 上海 10-30 23:30
    assert start <= starts_today < end
    assert not (start <= starts_yesterday < end)


def test_dst_spring_forward_day_is_23_hours() -> None:
    """2026-03-08 纽约只有 23 小时：日界必须是本地 00:00，不是 +24h。"""
    start, end = local_day_bounds(date(2026, 3, 8), "America/New_York")
    assert start == datetime(2026, 3, 8, 5, 0, tzinfo=UTC)
    assert end == datetime(2026, 3, 9, 4, 0, tzinfo=UTC)
    assert end - start == timedelta(hours=23)
    # 当天最早与最晚的一秒都在窗内；两侧边界都在窗外。
    assert start <= datetime(2026, 3, 8, 5, 0, tzinfo=UTC) < end
    assert start <= datetime(2026, 3, 9, 3, 59, 59, tzinfo=UTC) < end
    assert not (start <= datetime(2026, 3, 9, 4, 0, tzinfo=UTC) < end)
    assert not (start <= datetime(2026, 3, 8, 4, 59, 59, tzinfo=UTC) < end)


def test_dst_fall_back_day_is_25_hours() -> None:
    """2026-11-01 纽约有 25 小时；凌晨重复的一小时都算同一天。"""
    start, end = local_day_bounds(date(2026, 11, 1), "America/New_York")
    assert start == datetime(2026, 11, 1, 4, 0, tzinfo=UTC)
    assert end == datetime(2026, 11, 2, 5, 0, tzinfo=UTC)
    assert end - start == timedelta(hours=25)
    # 01:30 EDT 与 01:30 EST 是同一个本地日的两个瞬间，都在窗内。
    assert start <= datetime(2026, 11, 1, 5, 30, tzinfo=UTC) < end
    assert start <= datetime(2026, 11, 1, 6, 30, tzinfo=UTC) < end


@pytest.mark.parametrize(
    ("now", "timezone", "expected"),
    [
        # 上海 2026-10-31 00:30 = UTC 10-30 16:30：UTC 日期还是 30，本地已是 31。
        (datetime(2026, 10, 30, 16, 30, tzinfo=UTC), "Asia/Shanghai", date(2026, 10, 31)),
        (datetime(2026, 10, 30, 16, 30, tzinfo=UTC), "UTC", date(2026, 10, 30)),
        (datetime(2026, 10, 31, 15, 59, 59, tzinfo=UTC), "Asia/Shanghai", date(2026, 10, 31)),
        (datetime(2026, 10, 31, 16, 0, tzinfo=UTC), "Asia/Shanghai", date(2026, 11, 1)),
    ],
)
def test_local_today_follows_configured_timezone(now, timezone, expected) -> None:  # noqa: ANN001
    assert local_today(timezone, now=now) == expected
