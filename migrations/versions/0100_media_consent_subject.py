"""Link each material observation to the grant that authorized it."""

import sqlalchemy as sa
from alembic import op

revision = "0100_media_consent_subject"
down_revision = "0099_archive_speaker_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("media_evidence", sa.Column("consent_subject_id", sa.String(64)))
    op.execute("""
        UPDATE media_evidence AS evidence
        SET consent_subject_id = evidence.subject_id
        FROM windows AS source_window
        WHERE evidence.window_id = source_window.id
          AND evidence.subject_id = source_window.subject_id
          AND evidence.consent_revision > 0
    """)


def downgrade() -> None:
    op.drop_column("media_evidence", "consent_subject_id")
