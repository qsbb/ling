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
# 单段**切分阈值**：超过它才值得切开。这是审美/节奏参数，不是渠道约束。
MAX_SEGMENT_CHARS = 220

# 渠道单条文本的**硬上限**兜底值。
#
# 必须和 `MAX_SEGMENT_CHARS` 分开看——两者曾经被混为一谈，代价是
# "按 220 切出来的最后一段"被无上限地并成一条上千字的消息，渠道整条拒收：
#
# - `MAX_SEGMENT_CHARS`（220）：**切分阈值**。超过它才值得切，切多切少只影响
#   读起来像不像人说话。
# - `DEFAULT_PART_LIMIT_CHARS` / 调用方传入的 `max_part_chars`：**渠道硬上限**
#   （`CapabilityDescriptor.max_text_length`）。超过它平台直接拒收，是物理约束。
#
# 能拿到渠道能力时，调用方应显式传 `max_part_chars=capabilities().max_text_length`。
# 拿不到时（Web 展示、纯规划）用这个保守值：取已知渠道里的**最小值**
# （qqbot 1500、wechat 2048、qq 2000、onebot 4000 都比它大），
# 这样"不知道渠道"也不会规划出会被拒收的段。
DEFAULT_PART_LIMIT_CHARS = 1500

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


# 切分点优先选在这些字符**之后**（标点留在前一段里）
_CUT_BOUNDARY = frozenset("。！？!?；;…\n")


def _best_cut_index(text: str, limit: int) -> int:
    """在 ``text[:limit]`` 内挑一个尽量像样的断点，返回切分下标。

    优先句末标点/换行；找不到就硬切。**不插入任何字符**，
    所以"各段拼回去等于原文"仍然成立。
    """
    floor = max(1, limit // 2)
    for index in range(limit, floor, -1):
        if text[index - 1] in _CUT_BOUNDARY:
            return index
    return limit


def _cut_oversize(unit: str, limit: int) -> list[str]:
    """把单个**超过渠道硬上限**的单元切成若干 <= ``limit`` 的片。

    什么时候会走到这里：单个语义段本身就比渠道上限还长（极端的长句，
    或者比上限还大的代码块）。此时"不切开"已经不是可选项——不切就整条
    发不出去，用户什么都看不到。**物理上限优先于排版偏好**：
    切点仍尽量落在标点/换行上，且绝不增删字符。
    """
    pieces: list[str] = []
    rest = unit
    while len(rest) > limit:
        cut = _best_cut_index(rest, limit)
        pieces.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        pieces.append(rest)
    return pieces


def _pack_units(pieces: list[str], *, limit: int, max_parts: int) -> list[str]:
    """把单元装箱成若干段：**每段 <= limit，段数不超过防刷屏上限**。

    为什么不是原来的"前 N 段 + 其余全并进最后一段"：
    并进去的那段长度无上限。一条上千字的尾巴会被渠道整条拒收，
    而前面几段已经发出去了——"部分送达 + 尾段永久丢失"，
    本地还记成 failed。**丢字比"一次说完"严重得多。**

    段数的取法（这是"防刷屏"与"发得出去"两条约束的平衡点）：

    - 全部内容装得进一条消息（``total <= limit``）→ 按**自然节奏**切，
      段数不超过 ``max_parts``。这些单元是 `split_semantic` 按"一口气说一件事"
      切出来的，合并它们会把"连发几条"的人味抹掉。
    - 装不进一条（``total > limit``）→ 用**物理上的最低段数**
      ``ceil(total/limit)``。理由有两条：超长回复本来就满屏，多切只会更刷屏；
      而且不少平台对"一条入站消息能回几条"另有配额（qqbot 单聊只有 4 条），
      段数越少越不容易在发送中途撞上"被动回复次数用尽"。
      这也是**防刷屏意图**的落地：段数由内容长度决定，不由算法放大。

    每个单元进来前已保证 <= ``limit``，加上"装到超过 limit 就收段"
    这条硬规则，正常情况下每段必然 <= limit（末尾还有一次自检兜底）。
    """
    if not pieces:
        return []
    total = sum(len(piece) for piece in pieces)
    # 下界：内容装得进上限时，段数不超过 max_parts（自然节奏）；
    # 装不进时至少要 ceil(total/limit) 段，否则合并出来的段会超渠道上限。
    target = min(len(pieces), max_parts) if total <= limit else -(-total // limit)
    target = max(1, min(target, len(pieces)))

    parts: list[str] = []
    start = 0
    count = len(pieces)
    remaining_total, remaining_parts = total, target
    while start < count:
        if remaining_parts <= 1:
            parts.append("".join(pieces[start:]))
            break
        # 本段目标长度：均衡值 / 给后续段留够空间 / 不超过硬上限
        goal = max(
            remaining_total / remaining_parts,
            remaining_total - (remaining_parts - 1) * limit,
        )
        goal = max(1.0, min(goal, float(limit)))
        end = start
        taken = 0
        while end < count:
            nxt = len(pieces[end])
            if taken > 0 and (taken + nxt > limit or taken >= goal):
                break
            taken += nxt
            end += 1
        if end == start:
            # 单个单元就超过 goal（但它自身 <= limit）：单独成段
            taken = len(pieces[start])
            end = start + 1
        parts.append("".join(pieces[start:end]))
        remaining_total -= taken
        remaining_parts -= 1
        start = end

    # 硬约束的自检：任何一段都不许超限。超了说明上面的逻辑被改坏了，
    # 与其把超限消息交给渠道去整条拒收，不如在这里就切成合规的片。
    if any(len(part) > limit for part in parts):
        return _cut_oversize("".join(pieces), limit)
    return parts


def plan_parts(
    text: str,
    *,
    is_group: bool,
    max_part_chars: int | None = None,
) -> list[str]:
    """把一条完整回复规划成若干待发段。

    规则：
    - 群聊恒为 1 段（不拆，避免绕过防刷屏上限）
    - 私聊**正常长度**最多 ``MAX_PARTS_PRIVATE`` 段
    - **每段 <= 渠道硬上限 ``max_part_chars``**——超过就发不出去，
      不是"整条被拒"就是"尾段丢失"，两种都不能接受
    - **各段按序拼接必须等于原文本**——这是可机械检查的硬约束，
      防止分段过程增删内容

    ``max_part_chars`` 是**渠道硬上限**（``CapabilityDescriptor.max_text_length``），
    与切分阈值 ``MAX_SEGMENT_CHARS``（220，只管"切不切"）是两个概念。
    能拿到渠道能力时调用方必须显式传（`bridge._deliver` 就这么做）；
    拿不到时用 ``DEFAULT_PART_LIMIT_CHARS`` 这个保守兜底——
    "不知道渠道"不等于"渠道没有上限"。

    两个上限冲突时**硬上限优先**：当文本长到"``MAX_PARTS_PRIVATE`` 段装不下"
    时，段数会突破防刷屏上限——那种长度下多段是"发得出去"的前提。
    防刷屏意图保留在**正常长度**上：短回复仍然按自然节奏切 1~5 段。
    """
    if not text.strip():
        return []
    limit = DEFAULT_PART_LIMIT_CHARS if max_part_chars is None else max(1, int(max_part_chars))

    if is_group:
        # 群聊不拆：拆成多条会绕过 `group_turn_budget`（它数的是消息条数）。
        # 唯一的例外是**物理上一条发不出去**：那时不拆等于整条丢失，
        # 拆开至少话能说到。这种文本远超正常群聊回复长度，实际很少见。
        return [text] if len(text) <= limit else _cut_oversize(text, limit)

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
        return [text] if len(text) <= limit else _cut_oversize(text, limit)

    # 先保证每个单元自身不超限（单个语义段/代码块可能比渠道上限还长）
    pieces: list[str] = []
    for segment in segments:
        pieces.extend(_cut_oversize(segment, limit) if len(segment) > limit else [segment])

    parts = _pack_units(pieces, limit=limit, max_parts=MAX_PARTS_PRIVATE)

    # 可机械验证的约束：**内容不能丢**。
    #
    # 比较时忽略空白差异——分段会重新编排换行与首尾空格（那是分段本身的
    # 一部分），但一个字都不许增删。原来按严格字符串比较，于是"按换行分段"
    # 永远通不过校验、静默退回单条，等于这个功能没生效。
    #
    # 校验失败时**退回硬切而不是退回单条**：退回单条会在超限时把
    # "分段失败"变成"整条发不出去"，比分段难看严重得多。
    def _squash(value: str) -> str:
        return re.sub(r"\s+", "", value)

    if _squash("".join(parts)) != _squash(text):
        return _cut_oversize(text, limit)
    return parts


__all__ = [
    "DEFAULT_PART_LIMIT_CHARS",
    "DEFAULT_TARGET_CHARS",
    "MAX_PARTS_GROUP",
    "MAX_PARTS_PRIVATE",
    "MAX_SEGMENT_CHARS",
    "PART_DELAY_SECONDS",
    "plan_parts",
    "split_semantic",
]