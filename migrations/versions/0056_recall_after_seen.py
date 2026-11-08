"""消息撤回第二层：``messages.retracted_after_seen``

Revision ID: 0056_recall_after_seen
Revises: 0055_message_retracted_at
Create Date: 2026-11-09

## 为什么要再分一列

用户明确要求撤回分两种，不能一律"当作没发生"：

- **她看手机之后才撤回**：那条消息已经进过她的视野。正常处理照常（该回就回），
  之后她在上下文里能看到「（这条消息后来被撤回了）」——不能把"她见过"这件事
  悄悄抹掉；
- **她还没看到就被撤回**：等同没看见，`retracted_at` 打上、不进任何上下文。

`retracted_at` 只说了"撤回过"，没说"撤回时她看没看见"。这一列把判定**冻结在
撤回那一刻**。

## 为什么不读取时再推

`attention_state` 会变（群里积压的消息会从 `unseen` 走到 `missed`），
`handled_as` 也会随投递结果改写。读取时再推等于用**今天**的状态解释**撤回
那一刻**的事实——两种情况的处置完全不同，这种漂移会静默地把"她见过"变成
"她没见过"（或反过来）。判定必须在撤回时做一次、存下来。

取值：`NULL` = 没有撤回；`false` = 撤回时她还没看到；`true` = 撤回时已看到。

## 旧数据

`retracted_at` 非空但 `retracted_after_seen` 为 NULL 的行（本迁移之前撤回的），
**按 `false` 处理**（保守口径：不确定看没看见时，按"没看见"——不引用它，也不
声称她记得）。这不是数据丢失，是"没有证据就不冒充"。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056_recall_after_seen"
down_revision: str | None = "0055_message_retracted_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("messages")}
    if "retracted_after_seen" not in columns:
        op.add_column(
            "messages",
            sa.Column("retracted_after_seen", sa.Boolean(), nullable=True),
        )
        # 本迁移之前撤回过的行：保守按"没看见"处理（见模块注释）。
        op.execute(
            "UPDATE messages SET retracted_after_seen = false "
            "WHERE retracted_at IS NOT NULL AND retracted_after_seen IS NULL"
        )


def downgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("messages")}
    if "retracted_after_seen" in columns:
        op.drop_column("messages", "retracted_after_seen")
