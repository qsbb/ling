"""重复检测：把"你最近老用同一句/同一个口头禅"这个**事实**告诉她。

## 为什么这是代码该做的事

人格文档里写着「这些只是举例，每天的话题要换着来，别总提同一件」——
但模型仍然会抓着一两句最有特色的**举例**当模板反复用。

代码不该规定"怎么说"（那是人设的事），但**数她说了几遍是确定性事实**。
所以这里只负责数，然后把事实交给她：

    "你最近 4 条消息里有 3 条都带着「朋友的朋友」这个说法。
     连着用会像口头禅，换不换你看着办。"

换不换、怎么换，由她自己决定。

## 为什么是两层

只比对"整句完全相同"抓不到真实情况：实测她不会原样重复，而是换个措辞
沿用同一条口头禅——

    「朋友的朋友就是我的朋友。」
    「朋友的朋友就是朋友，别客气呀。」

两条字符串完全不同，整句比对永远不触发。所以第二层用**字符片段**
（shingle）统计跨消息重复的短语。
"""

from __future__ import annotations

import re

# 句子太短时重复是正常修辞（"好呀。"），不值得提醒
MIN_SENTENCE_CHARS = 8
# 最近看几条
LOOKBACK = 5
# 同一条尾巴出现几次才提醒
REPEAT_THRESHOLD = 3
# 短语级：连续几个字跨多条消息出现，就是口头禅
SHINGLE_CHARS = 4
SHINGLE_MIN_MESSAGES = 3


def _split_sentences(text: str) -> list[str]:
    """按中英文句末标点切句，标点留在前一段末尾。"""
    pieces = re.split(r"(?<=[。！？!?…\.])\s*", text)
    return [p for p in pieces if p]


def _tail_sentences(texts: list[str]) -> list[str]:
    """取每条消息的**最后一句**（去掉空白与换行）。"""
    out: list[str] = []
    for raw in texts:
        cleaned = (raw or "").strip()
        if not cleaned:
            continue
        parts = [p for p in _split_sentences(cleaned) if p.strip()]
        if parts:
            out.append(parts[-1].strip())
    return out


def _shingles(text: str) -> set[str]:
    """一条消息里的所有 4 字片段（去掉空白）。"""
    cleaned = "".join((text or "").split())
    if len(cleaned) < SHINGLE_CHARS:
        return set()
    return {
        cleaned[i : i + SHINGLE_CHARS]
        for i in range(len(cleaned) - SHINGLE_CHARS + 1)
    }


def _lcs(a: str, b: str) -> str:
    """最长公共子串（动态规划）。字符串都很短，不需要更复杂的算法。"""
    best = ""
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > len(best):
                    best = a[i - cur[j] : i]
        prev = cur
    return best


def _longest_shared(texts: list[str]) -> str:
    """多条消息共有的最长子串。"""
    if not texts:
        return ""
    shared = texts[0]
    for other in texts[1:]:
        shared = _lcs(shared, other)
        if len(shared) < SHINGLE_CHARS:
            return ""
    return shared


def repeated_phrase(texts: list[str]) -> str | None:
    """跨多条消息反复出现的最长短语；没有就 None。"""
    recent = [t for t in texts if (t or "").strip()][-LOOKBACK:]
    if len(recent) < SHINGLE_MIN_MESSAGES:
        return None

    counts: dict[str, int] = {}
    for text in recent:
        for shingle in _shingles(text):
            counts[shingle] = counts.get(shingle, 0) + 1

    hits = [sh for sh, n in counts.items() if n >= SHINGLE_MIN_MESSAGES]
    if not hits:
        return None

    # 命中一个 4 字片段后，把**包含它的那些消息**拿出来求最长公共子串，
    # 报出来的才是完整那句口头禅（"朋友就是" → "朋友的朋友就是我的朋友"），
    # 而不是一个碎片。
    best = ""
    for shingle in sorted(hits, key=len, reverse=True):
        carriers = [t for t in recent if shingle in "".join(t.split())]
        shared = _longest_shared(["".join(t.split()) for t in carriers])
        if len(shared) > len(best):
            best = shared
    return best if len(best) >= SHINGLE_CHARS else None


def _exact_tail_hint(recent: list[str]) -> str | None:
    """措辞完全一致时的提醒。"""
    tails = _tail_sentences(recent)
    counts: dict[str, int] = {}
    for tail in tails:
        if len(tail) < MIN_SENTENCE_CHARS:
            continue
        counts[tail] = counts.get(tail, 0) + 1
    if not counts:
        return None
    tail, count = max(counts.items(), key=lambda kv: kv[1])
    if count < REPEAT_THRESHOLD:
        return None
    return (
        f"你最近 {len(tails)} 条消息里有 {count} 条用「{tail}」收尾。"
        "同一个说法连着用会显得像口头禅，换不换你看着办。"
    )


def repetition_hint(texts: list[str]) -> str | None:
    """她最近反复用同一句或有同一口头禅时，返回一句**事实**描述；否则 None。

    `texts` 按时间顺序（旧→新）。只看最后 `LOOKBACK` 条。
    """
    recent = [t for t in texts if (t or "").strip()][-LOOKBACK:]
    if len(recent) < REPEAT_THRESHOLD:
        return None

    exact = _exact_tail_hint(recent)
    if exact:
        return exact

    phrase = repeated_phrase(recent)
    if phrase:
        hits = sum(1 for t in recent if phrase in "".join(t.split()))
        return (
            f"你最近 {len(recent)} 条消息里有 {hits} 条都带着「{phrase}」这个说法。"
            "连着用会像口头禅，换不换你看着办。"
        )
    return None


__all__ = [
    "LOOKBACK",
    "MIN_SENTENCE_CHARS",
    "REPEAT_THRESHOLD",
    "SHINGLE_CHARS",
    "SHINGLE_MIN_MESSAGES",
    "repeated_phrase",
    "repetition_hint",
]
