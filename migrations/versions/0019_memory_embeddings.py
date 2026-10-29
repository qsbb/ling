"""记忆向量表（可选能力）

Revision ID: 0019_memory_embeddings
Revises: 0018_memory_reinforcement
Create Date: 2026-10-27

参照实现（MaiBot A-Memorix 双路检索、以及基于 Embedding 的候选召回）
都把语义召回作为质量关键。这里加独立表存向量：

- 独立表而不是给 memories 加列：换模型时可整体 truncate 重建；
  向量体积大，不该拖慢主表扫描
- 用 JSON 存而非 pgvector：不引入扩展依赖；个人规模（千级）在候选集上
  算余弦足够。上万条时应改用 pgvector 或外部向量库——这是有意的降级。

**未配置 embedding 提供方时本表为空，检索自动退回 BM25+RRF 路径。**
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_memory_embeddings"
down_revision: str | None = "0018_memory_reinforcement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "memory_embeddings" in tables:
        return
    op.create_table(
        "memory_embeddings",
        sa.Column("memory_id", sa.String(64), primary_key=True),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("vector", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_memory_embeddings_model", "memory_embeddings", ["model_id"]
    )


def downgrade() -> None:
    if "memory_embeddings" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("memory_embeddings")
