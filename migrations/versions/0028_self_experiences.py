"""角色自己的经历（自我时间线）

Revision ID: 0028_self_experiences
Revises: 0027_proactive_causality
Create Date: 2026-10-27

阶段 5「自我时间线」。**不复用 `memory_episodes`**：后者永远是
(subject_id, window_id, turn_id) 形状——"我跟某人在某个窗口的一轮对话"。
自我经历是 (actor_id, 来源活动) 形状，三者都不存在。

硬塞进一张表要同时做两件危险的事：加类型过滤到每个查询、
把三个非空列改成可空。一条漏加过滤的"缺 subject 的 episode"
会被当成"和某人的记忆"召回——这类错误很安静。

`dedupe_key` 唯一：同一件活动只能派生一条经历。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_self_experiences"
down_revision: str | None = "0027_proactive_causality"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "self_experiences" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "self_experiences",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False, server_default="activity"),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("source_event_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("dedupe_key", sa.String(192), nullable=False),
        sa.Column("origin", sa.String(24), nullable=False, server_default="simulated"),
        sa.Column("visibility", sa.String(32), nullable=False, server_default="actor_private"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dedupe_key", name="uq_self_experiences_dedupe"),
    )
    op.create_index("ix_self_experiences_actor_id", "self_experiences", ["actor_id"])
    op.create_index("ix_self_experiences_occurred_at", "self_experiences", ["occurred_at"])


def downgrade() -> None:
    if "self_experiences" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("self_experiences")
