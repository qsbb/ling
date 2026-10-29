"""主动机会的草稿文本

Revision ID: 0038_proactive_draft
Revises: 0037_together_persist
Create Date: 2026-10-28

主动联系的派发整段原先缺失：候选会被收集、门控、过期，但没有任何代码
把它变成消息。直接上线"发送"太冒险——用户没法判断"她今天为什么突然找我"。

所以先做**可观察的中间态**：基于候选的 `reason` 生成一句开场白，存进
`draft_text`，让用户能先看到"她会说什么"，确认之后再开真发送。

`reason` 本身不能直接当消息发：那是内部推理（"问对方手机上聊天指什么项目"），
发出去很怪。必须过一次生成。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_proactive_draft"
down_revision: str | None = "0037_together_persist"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {
        col["name"]
        for col in sa.inspect(op.get_bind()).get_columns("proactive_opportunities")
    }
    if "draft_text" not in columns:
        op.add_column(
            "proactive_opportunities",
            sa.Column("draft_text", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    columns = {
        col["name"]
        for col in sa.inspect(op.get_bind()).get_columns("proactive_opportunities")
    }
    if "draft_text" in columns:
        op.drop_column("proactive_opportunities", "draft_text")
