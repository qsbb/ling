"""梦服务与跨夜结算的纯逻辑：醒点换算、prompt 边界、输出校验、I_wake 公式。

为什么这里要跟 ``agenda.timeline`` 对拍：跨域闸门只允许 dream 域 import 邻居的
``.service`` 模块，``agenda.timeline.sleep_anchors_for_day`` 不能直接 import，
所以 dream 里用**逐字同规则**的墙钟/DST 换算得到 wake 瞬时。这条对拍测试就是
"两边语义等价"的合同——哪天 agenda 那边改了，这里会红。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from ling.domains.affect.service import (
    HIGH_AROUSAL_EMOTIONS,
    NEGATIVE_EMOTIONS,
    SLEEP_AXIS_REGRESSION,
    SLEEP_FLOOR_RATIO,
    SLEEP_INTENSITY_CAP,
    SLEEP_SIGNIFICANT_MIN,
    is_high_arousal,
    is_negative,
    linear_intensity_at,
    overnight_intensity,
    regress_toward_zero,
    sleep_floor_applies,
)
from ling.domains.agenda.timeline import (
    RoutineSnapshot,
    sleep_anchors_for_day,
)
from ling.domains.agenda.timeline import (
    local_minute_instant as agenda_local_minute_instant,
)
from ling.domains.dream.fragments import Candidate, CueTuple, Score, ScoredCue, ThreatPlan
from ling.domains.dream.service import (
    DREAM_DUE_BEFORE_WAKE_MINUTES,
    DREAM_LEAD_MINUTES,
    DREAM_MISS_AFTER_WAKE_MINUTES,
    MAX_NARRATIVE_CHARS,
    DreamValidationError,
    NightWindow,
    build_dream_prompt,
    local_minute_instant,
    parse_dream_payload,
)

SLEEP_DATE = date(2026, 11, 8)

# (时区, wake, rest, 本地日) —— 覆盖跨日作息、DST 春季跳变/秋季回拨、
# 以及 wake 恰好落在"不存在/重复"的本地时刻。
WAKE_CASES = (
    ("Asia/Shanghai", 8 * 60, 23 * 60, date(2026, 11, 8)),
    ("Asia/Shanghai", 10 * 60, 2 * 60, date(2026, 11, 8)),
    ("America/New_York", 8 * 60, 23 * 60, date(2026, 3, 8)),
    ("America/New_York", 8 * 60, 23 * 60, date(2026, 11, 1)),
    ("America/New_York", 2 * 60 + 30, 23 * 60, date(2026, 3, 8)),
    ("America/New_York", 1 * 60 + 30, 23 * 60, date(2026, 11, 1)),
)


# ------------------------------------------------------------------ 醒点换算


@pytest.mark.parametrize(("timezone", "wake", "rest", "day"), WAKE_CASES)
def test_local_minute_matches_agenda(
    timezone: str, wake: int, rest: int, day: date
) -> None:
    """dream 的墙钟换算是 agenda 的同一条规则（DST 也不许分叉）。"""
    for minute in (0, wake, rest, 12 * 60, 23 * 60 + 59):
        assert local_minute_instant(day, minute, timezone) == agenda_local_minute_instant(
            day, minute, timezone
        )


@pytest.mark.parametrize(("timezone", "wake", "rest", "day"), WAKE_CASES)
def test_wake_instant_is_the_sleep_anchor_end(
    timezone: str, wake: int, rest: int, day: date
) -> None:
    """`wake_at` 必须等于 ``sleep_anchors_for_day`` 里那一段的右端。

    午夜只切**计划段**：``rest > wake`` 时 D 有两个锚点（``[00:00,wake)`` 与
    ``[rest,24:00)``），但"醒来"只发生在那段 ``[00:00,wake)`` 的右端——一晚
    不等于一段。
    """
    routine = RoutineSnapshot(
        wake_minute=wake, rest_minute=rest, revision=1, timezone=timezone
    )
    anchors = sleep_anchors_for_day(day, timezone, routine)
    wake_at = local_minute_instant(day, wake, timezone)
    if wake == rest:
        assert anchors == [], "wake == rest 表示没有有效睡眠窗"
        return
    assert any(anchor.end_at == wake_at for anchor in anchors), (
        "wake 瞬时必须是某个睡眠锚点的右端"
    )


def test_night_window_schedule_matches_design_values() -> None:
    """梦的生成窗口：醒前 30–60 分钟生成，醒后 30 分钟仍未跑到标 missed。"""
    bed = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
    wake = datetime(2026, 11, 8, 0, 0, tzinfo=UTC)
    window = NightWindow(
        sleep_date=SLEEP_DATE, bed_at=bed, wake_at=wake, timezone="Asia/Shanghai",
        routine_revision=3,
    )
    assert DREAM_LEAD_MINUTES == 60
    assert DREAM_DUE_BEFORE_WAKE_MINUTES == 45
    assert DREAM_MISS_AFTER_WAKE_MINUTES == 30
    assert window.generate_from == wake - timedelta(minutes=60)
    assert window.due_at == wake - timedelta(minutes=45)
    assert window.late_after == wake + timedelta(minutes=30)
    assert window.minutes == 9 * 60


# ------------------------------------------------------------------ prompt


def _scored_cue(
    *, cue_id: str, source: str, text_hint: str = "", emotion: str = ""
) -> ScoredCue:
    candidate = Candidate(
        cue_id=cue_id,
        kind="experience",
        provenance="self_experience",
        source_ref=source,
        occurred_at=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
        as_tuple=CueTuple(person="自己", place="厨房", action="做饭", emotion=emotion),
        signaled_intensity=0.4,
        text_hint=text_hint,
    )
    score = Score(
        recency=0.8, emotion=0.4, unfinished=0.0, persona=0.0, body=0.0, jitter=0.1, total=0.35
    )
    return ScoredCue(cue=candidate, score=score)


def test_prompt_carries_only_tuples_and_never_source_text() -> None:
    secret = "在厨房把番茄切成小块，然后接到妈妈的电话说外婆住院了"
    cues = [
        _scored_cue(cue_id="cue:experience:e1", source="self_experience:e1", text_hint=secret),
        _scored_cue(cue_id="cue:signal:s1", source="affect_signal:s1", emotion="uneasy"),
    ]
    prompt = build_dream_prompt(
        name="ling",
        sleep_date=SLEEP_DATE,
        timezone="Asia/Shanghai",
        cues=cues,
        transforms=[],
        threat=ThreatPlan(present=False, escape_allowed=False, outcome="none"),
        affect_summary={"uneasy": 0.4},
        persona={"temperament": "安静"},
    )
    assert "cue:experience:e1" in prompt and "cue:signal:s1" in prompt
    assert "厨房" in prompt and "做饭" in prompt
    assert secret not in prompt, "素材原文绝不能进 prompt"
    for fragment in ("番茄", "外婆", "住院"):
        assert fragment not in prompt
    assert "虚构梦景" in prompt
    assert "禁止逐字复述" in prompt
    assert '"present": false' in prompt


# ------------------------------------------------------------------ 输出校验


def _payload(**overrides: object) -> str:
    base: dict[str, object] = {
        "title": "走廊里的水",
        "narrative": "走廊尽头有一扇门，门后是水，我踩进去却没有声音。",
        "mood": "uneasy",
        "used_cue_ids": ["cue:experience:e1", "cue:signal:s1"],
        "transforms_used": ["swap_place"],
        "threat_present": False,
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


CUE_SOURCES = {
    "cue:experience:e1": "self_experience:e1",
    "cue:signal:s1": "affect_signal:s1",
}
PLANNED = ["swap_place", "time_reverse"]
NO_THREAT = ThreatPlan(present=False, escape_allowed=False, outcome="none")


def test_valid_payload_is_accepted() -> None:
    draft = parse_dream_payload(
        _payload(), cue_sources=CUE_SOURCES, planned_transforms=PLANNED, threat=NO_THREAT
    )
    assert draft.title == "走廊里的水"
    assert draft.cue_ids == ("cue:experience:e1", "cue:signal:s1")
    assert draft.transform_ids == ("swap_place",)
    assert draft.threat_present is False


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"extra": 1}, "未定义字段"),
        ({"used_cue_ids": ["cue:experience:e1", "cue:made_up"]}, "未输入过的 id"),
        ({"used_cue_ids": ["cue:experience:e1"]}, "至少要 2 个"),
        ({"transforms_used": ["object_animation"]}, "没计划的算子"),
        ({"transforms_used": []}, "transforms_used 必须是非空数组"),
        ({"narrative": "短" * (MAX_NARRATIVE_CHARS + 1)}, "narrative 超过"),
        ({"threat_present": "false"}, "threat_present 必须是布尔值"),
    ],
)
def test_invalid_payloads_are_rejected(overrides: dict[str, object], needle: str) -> None:
    with pytest.raises(DreamValidationError) as excinfo:
        parse_dream_payload(
            _payload(**overrides),
            cue_sources=CUE_SOURCES,
            planned_transforms=PLANNED,
            threat=NO_THREAT,
        )
    assert needle in str(excinfo.value)


def test_two_cues_from_the_same_source_are_rejected() -> None:
    """两条 cue 同源 = 只有一条素材，等于拿一条经历硬编一整篇梦。"""
    with pytest.raises(DreamValidationError) as excinfo:
        parse_dream_payload(
            _payload(used_cue_ids=["cue:experience:e1", "cue:experience:e2"]),
            cue_sources={
                **CUE_SOURCES,
                "cue:experience:e2": "self_experience:e1",
            },
            planned_transforms=PLANNED,
            threat=NO_THREAT,
        )
    assert "只来自 1 个来源" in str(excinfo.value)


def test_code_fence_is_rejected() -> None:
    fenced = "```json\n" + _payload() + "\n```"
    with pytest.raises(DreamValidationError):
        parse_dream_payload(
            fenced, cue_sources=CUE_SOURCES, planned_transforms=PLANNED, threat=NO_THREAT
        )


def test_threat_mismatch_is_rejected_with_its_own_reason_code() -> None:
    """威胁是分布上限：模型不能自己把某一晚变成威胁梦。"""
    with pytest.raises(DreamValidationError) as excinfo:
        parse_dream_payload(
            _payload(threat_present=True),
            cue_sources=CUE_SOURCES,
            planned_transforms=PLANNED,
            threat=NO_THREAT,
        )
    assert getattr(excinfo.value, "reason_code", "") == "dream_threat_mismatch"


def test_verbatim_recitation_is_rejected() -> None:
    source = "在厨房把番茄切成小块，然后接到妈妈的电话说外婆住院了"
    with pytest.raises(DreamValidationError) as excinfo:
        parse_dream_payload(
            _payload(narrative="梦里我" + source),
            cue_sources=CUE_SOURCES,
            planned_transforms=PLANNED,
            threat=NO_THREAT,
            source_texts=[source],
        )
    assert getattr(excinfo.value, "reason_code", "") == "dream_recites_source"


# ------------------------------------------------------------------ 跨夜结算公式


def test_i_wake_uses_floor_when_linear_decay_is_lower() -> None:
    """线性衰减更低时，负性/高唤醒分量保留 0.6×I_bed（但不超过 0.35）。"""
    assert SLEEP_FLOOR_RATIO == 0.6
    assert overnight_intensity(linear=0.05, bed=0.4, floor_applies=True) == pytest.approx(0.24)
    assert overnight_intensity(linear=0.05, bed=0.4, floor_applies=False) == pytest.approx(0.05)


def test_i_wake_keeps_linear_value_when_it_is_higher() -> None:
    assert overnight_intensity(linear=0.30, bed=0.4, floor_applies=True) == pytest.approx(0.30)


def test_i_wake_is_capped_at_0_35() -> None:
    assert SLEEP_INTENSITY_CAP == 0.35
    assert overnight_intensity(linear=0.5, bed=0.9, floor_applies=True) == pytest.approx(0.35)
    assert overnight_intensity(linear=0.1, bed=0.9, floor_applies=True) == pytest.approx(0.35)


def test_floor_only_applies_to_significant_negative_or_high_arousal_components() -> None:
    assert SLEEP_SIGNIFICANT_MIN == 0.2
    # 睡前不显著：不套下限。
    assert not sleep_floor_applies(
        emotion="uneasy", valence=-0.8, arousal=0.9, bed_intensity=0.1
    )
    # 负性（按 valence 或按标签）。
    assert sleep_floor_applies(emotion="uneasy", valence=-0.3, arousal=None, bed_intensity=0.5)
    assert is_negative("uneasy", None) and is_negative("tired", None)
    assert is_negative("warm", -0.1) and not is_negative("warm", 0.4)
    assert "uneasy" in NEGATIVE_EMOTIONS
    # 高唤醒（arousal ≥ 0.5 或标签）。
    assert is_high_arousal("calm", 0.6) and not is_high_arousal("calm", 0.2)
    assert "curious" in HIGH_AROUSAL_EMOTIONS
    assert sleep_floor_applies(emotion="calm", valence=0.2, arousal=0.6, bed_intensity=0.5)
    # 普通的好心情不套下限：睡一觉不该把开心"抬高"。
    assert not sleep_floor_applies(emotion="content", valence=0.6, arousal=0.1, bed_intensity=0.8)


def test_axes_regress_forty_percent_toward_zero() -> None:
    assert SLEEP_AXIS_REGRESSION == 0.4
    assert regress_toward_zero(0.5) == pytest.approx(0.3)
    assert regress_toward_zero(-0.5) == pytest.approx(-0.3)
    assert regress_toward_zero(None) is None


def test_linear_intensity_matches_the_existing_decay() -> None:
    """I_linear 必须与 ``AffectService.current()`` 同一条线性衰减口径。"""
    updated = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)
    later = updated + timedelta(hours=8)
    assert linear_intensity_at(
        intensity=0.8, decay_per_hour=0.05, updated_at=updated, at=later
    ) == pytest.approx(0.8 - 0.05 * 8)
    # 归零后不再变负（现有 decay_per_hour≈0.15 下 8 小时早就归零了）。
    assert (
        linear_intensity_at(intensity=0.8, decay_per_hour=0.15, updated_at=updated, at=later)
        == 0.0
    )
