"""媒体去重按窗口

Revision ID: 0040_media_dedupe_window
Revises: 0039_subject_trust
Create Date: 2026-10-28

## 为什么改约束

`media_evidence` 原来在 `dedupe_key` 上**全局唯一**。于是同一张图出现在
两个窗口时，第二条登记会撞唯一约束；而服务层是先查后插，查不到才插，
所以实际表现是——第二个窗口拿到第一个窗口的 evidence：

- 它的 `window_id` 对不上，描述任务按窗口取待办永远取不到
- 消息里引用的 evidence 属于**别的会话**

加上上传链路把占位 `media_ref` 写死成 "pending:upload"，去重键因此恒定，
从第二张图起全都返回第一张图的证据——"能发图"看起来正常，发的永远是第一张。

改成 `(dedupe_key, window_id)` 唯一：同一窗口内同内容只登记一次，
跨窗口各算一条。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040_media_dedupe_window"
down_revision: str | None = "0039_subject_trust"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    constraints = {
        c["name"] for c in sa.inspect(op.get_bind()).get_unique_constraints("media_evidence")
    }
    if "uq_media_evidence_dedupe" in constraints:
        op.drop_constraint("uq_media_evidence_dedupe", "media_evidence", type_="unique")
    op.create_unique_constraint(
        "uq_media_evidence_dedupe", "media_evidence", ["dedupe_key", "window_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_media_evidence_dedupe", "media_evidence", type_="unique")
    op.create_unique_constraint("uq_media_evidence_dedupe", "media_evidence", ["dedupe_key"])
