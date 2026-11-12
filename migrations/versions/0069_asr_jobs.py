"""Persist audio transcription jobs and submission state.

Revision ID: 0069_asr_jobs
Revises: 0068_media_asset_grants
"""
import sqlalchemy as sa
from alembic import op

revision = "0069_asr_jobs"
down_revision = "0068_media_asset_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asr_jobs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("window_id", sa.String(64), nullable=False, index=True),
        sa.Column("subject_id", sa.String(64), nullable=False, index=True),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("request_key", sa.String(200), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("source_message_id", sa.String(64), nullable=True),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, index=True),
        sa.Column("submission", sa.String(24), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("config_revision", sa.Integer(), nullable=False),
        sa.Column("config_payload", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("edited_text", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(300), nullable=True),
        sa.Column("charged_seconds", sa.Float(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("window_id", "subject_id", "request_key", name="uq_asr_request"),
    )
    op.create_table(
        "asr_call_reservations",
        sa.Column("job_id", sa.String(64), sa.ForeignKey("asr_jobs.id"), primary_key=True),
        sa.Column("attempt", sa.Integer(), primary_key=True),
        sa.Column("audio_index", sa.Integer(), primary_key=True),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("seconds", sa.Float(), nullable=False),
    )
    op.create_index("ix_asr_reservations_provider_day", "asr_call_reservations", ["provider_id", "day"])


def downgrade() -> None:
    op.drop_table("asr_call_reservations")
    op.drop_table("asr_jobs")
