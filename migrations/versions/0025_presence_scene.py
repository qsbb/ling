"""角色生活状态与当前场景

Revision ID: 0025_presence_scene
Revises: 0024_proactive_sent_at
Create Date: 2026-10-27

阶段 1「角色有当前状态」。两张表分别对应两类变化频率完全不同的状态：

- `presence_states`：精力/社交负担，按小时衰减
- `scene_commits`：此刻在做什么，短时、可中断、带 valid_until

不塞进 `ActorState`：那张表是版本栅栏（每条事件递增），
与"按时间衰减的投影"混在一起会让两边都难推理。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_presence_scene"
down_revision: str | None = "0024_proactive_sent_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "presence_states" not in tables:
        op.create_table(
            "presence_states",
            sa.Column("actor_id", sa.String(64), primary_key=True),
            sa.Column("energy", sa.Integer(), nullable=False, server_default="70"),
            sa.Column("social_load", sa.Integer(), nullable=False, server_default="20"),
            sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "scene_commits" not in tables:
        op.create_table(
            "scene_commits",
            sa.Column("actor_id", sa.String(64), primary_key=True),
            sa.Column("state", sa.String(16), nullable=False, server_default="unknown"),
            sa.Column("activity_id", sa.String(64), nullable=True),
            sa.Column("place_id", sa.String(64), nullable=True),
            sa.Column("origin", sa.String(24), nullable=False, server_default="simulated"),
            sa.Column("source_event_ids", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "scene_commits" in tables:
        op.drop_table("scene_commits")
    if "presence_states" in tables:
        op.drop_table("presence_states")
