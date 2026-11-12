"""Durable audio transcription and submission state."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ling.infra.db import Base
from ling.infra.models import TimestampMixin, utcnow


class AsrJob(Base, TimestampMixin):
    __tablename__ = "asr_jobs"
    __table_args__ = (UniqueConstraint("window_id", "subject_id", "request_key", name="uq_asr_request"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_id: Mapped[str] = mapped_column(String(64), index=True)
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    actor_id: Mapped[str] = mapped_column(String(64))
    request_key: Mapped[str] = mapped_column(String(200))
    fingerprint: Mapped[str] = mapped_column(String(64))
    source_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mode: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    submission: Mapped[str] = mapped_column(String(24), default="pending")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_id: Mapped[str] = mapped_column(String(64), default="")
    config_revision: Mapped[int] = mapped_column(Integer, default=0)
    config_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    text: Mapped[str] = mapped_column(Text, default="")
    edited_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(300), nullable=True)
    charged_seconds: Mapped[float] = mapped_column(Float, default=0)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AsrCallReservation(Base):
    """每次识别调用独立占用调用当日的音频秒数，未知结果仍保留。"""

    __tablename__ = "asr_call_reservations"
    __table_args__ = (Index("ix_asr_reservations_provider_day", "provider_id", "day"),)

    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("asr_jobs.id"), primary_key=True)
    attempt: Mapped[int] = mapped_column(Integer, primary_key=True)
    audio_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(64))
    day: Mapped[date] = mapped_column(Date)
    seconds: Mapped[float] = mapped_column(Float)
