"""语音段 `record` / 表情包 `mface`：**不再被静默丢弃**。

## 这条测试在守什么

OneBot **v11 规范里语音段的类型名是 `record`**（NapCat 发出来的就是它），
`voice` 只是老 CQ 码时代的别名。解析器原来只认 `voice`，于是：

- 用户在 QQ 里发一条语音 → 段类型 `record` 匹配不上任何分支 → **整条消失**；
- 连 `[voice]` 这样的占位都不剩，模型完全不知道"用户刚才发过语音"，
  表现就是"她无视语音消息"。

`mface`（表情包）是同一个形态：协议上有 `url` / `summary`，原来也一律丢弃。

## 为什么占位仍是 `[voice]` 而不是 `[语音]`

`kernel/bridge.py` 用 `_MEDIA_MARKERS = \\[(?:image|voice|video|file)\\]`
剥掉占位标记，判断"这条消息里有没有真话"（只发一张图时正文等于 `[image]`，
剥不掉就会被当成用户发言去回复）。占位写成 `[语音]` 会让那条判断失效。
所以**机器读的占位保持英文段名**，中文人话由 `media_segment_label()` 提供。
下面第二条用例专门钉这个前提：纯语音消息在 bridge 眼里必须"没有可读文本"。

## 两种消息形态都要覆盖

字符串（CQ 码）与数组共用 `_handle_segment`，但**入口不同**，所以两种都测：
真要分叉了（比如某天给字符串路径加了特殊处理），这里会立刻变红。
"""

from __future__ import annotations

import logging

import pytest
from connectors.onebot.message import ParsedMessage, media_segment_label, parse_message

VOICE_URL = "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=abc.silk"
MFACE_URL = "https://gxh.vip.qq.com/club/item/parcel/item/ab/cd/raw300.gif"


def _warning_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == "ling.connectors.onebot.message" and record.levelno == logging.WARNING
    ]


# ---------------------------------------------------------------- record（数组）


def test_record_array_with_url_is_kept() -> None:
    """数组形态的 `record`：URL 进 media_refs，正文留占位。"""
    parsed = parse_message(
        [
            {"type": "text", "data": {"text": "听一下这个"}},
            {"type": "record", "data": {"file": "abc.silk", "url": VOICE_URL}},
        ]
    )
    assert parsed.media_refs == [VOICE_URL]
    assert parsed.text == "听一下这个[voice]"
    assert parsed.text.startswith("听一下这个"), "用户自己说的话必须原样在前面"


def test_record_with_only_file_still_enters_media_refs() -> None:
    """只有 `file`、没有 `url` 的语音（部分实现的形态）也得进 media_refs。

    `file` 常常是文件名或内部 file id（NapCat 的 `abc.silk` 就是），
    它不是坏数据——`media_ref_is_valid` 明确放行"不带 scheme 的不透明 id"。
    """
    parsed = parse_message([{"type": "record", "data": {"file": "abc.silk"}}])
    assert parsed.media_refs == ["abc.silk"]
    assert parsed.text == "[voice]"


def test_record_with_magic_does_not_lose_the_attachment() -> None:
    """带 `magic: 1`（"变声/魔音"标记）的语音一样要收。

    `magic` 只是播放提示，不影响这个附件能不能下载；按它过滤等于把
    变声语音静默吞掉。
    """
    parsed = parse_message([{"type": "record", "data": {"file": "abc.amr", "magic": 1}}])
    assert parsed.media_refs == ["abc.amr"]
    assert parsed.text == "[voice]"


def test_voice_alias_behaves_like_record() -> None:
    """老别名 `voice` 与规范名 `record` 走同一条分支、产出同一种形态。"""
    alias = parse_message([{"type": "voice", "data": {"url": VOICE_URL}}])
    canonical = parse_message([{"type": "record", "data": {"url": VOICE_URL}}])
    assert alias.media_refs == canonical.media_refs == [VOICE_URL]
    assert alias.text == canonical.text == "[voice]"


# ---------------------------------------------------------------- record（CQ 码）


def test_record_cq_string_with_url_is_kept() -> None:
    """字符串（CQ 码）形态：`[CQ:record,...]` 同样要进 media_refs。"""
    parsed = parse_message(f"你好[CQ:record,file=abc.silk,url={VOICE_URL}]")
    assert parsed.media_refs == [VOICE_URL]
    assert parsed.text == "你好[voice]"


def test_record_cq_string_with_only_file() -> None:
    parsed = parse_message("[CQ:record,file=abc.silk]")
    assert parsed.media_refs == ["abc.silk"]
    assert parsed.text == "[voice]"


def test_record_cq_string_mixed_with_other_segments() -> None:
    """CQ 字符串里与 @、图片混排时，语音不能挤掉别人，也不能被挤掉。"""
    parsed = parse_message("[CQ:at,qq=10002] 你看[CQ:image,file=a.png]和[CQ:record,file=b.silk]")
    assert parsed.text == "@10002 你看[image]和[voice]"
    assert parsed.media_refs == ["a.png", "b.silk"]


# ---------------------------------------------------------------- 坏引用


def test_bad_record_url_only_drops_that_attachment(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """坏引用只丢该附件：正文保留、**只 warning 一次**、不抛异常。

    这条语义是现成的（`image` 一直如此），语音必须一致——否则一条打不开的
    语音会把整句"这个字怎么念"一起吞掉。
    """
    caplog.set_level(logging.WARNING, logger="ling.connectors.onebot.message")
    parsed = parse_message(
        [
            {"type": "text", "data": {"text": "这个字怎么念"}},
            # file: 协议会让 fetch_media 读本地文件 —— 必须被拒
            {"type": "record", "data": {"url": "file:///etc/passwd"}},
        ]
    )
    assert parsed.media_refs == []
    assert parsed.text == "这个字怎么念[voice]", "整条消息不能跟着一个坏附件一起消失"
    warnings = _warning_records(caplog)
    assert len(warnings) == 1, f"应当只警告一次，实际 {len(warnings)} 次"
    assert "record" in warnings[0].getMessage()


# ---------------------------------------------------------------- mface（表情包）


def test_mface_with_url_is_kept() -> None:
    """表情包：占位 `[表情包]`，有 url 时进 media_refs。"""
    parsed = parse_message([{"type": "mface", "data": {"summary": "[打call]", "url": MFACE_URL}}])
    assert parsed.text == "[表情包]"
    assert parsed.media_refs == [MFACE_URL]


def test_mface_without_url_still_leaves_a_placeholder() -> None:
    """没有 url（有的实现只给 `summary`）时仍要有占位。

    表情包是用户表达的一部分：整条消失和"看见了"是两回事。
    """
    parsed = parse_message([{"type": "mface", "data": {"summary": "[打call]"}}])
    assert parsed.text == "[表情包]"
    assert parsed.media_refs == []


def test_mface_cq_string() -> None:
    # CQ 码里的 `[` / `]` 必须转义（`&#91;` / `&#93;`），否则值里的 `]`
    # 会提前结束这一段——这是 CQ 格式自身的规则，不是解析器的 bug。
    parsed = parse_message(f"[CQ:mface,summary=&#91;打call&#93;,url={MFACE_URL}]")
    assert parsed.text == "[表情包]"
    assert parsed.media_refs == [MFACE_URL]


# ---------------------------------------------------------------- 明确不处理


@pytest.mark.parametrize("seg_type", ["dice", "rps", "shake", "share", "location"])
def test_unsupported_segments_stay_out_of_text(seg_type: str) -> None:
    """`dice`/`rps`/`shake`/`share`/`location` **明确不处理**（本轮不动）。

    它们没有可读文本，`share`/`location` 的 url 是网页/坐标而不是附件，
    塞进 media_refs 只会让下游去下载一个 HTML 页面。
    """
    parsed = parse_message(
        [{"type": seg_type, "data": {"url": "https://example.test/page", "file": "x"}}]
    )
    assert parsed == ParsedMessage(), f"{seg_type} 不该产生任何正文或媒体引用"


# ---------------------------------------------------------------- 与 bridge 的契约


@pytest.mark.parametrize("seg_type", ["record", "voice"])
def test_pure_voice_is_not_readable_text_for_bridge(seg_type: str) -> None:
    """纯语音消息在 bridge 眼里必须**没有可读文本**。

    `kernel/bridge.py` 用 `_MEDIA_MARKERS` 剥占位标记来判断"有没有真话"。
    占位若写成 `[语音]` 这类它不认的形态，纯语音会被当成用户发言去回复——
    所以这条用例钉住"占位必须是那四个英文段名之一"这个前提。
    这里直接 import bridge 的正则：它是判定口径的唯一来源，不在测试里抄一份。
    """
    from ling.kernel.bridge import _MEDIA_MARKERS

    parsed = parse_message([{"type": seg_type, "data": {"url": VOICE_URL}}])
    assert parsed.text == "[voice]"
    assert _MEDIA_MARKERS.sub("", parsed.text).strip() == "", (
        "占位没被 bridge 剥掉：纯语音会被误判为有可读文本"
    )


def test_segment_labels_are_chinese_human_words() -> None:
    """给日志/提示用的段名是中文人话，`record` 不能说成"record"。"""
    assert media_segment_label("record") == "语音"
    assert media_segment_label("voice") == "语音"
    assert media_segment_label("mface") == "表情包"
    # 不认识的段名原样返回，不要编一个
    assert media_segment_label("weird_type") == "weird_type"


def test_array_and_string_paths_share_segment_handling() -> None:
    """数组与 CQ 字符串两条入口，对同一段产出**完全相同**的结果。

    两条路径都走 `_handle_segment`；这个用例是那个事实的可执行版本——
    哪天有人给某条路径加特殊处理，两者会立刻不一致。
    """
    array_form = parse_message(
        [
            {"type": "text", "data": {"text": "在吗"}},
            {"type": "record", "data": {"file": "a.silk", "url": VOICE_URL}},
            {"type": "mface", "data": {"url": MFACE_URL}},
        ]
    )
    string_form = parse_message(
        f"在吗[CQ:record,file=a.silk,url={VOICE_URL}][CQ:mface,url={MFACE_URL}]"
    )
    assert array_form.text == string_form.text == "在吗[voice][表情包]"
    assert array_form.media_refs == string_form.media_refs == [VOICE_URL, MFACE_URL]


# ---------------------------------------------------------------- QQBot 侧核对
#
# QQ 官方机器人走的是另一套协议（`d.content` + `d.attachments`，没有消息段），
# 所以"段类型"这件事在那里体现为附件 `content_type`。核对结论：
# **语音附件不会被丢**（`_media_refs` 不看类型、只认 https URL），但占位名
# 依赖 `content_type` 主类型，而 QQ 家族在协议层把语音叫 `record`。


def test_qqbot_record_content_type_maps_to_voice_marker() -> None:
    """QQBot：`record/*` 的附件要显示成 `[voice]`，不是 `[file]`。

    与 OneBot 侧同一个教训（`record` 是规范名，`voice` 是别名）。占位若落成
    `[file]`，模型会以为用户发了个文档而不是一条语音。
    """
    from connectors.qqbot.message import media_marker

    assert media_marker("record/amr") == "[voice]"
    assert media_marker("voice/silk") == "[voice]"
    assert media_marker("audio/mpeg") == "[voice]"
    assert media_marker("image/png") == "[image]"
    assert media_marker("video/mp4") == "[video]"
    assert media_marker("file/pdf") == "[file]"


def test_qqbot_voice_attachment_keeps_its_url() -> None:
    """QQBot 语音附件本身**不丢**：url 照收，只是占位名要对。

    这条同时是"核对过、无此问题"的可执行证据：`_media_refs` 不按类型过滤。
    """
    from connectors.qqbot.message import parse_inbound_event

    payload = {
        "op": 0,
        "t": "C2C_MESSAGE_CREATE",
        "d": {
            "id": "msg_voice",
            "content": "",
            "timestamp": "2026-11-09T00:00:00Z",
            "author": {"user_openid": "openid-user"},
            "attachments": [
                {"content_type": "record/amr", "url": "https://qq.test/a.amr"},
            ],
        },
    }
    parsed = parse_inbound_event(payload)
    assert parsed.media_refs == ["https://qq.test/a.amr"]
    assert parsed.text == ""
