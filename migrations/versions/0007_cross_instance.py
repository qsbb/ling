"""跨实例协调：停止信号与发言租约

Revision ID: 0007_cross_instance
Revises: 0006_affect_scope
Create Date: 2026-10-27

设计文档 §10 要求首版不把 Redis 作为依赖。因此用已有 PostgreSQL 实现
多实例必需的两件事：

- turn_controls：stop 请求跨实例传递（在 A 点停止，轮次跑在 B）
- speak_leases：同一角色同时只在一个窗口说话，租约到期可抢占

流式片段与群发言频率仍是单进程语义（可接受降级：续传退化为快照、
群频率略宽松），已在代码注释说明。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_cross_instance"
down_revision: str | None = "0006_affect_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "turn_controls" not in tables:
        op.create_table(
            "turn_controls",
            sa.Column("turn_id", sa.String(64), primary_key=True),
            sa.Column("stop_requested_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "speak_leases" not in tables:
        op.create_table(
            "speak_leases",
            sa.Column("actor_id", sa.String(64), primary_key=True),
            sa.Column("window_id", sa.String(64), nullable=False),
            sa.Column("turn_id", sa.String(64), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("queue", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_speak_leases_expires_at", "speak_leases", ["expires_at"])


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "speak_leases" in tables:
        op.drop_table("speak_leases")
    if "turn_controls" in tables:
        op.drop_table("turn_controls")
