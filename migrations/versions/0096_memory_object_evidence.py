"""Index multiple objects against evidence-backed memories and experiences."""

import sqlalchemy as sa
from alembic import op

revision = "0096_memory_object_evidence"
down_revision = "0095_backfill_group_entities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("entities", sa.Column("source_key", sa.String(96)))
    op.create_unique_constraint("uq_entities_subject_source", "entities", ["subject_id", "source_key"])
    op.create_index(
        "uq_entities_legacy_name", "entities", ["subject_id", "name"], unique=True,
        postgresql_where=sa.text("source_key IS NULL"),
    )
    op.drop_constraint("uq_entities_subject_name", "entities", type_="unique")

    op.add_column(
        "memory_entities", sa.Column("record_type", sa.String(16), nullable=False, server_default="memory"),
    )
    op.create_check_constraint(
        "ck_memory_entities_record_type", "memory_entities", "record_type IN ('memory', 'experience')",
    )
    op.drop_constraint("memory_entities_pkey", "memory_entities", type_="primary")
    op.create_primary_key("memory_entities_pkey", "memory_entities", ["memory_id", "entity_id", "record_type"])


def downgrade() -> None:
    op.execute("""
        DELETE FROM memory_entities
         WHERE record_type = 'experience'
            OR entity_id IN (SELECT id FROM entities WHERE source_key IS NOT NULL)
    """)
    op.execute("DELETE FROM entities WHERE source_key IS NOT NULL")
    op.drop_constraint("memory_entities_pkey", "memory_entities", type_="primary")
    op.create_primary_key("memory_entities_pkey", "memory_entities", ["memory_id", "entity_id"])
    op.drop_constraint("ck_memory_entities_record_type", "memory_entities", type_="check")
    op.drop_column("memory_entities", "record_type")

    op.create_unique_constraint("uq_entities_subject_name", "entities", ["subject_id", "name"])
    op.drop_index("uq_entities_legacy_name", table_name="entities")
    op.drop_constraint("uq_entities_subject_source", "entities", type_="unique")
    op.drop_column("entities", "source_key")
