"""作息与日程计划

Revision ID: 0026_routine_agenda
Revises: 0025_presence_scene
Create Date: 2026-10-27

阶段 2「一天有节奏」。三件事：

- `actor_routines`：角色自己的软作息（几点醒/几点歇）。**软**：
  它是活动可发生的宽窗口，不是到点必须执行
- `agenda_plans`：角色自己的小事。`commitment_level` 区分 tentative/confirmed
- `activities` 补 `plan_id / origin / source_event_ids / visibility`：
  让计划、模拟生活与真实回执可区分，也让私聊促成的活动不泄漏到群里

`agenda_plans` 与 `commitments` 是两回事：后者是对用户的**硬承诺**。
混在一起会让"没按作息起床"和"答应用户却没做到"被当成同一类失信。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_routine_agenda"
down_revision: str | None = "0025_presence_scene"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "actor_routines" not in tables:
        op.create_table(
            "actor_routines",
            sa.Column("actor_id", sa.String(64), primary_key=True),
            sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Shanghai"),
            sa.Column("wake_minute", sa.Integer(), nullable=False, server_default="480"),
            sa.Column("rest_minute", sa.Integer(), nullable=False, server_default="1380"),
            sa.Column("source", sa.String(16), nullable=False, server_default="default"),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        )
    if "agenda_plans" not in tables:
        op.create_table(
            "agenda_plans",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("actor_id", sa.String(64), nullable=False),
            sa.Column("kind", sa.String(32), nullable=False),
            sa.Column("title", sa.String(120), nullable=False),
            sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Shanghai"),
            sa.Column("commitment_level", sa.String(16), nullable=False, server_default="tentative"),
            sa.Column("state", sa.String(16), nullable=False, server_default="planned"),
            sa.Column("place_id", sa.String(64), nullable=True),
            sa.Column("source_event_ids", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_agenda_plans_actor_id", "agenda_plans", ["actor_id"])
        op.create_index("ix_agenda_plans_state", "agenda_plans", ["state"])

    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("activities")}
    if "plan_id" not in cols:
        op.add_column("activities", sa.Column("plan_id", sa.String(64), nullable=True))
    if "origin" not in cols:
        op.add_column(
            "activities",
            sa.Column("origin", sa.String(24), nullable=False, server_default="simulated"),
        )
    if "source_event_ids" not in cols:
        op.add_column(
            "activities",
            sa.Column("source_event_ids", sa.JSON(), nullable=False, server_default="[]"),
        )
    if "visibility" not in cols:
        op.add_column(
            "activities",
            sa.Column(
                "visibility", sa.String(32), nullable=False, server_default="subject_private"
            ),
        )


def downgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("activities")}
    for name in ("visibility", "source_event_ids", "origin", "plan_id"):
        if name in cols:
            op.drop_column("activities", name)
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "agenda_plans" in tables:
        op.drop_table("agenda_plans")
    if "actor_routines" in tables:
        op.drop_table("actor_routines")
