"""生成媒体资产表：``media_assets``

Revision ID: 0065_media_assets
Revises: 0064_persona_clear_defaults
Create Date: 2026-11-11

## 为什么需要这张表

TTS 合成出音频后必须有地方放、有记录可查、有凭据可授权。此前项目里**没有
任何本地媒体目录**：入站媒体（用户发来的图片/视频）从不落盘，`media_evidence`
的 `media_ref` 存的是**远程 URL 字符串**，用时才由连接器去下载。

生成的音频不一样——它没有上游 URL 可取，必须存在本地。于是第一次出现
"受管的本地媒体文件"，这张表就是它的索引。

## 设计要点

- **主键 = 内容的 sha256**：与磁盘文件名一致，省掉一次映射；同内容只存一份。
- **`relative_path` 而不是绝对路径**：生产端 `data_dir` 在 NAS 的
  `/vol1/@appdata/ling`，开发机在仓库的 `.data/`。绝对路径一换机器就失效。
- **`ready_at`**：可对外提供的唯一判据。写入顺序是"先落盘、后插记录"，
  所以记录出现时文件必然完整；这一列让"两步之间失败"仍能表达出来。
- **`expires_at` 可空**：空 = 不自动过期。
- **没有 `consent_revision`**：这是自己生成的产物，不存在"用户撤回了他给
  我看的东西"这回事。授权靠 `actor_id` + `window_id`（见
  `src/ling/domains/media/exports.py`），**刻意不复用** `media_evidence`
  那套入站授权——那套默认拒绝，会直接把正常播放拦掉。

## 为什么先判 `has_table`

`create_table` 本身不幂等，而开发机/CI 里这张表可能已经由
`Base.metadata.create_all` 建好过。先查再建，迁移就能在两种库上都跑通。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0065_media_assets"
down_revision: str | None = "0064_persona_clear_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("media_assets"):
        op.create_table(
            "media_assets",
            sa.Column("digest", sa.String(length=64), primary_key=True),
            sa.Column("actor_id", sa.String(length=64), nullable=False),
            sa.Column("window_id", sa.String(length=64), nullable=False),
            sa.Column("subject_id", sa.String(length=64), nullable=True),
            sa.Column("kind", sa.String(length=16), nullable=False, server_default="audio"),
            sa.Column("format", sa.String(length=16), nullable=False, server_default="mp3"),
            sa.Column(
                "mime_type", sa.String(length=96), nullable=False, server_default="audio/mpeg"
            ),
            sa.Column("relative_path", sa.String(length=256), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("sample_rate", sa.Integer(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("turn_id", sa.String(length=64), nullable=True),
            sa.Column("message_id", sa.String(length=64), nullable=True),
            sa.Column("producer", sa.String(length=64), nullable=True),
            sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            ),
        )

        # 清理任务按 expires_at 扫。
        op.create_index("ix_media_assets_expires", "media_assets", ["expires_at"])
        # 播放列表按窗口取。
        op.create_index(
            "ix_media_assets_actor_window", "media_assets", ["actor_id", "window_id"]
        )

    # 单列索引（模型里每个 `index=True` 都会要一条）。
    existing = {index["name"] for index in sa.inspect(bind).get_indexes("media_assets")}
    for column in ("actor_id", "window_id", "subject_id", "turn_id", "message_id"):
        name = f"ix_media_assets_{column}"
        if name not in existing:
            op.create_index(name, "media_assets", [column])


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("media_assets"):
        op.drop_table("media_assets")
