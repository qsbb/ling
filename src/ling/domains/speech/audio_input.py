"""Decode bounded input audio to a real mono PCM16 WAV."""
from __future__ import annotations

import asyncio
import io
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

from ling.ports.speech import AudioInput, AudioSpec, SpeechError, SpeechErrorKind

MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_INPUT_SECONDS = 120


@dataclass(frozen=True)
class PreparedAudio:
    audio: AudioInput
    duration_seconds: float
    silent: bool


def _decode(data: bytes) -> PreparedAudio:
    if not data or len(data) > MAX_INPUT_BYTES:
        raise SpeechError(SpeechErrorKind.invalid_request, "音频为空或超过 10 MiB 限制")
    if data.startswith((b"\x02#!SILK", b"#!SILK")):
        try:
            import pysilk
            pcm = pysilk.decode(data, 16000)
        except Exception as exc:
            raise SpeechError(SpeechErrorKind.invalid_request, "当前环境无法解码这段 SILK 语音") from exc
        if len(pcm) > 16000 * 2 * MAX_INPUT_SECONDS:
            raise SpeechError(SpeechErrorKind.invalid_request, "语音超过 120 秒限制")
        stream = io.BytesIO()
        with wave.open(stream, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(pcm)
        data = stream.getvalue()
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SpeechError(SpeechErrorKind.not_configured, "运行环境缺少 ffmpeg，无法读取语音")
    with tempfile.TemporaryDirectory(prefix="ling-asr-") as tmp:
        source = Path(tmp) / "input.audio"
        target = Path(tmp) / "decoded.wav"
        source.write_bytes(data)
        try:
            subprocess.run(
                [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-protocol_whitelist", "file,pipe", "-i", str(source),
                 "-t", str(MAX_INPUT_SECONDS + 1), "-vn", "-ac", "1", "-ar", "16000",
                 "-c:a", "pcm_s16le", str(target)],
                check=True, capture_output=True, timeout=20,
            )
            decoded = target.read_bytes()
            with wave.open(io.BytesIO(decoded), "rb") as wav:
                frames = wav.readframes(wav.getnframes())
                duration = wav.getnframes() / wav.getframerate()
        except (subprocess.SubprocessError, OSError, wave.Error) as exc:
            raise SpeechError(SpeechErrorKind.invalid_request, "音频损坏或当前环境无法解码该格式") from exc
    if duration <= 0 or duration > MAX_INPUT_SECONDS:
        raise SpeechError(SpeechErrorKind.invalid_request, "语音为空或超过 120 秒限制")
    return PreparedAudio(
        AudioInput(decoded, "audio.wav", AudioSpec("audio/wav", "wav", "pcm_s16le", 16000, 1)),
        duration, not any(frames),
    )


async def prepare_audio(data: bytes) -> PreparedAudio:
    return await asyncio.to_thread(_decode, data)
