"""生成媒体资产的表定义：``media_assets``。

## 为什么表定义不在 `infra/models.py`

`models.py` 是全库 schema 的权威清单，但它此刻正被另一个任务改动
（避免同文件冲突）。这份表定义因此分出来单独放，**行为完全一样**：
用的还是同一个 `Base`（`ling.infra.db.Base`），所以 `metadata.create_all`
与 Alembic autogenerate 都认得它。

导入顺序要求：任何要用这张表的地方（迁移检查、测试建表、资产服务）都必须
`import ling.infra.media_assets` 一次，让模块级 `class` 定义把自己注册进
`Base.metadata`。`assets.py` 已经在顶部做了这件事。

## 这张表记什么

一行 = **一个已落盘的生成媒体文件**。核心不变量：

1. **`digest` 唯一**。文件名就是内容的 sha256，同一份内容只存一份、只有
   一行记录。两个不同的 TTS 请求产出同样的字节（同样的文本、同样的音色、
   同样的参数）时，第二行不该出现——否则清理任务删掉其中一行就会误删
   另一行还在用的文件。
2. **`relative_path` 而不是绝对路径**。绝对路径在换机器/换挂载点后失效，
   而生产端的 `data_dir` 与开发机不同（见项目约定：生产在 NAS 的
   `/vol1/@appdata/ling`）。
3. **`ready_at` 是"这个文件真的能读了"的唯一判据**。保存分两步：先落盘
   （原子），再插记录。插进来的记录默认就是 ready——因为落盘在前，记录
   出现时文件一定完整。保留这个列是为了给"两步之间失败"留下可观测状态，
   以及让未来的"先占位后流式写"仍能表达"还没好"。
4. **`expires_at` 可空**：空 = 不自动过期（用户明确要留着的产物）。
5. **不记录是否已发送**。发送状态属于投递域（`operations` /
   `delivery_receipts`）；在这里也记一份会出现两份真相。

## 权限列（与入站证据无关）

`actor_id` + `window_id` 是这套媒体的授权凭据，语义见 `exports.py`。
这里**故意没有** `consent_revision`：媒体是我们自己生成的，不存在"用户
撤回了他给我看的东西"这回事，撤权只会让它过期或被删。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
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


__all__ = ["MediaAssetRow", "utcnow"]
