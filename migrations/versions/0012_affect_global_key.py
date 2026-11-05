"""affect_states: global 作用域的 window_id 用空串而非 NULL

Revision ID: 0012_affect_global_key
Revises: 0011_affect_unique
Create Date: 2026-10-27

并发实测暴露的真实缺陷：PostgreSQL 中 NULL 互不相等，因此
`UNIQUE(actor_id, emotion, scope, window_id)` 对 global 行（window_id IS NULL）
**不生效**——6 个并发 nudge 插入了 4 条 global 记录，情绪增量被分散。

修法：global 行的 window_id 存空串。这样唯一约束对所有作用域都有效，
ON CONFLICT 也能正确命中。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_affect_global_key"
down_revision: str | None = "0011_affect_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 先把既有 NULL 合并成空串，并去重（保留强度最高的一条）
    op.execute("UPDATE affect_states SET window_id = '' WHERE window_id IS NULL")
    op.execute(
        """
        DELETE FROM affect_states a
        USING affect_states b
        WHERE a.actor_id = b.actor_id
          AND a.emotion = b.emotion
          AND a.scope = b.scope
          AND a.window_id = b.window_id
          AND a.intensity < b.intensity
        """
    )
    # 列改为 NOT NULL + 默认空串
    op.execute("ALTER TABLE affect_states ALTER COLUMN window_id SET DEFAULT ''")
    op.execute("ALTER TABLE affect_states ALTER COLUMN window_id SET NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE affect_states ALTER COLUMN window_id DROP NOT NULL")
    op.execute("ALTER TABLE affect_states ALTER COLUMN window_id DROP DEFAULT")
