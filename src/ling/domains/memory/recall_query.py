"""对话上下文的自动召回准入。"""

import re
from unicodedata import normalize

_GREETINGS = frozenset({
    "你好", "您好", "嗨", "哈喽", "早", "早呀", "早安", "早上好", "上午好",
    "中午好", "下午好", "晚上好", "晚安",
    "hi", "hey", "hello", "good morning", "good afternoon", "good evening", "good night",
})

_STANDALONE_REPLIES = frozenset({
    "",
    "嗯", "嗯嗯", "哦", "好", "好的", "对", "对啊", "是啊", "没错", "就这个", "就这样", "谢谢",
    "在吗", "你在吗", "在不在", "你在不在",
    "ok", "okay", "thanks", "thank you", "are you there",
})

_TOPIC_FOLLOWUP = re.compile(
    r"后来呢|然后呢|之后呢|刚结束了?|哪家(?:公司)?|哪一次|哪一天|几号|几点|"
    r"原话(?:是什么)?|具体(?:是什么|怎么说的)"
)
_TOPIC_CHANGE = re.compile(
    r"^(?:我们)?(?:换个话题吧?|换一个话题吧?|先不聊这个了|"
    r"(?:算了[，,、 ]*)?不说这个了)(?:$|[\s,，。.!?？！：:])"
)
_EARLIER_CONVERSATION = re.compile(
    r"(?:还记得|记不记得|接着[聊说]).{0,12}(?:昨天|前天|上次|那天|上[周回]|以前|之前)|"
    r"(?:还记得|记不记得).{0,24}(?:聊过|说过|告诉过)|"
    r"(?:昨天|前天|上次|那天|上[周回]|以前|之前)(?:我们|你|我)?"
    r"(?:聊|说|提|谈|讨论|那个)|(?:最早|一开始)(?:我们)?(?:聊|说|谈)"
)


def _plain(text: str) -> str:
    return normalize("NFKC", text).strip().casefold().rstrip(" \t\r\n.!?。~")


def opens_new_topic(text: str) -> bool:
    plain = _plain(text)
    return plain in _GREETINGS or _TOPIC_CHANGE.match(plain) is not None


def asks_for_earlier_conversation(text: str) -> bool:
    return _EARLIER_CONVERSATION.search(_plain(text)) is not None


def should_skip_automatic_recall(text: str) -> bool:
    """按整条消息匹配寒暄、简短应答和在线确认，保留带话题的短问句。"""
    plain = _plain(text)
    return plain in _STANDALONE_REPLIES or plain in _GREETINGS


def needs_recent_topic(text: str) -> bool:
    plain = normalize("NFKC", text).strip().rstrip(".!?。~")
    return _TOPIC_FOLLOWUP.fullmatch(plain) is not None
