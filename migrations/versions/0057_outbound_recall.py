"""主动撤回自己发出去的消息：``messages.recalled_at`` / ``recall_state`` /
``recall_receipt_id`` / ``recall_attempted_at``

Revision ID: 0057_outbound_recall
Revises: 0056_recall_after_seen
Create Date: 2026-11-09

## 为什么要和入站的 `retracted_at` 分开

`retracted_at` 的语义是"**对方**在平台上把那条收回了"，撤回方是别人；
这次的 `recalled_at` 是"**她自己**把自己说过的话收回来"，撤回方是她。
两者都让原文不能再被当成有效发言，但以下三件事完全不同：

- **谁触发的**（安全审计要回答"谁删了这条"）；
- **窗口与频次**（平台的 2 分钟窗只约束"撤回自己发的"）；
- **回执**（撤回自己发的才需要 `delete_msg` 的回执）。

挤进同一列会让"谁撤回的"永久丢失，而撤回是不可逆操作——这一列必须在。

## 四个字段各管什么

| 列 | 含义 | 什么时候写 |
| --- | --- | --- |
| `recalled_at` | **确认**撤回成功的时刻 | 只有平台回执是 `recalled` |
| `recall_state` | 最后一次尝试的结果（五态之一） | 每次尝试都写 |
| `recall_receipt_id` | 那条回执的 id | 拿到回执就写（含 `uncertain`） |
| `recall_attempted_at` | 最后一次**尝试**的时刻 | `uncertain` 时写（没确认） |

`recalled_at` 与 `recall_attempted_at` 必须分开：只有前者能说"撤回了"。
把它们合成一个"试过"的时间戳，下一次点击就会看到"已撤回"而跳过——于是
一次超时被永久记成成功。

## 为什么行不删

删行之后"这条消息从来不存在"和"它被撤回了"在库里长得一样，而
`memories.source_event_ids` / `turn_id` / 审阅页面会变成悬空引用。
撤回是"以后别再引用"，不是"过去没发生"。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057_outbound_recall"
down_revision: str | None = "0056_recall_after_seen"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("messages")}
    if "recalled_at" not in columns:
        op.add_column(
            "messages",
            sa.Column("recalled_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "recall_state" not in columns:
        op.add_column("messages", sa.Column("recall_state", sa.String(24), nullable=True))
    if "recall_receipt_id" not in columns:
        op.add_column(
            "messages", sa.Column("recall_receipt_id", sa.String(64), nullable=True)
        )
    if "recall_attempted_at" not in columns:
        op.add_column(
            "messages",
            sa.Column("recall_attempted_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("messages")}
    for name in (
        "recall_attempted_at",
        "recall_receipt_id",
        "recall_state",
        "recalled_at",
    ):
        if name in columns:
            op.drop_column("messages", name)
