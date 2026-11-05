"""情绪分层：global（可跨窗口的语气趋势）与 window（窗口局部）

Revision ID: 0006_affect_scope
Revises: 0005_memory_source_scope
Create Date: 2026-10-27

ADR-0004 修正：情绪可以有角色全局分量，但**另一窗口的具体原因、
对象和强烈态度不能随之传播**。因此拆两层：
- global：粗粒度语气趋势（如"回复稍短"），可跨窗口
- window：某窗口内具体情绪与原因，绝不跨窗口

已有数据视为 global（历史行为就是全局），语义一致。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_affect_scope"
down_revision: str | None = "0005_memory_source_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("affect_states")}
    if "scope" not in cols:
        op.add_column(
            "affect_states",
            sa.Column("scope", sa.String(16), nullable=False, server_default="global"),
        )
        op.create_index("ix_affect_states_scope", "affect_states", ["scope"])
    if "window_id" not in cols:
        op.add_column("affect_states", sa.Column("window_id", sa.String(64), nullable=True))
        op.create_index("ix_affect_states_window_id", "affect_states", ["window_id"])


def downgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("affect_states")}
    for name in ("window_id", "scope"):
        if name in cols:
            op.drop_column("affect_states", name)
