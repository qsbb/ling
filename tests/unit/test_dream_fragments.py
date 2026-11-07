"""梦的候选打分、碎片重混与威胁分布（纯函数）。

这些用例钉住设计 §5 里**比权重更硬**的三条约束：

1. 打分权重与 18h 半衰衰减（工程初值，改它要有回放依据）；
2. 碎片抽取/重混**不产生原文复述**（prompt 里根本没有原文，这里是第二道防线）；
3. 威胁是**分布上限**（≤1/4 晚，脱身 ≤1/3），不是每篇都要有。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from ling.domains.dream.fragments import (
    CROSS_SOURCE_TRANSFORMS,
    MIN_SOURCES,
    NEGATIVE_EMOTION_MULTIPLIER,
    RECENCY_FAR_AFTER_HOURS,
    RECENCY_FAR_FACTOR,
    RECENCY_HALF_LIFE_HOURS,
    THREAT_CYCLE_NIGHTS,
    THREAT_ESCAPE_EVERY,
    TRANSFORMS,
    WEIGHT_BODY,
    WEIGHT_EMOTION,
    WEIGHT_JITTER,
    WEIGHT_PERSONA,
    WEIGHT_RECENCY,
    WEIGHT_UNFINISHED,
    Candidate,
    CueTuple,
    emotion_factor,
    plan_transforms,
    recency_factor,
    recites_source,
    score_candidate,
    select_cues,
    stable_jitter,
    threat_plan,
)

SLEEP_DATE = date(2026, 11, 8)
BED_AT = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)  # 本地 23:00（Asia/Shanghai）


def _candidate(index: int, **overrides: object) -> Candidate:
    payload: dict[str, object] = {
        "cue_id": f"cue:experience:e{index}",
        "kind": "experience",
        "provenance": "self_experience",
        "source_ref": f"self_experience:e{index}",
        "occurred_at": BED_AT - timedelta(hours=index),
        "as_tuple": CueTuple(person="自己", action="读书"),
    }
    payload.update(overrides)
    return Candidate(**payload)  # type: ignore[arg-type]


# ------------------------------------------------------------------ 权重


def test_weights_are_the_documented_ones_and_sum_to_one() -> None:
    """设计 §5 的六个权重：0.30/0.25/0.20/0.15/0.05/0.05，合计 1.0。

    它们全是**工程初值**（调研 §2.3 明说不是论文测出的系数）。这条测试的意义
    不是"证明对"，而是"改权重必须同时改测试和文档"。
    """
    assert (WEIGHT_RECENCY, WEIGHT_EMOTION, WEIGHT_UNFINISHED) == (0.30, 0.25, 0.20)
    assert (WEIGHT_PERSONA, WEIGHT_BODY, WEIGHT_JITTER) == (0.15, 0.05, 0.05)
    assert pytest.approx(1.0) == (
        WEIGHT_RECENCY
        + WEIGHT_EMOTION
        + WEIGHT_UNFINISHED
        + WEIGHT_PERSONA
        + WEIGHT_BODY
        + WEIGHT_JITTER
    )


def test_score_total_is_the_weighted_sum_of_its_factors() -> None:
    score = score_candidate(
        _candidate(
            0,
            signaled_intensity=1.0,
            unfinished=1.0,
            persona_relevance=1.0,
            body_ambience=1.0,
        ),
        actor_id="actor_ling",
        sleep_date=SLEEP_DATE,
        bed_at=BED_AT,
    )
    expected = (
        WEIGHT_RECENCY * score.recency
        + WEIGHT_EMOTION * score.emotion
        + WEIGHT_UNFINISHED * score.unfinished
        + WEIGHT_PERSONA * score.persona
        + WEIGHT_BODY * score.body
        + WEIGHT_JITTER * score.jitter
    )
    assert score.total == pytest.approx(expected)
    # 全部因子为 1 时只有 jitter 不满 → 总分略低于 1。
    assert 0.9 < score.total <= 1.0
    for factor in (score.recency, score.emotion, score.unfinished, score.persona, score.body):
        assert factor == pytest.approx(1.0)


# ------------------------------------------------------------------ 衰减


def test_recency_halves_every_18_hours() -> None:
    assert RECENCY_HALF_LIFE_HOURS == 18.0
    assert recency_factor(BED_AT, bed_at=BED_AT) == pytest.approx(1.0)
    assert recency_factor(BED_AT - timedelta(hours=18), bed_at=BED_AT) == pytest.approx(0.5)
    assert recency_factor(BED_AT - timedelta(hours=24), bed_at=BED_AT) == pytest.approx(
        2 ** (-24 / 18)
    )


def test_recency_of_older_than_a_day_is_multiplied_by_point_three() -> None:
    assert RECENCY_FAR_AFTER_HOURS == 24.0
    assert RECENCY_FAR_FACTOR == 0.3
    age_25h = 2 ** (-25 / 18)
    assert recency_factor(BED_AT - timedelta(hours=25), bed_at=BED_AT) == pytest.approx(
        age_25h * 0.3
    )


def test_recency_has_no_value_without_a_time_or_in_the_future() -> None:
    """没有时间来源（人设/身体）记 0；未来时间按"刚发生"封顶，不凭空加分。"""
    assert recency_factor(None, bed_at=BED_AT) == 0.0
    assert recency_factor(BED_AT + timedelta(hours=3), bed_at=BED_AT) == pytest.approx(1.0)


def test_negative_emotion_is_amplified_and_still_clamped() -> None:
    assert NEGATIVE_EMOTION_MULTIPLIER == 1.3
    assert emotion_factor(0.5, negative=True) == pytest.approx(0.65)
    assert emotion_factor(0.9, negative=True) == pytest.approx(1.0)
    assert emotion_factor(0.9, negative=False) == pytest.approx(0.9)


def test_jitter_is_stable_and_bounded() -> None:
    first = stable_jitter(actor_id="actor_ling", sleep_date=SLEEP_DATE, cue_id="cue:x")
    second = stable_jitter(actor_id="actor_ling", sleep_date=SLEEP_DATE, cue_id="cue:x")
    other = stable_jitter(actor_id="actor_ling", sleep_date=SLEEP_DATE, cue_id="cue:y")
    assert first == second
    assert 0.0 <= first <= 1.0
    assert first != other


# ------------------------------------------------------------------ 来源门槛


def test_two_sources_are_required() -> None:
    """一条素材 + 人设/身体底色不足以生成梦：应当 ``no_dream``（0 次模型调用）。"""
    single = select_cues(
        [_candidate(0)], actor_id="actor_ling", sleep_date=SLEEP_DATE, bed_at=BED_AT
    )
    assert single == []

    only_ambience = select_cues(
        [
            _candidate(0),
            Candidate(
                cue_id="cue:persona:persona_ling",
                kind="persona",
                provenance="persona",
                source_ref="persona:persona_ling",
                occurred_at=None,
                as_tuple=CueTuple(person="自己", action="温和"),
                persona_relevance=1.0,
                material=False,
            ),
        ],
        actor_id="actor_ling",
        sleep_date=SLEEP_DATE,
        bed_at=BED_AT,
    )
    assert only_ambience == [], "底色不能代替生活素材凑够两个来源"
    assert MIN_SOURCES == 2


def test_selection_dedupes_by_source_and_caps_the_count() -> None:
    candidates = [_candidate(index) for index in range(6)]
    # 同一个来源出现两次：只留分高的那一条。
    duplicate = _candidate(0, cue_id="cue:experience:e0-again")
    selected = select_cues(
        [*candidates, duplicate],
        actor_id="actor_ling",
        sleep_date=SLEEP_DATE,
        bed_at=BED_AT,
        max_cues=3,
    )
    assert len(selected) == 3
    assert len({item.cue.source_ref for item in selected}) == 3
    # 确定性：同样的输入给同样的顺序。
    again = select_cues(
        [*candidates, duplicate],
        actor_id="actor_ling",
        sleep_date=SLEEP_DATE,
        bed_at=BED_AT,
        max_cues=3,
    )
    assert [item.cue.cue_id for item in selected] == [item.cue.cue_id for item in again]


def test_candidates_without_any_tuple_are_dropped() -> None:
    """四元组全空的候选不算碎片：它没有"人/地点/动作/情绪"可重混。"""
    selected = select_cues(
        [_candidate(0, as_tuple=CueTuple()), _candidate(1), _candidate(2)],
        actor_id="actor_ling",
        sleep_date=SLEEP_DATE,
        bed_at=BED_AT,
    )
    assert [item.cue.cue_id for item in selected] == [
        "cue:experience:e1",
        "cue:experience:e2",
    ]


# ------------------------------------------------------------------ 重混


def test_transforms_cross_two_sources_and_are_deterministic() -> None:
    cues = select_cues(
        [_candidate(0), _candidate(1), _candidate(2)],
        actor_id="actor_ling",
        sleep_date=SLEEP_DATE,
        bed_at=BED_AT,
    )
    steps = plan_transforms(cues, actor_id="actor_ling", sleep_date=SLEEP_DATE)
    assert len(steps) == 2
    assert [step.transform_id for step in steps][0] in CROSS_SOURCE_TRANSFORMS
    assert all(step.transform_id in TRANSFORMS for step in steps)

    sources = {item.cue.cue_id: item.cue.source_ref for item in cues}
    for step in steps:
        assert len(set(step.cue_ids)) == 2
        assert sources[step.cue_ids[0]] != sources[step.cue_ids[1]], "必须跨来源重混"

    again = plan_transforms(cues, actor_id="actor_ling", sleep_date=SLEEP_DATE)
    assert steps == again, "同一晚重放必须得到同一组算子"


def test_transform_details_only_use_tuple_labels_not_source_text() -> None:
    secret = "我在厨房把番茄切成了很小的块然后哭了"
    cues = select_cues(
        [
            _candidate(0, text_hint=secret),
            _candidate(1, text_hint=secret),
        ],
        actor_id="actor_ling",
        sleep_date=SLEEP_DATE,
        bed_at=BED_AT,
    )
    steps = plan_transforms(cues, actor_id="actor_ling", sleep_date=SLEEP_DATE)
    rendered = " ".join(step.detail for step in steps)
    assert "番茄" not in rendered and "哭了" not in rendered


# ------------------------------------------------------------------ 复述检测


def test_recites_source_catches_a_verbatim_copy() -> None:
    source = "读完了第二章海边的卡夫卡这一章写得很慢"
    narrative = "我坐在窗边，读完了第二章海边的卡夫卡这一章写得很慢，然后天亮了"
    assert recites_source(narrative, [source]) == source


def test_recites_source_ignores_short_overlap_and_remixed_text() -> None:
    source = "读完了第二章海边的卡夫卡这一章写得很慢"
    # 只重叠书名（< 12 字窗口）不算复述：梦本来就会用到片段词。
    narrative = "我坐在窗边翻一本叫海边的卡夫卡的书，然后天亮了"
    assert recites_source(narrative, [source]) is None
    # 只有四元组重混时，没有任何连续原文。
    assert recites_source("陌生的走廊里有人读书，字从墙上渗出来", [source]) is None


# ------------------------------------------------------------------ 威胁分布


def test_threat_is_capped_at_one_night_in_four() -> None:
    nights = [SLEEP_DATE + timedelta(days=offset) for offset in range(400)]
    plans = [threat_plan("actor_ling", night) for night in nights]
    present = sum(1 for plan in plans if plan.present)
    assert THREAT_CYCLE_NIGHTS == 4
    assert present == len(nights) // THREAT_CYCLE_NIGHTS, "威胁必须严格 ≤1/4 晚"
    # 任意连续 4 晚里恰好 1 晚有威胁（软分布约束，不是单晚写死）。
    for start in range(0, len(nights) - 4, 37):
        window = plans[start : start + 4]
        assert sum(1 for plan in window if plan.present) == 1


def test_escape_is_at_most_one_third_of_threat_nights() -> None:
    nights = [SLEEP_DATE + timedelta(days=offset) for offset in range(400)]
    plans = [threat_plan("actor_ling", night) for night in nights if threat_plan("actor_ling", night).present]
    escapes = [plan for plan in plans if plan.escape_allowed]
    assert THREAT_ESCAPE_EVERY == 3
    assert escapes, "不能永远逃不掉"
    assert len(escapes) * THREAT_ESCAPE_EVERY <= len(plans)
    assert all(plan.outcome == "escape" for plan in escapes)
    assert all(plan.outcome == "no_escape" for plan in plans if not plan.escape_allowed)


def test_threat_plan_is_stable_and_role_specific() -> None:
    first = threat_plan("actor_ling", SLEEP_DATE)
    assert first == threat_plan("actor_ling", SLEEP_DATE)
    # 不同角色不会永远同一晚做梦（偏移来自角色 id）。
    assert any(
        threat_plan("actor_other", SLEEP_DATE + timedelta(days=offset)).present
        != threat_plan("actor_ling", SLEEP_DATE + timedelta(days=offset)).present
        for offset in range(4)
    )
