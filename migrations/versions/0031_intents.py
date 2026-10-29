"""意图队列：她自己的「想做什么」

Revision ID: 0031_intents
Revises: 0030_message_handled_as
Create Date: 2026-10-28

独立于 agenda / activity：

  agenda_plans  日程（有明确起止时间的安排）
  activities    实际在做的事
  intents       念头（"等她上线要说这个"）

三者混在一起会让"没按日程做"和"没想起来说"变成同一类失信。
念头**不是承诺**：到期没做只是自动失效（state=expired），不需要道歉。

`rationale` 必须引用具体来源。没有来源的意图不允许产生——
"凭空想打个招呼"正是路线图明确排除的假自主。

`dedupe_key` 唯一：同一件事不会因为反复想而产生十条意图。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_intents"
down_revision: str | None = "0030_message_handled_as"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "intents" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "intents",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=True),
        sa.Column("window_id", sa.String(64), nullable=True),
        sa.Column("kind", sa.String(24), nullable=False, server_default="reach_out"),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("source_kind", sa.String(24), nullable=False, server_default="message"),
        sa.Column("source_ref", sa.String(128), nullable=True),
        sa.Column(
            "origin", sa.String(32), nullable=False, server_default="model_inference"
        ),
        sa.Column(
            "visibility", sa.String(32), nullable=False, server_default="subject_private"
        ),
        sa.Column("state", sa.String(24), nullable=False, server_default="open"),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("dedupe_key", sa.String(192), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dedupe_key", name="uq_intents_dedupe"),
    )
    op.create_index("ix_intents_actor_id", "intents", ["actor_id"])
    op.create_index("ix_intents_subject_id", "intents", ["subject_id"])
    op.create_index("ix_intents_state", "intents", ["state"])
    op.create_index("ix_intents_expires_at", "intents", ["expires_at"])
    op.create_index("ix_intents_state_due", "intents", ["state", "not_before"])


def downgrade() -> None:
    if "intents" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("intents")
