"""Durable per-opportunity reservations for proactive daily quotas."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from ling.infra.db import Base


class ProactiveQuotaReservation(Base):
    """One subject-local-day quota slot associated with a proactive opportunity."""

    __tablename__ = "proactive_quota_reservations"
    __table_args__ = (
        CheckConstraint(
            "state IN ('reserved', 'sent', 'uncertain', 'released')",
            name="ck_proactive_quota_reservation_state",
        ),
        Index(
            "ix_proactive_quota_subject_day_state",
            "actor_id",
            "subject_id",
            "local_day",
            "state",
        ),
    )

    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("proactive_opportunities.id", ondelete="CASCADE"), primary_key=True
    )
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    local_day: Mapped[date] = mapped_column(Date, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
