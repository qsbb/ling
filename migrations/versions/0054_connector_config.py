"""接入器可配置：覆盖层修订、覆盖值、指针与副本确认

Revision ID: 0054_connector_config
Revises: 0053_prompt_snapshots
Create Date: 2026-11-09

## 为什么需要 DB 覆盖层

生产容器非 root、根文件系统只读、且没有挂载宿主 `.env`（见
``deploy/compose.yaml`` 的 ``read_only`` 与挂载清单），所以"网页改
``.env`` 再重启"这条路在容器里**根本走不通**：写不了文件，也重启不了
自己。要让设置页的接入器变成可配置，值只能落在**数据库**里，由进程读出来
构造连接器。

## 为什么不复用 ``config_revisions``

那张表的 ``payload`` 是可读 JSON，装的是"模型怎么跑"，并且跟着
发布/回滚走。接入器修订里含**渠道密钥**（OneBot token、QQBot AppSecret…），
必须**逐字段密文**存储；它的生效方式也完全不同——重建长连接实例，
而不是把新 payload 交给下一次请求。混进一张表会让"回滚模型配置"顺手
改掉入站凭据，那是两个方向的错误。

## 四张表（照设计 §3）

- ``connector_config_revisions``：``pending`` → ``activating`` → ``active``；
  失败落 ``rolled_back``（已回到上一版），回退本身也失败才是 ``degraded``。
- ``connector_override_values``：``UNIQUE(revision_id, field_key)``；每个候选
  修订保存该 scope **完整覆盖集合快照**，未出现的 key 表示回退 env。
  CHECK 强制"密钥只存密文、非密钥只存规范化值"——防止未来某次改动把密钥
  写进可读列（由数据库拒绝，不依赖代码评审）。
- ``connector_config_heads``：每 scope 一行指针 + 单调 ``generation``；
  客户端的 ``If-Match`` 与副本 ACK 都以它为准。
- ``connector_runtime_acks``：``UNIQUE(scope, generation, replica_id)``；单副本部署
  里它就是"本地切换完成"的证据，多副本要求全部存活副本 ACK。

## 为什么不回填 env

老部署升级后覆盖表为空，连接器继续按 env + 默认值启动，**行为与升级前一致**。
把 env 明文复制进库会立刻造成两个问题：密钥多一份副本；之后改 env 静默失效
（因为 DB 覆盖优先），而用户以为 `.env` 还是权威。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0054_connector_config"
down_revision: str | None = "0053_prompt_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connector_config_revisions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("parent_id", sa.String(64), nullable=True),
        sa.Column("state", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(48), nullable=True),
        sa.Column("error_safe", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_connector_config_revisions_scope_state",
        "connector_config_revisions",
        ["scope", "state"],
    )
    op.create_index(
        "ix_connector_config_revisions_created", "connector_config_revisions", ["created_at"]
    )
    op.create_index(
        "ix_connector_config_revisions_scope", "connector_config_revisions", ["scope"]
    )
    op.create_index(
        "ix_connector_config_revisions_state", "connector_config_revisions", ["state"]
    )

    op.create_table(
        "connector_override_values",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("revision_id", sa.String(64), nullable=False),
        sa.Column("field_key", sa.String(64), nullable=False),
        sa.Column("value_kind", sa.String(16), nullable=False),
        sa.Column("plain_value", sa.JSON(), nullable=True),
        sa.Column("ciphertext", sa.Text(), nullable=True),
        sa.Column("preview", sa.String(32), nullable=True),
        sa.Column("audit_fingerprint", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("revision_id", "field_key", name="uq_connector_override_key"),
        sa.CheckConstraint(
            "value_kind IN ('secret', 'text', 'switch', 'list')",
            name="ck_connector_override_kind",
        ),
        # 密钥只存密文、非密钥只存规范化值：这条 CHECK 是**结构性**的，
        # 它让"把密钥写进可读列"这种疏忽在写库那一刻就失败。
        sa.CheckConstraint(
            "(value_kind = 'secret' AND ciphertext IS NOT NULL AND plain_value IS NULL)"
            " OR (value_kind <> 'secret' AND plain_value IS NOT NULL AND ciphertext IS NULL)",
            name="ck_connector_override_secret_shape",
        ),
    )
    op.create_index(
        "ix_connector_override_revision", "connector_override_values", ["revision_id"]
    )

    op.create_table(
        "connector_config_heads",
        sa.Column("scope", sa.String(16), primary_key=True),
        sa.Column("active_revision_id", sa.String(64), nullable=True),
        sa.Column("candidate_revision_id", sa.String(64), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "connector_runtime_acks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("replica_id", sa.String(64), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False, server_default="prepared"),
        sa.Column("health", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("error_safe", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # 唯一键必须带 scope（两个 scope 可以同时停在第 1 代）
        sa.UniqueConstraint("scope", "generation", "replica_id", name="uq_connector_ack_generation"),
    )
    op.create_index("ix_connector_ack_generation", "connector_runtime_acks", ["generation"])
    op.create_index("ix_connector_ack_scope", "connector_runtime_acks", ["scope"])


def downgrade() -> None:
    op.drop_index("ix_connector_ack_scope", table_name="connector_runtime_acks")
    op.drop_index("ix_connector_ack_generation", table_name="connector_runtime_acks")
    op.drop_table("connector_runtime_acks")
    op.drop_table("connector_config_heads")
    op.drop_index("ix_connector_override_revision", table_name="connector_override_values")
    op.drop_table("connector_override_values")
    op.drop_index("ix_connector_config_revisions_state", table_name="connector_config_revisions")
    op.drop_index("ix_connector_config_revisions_scope", table_name="connector_config_revisions")
    op.drop_index("ix_connector_config_revisions_created", table_name="connector_config_revisions")
    op.drop_index(
        "ix_connector_config_revisions_scope_state", table_name="connector_config_revisions"
    )
    op.drop_table("connector_config_revisions")
