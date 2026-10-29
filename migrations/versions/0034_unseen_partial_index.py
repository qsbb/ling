"""未读消息队列的偏索引

Revision ID: 0034_unseen_partial_index
Revises: 0033_message_attention
Create Date: 2026-10-28

注意力轮询要反复查「这个窗口有哪些还没看到的消息」。现有索引
`ix_messages_window_seq (window_id, sequence)` 能覆盖窗口+排序，
但 `attention_state='unseen'` 只能回表后过滤——窗口消息一多就变成
"扫完这个窗口再挑出未读"。

用**偏索引**（partial index）只索引未读行：

  WHERE attention_state = 'unseen' AND role = 'user'

未读是**小集合**（看见即出队），所以这个索引很小、维护成本低，
而查询变成"直接在索引上按顺序取 N 条"。

`role='user' 也进 WHERE`：她自己说的话不存在"没看见"，
不放进索引可以再小一半。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_unseen_partial_index"
down_revision: str | None = "0033_message_attention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_messages_unseen"


def upgrade() -> None:
    existing = {
        idx["name"]
        for idx in sa.inspect(op.get_bind()).get_indexes("messages")
    }
    if _INDEX in existing:
        return
    op.create_index(
        _INDEX,
        "messages",
        ["window_id", "sequence"],
        unique=False,
        postgresql_where=sa.text(
            "attention_state = 'unseen' AND role = 'user'"
        ),
    )


def downgrade() -> None:
    existing = {
        idx["name"]
        for idx in sa.inspect(op.get_bind()).get_indexes("messages")
    }
    if _INDEX in existing:
        op.drop_index(_INDEX, table_name="messages")
