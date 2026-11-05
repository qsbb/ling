"""用户标记：必须先改密码

Revision ID: 0043_must_change_password
Revises: 0042_message_sender
Create Date: 2026-10-29

## 为什么需要这一列

首次安装时没人知道该给管理员设什么密码，所以安装脚本生成一个随机口令，
并把它打印在启动日志里。这样"装完就能登进去"，但也留下两个问题：

1. 那个随机串会一直留在日志和 `.env` 里——只要没人改，它就是一个
   长期存在的后门。
2. 用户很可能把它抄在某处，或者干脆没看到，然后来问"密码是什么"。

所以随机口令只作为**一次性登船票**：置位这一列之后，除 `/v1/auth/*`
之外的接口一律返回 403，逼用户先改成自己记得的口令。改完清位。

默认值取 `false`：已有部署升级上来时不该被突然要求改密码。
只有引导创建（随机口令那条路）会显式置 true。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043_must_change_password"
down_revision: str | None = "0042_message_sender"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("users")}
    if "must_change_password" not in columns:
        op.add_column(
            "users",
            sa.Column(
                "must_change_password",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )


def downgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("users")}
    if "must_change_password" in columns:
        op.drop_column("users", "must_change_password")
