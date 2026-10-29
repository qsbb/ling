"""OneBot V11 消息解析与构造。

OneBot 的 ``message`` 字段有两种等价形态：
- **字符串格式**：CQ 码，如 ``你好[CQ:at,qq=123]``
- **数组格式**：``[{"type":"text","data":{"text":"你好"}}, {"type":"at",...}]``

适配器必须两种都收（不同实现的默认配置不同），发送时统一用数组格式
（避免用户输入的文本里恰好含 ``[CQ:`` 被误解析成指令）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ling.contracts.v1 import InputPart, Modality

# CQ 码： escaped 字符按 OneBot 规范处理（&#91; &#93; &#44; &amp;）
_CQ_RE = re.compile(r"\[CQ:([a-zA-Z0-9_.-]+)((?:,[^\]]*)?)\]")
_CQ_UNESCAPE = (("&#91;", "["), ("&#93;", "]"), ("&#44;", ","), ("&amp;", "&"))


@dataclass
class ParsedMessage:
    """解析结果：给模型看的文本 + 结构化元信息。"""

    text: str = ""
    parts: list[InputPart] = field(default_factory=list)
    media_refs: list[str] = field(default_factory=list)
    # 是否 @ 了机器人自己（群聊接话判断的关键线索）
    mentions_self: bool = False
    # 引用的消息 id（QQ 的"回复"），用于在群里判断"是不是在跟我说话"
    reply_to_message_id: str | None = None

    def modality(self) -> Modality:
        if self.media_refs and not self.text.strip():
            return Modality.text  # 纯媒体也走 text 车道，媒体走 media_refs
        return Modality.text


def _cq_unescape(value: str) -> str:
    for entity, char in _CQ_UNESCAPE:
        value = value.replace(entity, char)
    return value


def _parse_cq_params(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in raw.lstrip(",").split(","):
        if not chunk:
            continue
        key, _, val = chunk.partition("=")
        out[key.strip()] = _cq_unescape(val)
    return out


def parse_message(message: object, *, self_id: str | None = None) -> ParsedMessage:
    """解析 OneBot 的 message 字段（字符串或数组）。"""
    if isinstance(message, str):
        return _parse_string(message, self_id=self_id)
    if isinstance(message, list):
        return _parse_array(message, self_id=self_id)
    return ParsedMessage()


def _parse_string(message: str, *, self_id: str | None) -> ParsedMessage:
    result = ParsedMessage()
    cursor = 0
    for match in _CQ_RE.finditer(message):
        if match.start() > cursor:
            _append_text(result, _cq_unescape(message[cursor : match.start()]))
        _handle_segment(result, match.group(1), _parse_cq_params(match.group(2)), self_id)
        cursor = match.end()
    if cursor < len(message):
        _append_text(result, _cq_unescape(message[cursor:]))
    return result


def _parse_array(segments: list[object], *, self_id: str | None) -> ParsedMessage:
    result = ParsedMessage()
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        seg_type = str(seg.get("type") or "")
        data = seg.get("data")
        _handle_segment(result, seg_type, data if isinstance(data, dict) else {}, self_id)
    return result


def _handle_segment(
    result: ParsedMessage, seg_type: str, data: dict, self_id: str | None
) -> None:
    if seg_type == "text":
        _append_text(result, str(data.get("text") or ""))
    elif seg_type == "at":
        qq = str(data.get("qq") or "")
        if self_id and qq == str(self_id):
            result.mentions_self = True
            # @机器人 本身不该作为正文喂给模型（"@ling 你好" → "你好"）
            return
        if qq and qq != "all":
            _append_text(result, f"@{qq}")
    elif seg_type == "reply":
        reply_id = data.get("id")
        if reply_id is not None:
            result.reply_to_message_id = str(reply_id)
    elif seg_type in {"image", "voice", "video", "file"}:
        ref = data.get("url") or data.get("file")
        if ref:
            result.media_refs.append(str(ref))
            _append_text(result, f"[{seg_type}]")
    elif seg_type == "face":
        _append_text(result, "[表情]")
    # record / mface / dice / rps / shake / share / location 等
    # 暂不进入正文：它们没有可读文本，硬塞会让模型困惑。


def _append_text(result: ParsedMessage, text: str) -> None:
    if not text:
        return
    result.text += text


def build_text_segments(text: str) -> list[dict]:
    """构造发送用的消息段。

    统一用数组格式 + 单 text 段：即使用户文本里含 ``[CQ:`` 也不会被
    当成 CQ 码解析（字符串格式才会解析）。
    """
    return [{"type": "text", "data": {"text": text}}]


def build_media_segments(text: str, media_refs: list[str] | None = None) -> list[dict]:
    """构造带媒体的消息段：文本 + 若干 image 段。

    为什么必须改这里而不是只发文本：``OutboundMessage.media_refs`` 早就存在，
    但 ``send`` 一直只发 ``build_text_segments(text)``——媒体被**静默丢弃**，
    而投递仍然拿到回执、被记成"已送达"。这是本项目最不能容忍的那类错误：
    没有真实结果却留下成功记录。

    文本为空时只发媒体段，不塞空 text 段（部分实现会因此报错）。
    """
    segments: list[dict] = []
    if text:
        segments.append({"type": "text", "data": {"text": text}})
    for ref in media_refs or []:
        if not ref:
            continue
        segments.append({"type": "image", "data": {"file": ref}})
    if not segments:
        segments.append({"type": "text", "data": {"text": ""}})
    return segments


def window_ref_for(*, is_group: bool, target_id: str, instance_id: str) -> str:
    """渠道窗口标识。

    群聊窗口只由「群号」决定，**不含发言人**——同一个群必须是一个共享会话，
    否则每个成员各看到一个窗口，角色就永远看不到完整群聊语境
    （这是能否"看气氛、决定该不该接话"的前提）。
    """
    kind = "group" if is_group else "private"
    return f"{instance_id}:{kind}:{target_id}"


__all__ = [
    "ParsedMessage",
    "build_media_segments",
    "build_text_segments",
    "parse_message",
    "window_ref_for",
]


def build_outbound_segments(
    text: str,
    media_refs: list[str] | None = None,
    mention_ids: list[str] | None = None,
    reply_to: str | None = None,
) -> list[dict]:
    """构造外发消息段：@ + 文本 + 媒体。

    ## 为什么要 @ 段

    群里同时有好几个人说话时，"回你"这三个字谁也听不懂是对谁说的。
    真人会用 @ 消歧——这不是礼貌，是**可理解性**。

    ## 为什么用数组格式而不是拼 CQ 码

    数组格式里 `at` 是独立段，`text` 段的内容不会被当成 CQ 码解析。
    拼字符串的话，用户文本里恰好出现 `[CQ:at,qq=...]` 就会被平台执行——
    那是一条注入路径。

    ## 顺序

    @ 在前、文本在中、媒体在后。人也是这样发的：先点名，再说事。
    """
    segments: list[dict] = []
    # 引用段必须在最前：平台的 reply 语义是"这条消息引用了 X"
    if reply_to:
        segments.append({"type": "reply", "data": {"id": str(reply_to)}})
    for account in mention_ids or []:
        target = (account or "").strip()
        if not target:
            continue
        segments.append({"type": "at", "data": {"qq": target}})
    if segments:
        # @ 之后垫一个空格，否则在客户端里会跟正文黏在一起
        segments.append({"type": "text", "data": {"text": " "}})
    segments.extend(build_media_segments(text, media_refs))
    return segments
