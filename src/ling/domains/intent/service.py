"""意图队列：她自己产生的「想做什么」。

## 它解决什么

「回不回」已经是选择了（`kernel/autonomy.py`），但那只覆盖**当下这一轮**。
真人还会有跨轮的念头：「等她上线要问那个事」「回头再提一句」。

没有这一层，她每轮都从零开始想，记不住自己刚才决定"稍后再说"。

## 三条硬规则

1. **没有来源的意图不许产生。** `source_ref` 必须有具体引用
   （`msg:xxx` / `activity:xxx` / `commit:xxx`）。
   "凭空想打个招呼"正是路线图明确排除的假自主——它没有来源，所以进不来。
2. **念头不是承诺。** 到期没做只是 `expired`，不补发、不道歉。
   这和 `agenda_plans`（承诺）是两回事。
3. **同一件事只产生一条。** `dedupe_key` 唯一，反复想不会堆出十条。

## 与 proactive 的关系

到期的意图**不直接发消息**，而是变成一条 `ProactiveOpportunity`，
继续过 `ProactivityService.gate()` 那套硬门（勿扰/日限额/渠道能力）。
影子模式仍然有效——意图到期只是"更想说了"，不是"绕过硬门"。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import IntentRow, utcnow
from ling.infra.security import new_id

KINDS = ("reply", "note", "act", "reach_out")
SOURCE_KINDS = ("message", "activity", "event", "self")

STATE_OPEN = "open"
STATE_CONSUMED = "consumed"
STATE_DROPPED = "dropped"
STATE_EXPIRED = "expired"

# 默认寿命：想说的话过一天还没说出口，通常就不必再说了。
DEFAULT_TTL_HOURS = 24


class IntentError(Exception):
    """意图不合法。都是**编码错误**，不是运行期故障。"""


@dataclass(frozen=True)
class IntentView:
    intent_id: str
    kind: str
    goal: str
    rationale: str | None
    source_kind: str
    source_ref: str | None
    subject_id: str | None
    window_id: str | None
    visibility: str
    not_before: datetime | None
    expires_at: datetime | None


class IntentService:
    """意图的唯一写入方。"""

    def __init__(self, session: AsyncSession, *, actor_id: str) -> None:
        self._session = session
        self._actor_id = actor_id

    async def propose(
        self,
        *,
        goal: str,
        source_kind: str,
        source_ref: str,
        rationale: str | None = None,
        subject_id: str | None = None,
        window_id: str | None = None,
        kind: str = "reach_out",
        not_before: datetime | None = None,
        expires_at: datetime | None = None,
        confidence: float = 0.5,
        visibility: str = "subject_private",
        origin: str = "model_inference",
        dedupe_key: str | None = None,
    ) -> IntentView | None:
        """登记一个念头。重复（同 dedupe_key）返回 ``None``，不报错。

        **没有 source_ref 直接拒绝**——这条不是风格要求，是防假自主的
        最后一道闸：说不出"为什么会想到这个"的意图不允许存在。
        """
        if not (source_ref or "").strip():
            raise IntentError(
                "意图必须引用具体来源（source_ref）；没有来源的念头是凭空打扰"
            )
        if source_kind not in SOURCE_KINDS:
            raise IntentError(f"未知来源类型: {source_kind}")
        if kind not in KINDS:
            raise IntentError(f"未知意图类型: {kind}")
        if not goal.strip():
            raise IntentError("意图必须有目标（goal）")

        key = dedupe_key or f"{source_kind}:{source_ref}:{kind}"
        existing = (
            await self._session.execute(
                select(IntentRow).where(IntentRow.dedupe_key == key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return None

        now = utcnow()
        row = IntentRow(
            id=new_id("intent"),
            actor_id=self._actor_id,
            subject_id=subject_id,
            window_id=window_id,
            kind=kind,
            goal=goal.strip()[:2000],
            rationale=(rationale or "").strip()[:2000] or None,
            source_kind=source_kind,
            source_ref=source_ref.strip()[:128],
            origin=origin,
            visibility=visibility,
            state=STATE_OPEN,
            not_before=not_before or now,
            expires_at=expires_at or (now + timedelta(hours=DEFAULT_TTL_HOURS)),
            confidence=max(0.0, min(1.0, confidence)),
            dedupe_key=key,
        )
        self._session.add(row)
        await self._session.flush()
        return _view(row)

    async def due(self, *, now: datetime | None = None, limit: int = 20) -> list[IntentView]:
        """到点该兑现的念头。**过期的不会出现在这里**——过期是放弃，不是延后。"""
        moment = now or utcnow()
        rows = (
            (
                await self._session.execute(
                    select(IntentRow)
                    .where(
                        IntentRow.actor_id == self._actor_id,
                        IntentRow.state == STATE_OPEN,
                        IntentRow.not_before <= moment,
                        IntentRow.expires_at > moment,
                    )
                    .order_by(IntentRow.not_before)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_view(row) for row in rows]

    async def expire_overdue(self, *, now: datetime | None = None) -> int:
        """把过期未兑现的念头标记为 expired。返回条数。

        **不补发**：过期的念头过期就是过期了。补发等于"攒了一批打扰
        一起丢出去"，比不发更糟。
        """
        moment = now or utcnow()
        rows = (
            (
                await self._session.execute(
                    select(IntentRow).where(
                        IntentRow.actor_id == self._actor_id,
                        IntentRow.state == STATE_OPEN,
                        IntentRow.expires_at <= moment,
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.state = STATE_EXPIRED
            row.updated_at = moment
        if rows:
            await self._session.flush()
        return len(rows)

    async def mark(self, intent_id: str, state: str, *, reason: str | None = None) -> None:
        """推进念头状态。``consumed`` 表示已经变成过一次主动机会。"""
        if state not in (STATE_CONSUMED, STATE_DROPPED, STATE_EXPIRED):
            raise IntentError(f"未知目标状态: {state}")
        row = await self._session.get(IntentRow, intent_id)
        if row is None or row.actor_id != self._actor_id:
            return
        row.state = state
        if reason:
            # 追加而不是覆盖——原有的 rationale 是"为什么想到"，
            # 这里是"后来怎么处理的"，两者都值得留。
            row.rationale = f"{row.rationale or ''}\n[{state}] {reason}".strip()
        row.updated_at = utcnow()
        await self._session.flush()


async def record_followup(
    session: AsyncSession,
    *,
    actor_id: str,
    followup: object,
    subject_id: str | None,
    window_id: str | None,
    source_ref: str,
) -> IntentView | None:
    """把「她刚才说想稍后再提」落成一条意图。

    ``source_ref`` 必须是**具体**来源（如 ``msg:<message_id>``）。
    说不出为什么会有这个念头的，一律不产生——这是防假自主的最后一道闸。
    """
    from datetime import timedelta

    after = getattr(followup, "after_minutes", 0) or 0
    goal = str(getattr(followup, "goal", "") or "")
    if after <= 0 or not goal.strip():
        return None
    now = utcnow()
    return await IntentService(session, actor_id=actor_id).propose(
        goal=goal,
        source_kind="message",
        source_ref=source_ref,
        rationale=f"对话中选择稍后再提：{goal[:80]}",
        subject_id=subject_id,
        window_id=window_id,
        kind="reach_out",
        not_before=now + timedelta(minutes=after),
        expires_at=now + timedelta(minutes=max(after * 4, 120)),
    )


def _view(row: IntentRow) -> IntentView:
    return IntentView(
        intent_id=row.id,
        kind=row.kind,
        goal=row.goal,
        rationale=row.rationale,
        source_kind=row.source_kind,
        source_ref=row.source_ref,
        subject_id=row.subject_id,
        window_id=row.window_id,
        visibility=row.visibility,
        not_before=row.not_before,
        expires_at=row.expires_at,
    )


__all__ = [
    "DEFAULT_TTL_HOURS",
    "record_followup",
    "KINDS",
    "SOURCE_KINDS",
    "STATE_CONSUMED",
    "STATE_DROPPED",
    "STATE_EXPIRED",
    "STATE_OPEN",
    "IntentError",
    "IntentService",
    "IntentView",
]
