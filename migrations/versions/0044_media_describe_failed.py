"""媒体描述失败终态：引用不可用不再无限重试

Revision ID: 0044_media_describe_failed
Revises: 0043_must_change_password
Create Date: 2026-11-02

## 为什么需要这两列

`MediaRefError`（引用越界、超限、形式不支持）是**永久错误**：重试同一个
引用只会得到同一个结果。原实现把它和其它下载失败一起包成
`MediaDescribeError(retriable=True)`，而调用方**没有任何地方消费这个标志**
——`MediaEvidenceService.pending()` 只按 `description IS NULL` 过滤。

于是这条引用每一轮（`media.describe` 每 60 秒一次，见
`worker.py` 的 `PeriodicJob`）都会被重新捞出来重试，永远重试不完，
日志里一条"下载失败"重复到把真问题淹掉。

只把 `retriable` 改成 False 是**不够的**：那个标志没有任何读者。要真的
停下来，终态必须落库，并且 `pending()` 要按它过滤。

## 为什么不写进 `description`

`description` 有明确语义："看见了什么 / 推断出什么"，且 `fact_eligible`
与上下文装配都按它判定。往里面写"（无法描述）"会把"没看到"伪装成
"看到了但没内容"——既污染证据，又让真正的失败从队列里消失。

所以单独两列：`describe_failed_at`（时间，也是 `pending()` 的过滤依据）
与 `describe_failed_reason`（原因，给运维看，不参与判定）。

`describe_failed_at` 只由**永久错误**写入；可重试的失败（网络抖动、
模型超时）仍然留空，照旧每一轮重试。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044_media_describe_failed"
down_revision: str | None = "0043_must_change_password"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("media_evidence")}
    if "describe_failed_at" not in columns:
        op.add_column(
            "media_evidence",
            sa.Column("describe_failed_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "describe_failed_reason" not in columns:
        op.add_column(
            "media_evidence",
            sa.Column("describe_failed_reason", sa.String(200), nullable=True),
        )


def downgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("media_evidence")}
    if "describe_failed_reason" in columns:
        op.drop_column("media_evidence", "describe_failed_reason")
    if "describe_failed_at" in columns:
        op.drop_column("media_evidence", "describe_failed_at")
