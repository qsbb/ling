"""语音 provider 适配器（ASR/TTS）：按**工作流**建模，不按单一 model_id。

设计文档 §8.1 明确：语音输出必须按工作流建模，不能只换 model_id。
因此这里定义：

- ``AsrProvider``：流式 partial + 整段 final 两种调用
- ``TtsProvider``：合成 + 取消 + 播放回执期望

**状态：已实现未测试。** 没有语音凭据与设备，从未联调。
本模块不伪造音频，只给出协议与失败分类。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class SpeechErrorKind(StrEnum):
    unavailable = "provider_unavailable"
    timeout = "provider_timeout"
    error = "provider_error"
    missing_secret = "secret_missing"
    unsupported = "unsupported_capability"


class SpeechError(Exception):
    def __init__(self, kind: SpeechErrorKind, message: str, *, retriable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retriable = retriable


@dataclass(frozen=True)
class AudioRef:
    """受限媒体引用。原始音频不进领域对象，只存引用与哈希。"""

    media_ref: str
    mime_type: str
    duration_ms: int
    sha256: str


@dataclass(frozen=True)
class AsrPartial:
    text: str
    is_final: bool
    confidence: float | None = None


@dataclass(frozen=True)
class TtsChunk:
    audio: bytes
    sequence: int
    is_last: bool


@dataclass
class TtsOptions:
    voice: str | None = None
    speed: float = 1.0
    format: str = "wav"  # wav | mp3 | opus
    instructions: str | None = None
    extra: dict = field(default_factory=dict)


@runtime_checkable
class AsrProvider(Protocol):
    """语音识别。

    流式接口只产出 partial 用于字幕；``final=True`` 的结果才允许进入主链。
    """

    provider_id: str

    def stream(self, audio: AudioRef) -> AsyncIterator[AsrPartial]:
        ...

    async def transcribe(self, audio: AudioRef) -> AsrPartial:
        """整段识别。流式失败时可回退到这里（设计文档 §8.1）。"""
        ...


@runtime_checkable
class TtsProvider(Protocol):
    provider_id: str

    def synthesize(self, text: str, options: TtsOptions) -> AsyncIterator[TtsChunk]:
        ...


class UnconfiguredSpeechProvider:
    """未配置语音供应商时的占位实现。

    所有调用都明确失败并给出可执行原因，**不伪造音频**，
    也不假装"已经说给你听"。
    """

    provider_id = "speech-unconfigured"

    def __init__(self, reason: str = "尚未配置语音供应商") -> None:
        self._reason = reason

    async def stream(self, audio: AudioRef) -> AsyncIterator[AsrPartial]:
        raise SpeechError(SpeechErrorKind.missing_secret, self._reason, retriable=False)
        yield AsrPartial(text="", is_final=False)  # pragma: no cover

    async def transcribe(self, audio: AudioRef) -> AsrPartial:
        raise SpeechError(SpeechErrorKind.missing_secret, self._reason, retriable=False)

    async def synthesize(self, text: str, options: TtsOptions) -> AsyncIterator[TtsChunk]:
        raise SpeechError(SpeechErrorKind.missing_secret, self._reason, retriable=False)
        yield TtsChunk(audio=b"", sequence=0, is_last=True)  # pragma: no cover


@dataclass(frozen=True)
class SpeechProfile:
    """一个语音职责的绑定（asr / tts / realtime）。"""

    profile: str
    provider_id: str
    model_id: str
    streaming: bool
    languages: list[str] = field(default_factory=lambda: ["zh-CN"])
    max_audio_seconds: int = 120


# 已知职责与必需能力。缺能力就明确报 unsupported，不让模型猜。
SPEECH_PROFILE_REQUIREMENTS: dict[str, dict[str, bool]] = {
    "speech.asr": {"streaming": False, "produces_final": True},
    "speech.tts": {"streaming": True, "cancellable": True},
    "realtime.voice": {"streaming": True, "barge_in": True, "reconnect": True},
}


def validate_speech_profile(profile: SpeechProfile, capabilities: dict[str, bool]) -> list[str]:
    """校验一个语音职责是否满足必需能力。返回缺失项（空表示通过）。"""
    required = SPEECH_PROFILE_REQUIREMENTS.get(profile.profile)
    if required is None:
        return [f"未知语音职责: {profile.profile}"]
    missing = [name for name, needed in required.items() if needed and not capabilities.get(name)]
    if profile.profile == "realtime.voice" and not capabilities.get("event_mirror"):
        # 实时 provider 的 session 只是媒体会话，必须能把事件镜像回 ling
        missing.append("event_mirror")
    return missing


__all__ = [
    "SPEECH_PROFILE_REQUIREMENTS",
    "AsrPartial",
    "AsrProvider",
    "AudioRef",
    "SpeechError",
    "SpeechErrorKind",
    "SpeechProfile",
    "TtsChunk",
    "TtsOptions",
    "TtsProvider",
    "UnconfiguredSpeechProvider",
    "validate_speech_profile",
]
