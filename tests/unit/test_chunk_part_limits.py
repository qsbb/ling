"""A7「分段结果无渠道长度上限」的回归测试。

**缺陷原状**（`chunking.py` 旧版）：

```python
parts = segments[:MAX_PARTS_PRIVATE]
parts[-1] = "".join(segments[MAX_PARTS_PRIVATE - 1 :])   # 溢出并回最后一段，无长度上限
```

`MAX_SEGMENT_CHARS=220` 只是**切分阈值**（超过它才值得切），它不约束最终段长。
于是溢出部分被无上限地并回最后一段：一条 3340 字的回复在 qqbot（1500 上限）
下产出 `[105, 105, 110, 110, 2910]`——前四段发得出去，**尾段 2910 字被渠道整条
拒收**，`bridge._deliver` 一遇拒绝就 `break`，结果是
「部分送达 + 尾段永久丢失」，本地 operation 还记成 failed。

修复方向：**按渠道 `max_text_length` 二次切分，而不是整段拒绝**。
本文件盯三件事：

1. 任何段都不超过渠道上限（qqbot 1500 / onebot 4000 两种真实取值）
2. **没有任何段被丢弃**（拼回去等于原文，段数覆盖全部内容）
3. `MAX_PARTS_PRIVATE` 的防刷屏意图仍在（正常长度不膨胀成几十个气泡）

变异验证（回退修复后必须变红）：把 `_pack_units` 换回"前 N 段 + 其余全并进
最后一段"，`test_qqbot_long_reply_no_part_exceeds_channel_limit` 立刻失败。
"""

from __future__ import annotations

import pytest

from ling.capabilities.chunking import (
    DEFAULT_PART_LIMIT_CHARS,
    MAX_PARTS_PRIVATE,
    MAX_SEGMENT_CHARS,
    plan_parts,
)

# 各渠道真实上限（出处见任务单：qq 2000 / onebot 4000 / qqbot 1500 / wechat 2048）
QQBOT_MAX_TEXT_LENGTH = 1500
ONEBOT_MAX_TEXT_LENGTH = 4000


def _long_reply(chars: int) -> str:
    """造一段"有句子边界"的长回复：每句约 40 字，句末带句号。

    用句子而不是纯字符填充，是因为 `split_semantic` 只在句子边界收段——
    纯字符长句会走另一条分支（硬切），测不到"溢出合并"这条路径。
    """
    sentence = "今天下午去了一趟图书馆，借了两本书，回来路上还买了点菜。"
    body = sentence * (chars // len(sentence) + 1)
    return body[:chars]


def _squash(value: str) -> str:
    import re

    return re.sub(r"\s+", "", value)


@pytest.mark.parametrize(
    ("limit", "chars"),
    [
        (QQBOT_MAX_TEXT_LENGTH, 3340),  # 旧实现产出 2910 字的尾段
        (QQBOT_MAX_TEXT_LENGTH, 5000),
        (QQBOT_MAX_TEXT_LENGTH, 7400),  # 恰好 5 段装不下（ceil=5 但装不进）
        (QQBOT_MAX_TEXT_LENGTH, 12000),
        (ONEBOT_MAX_TEXT_LENGTH, 9000),
        (ONEBOT_MAX_TEXT_LENGTH, 20000),
    ],
)
def test_no_part_exceeds_channel_limit_and_nothing_is_dropped(limit: int, chars: int) -> None:
    """**核心断言**：每段 <= 渠道上限，且没有任何段被丢弃。

    这是修复的正面定义：不是"整条拒绝"，也不是"尾段丢字"，
    而是"按渠道上限切完，全部内容都在里面"。
    """
    text = _long_reply(chars)
    parts = plan_parts(text, is_group=False, max_part_chars=limit)

    assert parts, "规划出 0 段"
    assert all(len(part) <= limit for part in parts), (
        f"有段超过渠道上限 {limit}：{[len(p) for p in parts]}"
    )
    # **没有段被丢弃**：拼回去必须等于原文（分段会重排空白，忽略空白差异）
    assert _squash("".join(parts)) == _squash(text), "分段丢字了"
    # 段数足够覆盖全部内容——这是"没丢"的另一种机械检查
    assert sum(len(part) for part in parts) == len(text), "段长总和与原文不符"


def test_qqbot_long_reply_no_part_exceeds_channel_limit() -> None:
    """qqbot（1500）是最容易触发的渠道：旧实现在这里丢尾段。

    单独再写一条（不带参数化），因为这是**变异验证的靶子**：
    回退修复后这一条必须变红，而它没有参数化前缀便于肉眼定位。
    """
    text = _long_reply(3340)
    parts = plan_parts(text, is_group=False, max_part_chars=QQBOT_MAX_TEXT_LENGTH)

    assert max(len(p) for p in parts) <= QQBOT_MAX_TEXT_LENGTH, (
        f"尾段超过 qqbot 上限：{[len(p) for p in parts]}"
    )
    assert _squash("".join(parts)) == _squash(text)


def test_onebot_long_reply_no_part_exceeds_channel_limit() -> None:
    """onebot（4000）同样必须段段合规。

    上限更大不等于不会踩：7400 字在旧实现下会被并成 `[105,105,110,110,6910]`。
    """
    text = _long_reply(7400)
    parts = plan_parts(text, is_group=False, max_part_chars=ONEBOT_MAX_TEXT_LENGTH)

    assert max(len(p) for p in parts) <= ONEBOT_MAX_TEXT_LENGTH, (
        f"尾段超过 onebot 上限：{[len(p) for p in parts]}"
    )
    assert _squash("".join(parts)) == _squash(text)


def test_splitting_threshold_is_not_the_channel_limit() -> None:
    """切分阈值（220）与渠道硬上限是两个概念，不能互相冒充。

    这条钉住"别把 `MAX_SEGMENT_CHARS` 当成渠道上限"：
    段可以远长于 220（因为 220 只决定"切不切"，不决定"能发多长"），
    但绝不能超过渠道上限。
    """
    limit = QQBOT_MAX_TEXT_LENGTH
    text = _long_reply(2000)
    parts = plan_parts(text, is_group=False, max_part_chars=limit)

    assert all(len(p) <= limit for p in parts)
    # 至少有一段明显长于切分阈值——说明段长是按渠道上限而非 220 决定的
    assert max(len(p) for p in parts) > MAX_SEGMENT_CHARS, (
        "段长被切分阈值卡死了：分段本可以更省条数"
    )


def test_unknown_channel_falls_back_to_conservative_limit() -> None:
    """不知道渠道时不能假设"没有上限"。

    拿不到 `max_text_length` 的调用方（Web 展示、纯规划）必须拿到
    一个保守兜底——否则同一段文本在 Web 上"规划成功"、
    到 qqbot 上整条被拒，问题只是被推迟到投递层。
    """
    text = _long_reply(6000)
    parts = plan_parts(text, is_group=False)  # 不传 max_part_chars

    assert all(len(p) <= DEFAULT_PART_LIMIT_CHARS for p in parts), (
        f"兜底上限失效：{[len(p) for p in parts]}"
    )
    assert _squash("".join(parts)) == _squash(text)


def test_max_parts_private_still_caps_normal_length_replies() -> None:
    """**防刷屏意图必须保留**：正常长度的回复不该膨胀成几十个气泡。

    修复前的写法是"前 5 段 + 其余并进最后一段"，段数被硬卡在 5。
    修复后段数可以突破 5——但**只允许突破到"物理上必须"的那个数**。
    这条测的是另一端：文本明明能装进 5 段，就不许拆成更多。
    """
    # 6 段自然单元、总长 900 字：远小于 5*1500，必须并到 <= 5 段
    text = "\n".join(f"第{index}行的内容比较长，用来凑出多个自然分段。" for index in range(6))
    parts = plan_parts(text, is_group=False, max_part_chars=QQBOT_MAX_TEXT_LENGTH)

    assert len(parts) <= MAX_PARTS_PRIVATE, f"正常长度被拆成 {len(parts)} 段：防刷屏失效"
    assert _squash("".join(parts)) == _squash(text)


def test_extreme_length_does_not_produce_unbounded_parts() -> None:
    """极长文本的段数受**内容长度**约束，不是无穷多。

    防刷屏的真实含义：段数是 `ceil(字数/上限)` 的量级，不多造。
    这里用 6 万字 / 1500 上限：理论最少 40 段，实测允许少量余量，
    但绝不能变成"一句一段"的上百段。
    """
    import math

    limit = QQBOT_MAX_TEXT_LENGTH
    text = _long_reply(60000)
    parts = plan_parts(text, is_group=False, max_part_chars=limit)
    minimum = math.ceil(len(text) / limit)

    assert all(len(p) <= limit for p in parts)
    assert _squash("".join(parts)) == _squash(text)
    # 允许 20% 的装箱余量（句子边界导致装不满），但必须同阶
    assert len(parts) <= int(minimum * 1.2) + 2, (
        f"极长文本产出了 {len(parts)} 段（理论下限 {minimum}）：分段失控"
    )


def test_single_oversized_code_block_is_cut_not_dropped() -> None:
    """比渠道上限还大的代码块必须被切开——发不出去等于用户看不到。

    这条是"代码块永远不切开"与"渠道硬上限"冲突时的裁决：
    **物理上限优先**。不切的话整条消息发不出去，用户一个字都看不到。
    """
    code = "```python\n" + "\n".join(f"value_{i} = {i}" for i in range(300)) + "\n```"
    assert len(code) > QQBOT_MAX_TEXT_LENGTH

    parts = plan_parts(code, is_group=False, max_part_chars=QQBOT_MAX_TEXT_LENGTH)

    assert all(len(p) <= QQBOT_MAX_TEXT_LENGTH for p in parts)
    assert _squash("".join(parts)) == _squash(code), "代码块被切开时丢字了"


def test_group_stays_single_part_unless_physically_impossible() -> None:
    """群聊默认恒 1 段（拆多条会绕过 `group_turn_budget`），
    只有"一条物理上发不出去"时才拆——那时不拆等于整条丢失。"""
    normal = _long_reply(600)
    assert len(plan_parts(normal, is_group=True, max_part_chars=QQBOT_MAX_TEXT_LENGTH)) == 1

    oversized = _long_reply(6000)
    parts = plan_parts(oversized, is_group=True, max_part_chars=QQBOT_MAX_TEXT_LENGTH)
    assert len(parts) > 1, "超长群消息不拆等于整条发不出去"
    assert all(len(p) <= QQBOT_MAX_TEXT_LENGTH for p in parts)
    assert _squash("".join(parts)) == _squash(oversized)
