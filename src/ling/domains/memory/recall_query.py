"""对话上下文的自动召回准入。"""

import re
from unicodedata import normalize

_STANDALONE_REPLIES = {
    "",
    "嗯", "嗯嗯", "哦", "好", "好的", "谢谢", "你好",
    "在吗", "你在吗", "在不在", "你在不在",
    "hi", "hello", "ok", "okay", "thanks", "thank you", "are you there",
}

_TOPIC_FOLLOWUP = re.compile(
    r"后来呢|然后呢|之后呢|刚结束了?|哪家(?:公司)?|哪一次|哪一天|几号|几点|"
    r"原话(?:是什么)?|具体(?:是什么|怎么说的)"
)


def should_skip_automatic_recall(text: str) -> bool:
    """按整条消息匹配寒暄、简短应答和在线确认，保留带话题的短问句。"""
    plain = normalize("NFKC", text).strip().casefold().rstrip(" \t\r\n.!?。~")
    return plain in _STANDALONE_REPLIES


def needs_recent_topic(text: str) -> bool:
    plain = normalize("NFKC", text).strip().rstrip(".!?。~")
    return _TOPIC_FOLLOWUP.fullmatch(plain) is not None
