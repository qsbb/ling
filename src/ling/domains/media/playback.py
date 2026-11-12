"""SP03 Web 音频播放的**读取域**：消息 → 音频资产，以及"谁听过"。

## 这个模块为什么存在（而不是把逻辑塞进路由）

同一套判定被三处用到：

1. `GET /v1/sessions/{id}/voice-messages`（会话里哪些消息是语音）
2. `GET /v1/messages/{id}/voice`（单条消息的播放地址）
3. `POST /v1/messages/{id}/playback`（记一次播放）

三处如果各写一遍归属判定，早晚会有一处写成 200 而不是 404——而这类分叉
恰恰是越权泄漏的唯一入口。所以归属/可见性判定只有一份实现在这里。

## 「播放」与「送达」为什么必须分开记

这是本任务最关键的设计要求，也是 `infra/media_assets.py` 早就写下的约定：
那张表**刻意不记录是否已发送**，因为发送状态属于投递域
（`operations` / `delivery_receipts`）。反过来也一样：

| 问题 | 权威来源 | 语义 |
|---|---|---|
| 「这条她真的发出去了吗」 | `delivery_receipts.state` | 渠道回了回执 |
| 「主人在 Web 上真听到了吗」 | `media_play_events`（本模块） | 人耳听到了 |

两者回答的是**不同的问题**，所以不能在同一个字段里表示"好像都做了"。

三个具体后果，写在这里防止后人合并它们：

- **送达 ≠ 播放**：群里她发了一条语音，渠道回了 `sent`，但主人一次都没点开。
  `delivery_state=sent` + 没有播放行 才是事实全貌。
- **播放 ≠ 送达**：主人可能在本机 Web 上自己点开听（渠道发送失败时也能听）。
  不能因为"播过了"就把 operation 标成成功——那会让一次失败的发送被永久掩盖。
- **一次播放不是一次投递**：拖动进度条会触发多个 `Range` 请求。如果按请求
  计数，"听过几次"就会等于"点了多少下"。所以播放事件按 `(message_id,
  actor_subject)` 去重并累加 `play_count`，而不是一次请求一行。

## 为什么播放记录里要存 `asset_digest` 而不是只要 `message_id`

`media_assets.digest` 是内容哈希，同一句语音重新合成会得到不同 digest。播放
记录必须钉住**当时听到的那一份**：否则明天换了音色，昨天的播放记录看起来
就像是在播今天的音频。`message_id` 只用来回答"这条消息被听过没有"，两者
都存。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.media.assets import AssetView, _to_view
from ling.infra.media_assets import MediaAssetGrantRow, MediaAssetRow, utcnow
from ling.infra.media_playback import MediaPlayEventRow, play_state_for
from ling.infra.models import Message, Window

logger = logging.getLogger("ling.domains.media.playback")

#: 只有 **她说的** 消息才可能有语音。用户的语音消息属于入站媒体
#: （`media_evidence`），走另一套授权，**不在这里**——混进来会让"播放"这
#: 个动作同时表示"她说的"和"你说的"，统计立刻失去意义。
VOICE_MESSAGE_ROLE = "actor"


class VoiceNotFound(Exception):
    """消息/资产不存在、不可见、或者不是语音。

    四种情况**故意合并成同一个异常**：区分开就等于告诉调用方"这条消息存在，
    只是不是语音"或"这个资产存在，只是你没权限"，可以用来枚举。
    """


@dataclass(frozen=True)
class VoiceAsset:
    """一条语音消息对应的可播放资产。**不含磁盘路径**。"""

    message_id: str
    window_id: str
    digest: str
    mime_type: str
    format: str
    size_bytes: int
    duration_ms: int | None
    sample_rate: int | None
    #: 消息正文。用户要求后台看到「语音标签 + 文字」，所以文字必须跟着来，
    #: 但**不在附件里再存一份**——两份正文必然出现"该信哪个"。
    text: str
    created_at: datetime
    expires_at: datetime | None
    delivered: bool
    played: bool
    play_count: int
    last_played_at: datetime | None
    asset_id: str = ""


def _owner_only(window: Window | None, subject_id: str) -> bool:
    """窗口归属判定。群窗口按设计归属主人，所以群里她发的语音主人也能听。

    返回 ``bool`` 而不是抛异常：调用方要把"不存在/不是我的"合并成同一个
    404，异常类型分叉会诱导调用方把它们分别处理。
    """
    return window is not None and window.subject_id == subject_id


async def _window_of(session: AsyncSession, window_id: str) -> Window | None:
    return await session.get(Window, window_id)


async def _assets_for_messages(
    session: AsyncSession, message_ids: list[str]
) -> dict[str, AssetView]:
    if not message_ids:
        return {}
    current = utcnow()
    rows = (await session.execute(select(MediaAssetRow, MediaAssetGrantRow).join(
        MediaAssetGrantRow, MediaAssetGrantRow.blob_digest == MediaAssetRow.digest
    ).where(
        MediaAssetGrantRow.message_id.in_(message_ids),
        MediaAssetGrantRow.purpose == "tts_output", MediaAssetRow.kind == "audio",
    ).order_by(MediaAssetGrantRow.created_at))).all()
    picked: dict[str, AssetView] = {}
    for blob, grant in rows:
        if grant.message_id is not None and grant.is_live(now=current) and blob.ready_at is not None:
            picked.setdefault(grant.message_id, _to_view(blob, grant))
    legacy = (await session.scalars(select(MediaAssetRow).where(
        MediaAssetRow.message_id.in_(message_ids), MediaAssetRow.kind == "audio",
    ).order_by(MediaAssetRow.created_at))).all()
    for blob in legacy:
        if blob.message_id is not None and blob.is_live(now=current):
            picked.setdefault(blob.message_id, _to_view(blob))
    return picked


async def _delivered_message_ids(session: AsyncSession, message_ids: list[str]) -> set[str]:
    """哪些消息**已经真的送到渠道**了（读投递域，不写它）。

    这里只读 `delivery_receipts` 与 `operations` 的关系，不保存任何派生状态：
    送达是渠道回执的事实，播放是我们观察到的事实，两者各自有权威来源。
    """
    if not message_ids:
        return set()
    from ling.infra.models import DeliveryReceiptRow, Operation

    rows = (
        await session.execute(
            select(Operation.payload, DeliveryReceiptRow.state)
            .join(DeliveryReceiptRow, DeliveryReceiptRow.operation_id == Operation.id)
            .where(Operation.kind.in_(("speech.deliver", "delivery.send", "message.send")))
        )
    ).all()
    delivered: set[str] = set()
    for payload, state in rows:
        if state not in ("sent", "delivered"):
            continue
        if not isinstance(payload, dict):
            continue
        mid = payload.get("message_id")
        if isinstance(mid, str) and mid in message_ids:
            delivered.add(mid)
    return delivered


async def _play_state(
    session: AsyncSession, message_ids: list[str], *, subject_id: str
) -> dict[str, tuple[bool, int, datetime | None]]:
    """批量取播放状态：`message_id → (played, play_count, last_played_at)`。"""
    result = await session.execute(
        select(MediaPlayEventRow).where(
            MediaPlayEventRow.message_id.in_(message_ids or [""]),
            MediaPlayEventRow.subject_id == subject_id,
        )
    )
    rows = play_state_for(result.scalars())
    return {
        message_id: (bool(row.play_count), int(row.play_count), row.last_played_at)
        for message_id, row in rows.items()
    }


async def load_voice_assets(
    session: AsyncSession, *, window_id: str, subject_id: str, message_ids: list[str] | None = None
) -> list[VoiceAsset]:
    """一个会话里所有可播放的语音消息（可按 id 过滤）。

    非 owner 窗口直接返回空列表——调用方会把"空"和"不存在"渲染成同一句话。
    """
    window = await _window_of(session, window_id)
    if not _owner_only(window, subject_id):
        return []

    query = select(Message).where(
        Message.window_id == window_id,
        Message.role == VOICE_MESSAGE_ROLE,
    )
    if message_ids is not None:
        query = query.where(Message.id.in_(message_ids))
    messages = list((await session.execute(query.order_by(Message.sequence))).scalars())
    if not messages:
        return []

    ids = [m.id for m in messages]
    assets = await _assets_for_messages(session, ids)
    delivered = await _delivered_message_ids(session, ids)
    played = await _play_state(session, ids, subject_id=subject_id)

    out: list[VoiceAsset] = []
    for message in messages:
        asset = assets.get(message.id)
        if asset is None:
            continue
        is_played, count, last_at = played.get(message.id, (False, 0, None))
        out.append(
            VoiceAsset(
                message_id=message.id,
                window_id=message.window_id,
                digest=asset.digest,
                asset_id=asset.asset_id,
                mime_type=asset.mime_type,
                format=asset.format,
                size_bytes=asset.size_bytes,
                duration_ms=asset.duration_ms,
                sample_rate=asset.sample_rate,
                text=message.text or "",
                created_at=message.created_at,
                expires_at=asset.expires_at,
                delivered=message.id in delivered,
                played=is_played,
                play_count=count,
                last_played_at=last_at,
            )
        )
    return out


async def load_voice_for_message(
    session: AsyncSession, *, message_id: str, subject_id: str
) -> VoiceAsset:
    """单条消息的语音资产。不可见/不是语音 → `VoiceNotFound`（404）。"""
    message = await session.get(Message, message_id)
    if message is None or message.role != VOICE_MESSAGE_ROLE:
        raise VoiceNotFound("消息不存在")
    if not _owner_only(await _window_of(session, message.window_id), subject_id):
        raise VoiceNotFound("消息不存在")
    found = await load_voice_assets(
        session,
        window_id=message.window_id,
        subject_id=subject_id,
        message_ids=[message_id],
    )
    if not found:
        raise VoiceNotFound("消息不存在")
    return found[0]


async def record_playback(
    session: AsyncSession,
    *,
    message_id: str,
    subject_id: str,
    asset_digest: str,
    window_id: str,
    position_ms: int | None = None,
    completed: bool = False,
) -> MediaPlayEventRow:
    """记一次播放（**幂等累加**，不是一次请求一行）。

    为什么不存"每次播放一行"：拖动进度条会发多个 Range 请求，一次"听一遍"
    可能对应 10 次 HTTP。按请求计数会让「听过几次」变成「点了多少下」——
    那是网络层的事实，不是人的事实。

    所以这一行是「这个人听过这条」的**唯一一行**，`play_count` 累加、
    `last_played_at` 更新。要"每次播放的明细"时再加明细表，而不是把这个
    汇总表拆散。

    并发：两个标签页同时点播放会各自读到 `play_count=n` 再都写 `n+1`（丢一次）。
    播放计数是**统计量不是凭据**，丢一次不影响任何判定，不值得为它加行锁；
    真正需要严格性的地方（投递、合成）用的是幂等键抢占，见
    `domains/speech/orchestration.py`。
    """
    row = (
        await session.execute(
            select(MediaPlayEventRow).where(
                MediaPlayEventRow.message_id == message_id,
                MediaPlayEventRow.subject_id == subject_id,
            )
        )
    ).scalar_one_or_none()

    now = utcnow()
    if row is None:
        row = MediaPlayEventRow(
            id=f"play_{message_id}_{subject_id}"[:96],
            message_id=message_id,
            asset_digest=asset_digest,
            window_id=window_id,
            subject_id=subject_id,
            play_count=1,
            first_played_at=now,
            last_played_at=now,
            position_ms=position_ms,
            completed=completed,
        )
        session.add(row)
    else:
        row.play_count = int(row.play_count or 0) + 1
        row.last_played_at = now
        # 钉住"最近一次听到的那一份"：音色换了之后，记录要跟着换。
        row.asset_digest = asset_digest
        if position_ms is not None:
            row.position_ms = position_ms
        if completed:
            row.completed = True
    await session.flush()
    return row


__all__ = [
    "VOICE_MESSAGE_ROLE",
    "VoiceAsset",
    "VoiceNotFound",
    "load_voice_assets",
    "load_voice_for_message",
    "record_playback",
]
