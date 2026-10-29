"""主动机会的真实投递。**默认关闭。**

## 为什么默认关

主动联系会真的打扰人。前面几步（收集、门控、生成草稿）都可以在影子模式下
验证，但投递不行——它一旦发生就是"她突然找你"。

所以这里用 `DialoguePolicy.proactive_delivery_enabled`（默认 False）挡着：
用户先看几天草稿、确认语气合适，再自己打开。

## 目标窗口怎么找

平台侧的 `window_ref`（如 `onebot-e2e:private:20002`）存在
`Window.title` 里——这是 bridge 建窗口时就定下的约定。
投递需要的是它，不是 `Window.id`。

只挑**私聊**窗口（`visibility='subject_private'`）：主动找你时往群里发
一句"在吗"是不可接受的。

## 送达判据

只有 `DeliveryOutcome.delivered` 为真才标 `sent`。`uncertain` 留给对账，
**不盲目重发**——重复发一条"在吗"比没发更糟。
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import ProactiveOpportunity, Window, utcnow

logger = logging.getLogger("ling.domains.proactive.dispatch")

MAX_PER_ROUND = 3


async def _target_window_ref(
    session: AsyncSession, *, subject_id: str
) -> str | None:
    """这个人在 QQ 上最近活跃的**私聊**窗口的平台 ref。"""
    stmt = (
        select(Window)
        .where(
            Window.subject_id == subject_id,
            Window.channel == "onebot",
            Window.visibility == "subject_private",
            Window.archived.is_(False),
        )
        .order_by(Window.last_message_at.desc().nulls_last())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalars().first()
    if row is None:
        return None
    # 群窗口的 title 也是 window_ref，但私聊条件已经挡住了
    return str(row.title or "") or None


async def dispatch_pending(
    session: AsyncSession,
    *,
    actor_id: str,
    connector: object,
    config_reader: object,
    subject_timezone: str | None = None,
    now: datetime | None = None,
    limit: int = MAX_PER_ROUND,
) -> dict[str, int | str]:
    """把有草稿的候选真的发出去。

    返回统计，便于 worker 日志与运维观察。
    """
    from ling.capabilities.delivery import DeliveryService
    from ling.domains.proactive.service import ProactivityService
    from ling.ports.channel import OutboundMessage

    moment = now or utcnow()
    try:
        effective = await config_reader.effective()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - 读不到配置就不发
        logger.info("主动投递跳过：配置不可用（%s）", type(exc).__name__)
        return {"skipped": "config_unavailable"}

    if not bool(effective.dialogue.proactive_delivery_enabled):
        # 默认路径：只生成不发送。要让用户能看见"没发"，而不是以为发了。
        return {"skipped": "disabled"}

    service = ProactivityService(session, actor_id=actor_id)
    rows = (
        (
            await session.execute(
                select(ProactiveOpportunity)
                .where(
                    ProactiveOpportunity.actor_id == actor_id,
                    ProactiveOpportunity.state == "candidate",
                    ProactiveOpportunity.draft_text.is_not(None),
                    ProactiveOpportunity.expires_at > moment,  # type: ignore[arg-type]
                )
                .order_by(ProactiveOpportunity.created_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    sent = failed = rejected = 0
    gate_cache: dict[str, object] = {}
    for row in rows:
        gate = gate_cache.get(row.subject_id)
        if gate is None:
            gate = await service.gate(
                subject_id=row.subject_id,
                now=moment,
                subject_timezone=subject_timezone,
                channel_can_proactive=True,
            )
            gate_cache[row.subject_id] = gate
        if not getattr(gate, "allowed", False):
            # 门控没过就留着，不标 rejected——它只是"现在还不是时候"
            continue

        window_ref = await _target_window_ref(session, subject_id=row.subject_id)
        if window_ref is None:
            await service.mark(
                row.id, state="rejected", reason="没有可投递的私聊会话"
            )
            rejected += 1
            continue

        delivery = DeliveryService(session)
        try:
            outcome = await delivery.deliver(
                connector=connector,  # type: ignore[arg-type]
                message=OutboundMessage(
                    operation_id=f"proactive:{row.id}",
                    window_ref=window_ref,
                    text=str(row.draft_text),
                    is_group=False,
                ),
                actor_id=actor_id,
                subject_id=row.subject_id,
                require_proactive=True,
            )
        except Exception as exc:  # noqa: BLE001 - 一次失败不该中断整轮
            logger.warning("主动投递异常 %s: %s", row.id, type(exc).__name__)
            failed += 1
            continue

        if outcome.delivered:
            await service.mark(row.id, state="sent")
            sent += 1
        else:
            # 不确定 ≠ 失败：结果未知只能等对账，**不能盲目重发**。
            # 重复发一条"在吗"比没发更糟。
            await service.mark(
                row.id, state="failed", reason=outcome.detail or "投递未成功"
            )
            failed += 1

    return {"sent": sent, "failed": failed, "rejected": rejected, "considered": len(rows)}


__all__ = ["MAX_PER_ROUND", "dispatch_pending"]
