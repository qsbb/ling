"""语义分段：按意思切，不按字数切。

为什么重要：把人说的话按固定字数切开会立刻显得机械——一句半、
断在词中间。真人是按"一口气说一件事"分段的。

参照同类实现的做法
的思路，但保持**确定性**（不调模型，也不用随机延迟）。

三条硬约束：
1. **代码块永远不切开** —— 切开会把可执行内容变成不可执行
2. **引号内不切开** —— 「引用的原话」被切断就变了意思
3. **短句合并** —— 一句一个气泡反而更碎；短句要合到有分量为止
"""

from __future__ import annotations

import re

# 目标段长。太短会碎，太长一次说完又显压迫。
DEFAULT_TARGET_CHARS = 90
# 单段硬上限：再长就必然要切。
MAX_SEGMENT_CHARS = 220

# 句子边界（保留标点）
_SENTENCE_END = re.compile(r"(?<=[。！？!?…])|(?<=\.\s)|(?<=\n)")
# fenced code block：```...```
_CODE_FENCE = re.compile(r"```.*?```", re.S)
# 行内代码与中英文引号
_INLINE_PROTECTED = re.compile(r"`[^`\n]+`|「[^」]*」|『[^』]*』|\"[^\"]{2,}\"")


def _split_protected(text: str) -> list[tuple[str, bool]]:
    """把文本切成 [(片段, 是否受保护)]。

    受保护片段（代码块/行内代码/引号）**不参与切分**，
    它们原样作为一个整体进出。
    """
    spans: list[tuple[int, int]] = [m.span() for m in _CODE_FENCE.finditer(text)]
    # 行内受保护只在代码块之外找
    covered = [False] * len(text)
    for start, end in spans:
        for i in range(start, end):
            covered[i] = True
    for m in _INLINE_PROTECTED.finditer(text):
        if any(covered[m.start() : m.end()]):
            continue
        spans.append(m.span())
    spans.sort()
    if not spans:
        return [(text, False)]
    out: list[tuple[str, bool]] = []
    cursor = 0
    for start, end in spans:
        if start > cursor:
            out.append((text[cursor:start], False))
        out.append((text[start:end], True))
        cursor = end
    if cursor < len(text):
        out.append((text[cursor:], False))
    return out


def _split_sentences(text: str) -> list[str]:
    return [p for p in _SENTENCE_END.split(text) if p]


def split_semantic(
    text: str,
    *,
    target_chars: int = DEFAULT_TARGET_CHARS,
    max_chars: int = MAX_SEGMENT_CHARS,
) -> list[str]:
    """把一段话切成若干"一口气说得完"的段。

    返回至少一个元素；空输入返回空列表。
    """
    if not text or not text.strip():
        return []
    segments: list[str] = []
    buffer = ""

    def flush() -> None:
        nonlocal buffer
        if buffer.strip():
            segments.append(buffer.strip())
        buffer = ""

    for piece, protected in _split_protected(text):
        if protected:
            # 受保护片段整块处理：放不下就先把当前段收掉，
            # 但它自己绝不被切开
            if len(buffer) + len(piece) > max_chars:
                flush()
            buffer += piece
            if len(buffer) >= target_chars:
                flush()
            continue

        for sentence in _split_sentences(piece):
            # 单句就超硬上限：只能在这里切（极端情况，且此时无受保护结构）
            while len(sentence) > max_chars:
                if buffer:
                    flush()
                segments.append(sentence[:max_chars].strip())
                sentence = sentence[max_chars:]
            if len(buffer) + len(sentence) > max_chars:
                flush()
            buffer += sentence
            # 够长了就收——这是"按意思分段"与"按字数切"的分界：
            # 只在句子边界收，不在句子中间收
            if len(buffer) >= target_chars:
                flush()

    flush()
    return [s for s in segments if s]


# ---------------------------------------------------------------- 投递分段

# 一次回复最多拆成几段。
# 私聊 3 段：再多就不像"补充一句"，像刷屏。
# 群聊 1 段：**不拆**。拆成多条会绕过防刷屏上限（`group_turn_budget`
# 数的是消息条数），所以群聊默认单条。
# 私聊上限 5 段：真人「连发」常常三四条，3 段会把第四条并进第三条，
# 反而更不像在打字。群聊仍然恒 1 段（拆多条会绕过防刷屏上限）。
MAX_PARTS_PRIVATE = 5
MAX_PARTS_GROUP = 1

# 两段之间的停顿（秒）。**这是模拟的节奏，不是真实回执**，
# 所以它绝不写进 delivery 回执，只影响调度。
# 1.2 秒是试运行初值，未经真实会话验证。
PART_DELAY_SECONDS = 1.2


def _newline_units(text: str) -> list[str]:
    """按换行拆成"消息单元"，**但代码块/引号内部绝不拆**。

    换行是模型决定"分几条发"的信号，所以优先尊重。但代码块本身是多行的，
    按换行一刀切会把可执行内容切碎——那是本模块开头三条硬约束的第一条。
    所以只拆受保护片段**之外**的换行。
    """
    units: list[str] = []
    for piece, protected in _split_protected(text):
        if protected:
            units.append(piece)
            continue
        for line in piece.split("\n"):
            if line.strip():
                units.append(line)
    return units


def plan_parts(text: str, *, is_group: bool) -> list[str]:
    """把一条完整回复规划成若干待发段。

    规则：
    - 群聊恒为 1 段（不拆，避免绕过防刷屏上限）
    - 私聊最多 ``MAX_PARTS_PRIVATE`` 段
    - **各段按序拼接必须等于原文本**——这是可机械检查的硬约束，
      防止分段过程增删内容
    - 切不出安全边界就发一条
    """
    if not text.strip():
        return []
    if is_group:
        return [text]

    # **换行是模型自己决定的分段意图，优先尊重它。**
    #
    # `split_semantic` 有一条"短句合并"规则（按 90 字目标累积），
    # 那是给散文段落设计的。用在聊天上会把「你今天累啦？/ 谁让你又
    # 闷着不出去转转。/ 走，去小吃街加个餐」三句合并成一段——而模型的
    # 换行恰恰是它决定"分三条发"的信号。合并规则不该覆盖它。
    units = _newline_units(text)
    if len(units) > 1:
        segments: list[str] = []
        for unit in units:
            # 单行过长时再走语义切分（受保护片段整块进，不拆）
            segments.extend(split_semantic(unit) if len(unit) > MAX_SEGMENT_CHARS else [unit])
    else:
        segments = split_semantic(text)
    if len(segments) <= 1:
        return [text]
    parts = segments[:MAX_PARTS_PRIVATE]
    # 超出的部分**并回最后一段**，不能丢——丢字比"一次说完"严重得多
    if len(segments) > MAX_PARTS_PRIVATE:
        parts[-1] = "".join(segments[MAX_PARTS_PRIVATE - 1 :])

    # 可机械验证的约束：**内容不能丢**。
    #
    # 比较时忽略空白差异——分段会重新编排换行与首尾空格（那是分段本身的
    # 一部分），但一个字都不许增删。原来按严格字符串比较，于是"按换行分段"
    # 永远通不过校验、静默退回单条，等于这个功能没生效。
    def _squash(value: str) -> str:
        return re.sub(r"\s+", "", value)

    if _squash("".join(parts)) != _squash(text):
        return [text]
    return parts


__all__ = [
    "DEFAULT_TARGET_CHARS",
    "MAX_PARTS_GROUP",
    "MAX_PARTS_PRIVATE",
    "MAX_SEGMENT_CHARS",
    "PART_DELAY_SECONDS",
    "plan_parts",
    "split_semantic",
]