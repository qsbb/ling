"""重复检测：把"你最近老用同一句"这个**事实**告诉她。

## 为什么这是代码该做的事

人设里写着「这些只是举例，每天的话题要换着来，别总提同一件」——
但模型仍然会抓着一两句最有特色的举例当模板反复用（实测：无论问什么，
回复都以「朋友的朋友就是我的朋友」「街角新摊子香味飘过来」收尾）。

代码不该规定"怎么说"（那是人设的事），但**数她说了几遍是确定性事实**。
所以这里只负责数，然后把事实交给她：

    "你最近 5 条消息里有 3 条用「…」收尾，对方可能觉得重复。"

换不换、怎么换，由她自己决定。
"""

from __future__ import annotations

# 句子太短时重复是正常修辞（"好呀。"），不值得提醒
MIN_SENTENCE_CHARS = 8
# 最近看几条
LOOKBACK = 5
# 同一条尾巴出现几次才提醒
REPEAT_THRESHOLD = 3


def _tail_sentences(texts: list[str]) -> list[str]:
    """取每条消息的**最后一句**（去掉空白与换行）。"""
    out: list[str] = []
    for raw in texts:
        cleaned = (raw or "").strip()
        if not cleaned:
            continue
        # 按中英文句末标点切，取最后一个非空片段
        parts = [p for p in _split_sentences(cleaned) if p.strip()]
        if parts:
            out.append(parts[-1].strip())
    return out


def _split_sentences(text: str) -> list[str]:
    import re

    # 保留标点在前一段的末尾
    pieces = re.split(r"(?<=[。！？!?…\.])\s*", text)
    return [p for p in pieces if p]


def repetition_hint(texts: list[str]) -> str | None:
    """如果她最近反复用同一句收尾，返回一句**事实**描述；否则 None。

    `texts` 按时间顺序（旧→新）。只看最后 `LOOKBACK` 条。
    """
    recent = [t for t in texts if (t or "").strip()][-LOOKBACK:]
    if len(recent) < REPEAT_THRESHOLD:
        return None

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


__all__ = ["LOOKBACK", "MIN_SENTENCE_CHARS", "REPEAT_THRESHOLD", "repetition_hint"]
