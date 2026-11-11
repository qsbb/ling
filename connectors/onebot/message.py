"""OneBot V11 消息解析与构造。

OneBot 的 ``message`` 字段有两种等价形态：
- **字符串格式**：CQ 码，如 ``你好[CQ:at,qq=123]``
- **数组格式**：``[{"type":"text","data":{"text":"你好"}}, {"type":"at",...}]``

适配器必须两种都收（不同实现的默认配置不同），发送时统一用数组格式
（避免用户输入的文本里恰好含 ``[CQ:`` 被误解析成指令）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from ling.contracts.v1 import InputPart, Modality, media_ref_is_valid

logger = logging.getLogger("ling.connectors.onebot.message")

# CQ 码： escaped 字符按 OneBot 规范处理（&#91; &#93; &#44; &amp;）
_CQ_RE = re.compile(r"\[CQ:([a-zA-Z0-9_.-]+)((?:,[^\]]*)?)\]")
_CQ_UNESCAPE = (("&#91;", "["), ("&#93;", "]"), ("&#44;", ","), ("&amp;", "&"))

# 带媒体的消息段 → 正文占位。
#
# ## 为什么占位是英文标记，而不是 `[语音]`
#
# `kernel/bridge.py` 的 `_MEDIA_MARKERS`（`\[(?:image|voice|video|file)\]`）
# 用这批英文标记剥掉占位、判断"这条消息里有没有真话"：只发一张图/一条语音时
# 正文只剩占位，剥不掉就会被当成用户发言去回复（那是真实缺陷，注释在
# bridge.py:512）。占位换成 `[语音]` 会让那条判断失效。
#
# 所以：**机器读的部分保持英文段名**（`[voice]`），中文的"这是一条语音"
# 由 `media_segment_label()` 提供给需要人话的调用方与模型提示文本。
# `record` 是 OneBot v11 规范里的语音段名（NapCat 发的就是它），`voice`
# 只是老 CQ 码时代的别名——两者同一条分支、同一套 `media_refs` 规则。
_MEDIA_SEGMENTS = ("image", "voice", "record", "video", "file")

# 段名 → 中文人话（给模型提示 / 诊断日志用）。
_MEDIA_LABELS = {
    "image": "图片",
    "voice": "语音",
    "record": "语音",
    "video": "视频",
    "file": "文件",
    "mface": "表情包",
    "face": "表情",
}


def media_segment_label(seg_type: str) -> str:
    """段名 → 中文人话；不认识的段名原样返回。"""
    return _MEDIA_LABELS.get(seg_type, seg_type)


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
    elif seg_type in _MEDIA_SEGMENTS:
        # `record` 是 OneBot **v11 规范**里的语音段名（NapCat 发的就是它）；
        # `voice` 只是老 CQ 码时代的别名。两者必须走同一条分支——否则语音
        # 整条被静默丢掉，模型连"用户发过语音"都不知道。
        ref = str(data.get("url") or data.get("file") or "")
        if ref:
            # 坏引用只丢**这个附件**，不丢整条消息：契约层会拒绝带坏引用的
            # 信封，而正文对用户仍然有价值——"这图什么意思"不该跟着一张
            # 打不开的图片一起消失。过滤规则与 `MediaRef` 同一份。
            if media_ref_is_valid(ref):
                result.media_refs.append(ref)
            else:
                logger.warning(
                    "丢弃不合格的媒体引用（类型 %s，长度 %d）：只影响该附件",
                    seg_type,
                    len(ref),
                )
            # 占位用**规范段名**：`record` 也写成 `[voice]`，与老别名同一形态，
            # `bridge._MEDIA_MARKERS` 才能把它从"有没有真话"的判断里剥掉。
            _append_text(result, "[voice]" if seg_type == "record" else f"[{seg_type}]")
    elif seg_type == "mface":
        # 表情包：有的实现只给 `summary`、不给 `url`。无论有没有 url 都要留下
        # 提示——表情包是用户表达的一部分，不能整条消失；有 url 时顺带进
        # `media_refs`，坏引用照旧只丢该附件。
        ref = str(data.get("url") or data.get("file") or "")
        if ref:
            if media_ref_is_valid(ref):
                result.media_refs.append(ref)
            else:
                logger.warning(
                    "丢弃不合格的媒体引用（类型 %s，长度 %d）：只影响该附件",
                    seg_type,
                    len(ref),
                )
        _append_text(result, "[表情包]")
    elif seg_type == "face":
        _append_text(result, "[表情]")
    # dice / rps / shake / share / location 等**明确不处理**：没有可读文本，
    # 也没有可下载的媒体（`share` / `location` 的 url 是外部网页或坐标，
    # 不是附件），硬塞进正文只会让模型困惑。


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

    .. warning::

       **这里只发 ``image`` 段**。语音条不能走这里——它会被当成一张
       （打不开的）图片发出去，而本地回执仍然是 ``delivered``。语音走
       ``connectors/onebot/voice.py`` 的 ``voice_segment()``，由
       ``build_outbound_segments(voice_segments=...)`` 拼进同一条消息。
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
    "media_segment_label",
    "parse_message",
    "window_ref_for",
]


def build_outbound_segments(
    text: str,
    media_refs: list[str] | None = None,
    mention_ids: list[str] | None = None,
    reply_to: str | None = None,
    voice_segments: list[dict] | None = None,
) -> list[dict]:
    """构造外发消息段：@ + 文本 + 图片 + 语音。

    ## 为什么要 @ 段

    群里同时有好几个人说话时，"回你"这三个字谁也听不懂是对谁说的。
    真人会用 @ 消歧——这不是礼貌，是**可理解性**。

    ## 为什么用数组格式而不是拼 CQ 码

    数组格式里 `at` 是独立段，`text` 段的内容不会被当成 CQ 码解析。
    拼字符串的话，用户文本里恰好出现 `[CQ:at,qq=...]` 就会被平台执行——
    那是一条注入路径。

    ## 顺序

    @ 在前、文本在中、媒体在后。人也是这样发的：先点名，再说事。
    语音排在图片**之后**：QQ 客户端把语音条渲染成一条独立的播放控件，
    放最后才不会把正文挤到控件下面。

    ## ``media_refs`` 里为什么不能放语音

    ``media_refs`` 是 ``list[str]``，**没有类型信息**。从这里构造的每一段
    都是 ``image``——把语音的 file 值塞进来，用户收到的是一张打不开的图，
    而本地回执仍然是 ``delivered``。语音必须由调用方用
    ``voice.build_voice_segments()`` 显式构造成 ``record`` 段后经
    ``voice_segments`` 传进来（见 ``connectors/onebot/voice.py`` 的说明）。
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
    for segment in voice_segments or []:
        # 只接受已经构造成 record 的段：任何别的类型到这里都是上游搞错了，
        # 静默放过就会变成"图片版的语音"。
        if str(segment.get("type") or "") != "record":
            raise ValueError(f"voice_segments 只接受 record 段，收到 {segment.get('type')!r}")
        segments.append(segment)
    return segments
