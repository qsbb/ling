"""OneBot（NapCat）语音条**发送**：三种 transport 与 record 段结构。

## 这条测试在防什么

改动前 `connectors/onebot/` **只能收语音、不能发语音**：发送侧只实现了
`text` / `image` / `reply` / `at` 四种段，而 `build_media_segments` 把每个
``media_ref`` 都硬编码成 ``{"type": "image"}``。语音走那条路的后果是：
**用户收到的是一张打不开的图片，本地回执却是 `delivered`。**

所以这里钉的不是"能跑"，而是几条**不许退让**的规则：

1. ``record`` 段的**唯一**结构是 ``{"type":"record","data":{"file":<str>}}``
   ——``url`` / ``path`` 是接收字段，混进发送 payload 会被当成"本地文件名"。
2. 三种 transport 各自产出正确形态，**没有自动猜测**：未配置映射直接报错，
   不静默换一种方式发。
3. 超限**报错**，不截断、不降级（截断会产生坏音频，降级会变成别的东西）。
4. 「拒收后去装饰重试」这条分支**不能把语音丢掉，更不能变成 image**。
5. 回执语义与文本发送一致：没拿到 ``message_id`` 就只是 ``uncertain``。

全部用 ``httpx.MockTransport``，**不真发任何消息到 QQ**。
"""

from __future__ import annotations

import base64
import io
import json
import math
import shutil
import struct
import wave
from pathlib import Path

import httpx
import pytest
from connectors.onebot.connector import OneBotV11Connector
from connectors.onebot.message import build_outbound_segments
from connectors.onebot.settings import OneBotConfig
from connectors.onebot.voice import (
    DEFAULT_BASE64_MAX_BYTES,
    VoiceAsset,
    VoiceSendError,
    VoiceSendOptions,
    VoiceTransport,
    build_voice_file,
    build_voice_file_async,
    build_voice_segments,
    build_voice_segments_async,
    ensure_silk,
    silk_tmp_root,
    suggest_alternative,
    voice_segment,
    voice_summary,
)

from ling.contracts.v1 import AttachmentKind, DeliveryState, Modality
from ling.ports.channel import (
    ChannelError,
    ChannelErrorKind,
    OutboundAttachment,
    OutboundMessage,
)

# ------------------------------------------------------------------ 夹具

#: 一段假的 silk 字节（腾讯文件头 + 无意义负载）。
#:
#: 用真头有意义：NapCat 判"要不要转码"看的就是字节头。这里模拟的是
#: "已经是 silk、无需 NapCat 再转"的那一路，也让 base64 的内容可断言。
SILK_BYTES = b"\x02#!SILK_V3" + b"\x00" * 96


@pytest.fixture()
def silk_file(tmp_path: Path) -> Path:
    path = tmp_path / "reply.silk"
    path.write_bytes(SILK_BYTES)
    return path


@pytest.fixture()
def shared_root(tmp_path: Path) -> Path:
    root = tmp_path / "shared"
    root.mkdir()
    audio = root / "out" / "hello.silk"
    audio.parent.mkdir()
    audio.write_bytes(SILK_BYTES)
    return root


def _asset(path: Path, **kwargs) -> VoiceAsset:  # noqa: ANN003
    return VoiceAsset(path=path, **kwargs)


def _options(**overrides) -> VoiceSendOptions:  # noqa: ANN003
    return VoiceSendOptions(**overrides)


def _ok(data: object) -> httpx.Response:
    return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": data})


def _failed(retcode: int, wording: str = "") -> httpx.Response:
    body: dict = {"status": "failed", "retcode": retcode, "data": None}
    if wording:
        body["wording"] = wording
    return httpx.Response(200, json=body)


class _Recorder:
    """接住每次 POST 的 payload，供断言"到底发了什么"。"""

    def __init__(self, *responses: httpx.Response) -> None:
        self.payloads: list[dict] = []
        self._responses = list(responses) or [_ok({"message_id": 9001})]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.payloads.append(json.loads(request.content.decode("utf-8")))
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


def _connector(recorder: _Recorder, **config_overrides) -> OneBotV11Connector:  # noqa: ANN003
    config = OneBotConfig(
        api_base="http://onebot.test:5700",
        access_token="",
        self_id="10001",
        instance_id="onebot-voice",
        **config_overrides,
    )
    return OneBotV11Connector(
        config, http=httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))
    )


# ------------------------------------------------------------------ record 段结构


def test_record_segment_has_exactly_one_field() -> None:
    """段结构就是 ``{"type":"record","data":{"file":...}}``，别的一个字段都不许有。

    为什么钉死字段集合：``url`` 是**接收**字段（腾讯的下载地址）。一旦有人
    "顺手"把收到的 ``url`` 放进发送 payload，NapCat 会把它当成一个叫
    ``https://...`` 的本地文件去打开，然后失败——表现为"发了语音，对方没收到"，
    而本地没有任何报错。
    """
    segment = voice_segment("base64://AAAA")

    assert segment == {"type": "record", "data": {"file": "base64://AAAA"}}
    assert set(segment["data"]) == {"file"}
    # 段名必须是规范名：``voice`` 是老 CQ 码别名，发送侧一部分实现会忽略它。
    assert segment["type"] == "record"


def test_record_segment_rejects_empty_file() -> None:
    """空 ``file`` 直接报错：发一个空 record 让平台去猜，只会得到含糊的错误。"""
    with pytest.raises(VoiceSendError) as excinfo:
        voice_segment("")
    assert excinfo.value.reason == "empty_file_value"


# ------------------------------------------------------------------ transport: base64


def test_base64_transport_inlines_bytes(silk_file: Path) -> None:
    """base64 transport → ``base64://<b64>``，且解码回来就是原字节。"""
    file_value = build_voice_file(_asset(silk_file), _options(transport=VoiceTransport.base64))

    prefix = "base64://"
    assert file_value.startswith(prefix)
    assert base64.b64decode(file_value[len(prefix) :]) == SILK_BYTES
    # 体积代价如实存在（约 +33%）：这里顺带把它钉成可观测的事实
    assert len(file_value) > len(SILK_BYTES) * 4 / 3 - 4


def test_base64_transport_rejects_oversized_file(tmp_path: Path) -> None:
    """超过 base64 上限**报错**，不截断。

    截断的后果是一段能播放但内容是杂音的语音，用户会以为"她声音坏了"——
    比明确失败更难查。所以这里断言 reason 与文案里都带上了大小与上限。
    """
    big = tmp_path / "big.silk"
    big.write_bytes(b"\x02#!SILK_V3" + b"\x00" * 4096)
    options = _options(transport=VoiceTransport.base64, base64_max_bytes=1024)

    with pytest.raises(VoiceSendError) as excinfo:
        build_voice_file(_asset(big), options)

    assert excinfo.value.reason == "base64_too_large"
    assert "1024" in excinfo.value.detail
    # 失败要给可执行的替代方案，不能只说"太大"
    assert "shared_file" in excinfo.value.detail


def test_remote_url_without_local_file_is_rejected_not_sent_as_is() -> None:
    """只有远端 URL、没有本地文件 → **拒绝**，不把那个 URL 原样发出去。

    ## 这条在返工中改了语义（原来是"直接透传"）

    原来的实现把 ``asset.url`` 原样当 ``file`` 发出去，理由是"省一次
    下载+base64"。§17.3 裁决之后这条路**必须堵掉**：我们无法确认那个 URL
    指向的是不是 SILK。如果它是 mp3/wav，NapCat 会去调宿主机上那个不存在的
    ``ntsilk_s16le`` muxer——用户一条语音都收不到，而本地回执是 ``delivered``。

    现在的规则是：**要么本地有文件（我们自己转 SILK），要么不发。**
    """
    asset = VoiceAsset(path=None, url="https://media.test/a.mp3", name="a.mp3")

    with pytest.raises(VoiceSendError) as excinfo:
        build_voice_file(asset, _options(transport=VoiceTransport.base64))

    assert excinfo.value.reason == "silk_encode_failed"
    # 错误信息要说清"为什么不能发"和"该由谁负责"
    assert "SILK" in excinfo.value.detail


def test_default_base64_limit_is_documented_value() -> None:
    """默认上限是被论证过的常量，不是随手一个数——改动它要连带改注释。"""
    assert DEFAULT_BASE64_MAX_BYTES == 8 * 1024 * 1024


# ------------------------------------------------------------------ transport: shared_file


def test_shared_file_transport_maps_to_napcat_side_path(shared_root: Path) -> None:
    """``shared_file`` → ``file:///<NapCat 侧路径>``，路径来自**显式映射**。

    注意映射后的路径（``/app/qq/audio/...``）与 ling 侧完全不同：这正是
    跨容器部署的真实形态。
    """
    asset = _asset(shared_root / "out" / "hello.silk")
    options = _options(
        transport=VoiceTransport.shared_file,
        shared_path_from=str(shared_root),
        shared_path_to="/app/qq/audio",
    )

    file_value = build_voice_file(asset, options)

    assert file_value == "file:///app/qq/audio/out/hello.silk"


def test_shared_file_transport_requires_explicit_mapping(shared_root: Path) -> None:
    """没配映射就**报错**，不许把 ling 的路径直接发过去。

    这是本任务最容易写错的一处：``file:///`` 里的路径是 NapCat 所在
    容器/主机的路径。未配置时直接发 ling 的绝对路径，NapCat 那边不存在，
    平台回一句含糊的失败——而且看起来"配置能跑"，直到真正发语音才发现。
    """
    asset = _asset(shared_root / "out" / "hello.silk")

    with pytest.raises(VoiceSendError) as excinfo:
        build_voice_file(asset, _options(transport=VoiceTransport.shared_file))

    assert excinfo.value.reason == "shared_path_not_configured"
    assert "NapCat" in excinfo.value.detail


def test_shared_file_transport_rejects_path_outside_shared_root(
    silk_file: Path, shared_root: Path
) -> None:
    """文件不在共享目录里 → 报错（NapCat 看不到它，发出去也只会失败）。"""
    asset = _asset(silk_file)  # tmp_path/reply.silk，不在 tmp_path/shared 里
    options = _options(
        transport=VoiceTransport.shared_file,
        shared_path_from=str(shared_root),
        shared_path_to="/app/qq/audio",
    )

    with pytest.raises(VoiceSendError) as excinfo:
        build_voice_file(asset, options)

    assert excinfo.value.reason == "shared_path_outside"


def test_shared_file_transport_trailing_slash_is_irrelevant(shared_root: Path) -> None:
    """``shared_path_to`` 带不带尾斜杠都要拼出同一个路径（配置容错但不猜语义）。"""
    asset = _asset(shared_root / "out" / "hello.silk")
    base = {
        "transport": VoiceTransport.shared_file,
        "shared_path_from": str(shared_root),
    }

    with_slash = build_voice_file(asset, _options(**base, shared_path_to="/app/qq/audio/"))
    without = build_voice_file(asset, _options(**base, shared_path_to="/app/qq/audio"))

    assert with_slash == without == "file:///app/qq/audio/out/hello.silk"


# ------------------------------------------------------------------ transport: signed_url


@pytest.mark.asyncio
async def test_signed_url_transport_uses_injected_signer(silk_file: Path) -> None:
    """``signed_url`` 走**注入的签名回调**，产出短时签名 URL。"""
    seen: list[VoiceAsset] = []

    async def signer(asset: VoiceAsset) -> str:
        seen.append(asset)
        return "https://cdn.test/voice/reply.silk?exp=1700000000&sig=abc"

    file_value = await build_voice_file_async(
        _asset(silk_file), _options(transport=VoiceTransport.signed_url), sign_url=signer
    )

    assert file_value.startswith("https://cdn.test/voice/reply.silk?")
    assert seen and seen[0].path == silk_file


@pytest.mark.asyncio
async def test_signed_url_without_signer_fails_loudly(silk_file: Path) -> None:
    """未注入签名器 → **明确报错**，不许静默退回 base64。

    静默退回的后果：配了 ``signed_url`` 的部署在超限时会走另一条路，
    运维永远不知道签名根本没生效（这种"悄悄换方式"是本项目反复踩的坑）。
    """
    with pytest.raises(VoiceSendError) as excinfo:
        await build_voice_file_async(
            _asset(silk_file), _options(transport=VoiceTransport.signed_url)
        )

    assert excinfo.value.reason == "signer_not_configured"


@pytest.mark.asyncio
async def test_signed_url_rejects_non_http_scheme(silk_file: Path) -> None:
    """签名回来不是 http(s) → 报错。带 ``xxx:`` 前缀的非 http 值会被 NapCat
    当成**本地文件名**去打开。"""

    async def bad_signer(asset: VoiceAsset) -> str:  # noqa: ARG001
        return "s3://bucket/reply.silk"

    with pytest.raises(VoiceSendError) as excinfo:
        await build_voice_file_async(
            _asset(silk_file),
            _options(transport=VoiceTransport.signed_url),
            sign_url=bad_signer,
        )

    assert excinfo.value.reason == "signer_returned_bad_url"


@pytest.mark.asyncio
async def test_signed_url_rejects_empty_signer_result(silk_file: Path) -> None:
    """签名回调返回空串 → 报错（那等于"没有可下载地址"）。"""

    async def empty_signer(asset: VoiceAsset) -> str:  # noqa: ARG001
        return ""

    with pytest.raises(VoiceSendError) as excinfo:
        await build_voice_file_async(
            _asset(silk_file),
            _options(transport=VoiceTransport.signed_url),
            sign_url=empty_signer,
        )

    assert excinfo.value.reason == "signer_returned_empty"


def test_sync_entry_refuses_signed_url_instead_of_guessing(silk_file: Path) -> None:
    """同步入口遇到 signed_url 明确报错（签名要 await），不偷偷换 transport。"""
    with pytest.raises(VoiceSendError) as excinfo:
        build_voice_file(_asset(silk_file), _options(transport=VoiceTransport.signed_url))

    assert excinfo.value.reason == "async_only"


# ------------------------------------------------------------------ 一个语音条 = 一个 part


def test_one_voice_is_exactly_one_record_part(silk_file: Path) -> None:
    """**一个语音条 = 一个 record 段**，payload 里只有它，不掺 text。"""
    segments = build_voice_segments(_asset(silk_file), _options())

    assert len(segments) == 1
    assert segments[0]["type"] == "record"
    assert [s["type"] for s in segments] == ["record"]


@pytest.mark.asyncio
async def test_async_builder_matches_sync_builder(silk_file: Path) -> None:
    """异步版与同步版在不需要签名的 transport 上必须产出同一份结果。"""
    options = _options()
    sync_segments = build_voice_segments(_asset(silk_file), options)
    async_segments = await build_voice_segments_async(_asset(silk_file), options)

    assert sync_segments == async_segments


def test_voice_is_appended_after_text_not_mixed_in(silk_file: Path) -> None:
    """语音段排在正文之后：正文仍是一个独立 text 段，不被改写成别的东西。"""
    voice = build_voice_segments(_asset(silk_file), _options())

    segments = build_outbound_segments("我唱给你听", [], [], None, voice_segments=voice)

    assert [s["type"] for s in segments] == ["text", "record"]
    assert segments[0]["data"]["text"] == "我唱给你听"


def test_outbound_segments_refuses_non_record_voice_payload(silk_file: Path) -> None:
    """上游若把图片段塞进 ``voice_segments``，直接崩——不许静默变成图片。

    这条是"语音静默变成图片"的最后一道闸。
    """
    with pytest.raises(ValueError, match="record"):
        build_outbound_segments(
            "", [], [], None, voice_segments=[{"type": "image", "data": {"file": "a.png"}}]
        )


# ------------------------------------------------------------------ 端到端（mock）


@pytest.mark.asyncio
async def test_send_voice_posts_single_record_segment(silk_file: Path) -> None:
    """``send_voice`` 的 HTTP payload：只有一个 record 段，action 正确。"""
    recorder = _Recorder(_ok({"message_id": 4321}))
    connector = _connector(recorder)

    receipt = await connector.send_voice(
        window_ref="onebot-voice:private:100200300",
        asset=_asset(silk_file),
        operation_id="op_voice",
    )

    assert len(recorder.payloads) == 1
    payload = recorder.payloads[0]
    assert payload["user_id"] == "100200300"
    assert [seg["type"] for seg in payload["message"]] == ["record"]
    # **没有空 text 段**：部分实现对空 text 段直接报错
    assert all(seg["type"] != "text" for seg in payload["message"])
    assert receipt.state is DeliveryState.delivered
    assert receipt.platform_message_id == "4321"
    assert receipt.operation_id == "op_voice"


@pytest.mark.asyncio
async def test_send_voice_group_uses_group_action(silk_file: Path) -> None:
    """群聊走 ``send_group_msg``，参数是群号。"""
    recorder = _Recorder(_ok({"message_id": 7}))
    connector = _connector(recorder)

    await connector.send_voice(
        window_ref="onebot-voice:group:100200400",
        asset=_asset(silk_file),
        is_group=True,
    )

    assert recorder.payloads[0]["group_id"] == "100200400"
    assert [seg["type"] for seg in recorder.payloads[0]["message"]] == ["record"]


@pytest.mark.asyncio
async def test_send_voice_without_message_id_is_uncertain_not_delivered(
    silk_file: Path,
) -> None:
    """平台回了成功但没给 ``message_id`` → ``uncertain``，**不是** delivered。

    这条与文本发送同一条规矩：``retcode=0`` 只是"没说失败"。
    记成 delivered 会让上层以为用户听到了。
    """
    recorder = _Recorder(_ok({}))
    connector = _connector(recorder)

    receipt = await connector.send_voice(
        window_ref="onebot-voice:private:100200300", asset=_asset(silk_file)
    )

    assert receipt.state is DeliveryState.uncertain
    assert receipt.platform_message_id is None
    assert "无法确认" in (receipt.detail or "")


@pytest.mark.asyncio
async def test_send_voice_propagates_platform_rejection(silk_file: Path) -> None:
    """平台明确拒收 → ``ChannelError(rejected)`` 冒泡，不记成功。"""
    recorder = _Recorder(_failed(1400, "参数校验失败"))
    connector = _connector(recorder)

    with pytest.raises(ChannelError) as excinfo:
        await connector.send_voice(
            window_ref="onebot-voice:private:100200300", asset=_asset(silk_file)
        )

    assert excinfo.value.kind is ChannelErrorKind.rejected


@pytest.mark.asyncio
async def test_send_voice_rejects_oversized_asset_before_any_network_call(
    tmp_path: Path,
) -> None:
    """超过 ``voice_max_bytes`` 在**任何网络动作之前**就失败。"""
    big = tmp_path / "big.silk"
    big.write_bytes(b"\x02#!SILK_V3" + b"\x00" * 2048)
    recorder = _Recorder(_ok({"message_id": 1}))
    connector = _connector(recorder, voice_max_bytes=512)

    with pytest.raises(VoiceSendError) as excinfo:
        await connector.send_voice(
            window_ref="onebot-voice:private:100200300", asset=_asset(big)
        )

    assert excinfo.value.reason == "too_large"
    assert recorder.payloads == []  # 一个字都没发


@pytest.mark.asyncio
async def test_send_voice_uses_configured_shared_file_transport(
    shared_root: Path,
) -> None:
    """连接器的 ``shared_file`` 配置真的会生效（不是只有直接调函数才行）。"""
    recorder = _Recorder(_ok({"message_id": 55}))
    connector = _connector(
        recorder,
        voice_transport=VoiceTransport.shared_file,
        voice_shared_path_from=str(shared_root),
        voice_shared_path_to="/app/qq/audio",
    )

    await connector.send_voice(
        window_ref="onebot-voice:private:100200300",
        asset=_asset(shared_root / "out" / "hello.silk"),
    )

    segment = recorder.payloads[0]["message"][0]
    assert segment == {
        "type": "record",
        "data": {"file": "file:///app/qq/audio/out/hello.silk"},
    }


@pytest.mark.asyncio
async def test_send_voice_signed_url_end_to_end(silk_file: Path) -> None:
    """``signed_url`` 端到端：签名回调 → record 段 → 平台收到那个 URL。"""

    async def signer(asset: VoiceAsset) -> str:  # noqa: ARG001
        return "https://cdn.test/v/a.silk?sig=xyz"

    recorder = _Recorder(_ok({"message_id": 88}))
    connector = _connector(recorder, voice_transport=VoiceTransport.signed_url)

    await connector.send_voice(
        window_ref="onebot-voice:private:100200300",
        asset=_asset(silk_file),
        sign_url=signer,
    )

    assert recorder.payloads[0]["message"][0]["data"]["file"] == (
        "https://cdn.test/v/a.silk?sig=xyz"
    )


# ------------------------------------------------------------------ 拒收重试不许丢语音/变图片


@pytest.mark.asyncio
async def test_decoration_retry_keeps_voice_segment_and_never_becomes_image(
    silk_file: Path,
) -> None:
    """「拒收后去装饰重试」分支：语音**必须在**，且**绝不能**变成 image。

    这是本任务明确的硬要求。改动前那条分支的写法是
    ``build_outbound_segments(text, media_refs)``——它会把语音悄悄丢掉；
    如果有人"顺手"把语音塞进 ``media_refs`` 来绕过这个问题，语音就会以
    ``image`` 段发出去（用户在 QQ 里看到一张打不开的图）。
    """
    recorder = _Recorder(
        _failed(1400, "reply 段校验失败"),  # 第一次：装饰段被拒
        _ok({"message_id": 100}),  # 第二次：去装饰后成功
    )
    connector = _connector(recorder)

    message = OutboundMessage(
        operation_id="op_retry",
        window_ref="onebot-voice:private:100200300",
        text="这条带语音",
        is_group=False,
        mentions=["100200300"],
        attachments=[OutboundAttachment(kind=AttachmentKind.audio, ref=str(silk_file))],
    )
    receipt = await connector.send(message)

    assert len(recorder.payloads) == 2
    first, second = recorder.payloads
    # 第一次：@ 段 + 正文 + 语音
    assert "at" in [seg["type"] for seg in first["message"]]
    assert "record" in [seg["type"] for seg in first["message"]]
    # 第二次：@ 被去掉，正文与语音都还在
    assert "at" not in [seg["type"] for seg in second["message"]]
    assert [seg["type"] for seg in second["message"]] == ["text", "record"]
    # 语音段的 file 值是**转好的 SILK**（base64 内联）
    assert second["message"][1]["data"]["file"].startswith("base64://")
    # **绝不能变成 image**
    assert all(seg["type"] != "image" for seg in second["message"])
    assert receipt.state is DeliveryState.delivered


@pytest.mark.asyncio
async def test_voice_only_message_is_sent_once_without_a_placeholder_text_segment(
    silk_file: Path,
) -> None:
    """没有装饰段时不该进入重试分支：只发一次。

    ## 纯语音不需要空文本占位

    普通空消息仍由 ``build_media_segments`` 补一个 text 占位，
    但带 record 的纯语音消息必须只发 record 段。
    """
    recorder = _Recorder(_ok({"message_id": 1}))
    connector = _connector(recorder)

    await connector.send(
        OutboundMessage(
            operation_id="op_once",
            window_ref="onebot-voice:private:100200300",
            text="",
            is_group=False,
            attachments=[OutboundAttachment(kind=AttachmentKind.audio, ref=str(silk_file))],
        )
    )

    assert len(recorder.payloads) == 1
    types = [seg["type"] for seg in recorder.payloads[0]["message"]]
    assert types == ["record"]


# ------------------------------------------------------------------ 日志/摘要不许泄漏 payload


def test_each_transport_failure_names_a_workable_alternative() -> None:
    """失败信息必须带**可执行**的下一步，不能只说"发送失败"。

    为什么单独立一条：超限/未配置映射/签名失效是三种不同的配置问题，
    运维看到"语音未发送"四个字无从下手。``suggest_alternative`` 就是那段
    替代方案——把它摘掉，错误信息会退回成不可执行的一句话，而其它测试
    全都还是绿的（实测：删掉返回值 29 个测试全过）。
    """
    alternatives = {
        transport: suggest_alternative(VoiceSendOptions(transport=transport))
        for transport in VoiceTransport
    }

    for transport, text in alternatives.items():
        assert text.strip(), f"{transport} 的失败提示没给替代方案"
    # base64 超限时该指向另外两种 transport
    assert "shared_file" in alternatives[VoiceTransport.base64]
    assert "signed_url" in alternatives[VoiceTransport.base64]
    assert "base64" in alternatives[VoiceTransport.shared_file]
    assert "base64" in alternatives[VoiceTransport.signed_url]


def test_voice_summary_never_contains_payload(silk_file: Path) -> None:
    """摘要只说名字/大小/transport。

    ``base64://`` 的体动辄几 MB，写进日志既拖慢又等于把用户听到的东西整段
    落盘。
    """
    summary = voice_summary(_asset(silk_file), _options())

    assert "reply.silk" in summary
    assert str(len(SILK_BYTES)) in summary
    assert "base64" in summary
    assert "base64://" not in summary


# ------------------------------------------------------------------ SILK 转换接线
#
# 返工背景见 `connectors/onebot/silk.py` 与 docs/design/SPEECH-IO-ARCHITECTURE.md §17.3：
# NapCat 收到非 SILK 会调宿主机上不存在的 `ntsilk_s16le` muxer，用户一条语音
# 都收不到。所以**发出去的必须是 SILK**，这条不能是某个 transport 的可选项。


def _wav_bytes(rate: int = 44100, seconds: float = 0.2) -> bytes:
    """造一段正弦 WAV。"""
    frames = int(rate * seconds)
    pcm = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / rate)))
        for i in range(frames)
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


@pytest.fixture()
def mp3_like_wav(tmp_path: Path) -> Path:
    """一段**非 SILK** 的音频（44.1 kHz WAV，模拟厂商 TTS 产物）。"""
    path = tmp_path / "tts.wav"
    path.write_bytes(_wav_bytes())
    return path


def test_asset_already_silk_is_not_reencoded(mp3_like_wav: Path, tmp_path: Path) -> None:
    """已经是 SILK 的资产**原样返回**，不重新编码。

    为什么要短路：重新编码会引入代数损失，把已经校验过的音频再劣化一次。
    """
    from connectors.onebot.silk import default_codec

    silk_bytes = default_codec().to_silk(mp3_like_wav).data
    silk_path = tmp_path / "already.silk"
    silk_path.write_bytes(silk_bytes)
    asset = VoiceAsset(path=silk_path)

    handle = ensure_silk(asset)

    assert handle.converted is False
    assert handle.asset is asset  # 同一个对象，没被替换
    assert handle.temp_dir is None
    assert silk_path.read_bytes() == silk_bytes


def test_non_silk_audio_is_converted_before_sending(mp3_like_wav: Path) -> None:
    """**非 SILK 音频必须先转成 SILK**，且转出来的文件带 SILK 魔数。

    这是本次返工的核心断言：改动前会把 44.1k WAV 原样当 ``file`` 发出去，
    NapCat 收到后转码失败，用户一条语音都收不到。
    """
    asset = VoiceAsset(path=mp3_like_wav)
    assert not asset.looks_like_silk(), "前提：这个 fixture 不该是 SILK"

    handle = ensure_silk(asset)
    try:
        assert handle.converted is True
        assert handle.asset.path is not None
        assert handle.asset.path != mp3_like_wav
        assert handle.asset.path.read_bytes()[:10] == b"\x02#!SILK_V3"
        # 时长保住了（0.2s，允许 padding）
        assert handle.duration_seconds == pytest.approx(0.2, abs=0.06)
    finally:
        handle.cleanup()


def test_converted_silk_is_what_gets_base64_encoded(mp3_like_wav: Path) -> None:
    """base64 transport 编码的是**转好的 SILK**，不是原始 WAV。"""
    file_value = build_voice_file(
        VoiceAsset(path=mp3_like_wav), _options(transport=VoiceTransport.base64)
    )

    raw = base64.b64decode(file_value[len("base64://") :])
    assert raw[:10] == b"\x02#!SILK_V3", "发出去的字节不是 SILK"
    # 原始 WAV 的 RIFF 头不该出现在 payload 里
    assert not raw.startswith(b"RIFF")


def test_silk_conversion_failure_never_falls_back_to_original(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    """转码失败 → ``VoiceSendError(silk_encode_failed)``，**绝不**降级发原始文件。

    这是"不能偷偷换方式"的硬约束：降级发 WAV 的后果是 NapCat 转码失败、
    用户一条语音收不到，而本地回执是 delivered。
    """
    from connectors.onebot import voice as voice_module

    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"this is not audio at all")

    def boom(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise voice_module.SilkEncodeError("ffmpeg_failed", "解码失败（假的）")

    monkeypatch.setattr(voice_module, "default_codec", lambda: _BoomCodec())

    with pytest.raises(VoiceSendError) as excinfo:
        build_voice_file(VoiceAsset(path=bad), _options())

    assert excinfo.value.reason == "silk_encode_failed"
    assert "SILK" in excinfo.value.detail


class _BoomCodec:
    """总是失败的 codec，用来验证失败路径。"""

    max_duration_seconds = 60.0

    def to_silk(self, _source):  # noqa: ANN001, ANN202
        from connectors.onebot.silk import SilkEncodeError

        raise SilkEncodeError("ffmpeg_failed", "解码失败（假的）")


def test_missing_encoder_reason_is_actionable(mp3_like_wav: Path, monkeypatch) -> None:  # noqa: ANN001
    """没装 pysilk-mod 时，错误信息要指出**装哪个 extra**。"""
    from connectors.onebot import voice as voice_module
    from connectors.onebot.silk import PysilkEncoder, SilkCodec, SilkEncodeError

    class _NoEncoder(PysilkEncoder):
        def available(self):  # noqa: ANN202
            return False, "运行环境没有安装 pysilk-mod。装法：pip install 'ling[speech-codecs]'"

        def encode(self, _pcm):  # noqa: ANN001, ANN202
            raise SilkEncodeError("encoder_unavailable", self.available()[1])

    monkeypatch.setattr(
        voice_module,
        "default_codec",
        lambda: SilkCodec(encoder=_NoEncoder()),
    )

    with pytest.raises(VoiceSendError) as excinfo:
        build_voice_file(VoiceAsset(path=mp3_like_wav), _options())

    assert excinfo.value.reason == "silk_encode_failed"
    assert "speech-codecs" in excinfo.value.detail


def test_duration_guard_rejects_wrongly_encoded_audio(mp3_like_wav: Path) -> None:
    """时长漂移超限 → 报错。

    ## 为什么单独立一条（变异测试抓出来的洞）

    把 `to_silk` 里的漂移校验改成 `if False:` 后，**58 条测试全绿**——
    因为正常路径从来不产生漂移，那个校验等于没有被任何用例触发过。
    没有这条测试，"时长校验存在"只是纸面上的。

    这里用一个**故意把时长搞错的编码器**（把 PCM 变成 1.84 倍长，模拟
    谎报采样率）来逼出那个分支。
    """
    from connectors.onebot.silk import SilkCodec

    class SlowEncoder:
        """模拟"谎报采样率"：解码出来是输入的 1.84 倍长。"""

        name = "slow"

        def available(self) -> tuple[bool, str]:
            return True, ""

        def encode(self, pcm: bytes) -> bytes:
            return b"\x02#!SILK_V3" + pcm

        def decode(self, silk: bytes) -> bytes:
            body = silk[10:]
            return body + b"\x00" * int(len(body) * 0.84)

    # 恒等解码器：让输入直接当 24k PCM 用（默认解码器会把 bytes 当音频文件喂 ffmpeg）
    codec = SilkCodec(encoder=SlowEncoder(), decoder=lambda src: src)
    pcm = b"\x00\x00" * 24000  # 1.0s @24k

    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - 断言 reason 更有意义
        codec.to_silk(pcm)

    assert getattr(excinfo.value, "reason", "") == "duration_drift"
    assert "1.84" in getattr(excinfo.value, "detail", "") or "漂移" in str(excinfo.value)


@pytest.mark.asyncio
async def test_signed_url_also_converts_to_silk(mp3_like_wav: Path) -> None:
    """``signed_url`` transport **也要**先转 SILK 再签名。

    ## 为什么单独立一条（变异测试抓出来的洞）

    把异步分支里的 `ensure_silk` 删掉后，**58 条测试全绿**——因为原来的
    signed_url 用例喂的 fixture 本身就"看起来像 SILK"，转换是个空操作。
    这一个洞会让 signed_url 部署重蹈覆辙：签出去的是原始 mp3 的 URL，
    NapCat 下载后仍要转码，仍然失败。

    这里断言：签名回调**收到的 asset** 已经是 SILK 文件。
    """
    seen: list[VoiceAsset] = []

    async def signer(asset: VoiceAsset) -> str:
        seen.append(asset)
        assert asset.path is not None
        # 签名那一刻，文件必须已经是 SILK
        assert asset.path.read_bytes()[:10] == b"\x02#!SILK_V3"
        return "https://cdn.test/v/a.silk"

    file_value = await build_voice_file_async(
        VoiceAsset(path=mp3_like_wav),
        _options(transport=VoiceTransport.signed_url),
        sign_url=signer,
    )

    assert file_value == "https://cdn.test/v/a.silk"
    assert len(seen) == 1
    assert seen[0].path != mp3_like_wav


@pytest.mark.asyncio
async def test_signed_url_does_not_sign_the_original_mp3_url() -> None:
    """已有远端 URL 但**没有本地文件** → signed_url 也要拒绝，不能签原始 URL。

    签出去一个 mp3 的 URL 等于把"NapCat 转码会失败"这件事原样传给下一个环节，
    而且这次我们连本地转码的机会都没有。
    """
    asset = VoiceAsset(path=None, url="https://remote.test/a.mp3", name="a.mp3")

    with pytest.raises(VoiceSendError) as excinfo:
        await build_voice_file_async(
            asset,
            _options(transport=VoiceTransport.signed_url),
            sign_url=lambda _a: _async_none(),
        )

    assert excinfo.value.reason == "silk_encode_failed"


async def _async_none() -> str:
    return "https://cdn.test/should-not-get-here.silk"


# ------------------------------------------------------------------ 临时文件清理


@pytest.mark.asyncio
async def test_send_voice_cleans_up_temp_silk_after_success(mp3_like_wav: Path) -> None:
    """发送成功后**必须删掉**转码临时文件。

    ## 为什么值得一条测试（变异测试抓出来的洞）

    清理逻辑放在 `finally` 里，把它删掉后原有 58 条测试**全绿**——因为没人
    检查过 /tmp 里有没有残留。不清理的后果很实在：每发一条语音就在 /tmp
    留一个目录，长期跑把容器磁盘填满，而且是**静默**的（日志里没有错误）。
    """
    import tempfile as tf

    recorder = _Recorder(_ok({"message_id": 1}))
    connector = _connector(recorder)
    before = set(Path(tf.gettempdir()).glob("ling-silk-*"))

    await connector.send_voice(
        window_ref="onebot-voice:private:100200300",
        asset=VoiceAsset(path=mp3_like_wav),
    )

    after = set(Path(tf.gettempdir()).glob("ling-silk-*"))
    leaked = after - before
    assert not leaked, f"发送后残留临时目录：{leaked}"


@pytest.mark.asyncio
async def test_send_voice_cleans_up_temp_silk_after_failure(mp3_like_wav: Path) -> None:
    """平台拒收时也要清理：失败路径的临时文件更没价值。"""
    import tempfile as tf

    recorder = _Recorder(_failed(1400, "参数校验失败"))
    connector = _connector(recorder)
    before = set(Path(tf.gettempdir()).glob("ling-silk-*"))

    with pytest.raises(ChannelError):
        await connector.send_voice(
            window_ref="onebot-voice:private:100200300",
            asset=VoiceAsset(path=mp3_like_wav),
        )

    after = set(Path(tf.gettempdir()).glob("ling-silk-*"))
    assert not (after - before), "失败后残留临时目录"


def test_shared_file_transport_puts_temp_silk_inside_shared_root(
    mp3_like_wav: Path, shared_root: Path, tmp_path: Path
) -> None:
    """``shared_file`` 下转码临时文件必须落在**共享根目录之内**。

    ## 为什么这条必须存在（变异测试抓出来的洞）

    把 `silk_tmp_root` 改成永远返回 None（即用系统 /tmp）后，**63 条测试
    全绿**——因为没有用例检查过临时文件的**位置**。而位置在这里是硬约束：

    ``_map_shared_path`` 要求文件在 ``shared_path_from`` 之内，否则报
    ``shared_path_outside``。临时文件放在 /tmp 时，NapCat 那个容器里根本
    没有这个路径，`file:///` 指的是一份不存在的文件——表现是
    "base64 能用、shared_file 一开全失败"，而错误信息看起来像路径配置问题。
    """
    source = shared_root / "out" / "input.wav"
    source.write_bytes(_wav_bytes())
    options = _options(
        transport=VoiceTransport.shared_file,
        shared_path_from=str(shared_root),
        shared_path_to="/app/qq/audio",
    )
    # 临时目录应当就是共享根
    assert silk_tmp_root(options) == shared_root

    before = set(shared_root.glob("ling-silk-*"))
    file_value = build_voice_file(VoiceAsset(path=source), options)
    after = set(shared_root.glob("ling-silk-*"))
    created = after - before
    try:
        assert created, "没在共享根下建临时目录"
        # 映射出来的路径必须指向那个临时目录里的文件
        assert file_value.startswith("file:///app/qq/audio/ling-silk-")
        mapped_rel = file_value[len("file:///app/qq/audio/") :]
        assert (shared_root / mapped_rel).is_file()
    finally:
        for directory in created:
            shutil.rmtree(directory, ignore_errors=True)


def test_base64_transport_uses_system_temp(mp3_like_wav: Path) -> None:
    """非 shared_file 的 transport 用系统临时目录（不需要落在共享根里）。"""
    assert silk_tmp_root(_options(transport=VoiceTransport.base64)) is None


# ------------------------------------------------------------------ send() 走类型化附件
#
# 返工第 2 步：语音不再靠 `getattr(message, "voice_segments")` 的幽灵通道，
# 而是走 `OutboundMessage.attachments`（SP00 已定稿的正式字段）。
# 一个消息**最多一个** audio 附件——「一个语音条 = 一个 payload」由结构保证。


@pytest.mark.asyncio
async def test_send_delivers_voice_via_typed_attachment(silk_file: Path) -> None:
    """``send()`` + 一个 audio 附件 → payload 里出现 record 段。"""
    recorder = _Recorder(_ok({"message_id": 77}))
    connector = _connector(recorder)

    receipt = await connector.send(
        OutboundMessage(
            operation_id="op_typed",
            window_ref="onebot-voice:private:100200300",
            text="听这个",
            attachments=[OutboundAttachment(kind=AttachmentKind.audio, ref=str(silk_file))],
        )
    )

    assert len(recorder.payloads) == 1
    types = [seg["type"] for seg in recorder.payloads[0]["message"]]
    assert "record" in types
    assert receipt.state is DeliveryState.delivered


@pytest.mark.asyncio
async def test_send_rejects_two_audio_attachments(tmp_path: Path, silk_file: Path) -> None:
    """**一个消息带 2 个 audio 附件 → 报错**，且不发出任何请求。

    ## 为什么这条必须存在

    「一个语音条 = 一个 payload」在方向 1 下是**结构性**保证的。若允许
    两个 audio 附件通过，实现就只能二选一：
    - 只发第一个 → 用户少听一条，回执却记 delivered（**静默丢失**）
    - 循环发两条 → 一次 ``send()`` 产生 2 个 ``platform_message_id``，
      而``DeliveryReceipt`` 只有一个字段，另一条的回执只能塞进人类可读的
      ``detail`` 字符串（以后对账要再解析它）

    两个都是坏结果，所以在**任何网络动作之前**就拒绝。

    错误信息必须说**怎么做**（拆成多条消息、各自独立 part_ordinal），
    不能只说"参数错误"。
    """
    recorder = _Recorder(_ok({"message_id": 1}))
    connector = _connector(recorder)
    second = tmp_path / "second.silk"
    second.write_bytes(silk_file.read_bytes())

    with pytest.raises(ChannelError) as excinfo:
        await connector.send(
            OutboundMessage(
                operation_id="op_two",
                window_ref="onebot-voice:private:100200300",
                text="两条语音",
                attachments=[
                    OutboundAttachment(kind=AttachmentKind.audio, ref=str(silk_file)),
                    OutboundAttachment(kind=AttachmentKind.audio, ref=str(second)),
                ],
            )
        )

    assert excinfo.value.kind is ChannelErrorKind.rejected
    # 说清"怎么做"
    assert "拆成多条消息" in excinfo.value.message
    assert "part_ordinal" in excinfo.value.message
    # **一个字都没发出去**
    assert recorder.payloads == []


@pytest.mark.asyncio
async def test_send_ignores_image_attachments_for_voice_path(silk_file: Path) -> None:
    """图片附件不会被误当语音；语音与图片可以同消息（1 图 + 1 语音是允许的）。

    「最多一个 audio」限的是**语音**，不是附件总数：一条消息带图又说一句
    语音是合法表达。
    """
    recorder = _Recorder(_ok({"message_id": 9}))
    connector = _connector(recorder)

    await connector.send(
        OutboundMessage(
            operation_id="op_mixed",
            window_ref="onebot-voice:private:100200300",
            text="图文语音",
            media_refs=["asset:pic1"],
            attachments=[OutboundAttachment(kind=AttachmentKind.audio, ref=str(silk_file))],
        )
    )

    types = [seg["type"] for seg in recorder.payloads[0]["message"]]
    assert types.count("image") == 1
    assert types.count("record") == 1


@pytest.mark.asyncio
async def test_send_voice_attachment_is_converted_to_silk(mp3_like_wav: Path) -> None:
    """走 ``send()`` 的非 SILK 音频**也会被转成 SILK**（与 send_voice 同一套逻辑）。"""
    recorder = _Recorder(_ok({"message_id": 5}))
    connector = _connector(recorder)

    await connector.send(
        OutboundMessage(
            operation_id="op_conv",
            window_ref="onebot-voice:private:100200300",
            text="",
            attachments=[
                OutboundAttachment(kind=AttachmentKind.audio, ref=str(mp3_like_wav))
            ],
        )
    )

    file_value = recorder.payloads[0]["message"][-1]["data"]["file"]
    assert file_value.startswith("base64://")
    raw = base64.b64decode(file_value[len("base64://") :])
    assert raw[:10] == b"\x02#!SILK_V3"


@pytest.mark.asyncio
async def test_send_rejects_base64_voice_ref(silk_file: Path) -> None:
    """``ref`` 是 ``base64://`` 时明确拒绝，并指出该怎么做。

    连接器需要**本地文件**才能校验/转 SILK。接受一串来路不明的 base64
    等于放弃校验——我们无法确认那串字节是不是 SILK，而"看起来能发"正是
    最危险的状态。
    """
    recorder = _Recorder(_ok({"message_id": 1}))
    connector = _connector(recorder)

    with pytest.raises(ChannelError) as excinfo:
        await connector.send(
            OutboundMessage(
                operation_id="op_b64",
                window_ref="onebot-voice:private:100200300",
                text="",
                attachments=[
                    OutboundAttachment(kind=AttachmentKind.audio, ref="base64://QUJD")
                ],
            )
        )

    assert excinfo.value.kind is ChannelErrorKind.rejected
    assert "本地" in excinfo.value.message
    assert recorder.payloads == []


# ------------------------------------------------------------------ 能力声明与 readiness 联动


def test_capabilities_declares_audio_when_silk_ready() -> None:
    """SILK 链路就绪 → ``supported_media`` 里**有** audio。"""
    connector = _connector(_Recorder(_ok({})))

    caps = connector.capabilities()

    assert caps.supported_media is not None
    assert Modality.audio in caps.supported_media
    assert Modality.image in caps.supported_media


def test_capabilities_omits_audio_when_silk_not_ready(monkeypatch) -> None:  # noqa: ANN001
    """SILK 未就绪 → ``supported_media`` 里**没有** audio。

    ## 这是防止"虚假能力声明"的那道闸

    不这么做的话：投递层看到 audio 被支持 → 放行 → 连接器在发送时才失败。
    用户看到的是「她试着说话但失败了」，而不是「这个渠道不支持语音」。
    而且旁路 ``send_voice()`` 却能跑通——等于**奖励错误路径**。

    依据 §17.6 点名的"用总 supports_media 宣称已支持语音"。
    """
    from connectors.onebot import connector as connector_module

    class _NotReady:
        max_duration_seconds = 60.0

        def readiness(self):  # noqa: ANN202
            return False, "运行环境缺少 ffmpeg"

    monkeypatch.setattr(connector_module, "default_codec", lambda: _NotReady())
    connector = _connector(_Recorder(_ok({})))

    caps = connector.capabilities()

    assert caps.supported_media is not None
    assert Modality.audio not in caps.supported_media
    # 图片仍然声明（缺 ffmpeg 不影响发图）
    assert Modality.image in caps.supported_media


def test_voice_readiness_reason_is_actionable(monkeypatch) -> None:  # noqa: ANN001
    """运维要能查"为什么没有语音"：原因里要指出缺什么、怎么装。"""
    from connectors.onebot import connector as connector_module

    class _NotReady:
        max_duration_seconds = 60.0

        def readiness(self):  # noqa: ANN202
            return False, "运行环境缺少 ffmpeg。Debian/Ubuntu 装法：apt-get install -y ffmpeg"

    monkeypatch.setattr(connector_module, "default_codec", lambda: _NotReady())
    connector = _connector(_Recorder(_ok({})))

    ready, reason = connector.voice_readiness()

    assert ready is False
    assert "ffmpeg" in reason
    assert "apt-get" in reason


def test_voice_readiness_is_cached(monkeypatch) -> None:  # noqa: ANN001
    """探测结果要缓存：``capabilities()`` 每条消息都调，不能每次都查磁盘。"""
    from connectors.onebot import connector as connector_module

    calls: list[int] = []

    class _Counting:
        max_duration_seconds = 60.0

        def readiness(self):  # noqa: ANN202
            calls.append(1)
            return True, ""

    monkeypatch.setattr(connector_module, "default_codec", lambda: _Counting())
    connector = _connector(_Recorder(_ok({})))

    for _ in range(5):
        connector.capabilities()

    assert len(calls) == 1, f"探测了 {len(calls)} 次，缓存没生效"


def test_refresh_voice_readiness_invalidates_cache(monkeypatch) -> None:  # noqa: ANN001
    """依赖装好后不用重启进程：``refresh_voice_readiness()`` 要能失效缓存。

    场景：进程启动时没有 ffmpeg → audio 不声明；运维装了 ffmpeg 之后
    调用 refresh → audio 立刻出现。
    """
    from connectors.onebot import connector as connector_module

    state = {"ok": False}

    class _Flaky:
        max_duration_seconds = 60.0

        def readiness(self):  # noqa: ANN202
            return (True, "") if state["ok"] else (False, "缺少 ffmpeg")

    monkeypatch.setattr(connector_module, "default_codec", lambda: _Flaky())
    connector = _connector(_Recorder(_ok({})))

    assert Modality.audio not in (connector.capabilities().supported_media or [])
    # 运维装好了
    state["ok"] = True
    connector.refresh_voice_readiness()
    assert Modality.audio in (connector.capabilities().supported_media or [])
