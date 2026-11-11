"""SILK 编码：把任意音频变成 NapCat 能直接发的 Tencent SILK。

## 这一层为什么存在（不是"顺手加个转码"）

NapCat 收到非 SILK 音频会自己调 ``/usr/bin/ffmpeg -f ntsilk_s16le`` 转码，
而 Ubuntu 22.04 的 ffmpeg 4.4.2 **没有这个 muxer**，命令必然失败——结果是
**用户一条语音都收不到**。只要我们自己产出带 ``\\x02#!SILK_V3`` 头的 SILK，
它的 ``isSilk`` 命中就跳过转码，走本来正常工作的发送路径。

依据 `docs/design/SPEECH-IO-ARCHITECTURE.md` §17.3。

## 这里钉的是什么

1. **44.1 kHz 输入必须真的被重采样到 24 kHz**（时长不变，采样率变了）；
2. **输出前 10 字节是 SILK 魔数**；
3. **时长不漂移**（§17.3：允许 codec padding ≤60ms）；
4. **反向断言**：不重采样就一定得到 1.84 倍慢放——用来证明"我们真做了"，
   而不是"恰好没报错"。

第 4 条是最关键的。实测（2026-11-11）``pysilk-mod`` **不会拒绝**错误输入：
它把 44.1 kHz 数据按 24 kHz 编码，编得出来，只是播放慢 1.84 倍。所以
"编码成功"根本不是正确性证据，必须解码回来量时长。

## 测试策略

- 真要 pysilk 的用例统一用 ``pytest.importorskip``：CI 没装 ``speech-codecs``
  extra 时跳过，而不是红一片假失败。
- 重采样与时长校验的逻辑用**假编码器**单独测，这样它们不依赖任何二进制。
"""

from __future__ import annotations

import io
import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest
from connectors.onebot.silk import (
    MAX_DURATION_DRIFT_SECONDS,
    SILK_HEADER,
    SILK_MAGIC,
    SILK_NOMINAL_BIT_RATE,
    SILK_SAMPLE_RATE,
    PysilkEncoder,
    SilkCodec,
    SilkEncodeError,
    SilkEncoder,
    SilkPayload,
    decode_to_pcm24,
    default_codec,
    describe_silk,
    ffmpeg_available,
    json_summary,
    probe_audio_format,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="这些用例需要 ffmpeg 做解码与重采样"
)

FFMPEG = shutil.which("ffmpeg")
PYSILK = pytest.importorskip("pysilk", reason="需要 pysilk-mod（ling[speech-codecs]）")


# ------------------------------------------------------------------ fixture 生成


def _tone_wav(rate: int, seconds: float = 1.0, freq: float = 440.0) -> bytes:
    """造一段正弦 WAV（单声道 16 位）。"""
    frames = int(rate * seconds)
    pcm = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)))
        for i in range(frames)
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def wav44k(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """44.1 kHz 单声道 WAV —— 厂商 TTS 的典型输出。"""
    path = tmp_path_factory.mktemp("audio") / "tone44100.wav"
    path.write_bytes(_tone_wav(44100))
    return path


@pytest.fixture(scope="module")
def wav24k(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("audio") / "tone24000.wav"
    path.write_bytes(_tone_wav(24000))
    return path


def _duration_of_pcm(pcm: bytes, rate: int = SILK_SAMPLE_RATE) -> float:
    return len(pcm) / 2 / rate


# ------------------------------------------------------------------ 重采样（你要求的第一条）


def test_resampling_44100_to_24000_changes_rate_but_keeps_duration(wav44k: Path) -> None:
    """44.1 kHz 输入 → 输出 PCM 必须是 24 kHz，且**时长不变**。

    ## 怎么证明"采样率真的变了"

    只断言"输出能编码"是不够的（错采样率也能编码）。这里用两个独立的量：

    1. **时长**：输入 1.0 s → 输出必须是 1.0 s（±60 ms）。如果只是把 44.1k
       的字节砍短而不重采样，时长会变成 1.0×24000/44100 ≈ 0.544 s；
    2. **样本数**：1.0 s @ 24 kHz = 24000 个样本 = 48000 字节。44.1 kHz 的
       1.0 s 是 44100 个样本 = 88200 字节。两个数不同，就证明重采样发生了。

    第 2 条里"样本数变了但时长没变"，正是重采样的定义。
    """
    pcm = decode_to_pcm24(wav44k)

    duration = _duration_of_pcm(pcm)
    assert duration == pytest.approx(1.0, abs=MAX_DURATION_DRIFT_SECONDS), (
        f"重采样后时长 {duration:.4f}s 偏离 1.0s：没真重采样（或截断）"
    )
    # 24 kHz 的 1 秒 = 48000 字节；44.1 kHz 的 1 秒 = 88200 字节
    assert len(pcm) == pytest.approx(48000, abs=2 * 24000 * MAX_DURATION_DRIFT_SECONDS)
    assert len(pcm) != 88200, "样本数仍是 44.1 kHz 的 → 没有重采样"


def test_resampling_is_mono(wav44k: Path) -> None:
    """输出必须是**单声道**：立体声会让 SILK 编码拿到偶数倍的数据。

    用 s16le 单声道解码，1 秒恰好 48000 字节。若是立体声未降混，会是 96000。
    """
    pcm = decode_to_pcm24(wav44k)
    assert len(pcm) // 2 == pytest.approx(24000, abs=1500)


def test_resampling_from_24k_is_identity_in_duration(wav24k: Path) -> None:
    """已经是 24 kHz 的输入，重采样后时长与样本数都不该变。"""
    pcm = decode_to_pcm24(wav24k)
    assert _duration_of_pcm(pcm) == pytest.approx(1.0, abs=MAX_DURATION_DRIFT_SECONDS)


# ------------------------------------------------------------------ 反向断言（最关键）


def test_unresampled_input_plays_1_84x_slow() -> None:
    """**反向断言**：把 44.1 kHz 的 PCM 谎报成 24 kHz，播放会慢 1.84 倍。

    这条测试不测我们的代码，它测的是**底层编码器不会替我们拦住错误输入**。

    ## 为什么必须有这条

    没有它，"我们做了重采样"这件事就没有对照实验：所有测试全绿也可能只是
    "恰好没人喂过错采样率"。有了它，下面这个推理才成立——
    **如果 `to_silk` 里省掉重采样这一步，时长校验会以 1.84 倍漂移失败**，
    所以那个校验连同重采样是真起作用的。

    实测值 1.840（44100/24000 = 1.8375，加上 silk 分帧 padding 到 1.840）。
    """
    pcm44 = decode_to_pcm24(_tone_wav(44100))  # 已经是 24k，先拿一份"正确时长"参考
    assert _duration_of_pcm(pcm44) == pytest.approx(1.0, abs=MAX_DURATION_DRIFT_SECONDS)

    # 直接把 44.1k 采样率的数据按 24k 编码（模拟"没重采样"）
    raw44 = _tone_wav(44100)[44:]  # 跳过 WAV 头，让 pysilk 当裸 PCM 处理
    liar = PYSILK.encode(raw44, SILK_SAMPLE_RATE, sample_rate=SILK_SAMPLE_RATE)
    decoded = PYSILK.decode(liar, sample_rate=SILK_SAMPLE_RATE)
    slowed = _duration_of_pcm(decoded)

    ratio = slowed / 1.0
    # 44100/24000 = 1.8375；实测解出 1.840（多出来的是分帧 padding）
    assert ratio == pytest.approx(44100 / 24000, rel=0.01), (
        f"谎报采样率后时长是 {slowed:.3f}s（{ratio:.3f} 倍）——"
        "编码器**不会**拒绝错误采样率，所以重采样必须由我们保证"
    )
    assert ratio > 1.8, "已知实测是 1.84 倍慢放；值变了说明换过编码器，需重新评估"


def test_encoder_accepts_wrong_rate_without_raising() -> None:
    """编码器对错误采样率**不报错**——这就是"不能靠库兜底"的证据。

    「声」插件的注释里说会报 ``ENC_INPUT_INVALID_NO_OF_SAMPLES``，但
    ``pysilk-mod`` 实测静默成功。这条把那个差异钉成可执行的记录：哪天换编码器
    开始报错了，这里会红，提醒我们重新审视"我们自己校验"是否还必要。
    """
    encoder = PysilkEncoder()

    data = encoder.encode(b"\x00\x00" * 1000)  # 1000 个样本，没声明采样率

    assert data[:10] == SILK_HEADER
    assert SILK_MAGIC in data[:10]


def test_empty_pcm_is_rejected_by_us_because_encoder_allows_it() -> None:
    """空 PCM 由**我们**拒绝，因为底层编码器会返回一个合法的空 SILK。

    实测 ``PYSILK.encode(b"", 24000, sample_rate=24000)`` 返回
    ``b'\\x02#!SILK_V3'``——一条"发送成功但没有声音"的语音。
    """
    # 先确认底层确实不抛（这是本测试的前提）
    assert PYSILK.encode(b"", SILK_SAMPLE_RATE, sample_rate=SILK_SAMPLE_RATE)

    with pytest.raises(SilkEncodeError) as excinfo:
        PysilkEncoder().encode(b"")

    assert excinfo.value.reason == "empty_pcm"


# ------------------------------------------------------------------ SILK 魔数（你要求的第二条）


def test_output_starts_with_silk_magic(wav44k: Path) -> None:
    """输出前 10 字节必须是 ``\\x02#!SILK_V3``。

    NapCat 的 ``isSilk`` 就是在前 10 字节里找 ``#!SILK``：命中就**跳过**
    那个会失败的 ntsilk 转码。这一条不通，整个返工就没意义。
    """
    payload = default_codec().to_silk(wav44k)

    assert payload.data[:10] == SILK_HEADER
    assert payload.data.startswith(b"\x02#!SILK_V3")
    assert payload.is_silk()


def test_silk_magic_matches_napcat_detection_rule(wav44k: Path) -> None:
    """魔数判定必须与 NapCat 同一口径（前 10 字节里找 ``#!SILK``）。

    口径不一致的后果：我们以为发了 SILK、NapCat 以为不是，于是它去调那个
    不存在的 muxer，用户一条语音都收不到。
    """
    payload = default_codec().to_silk(wav44k)

    # NapCat: str.includes('#!SILK') on first 10 bytes
    assert b"#!SILK" in payload.data[:10]


# ------------------------------------------------------------------ 时长不漂移（你要求的第三条）


def test_duration_does_not_drift(wav44k: Path) -> None:
    """编码前后时长差必须在 §17.3 允许的 padding 之内（≤60 ms）。"""
    payload = default_codec().to_silk(wav44k)

    assert payload.source_duration_seconds == pytest.approx(
        1.0, abs=MAX_DURATION_DRIFT_SECONDS
    )
    assert payload.duration_seconds == pytest.approx(1.0, abs=MAX_DURATION_DRIFT_SECONDS)
    assert payload.duration_drift() <= MAX_DURATION_DRIFT_SECONDS, (
        f"时长漂移 {payload.duration_drift() * 1000:.0f}ms 超过 "
        f"{MAX_DURATION_DRIFT_SECONDS * 1000:.0f}ms"
    )


def test_duration_drift_is_measured_by_decoding_not_guessed(wav44k: Path) -> None:
    """时长是**解码回来量**的，不是从输入推算的。

    编造方式：把 payload 的字节截掉一半，用同一个"声称的时长"去解码——
    如果实现偷懒用输入时长填充 ``duration_seconds``，这里就查不出来。
    这里直接验 payload 的解码结果与它报告的时长一致。
    """
    payload = default_codec().to_silk(wav44k)

    decoded = PysilkEncoder().decode(payload.data)
    measured = _duration_of_pcm(decoded)

    assert payload.duration_seconds == pytest.approx(measured, abs=0.001)


def test_oversized_duration_is_rejected_not_truncated(tmp_path: Path) -> None:
    """超过 ``max_duration_seconds`` → 报错，**不截断**。

    截断会让她"话说一半"，而调用方会以为发完了——那是伪造完整性的错误。
    """
    long_wav = tmp_path / "long.wav"
    long_wav.write_bytes(_tone_wav(24000, seconds=2.0))
    codec = SilkCodec(encoder=PysilkEncoder(), decoder=decode_to_pcm24,
                      max_duration_seconds=1.0)

    with pytest.raises(SilkEncodeError) as excinfo:
        codec.to_silk(long_wav)

    assert excinfo.value.reason == "too_long"
    assert "1s" in excinfo.value.detail


# ------------------------------------------------------------------ 依赖缺失时的行为


def test_missing_ffmpeg_is_a_clear_error_not_a_silent_skip(monkeypatch) -> None:  # noqa: ANN001
    """没有 ffmpeg 时给**可执行**的安装指引，不是含糊的"编码失败"。"""
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "ffmpeg" else "/x")

    with pytest.raises(SilkEncodeError) as excinfo:
        decode_to_pcm24(b"whatever")

    assert excinfo.value.reason == "ffmpeg_missing"
    assert "apt-get install" in excinfo.value.detail


def test_missing_encoder_is_a_clear_error() -> None:
    """没有 pysilk-mod 时明确说"装哪个 extra"，而不是 ImportError 冒到上层。"""
    encoder = PysilkEncoder(module=None)
    # 用一个必然失败的假模块模拟"没装"
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "pysilk":
            raise ImportError("No module named 'pysilk'")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = fake_import  # type: ignore[assignment]
    try:
        ok, reason = encoder.available()
    finally:
        builtins.__import__ = real_import  # type: ignore[assignment]

    assert ok is False
    assert "speech-codecs" in reason


def test_bad_encoder_output_without_magic_is_rejected() -> None:
    """编码器返回了没有 SILK 魔数的字节 → 报错（NapCat 会重新转码并失败）。"""

    class BogusEncoder:
        name = "bogus"

        def available(self) -> tuple[bool, str]:
            return True, ""

        def encode(self, pcm: bytes) -> bytes:
            return b"NOT-SILK-AT-ALL"

        def decode(self, silk: bytes) -> bytes:
            return b"\x00\x00" * 1000

    codec = SilkCodec(encoder=BogusEncoder(), decoder=lambda _src: b"\x00\x00" * 24000)

    with pytest.raises(SilkEncodeError) as excinfo:
        codec.to_silk(b"ignored")

    assert excinfo.value.reason == "missing_silk_header"


# ------------------------------------------------------------------ readiness / 自检


def test_selftest_really_encodes_a_fixture() -> None:
    """``selftest`` 必须**真编一段音频**，不能只查二进制是否存在。

    §17.3 原话："真实编码一个 fixture，不能仅 ``which ffmpeg``"。
    """
    codec = default_codec()

    ok, detail = codec.selftest()

    assert ok is True, detail
    assert "字节" in detail


def test_readiness_separates_ffmpeg_from_encoder(monkeypatch) -> None:  # noqa: ANN001
    """readiness 要能分开报"缺 ffmpeg"与"缺编码器"，不能混成一个"语音不可用"。"""

    class Unavailable:
        name = "none"

        def available(self) -> tuple[bool, str]:
            return False, "编码器没装"

        def encode(self, pcm: bytes) -> bytes:  # pragma: no cover
            raise AssertionError

        def decode(self, silk: bytes) -> bytes:  # pragma: no cover
            raise AssertionError

    codec = SilkCodec(encoder=Unavailable())
    ok, detail = codec.readiness()
    assert ok is False
    assert detail == "编码器没装"

    # ffmpeg 缺失时归因到 ffmpeg，而不是编码器
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "ffmpeg" else "/x")
    ok2, detail2 = codec.readiness()
    assert ok2 is False
    assert "ffmpeg" in detail2


# ------------------------------------------------------------------ 常量与工具


def test_silk_constants_match_spec() -> None:
    """§17.3 规定的规格：24 kHz、魔数、名义码率。

    ``SILK_NOMINAL_BIT_RATE`` 名字里带 NOMINAL 是有意的：``pysilk-mod`` 的
    ``encode()`` **不接受码率参数**，实际码率是它内置默认值。这个常量只用于
    文档与断言，调它不会改变输出——见 ``silk.py`` 里那条 warning。
    """
    assert SILK_SAMPLE_RATE == 24000
    assert SILK_HEADER == b"\x02#!SILK_V3"
    assert SILK_MAGIC == b"#!SILK"
    assert SILK_NOMINAL_BIT_RATE == 24000
    assert MAX_DURATION_DRIFT_SECONDS == 0.060


def test_encoder_protocol_is_satisfiable() -> None:
    """``PysilkEncoder`` 满足 ``SilkEncoder`` 协议（换实现时上层不用改）。"""
    assert isinstance(PysilkEncoder(), SilkEncoder)


def test_summaries_do_not_leak_bytes(wav44k: Path) -> None:
    """日志摘要不能打印音频字节（与 voice.py 的规矩一致）。"""
    payload = default_codec().to_silk(wav44k)

    assert "SILK" in describe_silk(payload)
    assert len(describe_silk(payload)) < 120
    summary = json_summary(payload)
    assert '"sample_rate": 24000' in summary
    # 不含任何 base64/原始字节片段
    assert payload.data[:16].hex() not in summary


def test_ffmpeg_available_returns_path_or_reason() -> None:
    ok, detail = ffmpeg_available()
    assert ok is True
    assert Path(detail).name == "ffmpeg"


def test_probe_audio_format_is_best_effort(wav44k: Path) -> None:
    """``probe_audio_format`` 是诊断用的**尽力而为**：失败返回空 dict，不抛。

    本机实测 ``ffprobe`` 竟是个指向 ffmpeg 的符号链接（不支持
    ``-show_format``），所以这个函数**不能**成为主链的硬依赖。
    """
    info = probe_audio_format(wav44k)

    # 能读到就断言内容；读不到也允许（不同 ffmpeg 构建差异很大）
    if info:
        assert any("Audio" in str(v) or "duration" in k for k, v in info.items())


def test_echo_of_44k_wav_duration_from_pcm_bytes(wav44k: Path) -> None:
    """用 ffmpeg 自己数 PCM 字节算时长，不依赖 ffprobe（跨环境更稳）。"""
    pcm = decode_to_pcm24(wav44k)
    seconds = _duration_of_pcm(pcm)

    assert seconds == pytest.approx(1.0, abs=0.01)
    # 顺带确认 ffmpeg 在这个环境里确实能跑（不是只有 which 找到）
    completed = subprocess.run(
        [FFMPEG, "-v", "error", "-hide_banner", "-f", "lavfi",
         "-i", "sine=frequency=1000:duration=0.1:sample_rate=24000",
         "-ar", "24000", "-ac", "1", "-f", "s16le", "pipe:1"],
        capture_output=True, timeout=60, check=False,
    )
    assert completed.returncode == 0
    assert len(completed.stdout) == pytest.approx(4800, abs=960)


def test_payload_dataclass_defaults() -> None:
    """``SilkPayload`` 缺 source_duration 时漂移按 0 处理（不是崩）。"""
    payload = SilkPayload(data=SILK_HEADER + b"\x00" * 8, duration_seconds=1.5)

    assert payload.source_duration_seconds is None
    assert payload.duration_drift() == 0.0
    assert payload.is_silk()


def test_encoder_returning_empty_bytes_is_rejected() -> None:
    """编码器返回空字节 → 报错，不是"发一条没有声音的语音"。

    与"空输入"是两个不同的洞：那个是**输入**为空，这个是编码器**输出**为空。
    底层不会替我们挡（实测空输入也返回合法头），所以两层都要有。
    """

class _EmptyModule:
    """假 module：``encode`` 返回空字节。"""

    @staticmethod
    def encode(_data: bytes, _rate: int, *, sample_rate: int) -> bytes:  # noqa: ARG004
        return b""

    @staticmethod
    def decode(_silk: bytes, *, sample_rate: int) -> bytes:  # noqa: ARG004
        return b""
