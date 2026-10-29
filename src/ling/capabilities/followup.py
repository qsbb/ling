"""FollowupGuard：去掉客服式的机械追问。

**为什么这是"像人"的高性价比一项**：真人不会每句话后面都接
"还有什么可以帮您的吗"。这种尾句是模型被训练成"助手"留下的痕迹，
在陪伴场景里恰恰是最出戏的地方。

设计思路：
但**只做减法、不做加法**：我们不生成新句子，只判断"最后这句
是不是该删掉"。

边界（很重要）：
- **只删服务式追问**，不删真正关心对方的问题
  （"你面试怎么样了" 必须保留）
- 删完不能变成空回复——那比客服腔更糟
- 只处理**最后一句**，中间句子不动，避免误删正常内容
"""

from __future__ import annotations

import re

# 服务式追问的特征：以"还能/需要/要/是否"起头，且含"帮/需要/其他/别"等词，
# 或者干脆是固定的客服句式。
_SERVICE_TAIL_PATTERNS = [
    re.compile(r"^还(有|需要|要)[^？?]{0,12}(吗|么|嘛)[？?]?$"),
    re.compile(r"^还有什么(可以|能|需要)[^？?]{0,12}[？?]?$"),
    re.compile(r"^(需要|要不要)我[^？?]{0,16}[？?]?$"),
    re.compile(r"^(有|还有)(其他|别的)?(问题|需要|事)[^？?]{0,10}[？?]?$"),
    re.compile(r"^(随时|有事)(告诉我|找我|说)[^？?]{0,10}[。.!！]?$"),
    re.compile(r"^(希望|但愿)(能|可以)?帮[到上][你我][^。.!！]{0,10}[。.!！]?$"),
]

# 这些词一出现就说明是在问**对方的处境**，绝不能删
_CARE_MARKERS = (
    "你",
    "面试",
    "身体",
    "家里",
    "工作",
    "考试",
    "医生说",
    "结果",
    "怎么样",
    "累不累",
    "还好吗",
)

# 分句：句号/问号/感叹号/分号/换行。
_TAIL_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")


def _last_sentence(text: str) -> tuple[str, str]:
    """拆出（前面部分, 最后一句）。"""
    pieces = [p for p in _TAIL_SPLIT.split(text) if p]
    if not pieces:
        return "", text
    return "".join(pieces[:-1]), pieces[-1]


def is_service_tail(sentence: str) -> bool:
    """这句是不是服务式追问。"""
    cleaned = sentence.strip()
    if not cleaned:
        return False
    matched = any(p.match(cleaned) for p in _SERVICE_TAIL_PATTERNS)
    # 含"你"之类不等于客服腔（"需要我帮你查吗"也含），所以要看句式：
    # 关心对方但**不是**服务句式的，一律不删。
    if not matched and any(marker in cleaned for marker in _CARE_MARKERS):
        return False
    return matched


def strip_service_tail(text: str) -> tuple[str, bool]:
    """去掉结尾的服务式追问。返回 (处理后文本, 是否删过)。

    删完为空则保留原文——空回复比客服腔更糟。
    """
    if not text or not text.strip():
        return text, False
    head, tail = _last_sentence(text)
    if not is_service_tail(tail):
        return text, False
    result = head.rstrip()
    if not result.strip():
        # 整条回复就是一句"还有什么可以帮你的吗" —— 保留它，
        # 因为删掉会变成空消息
        return text, False
    return result, True


__all__ = ["is_service_tail", "strip_service_tail"]