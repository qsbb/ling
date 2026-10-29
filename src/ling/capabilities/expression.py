"""Expression：文本、TTS 与受限角色动作的输出端口（W2：已实现未测试）。

硬规则：
- 文本永远可交付；语音/动作必须等真实回执才能说"已经说了/做了"。
- 平台能力不足时降级（语音→文本），并明确告知，不假装已发送。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from ling.contracts.v1 import new_id


class ExpressionKind(StrEnum):
    text = "text"
    speech = "speech"
    avatar_action = "avatar_action"


@dataclass(frozen=True)
class ExpressionChunk:
    """按平台能力切分后的一个交付单元。"""

    chunk_id: str
    kind: ExpressionKind
    text: str
    index: int
    total: int


@dataclass(frozen=True)
class ExpressionPlan:
    """一次表达的完整计划。``degraded`` 说明做了降级。"""

    plan_id: str
    chunks: list[ExpressionChunk]
    degraded: bool
    degrade_reason: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def full_text(self) -> str:
        return "".join(c.text for c in self.chunks)


class ExpressionService:
    """把模型输出转成平台可交付的形式。不做权限判断（那是 PolicyGate）。"""

    def __init__(self, *, max_text_length: int = 4096, supports_speech: bool = False) -> None:
        self._max_text_length = max(64, max_text_length)
        self._supports_speech = supports_speech

    def plan_text(self, text: str) -> ExpressionPlan:
        chunks: list[ExpressionChunk] = []
        pieces = self._split(text)
        for index, piece in enumerate(pieces):
            chunks.append(
                ExpressionChunk(
                    chunk_id=new_id("chunk"),
                    kind=ExpressionKind.text,
                    text=piece,
                    index=index,
                    total=len(pieces),
                )
            )
        return ExpressionPlan(plan_id=new_id("plan"), chunks=chunks, degraded=False)

    def plan_speech(
        self, text: str, *, fallback_text: bool = True
    ) -> ExpressionPlan:
        """计划语音输出。平台不支持语音时降级为文本并说明原因。"""
        if not self._supports_speech:
            if not fallback_text:
                return ExpressionPlan(
                    plan_id=new_id("plan"),
                    chunks=[],
                    degraded=True,
                    degrade_reason="当前渠道不支持语音，且未允许文本降级",
                )
            plan = self.plan_text(text)
            return ExpressionPlan(
                plan_id=plan.plan_id,
                chunks=plan.chunks,
                degraded=True,
                degrade_reason="当前渠道不支持语音，已降级为文本",
            )
        return ExpressionPlan(
            plan_id=new_id("plan"),
            chunks=[
                ExpressionChunk(
                    chunk_id=new_id("chunk"),
                    kind=ExpressionKind.speech,
                    text=text[:2000],
                    index=0,
                    total=1,
                )
            ],
            degraded=False,
        )

    def _split(self, text: str) -> list[str]:
        if len(text) <= self._max_text_length:
            return [text]
        out: list[str] = []
        current = ""
        for sentence in _sentences(text):
            if len(current) + len(sentence) <= self._max_text_length:
                current += sentence
            else:
                if current:
                    out.append(current)
                while len(sentence) > self._max_text_length:
                    out.append(sentence[: self._max_text_length])
                    sentence = sentence[self._max_text_length :]
                current = sentence
        if current:
            out.append(current)
        return out


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    buffer = ""
    for char in text:
        buffer += char
        if char in "。！？!?；;\n":
            out.append(buffer)
            buffer = ""
    if buffer:
        out.append(buffer)
    return out


__all__ = [
    "ExpressionChunk",
    "ExpressionKind",
    "ExpressionPlan",
    "ExpressionService",
]
