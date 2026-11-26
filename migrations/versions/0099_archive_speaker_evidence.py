"""Keep an archive speaker's name and evidence on the individual memory link."""

import sqlalchemy as sa
from alembic import op

revision = "0099_archive_speaker_evidence"
down_revision = "0098_core_memory_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memory_entities", sa.Column("source_reference", sa.String(200)))
    op.add_column("memory_entities", sa.Column("source_label", sa.String(64)))


def downgrade() -> None:
    op.execute("DELETE FROM memory_entities WHERE source_reference IS NOT NULL AND role = 'speaker'")
    op.execute("""
        DELETE FROM entities
        WHERE starts_with(source_key, 'archive_speaker:') AND NOT EXISTS (
            SELECT 1 FROM memory_entities WHERE memory_entities.entity_id = entities.id
        )
    """)
    op.drop_column("memory_entities", "source_label")
    op.drop_column("memory_entities", "source_reference")
