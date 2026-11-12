"""媒体内容、逻辑资产授权与租约。

media_assets 保留内容摘要及旧引用的原始归属。
media_asset_grants 为新调用提供独立 asset_id、窗口授权和保留期。
同一份磁盘内容可被多个独立逻辑资产引用。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ling.infra.db import Base
from ling.infra.models import TimestampMixin, utcnow


class MediaAssetRow(Base, TimestampMixin):
    """一个已生成的媒体文件（当前只用于 TTS 音频，列不绑定具体来源）。"""

    __tablename__ = "media_assets"
    __table_args__ = (
        # 清理任务按 `expires_at` 扫，所以它必须有索引。
        Index("ix_media_assets_expires", "expires_at"),
        # 取"这个窗口里的产物"是播放列表的主查询。
        Index("ix_media_assets_actor_window", "actor_id", "window_id"),
    )

    #: 主键就是内容摘要 —— 与文件名一致，省掉一次映射。
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)

    #: 产出它的角色/用户身份。授权凭据之一。
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    #: 产出它的会话窗口。授权凭据之二。
    window_id: Mapped[str] = mapped_column(String(64), index=True)
    #: 生成者的 subject（角色自己 = 她的 subject）。可空：系统级产物没有。
    subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    #: audio / image / video。首版只写 audio，但列不限定死。
    kind: Mapped[str] = mapped_column(String(16), default="audio")
    #: 具体格式：mp3 / wav / ogg / silk …（由产出方声明，不靠猜扩展名）。
    format: Mapped[str] = mapped_column(String(16), default="mp3")
    #: MIME 类型。播放与渠道都用它决定怎么处理。
    mime_type: Mapped[str] = mapped_column(String(96), default="audio/mpeg")

    #: 相对 `data_dir` 的路径（`generated_media/ab/abcd…`）。
    relative_path: Mapped[str] = mapped_column(String(256))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    #: 采样率（Hz）。音频才有，其他可空。
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 时长（毫秒）。**毫秒整数而不是浮点秒**：浮点在库里比较/求和会出现
    #: 2.9999999 这类值，清理与统计都不需要这个麻烦。
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: 关联的对话轮次 / 消息。可空：不是所有产物都挂在某一轮上。
    turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: 产出方标识（如 `tts.edge`），用于排查"哪个后端出的文件"。
    producer: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: 可读时间。为空表示"还没写完/没校验完"，对外一律当不存在。
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: 过期时间。为空 = 不自动清理。
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: 软删除时间。删除先标记、后删文件（见 `assets.py` 的删除语义）。
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: 备注/失败原因。只用于人工排查，不参与任何判定。
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    def is_live(self, *, now: datetime | None = None) -> bool:
        """是否可对外提供：已 ready、未删除、未过期。"""
        current = now or utcnow()
        if self.ready_at is None or self.deleted_at is not None:
            return False
        return not (self.expires_at is not None and self.expires_at <= current)


class MediaAssetGrantRow(Base, TimestampMixin):
    """一个窗口对内容的逻辑资产引用。"""

    __tablename__ = "media_asset_grants"
    __table_args__ = (
        Index("ix_media_asset_grants_owner", "actor_id", "window_id"),
        Index("ix_media_asset_grants_expires", "expires_at"),
        Index("ix_media_asset_grants_message", "message_id"),
    )

    asset_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    blob_digest: Mapped[str] = mapped_column(
        String(64), ForeignKey("media_assets.digest"), index=True
    )
    actor_id: Mapped[str] = mapped_column(String(64))
    window_id: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    purpose: Mapped[str] = mapped_column(String(32), default="tts_output")
    turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    producer: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    def is_live(self, *, now: datetime | None = None) -> bool:
        current = now or utcnow()
        return self.deleted_at is None and (self.expires_at is None or self.expires_at > current)


class MediaAssetLeaseRow(Base):
    """在途使用者持有的有期限租约。"""

    __tablename__ = "media_asset_leases"
    __table_args__ = (Index("ix_media_asset_leases_expires", "expires_at"),)

    lease_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("media_asset_grants.asset_id", ondelete="CASCADE"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


__all__ = ["MediaAssetRow", "MediaAssetGrantRow", "MediaAssetLeaseRow", "utcnow"]
