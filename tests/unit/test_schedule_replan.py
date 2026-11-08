"""临时提议的纯规则（零 IO）：识别、时间抽取、严格解析、段校验、prompt 边界。

这些函数不碰数据库；真 PostgreSQL 上的硬门、预算、CAS 与跨午夜原子性在
`tests/integration/test_schedule_replan.py`。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ling.domains.agenda.replan import (
    GateVerdict,
    ProposalSignal,
    ReplanValidationError,
    build_replan_prompt,
    detect_proposal,
    parse_replan_output,
    parse_requested_window,
    validate_replan_segments,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
DAY = date(2026, 11, 8)


def _local(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 8, hour, minute, tzinfo=SHANGHAI).astimezone(UTC)


def _segment(
    start: datetime,
    end: datetime,
    *,
    kind: str = "leisure",
    title: str = "看电影",
    **extra: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": kind,
        "title": title,
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
        "place_id": None,
    }
    payload.update(extra)
    return payload


# ---------------------------------------------------------------- 识别


def test_plain_chat_is_not_a_candidate() -> None:
    """没有邀请的闲聊：连候选都不是。"""
    assert detect_proposal("今天天气不错") is None
    assert detect_proposal("我昨天看了一部老电影") is None, "陈述句不是提议"
    assert detect_proposal("") is None


def test_invitation_is_only_a_candidate() -> None:
    """命中关键词**只是候选**：字段齐全，但还没有任何决定。"""
    signal = detect_proposal("我们七点去看电影吧")
    assert signal is not None
    assert signal.kind == "movie"
    assert signal.source == "keyword"
    assert signal.ambiguity is None, "明确的邀请不是歧义"
    assert signal.outdoor is True


def test_ambiguity_precedence_question_before_negation() -> None:
    """"去不去"里含"不去"：必须先判问句，否则会把邀请记成她的拒绝。"""
    signal = detect_proposal("去不去看电影？")
    assert signal is not None
    assert signal.interrogative is True
    assert signal.negated is True  # "去不去" 里确实含子串"不去"
    assert signal.ambiguity == "ambiguous_question"

    plain_question = detect_proposal("要不要一起去看电影？")
    assert plain_question is not None
    assert plain_question.ambiguity == "ambiguous_question"


def test_negation_and_hypothetical_are_flagged() -> None:
    negated = detect_proposal("算了，不去看电影了")
    assert negated is not None and negated.negated is True
    assert negated.ambiguity == "ambiguous_negation"

    hypothetical = detect_proposal("如果明天下雨我们就去看电影吧")
    assert hypothetical is not None and hypothetical.hypothetical is True
    assert hypothetical.ambiguity == "ambiguous_hypothetical"


def test_indoor_kinds_are_not_outdoor() -> None:
    """室内/非外出类别不吃体力门（`outdoor` 决定 gate 是否查精力/饱腹）。"""
    assert ProposalSignal(kind="work", phrase="写点东西").outdoor is False
    assert ProposalSignal(kind="movie", phrase="看电影").outdoor is True


# ---------------------------------------------------------------- 时间抽取


def test_requested_window_from_clock_forms() -> None:
    window = parse_requested_window("19:30 去看电影吧", plan_day=DAY, timezone="Asia/Shanghai")
    assert window is not None
    start, end = window
    assert start == _local(19, 30)
    assert end == start + timedelta(minutes=120)

    half = parse_requested_window("19点半去看电影吧", plan_day=DAY, timezone="Asia/Shanghai")
    assert half is not None and half[0] == _local(19, 30)

    oclock = parse_requested_window("19点去看电影吧", plan_day=DAY, timezone="Asia/Shanghai")
    assert oclock is not None and oclock[0] == _local(19)


def test_requested_window_is_none_when_unparseable() -> None:
    """中文数字/含糊时间一律**不猜**：作用域退回默认范围。"""
    assert parse_requested_window("晚点去看电影吧", plan_day=DAY, timezone="Asia/Shanghai") is None
    assert parse_requested_window("七点去看电影吧", plan_day=DAY, timezone="Asia/Shanghai") is None
    assert parse_requested_window("几点去看电影吧", plan_day=DAY, timezone="Asia/Shanghai") is None


# ---------------------------------------------------------------- 严格解析


def _payload(**overrides: object) -> str:
    base: dict[str, object] = {
        "decision": "accept",
        "reason_code": "feels_right",
        "proposed_segments": [],
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


def test_parse_accepts_contract() -> None:
    parsed = parse_replan_output(_payload())
    assert parsed.decision == "accept"
    assert parsed.reason_code == "feels_right"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "不是 JSON",
        json.dumps(["accept"]),
        _payload(decision="maybe"),
        _payload(reason_code="Feels Wrong!"),
        _payload(proposed_segments="nope"),
        json.dumps({"decision": "accept", "extra": 1}),
    ],
)
def test_parse_rejects_non_contract(raw: str) -> None:
    with pytest.raises(ReplanValidationError):
        parse_replan_output(raw)


def test_decline_with_segments_is_self_contradictory() -> None:
    """拒绝了还改时间轴 = 自相矛盾，整份拒收。"""
    with pytest.raises(ReplanValidationError):
        validate_replan_segments(
            (_segment(_local(19), _local(21)),),
            scope_start=_local(18, 30),
            scope_end=_local(23),
            decision="decline",
        )


def test_accept_without_segments_is_rejected() -> None:
    with pytest.raises(ReplanValidationError):
        validate_replan_segments(
            (), scope_start=_local(18, 30), scope_end=_local(23), decision="accept"
        )


# ---------------------------------------------------------------- 段校验


def test_valid_segment_becomes_draft() -> None:
    drafts = validate_replan_segments(
        (_segment(_local(19), _local(21)),),
        scope_start=_local(18, 30),
        scope_end=_local(23),
        decision="accept",
    )
    assert len(drafts) == 1
    assert drafts[0].draft.kind == "leisure"
    assert drafts[0].draft.start_at == _local(19)
    assert drafts[0].draft.end_at == _local(21)
    assert drafts[0].parent_index is None


def test_out_of_scope_segment_is_rejected_not_clipped() -> None:
    """越界**拒收**，不裁剪：裁剪等于替模型编一个它没说的安排。"""
    with pytest.raises(ReplanValidationError) as caught:
        validate_replan_segments(
            (_segment(_local(17), _local(19)),),
            scope_start=_local(18, 30),
            scope_end=_local(23),
            decision="accept",
        )
    assert any("越出" in item for item in caught.value.problems)


def test_sleep_kind_is_rejected() -> None:
    """模型不能造睡眠段（睡眠只能由作息锚点产生）。"""
    with pytest.raises(ReplanValidationError):
        validate_replan_segments(
            (_segment(_local(19), _local(21), kind="sleep", title="睡觉"),),
            scope_start=_local(18, 30),
            scope_end=_local(23),
            decision="accept",
        )


def test_naive_and_second_precision_timestamps_are_rejected() -> None:
    naive = "2026-11-08T19:00:00"
    with pytest.raises(ReplanValidationError):
        validate_replan_segments(
            (
                {
                    "kind": "leisure",
                    "title": "看电影",
                    "start_at": naive,
                    "end_at": _local(21).isoformat(),
                },
            ),
            scope_start=_local(18, 30),
            scope_end=_local(23),
            decision="accept",
        )
    with pytest.raises(ReplanValidationError):
        validate_replan_segments(
            (
                {
                    "kind": "leisure",
                    "title": "看电影",
                    "start_at": _local(19).replace(second=30).isoformat(),
                    "end_at": _local(21).isoformat(),
                },
            ),
            scope_start=_local(18, 30),
            scope_end=_local(23),
            decision="accept",
        )


def test_unknown_place_and_extra_keys_are_rejected() -> None:
    with pytest.raises(ReplanValidationError):
        validate_replan_segments(
            (_segment(_local(19), _local(21), place_id="place_ghost"),),
            scope_start=_local(18, 30),
            scope_end=_local(23),
            valid_place_ids=frozenset({"place_1"}),
            decision="accept",
        )
    with pytest.raises(ReplanValidationError):
        validate_replan_segments(
            (_segment(_local(19), _local(21), anchor_kind="sleep"),),
            scope_start=_local(18, 30),
            scope_end=_local(23),
            decision="accept",
        )


def test_too_many_segments_are_rejected() -> None:
    spans = [
        _segment(_local(18, 30) + timedelta(minutes=10 * index),
                 _local(18, 30) + timedelta(minutes=10 * index + 5))
        for index in range(6)
    ]
    with pytest.raises(ReplanValidationError) as caught:
        validate_replan_segments(
            tuple(spans),
            scope_start=_local(18, 30),
            scope_end=_local(23),
            decision="accept",
        )
    assert any("超过上限" in item for item in caught.value.problems)


# ---------------------------------------------------------------- prompt 边界


def test_prompt_is_bounded_and_has_no_chat_transcript() -> None:
    """prompt 里有类别与短片段、有可改范围；**没有**整段聊天原文。"""
    verdict = GateVerdict(
        ok=True,
        plan_day=DAY,
        plan_id="dayplan_x",
        scope_start=_local(18, 30),
        scope_end=_local(23),
        targets=(),
        anchors=(),
        days=(DAY,),
    )
    signal = ProposalSignal(kind="movie", phrase="看电影")
    prompt = build_replan_prompt(
        signal=signal,
        verdict=verdict,
        now=_local(18, 30),
        timezone="Asia/Shanghai",
        presence=(70, 60),
    )
    assert "看电影" in prompt
    assert _local(18, 30).isoformat() in prompt
    assert _local(23).isoformat() in prompt
    # 输出契约逐字出现，模型不用猜字段名
    assert '"decision"' in prompt and '"proposed_segments"' in prompt
    # 不该把"她此刻的整段聊天"塞进来
    assert "我们七点去看电影吧" not in prompt
    # 长度有界（局部重排的输入必须能估得出钱）
    assert len(prompt) < 4000


def test_prompt_tells_the_model_what_it_may_not_touch() -> None:
    """作用域里"不能换"的段必须**明确**写进 prompt（否则模型会往上压再被拒收）。"""

    class _Seg:  # 只用 prompt 需要的那几个字段
        def __init__(self, start: datetime, end: datetime, title: str, kind: str) -> None:
            self.start_at = start
            self.end_at = end
            self.title = title
            self.kind = kind
            self.id = f"seg_{title}"

    verdict = GateVerdict(
        ok=True,
        plan_day=DAY,
        plan_id="dayplan_x",
        scope_start=_local(18, 30),
        scope_end=_local(23),
        targets=(_Seg(_local(19), _local(21), "赶稿", "work"),),  # type: ignore[arg-type]
        blocked=(_Seg(_local(18), _local(19), "吃饭", "meal"),),  # type: ignore[arg-type]
        anchors=(),
        days=(DAY,),
    )
    prompt = build_replan_prompt(
        signal=ProposalSignal(kind="movie", phrase="看电影"),
        verdict=verdict,
        now=_local(18, 30),
        timezone="Asia/Shanghai",
        presence=None,
    )
    assert "赶稿" in prompt, "可换的段要列出来（她得知道现在排了什么）"
    assert "吃饭" in prompt and "不能碰" in prompt
