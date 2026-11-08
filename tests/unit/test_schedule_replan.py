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
    REPLAN_NOTICE_MAX_CHARS,
    REPLAN_NOTICE_TEXTS,
    GateVerdict,
    ProposalSignal,
    ReplanOutcome,
    ReplanValidationError,
    build_replan_prompt,
    detect_proposal,
    notice_for,
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


# ---------------------------------------------------------------- 回话文案
#
# 缺口：拒绝/推迟一次提议时用户收不到任何回话。`notice_for` 就是那句回话，
# 它必须是**固定、诚实、短**的：为一句固定短话调模型是浪费，说内部机制是
# 泄漏，承诺新安排是撒谎（计划不是事实）。

# 走到 `declined` / `deferred` 的全部原因码。**一个都不能漏**：新增一道硬门
# 却忘了写文案时，下面那条"覆盖恰好相等"的用例会红。
REACHABLE_REASONS = (
    # 硬门：她自己不可用
    "sleep_window",
    "rest_window",
    "commitment_conflict",
    "activity_running",
    "low_energy",
    # 硬门：这话还不算"要改计划"
    "ambiguous_question",
    "ambiguous_negation",
    "ambiguous_hypothetical",
    # 改不动
    "plan_missing",
    "cross_midnight_plan_missing",
    "scope_too_small",
    # 频率与预算
    "window_hourly_limit",
    "daily_limit",
    "budget_exhausted",
    # 模型侧 / 版本冲突
    "model_unavailable",
    "model_invalid",
    "model_error",
    "stale_revision",
)

# 不能出现在**给用户**的话里的词：那是我们的账本和内部机制。
FORBIDDEN_IN_NOTICE = (
    "预算",
    "额度",
    "token",
    "Token",
    "模型",
    "频率",
    "限流",
    "预算耗尽",
    "重排",
)

# 承诺式措辞：计划不是事实，没排进时间轴的事不能先说出去。
PROMISE_WORDS = ("一定", "肯定", "保证", "明天", "改天", "下次", "maybe", "probably")


def _notice(state: str, reason: str | None) -> str | None:
    return notice_for(ReplanOutcome(state=state, turn_id="turn-1", reason_code=reason))


def test_no_notice_for_plain_chat_accepted_or_replayed() -> None:
    """闲聊、已经改了、以及同一轮的重复消费，都**一个字都不回**。"""
    for state in ("no_proposal", "accepted", "duplicate"):
        for reason in (None, *REACHABLE_REASONS):
            assert _notice(state, reason) is None, (state, reason)


def test_every_reachable_reason_has_a_notice() -> None:
    """每个能走到"拒绝/推迟"的原因码都有固定文案，且表里没有多余/错拼的键。"""
    assert set(REPLAN_NOTICE_TEXTS) == set(REACHABLE_REASONS)


@pytest.mark.parametrize("reason", REACHABLE_REASONS)
def test_notice_is_short_and_free_of_internal_words_and_promises(reason: str) -> None:
    """一句话、≤30 字；不含内部词；不承诺任何新安排；不漏原因码。"""
    text = _notice("deferred", reason)
    assert text, reason
    assert text == _notice("declined", reason), "同一个门不管拒绝还是推迟都说同一句"
    assert len(text) <= REPLAN_NOTICE_MAX_CHARS, text
    assert len(text) >= 4, "短到读不懂也不算好文案"
    # 内部词：预算/额度/token/模型/频率……
    for word in FORBIDDEN_IN_NOTICE:
        assert word not in text, (reason, word, text)
    # 承诺：不能替她答应任何以后的事
    for word in PROMISE_WORDS:
        assert word not in text, (reason, word, text)
    # 原因码本身漏进聊天（例如 "sleep_window"）就是机制泄漏
    assert reason.replace("_", "") not in text.replace("_", "")
    assert not any(char.isascii() and char.isalpha() for char in text), text


def test_same_family_reasons_share_one_sentence() -> None:
    """对用户是同一件事的门就说同一句：频率/预算三个闸、模型四种故障。"""
    rate = {_notice("deferred", reason) for reason in (
        "window_hourly_limit",
        "daily_limit",
        "budget_exhausted",
    )}
    assert len(rate) == 1, rate
    model = {_notice("deferred", reason) for reason in (
        "model_unavailable",
        "model_invalid",
        "model_error",
        "stale_revision",
    )}
    assert len(model) == 1, model
    # 而不同族的说法必须**不同**（否则用户分不清"该睡了"和"没改成"）
    assert rate != model
    assert len({_notice("deferred", reason) for reason in REACHABLE_REASONS}) >= 6


def test_unknown_reason_falls_back_by_decision_not_by_guess() -> None:
    """模型自己给的原因码不在表里：按**决定**兜底，不猜它什么意思。

    `declined` 是"她不想去"，`deferred` 是"现在动不了"——把两者说成同一句，
    等于要么把她的选择说成技术故障，要么替她拒绝。
    """
    declined = _notice("declined", "feels_wrong")
    deferred = _notice("deferred", "weird_code")
    assert declined and deferred and declined != deferred
    # 她的选择不能被说成技术故障；"现在动不了"也不能被说成"她不去"
    assert "没改成" not in declined, declined
    assert "不去" not in deferred, deferred
    # 没有原因码也一样（`ReplanOutcome.reason_code` 允许 None）
    assert _notice("declined", None) == declined
    assert _notice("deferred", None) == deferred

