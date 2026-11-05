"""subject 亲疏等级

Revision ID: 0039_subject_trust
Revises: 0038_proactive_draft
Create Date: 2026-10-28

## 为什么需要这一列

原来只有两态：**已证明的自然人** 或 **不存在**。于是群里任何人只要没走过
绑定流程，`resolve_account` 返回 None，整条消息被拒——实测确认：

    {"accepted": false, "reason": "渠道账号尚未绑定到已证明的自然人"}

等于她只能听见已绑定的人说话。朋友拉她进群，群里其他人说什么都收不到。

现实中人是渐进的：先"群里的那个 QQ 号"，聊久了才知道是谁。所以加一列
`trust_level` 区分 `owner` / `known` / `stranger`。

## 它限制的是**记忆范围**，不是权限

- `owner`：主人，全部私聊记忆，可触发跨窗召回
- `known`：聊过但未验证身份，记忆只限本群相关
- `stranger`：第一次见，不写记忆

跨窗事实（Awareness）对非 owner 一律不注入——隐私是 ling 对
这是最不该妥协的一条，不能为了"群里热闹"换成泄漏。

## 存量数据

已有的 subject 都是通过引导或已证明绑定产生的，那就是主人。所以直接把
`users.role='owner'` 对应的 subject 标成 `owner`，其余留 `known`。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_subject_trust"
down_revision: str | None = "0038_proactive_draft"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("subjects")}
    if "trust_level" not in columns:
        op.add_column(
            "subjects",
            sa.Column(
                "trust_level",
                sa.String(32),
                nullable=False,
                server_default="known",
            ),
        )
    # 引导出来的那个 subject 就是主人
    op.execute(
        sa.text(
            "UPDATE subjects SET trust_level = 'owner' "
            "WHERE id IN (SELECT subject_id FROM users WHERE role = 'owner')"
        )
    )


def downgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("subjects")}
    if "trust_level" in columns:
        op.drop_column("subjects", "trust_level")
