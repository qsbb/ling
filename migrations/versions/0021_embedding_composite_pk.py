"""memory_embeddings 主键改为 (memory_id, model_id)

Revision ID: 0021_embedding_composite_pk
Revises: 0020_memory_value
Create Date: 2026-10-27

原主键只有 memory_id，但代码按 (memory_id, model_id) 判断是否已有向量。
换 embedding 模型时新向量写不进去（主键冲突），异常被吞后事务失效，
后续召回报 PendingRollbackError。

改成复合主键后，新旧模型的向量可以并存；检索只取当前模型的。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_embedding_composite_pk"
down_revision: str | None = "0020_memory_value"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    pk = inspector.get_pk_constraint("memory_embeddings")
    cols = list(pk.get("constrained_columns") or [])
    if cols == ["memory_id", "model_id"]:
        return
    op.drop_constraint(pk.get("name") or "memory_embeddings_pkey",
                       "memory_embeddings", type_="primary")
    op.create_primary_key(
        "memory_embeddings_pkey", "memory_embeddings", ["memory_id", "model_id"]
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    pk = inspector.get_pk_constraint("memory_embeddings")
    cols = list(pk.get("constrained_columns") or [])
    if cols == ["memory_id"]:
        return
    # 回退会丢多模型向量：只保留每个 memory 的第一条
    op.execute(
        """
        DELETE FROM memory_embeddings a
        USING memory_embeddings b
        WHERE a.memory_id = b.memory_id AND a.model_id > b.model_id
        """
    )
    op.drop_constraint(pk.get("name") or "memory_embeddings_pkey",
                       "memory_embeddings", type_="primary")
    op.create_primary_key("memory_embeddings_pkey", "memory_embeddings", ["memory_id"])
