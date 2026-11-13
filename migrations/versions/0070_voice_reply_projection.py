"""Persist voice reply modality and independent synthesis/delivery states.

Revision ID: 0070_voice_reply_projection
Revises: 0069_asr_jobs
"""

import sqlalchemy as sa
from alembic import op

revision = "0070_voice_reply_projection"
down_revision = "0069_asr_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "speech_synthesis_jobs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("message_id", sa.String(64), nullable=False, unique=True),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(200), nullable=False),
        sa.Column("config_revision", sa.Integer(), nullable=False),
        sa.Column("voice_config_hash", sa.String(64), nullable=True),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("input_characters", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("usage_source", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_speech_synthesis_jobs_provider_id", "speech_synthesis_jobs", ["provider_id"]
    )
    op.add_column(
        "proactive_opportunities",
        sa.Column(
            "output_modality",
            sa.String(16),
            nullable=False,
            server_default="text",
        ),
    )
    op.add_column(
        "messages",
        sa.Column(
            "output_modality",
            sa.String(16),
            nullable=False,
            server_default="text",
        ),
    )
    op.add_column("messages", sa.Column("audio_asset_id", sa.String(64), nullable=True))
    op.add_column(
        "messages",
        sa.Column(
            "speech_state",
            sa.String(24),
            nullable=False,
            server_default="none",
        ),
    )
    op.add_column("messages", sa.Column("speech_delivery_state", sa.String(24), nullable=True))
    op.execute("""
        UPDATE messages SET
            output_modality = 'voice',
            speech_state = 'ready',
            audio_asset_id = COALESCE(
                (SELECT g.asset_id FROM media_asset_grants g
                 JOIN media_assets b ON b.digest = g.blob_digest
                 WHERE g.message_id = messages.id AND g.purpose = 'tts_output'
                   AND b.kind = 'audio' AND b.ready_at IS NOT NULL
                 ORDER BY g.created_at DESC LIMIT 1),
                (SELECT b.digest FROM media_assets b
                 WHERE b.message_id = messages.id AND b.kind = 'audio'
                   AND b.ready_at IS NOT NULL LIMIT 1)
            )
        WHERE role = 'actor' AND (
            EXISTS (SELECT 1 FROM media_asset_grants g
                    JOIN media_assets b ON b.digest = g.blob_digest
                    WHERE g.message_id = messages.id AND g.purpose = 'tts_output'
                      AND b.kind = 'audio' AND b.ready_at IS NOT NULL)
            OR EXISTS (SELECT 1 FROM media_assets b
                       WHERE b.message_id = messages.id AND b.kind = 'audio'
                         AND b.ready_at IS NOT NULL)
        )
    """)


def downgrade() -> None:
    op.drop_table("speech_synthesis_jobs")
    op.drop_column("proactive_opportunities", "output_modality")
    op.drop_column("messages", "speech_delivery_state")
    op.drop_column("messages", "speech_state")
    op.drop_column("messages", "audio_asset_id")
    op.drop_column("messages", "output_modality")
