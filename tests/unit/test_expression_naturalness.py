"""表达自然度：去掉客服腔 + 按语义分段。

这两项是路线图里"像人贡献 ÷ 实现成本"排前两位的改动：
- `FollowupGuard`：真人不会每句后面都接"还有什么可以帮您的吗"
- `split_semantic`：按一口气说完一件事分段，不按固定字数切

**关键约束是"不能删过头"**——删掉真正关心对方的问题，
比留着客服腔更糟。
"""

from __future__ import annotations

import pytest

from ling.capabilities.chunking import (
    DEFAULT_TARGET_CHARS,
    MAX_SEGMENT_CHARS,
    split_semantic,
)
from ling.capabilities.followup import is_service_tail, strip_service_tail

# ------------------------------------------------------------------ 客服腔


@pytest.mark.parametrize(
    "tail",
    [
        "还有什么可以帮你的吗？",
        "还有需要吗？",
        "还有什么需要我做的？",
        "需要我帮你查一下吗？",
        "要不要我再看看？",
        "还有其他问题吗？",
        "有别的需要吗？",
        "随时告诉我。",
        "希望能帮到你。",
    ],
)
def test_service_tails_detected(tail: str) -> None:
    assert is_service_tail(tail)


@pytest.mark.parametrize(
    "tail",
    [
        "我记得你说周五面试，准备得怎么样了？",
        "你今天累不累？",
        "你身体还好吗？",
        "那就这么定了。",
        "我在读一本讲城市的老书。",
        "明天降温，多穿点。",
    ],
)
def test_genuine_care_not_stripped(tail: str) -> None:
    """真正关心对方的问题不能删——这是最容易误伤的一类。"""
    assert not is_service_tail(tail)


def test_strip_removes_only_last_sentence() -> None:
    text = "今天确实挺冷的，出门多穿点。还有什么可以帮你的吗？"
    out, stripped = strip_service_tail(text)
    assert stripped is True
    assert out == "今天确实挺冷的，出门多穿点。"


def test_strip_keeps_content_when_no_tail() -> None:
    text = "我在读一本讲城市的老书。"
    out, stripped = strip_service_tail(text)
    assert stripped is False
    assert out == text


def test_strip_never_returns_empty() -> None:
    """整条就是一句客服腔时，删掉会变成空消息——空比客服腔更糟。"""
    text = "还有什么可以帮你的吗？"
    out, stripped = strip_service_tail(text)
    assert stripped is False
    assert out == text


def test_strip_middle_service_sentence_untouched() -> None:
    """只处理最后一句；中间的同类句子不动（避免误删正常内容）。"""
    text = "还有需要吗？我今天在整理书架。"
    out, stripped = strip_service_tail(text)
    assert stripped is False
    assert out == text


def test_strip_handles_empty() -> None:
    assert strip_service_tail("") == ("", False)


# ------------------------------------------------------------------ 分段


def test_short_text_is_one_segment() -> None:
    assert split_semantic("嗯，我在。") == ["嗯，我在。"]


def test_empty_returns_empty_list() -> None:
    assert split_semantic("") == []
    assert split_semantic("   ") == []


def test_short_sentences_merged() -> None:
    """一句一个气泡反而更碎，短句要合并。"""
    assert len(split_semantic("嗯。好的。行。那就这样。")) == 1


def test_long_text_splits_at_sentence_boundary() -> None:
    long = ("今天早上出门的时候天还挺冷，风刮得脸疼。" * 3
            + "我绕路去买了杯热豆浆，路上遇到邻居在遛狗。" * 3
            + "那只狗特别兴奋，一直往我身上扑，把我裤子都弄脏了。" * 3)
    segs = split_semantic(long)
    assert len(segs) > 1, "长文本没有分段"
    # 每段都以句末标点结尾 = 没有切在句子中间
    assert all(s.rstrip()[-1] in "。！？…" for s in segs), f"切在句子中间: {segs}"


def test_code_fence_never_split() -> None:
    """代码块切开就不可执行了。"""
    body = "x = 1\n" * 40
    text = f"看这段代码。\n```python\n{body}```\n大概就是这样。"
    segs = split_semantic(text)
    with_code = [s for s in segs if "```" in s]
    assert len(with_code) == 1, "代码块被切开了"
    assert with_code[0].count("```") % 2 == 0, "代码块围栏不成对"
    assert "x = 1" in with_code[0]


def test_quoted_speech_never_split() -> None:
    """引用的原话被切断就变了意思。"""
    quote = "「我下周一定来，不会放你鸽子。」"
    text = ("他当时是这么说的：" + quote + "我到现在还记得。") * 3
    segs = split_semantic(text)
    for s in segs:
        assert s.count("「") == s.count("」"), f"引号被切开: {s}"


def test_no_segment_exceeds_hard_max() -> None:
    text = "这句话很长" * 100
    for seg in split_semantic(text):
        assert len(seg) <= MAX_SEGMENT_CHARS


def test_target_is_reasonable() -> None:
    """目标段长要在一个"一口气说得完"的量级。"""
    assert 40 <= DEFAULT_TARGET_CHARS <= 200


def test_segments_reassemble_to_original_content() -> None:
    """分段只改变切点，不丢内容。"""
    text = "第一件事。第二件事很重要，需要多说两句。第三件事。"
    segs = split_semantic(text, target_chars=10, max_chars=30)
    assert "".join(segs).replace(" ", "") == text.replace(" ", "")