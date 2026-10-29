"""语音流水线：VAD/ASR → 对话 → TTS，各环节独立选模型。

唯一写入方（SpeechSegment）。硬规则（设计文档 §5）：
- ASR **partial 只用于字幕和诊断**，不写记忆、不触发工具。
- 只有 **final** 才成为正式输入进入认知管线。
- TTS 播放必须有真实回执；没有回执不能说"已经说给你听了"。
- 用户插话时取消旧轮；迟到音频带 turn_id/sequence 被丢弃。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import SpeechSegment, utcnow
from ling.infra.security import new_id


@dataclass(frozen=True)
class SegmentView:
    segment_id: str
    window_id: str
    turn_id: str | None
    kind: str
    text: str
    model_label: str | None
    created_at: datetime


@dataclass(frozen=True)
class PlaybackReceipt:
    """TTS 播放回执。只有客户端回报 played 才算真的播放。"""

    playback_id: str
    turn_id: str
    played_ms: int
    completed: bool
    observed_at: datetime


class SpeechService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_partial(
        self,
        *,
        window_id: str,
        text: str,
        turn_id: str | None = None,
        model_label: str | None = None,
    ) -> SegmentView:
        """记录 partial：仅字幕/诊断，不进入主链。"""
        return await self._add(window_id, text, kind="partial", turn_id=turn_id,
                               model_label=model_label)

    async def record_final(
        self,
        *,
        window_id: str,
        text: str,
        turn_id: str | None = None,
        model_label: str | None = None,
        audio_ref: str | None = None,
    ) -> SegmentView:
        """记录 final：这是正式输入。空 final 不入主链。"""
        cleaned = text.strip()
        if not cleaned:
            raise ValueError("ASR final 为空，不能进入主链")
        view = await self._add(
            window_id, cleaned, kind="final", turn_id=turn_id, model_label=model_label
        )
        if audio_ref:
            row = await self._session.get(SpeechSegment, view.segment_id)
            if row is not None:
                row.audio_ref = audio_ref
                await self._session.flush()
        return view

    async def _add(
        self,
        window_id: str,
        text: str,
        *,
        kind: str,
        turn_id: str | None,
        model_label: str | None,
    ) -> SegmentView:
        row = SpeechSegment(
            id=new_id("seg"),
            window_id=window_id,
            turn_id=turn_id,
            kind=kind,
            text=text[:8000],
            model_label=model_label,
        )
        self._session.add(row)
        await self._session.flush()
        return self._view(row)

    async def latest_final(self, window_id: str) -> SegmentView | None:
        row = (
            await self._session.execute(
                select(SpeechSegment)
                .where(SpeechSegment.window_id == window_id, SpeechSegment.kind == "final")
                .order_by(SpeechSegment.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return self._view(row) if row else None

    async def discard_late_audio(self, *, window_id: str, turn_id: str, sequence: int) -> int:
        """丢弃迟到的 partial：已取消轮次的音频不得写入。"""
        rows = list(
            (
                await self._session.execute(
                    select(SpeechSegment).where(
                        SpeechSegment.window_id == window_id,
                        SpeechSegment.turn_id == turn_id,
                        SpeechSegment.kind == "partial",
                    )
                )
            ).scalars()
        )
        for row in rows:
            await self._session.delete(row)
        await self._session.flush()
        return len(rows)

    @staticmethod
    def _view(row: SpeechSegment) -> SegmentView:
        created = row.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return SegmentView(
            segment_id=row.id,
            window_id=row.window_id,
            turn_id=row.turn_id,
            kind=row.kind,
            text=row.text,
            model_label=row.model_label,
            created_at=created,
        )


class PlaybackTracker:
    """TTS 播放回执（内存态；W3 联调时改持久化）。"""

    def __init__(self) -> None:
        self._receipts: dict[str, PlaybackReceipt] = {}

    def record(self, *, turn_id: str, played_ms: int, completed: bool) -> PlaybackReceipt:
        receipt = PlaybackReceipt(
            playback_id=new_id("pb"),
            turn_id=turn_id,
            played_ms=max(0, played_ms),
            completed=completed,
            observed_at=utcnow(),
        )
        self._receipts[turn_id] = receipt
        return receipt

    def get(self, turn_id: str) -> PlaybackReceipt | None:
        return self._receipts.get(turn_id)
