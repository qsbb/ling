"""人设的场景提示词：同一份人设，在不同情境下怎么写

Revision ID: 0047_persona_scenarios
Revises: 0046_phone_state
Create Date: 2026-11-06

## 为什么是一列 JSON，而不是一张 scenario 表

场景键是**代码里的判定**（私聊 / 群聊 / 群里被点名 / 群里没被点名），
不是用户可以自由增删的分类。行的数量由代码决定（四行，全仓固定），
做成一张表只会让"这条键到底有没有注入点"更难核对。

JSON 列只存**覆盖**：没写过的场景不落库，读的时候回落到代码里的默认。
这样默认值改一次，所有人设立刻跟着变；反过来，如果把生效文本存下来，
默认就永远停在建行那天的版本上。

## 为什么给 server_default

`personas` 是已有表，加 NOT NULL 列必须有服务端默认值，否则老行无法回填。
`'{}'` 正好就是"全是默认"的语义。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0047_persona_scenarios"
down_revision: str | None = "0046_phone_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("personas")}
    if "scenario_prompts" not in columns:
        op.add_column(
            "personas",
            sa.Column("scenario_prompts", sa.JSON(), nullable=False, server_default="{}"),
        )


def downgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("personas")}
    if "scenario_prompts" in columns:
        op.drop_column("personas", "scenario_prompts")
