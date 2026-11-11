"""Web 播放事件表：``media_play_events``。

## 为什么它必须与「送达」分开

| 问题 | 权威来源 |
|---|---|
| 「这条她真的发到渠道了吗」 | `delivery_receipts`（渠道回执） |
| 「主人在 Web 上真的听了吗」 | **这张表** |

两件事由不同的人、在不同的时刻、通过不同的通道确认。混成一个字段之后，
两个问题就都回答不了了：

- 渠道回执 `sent` 但主人一直没点开 → 混起来的字段会说"已完成"，而"用户听没听"
  这个真正想知道的问题没有答案；
- 主人自己在本机点开听（甚至渠道发送失败时也能听）→ 混起来的字段会让一次
  失败的投递看起来成功了，掩盖真实故障。

所以这张表**只**记播放，且**只**由 API 写入；`delivery_receipts` **只**记送达，
且只由投递层写入。交叉引用靠 `message_id` / `asset_digest`，不靠改对方的列。

## 为什么是"一行一人一消息"而不是一请求一行

拖动进度条会发多个 `Range` 请求，一次"听一遍"可能对应十几次 HTTP。按请求
计数，「听过几次」就变成了「点了多少下」。所以主键语义是
`(message_id, subject_id)`，`play_count` 累加、`last_played_at` 更新。
需要逐次明细时再建明细表，而不是把这张汇总表拆散。

## 为什么同时存 `message_id` 与 `asset_digest`

- `message_id`：回答"这条消息被听过没有"（会话页要的是这个）；
- `asset_digest`：钉住**当时听到的那一份音频**。同一句话重新合成会得到不同
  digest，只存 `message_id` 的话，明天换了音色，昨天的播放记录看起来像是在
  播今天的音频。

## 保留期

不设过期：播放记录是"用户行为历史"，被它引用的音频过期删除之后，记录本身
仍然有意义（"她那天发过语音，主人听过"）。文件删了不代表事实没发生。

## 为什么这张表不在 `infra/models.py`

与 `media_assets` 同样的理由：`models.py` 是全库 schema 的权威清单，但它
此刻正被 SP04/SP05 改动。分文件放**行为完全一样**（同一个 `Base`），只是
避免同文件冲突。导入顺序：任何要用这张表的地方（迁移检查、测试建表、服务）
都必须 `import ling.infra.media_playback` 一次。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ling.infra.db import Base
from ling.infra.models import TimestampMixin, utcnow


class MediaPlayEventRow(Base, TimestampMixin):
    """里「某人听过某条语音」的**唯一**一行。"""

    __tablename__ = "media_play_events"
    __table_args__ = (
        # 会话页会问"这个窗口里哪些听过"，按窗口 + 消息查最快。
        Index("ix_media_play_window", "window_id", "last_played_at"),
    )

    #: 主键由 `(message_id, subject_id)` 派生，保证"一行一人一消息"。
    #: 不用复合主键是因为这张表会被别的表按单列 id 引用（审计/导出）；
    #: 派生规则在 `domains/media/playback.py::record_playback` 一处。
    id: Mapped[str] = mapped_column(String(96), primary_key=True)

    #: 被播放的那条消息。**不是**播放这次请求的消息——拖动进度条是同一个。
    message_id: Mapped[str] = mapped_column(String(64), index=True)
    #: 当时听到的那一份音频（`media_assets.digest`）。换音色后跟着变。
    asset_digest: Mapped[str] = mapped_column(String(64))
    window_id: Mapped[str] = mapped_column(String(64), index=True)
    #: 谁听的（`subjects.id`）。多用户部署时播放记录必须分人，
    #: 否则"她发过语音、有人听过"会变成每个人都能看到"别人听过"。
    subject_id: Mapped[str] = mapped_column(String(64), index=True)

    #: 累计听过几次。**统计量不是凭据**：丢一次不影响任何判定。
    play_count: Mapped[int] = mapped_column(Integer, default=1)
    first_played_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_played_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    #: 最后一次听到的位置（毫秒）。用来回答"他听完没有"，不用于断点续播
    #: （音频很短，续播反而让人以为少了一段）。
    position_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 是否听到过结尾（`ended` 事件）。**只有明确收到 ended 才为真**——
    #: 把"播到 99%"当成听完，会让"他根本没听完"这个信号消失。
    completed: Mapped[bool] = mapped_column(Boolean, default=False)


def play_state_for(rows: Iterable[MediaPlayEventRow]) -> dict[str, MediaPlayEventRow]:
    """把行摊平成 `message_id → 行`（同一 message+subject 只会有一行）。"""
    return {row.message_id: row for row in rows}


__all__ = ["MediaPlayEventRow", "play_state_for"]
