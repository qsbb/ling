"""表达策略：记忆该"怎么说"，不只是"用哪些"。

参照 同类实现 的 mention_memory / tone_memory /
uncertain_memory 分层。把所有记忆平铺成"你记得的相关事实"，模型就会把
不确定的也当事实说出来——这比"少记一件事"更容易伤害信任。
"""

from __future__ import annotations

import pytest

from ling.contracts.v1 import Origin, Visibility
from ling.domains.memory.service import MemoryHit, classify_usage, render_evidence


def _hit(text: str, confidence: float, origin: Origin) -> MemoryHit:
    return MemoryHit(
        memory_id=f"m_{abs(hash(text)) % 1000}",
        kind="assertion",
        text=text,
        origin=origin,
        visibility=Visibility.subject_private,
        confidence=confidence,
        source_event_ids=[],
        score=0.1,
        valid_until=None,
    )


# ------------------------------------------------------------------ 分类


def test_high_confidence_user_statement_is_mention() -> None:
    assert classify_usage(_hit("用户喜欢下雨天", 0.9, Origin.user_statement)) == "mention"


def test_mid_confidence_is_tone() -> None:
    assert classify_usage(_hit("用户可能养猫", 0.6, Origin.external_observation)) == "tone"


def test_low_confidence_is_uncertain() -> None:
    assert classify_usage(_hit("用户也许住杭州", 0.2, Origin.user_statement)) == "uncertain"


def test_model_inference_always_uncertain() -> None:
    """模型自己推测的内容，**哪怕置信度标到 0.99** 也只能算隐约印象。

    那是它自己想的，不是对方说过的。这条如果放松，角色就会把臆测
    当事实复述出来。
    """
    assert classify_usage(_hit("用户应该喜欢猫", 0.99, Origin.model_inference)) == "uncertain"


@pytest.mark.parametrize("confidence", [0.0, 0.3, 0.75, 1.0])
def test_model_inference_never_mention(confidence: float) -> None:
    hit = _hit("推测", confidence, Origin.model_inference)
    assert classify_usage(hit) != "mention"


# ------------------------------------------------------------------ 渲染


def test_render_groups_by_usage() -> None:
    hits = [
        _hit("用户喜欢下雨天", 0.9, Origin.user_statement),
        _hit("用户可能养猫", 0.6, Origin.external_observation),
        _hit("用户也许住杭州", 0.2, Origin.user_statement),
    ]
    text = render_evidence(hits)
    assert "可以直接说出来" in text
    assert "不要当成确凿事实复述" in text
    assert "不要主动断言" in text
    # 三组都要出现各自的条目
    assert "用户喜欢下雨天" in text
    assert "用户可能养猫" in text
    assert "用户也许住杭州" in text


def test_render_only_present_groups() -> None:
    """只有一种类别时不该输出空组标题。"""
    text = render_evidence([_hit("用户喜欢下雨天", 0.9, Origin.user_statement)])
    assert "可以直接说出来" in text
    assert "不要主动断言" not in text


def test_render_empty_is_empty() -> None:
    assert render_evidence([]) == ""


def test_mention_carries_origin_and_confidence() -> None:
    """可以直接说出来的那组要带来源与置信度，便于模型/审计判断依据。"""
    text = render_evidence([_hit("用户喜欢下雨天", 0.9, Origin.user_statement)])
    assert "user_statement" in text
    assert "0.90" in text


def test_tone_group_omits_provenance() -> None:
    """语气底色那组不点明出处——写出来反而诱导模型去"引用"它。"""
    text = render_evidence([_hit("用户可能养猫", 0.6, Origin.external_observation)])
    assert "external_observation" not in text
