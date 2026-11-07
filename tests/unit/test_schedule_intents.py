"""长程意向的**纯规则**闸门：授权入口、状态机、证据、容量、可见范围。

这一组不碰数据库，守的是最容易被"顺手放宽"的几条：

- 升级到 ``confirmed`` 只有**两条**授权入口（用户明确确认 / 承诺链路成功），
  模型输出、反复进入 prompt 都不在入口列表里；
- 终态不可复活，``completed`` 只能从 ``planned``/``confirmed`` 来；
- 终态必须有证据，**唯一**例外是 ``expired_unknown``——而它反过来不接受证据
  （"过期 = 不知道"，不是"没做"）；
- 容量 12/4 只回答"能不能再建一条"，从不选择淘汰谁；
- 准备段的可见范围取来源里**最严**的那个；未知取值按最严处理。

数据库层的 CHECK/唯一键由 ``tests/integration/test_schedule_intentions.py``
在真 PostgreSQL 上验，这里不重复。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from ling.domains.agenda.intents import (
    CONFIRM_ENTRY_COMMITMENT,
    CONFIRM_ENTRY_USER,
    MAX_ACTIVE_INTENTIONS,
    MAX_PROPOSED_INTENTIONS,
    MAX_TARGET_HORIZON_DAYS,
    MAX_TARGET_SPAN_DAYS,
    REASON_EVIDENCE_INVALID,
    REASON_EVIDENCE_REQUIRED,
    REASON_INTENTION_CAPACITY,
    REASON_TERMINAL,
    CapacityVerdict,
    IntentEvidenceError,
    IntentStateError,
    IntentTerminalError,
    IntentValidationError,
    IntentView,
    ensure_confirm_entry,
    ensure_resolution_evidence,
    ensure_target_window,
    ensure_transition,
    evaluate_capacity,
    is_terminal,
    local_date,
    strictest_visibility,
)
from ling.infra.models import INTENTION_STATES, INTENTION_TERMINAL_STATES

TODAY = date(2026, 11, 8)


# ---------------------------------------------------------------- 授权入口


def test_confirm_entries_are_exactly_two() -> None:
    """只有"用户明确确认"和"承诺链路成功"两条入口，没有第三条。"""
    assert ensure_confirm_entry(CONFIRM_ENTRY_USER) == "user"
    assert ensure_confirm_entry(CONFIRM_ENTRY_COMMITMENT) == "commitment"


@pytest.mark.parametrize("entry", ["model", "prompt", "nightly", "user_confirmed", "", " "])
def test_model_and_exposure_cannot_confirm(entry: str) -> None:
    """模型/被看到/换个别名都不是授权入口。"""
    with pytest.raises(IntentStateError) as excinfo:
        ensure_confirm_entry(entry)
    assert excinfo.value.reason_code == "intention_confirm_entry"


# ---------------------------------------------------------------- 状态机


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("proposed", "planned"),
        ("proposed", "confirmed"),
        ("planned", "confirmed"),
        ("planned", "completed"),
        ("confirmed", "completed"),
        ("proposed", "missed"),
        ("confirmed", "missed"),
        ("planned", "abandoned"),
        ("confirmed", "expired_unknown"),
    ],
)
def test_allowed_transitions(current: str, target: str) -> None:
    ensure_transition(current=current, target=target)


def test_proposed_cannot_jump_to_completed() -> None:
    """从未成为计划的候选不能直接"完成了"。"""
    with pytest.raises(IntentStateError):
        ensure_transition(current="proposed", target="completed")


@pytest.mark.parametrize("terminal", INTENTION_TERMINAL_STATES)
@pytest.mark.parametrize("target", INTENTION_STATES)
def test_terminal_state_cannot_be_revived(terminal: str, target: str) -> None:
    """终态对**任何**目标状态都拒（含再确认、再完成、再撤回）。"""
    assert is_terminal(terminal)
    with pytest.raises(IntentTerminalError) as excinfo:
        ensure_transition(current=terminal, target=target)
    assert excinfo.value.reason_code == REASON_TERMINAL


def test_every_state_has_a_transition_rule() -> None:
    """状态白名单与转换表必须覆盖同一组取值（新增状态不能悄悄漏判）。"""
    for state in INTENTION_STATES:
        with pytest.raises((IntentStateError, IntentTerminalError)):
            # 自反转换永远非法：状态推进必须换一个值。
            ensure_transition(current=state, target=state)


# ---------------------------------------------------------------- 证据


@pytest.mark.parametrize("state", ["completed", "missed", "abandoned"])
def test_terminal_states_require_evidence(state: str) -> None:
    with pytest.raises(IntentEvidenceError) as excinfo:
        ensure_resolution_evidence(state=state, evidence_ref=None)
    assert excinfo.value.reason_code == REASON_EVIDENCE_REQUIRED
    ensure_resolution_evidence(state=state, evidence_ref="turn:123")


def test_expired_unknown_needs_no_evidence() -> None:
    ensure_resolution_evidence(state="expired_unknown", evidence_ref=None)


def test_expired_unknown_rejects_evidence() -> None:
    """有证据就该是 missed/completed；"不知道"不能拿证据洗成"知道"。"""
    with pytest.raises(IntentEvidenceError) as excinfo:
        ensure_resolution_evidence(state="expired_unknown", evidence_ref="turn:123")
    assert excinfo.value.reason_code == REASON_EVIDENCE_INVALID


def test_non_terminal_cannot_be_resolved() -> None:
    with pytest.raises(IntentValidationError):
        ensure_resolution_evidence(state="confirmed", evidence_ref="turn:123")


# ---------------------------------------------------------------- 容量


def test_proposed_cap_is_four() -> None:
    verdict = evaluate_capacity(
        active_count=4, proposed_count=MAX_PROPOSED_INTENTIONS, state="proposed"
    )
    assert verdict == CapacityVerdict(
        allowed=False,
        reason_code=REASON_INTENTION_CAPACITY,
        axis="proposed",
        active_count=4,
        proposed_count=4,
    )


def test_active_cap_is_twelve_for_any_new_row() -> None:
    """满 12 条活跃时，planned 也不能再进——但判定**不**说淘汰谁。"""
    for state in ("proposed", "planned"):
        verdict = evaluate_capacity(
            active_count=MAX_ACTIVE_INTENTIONS,
            proposed_count=0,
            state=state,
        )
        assert not verdict.allowed
        assert verdict.axis == "active"


def test_capacity_allows_under_both_caps() -> None:
    verdict = evaluate_capacity(active_count=3, proposed_count=1, state="proposed")
    assert verdict.allowed and verdict.reason_code is None and verdict.axis == ""


def test_capacity_only_for_creatable_states() -> None:
    with pytest.raises(IntentValidationError):
        evaluate_capacity(active_count=0, proposed_count=0, state="confirmed")


# ---------------------------------------------------------------- 目标窗口


def test_target_window_rejects_reversed_days() -> None:
    with pytest.raises(IntentValidationError):
        ensure_target_window(
            start_day=date(2026, 11, 10), end_day=date(2026, 11, 9), today=TODAY
        )


def test_target_window_rejects_too_wide_span() -> None:
    with pytest.raises(IntentValidationError):
        ensure_target_window(
            start_day=TODAY,
            end_day=TODAY + timedelta(days=MAX_TARGET_SPAN_DAYS + 1),
            today=TODAY,
        )


def test_target_window_rejects_past_target() -> None:
    with pytest.raises(IntentValidationError):
        ensure_target_window(
            start_day=TODAY - timedelta(days=2),
            end_day=TODAY - timedelta(days=1),
            today=TODAY,
        )


def test_target_window_rejects_beyond_horizon() -> None:
    with pytest.raises(IntentValidationError):
        ensure_target_window(
            start_day=TODAY + timedelta(days=MAX_TARGET_HORIZON_DAYS + 1),
            end_day=TODAY + timedelta(days=MAX_TARGET_HORIZON_DAYS + 1),
            today=TODAY,
        )


def test_target_window_accepts_today_and_horizon_edge() -> None:
    ensure_target_window(start_day=TODAY, end_day=TODAY, today=TODAY)
    edge = TODAY + timedelta(days=MAX_TARGET_HORIZON_DAYS)
    ensure_target_window(start_day=edge, end_day=edge, today=TODAY)


# ---------------------------------------------------------------- 时区投影


def test_target_day_uses_the_intention_timezone() -> None:
    """UTC 23:00 在上海已经是"第二天"——目标日必须按意向自己的时区判。"""
    moment = datetime(2026, 11, 7, 23, 0, tzinfo=UTC)
    assert local_date(moment, "Asia/Shanghai") == date(2026, 11, 8)
    assert local_date(moment, "UTC") == date(2026, 11, 7)
    # 坏时区退回 UTC，而不是服务器本地时区
    assert local_date(moment, "Not/AZone") == date(2026, 11, 7)


# ---------------------------------------------------------------- 可见范围


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (("public", "window_private"), "window_private"),
        (("actor_private", "window_private"), "actor_private"),
        (("group_scope", "subject_private"), "subject_private"),
        (("public", None, ""), "public"),
        (("public", "admin_only"), "admin_only"),
        (("public", "不是范围"), "actor_private"),
        ((), "actor_private"),
    ],
)
def test_strictest_visibility(values: tuple[str | None, ...], expected: str) -> None:
    assert strictest_visibility(*values) == expected


# ---------------------------------------------------------------- 视图


def test_intention_is_never_a_hard_anchor() -> None:
    """确认"目标"≠ 确认"时段"：视图里没有任何"硬锚点"可能。"""
    view = IntentView(
        intention_id="intent_x",
        actor_id="actor_ling",
        kind="travel",
        title="周五想旅行",
        state="confirmed",
        source_kind="user",
        source_ref="turn:1",
        target_start_day=TODAY,
        target_end_day=TODAY,
        target_timezone="Asia/Shanghai",
        priority=50,
        visibility="actor_private",
        revision=2,
        horizon_rev=2,
        prep_source_ref=None,
        prep_due_day=None,
        confirmation_source_ref="turn:1",
        confirmed_at=datetime(2026, 11, 8, tzinfo=UTC),
        resolution_evidence_ref="turn:1",
        resolved_at=None,
        created_at=datetime(2026, 11, 8, tzinfo=UTC),
        updated_at=datetime(2026, 11, 8, tzinfo=UTC),
    )
    assert view.state == "confirmed"
    assert view.is_hard_anchor is False
    assert view.is_terminal is False
