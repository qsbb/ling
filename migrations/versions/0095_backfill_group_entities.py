"""backfill group entities for already imported group memories

Revision ID: 0095_backfill_group_entities
Revises: e55c1cf92a74
Create Date: 2026-11-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = '0095_backfill_group_entities'
down_revision: str | None = 'e55c1cf92a74'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing imports predate EntityService; without this, their groups never
    # appear in "按对象看" even though the memories are already in the library.
    op.execute("""
        INSERT INTO entities (id, subject_id, name, kind, aliases, created_at, updated_at)
        SELECT 'ent_g' || substr(md5(m.subject_id || ':' || (m.import_source->>'group_id')), 1, 24),
               m.subject_id,
               '群 ' || (m.import_source->>'group_id'),
               'group',
               json_build_array(m.import_source->>'group_id'),
               now(), now()
          FROM memories m
         WHERE m.import_source->>'scope' = 'group'
           AND coalesce(m.import_source->>'group_id', '') <> ''
         GROUP BY m.subject_id, (m.import_source->>'group_id')
        ON CONFLICT (subject_id, name) DO NOTHING
    """)
    op.execute("""
        INSERT INTO memory_entities (memory_id, entity_id, role)
        SELECT m.id, e.id, 'with'
          FROM memories m
          JOIN entities e
            ON e.subject_id = m.subject_id
           AND e.kind = 'group'
           AND e.name = '群 ' || (m.import_source->>'group_id')
         WHERE m.import_source->>'scope' = 'group'
           AND coalesce(m.import_source->>'group_id', '') <> ''
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM memory_entities
         WHERE entity_id IN (SELECT id FROM entities WHERE id LIKE 'ent_g%')
    """)
    op.execute("DELETE FROM entities WHERE id LIKE 'ent_g%'")
