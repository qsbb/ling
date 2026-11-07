"""只读生活时间轴的纯规则：裁剪、双时区换算、摘要选条、四态判定（P3）。

这一组用例不碰数据库（`tests/unit` 的规矩）：喂的是内存里的 ORM 行对象与
纯函数参数。真正落库的验收在 `tests/integration/test_life_timeline.py`。

每条用例都对应设计 §7/§9 的一条可程序化验收，注释里写了它是哪一条。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from ling.api.routes.life_timeline import (
    TimelineInputs,
    build_timeline,
    clip_to_window,
    continues_flags,
    derive_state,
    dream_card,
    intent_teaser_items,
    is_confirmed_anchor,
    project_segments,
    resolve_dual_window,
    select_today_outline,
    state_note,
    viewer_is_owner,
    visible_to,
)
from ling.contracts.v1 import OutlineRelation, TimelineState
from ling.domains.agenda.intents import IntentView
from ling.infra.models import DayPlanRow, DreamReportRow, ScheduleSegmentRow

ACTOR = "actor_ling"
TZ = "Asia/Shanghai"
DAY = date(2026, 11, 8)


def _utc(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def _shanghai(day: date, hour: int, minute: int = 0) -> datetime:
    """上海本地墙钟 → UTC 瞬时（东八区无夏令时，直接减 8 小时）。"""
    return _utc(day, hour, minute) - timedelta(hours=8)


def _segment(
    segment_id: str,
    start_at: datetime,
    end_at: datetime,
    *,
    title: str = "做事",
    is_leaf: bool = True,
    status: str = "current",
    anchor_kind: str = "",
    commitment_level: str = "tentative",
    visibility: str | None = None,
    parent_id: str | None = None,
    layer: str = "block",
    plan_id: str = "plan_1",
    revision: int = 1,
) -> ScheduleSegmentRow:
    """内存里的段行（不入库；不设 created_at/updated_at——读侧不看它们）。"""
    return ScheduleSegmentRow(
        id=segment_id,
        plan_id=plan_id,
        actor_id=ACTOR,
        parent_id=parent_id,
        parent_key=parent_id or "",
        layer=layer,
        kind="work",
        title=title,
        start_at=start_at,
        end_at=end_at,
        place_id=None,
        commitment_level=commitment_level,
        status=status,
        is_leaf=is_leaf,
        anchor_kind=anchor_kind,
        source="model",
        revision_created=revision,
        revision_ended=None,
        source_event_ids=[],
        intention_id=None,
        preparation_for_day=None,
        visibility=visibility,
    )


def _plan(
    plan_day: date = DAY,
    *,
    state: str = "ready",
    revision: int = 2,
    error: str | None = None,
    timezone: str = TZ,
    timezone_source: str = "preference",
    updated_at: datetime | None = None,
) -> DayPlanRow:
    start = _shanghai(plan_day, 0)
    return DayPlanRow(
        id="plan_1",
        actor_id=ACTOR,
        plan_day=plan_day,
        timezone=timezone,
        timezone_source=timezone_source,
        timezone_revision=0,
        routine_revision=1,
        window_start_at=start,
        window_end_at=_shanghai(plan_day + timedelta(days=1), 0),
        revision=revision,
        state=state,
        generation_source="model" if state == "ready" else "rule",
        last_error_code=error,
        updated_at=updated_at or datetime(2026, 11, 7, 13, 5, tzinfo=UTC),
    )


# ---------------------------------------------------------------- 裁剪


def test_clip_returns_intersection_and_none_when_disjoint() -> None:
    """只返回相交区间；端点相接（半开区间）不算相交。"""
    window_start = _utc(DAY, 10)
    window_end = _utc(DAY, 12)

    assert clip_to_window(
        _utc(DAY, 11), _utc(DAY, 13), window_start, window_end
    ) == (_utc(DAY, 11), _utc(DAY, 12))
    assert clip_to_window(
        _utc(DAY, 9), _utc(DAY, 11), window_start, window_end
    ) == (_utc(DAY, 10), _utc(DAY, 11))
    # 完整落在窗口内：原样返回
    assert clip_to_window(
        _utc(DAY, 10, 30), _utc(DAY, 11, 30), window_start, window_end
    ) == (_utc(DAY, 10, 30), _utc(DAY, 11, 30))
    # 半开区间：12:00 开始 / 10:00 结束都不相交
    assert clip_to_window(_utc(DAY, 12), _utc(DAY, 13), window_start, window_end) is None
    assert clip_to_window(_utc(DAY, 9), _utc(DAY, 10), window_start, window_end) is None


def test_continues_flags_use_raw_interval_not_clipped_one() -> None:
    """``continues_*`` 必须拿**真实**区间比窗口；用裁剪后的值比永远是 False。"""
    window_start = _shanghai(DAY, 0)
    window_end = _shanghai(DAY + timedelta(days=1), 0)
    start = _shanghai(DAY, 23, 30)
    end = _shanghai(DAY + timedelta(days=1), 0, 30)

    assert continues_flags(start, end, window_start, window_end) == (False, True)
    # 另外半天（次日窗口）是同一个段的另一半
    next_start = window_end
    next_end = _shanghai(DAY + timedelta(days=2), 0)
    assert continues_flags(start, end, next_start, next_end) == (True, False)


def test_cross_midnight_segment_is_clipped_in_both_days() -> None:
    """④ 23:30–00:30 跨日段在两天各自被正确裁剪并带 continues_*。"""
    night = _segment("seg_night", _shanghai(DAY, 23, 30), _shanghai(DAY + timedelta(days=1), 0, 30))

    day_one = resolve_dual_window(DAY, TZ, TZ)
    first = project_segments(
        [(night, DAY)],
        window_start=day_one.display_start_at,
        window_end=day_one.display_end_at,
        is_owner=True,
    )
    assert first.hidden_count == 0
    assert len(first.segments) == 1
    seg = first.segments[0]
    assert seg.display_start_at == _shanghai(DAY, 23, 30)
    assert seg.display_end_at == _shanghai(DAY + timedelta(days=1), 0)  # 裁到午夜
    assert seg.continues_from_previous_day is False
    assert seg.continues_next_day is True
    # 原始区间不动——裁剪值不是段的真实起止
    assert seg.start_at == _shanghai(DAY, 23, 30)
    assert seg.end_at == _shanghai(DAY + timedelta(days=1), 0, 30)

    day_two = resolve_dual_window(DAY + timedelta(days=1), TZ, TZ)
    second = project_segments(
        [(night, DAY)],
        window_start=day_two.display_start_at,
        window_end=day_two.display_end_at,
        is_owner=True,
    )
    assert len(second.segments) == 1
    seg2 = second.segments[0]
    assert seg2.display_start_at == _shanghai(DAY + timedelta(days=1), 0)
    assert seg2.display_end_at == _shanghai(DAY + timedelta(days=1), 0, 30)
    assert seg2.continues_from_previous_day is True
    assert seg2.continues_next_day is False
    # plan_day 来自计划行的 join，不是从 start_at 反推
    assert seg2.plan_day == DAY


# ---------------------------------------------------------------- 双时区


def test_dual_window_converts_display_and_plan_timezones() -> None:
    """③ 用户时区 ≠ 角色时区时两套窗口都对（固定日期，不依赖真实时钟）。"""
    window = resolve_dual_window(DAY, "America/New_York", "Asia/Shanghai")

    # 美东 2026-11-08 已是 EST（UTC−5）
    assert window.display_start_at == datetime(2026, 11, 8, 5, 0, tzinfo=UTC)
    assert window.display_end_at == datetime(2026, 11, 9, 5, 0, tzinfo=UTC)
    # 角色侧仍是上海本地午夜到午夜
    assert window.plan_day == DAY
    assert window.plan_start_at == datetime(2026, 11, 7, 16, 0, tzinfo=UTC)
    assert window.plan_end_at == datetime(2026, 11, 8, 16, 0, tzinfo=UTC)
    # 两个窗口确实不同（否则"双时区"是装饰）
    assert window.display_start_at != window.plan_start_at


def test_dual_window_handles_dst_short_day() -> None:
    """DST 当天展示日是 23 小时：不能用 ``+24h`` 求次日边界。"""
    window = resolve_dual_window(date(2026, 3, 8), "America/New_York", "Asia/Shanghai")
    assert window.display_end_at - window.display_start_at == timedelta(hours=23)
    assert window.display_start_at == datetime(2026, 3, 8, 5, 0, tzinfo=UTC)
    assert window.display_end_at == datetime(2026, 3, 9, 4, 0, tzinfo=UTC)
    # 角色侧不受美东 DST 影响
    assert window.plan_end_at - window.plan_start_at == timedelta(hours=24)


def test_plan_day_can_differ_from_display_date() -> None:
    """展示日窗口的起点可能落在**前一个**角色计划日里。"""
    window = resolve_dual_window(DAY, "Pacific/Auckland", "America/Los_Angeles")
    # 奥克兰 11-08 00:00（UTC+13）＝ UTC 11-07 11:00 ＝ 洛杉矶 11-07 03:00（PST）
    assert window.display_start_at == datetime(2026, 11, 7, 11, 0, tzinfo=UTC)
    assert window.plan_day == date(2026, 11, 7)
    assert window.plan_day != window.display_date


# ---------------------------------------------------------------- 四态


def test_derive_state_distinguishes_all_states() -> None:
    """⑤ 空/降级/失败/不可用（+待生成/就绪）必须给出不同的 state。"""
    assert derive_state(None) is TimelineState.empty
    assert derive_state(_plan(state="ready")) is TimelineState.ready
    assert derive_state(_plan(state="degraded", error="model_unavailable")) is TimelineState.degraded
    # pending + 原因码 = 生成失败（这一版压根没提交出来）
    assert derive_state(_plan(state="pending", error="model_unavailable")) is TimelineState.failed
    # pending 但没有原因码 = 还没到期/还没跑，不是失败
    assert derive_state(_plan(state="pending", error=None)) is TimelineState.pending


def test_state_note_is_distinct_for_the_four_states() -> None:
    notes = {
        state_note(TimelineState.empty),
        state_note(TimelineState.degraded),
        state_note(TimelineState.failed),
        state_note(TimelineState.unavailable),
    }
    assert len(notes) == 4, notes
    assert None not in notes
    assert state_note(TimelineState.ready) is None


# ---------------------------------------------------------------- 层级与受众


def test_parent_and_child_segments_both_projected() -> None:
    """父子段都要在（前端要能折叠层级），按展示起点排序。"""
    window = resolve_dual_window(DAY, TZ, TZ)
    parent = _segment("seg_parent", _shanghai(DAY, 13), _shanghai(DAY, 17), is_leaf=False)
    child = _segment(
        "seg_child",
        _shanghai(DAY, 14),
        _shanghai(DAY, 15),
        parent_id="seg_parent",
        layer="hour",
    )
    projection = project_segments(
        [(parent, DAY), (child, DAY)],
        window_start=window.display_start_at,
        window_end=window.display_end_at,
        is_owner=True,
    )
    assert [s.segment_id for s in projection.segments] == ["seg_parent", "seg_child"]
    assert projection.segments[0].is_leaf is False
    assert projection.segments[1].parent_id == "seg_parent"
    assert projection.segments[1].is_leaf is True


def test_private_segment_hidden_from_non_owner() -> None:
    """② ``visibility=actor_private`` 的段只对 owner 返回正文。"""
    window = resolve_dual_window(DAY, TZ, TZ)
    private = _segment(
        "seg_private",
        _shanghai(DAY, 16),
        _shanghai(DAY, 17),
        visibility="actor_private",
    )
    owner_view = project_segments(
        [(private, DAY)],
        window_start=window.display_start_at,
        window_end=window.display_end_at,
        is_owner=True,
    )
    assert len(owner_view.segments) == 1
    assert owner_view.hidden_count == 0

    other_view = project_segments(
        [(private, DAY)],
        window_start=window.display_start_at,
        window_end=window.display_end_at,
        is_owner=False,
    )
    assert other_view.segments == ()
    assert other_view.hidden_count == 1


def test_null_visibility_counts_as_visible() -> None:
    """旧段 ``visibility`` 为 NULL 表示"未知"，不猜成私有。"""
    assert visible_to(None, is_owner=False) is True
    assert visible_to("subject_private", is_owner=False) is True
    assert visible_to("actor_private", is_owner=False) is False
    assert visible_to("actor_private", is_owner=True) is True


def test_superseded_segments_are_counted_not_returned() -> None:
    """已替代的段不进时间轴（会与当前段重叠），只计数给界面标"改动过"。"""
    window = resolve_dual_window(DAY, TZ, TZ)
    current = _segment("seg_now", _shanghai(DAY, 14), _shanghai(DAY, 15))
    old = _segment("seg_old", _shanghai(DAY, 14), _shanghai(DAY, 15), status="superseded")
    projection = project_segments(
        [(current, DAY), (old, DAY)],
        window_start=window.display_start_at,
        window_end=window.display_end_at,
        is_owner=True,
    )
    assert [s.segment_id for s in projection.segments] == ["seg_now"]
    assert projection.superseded_count == 1


def test_viewer_is_owner_accepts_owner_role_and_owner_subject() -> None:
    assert viewer_is_owner(role="owner", subject_id="subj_x", owner_subject="subj_owner") is True
    assert viewer_is_owner(role="member", subject_id="subj_owner", owner_subject="subj_owner") is True
    assert viewer_is_owner(role="member", subject_id="subj_other", owner_subject="subj_owner") is False
    # 没有 owner 账号的部署：只有 role=owner 才算
    assert viewer_is_owner(role="member", subject_id="subj_owner", owner_subject=None) is False


# ---------------------------------------------------------------- 摘要选条


def test_select_today_outline_picks_now_next_and_anchor() -> None:
    """② 仪表盘 today_outline：当前段 + 下一条 + 已确认锚点，已过去的不填数。"""
    now = _shanghai(DAY, 14, 30)
    past = _segment("seg_past", _shanghai(DAY, 9), _shanghai(DAY, 10), title="早餐")
    current = _segment("seg_now", _shanghai(DAY, 14), _shanghai(DAY, 15), title="工作")
    upcoming = _segment("seg_next", _shanghai(DAY, 16), _shanghai(DAY, 17), title="读书")
    anchor = _segment(
        "seg_anchor",
        _shanghai(DAY, 18),
        _shanghai(DAY, 19),
        title="和人约好",
        anchor_kind="commitment",
        commitment_level="confirmed",
    )
    picks = select_today_outline(
        [past, current, upcoming, anchor], now=now, limit=3
    )
    assert [p.relation for p in picks] == [
        OutlineRelation.now,
        OutlineRelation.next,
        OutlineRelation.anchor,
    ]
    assert [p.title for p in picks] == ["工作", "读书", "和人约好"]


def test_select_today_outline_does_not_backfill_with_past_segments() -> None:
    """没有当前段/后续段时宁可少给几条，也不拿过去的事填满。"""
    now = _shanghai(DAY, 20)
    past = _segment("seg_past", _shanghai(DAY, 9), _shanghai(DAY, 10))
    assert select_today_outline([past], now=now, limit=3) == ()


def test_select_today_outline_treats_sleep_as_next_not_anchor() -> None:
    """预计睡眠是"接下来的一段"，**不是**已确认锚点（设计 §2）。"""
    now = _shanghai(DAY, 14, 30)
    current = _segment("seg_now", _shanghai(DAY, 14), _shanghai(DAY, 15))
    sleep = _segment(
        "seg_sleep",
        _shanghai(DAY, 23),
        _shanghai(DAY + timedelta(days=1), 0),
        title="睡觉",
        anchor_kind="sleep",
        commitment_level="confirmed",
    )
    assert is_confirmed_anchor(sleep) is False
    picks = select_today_outline([current, sleep], now=now, limit=3)
    assert [p.relation for p in picks] == [OutlineRelation.now, OutlineRelation.next]
    assert picks[1].title == "睡觉"


def test_select_today_outline_keeps_anchor_distinct_from_next() -> None:
    """已确认锚点占 anchor 槽位，不被"接下来那一条"顶掉（否则界面看不出是约定）。"""
    now = _shanghai(DAY, 14, 30)
    anchor = _segment(
        "seg_anchor",
        _shanghai(DAY, 16),
        _shanghai(DAY, 17),
        anchor_kind="commitment",
        commitment_level="confirmed",
    )
    picks = select_today_outline([anchor], now=now, limit=3)
    assert [p.relation for p in picks] == [OutlineRelation.anchor]
    # 已达上限时不再追加
    limited = select_today_outline([anchor], now=now, limit=1)
    assert len(limited) == 1


def test_select_today_outline_ignores_parent_and_superseded() -> None:
    now = _shanghai(DAY, 14, 30)
    parent = _segment("seg_parent", _shanghai(DAY, 13), _shanghai(DAY, 17), is_leaf=False)
    old = _segment("seg_old", _shanghai(DAY, 14), _shanghai(DAY, 15), status="superseded")
    assert select_today_outline([parent, old], now=now, limit=3) == ()


# ---------------------------------------------------------------- 装配


def test_build_timeline_carries_both_timezones_and_generation_fields() -> None:
    """① 返回体必须同时给展示侧与角色侧，且 generated_at / outline_due_at 分开。"""
    window = resolve_dual_window(DAY, "America/New_York", "Asia/Shanghai")
    segment = _segment("seg_1", _shanghai(DAY, 14), _shanghai(DAY, 15), title="工作")
    out = build_timeline(
        TimelineInputs(
            display_day=DAY,
            display_timezone="America/New_York",
            display_timezone_source="preference",
            actor_timezone="Asia/Shanghai",
            actor_timezone_source="default",
            is_owner=True,
            plan=_plan(revision=3),
            segment_rows=((segment, DAY),),
        )
    )
    assert out.display_date == DAY
    assert out.display_timezone == "America/New_York"
    assert out.display_window_start_at == window.display_start_at
    assert out.plan_day == DAY
    assert out.plan_window_start_at == window.plan_start_at
    assert out.actor_timezone == "Asia/Shanghai"
    assert out.state is TimelineState.ready
    assert out.plan_state == "ready"
    assert out.revision == 3
    # 计划行记录的时区来源优先（它说明这一版是按谁算的）
    assert out.timezone_source == "preference"
    assert out.generated_at == datetime(2026, 11, 7, 13, 5, tzinfo=UTC)
    # 生成到期时刻是 D−1 本地 21:00，不是 generated_at
    assert out.outline_due_at == datetime(2026, 11, 7, 13, 0, tzinfo=UTC)
    assert out.horizon_unplanned is True
    assert [s.segment_id for s in out.segments] == ["seg_1"]


def test_build_timeline_marks_private_content_without_leaking_it() -> None:
    """② 非 owner：私密段/梦正文都不返回，但要有"存在私有内容"的标记。"""
    segment = _segment(
        "seg_private",
        _shanghai(DAY, 16),
        _shanghai(DAY, 17),
        visibility="actor_private",
    )
    report = DreamReportRow(
        id="dream_1",
        actor_id=ACTOR,
        sleep_date=DAY,
        status="ready",
        title="迷路的电梯",
        narrative="我站在一部只会向上的电梯里……",
        mood="uneasy",
        cue_ids=[],
        transform_ids=[],
        origin="simulated",
        visibility="actor_private",
        plan_id="plan_1",
    )
    out = build_timeline(
        TimelineInputs(
            display_day=DAY,
            display_timezone=TZ,
            display_timezone_source="default",
            actor_timezone=TZ,
            actor_timezone_source="default",
            is_owner=False,
            plan=_plan(),
            segment_rows=((segment, DAY),),
            dream=dream_card(report, is_owner=False),
        )
    )
    assert out.segments == []
    assert out.hidden_segment_count == 1
    assert out.has_private_content is True
    assert out.dream is not None
    assert out.dream.restricted is True
    assert out.dream.narrative is None
    assert out.dream.title is None
    assert out.dream.status == "ready"

    owner_out = build_timeline(
        TimelineInputs(
            display_day=DAY,
            display_timezone=TZ,
            display_timezone_source="default",
            actor_timezone=TZ,
            actor_timezone_source="default",
            is_owner=True,
            plan=_plan(),
            segment_rows=((segment, DAY),),
            dream=dream_card(report, is_owner=True),
        )
    )
    assert len(owner_out.segments) == 1
    assert owner_out.hidden_segment_count == 0
    assert owner_out.dream is not None
    assert owner_out.dream.narrative.startswith("我站在")
    assert owner_out.has_private_content is False


def test_dream_card_is_null_when_there_is_no_report() -> None:
    """没有记录（null）与"有记录但看不到"（restricted）必须能区分。"""
    assert dream_card(None, is_owner=True) is None
    assert dream_card(None, is_owner=False) is None


def test_build_timeline_empty_day_has_no_plan_and_no_generated_at() -> None:
    """⑥ 没有计划行 = empty 空态，不是错误；generated_at 为 null。"""
    out = build_timeline(
        TimelineInputs(
            display_day=DAY,
            display_timezone=TZ,
            display_timezone_source="default",
            actor_timezone=TZ,
            actor_timezone_source="default",
        )
    )
    assert out.state is TimelineState.empty
    assert out.plan_state is None
    assert out.revision == 0
    assert out.generated_at is None
    assert out.segments == []
    assert out.dream is None
    assert out.note == "这一天还没有安排"


# ---------------------------------------------------------------- 意向预告


def _intent(
    intention_id: str,
    *,
    title: str = "去看展",
    source_ref: str = "turn:abc",
    state: str = "planned",
    source_kind: str = "user",
    start_day: date = DAY + timedelta(days=2),
) -> IntentView:
    return IntentView(
        intention_id=intention_id,
        actor_id=ACTOR,
        kind="leisure",
        title=title,
        state=state,
        source_kind=source_kind,
        source_ref=source_ref,
        target_start_day=start_day,
        target_end_day=start_day,
        target_timezone=TZ,
        priority=60,
        visibility="subject_private",
        revision=1,
        horizon_rev=1,
        prep_source_ref=None,
        prep_due_day=None,
        confirmation_source_ref=None,
        confirmed_at=None,
        resolution_evidence_ref=None,
        resolved_at=None,
        created_at=datetime(2026, 11, 7, 13, 0, tzinfo=UTC),
        updated_at=datetime(2026, 11, 7, 13, 0, tzinfo=UTC),
    )


def test_intent_teaser_only_gives_sourced_intents() -> None:
    """③ ``intent_teaser``：只给有来源的，最多 2 条，且带来源类别。"""
    items = intent_teaser_items(
        [
            _intent("int_a", source_ref="turn:abc"),
            _intent("int_b", source_ref="   ", title="没有来源"),
            _intent("int_c", source_ref="turn:def", state="confirmed", source_kind="user"),
            _intent("int_d", source_ref="recurrence:1", source_kind="recurrence"),
        ],
        limit=2,
    )
    assert [item.intention_id for item in items] == ["int_a", "int_c"]
    assert items[0].source_kind == "user"
