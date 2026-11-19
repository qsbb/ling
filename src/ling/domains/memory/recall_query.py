"""对话上下文的自动召回准入。"""

from unicodedata import normalize

_STANDALONE_REPLIES = {
    "",
    "嗯", "嗯嗯", "哦", "好", "好的", "谢谢", "你好",
    "在吗", "你在吗", "在不在", "你在不在",
    "hi", "hello", "ok", "okay", "thanks", "thank you", "are you there",
}


def should_skip_automatic_recall(text: str) -> bool:
    """按整条消息匹配寒暄、简短应答和在线确认，保留带话题的短问句。"""
    plain = normalize("NFKC", text).strip().casefold().rstrip(" \t\r\n.!?。~")
    return plain in _STANDALONE_REPLIES
