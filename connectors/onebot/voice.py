"""OneBot（NapCat）语音条**发送**：把一段音频变成 ``record`` 段的 ``file`` 值。

## 为什么需要一个独立模块

`.message.py` 已经有 ``image`` 段的构造，但它把每个媒体引用都硬编码成
``{"type": "image"}``。语音条如果走那条路，会**被当成图片发出去**——在
QQ 客户端里表现为"一张打不开的图"，而本地回执是 ``delivered``。这是本项目
最不能容忍的那类错误：看起来成功了，实际什么都没到。

而 ``OutboundMessage.media_refs`` 只是 ``list[str]``，**没有类型信息**——
从字符串本身猜"这是不是语音"（看扩展名）是不可靠的：NapCat 的语音 ``file``
经常是 ``abc.amr`` 这种无域名裸文件名，而一张图片也可能叫 ``a.mp3.png``。
所以判断依据必须由调用方**显式**给（见 :func:`voice_segment` 的 ``kind``），
这里不提供任何猜测分支。

## OneBot v11 的语音段

.. code-block:: json

    {"type": "record", "data": {"file": "<四种形态之一>"}}

``file`` 支持四种形态，本项目实现其中三种 **transport**：

=========== ========================== =============================================
transport   ``file`` 的形态             适用
=========== ========================== =============================================
``base64``  ``base64://<b64>``          跨机部署默认候选；体积 +33%
``shared_file`` ``file:///<绝对路径>``  NapCat 与 ling 共享同一个目录时
``signed_url`` 短时签名 https URL       对象存储/自建签名服务
=========== ========================== =============================================

**第四种（收到的原始文件名）故意不支持**：那要求文件已经在 NapCat 自己的
``data/record`` 目录里，而我们产生的是**新**音频，不存在那个名字。硬要支持
等于让调用方去猜 NapCat 的文件布局。

## ``url`` 不是 ``file``

接收侧 ``data`` 里有 ``url``（腾讯的下载地址），发送侧**只认 ``file``**。
把接收字段原样回填到 ``file`` 里，NapCat 会当成"本地文件名叫
``https://...``"去打开然后失败——这就是最常见的"发了没反应"。

## NapCat 会自己转码

``napcat-core/helper/audio.ts`` 检查字节是不是 SILK，不是就调
``FFmpegService.convertToNTSilkTct()``。所以这里**不做 silk 编码**：
我们只管把字节/路径送到它手上。已知风险：NapCat 宿主机的 ffmpeg 若缺
``ntsilk_s16le`` muxer（Ubuntu 22.04 的 4.4.2 就没有），转码会失败——
那时平台会返回错误码，我们如实记成 ``rejected``，不谎报送达。

## 三条不许退让的规则

1. **一个语音条 = 一个 part**：payload 里只有一个 ``record``，不掺 ``text``。
   混在一起会让"她只发了一条语音"变成"一条文字+一条语音"。
2. **不能退回成 ``image``**：任何失败路径都报错或降级为**文本说明**，
   不能悄悄换段名（那等于把语音静默变成一张图）。
3. **失败必须可区分**：超限、未配置、签名失败是三种不同的事实，
   各自有明确的 :class:`VoiceSendError` 消息，不许压成一句"语音发送失败"。
"""

from __future__ import annotations

import base64
import logging
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from .silk import SILK_MAGIC, SilkCodec, SilkEncodeError, default_codec, describe_silk

logger = logging.getLogger("ling.connectors.onebot.voice")

#: OneBot v11 里语音段的段名。
#:
#: ``record`` 是**规范**段名（NapCat 发的就是它）；``voice`` 只是老 CQ 码
#: 时代的别名，**发送侧不能用**——发 ``voice`` 段一部分实现会忽略它。
RECORD_SEGMENT = "record"

#: base64 transport 的默认体积上限（字节）。
#:
#: 为什么要有上限：``base64://`` 是**内联在 JSON 里**的，NapCat 收到后先落
#: 临时文件再转码。一段 60 秒的 wav（44.1kHz/16bit 单声道 ≈ 5.3 MB）经
#: base64 变成 7 MB 的 JSON body；QQ 的一条消息撑不住这个量级，而且
#: ``api_base`` 那一跳是全量进内存的。8 MB 原始字节（≈ 10.7 MB base64）
#: 是"能发出去"和"别把 NapCat 打挂"之间的折中，可由配置覆盖。
DEFAULT_BASE64_MAX_BYTES = 8 * 1024 * 1024

#: 语音条的大小上限（字节），三种 transport 共用。
#:
#: 与 base64 上限分开：路径与 URL 不占 JSON 体积，但仍然不该毫无节制——
#: 超大音频在 QQ 侧会被拒绝或转码失败，早点拒绝比发出去再失败好。
DEFAULT_VOICE_MAX_BYTES = 32 * 1024 * 1024


class VoiceTransport(StrEnum):
    """音频怎么送到 NapCat 手上。

    **没有默认值**，必须显式配置。猜一个默认值等于把"跨机部署"和
    "同机部署"两种完全不同的环境绑在一起：猜 base64 会让大文件无声失败，
    猜 shared_file 会在跨机时发出一堆 ``file:///`` 打不开的消息。
    """

    base64 = "base64"
    shared_file = "shared_file"
    signed_url = "signed_url"


class VoiceAudioKind(StrEnum):
    """音频来源的形态——决定 transport 拿什么做输入。"""

    #: 服务器本地磁盘上的一个文件（shared_file / base64 都能用）
    local_file = "local_file"
    #: 已经是一个可访问的 https URL（只有 signed_url 需要，见下文）
    remote_url = "remote_url"


class VoiceSendError(Exception):
    """语音**没发出去**的确定性原因。

    带 ``transport`` 与 ``reason``：事后能分清"是配置错了"还是"文件太大"，
    而不是只剩一句"语音发送失败"。
    """

    def __init__(self, reason: str, detail: str, *, transport: str = "") -> None:
        super().__init__(f"语音未发送（{reason}）：{detail}")
        self.reason = reason
        self.detail = detail
        self.transport = transport


@dataclass(frozen=True)
class VoiceAsset:
    """待发送的一段音频。

    刻意做成**小值对象**而不是直接吃 ``bytes``：这样 transport 之间可以互相
    校验（例如 shared_file 必须知道"ling 的路径"和"NapCat 看到的路径"
    是不是同一个），而且延迟到真正需要时才读文件——base64 要读全文，
    shared_file 一个字节都不用读。
    """

    #: ling 这一侧的文件路径。``remote_url`` 形态时为 ``None``。
    path: Path | None
    mime_type: str = "audio/silk"
    #: 已经能访问的 URL（``signed_url`` 且调用方自己签好时填它）
    url: str | None = None
    #: 诊断用的名字（日志里只说名字不打印整段 base64）
    name: str = ""

    def display_name(self) -> str:
        if self.name:
            return self.name
        if self.path is not None:
            return self.path.name
        return "voice"

    def size_bytes(self) -> int:
        if self.path is None:
            return 0
        try:
            return self.path.stat().st_size
        except OSError as exc:
            raise VoiceSendError(
                "file_unreadable",
                f"读不到音频文件 {self.display_name()}（{type(exc).__name__}）",
            ) from exc

    def read_bytes(self) -> bytes:
        if self.path is None:
            raise VoiceSendError(
                "no_local_bytes", f"{self.display_name()} 不是本地文件，无法内联"
            )
        try:
            return self.path.read_bytes()
        except OSError as exc:
            raise VoiceSendError(
                "file_unreadable",
                f"读不到音频文件 {self.display_name()}（{type(exc).__name__}）",
            ) from exc

    def looks_like_silk(self) -> bool:
        """前 10 字节是否已经是 SILK（与 NapCat 的 ``isSilk`` 同一口径）。

        ## 为什么要有这个短路

        自己产出 SILK 之后，**绝大多数**语音到这里已经是 SILK 了（TTS 产物
        走 ``silk.py`` 转一次，存在资产库里的就是 SILK）。再转一遍不只是浪费：
        解码 SILK → 重采样 → 重新编码会引入**代数损失**，把上一轮已经
        校验过的音频再劣化一次。

        判断口径必须与 NapCat 完全一致（前 10 字节找 ``#!SILK``）：口径不一致
        就会出现"我们以为它是 SILK、NapCat 以为它不是"，然后 NapCat 去调那个
        不存在的 ``ntsilk_s16le`` muxer，用户一条语音都收不到。
        """
        if self.path is None:
            return False
        try:
            with self.path.open("rb") as handle:
                head = handle.read(10)
        except OSError:
            # 读不到就**不声称**它是 silk（宁可多转一次，也别把损坏文件
            # 当 SILK 发出去）。
            return False
        return SILK_MAGIC in head


#: 签名回调：把 ling 侧路径变成"外部能下载的短时 URL"。
#:
#: 为什么是**注入的回调**而不是内置 S3/OSS 客户端：
#: 签名机制属于部署环境（对象存储、反代、自建签名服务各不相同），
#: 把它写进连接器会把一套存储凭据绑进渠道层。连接器只声明协议，
#: 未配置时**明确失败**而不是退化成别的方式——退化成 base64 会在大文件上
#: 静默失败，那比直接报错更难查。
SignUrl = Callable[[VoiceAsset], Awaitable[str]]


@dataclass(frozen=True)
class VoiceSendOptions:
    """三种 transport 的配置。

    对应 ``OneBotConfig`` 的同名字段；单独成类是为了让测试能直接构造，
    不必凭空造一个完整连接配置。
    """

    transport: VoiceTransport = VoiceTransport.base64
    #: ling 侧路径前缀 → NapCat 侧路径前缀。
    #:
    #: **必须显式配置**：``file:///`` 里的路径是 **NapCat 所在容器/主机**的
    #: 路径，不是 ling 的。两者相同只是同机同文件系统时的特例；跨容器时
    #: 同一个文件在两边的绝对路径通常不同（例如 ling 看到
    #: ``/var/lib/ling/media/a.silk``，NapCat 看到 ``/app/.config/QQ/a.silk``
    #: 或 Windows 上的 ``C:\\Users\\Public\\audio\\a.silk``）。
    shared_path_from: str = ""
    shared_path_to: str = ""
    base64_max_bytes: int = DEFAULT_BASE64_MAX_BYTES
    max_bytes: int = DEFAULT_VOICE_MAX_BYTES
    #: 短时签名 URL 的有效期（秒）。只用于日志与交给签名回调，本模块不校验。
    signed_url_ttl_seconds: int = 300


def _map_shared_path(asset: VoiceAsset, options: VoiceSendOptions) -> str:
    """把 ling 侧路径映射成 NapCat 侧路径，返回 ``file:///`` 形态。"""
    if asset.path is None:
        raise VoiceSendError(
            "no_local_file",
            f"{asset.display_name()} 不是本地文件，shared_file 发不了",
            transport=VoiceTransport.shared_file.value,
        )
    if not options.shared_path_from or not options.shared_path_to:
        # 未配置映射时**不能**直接把 ling 的路径发过去：NapCat 在另一个
        # 容器/主机里，那个路径大概率不存在，平台只会回一句含糊的失败。
        raise VoiceSendError(
            "shared_path_not_configured",
            "shared_file transport 需要同时配置 shared_path_from 与 shared_path_to"
            "（file:/// 里的路径是 NapCat 那一侧的路径，不是 ling 的）",
            transport=VoiceTransport.shared_file.value,
        )
    local = asset.path.resolve()
    prefix = Path(options.shared_path_from).resolve()
    try:
        relative = local.relative_to(prefix)
    except ValueError as exc:
        raise VoiceSendError(
            "shared_path_outside",
            f"{local} 不在共享目录 {prefix} 之内，NapCat 读不到它",
            transport=VoiceTransport.shared_file.value,
        ) from exc
    # 用 POSIX 拼接：NapCat 侧即使跑在 Windows（共享目录映射）也接受 `/`
    # 分隔的 file URI；反斜杠在 JSON 里还要二次转义，容易出错。
    target = PurePosixPath(options.shared_path_to.rstrip("/")) / relative.as_posix()
    return f"file:///{str(target).lstrip('/')}"


#: 转码后文件的临时目录前缀。用系统临时目录而不是资产库：
#: §17.3 的资产服务（``data_dir/generated_media``）归 E02/E03，本连接器
#: 只做到"临发送时把 SILK 字节交出去"这一步。
SILK_TMP_PREFIX = "ling-silk-"


@dataclass(frozen=True)
class SilkVoice:
    """一次「非 SILK → SILK」转换的结果。

    临时文件的生命周期由调用方负责（:meth:`cleanup`）。**故意不自动删**：
    transport 构造完 ``file`` 值之后，平台还要去读那个文件（``shared_file``
    是直接读路径），提前删掉会让发送失败。
    """

    asset: VoiceAsset
    temp_dir: Path | None
    converted: bool
    duration_seconds: float = 0.0

    def cleanup(self) -> None:
        if self.temp_dir is None:
            return
        shutil.rmtree(self.temp_dir, ignore_errors=True)


def ensure_silk(
    asset: VoiceAsset,
    *,
    codec: SilkCodec | None = None,
    tmp_root: Path | None = None,
) -> SilkVoice:
    """保证交给平台的字节是**真正的 SILK**。

    ## 两条路径

    1. 已经是 SILK（前 10 字节命中 ``#!SILK``）→ **原样返回**，不重新编码。
       重新编码会引入代数损失，把已经校验过的音频再劣化一次。
    2. 不是 SILK → 解码、**真重采样到 24 kHz 单声道**、编码、校验时长，
       写到临时文件，返回那个新 asset。

    ## 失败一律抛 ``VoiceSendError(reason="silk_encode_failed")``

    **绝不**降级发原始 wav/mp3：NapCat 收到非 SILK 会去调它宿主机上
    那个不存在的 ``ntsilk_s16le`` muxer，结果是**用户一条语音都收不到**。
    那正是这次返工要根治的事，不能在这里留一个"实在不行就发原文件"的口子。

    （§17.1 裁决表 / §17.3 固定转换链。理由见 ``silk.py`` 模块 docstring。）
    """
    if asset.looks_like_silk():
        return SilkVoice(asset=asset, temp_dir=None, converted=False)
    if asset.path is None:
        raise VoiceSendError(
            "silk_encode_failed",
            f"{asset.display_name()} 既不是 SILK 也不是本地文件，无法转码"
            "（远程 URL 形态必须由上游先产出 SILK）",
        )
    active_codec = codec or default_codec()
    try:
        payload = active_codec.to_silk(asset.path)
    except SilkEncodeError as exc:
        raise VoiceSendError(
            "silk_encode_failed",
            f"{asset.display_name()} 无法转成 SILK（{exc.reason}）：{exc.detail}",
        ) from exc
    # 临时文件用带前缀的独立目录：cleanup 一次删干净，不会误删别的东西。
    directory = Path(tempfile.mkdtemp(prefix=SILK_TMP_PREFIX, dir=tmp_root))
    target = directory / "voice.silk"
    try:
        target.write_bytes(payload.data)
    except OSError as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise VoiceSendError(
            "silk_write_failed",
            f"写不出临时 SILK 文件（{type(exc).__name__}）",
        ) from exc
    converted_asset = VoiceAsset(
        path=target,
        mime_type="audio/silk",
        url=None,
        # 名字保留原来的：日志里说"reply.mp3"比说"voice.silk"有用，
        # 运维要能对上"是哪个产物转的"。
        name=asset.display_name(),
    )
    logger.debug(
        "已转成 SILK：%s → %s（%s）",
        asset.display_name(),
        target,
        describe_silk(payload),
    )
    return SilkVoice(
        asset=converted_asset,
        temp_dir=directory,
        converted=True,
        duration_seconds=payload.duration_seconds,
    )


def silk_tmp_root(options: VoiceSendOptions) -> Path | None:
    """转码临时文件该落在哪里。

    ``shared_file`` 有一个**硬约束**：转码后的文件必须落在
    ``shared_path_from`` 之内，否则 ``_map_shared_path`` 会拒绝它
    （NapCat 读不到共享目录外的路径）。所以这种 transport 下临时目录要建在
    共享根目录里；其它 transport 用系统临时目录即可。

    这条容易被漏掉：不这么做的表现是"base64 能用、shared_file 一开就全失败"，
    而且错误信息看起来是路径配置问题，实际是临时文件放错地方。
    """
    if options.transport is VoiceTransport.shared_file and options.shared_path_from:
        root = Path(options.shared_path_from)
        if root.is_dir():
            return root
    return None


def build_voice_file(
    asset: VoiceAsset,
    options: VoiceSendOptions,
    *,
    sign_url: SignUrl | None = None,
    codec: SilkCodec | None = None,
) -> str:
    """按 ``options.transport`` 算出 ``record`` 段的 ``file`` 值。

    三种 transport 一一对应，**没有"自动选择"分支**：猜错会静默发不出去。
    """
    transport = options.transport
    if transport is VoiceTransport.signed_url:
        # 签名要 await，走 ``build_voice_file_async``。放在这里**明确报错**
        # 而不是让同步入口悄悄退回 base64——那会让"配了 signed_url"的部署
        # 在超限时静默走另一条路，运维永远不知道签名没生效。
        raise VoiceSendError(
            "async_only",
            "signed_url 需要签名回调，请用 build_voice_file_async / "
            "build_voice_segments_async",
            transport=transport.value,
        )

    # **发出去的永远是 SILK**。这一步放在所有 transport 之前，是本次返工的
    # 核心：NapCat 收到非 SILK 会去调它宿主机上那个不存在的 ntsilk_s16le
    # muxer，结果是用户一条语音都收不到。所以它不能是"某个 transport 的可选项"。
    #
    # 注意：调用方拿到 file 值之后还要用那个临时文件（shared_file 直接读路径），
    # 所以这里**不清理**临时目录；生命周期由调用方通过 ensure_silk 的返回值管理。
    silk = ensure_silk(asset, codec=codec, tmp_root=silk_tmp_root(options))
    asset = silk.asset

    if transport is VoiceTransport.base64:
        if asset.path is None:
            if asset.url:
                # 已经是 https URL：交给平台自己取。这条路不经过 base64，
                # 因为把远程 URL 下载下来再 base64 一遍纯属浪费带宽与内存。
                return asset.url
            raise VoiceSendError(
                "no_local_file",
                f"{asset.display_name()} 既没有本地文件也没有 URL，base64 无从构造",
                transport=transport.value,
            )
        size = asset.size_bytes()
        if size > options.base64_max_bytes:
            # 超限**报错**，不截断也不降级成别的 transport：
            # 截断会产生一段坏音频（用户听到杂音），降级则会让"发语音"
            # 变成"发了别的东西"。
            raise VoiceSendError(
                "base64_too_large",
                f"{asset.display_name()} 大小 {size} 字节，超过 base64 上限 "
                f"{options.base64_max_bytes} 字节（改 base64_max_bytes 或改用 "
                f"shared_file/signed_url）",
                transport=transport.value,
            )
        payload = base64.b64encode(asset.read_bytes()).decode("ascii")
        return f"base64://{payload}"

    if transport is VoiceTransport.shared_file:
        return _map_shared_path(asset, options)

    raise VoiceSendError(
        "unknown_transport",
        f"不认识的 transport：{transport!r}",
        transport=str(transport),
    )


async def build_voice_file_async(
    asset: VoiceAsset,
    options: VoiceSendOptions,
    *,
    sign_url: SignUrl | None = None,
    codec: SilkCodec | None = None,
) -> str:
    """与 :func:`build_voice_file` 相同，但支持需要 ``await`` 的签名回调。"""
    if options.transport is not VoiceTransport.signed_url:
        return build_voice_file(asset, options, sign_url=sign_url, codec=codec)
    if sign_url is None:
        raise VoiceSendError(
            "signer_not_configured",
            "signed_url transport 需要一个签名回调（sign_url），未配置时不能发送",
            transport=VoiceTransport.signed_url.value,
        )
    # **先转 SILK，再签名**。顺序不能反：
    # 先签名等于把「原始 mp3 的 URL」签出去，NapCat 下载下来仍是非 SILK，
    # 于是又回到那个会失败的 ntsilk 转码路径——而且这次连"改本地文件"
    # 这条退路都没有（我们手上只有一个远端 URL）。
    #
    # 代价是"必须先有本地文件"。已经有远端 URL 的调用方（`asset.url`）如果
    # 那个 URL 指向的不是 SILK，这里会明确失败，而不是把 mp3 的 URL 发出去。
    silk = ensure_silk(asset, codec=codec, tmp_root=silk_tmp_root(options))
    asset = silk.asset
    url = await sign_url(asset)
    if not url:
        raise VoiceSendError(
            "signer_returned_empty",
            "签名回调返回了空 URL（音频没有可下载地址）",
            transport=VoiceTransport.signed_url.value,
        )
    if not url.startswith(("https://", "http://")):
        # OneBot 的 file 若是带 ``xxx:`` 前缀但又不是 http(s)/base64/file，
        # NapCat 会当成**本地文件名**去打开。宁可这里报错。
        raise VoiceSendError(
            "signer_returned_bad_url",
            f"签名回调返回的不是 http(s) URL（{url.split(':', 1)[0]}://…），"
            "NapCat 会把它当本地文件名",
            transport=VoiceTransport.signed_url.value,
        )
    return url


def voice_segment(file_value: str) -> dict:
    """构造**唯一**的那一个 ``record`` 段。

    返回 ``{"type": "record", "data": {"file": <file_value>}}``——
    除了 ``file`` 之外不放别的东西：``url`` / ``path`` 是接收字段，
    混进发送 payload 只会被平台忽略或误用。
    """
    if not file_value:
        raise VoiceSendError("empty_file_value", "record 段的 file 不能为空")
    return {"type": RECORD_SEGMENT, "data": {"file": file_value}}


def build_voice_segments(
    asset: VoiceAsset, options: VoiceSendOptions, *, sign_url: SignUrl | None = None
) -> list[dict]:
    """同步版：一个语音条 = 一个 ``record`` 段的列表。"""
    return [voice_segment(build_voice_file(asset, options, sign_url=sign_url))]


async def build_voice_segments_async(
    asset: VoiceAsset, options: VoiceSendOptions, *, sign_url: SignUrl | None = None
) -> list[dict]:
    """异步版：``signed_url`` 的签名回调可能要外呼。"""
    return [voice_segment(await build_voice_file_async(asset, options, sign_url=sign_url))]


def voice_summary(asset: VoiceAsset, options: VoiceSendOptions) -> str:
    """给日志/回执用的**不含 payload** 的摘要。

    ``base64://`` 的 payload 动辄几 MB：把它写进日志既拖慢又等于把用户听到的
    东西整段落盘。摘要只说名字、大小、transport。
    """
    try:
        size = asset.size_bytes()
    except VoiceSendError:
        size = -1
    return (
        f"语音 {asset.display_name()}（{size} 字节，transport={options.transport.value}）"
    )


def suggest_alternative(options: VoiceSendOptions) -> str:
    """失败时给一句**可执行**的替代方案（不许只说"发送失败"）。"""
    transport = options.transport
    if transport is VoiceTransport.base64:
        return "改 shared_file（配好路径映射）或 signed_url 可发大文件"
    if transport is VoiceTransport.shared_file:
        return "检查 shared_path_from/shared_path_to 覆盖了这个目录；或改用 base64"
    if transport is VoiceTransport.signed_url:
        return "检查签名回调与 TTL；或改用 base64/shared_file"
    return "检查 voice_transport 配置"


__all__ = [
    "DEFAULT_BASE64_MAX_BYTES",
    "DEFAULT_VOICE_MAX_BYTES",
    "RECORD_SEGMENT",
    "SignUrl",
    "VoiceAsset",
    "VoiceAudioKind",
    "VoiceSendError",
    "VoiceSendOptions",
    "VoiceTransport",
    "build_voice_file",
    "build_voice_file_async",
    "build_voice_segments",
    "build_voice_segments_async",
    "suggest_alternative",
    "voice_segment",
    "voice_summary",
]
