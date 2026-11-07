"""她的一天（阶段 1）的**纯函数**闸门：日界、DST、时间轴校验。

这一组不碰数据库、不调模型——它们守的是最容易写错、也最难在集成里
一次覆盖全的东西：

- 计划日 ``[D 04:00, D+1 04:00)`` 在 DST 当天是 **23/25 小时**（不能用 +24h）；
- 不存在的本地时刻要顺延、重复的本地时刻取第一次（``fold=0``）；
- 时间轴左闭右开、允许空档、父子段有意重叠、叶段不许重叠、锚点不可移动；
- 模型输出只接受一个形状，任何越界/过去/未知地点/非分钟精度都整份拒收。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from ling.domains.agenda.timeline import (
    Anchor,
    OutlineValidationError,
    build_sleep_anchor,
    contains,
    display_day_for,
    display_day_window,
    local_minute_instant,
    overlaps,
    parse_outline_payload,
    plan_day_for,
    plan_day_length,
    plan_day_window,
    rule_outline,
    validate_outline,
)

NY = "America/New_York"
SHANGHAI = "Asia/Shanghai"


def _utc(hour: int, minute: int = 0, day: int = 7) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


# ---------------------------------------------------------------- 两个日


def test_plan_day_window_is_04_to_04_in_utc() -> None:
    start, end = plan_day_window(date(2026, 11, 7), SHANGHAI)
    # 上海 04:00 = UTC 前一日 20:00
    assert start == datetime(2026, 11, 6, 20, 0, tzinfo=UTC)
    assert end == datetime(2026, 11, 7, 20, 0, tzinfo=UTC)
    assert end - start == timedelta(hours=24)


def test_plan_day_is_23_hours_on_spring_forward() -> None:
    """2026-03-08 美东春季跳变：03-07 的计划日只有 23 小时。"""
    assert plan_day_length(date(2026, 3, 7), NY) == timedelta(hours=23)


def test_plan_day_is_25_hours_on_fall_back() -> None:
    assert plan_day_length(date(2026, 10, 31), NY) == timedelta(hours=25)


def test_plan_day_boundaries_use_local_wall_clock_not_plus_24h() -> None:
    """注意：+24h 会得到 04:00/05:00 的错误边界；这里必须是本地 04:00。"""
    for day in (date(2026, 3, 7), date(2026, 10, 31), date(2026, 6, 1)):
        start, end = plan_day_window(day, NY)
        local_start = start.astimezone(ZoneInfoNY())
        local_end = end.astimezone(ZoneInfoNY())
        assert (local_start.hour, local_start.minute) == (4, 0), local_start
        assert (local_end.hour, local_end.minute) == (4, 0), local_end


def ZoneInfoNY():  # noqa: N802 - 测试辅助
    from zoneinfo import ZoneInfo

    return ZoneInfo(NY)


def test_plan_day_for_boundary_is_0400_local() -> None:
    zone = ZoneInfoNY()
    assert plan_day_for(datetime(2026, 11, 7, 3, 59, tzinfo=zone), NY) == date(
        2026, 11, 6
    )
    assert plan_day_for(datetime(2026, 11, 7, 4, 0, tzinfo=zone), NY) == date(
        2026, 11, 7
    )


def test_plan_day_for_uses_given_timezone() -> None:
    """同一瞬时在不同规范时区可能属于不同计划日——这正是"时区变更"要处理的。"""
    instant = datetime(2026, 11, 7, 0, 30, tzinfo=ZoneInfoNY())  # NY 00:30
    assert plan_day_for(instant, NY) == date(2026, 11, 6)
    # 同一瞬时的上海本地时间是 11-07 13:30 → 属于 11-07 计划日
    assert plan_day_for(instant, SHANGHAI) == date(2026, 11, 7)


def test_display_day_window_uses_local_midnight() -> None:
    start, end = display_day_window(date(2026, 11, 7), SHANGHAI)
    assert start == datetime(2026, 11, 6, 16, 0, tzinfo=UTC)
    assert end - start == timedelta(hours=24)
    assert display_day_for(start, SHANGHAI) == date(2026, 11, 7)
    assert display_day_for(end, SHANGHAI) == date(2026, 11, 8)


def test_display_day_is_23_or_25_hours_on_dst() -> None:
    spring = display_day_window(date(2026, 3, 8), NY)
    fall = display_day_window(date(2026, 11, 1), NY)
    assert spring[1] - spring[0] == timedelta(hours=23)
    assert fall[1] - fall[0] == timedelta(hours=25)


# ---------------------------------------------------------------- DST 的本地时刻


def test_nonexistent_local_time_shifts_to_next_valid_minute() -> None:
    """春季跳变里不存在的 02:30 → 顺延到 03:00（不是报错，也不是照抄）。"""
    instant = local_minute_instant(date(2026, 3, 8), 2 * 60 + 30, NY)
    back = instant.astimezone(ZoneInfoNY())
    assert (back.hour, back.minute) == (3, 0), back


def test_ambiguous_local_time_uses_fold_zero() -> None:
    """秋季回拨里重复的 01:30 → 取第一次出现的瞬时（fold=0，EDT）。"""
    instant = local_minute_instant(date(2026, 11, 1), 90, NY)
    back = instant.astimezone(ZoneInfoNY())
    assert (back.hour, back.minute) == (1, 30)
    assert back.utcoffset() == timedelta(hours=-4), "应当取第一次（EDT）"


def test_arbitrary_minute_is_allowed() -> None:
    """粒度 1 分钟：03:31 这种非整点也必须能精确落成 UTC。"""
    instant = local_minute_instant(date(2026, 11, 7), 3 * 60 + 31, SHANGHAI)
    assert instant == datetime(2026, 11, 6, 19, 31, tzinfo=UTC)


# ---------------------------------------------------------------- 区间语义


def test_half_open_overlap_endpoints_touch_but_do_not_overlap() -> None:
    a = (_utc(8), _utc(9))
    b = (_utc(9), _utc(10))
    assert overlaps(*a, *b) is False, "端点相接不算重叠（左闭右开）"
    assert overlaps(_utc(8), _utc(10), _utc(9), _utc(11)) is True


def test_child_must_fit_inside_parent() -> None:
    assert contains(_utc(8), _utc(12), _utc(9), _utc(10)) is True
    assert contains(_utc(8), _utc(10), _utc(9), _utc(11)) is False
    assert contains(_utc(8), _utc(12), _utc(8), _utc(12)) is True


# ---------------------------------------------------------------- 睡眠锚点


def test_sleep_anchor_crosses_midnight() -> None:
    anchor = build_sleep_anchor(
        date(2026, 11, 7), timezone=SHANGHAI, rest_minute=23 * 60, wake_minute=8 * 60
    )
    assert anchor is not None
    start, end = plan_day_window(date(2026, 11, 7), SHANGHAI)
    # 23:00 属于 D 夜，次日 08:00 已越过 D+1 04:00 的日界
    assert anchor.start_at > start
    assert anchor.end_at > end


def test_sleep_anchor_skipped_when_rest_before_plan_start() -> None:
    """rest=02:00 属于 D−1 的夜：D 不再补一个"已经睡过"。"""
    assert (
        build_sleep_anchor(
            date(2026, 11, 7), timezone=SHANGHAI, rest_minute=2 * 60, wake_minute=10 * 60
        )
        is None
    )


def test_sleep_anchor_skipped_when_window_is_empty() -> None:
    assert (
        build_sleep_anchor(
            date(2026, 11, 7),
            timezone=SHANGHAI,
            rest_minute=23 * 60,
            wake_minute=23 * 60,
        )
        is None
    )


# ---------------------------------------------------------------- 模型输出校验


def _segment(
    start: str,
    end: str,
    *,
    kind: str = "work",
    title: str = "写代码",
    place_id: object = None,
    **extra: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": kind,
        "title": title,
        "start_at": start,
        "end_at": end,
        "place_id": place_id,
        "flexibility": "flexible",
    }
    payload.update(extra)
    return payload


def _validate(segments: list[object], *, anchors: list[Anchor] | None = None, places=()):
    start, end = plan_day_window(date(2026, 11, 7), SHANGHAI)
    return validate_outline(
        segments,
        window_start=start,
        window_end=end,
        not_before=start + timedelta(hours=1),
        anchors=anchors or [],
        valid_place_ids=set(places),
    )


def test_valid_outline_accepts_gaps_and_reorders() -> None:
    start, _ = plan_day_window(date(2026, 11, 7), SHANGHAI)
    local = lambda hour, minute=0: (  # noqa: E731
        start + timedelta(hours=hour, minutes=minute)
    ).isoformat()
    blocks = _validate(
        [
            _segment(local(3), local(4), title="下午写代码"),
            _segment(local(1), local(2), title="吃早饭"),
        ]
    )
    assert [block.title for block in blocks] == ["下午写代码", "吃早饭"]
    assert blocks[1].start_at < blocks[0].start_at


def test_outline_rejects_sibling_overlap() -> None:
    start, _ = plan_day_window(date(2026, 11, 7), SHANGHAI)
    t = lambda hour, minute=0: (start + timedelta(hours=hour, minutes=minute)).isoformat()  # noqa: E731
    with pytest.raises(OutlineValidationError) as excinfo:
        _validate(
            [
                _segment(t(2), t(4)),
                _segment(t(3), t(5)),
            ]
        )
    assert any("重叠" in item for item in excinfo.value.problems)


def test_outline_rejects_past_and_out_of_window() -> None:
    start, end = plan_day_window(date(2026, 11, 7), SHANGHAI)
    with pytest.raises(OutlineValidationError) as excinfo:
        validate_outline(
            [_segment(start.isoformat(), (start + timedelta(minutes=30)).isoformat())],
            window_start=start,
            window_end=end,
            not_before=start + timedelta(hours=2),
            anchors=[],
            valid_place_ids=set(),
        )
    assert any("过去" in item or "可变起点" in item for item in excinfo.value.problems)

    with pytest.raises(OutlineValidationError) as excinfo:
        _validate([_segment(end.isoformat(), (end + timedelta(minutes=30)).isoformat())])
    assert any("越出" in item for item in excinfo.value.problems)


def test_outline_rejects_anchor_overlap() -> None:
    start, _ = plan_day_window(date(2026, 11, 7), SHANGHAI)
    t = lambda hour, minute=0: (start + timedelta(hours=hour, minutes=minute)).isoformat()  # noqa: E731
    sleep = Anchor(
        kind="sleep",
        start_at=start + timedelta(hours=19),
        end_at=start + timedelta(hours=28),
        title="睡觉",
    )
    with pytest.raises(OutlineValidationError) as excinfo:
        _validate([_segment(t(20), t(21))], anchors=[sleep])
    assert any("不可移动锚点" in item for item in excinfo.value.problems)


def test_outline_rejects_unknown_place_and_kind() -> None:
    start, _ = plan_day_window(date(2026, 11, 7), SHANGHAI)
    t = lambda hour: (start + timedelta(hours=hour)).isoformat()  # noqa: E731
    with pytest.raises(OutlineValidationError) as excinfo:
        _validate([_segment(t(2), t(3), place_id="place_ghost")])
    assert any("未知地点" in item for item in excinfo.value.problems)

    with pytest.raises(OutlineValidationError) as excinfo:
        _validate([_segment(t(2), t(3), kind="sleep")])
    assert any("白名单" in item for item in excinfo.value.problems)


def test_outline_rejects_non_minute_and_naive_times() -> None:
    start, _ = plan_day_window(date(2026, 11, 7), SHANGHAI)
    t = (start + timedelta(hours=2)).isoformat()
    with pytest.raises(OutlineValidationError) as excinfo:
        _validate([_segment(t, (start + timedelta(hours=2, seconds=30)).isoformat())])
    assert any("分钟" in item for item in excinfo.value.problems)

    with pytest.raises(OutlineValidationError) as excinfo:
        _validate([_segment(t, "2026-11-07T12:00:00")])
    assert any("时区偏移" in item for item in excinfo.value.problems)


def test_outline_rejects_empty_and_extra_fields() -> None:
    with pytest.raises(OutlineValidationError) as excinfo:
        _validate([])
    assert any("为空" in item for item in excinfo.value.problems)

    start, _ = plan_day_window(date(2026, 11, 7), SHANGHAI)
    t = lambda hour: (start + timedelta(hours=hour)).isoformat()  # noqa: E731
    with pytest.raises(OutlineValidationError) as excinfo:
        _validate([_segment(t(2), t(3), completed=True)])
    assert any("未定义字段" in item for item in excinfo.value.problems)


def test_parse_payload_is_strict() -> None:
    assert parse_outline_payload('{"segments": []}') == []
    with pytest.raises(OutlineValidationError):
        parse_outline_payload("```json\n{\"segments\": []}\n```")
    with pytest.raises(OutlineValidationError):
        parse_outline_payload('{"segments": [], "note": "hi"}')
    with pytest.raises(OutlineValidationError):
        parse_outline_payload("[]")
    with pytest.raises(OutlineValidationError):
        parse_outline_payload("")


# ---------------------------------------------------------------- 规则大纲


def test_rule_outline_only_uses_anchors_and_skips_past() -> None:
    start, end = plan_day_window(date(2026, 11, 7), SHANGHAI)
    sleep = build_sleep_anchor(
        date(2026, 11, 7), timezone=SHANGHAI, rest_minute=23 * 60, wake_minute=8 * 60
    )
    assert sleep is not None
    past_commitment = Anchor(
        kind="commitment",
        start_at=start + timedelta(minutes=10),
        end_at=start + timedelta(minutes=70),
        title="约定：早就过去了",
    )
    future_commitment = Anchor(
        kind="commitment",
        start_at=start + timedelta(hours=6),
        end_at=start + timedelta(hours=6, minutes=60),
        title="约定：下午见",
    )
    drafts = rule_outline(
        anchors=[past_commitment, future_commitment, sleep],
        window_start=start,
        window_end=end,
        not_before=start + timedelta(hours=2),
    )
    titles = [draft.title for draft in drafts]
    assert "约定：早就过去了" not in titles
    assert "约定：下午见" in titles
    assert "睡觉" in titles
    # 规则大纲不编可选活动：只会有锚点
    assert all(draft.anchor_kind for draft in drafts)
    # 锚点是不可移动段：不是叶段
    assert all(draft.is_leaf is False for draft in drafts)
