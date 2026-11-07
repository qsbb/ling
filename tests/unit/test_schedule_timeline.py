"""她的一天（阶段 1.5）的**纯函数**闸门：日界、生成时刻、DST、时间轴校验。

这一组不碰数据库、不调模型——它们守的是最容易写错、也最难在集成里
一次覆盖全的东西：

- 计划日是本地 ``[D 00:00, D+1 00:00)``，在 DST 当天是 **23/25 小时**（不能用 +24h）；
- 生成到期时刻与日界分离：D 的大纲在 **D−1 本地 21:00** 到期（``outline_due_at``）；
- catch-up 三档判据（``outline_due_decision``）：21:00 到期 / 醒前补跑 / 醒后晚到；
- 不存在的本地时刻要顺延、重复的本地时刻取第一次（``fold=0``）；
- 睡眠锚点是"作息非清醒区间 ∩ D 的午夜窗口"，至多两段且都完全归属 D；
- 锚点冲突由**代码**裁决（``resolve_anchor_conflicts``）：承诺裁到 D 内、
  同级硬约定两边都留、承诺压睡眠时保留承诺并标原因码；
- 时间轴左闭右开、允许空档、父子段有意重叠、叶段不许重叠、锚点不可移动；
- 模型输出只接受一个形状，任何越界/过去/未知地点/非分钟精度都整份拒收。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ling.domains.agenda.timeline import (
    MIN_SLEEP_MINUTES,
    PHASE_CATCHUP,
    PHASE_DUE,
    PHASE_LATE,
    REASON_NEEDS_REVIEW,
    REASON_ROUTINE_CONFLICT,
    Anchor,
    OutlineValidationError,
    RoutineSnapshot,
    contains,
    display_day_for,
    display_day_window,
    local_minute_instant,
    outline_due_at,
    outline_due_decision,
    overlaps,
    parse_outline_payload,
    plan_day_for,
    plan_day_length,
    plan_day_window,
    resolve_anchor_conflicts,
    rule_outline,
    sleep_anchors_for_day,
    validate_outline,
)

NY = "America/New_York"
SHANGHAI = "Asia/Shanghai"


def _utc(hour: int, minute: int = 0, day: int = 7) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


def _routine(wake_hour: int, rest_hour: int, *, minute: int = 0) -> RoutineSnapshot:
    """作息快照：wake/rest 是规范时区的本地分钟。"""
    return RoutineSnapshot(
        wake_minute=wake_hour * 60 + minute,
        rest_minute=rest_hour * 60 + minute,
        revision=1,
        timezone=SHANGHAI,
    )


# ---------------------------------------------------------------- 两个日


def test_plan_day_window_is_midnight_to_midnight_in_utc() -> None:
    start, end = plan_day_window(date(2026, 11, 7), SHANGHAI)
    # 上海 00:00 = UTC 前一日 16:00
    assert start == datetime(2026, 11, 6, 16, 0, tzinfo=UTC)
    assert end == datetime(2026, 11, 7, 16, 0, tzinfo=UTC)
    assert end - start == timedelta(hours=24)


def test_plan_day_is_23_hours_on_spring_forward() -> None:
    """2026-03-08 美东春季跳变：这一天的计划日只有 23 小时。"""
    assert plan_day_length(date(2026, 3, 8), NY) == timedelta(hours=23)


def test_plan_day_is_25_hours_on_fall_back() -> None:
    assert plan_day_length(date(2026, 11, 1), NY) == timedelta(hours=25)


def test_plan_day_boundaries_use_local_wall_clock_not_plus_24h() -> None:
    """注意：+24h 会得到 23:00/01:00 的错误边界；这里必须是本地午夜。"""
    for day, hours in (
        (date(2026, 3, 8), 23),
        (date(2026, 11, 1), 25),
        (date(2026, 6, 1), 24),
    ):
        start, end = plan_day_window(day, NY)
        local_start = start.astimezone(ZoneInfoNY())
        local_end = end.astimezone(ZoneInfoNY())
        assert (local_start.hour, local_start.minute) == (0, 0), local_start
        assert (local_end.hour, local_end.minute) == (0, 0), local_end
        assert end - start == timedelta(hours=hours)


def ZoneInfoNY():  # noqa: N802 - 测试辅助
    return ZoneInfo(NY)


def test_plan_day_for_boundary_is_local_midnight() -> None:
    zone = ZoneInfoNY()
    assert plan_day_for(datetime(2026, 11, 6, 23, 59, tzinfo=zone), NY) == date(
        2026, 11, 6
    )
    assert plan_day_for(datetime(2026, 11, 7, 0, 0, tzinfo=zone), NY) == date(
        2026, 11, 7
    )


def test_plan_day_for_does_not_shift_early_morning_to_previous_day() -> None:
    """旧 04:00 日界会把 03:59 算到前一天；午夜日界下它就是当天。"""
    zone = ZoneInfoNY()
    assert plan_day_for(datetime(2026, 11, 7, 3, 59, tzinfo=zone), NY) == date(
        2026, 11, 7
    )


def test_plan_day_for_uses_given_timezone() -> None:
    """同一瞬时在不同规范时区可能属于不同计划日——这正是"时区变更"要处理的。"""
    instant = datetime(2026, 11, 7, 23, 30, tzinfo=ZoneInfoNY())  # NY 11-07 23:30
    assert plan_day_for(instant, NY) == date(2026, 11, 7)
    # 同一瞬时的上海本地时间是 11-08 12:30 → 属于 11-08 计划日
    assert plan_day_for(instant, SHANGHAI) == date(2026, 11, 8)


# ---------------------------------------------------------------- 生成到期时刻


def test_outline_due_at_is_previous_day_2100_local() -> None:
    """① 目标日 D 的大纲在 **D−1 本地 21:00** 到期，与午夜日界分开。"""
    due = outline_due_at(date(2026, 11, 7), SHANGHAI)
    # 上海 11-06 21:00 = UTC 11-06 13:00
    assert due == datetime(2026, 11, 6, 13, 0, tzinfo=UTC)
    local = due.astimezone(ZoneInfo(SHANGHAI))
    assert (local.date(), local.hour, local.minute) == (date(2026, 11, 6), 21, 0)
    # 早于 D 的窗口起点：21:00–24:00 仍属于 D−1 的生活，不是日界
    window_start, _window_end = plan_day_window(date(2026, 11, 7), SHANGHAI)
    assert due < window_start


def test_outline_due_at_follows_wall_clock_across_dst() -> None:
    """到期点按 **D−1 的本地墙钟**组合：跨 DST 仍是那天 21:00，不会漂成 20/22 点。"""
    due = outline_due_at(date(2026, 11, 2), NY)
    local = due.astimezone(ZoneInfoNY())
    assert (local.date(), local.hour, local.minute) == (date(2026, 11, 1), 21, 0)
    assert local.utcoffset() == timedelta(hours=-5), "11-01 21:00 已是 EST"
    assert due == datetime(2026, 11, 2, 2, 0, tzinfo=UTC)
    # 春季跳变那一侧的 D−1：
    spring = outline_due_at(date(2026, 3, 8), NY)
    spring_local = spring.astimezone(ZoneInfoNY())
    assert (spring_local.date(), spring_local.hour, spring_local.minute) == (
        date(2026, 3, 7),
        21,
        0,
    )
    assert spring_local.utcoffset() == timedelta(hours=-5)
    # 无论如何，到期点都早于 D 的窗口起点（21:00–24:00 仍属于 D−1 的生活）
    window_start, _window_end = plan_day_window(date(2026, 11, 2), NY)
    assert due < window_start


# ---------------------------------------------------------------- catch-up 三档


def test_outline_due_decision_due_right_after_2100_targets_tomorrow() -> None:
    """21:00（含）之后到期 → 目标日 D = 本地日 + 1，相位 due。"""
    day = date(2026, 11, 6)
    for minute in (21 * 60, 21 * 60 + 1, 23 * 60 + 59):
        now = local_minute_instant(day, minute, SHANGHAI)
        assert outline_due_decision(now, timezone=SHANGHAI, wake_minute=8 * 60) == (
            date(2026, 11, 7),
            PHASE_DUE,
        )


def test_outline_due_decision_one_minute_before_2100_is_not_due_yet() -> None:
    """20:59 还不到 21:00，但今天的计划如果还没 ready 就是**晚到**（醒点已过）。"""
    now = local_minute_instant(date(2026, 11, 6), 20 * 60 + 59, SHANGHAI)
    assert outline_due_decision(now, timezone=SHANGHAI, wake_minute=8 * 60) == (
        date(2026, 11, 6),
        PHASE_LATE,
    )


def test_outline_due_decision_catchup_before_wake() -> None:
    """错过 21:00、但还在 D 的醒点之前 → 同一目标日 catch-up。"""
    day = date(2026, 11, 7)
    for minute in (0, 3 * 60, 7 * 60 + 59):
        now = local_minute_instant(day, minute, SHANGHAI)
        assert outline_due_decision(now, timezone=SHANGHAI, wake_minute=8 * 60) == (
            day,
            PHASE_CATCHUP,
        )


def test_outline_due_decision_is_late_at_exact_wake_minute() -> None:
    """"已到/过了醒点"含**醒点那一刻**：08:00 起不再补造早晨。"""
    day = date(2026, 11, 7)
    for minute in (8 * 60, 10 * 60, 20 * 60 + 59):
        now = local_minute_instant(day, minute, SHANGHAI)
        assert outline_due_decision(now, timezone=SHANGHAI, wake_minute=8 * 60) == (
            day,
            PHASE_LATE,
        )
    # 21:00 之后"今天的计划还晚着"这件事被"明天到期"盖过（设计 §3：21:00 是
    # 生成时刻）；要复核今天只能显式传 plan_day。
    now = local_minute_instant(day, 23 * 60 + 30, SHANGHAI)
    assert outline_due_decision(now, timezone=SHANGHAI, wake_minute=8 * 60) == (
        date(2026, 11, 8),
        PHASE_DUE,
    )


def test_outline_due_decision_explicit_plan_day_wins() -> None:
    """显式传 D 时用它：同一瞬时（本地 23:30）能判成"今天晚到"或"明天到期"。"""
    now = local_minute_instant(date(2026, 11, 6), 23 * 60 + 30, SHANGHAI)
    assert outline_due_decision(
        now, timezone=SHANGHAI, wake_minute=8 * 60, plan_day=date(2026, 11, 6)
    ) == (date(2026, 11, 6), PHASE_LATE)
    assert outline_due_decision(
        now, timezone=SHANGHAI, wake_minute=8 * 60, plan_day=date(2026, 11, 7)
    ) == (date(2026, 11, 7), PHASE_DUE)


def test_outline_due_decision_without_sleep_window_is_late_after_midnight() -> None:
    """``wake == rest``（没有有效睡眠窗）→ 午夜之后没有"醒前"，直接晚到。"""
    now = local_minute_instant(date(2026, 11, 7), 1, SHANGHAI)
    assert outline_due_decision(now, timezone=SHANGHAI, wake_minute=None) == (
        date(2026, 11, 7),
        PHASE_LATE,
    )
    # 还没到午夜（D−1 21:00 之后）仍然是正常到期
    assert outline_due_decision(
        local_minute_instant(date(2026, 11, 6), 21 * 60, SHANGHAI),
        timezone=SHANGHAI,
        wake_minute=None,
    ) == (date(2026, 11, 7), PHASE_DUE)


def test_outline_due_decision_follows_local_wall_clock_across_dst() -> None:
    """21:00 是**本地墙钟**：美东秋季回拨当天的 21:00 已按 EST 落成 02:00Z。"""
    assert outline_due_decision(
        datetime(2026, 11, 2, 2, 0, tzinfo=UTC), timezone=NY, wake_minute=8 * 60
    ) == (date(2026, 11, 2), PHASE_DUE)
    assert outline_due_decision(
        datetime(2026, 11, 2, 1, 59, tzinfo=UTC), timezone=NY, wake_minute=8 * 60
    ) == (date(2026, 11, 1), PHASE_LATE)


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


def test_sleep_anchors_are_two_segments_fully_inside_plan_day() -> None:
    """wake=08:00 / rest=23:00 → ``[00:00,08:00)`` 与 ``[23:00,24:00)``，都在 D 内。

    D 的 00:00–08:00 由 **D 自己的计划**产出，不再"来自 D−1 的跨夜锚点"；
    两段都不越过午夜，所以不会与 D−1 / D+1 的行抢同一个区间。
    """
    plan_day = date(2026, 11, 7)
    anchors = sleep_anchors_for_day(plan_day, SHANGHAI, _routine(8, 23))
    window_start, window_end = plan_day_window(plan_day, SHANGHAI)

    assert len(anchors) == 2
    assert anchors[0].start_at == window_start
    assert anchors[0].end_at == window_start + timedelta(hours=8)
    # [23:00, 24:00)：end 是 D+1 的 00:00 瞬时，不是 D 的 23:59
    assert anchors[1].start_at == window_start + timedelta(hours=23)
    assert anchors[1].end_at == window_end
    for anchor in anchors:
        assert anchor.kind == "sleep"
        assert anchor.start_at < anchor.end_at, "单段必须 start < end"
        assert contains(window_start, window_end, anchor.start_at, anchor.end_at)


def test_sleep_anchor_is_early_morning_only_when_rest_before_wake() -> None:
    """② wake=10:00 / rest=02:00 → 只有 ``[02:00,10:00)``；午夜到 02:00 是清醒。"""
    plan_day = date(2026, 11, 7)
    anchors = sleep_anchors_for_day(plan_day, SHANGHAI, _routine(10, 2))
    window_start, _window_end = plan_day_window(plan_day, SHANGHAI)

    assert len(anchors) == 1, "rest < wake 时只有单段睡眠"
    assert anchors[0].start_at == window_start + timedelta(hours=2)
    assert anchors[0].end_at == window_start + timedelta(hours=10)


def test_sleep_anchor_is_empty_when_wake_equals_rest() -> None:
    """③ wake == rest：现有作息语义是"没有有效睡眠窗"，不凭空造睡眠段。"""
    assert sleep_anchors_for_day(date(2026, 11, 7), SHANGHAI, _routine(9, 9)) == []


# ---------------------------------------------------------------- 锚点冲突裁决


PLAN_DAY = date(2026, 11, 7)
WINDOW_START, WINDOW_END = plan_day_window(PLAN_DAY, SHANGHAI)


def _at(minute: int) -> datetime:
    return local_minute_instant(PLAN_DAY, minute, SHANGHAI)


def _commitment(
    start_minute: int, end_minute: int, *, title: str = "约定", source_id: str = ""
) -> Anchor:
    return Anchor(
        kind="commitment",
        start_at=_at(start_minute),
        end_at=_at(end_minute),
        title=title,
        protective=True,
        source_id=source_id,
    )


def _resolve(
    anchors: list[Anchor], *, routine: RoutineSnapshot | None = None
) -> tuple[list[Anchor], list]:
    return resolve_anchor_conflicts(
        anchors,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        routine=routine or _routine(8, 23),
    )


def test_resolve_clips_commitment_protective_window_to_plan_day() -> None:
    """承诺保护窗裁到 D 内：23:30 的约定不能伸进次日凌晨。"""
    anchors, _conflicts = _resolve([_commitment(23 * 60 + 30, 24 * 60 + 30)])
    assert len(anchors) == 1
    assert anchors[0].start_at == _at(23 * 60 + 30)
    assert anchors[0].end_at == WINDOW_END, "保护窗必须在 D 的午夜边界截止"


def test_resolve_drops_commitment_entirely_outside_plan_day() -> None:
    outside = Anchor(
        kind="commitment",
        start_at=WINDOW_START - timedelta(hours=3),
        end_at=WINDOW_START - timedelta(hours=2),
        title="昨天的约定",
    )
    anchors, conflicts = _resolve([outside])
    assert anchors == []
    assert conflicts == []


def test_resolve_keeps_both_same_level_commitments_with_needs_review() -> None:
    """同级硬约定冲突：两边都留、两边都带 needs_review，不选赢家。"""
    left = _commitment(9 * 60, 10 * 60, title="A", source_id="commitment:a")
    right = _commitment(9 * 60 + 30, 10 * 60 + 30, title="B", source_id="commitment:b")
    anchors, conflicts = _resolve([left, right])

    assert [item.title for item in anchors] == ["A", "B"], "两边都必须保留"
    assert all(REASON_NEEDS_REVIEW in item.reason_codes for item in anchors)
    assert len(conflicts) == 1
    assert conflicts[0].reason_code == REASON_NEEDS_REVIEW
    assert conflicts[0].anchor_ids == ("commitment:a", "commitment:b")


def test_resolve_does_not_report_conflict_for_identical_interval() -> None:
    """完全同一区间的两个来源视为同一条约定，不报冲突（否则每晚记一次）。"""
    anchors, conflicts = _resolve(
        [
            _commitment(9 * 60, 10 * 60, title="A", source_id="commitment:a"),
            _commitment(9 * 60, 10 * 60, title="B", source_id="agenda_plan:b"),
        ]
    )
    assert len(anchors) == 2
    assert conflicts == []


def test_resolve_keeps_commitment_and_shortens_expected_sleep() -> None:
    """承诺压睡眠：**保留承诺**、睡眠缩短并标 routine_conflict（不移动承诺）。"""
    sleep = sleep_anchors_for_day(PLAN_DAY, SHANGHAI, _routine(8, 23))
    assert len(sleep) == 2
    commitment = _commitment(7 * 60, 8 * 60, title="叫醒", source_id="commitment:wake")
    anchors, conflicts = _resolve([*sleep, commitment])

    kept = [item for item in anchors if item.kind == "commitment"]
    assert [(item.start_at, item.end_at) for item in kept] == [
        (commitment.start_at, commitment.end_at)
    ], "承诺本身既不动、也不带冲突码"
    assert kept[0].reason_codes == ()

    morning = [item for item in anchors if item.kind == "sleep" and item.start_at == WINDOW_START]
    assert len(morning) == 1
    assert morning[0].end_at == _at(7 * 60), "预计睡眠被裁到 07:00"
    assert REASON_ROUTINE_CONFLICT in morning[0].reason_codes
    assert REASON_NEEDS_REVIEW not in morning[0].reason_codes, (
        "总睡眠还有 7h+1h，未低于下限"
    )
    assert [item.reason_code for item in conflicts] == [REASON_ROUTINE_CONFLICT]


def test_resolve_splits_expected_sleep_around_commitment() -> None:
    """睡眠可以**分裂**：01:00–03:00 的约定把凌晨睡眠切成两段，两段都带码。"""
    sleep = sleep_anchors_for_day(PLAN_DAY, SHANGHAI, _routine(8, 23))
    anchors, _conflicts = _resolve([*sleep, _commitment(60, 180, title="早课")])

    morning = [
        item for item in anchors if item.kind == "sleep" and item.end_at <= _at(8 * 60)
    ]
    assert [(item.start_at, item.end_at) for item in morning] == [
        (_at(0), _at(60)),
        (_at(180), _at(8 * 60)),
    ]
    assert all(REASON_ROUTINE_CONFLICT in item.reason_codes for item in morning)


def test_resolve_marks_needs_review_when_remaining_sleep_below_floor() -> None:
    """剩余预计睡眠 < 420 分钟 → 再加 needs_review（仍不取消承诺）。"""
    sleep = sleep_anchors_for_day(PLAN_DAY, SHANGHAI, _routine(8, 23))
    commitment = _commitment(0, 8 * 60, title="通宵", source_id="commitment:allnight")
    anchors, conflicts = _resolve([*sleep, commitment])

    assert [item for item in anchors if item.kind == "commitment"] == [commitment]
    sleep_left = [item for item in anchors if item.kind == "sleep"]
    # 凌晨那段被完全覆盖 → 不写零长度锚点；只剩夜里的 [23:00,24:00)
    assert [(item.start_at, item.end_at) for item in sleep_left] == [
        (_at(23 * 60), WINDOW_END)
    ]
    assert REASON_NEEDS_REVIEW in sleep_left[0].reason_codes
    assert {item.reason_code for item in conflicts} == {
        REASON_ROUTINE_CONFLICT,
        REASON_NEEDS_REVIEW,
    }
    floor_conflict = next(
        item for item in conflicts if item.reason_code == REASON_NEEDS_REVIEW
    )
    assert str(MIN_SLEEP_MINUTES) in floor_conflict.detail


def test_resolve_does_not_invent_sleep_when_wake_equals_rest() -> None:
    """wake == rest：作息没有有效睡眠窗，不造睡眠（调用方传进来也不照抄）。"""
    bogus = sleep_anchors_for_day(PLAN_DAY, SHANGHAI, _routine(9, 9))
    assert bogus == []
    anchors, conflicts = _resolve(
        [
            Anchor(
                kind="sleep",
                start_at=_at(0),
                end_at=_at(9 * 60),
                title="睡觉",
            ),
            _commitment(12 * 60, 13 * 60),
        ],
        routine=_routine(9, 9),
    )
    assert [item.kind for item in anchors] == ["commitment"]
    assert conflicts == []


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
    # 锚点都在 D 内（午夜日界后没有跨夜锚点）：[19:00,23:00) 那段预计睡眠
    sleep = Anchor(
        kind="sleep",
        start_at=start + timedelta(hours=19),
        end_at=start + timedelta(hours=23),
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
    sleep = sleep_anchors_for_day(date(2026, 11, 7), SHANGHAI, _routine(8, 23))
    assert len(sleep) == 2
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
        anchors=[past_commitment, future_commitment, *sleep],
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
