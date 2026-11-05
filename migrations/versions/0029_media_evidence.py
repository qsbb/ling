"""媒体证据：图片/音频被看到或被推断出什么

Revision ID: 0029_media_evidence
Revises: 0028_self_experiences
Create Date: 2026-10-28

阶段 6 的第一片。**不新建 memory 表**：媒体引用的生命周期比记忆短得多
（``expires_at``），而且它的可信度决定能不能进事实。混进 memory 会丢掉
"这是猜的还是看到的"这个区分——而这条区分正是本表存在的理由。

两条硬规则：

1. **只在授权窗口被描述**。记录绑定 ``window_id``；能否描述由
   ``media.inbound`` 授权决定，默认拒绝。
2. **图像猜测不写事实**。``origin=model_inference`` 的描述永远不可作为
   事实使用（由服务层的 ``fact_eligible`` 判定，不靠调用方自觉）。

``dedupe_key`` 唯一：同一个来源事件只能产生一条证据，重复回调不多记。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_media_evidence"
down_revision: str | None = "0028_self_experiences"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "media_evidence" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "media_evidence",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=True),
        sa.Column("window_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="image"),
        sa.Column("media_ref", sa.String(512), nullable=False),
        sa.Column("origin", sa.String(24), nullable=False, server_default="user_upload"),
        sa.Column(
            "visibility", sa.String(32), nullable=False, server_default="window_private"
        ),
        sa.Column("consent_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("description_origin", sa.String(24), nullable=True),
        sa.Column("source_event_id", sa.String(128), nullable=True),
        sa.Column("dedupe_key", sa.String(192), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dedupe_key", name="uq_media_evidence_dedupe"),
    )
    op.create_index("ix_media_evidence_actor_id", "media_evidence", ["actor_id"])
    op.create_index("ix_media_evidence_subject_id", "media_evidence", ["subject_id"])
    op.create_index("ix_media_evidence_window_id", "media_evidence", ["window_id"])
    op.create_index("ix_media_evidence_expires_at", "media_evidence", ["expires_at"])
    op.create_index(
        "ix_media_evidence_actor_window", "media_evidence", ["actor_id", "window_id"]
    )


def downgrade() -> None:
    if "media_evidence" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("media_evidence")
