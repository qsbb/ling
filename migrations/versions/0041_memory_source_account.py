"""记忆记录来源账号

Revision ID: 0041_memory_source_account
Revises: 0040_media_dedupe_window
Create Date: 2026-10-29

## 为什么需要这一列

`subjects` 与 `actor_accounts` 是 1:n —— 一个自然人天然可以挂 QQ 大号、
小号、微信、Telegram。合并成同一个 subject 之后，`memories.subject_id`
只能说明"这是关于这个人的记忆"，**说不出是哪个号说的**。

合并功能要能无损拆分，就必须知道每条记忆的来源账号：

- 合并 = 召回按 subject 跨账号查（她"都记得"）
- 拆分 = `subject_id` 改回去，靠 `source_account` 把每条记忆送回原账号

事后按窗口反推是不行的：群窗口是多人的，反推不出是谁说的。
所以必须在写入时就记下。

## 存量数据

老记忆没有来源账号，留 NULL。它们属于"合并之前就存在"的记录，
拆分时无法归位——这是可以接受的：合并功能上线前不会有合并，
没有合并就没有需要拆的场景。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041_memory_source_account"
down_revision: str | None = "0040_media_dedupe_window"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("memories")}
    if "source_account" not in columns:
        op.add_column(
            "memories", sa.Column("source_account", sa.String(160), nullable=True)
        )
        op.create_index(
            "ix_memories_source_account", "memories", ["source_account"]
        )


def downgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("memories")}
    if "source_account" in columns:
        op.drop_index("ix_memories_source_account", table_name="memories")
        op.drop_column("memories", "source_account")
