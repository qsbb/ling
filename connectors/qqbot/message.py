"""QQ 官方机器人的消息解析与构造。

与 OneBot 的 ``message.py`` 完全不同，不要照抄：

- 入站事件是 **JSON 事件体**（``{"op":0,"t":"C2C_MESSAGE_CREATE","d":{...}}``），
  没有 CQ 码、没有消息段数组；正文在 ``d.content``，附件在 ``d.attachments``。
- 出站消息是 **``msg_id`` + ``msg_seq`` 的被动回复**：一条用户消息只允许
  有限次数的回复，因此「回复哪条」不是可选的装饰，而是必填的协议字段。
- 身份是 **openid**（``author.member_openid`` / ``author.user_openid``），
  **不是 QQ 号**。同一个真人在不同机器人下 openid 不同，也拿不到 QQ 号。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ling.contracts.v1 import Modality
from ling.ports.channel import ChannelError, ChannelErrorKind

# 事件类型（官方 ``t`` 字段）。只列本连接器真的会处理/明确拒绝的。
EVENT_C2C_MESSAGE_CREATE = "C2C_MESSAGE_CREATE"
EVENT_GROUP_AT_MESSAGE_CREATE = "GROUP_AT_MESSAGE_CREATE"
# 频道（guild）产品线的事件。语义与 QQ 群/私聊完全不同（频道消息走
# /channels/{channel_id}/messages，还有子频道权限），这里**明确不支持**：
# 与其猜一套窗口语义，不如如实拒绝并留下原因。
EVENT_AT_MESSAGE_CREATE = "AT_MESSAGE_CREATE"
EVENT_DIRECT_MESSAGE_CREATE = "DIRECT_MESSAGE_CREATE"

SUPPORTED_EVENTS = frozenset({EVENT_C2C_MESSAGE_CREATE, EVENT_GROUP_AT_MESSAGE_CREATE})
CHANNEL_EVENTS = frozenset({EVENT_AT_MESSAGE_CREATE, EVENT_DIRECT_MESSAGE_CREATE})

# 正文里的 @ 标记（``<@!1234>`` / ``<@1234>`` / ``<@everyone>``）。
# 官方在群聊 @ 机器人的消息里会带这个前缀，且它们不是用户说的话。
_MENTION_RE = re.compile(r"<@!?[^>\s]+>")

_ATTACHMENT_KINDS = {
    "image": "image",
    "voice": "voice",
    "video": "video",
    "file": "file",
}


@dataclass(frozen=True)
class ParsedInbound:
    """一条入站事件的翻译结果（仍是渠道侧数据，不含身份解析）。"""

    event_type: str
    # 平台消息 id。既做去重键，也是被动回复要回带的 ``msg_id``。
    message_id: str
    # 发送者 openid（**不是 QQ 号**）
    account_ref: str
    display_name: str | None = None
    is_group: bool = False
    group_openid: str | None = None
    text: str = ""
    media_refs: list[str] = field(default_factory=list)
    # 是否 @ 了机器人。群聊事件类型本身就意味着"被 @"，私聊恒为对话。
    mentions_actor: bool = False
    occurred_at: datetime | None = None

    def modality(self) -> Modality:
        # 纯媒体也走 text 车道（媒体另走 media_refs），与 OneBot 侧保持一致：
        # 决策层只认"有没有可读文本"，媒体是不是图不改变车道。
        return Modality.text


def parse_inbound_event(payload: dict) -> ParsedInbound:
    """把事件体翻译成 ``ParsedInbound``。

    只做协议翻译：缺字段就报错（``rejected``），不认识的事件类型报
    ``unsupported``。**不猜**：拿不到 openid 就拒绝，不拿 ``id`` 冒充。
    """
    op = payload.get("op", 0)
    try:
        op_value = int(op)  # 平台会偶尔给字符串
    except (TypeError, ValueError) as exc:
        raise ChannelError(
            ChannelErrorKind.rejected, f"事件 op 非法: {op!r}"
        ) from exc
    if op_value != 0:
        # op=10/11 心跳、op=13 回调地址校验等都不进对话链路。
        # op=13 由路由层处理（需要回签名，不是一条消息）。
        raise ChannelError(ChannelErrorKind.unsupported, f"暂不处理 op={op_value} 的事件")

    event_type = str(payload.get("t") or "")
    data = payload.get("d")
    if not isinstance(data, dict):
        raise ChannelError(ChannelErrorKind.rejected, "事件缺少 d 对象")

    if event_type in CHANNEL_EVENTS:
        raise ChannelError(
            ChannelErrorKind.unsupported,
            f"频道(guild)消息暂未接入: {event_type}（窗口与发送语义不同，未核实）",
        )
    if event_type not in SUPPORTED_EVENTS:
        raise ChannelError(
            ChannelErrorKind.unsupported, f"暂不处理该事件类型: {event_type or '(空)'}"
        )

    message_id = str(data.get("id") or "")
    if not message_id:
        # 没有消息 id 既没法去重、也没法被动回复 → 不能受理
        raise ChannelError(ChannelErrorKind.rejected, "事件缺少消息 id")

    author_raw = data.get("author")
    author: dict = author_raw if isinstance(author_raw, dict) else {}
    is_group = event_type == EVENT_GROUP_AT_MESSAGE_CREATE
    group_openid = str(data.get("group_openid") or "") or None

    if is_group:
        account_ref = str(
            author.get("member_openid") or author.get("union_openid") or ""
        )
        if not group_openid:
            raise ChannelError(ChannelErrorKind.rejected, "群消息缺少 group_openid")
        if not account_ref:
            # 群事件的成员身份是 member_openid；拿不到就不受理，
            # **不要**退回去用 author.id（那是频道产品线的用户 id，语义不同）。
            raise ChannelError(ChannelErrorKind.rejected, "群消息缺少 member_openid")
        # GROUP_AT_MESSAGE_CREATE 语义上就是"@了机器人"，这是渠道能直接
        # 判断的"冲我说的"，必须如实上报，否则群里永远不会接话。
        mentions_actor = True
    else:
        account_ref = str(author.get("user_openid") or author.get("union_openid") or "")
        if not account_ref:
            raise ChannelError(ChannelErrorKind.rejected, "私聊消息缺少 user_openid")
        mentions_actor = False

    raw_content = data.get("content")
    text, mentioned_in_text = strip_mentions(raw_content if isinstance(raw_content, str) else "")
    media_refs = _media_refs(data.get("attachments"))

    return ParsedInbound(
        event_type=event_type,
        message_id=message_id,
        account_ref=account_ref,
        display_name=_display_name(author),
        is_group=is_group,
        group_openid=group_openid,
        text=text,
        media_refs=media_refs,
        mentions_actor=mentions_actor or mentioned_in_text,
        occurred_at=_parse_ts(data.get("timestamp")),
    )


def strip_mentions(content: str) -> tuple[str, bool]:
    """去掉 ``<@!xxx>`` 标记，返回 (正文, 是否出现过 @ 标记)。

    @ 本身不该作为正文喂给模型（"@ling 你好" → "你好"），
    但"出现过 @"是有用信号，所以单独返回而不是丢掉。
    """
    if not content:
        return "", False
    mentioned = bool(_MENTION_RE.search(content))
    cleaned = _MENTION_RE.sub("", content)
    return cleaned.strip(), mentioned


def _display_name(author: dict) -> str | None:
    """取显示名。拿不到就 None——**不编**。

    这一项只用于称呼；身份永远走 openid（昵称可以随便改、也可以重名）。
    """
    for key in ("username", "nickname", "nick"):
        value = author.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:128]
    return None


def _media_refs(attachments: object) -> list[str]:
    """从 attachments 里取可下载的 url。

    只收 https：官方附件地址是 https，且 url 有时效；http 或本地路径
    一律不收，避免把任意字符串变成 SSRF 跳板。
    """
    if not isinstance(attachments, list):
        return []
    refs: list[str] = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if isinstance(url, str) and url.startswith("https://"):
            refs.append(url)
    return refs


def media_marker(content_type: str) -> str:
    """附件类型 → 正文占位标记（``[image]`` / ``[voice]`` …）。

    与 OneBot 侧约定一致：bridge 用同一组标记判断"有没有真话"。
    """
    head = (content_type or "").split("/", 1)[0].lower()
    return f"[{_ATTACHMENT_KINDS.get(head, 'file')}]"


def _parse_ts(value: object) -> datetime | None:
    """平台 ``timestamp`` 是 ISO8601 字符串；解析不了就留空，不要编时间。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ------------------------------------------------------------------ 窗口


def window_ref_for(*, instance_id: str, is_group: bool, target_id: str) -> str:
    """渠道窗口标识。

    群聊窗口只由 ``group_openid`` 决定，**不含发言人**——同一个群必须是一个
    共享会话，否则每人一个窗口，角色永远看不到完整群聊语境。
    """
    kind = "group" if is_group else "private"
    return f"{instance_id}:{kind}:{target_id}"


def target_from_window(window_ref: str) -> tuple[bool, str]:
    """``window_ref`` → (是否群聊, openid)。

    解析不出来就报错而不是拿整串去当 openid——发错了是给别人发消息，
    比明确失败严重得多。
    """
    parts = window_ref.split(":")
    if len(parts) != 3 or parts[1] not in {"group", "private"} or not parts[2]:
        raise ChannelError(
            ChannelErrorKind.rejected, f"无法从窗口标识解析目标: {window_ref}"
        )
    return parts[1] == "group", parts[2]


# ------------------------------------------------------------------ 出站


def build_message_body(
    text: str,
    *,
    msg_id: str | None,
    msg_seq: int | None,
    msg_type: int = 0,
    keyboard: dict | None = None,
    media: dict | None = None,
) -> dict:
    """构造发送消息体。

    ``msg_id`` 是**被动回复的凭据**：带上它，平台按"回复这条消息"处理，
    受次数/时效限制；不带它，平台按主动推送处理（需单独申请权限）。
    这里如实把两种情形都表达出来，不替调用方假装成被动回复。
    """
    body: dict = {"msg_type": msg_type}
    if msg_type == 2:
        # markdown：content 需由机器人侧自行渲染成 markdown 文本
        body["markdown"] = {"content": text}
    else:
        body["content"] = text
    if msg_id:
        body["msg_id"] = msg_id
    if msg_seq is not None:
        body["msg_seq"] = msg_seq
    if keyboard:
        body["keyboard"] = keyboard
    if media:
        body["media"] = media
    return body


def send_path(*, is_group: bool, target_id: str) -> str:
    """发送接口路径。

    - 私聊：``/v2/users/{openid}/messages``
    - 群聊：``/v2/groups/{group_openid}/messages``
    """
    if is_group:
        return f"/v2/groups/{target_id}/messages"
    return f"/v2/users/{target_id}/messages"


def split_text(text: str, limit: int) -> list[str]:
    """超长文本分片：优先在句末标点/换行处断开，**绝不丢字**。

    不做静默截断：截断会让用户看到半句话，而本地记录却是"已发送"。
    """
    if limit <= 0:
        raise ValueError("limit 必须为正")
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    current = ""
    for sentence in _sentences(text):
        while len(sentence) > limit:
            # 单句本身超长：硬切，但把已攒的先吐出去，保持顺序
            if current:
                chunks.append(current)
                current = ""
            chunks.append(sentence[:limit])
            sentence = sentence[limit:]
        if len(current) + len(sentence) <= limit:
            current += sentence
        else:
            if current:
                chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks


def _sentences(text: str) -> list[str]:
    """按句末标点/换行切句，标点留在句子里。"""
    out: list[str] = []
    buffer = ""
    for char in text:
        buffer += char
        if char in "。！？!?；;\n…":
            out.append(buffer)
            buffer = ""
    if buffer:
        out.append(buffer)
    return out


__all__ = [
    "CHANNEL_EVENTS",
    "EVENT_AT_MESSAGE_CREATE",
    "EVENT_C2C_MESSAGE_CREATE",
    "EVENT_DIRECT_MESSAGE_CREATE",
    "EVENT_GROUP_AT_MESSAGE_CREATE",
    "ParsedInbound",
    "SUPPORTED_EVENTS",
    "build_message_body",
    "media_marker",
    "parse_inbound_event",
    "send_path",
    "split_text",
    "strip_mentions",
    "target_from_window",
    "window_ref_for",
]
