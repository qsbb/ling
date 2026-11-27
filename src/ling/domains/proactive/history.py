"""Read-only projection of proactive opportunities and their delivery evidence."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import (
    DeliveryReceiptRow,
    Message,
    Operation,
    ProactiveDraftBinding,
    ProactiveOpportunity,
)

_PARTICIPANT_VISIBILITY = ("subject_private", "public")
_SAFE_TERMINAL_REASONS = frozenset({
    "没有可投递的 Web 会话",
    "没有可投递的私聊会话",
    "机会已过期（时效性）",
    "投递认领已超时，平台可能已收到",
    "投递结果不确定，禁止自动重发",
    "草稿表达控制无效",
    "已在对话中分享",
    "用户撤销了这次分享",
    "用户已暂停主动分享",
    "用户已设定话题边界",
    "主动联系授权已失效",
    "共同经历已不可用",
    "原会话目标已不可用",
    "用户正在对话，稍后复核",
    "上次分享尚无回应，稍后复核",
})


async def recent_opportunities(
    session: AsyncSession, *, actor_id: str, subject_id: str, operator: bool, limit: int = 50
) -> list[dict]:
    """Return recent records without scheduling work or exposing raw platform errors.

    A retained operation id is only a hint: a rolled-back operation or an unrelated
    operation must never be presented as this opportunity's receipt.
    """
    stmt = select(ProactiveOpportunity).where(
        ProactiveOpportunity.actor_id == actor_id,
        ProactiveOpportunity.subject_id == subject_id,
    )
    if not operator:
        # A window-scoped record lacks a window id here, so ownership of that
        # window cannot be established from the opportunity alone.
        stmt = stmt.where(ProactiveOpportunity.visibility.in_(_PARTICIPANT_VISIBILITY))
    rows = list((await session.scalars(
        stmt.order_by(ProactiveOpportunity.created_at.desc(), ProactiveOpportunity.id.desc())
        .limit(limit)
    )).all())

    draft_states: dict[str, str] = {}
    if rows:
        states = (await session.execute(
            select(ProactiveDraftBinding.opportunity_id, ProactiveDraftBinding.state)
            .where(ProactiveDraftBinding.opportunity_id.in_([row.id for row in rows]))
        )).all()
        draft_states = {opportunity_id: state for opportunity_id, state in states}

    ids = [row.delivery_operation_id for row in rows if row.delivery_operation_id]
    receipts: dict[str, DeliveryReceiptRow] = {}
    if ids:
        pairs = (await session.execute(
            select(Operation, DeliveryReceiptRow)
            .join(DeliveryReceiptRow, DeliveryReceiptRow.operation_id == Operation.id)
            .where(
                Operation.id.in_(ids),
                Operation.kind == "delivery",
                Operation.actor_id == actor_id,
                Operation.subject_id == subject_id,
            )
            .order_by(DeliveryReceiptRow.observed_at.desc(), DeliveryReceiptRow.id.desc())
        )).all()
        expected = {row.id: row.delivery_operation_id for row in rows}
        for operation, receipt in pairs:
            payload = operation.payload or {}
            if not isinstance(payload, dict):
                continue
            opportunity_id = payload.get("opportunity_id")
            if (
                isinstance(opportunity_id, str)
                and payload.get("source") == "proactive"
                and expected.get(opportunity_id) == operation.id
                and opportunity_id not in receipts
            ):
                receipts[opportunity_id] = receipt

    result = []
    for row in rows:
        receipt = receipts.get(row.id)
        acknowledgement = None
        if row.acknowledged_message_id is not None:
            message = await session.get(Message, row.acknowledged_message_id)
            if (
                message is not None and message.role == "user" and message.sender_subject_id == row.subject_id
                and message.window_id == row.target_window_id
                and message.retracted_at is None and message.memory_hidden_at is None
            ):
                acknowledgement = row.acknowledgement
        result.append({
            "opportunity_id": row.id,
            "kind": row.kind,
            "reason": row.reason,
            "reason_code": row.reason_code,
            "has_source": row.source_event_id is not None,
            **({
                "activity_id": (row.source_event_id or "").removeprefix("activity:"),
                "related_memory_id": row.related_memory_id,
                "target_window_id": row.target_window_id,
                "acknowledgement": acknowledgement,
            } if row.related_memory_id else {}),
            "state": "candidate" if row.state in {"drafting", "bound_candidate"} else row.state,
            "draft_state": draft_states.get(row.id),
            "draft_origin": (
                "bound" if draft_states.get(row.id) == "ready" else "unknown"
            ) if row.draft_text is not None else None,
            "created_at": row.created_at.isoformat(),
            "sent_at": row.sent_at.isoformat() if row.sent_at else None,
            "gate": {
                "allowed": row.last_gate_allowed,
                "reasons": row.last_gate_reasons or [],
                "checked_at": row.last_gate_at.isoformat() if row.last_gate_at else None,
            },
            "delivery_state": row.delivery_state,
            # Web text delivery is recorded with this sentinel when the message
            # and settlement are committed. It has no separate platform receipt.
            "web_saved": row.state == "sent" and row.rejected_reason == "web",
            "terminal_reason": (
                row.rejected_reason if row.rejected_reason in _SAFE_TERMINAL_REASONS
                else None
            ),
            "receipt": (
                {"state": receipt.state, "channel": receipt.channel,
                 "observed_at": receipt.observed_at.isoformat()}
                if receipt else None
            ),
        })
    return result
