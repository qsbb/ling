"""memories.value：槽位值（供压缩级联按值合并）

Revision ID: 0020_memory_value
Revises: 0019_memory_embeddings
Create Date: 2026-10-27

压缩级联要把同槽位的多条低价值记忆并成一条高层摘要。合并需要**值**本身，
靠解析 text 反推值既脆弱（模板会改）又容易错，所以写入时就存下来。

旧行为 NULL：没有值的行不参与压缩，按保留期自然归档，
不做"猜一个值"的迁移（参照 LivingMemory 的教训：不凭摘要补造证据）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_memory_value"
down_revision: str | None = "0019_memory_embeddings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("memories")}
    if "value" not in cols:
        op.add_column("memories", sa.Column("value", sa.String(64), nullable=True))


def downgrade() -> None:
    if "value" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("memories")}:
        op.drop_column("memories", "value")
