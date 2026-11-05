"""交互负担：按范围隔离的「回烦了」

Revision ID: 0032_interaction_load
Revises: 0031_intents
Create Date: 2026-10-28

不复用 `actor_state.social_load`：那是**整体**负担、跨窗口累计。
"被这个群烦到"和"被那个人烦到"是两件事，混在一起就是跨受众泄露——
群里的烦躁不该影响对私聊对象的态度。

所以按 `scope_key`（窗口 id）隔离，并按时间惰性衰减（读时用时间戳推算，
不需要定时任务）。

它只影响「要不要回、回多长」。**语气由人设决定，不由这个数值决定。**
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_interaction_load"
down_revision: str | None = "0031_intents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "interaction_load" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "interaction_load",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("scope_key", sa.String(128), nullable=False),
        sa.Column("local_load", sa.Float(), nullable=False, server_default="0"),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("actor_id", "scope_key", name="uq_interaction_load_scope"),
    )
    op.create_index("ix_interaction_load_actor_id", "interaction_load", ["actor_id"])


def downgrade() -> None:
    if "interaction_load" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("interaction_load")
