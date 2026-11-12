"""媒体逻辑授权与在途租约。

Revision ID: 0068_media_asset_grants
Revises: 0067_connector_instances
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0068_media_asset_grants"
down_revision = "0067_connector_instances"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("media_asset_grants"):
        op.create_table(
            "media_asset_grants",
            sa.Column("asset_id", sa.String(64), primary_key=True),
            sa.Column(
                "blob_digest", sa.String(64), sa.ForeignKey("media_assets.digest"), nullable=False
            ),
            sa.Column("actor_id", sa.String(64), nullable=False),
            sa.Column("window_id", sa.String(64), nullable=False),
            sa.Column("subject_id", sa.String(64), nullable=True),
            sa.Column("purpose", sa.String(32), nullable=False, server_default="tts_output"),
            sa.Column("turn_id", sa.String(64), nullable=True),
            sa.Column("message_id", sa.String(64), nullable=True),
            sa.Column("producer", sa.String(64), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index("ix_media_asset_grants_blob_digest", "media_asset_grants", ["blob_digest"])
        op.create_index(
            "ix_media_asset_grants_owner", "media_asset_grants", ["actor_id", "window_id"]
        )
        op.create_index("ix_media_asset_grants_expires", "media_asset_grants", ["expires_at"])
        op.create_index("ix_media_asset_grants_message", "media_asset_grants", ["message_id"])
    if not sa.inspect(bind).has_table("media_asset_leases"):
        op.create_table(
            "media_asset_leases",
            sa.Column("lease_id", sa.String(64), primary_key=True),
            sa.Column(
                "asset_id",
                sa.String(64),
                sa.ForeignKey("media_asset_grants.asset_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_media_asset_leases_asset_id", "media_asset_leases", ["asset_id"])
        op.create_index("ix_media_asset_leases_expires", "media_asset_leases", ["expires_at"])
    # 老内容只继承既有单一归属。数据回填可重复执行。
    op.execute(
        sa.text("""
        INSERT INTO media_asset_grants (
            asset_id, blob_digest, actor_id, window_id, subject_id, purpose,
            turn_id, message_id, producer, expires_at, deleted_at, note, created_at, updated_at
        )
        SELECT 'asset_' || md5(digest), digest, actor_id, window_id, subject_id, 'tts_output',
               turn_id, message_id, producer, expires_at, deleted_at, note, created_at, updated_at
          FROM media_assets
         WHERE NOT EXISTS (
            SELECT 1 FROM media_asset_grants g WHERE g.blob_digest = media_assets.digest
         )
        ON CONFLICT (asset_id) DO NOTHING
    """)
    )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("media_asset_leases"):
        op.drop_table("media_asset_leases")
    if sa.inspect(bind).has_table("media_asset_grants"):
        op.drop_table("media_asset_grants")
