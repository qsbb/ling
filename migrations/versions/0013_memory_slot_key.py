"""memories 幂等键改为 (turn_id, slot)

Revision ID: 0013_memory_slot_key
Revises: 0012_affect_global_key
Create Date: 2026-10-27

此前实现的隐患：`(turn_id, text)` 用文本作幂等键，文本随提取规则变化，
规则升级或换模板后同一轮会重复长出记忆。

改为 `(turn_id, slot)`：slot 是提取器的**语义槽位**（name / likes / lives_in），
与措辞无关。

历史数据：旧行 slot 为 NULL，不参与唯一约束（PostgreSQL NULL 互不相等）。
因此先把旧行的 slot 从文本推断出来，能推断的填补，不能的留 NULL。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_memory_slot_key"
down_revision: str | None = "0012_affect_global_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("memories")}
    if "slot" not in cols:
        op.add_column("memories", sa.Column("slot", sa.String(64), nullable=True))
        op.create_index("ix_memories_slot", "memories", ["slot"])

    # 旧约束先删（存在才删）
    existing = {
        c["name"] for c in sa.inspect(op.get_bind()).get_unique_constraints("memories")
    }
    if "uq_memories_turn_text" in existing:
        op.drop_constraint("uq_memories_turn_text", "memories", type_="unique")

    # 从既有文本反推槽位，让历史数据也进入新语义
    op.execute("UPDATE memories SET slot = 'name' WHERE slot IS NULL AND text LIKE '用户的名字是%'")
    op.execute("UPDATE memories SET slot = 'likes' WHERE slot IS NULL AND text LIKE '用户喜欢%'")
    op.execute("UPDATE memories SET slot = 'lives_in' WHERE slot IS NULL AND text LIKE '用户住在%'")

    # 去重：同 (turn_id, slot) 保留最早一条
    op.execute(
        """
        DELETE FROM memories a
        USING memories b
        WHERE a.turn_id IS NOT NULL
          AND a.slot IS NOT NULL
          AND a.turn_id = b.turn_id
          AND a.slot = b.slot
          AND a.created_at > b.created_at
        """
    )

    existing = {
        c["name"] for c in sa.inspect(op.get_bind()).get_unique_constraints("memories")
    }
    if "uq_memories_turn_slot" not in existing:
        op.create_unique_constraint("uq_memories_turn_slot", "memories", ["turn_id", "slot"])


def downgrade() -> None:
    op.drop_constraint("uq_memories_turn_slot", "memories", type_="unique")
    op.create_unique_constraint("uq_memories_turn_text", "memories", ["turn_id", "text"])
    if "slot" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("memories")}:
        op.drop_column("memories", "slot")
