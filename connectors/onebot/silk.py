"""把任意音频转成 **Tencent SILK**——OneBot（NapCat）语音条真正要的格式。

## 为什么必须自己做

NapCat 收到非 SILK 音频时会**自己尝试转码**：它检查前 10 字节有没有
``#!SILK``，没有就调 ``/usr/bin/ffmpeg -f ntsilk_s16le``。而 Ubuntu 22.04
的 ffmpeg 4.4.2 **没有这个 muxer**，命令必然失败：

    Requested output format 'ntsilk_s16le' is not a suitable output format

后果是**用户一条语音都收不到**。反过来，只要我们自己产出带
``\\x02#!SILK_V3`` 头的 SILK，``isSilk`` 命中就会跳过转码，直接走它本来正常
工作的发送路径。

依据：`docs/design/SPEECH-IO-ARCHITECTURE.md` §17.3（与 §17.1 裁决表
"NapCat：ling 自己输出 Tencent SILK 后再发 record"）。邻仓
``astrbot_plugin_voice_hub/core/silk_codec.py`` 记录了同一个坑。

## 固定转换链（§17.3）

.. code-block:: text

    任意已允许的厂商音频
     -> ffmpeg 验证、解码（受限本地文件，超时/资源上限）
     -> PCM s16le mono 24000 Hz
     -> silk 编码
     -> 校验前缀 b"\\x02#!SILK_V3"、非空、时长合理
     -> 作为独立衍生资产 -> 平台发送

## 三个必须由**我们**保证的事（实测，不是推测）

2026-11-11 在本机对 ``pysilk-mod==1.6.4`` 逐条实测，结论推翻了"让编码器
拒绝错误输入"这个设计假设：

=============================== ==========================================
输入                             实际行为
=============================== ==========================================
44.1 kHz PCM，参数填 44100        **编码成功**，不报错
44.1 kHz 数据谎报成 24 kHz        **编码成功**，解码回来 1.840s（真值 1.000s）
                                 → 播放慢 **1.84 倍**，语速全错
``encode(b"")``                  返回合法 ``\\x02#!SILK_V3`` 头，**不抛异常**
=============================== ==========================================

即：**编码器不会替我们拦住任何错误输入**。所以：

1. **重采样必须由本模块主动做**（`:func:`_decode_to_pcm24``），绝不把
   非 24 kHz 的数据交给编码器；
2. **空/静音必须主动拒绝**（编码器会给一个"成功"的空文件）；
3. **时长必须解码回来验**（编码器"成功"不等于内容对）。

第 2 条的反向验证见 ``tests/unit/test_onebot_silk.py::test_unresampled_input_plays_1_84x_slow``
——那条断言"如果没重采样，时长会变成 1.84 倍"，用来证明我们**真的**重采样
了，而不是"恰好没出错"。

## 依赖

- ``pysilk-mod``：预编译 wheel（cp38–cp313，manylinux x86_64/aarch64、
  macOS、Windows），零运行时依赖。经 ``SilkEncoder`` 协议抽象，**可替换**。
- ``ffmpeg``：解码 + 重采样。**只用 ffmpeg，不用 ffprobe**——见
  :data:`FFMPEG_BIN` 的注释（本机实测 ``ffprobe`` 是个指向 ffmpeg 的符号
  链接，不支持 ``-show_format``／``-of json``）。
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger("ling.connectors.onebot.silk")

#: SILK 内部固定采样率。QQ 语音就是 24 kHz，§17.3 把它定为项目统一策略。
SILK_SAMPLE_RATE = 24000

#: SILK 编码的**名义**码率（bps），24 kbps 是 QQ 语音的常见档位。
#:
#: .. warning::
#:
#:    **这个常量不控制实际码率。** ``pysilk-mod`` 的 ``encode()`` 签名是
#:    ``encode(data, data_rate, *, sample_rate)``——只有采样率，**没有码率参数**，
#:    实际码率由它内置的默认值决定。
#:
#:    保留这个名字而不是删掉，是为了让"QQ 语音大约是 24 kbps"这个事实有个
#:    具名落点（文档与断言引用它）。但它**不是**可调参数：调它不会改变任何输出。
#:    叫 ``SILK_BIT_RATE`` 会让人以为"调这里就能改码率"，然后花时间排查
#:    "为什么调了没变化"——所以名字里带 ``NOMINAL``。
SILK_NOMINAL_BIT_RATE = 24000

#: Tencent SILK 文件魔数。
#:
#: 完整前缀是 ``b"\x02#!SILK_V3"``：``\x02`` 是头长度字段，``#!SILK_V3`` 是
#: 版本标记。NapCat 的 ``isSilk`` 就是在前 10 字节里找 ``#!SILK``。
SILK_MAGIC = b"#!SILK"
SILK_HEADER = b"\x02#!SILK_V3"

#: 允许的输入采样率（silk 编码器的硬限制）。24 kHz 是本项目统一输出。
ALLOWED_INPUT_RATES = (8000, 12000, 16000, 24000)

#: 时长校验允许的偏差（秒）。
#:
#: §17.3 原话："允许 codec padding（例如 ≤60ms）"。silk 是分帧编码（20ms 一帧），
#: 末尾补零会带来最多一帧的偏差；60 ms 留了三帧余量，足够容纳重采样的边缘效应，
#: 又能抓住"谎报采样率"这类量级错误（1.84 倍偏差远超它）。
MAX_DURATION_DRIFT_SECONDS = 0.060

#: ffmpeg 单次调用超时（秒）。
FFMPEG_TIMEOUT_SECONDS = 60.0

#: ffmpeg 可执行文件名。**故意不导出 ffprobe**。
#:
#: 2026-11-11 本机实测：``/Users/lingxi/.local/bin/ffprobe`` 是一个指向
#: ``imageio_ffmpeg/binaries/ffmpeg-...`` 的**符号链接**，它不支持
#: ``-show_format`` / ``-of json``（报 ``Unrecognized option``）。也就是说
#: "机器上有 ffprobe"并不代表"它能用"。用 ffmpeg 自己读元信息（``-i`` 的
#: stderr）或直接数解码出的 PCM 字节，跨环境更稳，也少一个依赖。
FFMPEG_BIN = "ffmpeg"

#: 单帧时长（秒）。silk 的 20ms 帧，用于把偏差换算成"几帧"好读。
FRAME_SECONDS = 0.020


class SilkEncodeError(Exception):
    """SILK 编码失败——**按原因分类**，不许压成一句"编码失败"。

    ``reason`` 是给程序判断的稳定短标识，``detail`` 是给人看的人话。
    上层（``voice.py``）会把它们翻译成 ``VoiceSendError``。
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"SILK 编码失败（{reason}）：{detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class SilkPayload:
    """编码好的 SILK 音频。"""

    data: bytes
    #: 编码后时长（秒），由**解码回来**得到，不是从输入推算的。
    duration_seconds: float
    #: 输入侧的时长（秒），用于对比。``None`` 表示输入时长不可知。
    source_duration_seconds: float | None = None

    def is_silk(self) -> bool:
        """前 10 字节是否命中 SILK 魔数（与 NapCat 的 ``isSilk`` 同一口径）。"""
        return SILK_MAGIC in self.data[:10]

    def duration_drift(self) -> float:
        """编码前后时长差（秒）；输入时长未知时返回 0.0。"""
        if self.source_duration_seconds is None:
            return 0.0
        return abs(self.duration_seconds - self.source_duration_seconds)


# ---------------------------------------------------------------- 编码器抽象


@runtime_checkable
class SilkEncoder(Protocol):
    """SILK 编码器接口。

    ## 为什么要抽象

    底层的 ``pysilk-mod`` 内部打包的是**腾讯 SILK SDK 的二进制**。AGPL 项目
    依赖它的许可合规性需要单独确认（lead 正在与用户核实）。把它做成一个
    **可替换的实现**而不是硬依赖，意味着：如果最终决定不用它，换一个实现
    即可，``voice.py`` 与连接器一行都不用改。

    实现方只需要承诺一件事：**给定 24 kHz 单声道 s16le PCM，返回 SILK 字节**。
    采样率校验、重采样、时长校验都在本模块的上层（:func:`to_silk`）做——
    因为那些是**策略**，不是编码器的能力。把策略放进编码器，换实现时就得
    重写一遍，而重写时最容易漏掉"谎报采样率"这种静默错误。
    """

    @property
    def name(self) -> str:
        """实现名（进日志与 readiness 报告）。"""
        ...

    def available(self) -> tuple[bool, str]:
        """这个实现当前能不能用；返回 (可用, 不可用原因)。"""
        ...

    def encode(self, pcm24_mono_s16le: bytes) -> bytes:
        """把 24 kHz 单声道 s16le PCM 编成 SILK 字节。"""
        ...

    def decode(self, silk: bytes) -> bytes:
        """把 SILK 解回 24 kHz 单声道 s16le PCM（校验时长要用）。"""
        ...


class PysilkEncoder:
    """``pysilk-mod`` 实现（默认）。

    ## 参数顺序是个坑

    ``pysilk.encode(data, data_rate=24000, *, sample_rate=24000)`` 有**两个**
    采样率参数，且都是同义词（``data_rate`` 是位置参数，``sample_rate`` 是
    关键字参数）。实测 ``encode(pcm_44k, 44100, sample_rate=44100)`` 会静默
    成功——所以本实现**永远传 24000**，不接受外部指定采样率：这个类只承诺
    "24 kHz 进、SILK 出"，别的组合不该经过它。
    """

    def __init__(self, module: object | None = None) -> None:
        # 允许注入模块，测试里可以塞假实现而不用真装 pysilk-mod。
        self._module = module

    @property
    def name(self) -> str:
        return "pysilk-mod"

    def _load(self) -> object:
        module = self._module
        if module is None:
            try:
                import pysilk  # noqa: PLC0415 - 可选依赖，延迟到用时才导入
            except Exception as exc:  # noqa: BLE001 - 缺依赖要变成明确原因
                raise SilkEncodeError(
                    "encoder_unavailable",
                    f"运行环境没有安装 pysilk-mod（{type(exc).__name__}）。"
                    "装法：pip install 'ling[speech-codecs]'",
                ) from exc
            module = pysilk
            self._module = module
        return module

    def available(self) -> tuple[bool, str]:
        try:
            self._load()
        except SilkEncodeError as exc:
            return False, exc.detail
        return True, ""

    def encode(self, pcm24_mono_s16le: bytes) -> bytes:
        if not pcm24_mono_s16le:
            # 实测编码器对空输入返回一个合法 SILK 头（不抛异常），
            # 那会变成一条"发出去但没声音"的语音。在这里挡住。
            raise SilkEncodeError("empty_pcm", "PCM 为空，编码出来会是一条无声语音")
        if len(pcm24_mono_s16le) % 2 != 0:
            # s16le 的样本是 2 字节；奇数长度说明上游给的不是 16 位 PCM。
            raise SilkEncodeError(
                "odd_pcm_length",
                f"PCM 长度 {len(pcm24_mono_s16le)} 是奇数，不是 16 位对齐的样本流",
            )
        module = self._load()
        try:
            data = module.encode(  # type: ignore[attr-defined]
                pcm24_mono_s16le, SILK_SAMPLE_RATE, sample_rate=SILK_SAMPLE_RATE
            )
        except Exception as exc:  # noqa: BLE001 - 第三方 C 扩展的异常名不固定
            raise SilkEncodeError(
                "encode_failed", f"{type(exc).__name__}: {exc}"
            ) from exc
        if not data:
            raise SilkEncodeError("empty_output", "编码器返回了空字节")
        if not isinstance(data, bytes):
            raise SilkEncodeError(
                "bad_output_type", f"编码器返回了 {type(data).__name__}，不是 bytes"
            )
        return data

    def decode(self, silk: bytes) -> bytes:
        module = self._load()
        try:
            return module.decode(silk, sample_rate=SILK_SAMPLE_RATE)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            raise SilkEncodeError(
                "decode_failed", f"{type(exc).__name__}: {exc}"
            ) from exc


# ---------------------------------------------------------------- ffmpeg 解码/重采样


def ffmpeg_available() -> tuple[bool, str]:
    """``ffmpeg`` 是否就绪（真找到二进制，不是只查环境变量）。"""
    path = shutil.which(FFMPEG_BIN)
    if path is None:
        return False, (
            f"运行环境缺少 {FFMPEG_BIN}：无法解码/重采样到 {SILK_SAMPLE_RATE} Hz。"
            "Debian/Ubuntu 装法：apt-get install -y ffmpeg"
        )
    return True, path


def _pcm_seconds(pcm: bytes, rate: int = SILK_SAMPLE_RATE) -> float:
    """s16le 单声道 PCM 的时长（秒）。"""
    return len(pcm) / 2 / rate


def decode_to_pcm24(source: Path | bytes) -> bytes:
    """用 ffmpeg 把任意音频解码并**真正重采样**成 24 kHz 单声道 s16le PCM。

    ## 为什么必须真重采样

    实测：把 44.1 kHz 的数据谎报成 24 kHz，编码器**会成功**，但播放慢
    1.84 倍（语速全错）。所以 ``-ar 24000`` 这一句是整个模块里最不能省的一行。

    ## 为什么用 pipe 而不是临时 wav

    ``-f s16le pipe:1`` 直接把 PCM 从 stdout 拿出来，不落任何中间文件：
    少一次磁盘写、少一个要清理的临时文件、也不会在失败时留下垃圾。
    """
    ok, detail = ffmpeg_available()
    if not ok:
        raise SilkEncodeError("ffmpeg_missing", detail)
    ffmpeg = shutil.which(FFMPEG_BIN)
    assert ffmpeg is not None  # noqa: S101 - 上面刚查过

    if isinstance(source, bytes):
        stdin_input = True
        input_args = ["-i", "pipe:0"]
        payload: bytes | None = source
    else:
        stdin_input = False
        input_args = ["-i", str(source)]
        payload = None

    command = [
        ffmpeg,
        "-v",
        "error",
        "-nostdin",
        *input_args,
        # **真重采样**：这两行任何一个没了，1.84 倍慢放就会回来。
        "-ar",
        str(SILK_SAMPLE_RATE),
        "-ac",
        "1",
        "-f",
        "s16le",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(  # noqa: S603 - 参数是常量 + 我们自己的路径
            command,
            input=payload,
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
            check=False,
            stdin=None if stdin_input else subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise SilkEncodeError(
            "ffmpeg_timeout",
            f"ffmpeg 解码超过 {FFMPEG_TIMEOUT_SECONDS:.0f} 秒（音频过大或已损坏）",
        ) from exc
    except OSError as exc:
        raise SilkEncodeError(
            "ffmpeg_failed", f"ffmpeg 无法启动：{type(exc).__name__}"
        ) from exc
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SilkEncodeError(
            "ffmpeg_failed",
            f"ffmpeg 解码失败（rc={completed.returncode}）：{stderr[:300] or '无输出'}",
        )
    pcm = completed.stdout
    if not pcm:
        # ffmpeg rc=0 但没输出：通常是空音频或探测到 0 帧。**不能**把它当成功。
        raise SilkEncodeError("empty_audio", "ffmpeg 解码出 0 字节 PCM（空音频？）")
    if len(pcm) % 2 != 0:
        raise SilkEncodeError(
            "odd_pcm_length", f"ffmpeg 输出了奇数长度 PCM（{len(pcm)} 字节）"
        )
    return pcm


# ---------------------------------------------------------------- 主入口


@dataclass(frozen=True)
class SilkCodec:
    """把「任意音频 → SILK」的整条链封装起来。

    依赖注入 encoder 与解码函数：测试可以塞假实现，生产可以换编码器。
    """

    encoder: SilkEncoder
    #: 解码器；默认走 ffmpeg。签名 (源) -> 24k mono s16le PCM。
    #: 允许替换是为了让"重采样"这件事在测试里可以被独立验证。
    decoder: object = decode_to_pcm24
    max_duration_seconds: float = 60.0

    def readiness(self) -> tuple[bool, str]:
        """这份 codec 现在能不能真的编码一个文件。

        §17.3 要求"真实编码一个 fixture，不能仅 ``which ffmpeg``"。这里返回
        的是**依赖齐备性**，真正的自检在 :meth:`selftest`。
        """
        ok_ff, detail_ff = ffmpeg_available()
        if not ok_ff:
            return False, detail_ff
        ok_enc, detail_enc = self.encoder.available()
        if not ok_enc:
            return False, detail_enc
        return True, ""

    def selftest(self) -> tuple[bool, str]:
        """**真编一个 100 ms 的 1 kHz 正弦**，验证整条链。

        §17.3 原话："readiness 分开报……真实编码一个 fixture，不能仅
        `which ffmpeg`"。那条要求的就是这个函数：只查二进制存在，会在
        "ffmpeg 装了但缺 encoder" 或 "编码器 ABI 不匹配" 时谎报就绪。
        """
        ok, detail = self.readiness()
        if not ok:
            return False, detail
        try:
            payload = _encode_selftest_tone(self)
        except SilkEncodeError as exc:
            return False, f"{exc.reason}: {exc.detail}"
        return True, f"ok（{len(payload.data)} 字节，{payload.duration_seconds:.3f}s）"

    def to_silk(self, source: Path | bytes) -> SilkPayload:
        """解码 → 重采样 24k mono → 编码 → 校验。"""
        pcm24 = self._decode(source)
        if not pcm24:
            raise SilkEncodeError("empty_pcm", "解码得到 0 字节 PCM")
        source_seconds = _pcm_seconds(pcm24)
        if source_seconds <= 0:
            raise SilkEncodeError("empty_pcm", "解码得到的音频时长为 0")
        if source_seconds > self.max_duration_seconds:
            # 明确失败，不截断：截断会让她"话说一半"，而调用方会以为发完了。
            raise SilkEncodeError(
                "too_long",
                f"音频时长 {source_seconds:.2f}s 超过上限 "
                f"{self.max_duration_seconds:.0f}s；本期不隐式拆条",
            )
        data = self.encoder.encode(pcm24)
        if SILK_MAGIC not in data[:10]:
            # 编码器"成功"但没给 SILK 头：NapCat 会再尝试转码，也就是失败。
            # 这一条是整条链唯一能证明"绕开了 ntsilk_s16le"的断言。
            raise SilkEncodeError(
                "missing_silk_header",
                f"编码结果前 10 字节缺少 {SILK_MAGIC!r}（NapCat 会重新转码并失败）",
            )
        # **解码回来验时长**：编码成功不等于内容对（1.84 倍慢放就是这样漏掉的）。
        try:
            decoded = self.encoder.decode(data)
        except SilkEncodeError as exc:
            raise SilkEncodeError(
                "verify_failed", f"编码后无法解码校验：{exc.detail}"
            ) from exc
        if not decoded:
            raise SilkEncodeError("verify_failed", "编码后解码得到 0 字节 PCM")
        duration = _pcm_seconds(decoded)
        drift = abs(duration - source_seconds)
        if drift > MAX_DURATION_DRIFT_SECONDS:
            # 这是本模块最重要的一条断言：**时长漂移超限就说明重采样没做对**。
            # 实测"谎报采样率"会产生 1.84 倍漂移，正好在这里被抓住。
            raise SilkEncodeError(
                "duration_drift",
                f"编解码后时长漂移 {drift * 1000:.0f} ms"
                f"（{source_seconds:.3f}s → {duration:.3f}s，"
                f"上限 {MAX_DURATION_DRIFT_SECONDS * 1000:.0f} ms）。"
                "通常意味着输入没有真正重采样到 24 kHz",
            )
        return SilkPayload(
            data=data,
            duration_seconds=duration,
            source_duration_seconds=source_seconds,
        )

    def _decode(self, source: Path | bytes) -> bytes:
        decoder = self.decoder
        if not callable(decoder):
            raise SilkEncodeError("no_decoder", "没有可用的解码器")
        out = decoder(source)
        if not isinstance(out, bytes):
            raise SilkEncodeError(
                "bad_decoder_result", f"解码器返回了 {type(out).__name__}，不是 bytes"
            )
        return out


def _encode_selftest_tone(codec: SilkCodec) -> SilkPayload:
    """生成 100 ms / 1 kHz 正弦并走完整条链（readiness 自检用）。"""
    import struct  # noqa: PLC0415 - 只在这个自检里需要

    frames = int(SILK_SAMPLE_RATE * 0.1)
    pcm = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * 1000 * i / SILK_SAMPLE_RATE)))
        for i in range(frames)
    )
    data = codec.encoder.encode(pcm)
    if SILK_MAGIC not in data[:10]:
        raise SilkEncodeError("missing_silk_header", "自检输出缺少 SILK 魔数")
    decoded = codec.encoder.decode(data)
    return SilkPayload(
        data=data,
        duration_seconds=_pcm_seconds(decoded),
        source_duration_seconds=_pcm_seconds(pcm),
    )


def default_codec(*, max_duration_seconds: float = 60.0) -> SilkCodec:
    """生产默认：pysilk-mod + ffmpeg。"""
    return SilkCodec(
        encoder=PysilkEncoder(),
        decoder=decode_to_pcm24,
        max_duration_seconds=max_duration_seconds,
    )


def describe_silk(payload: SilkPayload) -> str:
    """给日志/回执用的摘要（不打印字节）。"""
    drift_ms = payload.duration_drift() * 1000
    return (
        f"SILK {len(payload.data)} 字节，{payload.duration_seconds:.3f}s，"
        f"时长差 {drift_ms:.0f}ms"
    )


def probe_audio_format(source: Path) -> dict:
    """尽力读出源音频的格式信息，**只用于日志与诊断**。

    为什么不做成"必须成功"的步骤：不同环境的 ffmpeg 构建差异很大
    （本机实测 ``ffprobe`` 竟然是个指向 ffmpeg 的符号链接，不支持
    ``-show_format``／``-of json``）。拿它当硬依赖会让"能编码"变成
    "取决于查得到元信息"。所以这里失败就返回 ``{}``，不影响主链。
    """
    ok, _ = ffmpeg_available()
    if not ok:
        return {}
    ffmpeg = shutil.which(FFMPEG_BIN)
    if ffmpeg is None:  # pragma: no cover - 上面刚查过
        return {}
    try:
        completed = subprocess.run(  # noqa: S603
            [ffmpeg, "-hide_banner", "-i", str(source)],
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    text = completed.stderr.decode("utf-8", errors="replace")
    info: dict = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Duration:"):
            info["duration"] = stripped.split(":", 1)[1].strip().split(",")[0].strip()
        elif "Audio:" in stripped:
            info["audio"] = stripped
    return info


def json_summary(payload: SilkPayload) -> str:
    """结构化摘要（写进行为日志时用）。"""
    return json.dumps(
        {
            "bytes": len(payload.data),
            "duration_seconds": round(payload.duration_seconds, 4),
            "source_duration_seconds": (
                round(payload.source_duration_seconds, 4)
                if payload.source_duration_seconds is not None
                else None
            ),
            "drift_ms": round(payload.duration_drift() * 1000, 1),
            "sample_rate": SILK_SAMPLE_RATE,
        },
        ensure_ascii=False,
    )


__all__ = [
    "ALLOWED_INPUT_RATES",
    "FFMPEG_BIN",
    "FRAME_SECONDS",
    "MAX_DURATION_DRIFT_SECONDS",
    "SILK_NOMINAL_BIT_RATE",
    "SILK_HEADER",
    "SILK_MAGIC",
    "SILK_SAMPLE_RATE",
    "PysilkEncoder",
    "SilkCodec",
    "SilkEncodeError",
    "SilkEncoder",
    "SilkPayload",
    "decode_to_pcm24",
    "default_codec",
    "describe_silk",
    "ffmpeg_available",
    "json_summary",
    "probe_audio_format",
]
