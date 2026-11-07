"""Activity 域：模拟活动进程、检查点与完成证据。

唯一写入方。状态机（设计文档 §6.3）：
``proposed → running → paused/completed/failed/cancelled``

硬规则：
- 做饭被电话打断可暂停，再从检查点恢复。
- 无食材不能说已做完；完成必须有 ``completion_evidence``。
- 无新事件或到期边界时不持续空转调用模型。

## 完成证据必须带**种类**（P0.5）

"有证据"不等于"是事实"。``life.tick`` 的计划时段结束时，证据只说明
"时间到了"，那是 ``clock_only``——不能被自我时间线/日程大纲/控制台当成
"她真做过"。写完成证据时这里会把种类编码进 ``completion_evidence`` 前缀
（约定见 :mod:`ling.domains.evidence`）：

- 调用方显式传 ``evidence_kind=observed|user_confirmed`` → 用它；
- 否则按 ``origin`` 结构化列推断：``tool_receipt`` → ``observed``、
  ``user_stated`` → ``user_confirmed``、其余（含 ``simulated``）→ ``clock_only``。

计划时段结束推导出来的完成都属于最后一种：**模拟活动不是真实经历**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.evidence import (
    EvidenceKind,
    encode_completion_evidence,
    evidence_kind_from_origin,
    parse_completion_evidence,
)
from ling.infra.models import ActivityRow, utcnow
from ling.infra.security import new_id

VALID_STATES = {"proposed", "running", "paused", "completed", "failed", "cancelled"}

# 允许的状态迁移；其余一律拒绝（确定性硬门，不由模型决定）
TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"running", "cancelled", "failed"},
    "running": {"paused", "completed", "failed", "cancelled"},
    "paused": {"running", "cancelled", "failed"},
    "completed": set(),
    "failed": {"proposed"},
    "cancelled": {"proposed"},
}


class InvalidTransition(Exception):
    pass


@dataclass(frozen=True)
class ActivityView:
    activity_id: str
    kind: str
    state: str
    place_id: str | None
    checkpoint: dict = field(default_factory=dict)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    completion_evidence: str | None = None
    # 完成证据的种类（clock_only / observed / user_confirmed）。
    # 没有证据时是最保守的 clock_only——不要把它读成"事实"。
    evidence_kind: str = EvidenceKind.clock_only.value
    # 来源：simulated（模拟生活）/ tool_receipt（真实回执）/ user_stated
    origin: str = "simulated"
    plan_id: str | None = None
    visibility: str = "subject_private"

    @property
    def is_active(self) -> bool:
        return self.state in {"running", "paused"}


class ActivityService:
    def __init__(self, session: AsyncSession, *, actor_id: str = "actor_ling") -> None:
        self._session = session
        self._actor_id = actor_id

    async def propose(
        self,
        *,
        kind: str,
        place_id: str | None = None,
        plan_id: str | None = None,
        origin: str = "simulated",
        source_event_ids: list[str] | None = None,
        visibility: str = "subject_private",
    ) -> ActivityView:
        """新建一个活动。

        ``origin`` 必须如实：``simulated`` 是模拟生活（不能冒充真实经历），
        ``tool_receipt`` 是有真实回执的动作，``user_stated`` 是用户刚说的。
        """
        row = ActivityRow(
            id=new_id("act"),
            actor_id=self._actor_id,
            kind=kind,
            state="proposed",
            place_id=place_id,
            checkpoint={},
            plan_id=plan_id,
            origin=origin,
            source_event_ids=list(source_event_ids or []),
            visibility=visibility,
        )
        self._session.add(row)
        await self._session.flush()
        return self._view(row)

    async def _get(self, activity_id: str) -> ActivityRow:
        row = await self._session.get(ActivityRow, activity_id)
        if row is None or row.actor_id != self._actor_id:
            raise KeyError(f"活动不存在: {activity_id}")
        return row

    async def transition(
        self,
        activity_id: str,
        to_state: str,
        *,
        completion_evidence: str | None = None,
        checkpoint: dict | None = None,
        evidence_kind: EvidenceKind | str | None = None,
    ) -> ActivityView:
        """状态迁移。

        ``evidence_kind`` 只在标 ``completed`` 时有意义：显式说明"这条证据
        是哪一种"。注意它**不会被自动放大**——不传就按 ``origin`` 判，
        模拟生活的活动永远是 ``clock_only``，调用方不能靠传文本自封事实。
        """
        if to_state not in VALID_STATES:
            raise InvalidTransition(f"未知状态: {to_state}")
        row = await self._get(activity_id)
        allowed = TRANSITIONS.get(row.state, set())
        if to_state not in allowed:
            raise InvalidTransition(f"不允许 {row.state} → {to_state}")
        if to_state == "completed" and not (completion_evidence or "").strip():
            # 没有完成证据就不能标记完成
            raise InvalidTransition(
                "标记 completed 必须提供完成证据（completion_evidence）："
                "计划不等于完成，不能仅凭自称就标记做完"
            )
        row.state = to_state
        if to_state == "running" and row.started_at is None:
            row.started_at = utcnow()
        if to_state in {"completed", "failed", "cancelled"}:
            row.ended_at = utcnow()
        if completion_evidence:
            row.completion_evidence = encode_completion_evidence(
                self._resolve_evidence_kind(row, evidence_kind),
                completion_evidence,
            )[:1000]
        if checkpoint is not None:
            row.checkpoint = dict(checkpoint)
        row.updated_at = utcnow()
        await self._session.flush()
        return self._view(row)

    @staticmethod
    def _resolve_evidence_kind(
        row: ActivityRow, declared: EvidenceKind | str | None
    ) -> EvidenceKind:
        """决定这次完成证据的种类。

        优先级：调用方显式声明 > ``origin`` 推断 > ``clock_only``。
        没有"证据文本看起来像观测"这种分支：文本可以被模型写，不能自封事实。
        """
        if declared is not None:
            try:
                return EvidenceKind(str(declared))
            except ValueError as exc:  # 未知种类要报错，不能静默降级成事实
                raise InvalidTransition(f"未知的证据种类: {declared}") from exc
        return evidence_kind_from_origin(row.origin)

    async def save_checkpoint(self, activity_id: str, checkpoint: dict) -> ActivityView:
        row = await self._get(activity_id)
        if row.state != "running":
            raise InvalidTransition("只有 running 的活动可以保存检查点")
        row.checkpoint = dict(checkpoint)
        row.updated_at = utcnow()
        await self._session.flush()
        return self._view(row)

    async def current(self) -> ActivityView | None:
        row = (
            await self._session.execute(
                select(ActivityRow)
                .where(
                    ActivityRow.actor_id == self._actor_id,
                    ActivityRow.state.in_(["running", "paused"]),
                )
                .order_by(ActivityRow.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return self._view(row) if row else None

    async def history(self, limit: int = 20) -> list[ActivityView]:
        rows = list(
            (
                await self._session.execute(
                    select(ActivityRow)
                    .where(ActivityRow.actor_id == self._actor_id)
                    .order_by(ActivityRow.updated_at.desc())
                    .limit(limit)
                )
            ).scalars()
        )
        return [self._view(r) for r in rows]

    @staticmethod
    def _view(row: ActivityRow) -> ActivityView:
        def _utc(value: datetime | None) -> datetime | None:
            if value is None:
                return None
            return value if value.tzinfo else value.replace(tzinfo=UTC)

        # 对外的 `completion_evidence` 只给人读文本：`kind=…;` 前缀是**存储层**
        # 的结构化段（见 `ling.domains.evidence`），不该漏进界面或回复。
        # 种类单独用 `evidence_kind` 透出，读侧据此判断能不能当事实用。
        evidence_kind, evidence_text = parse_completion_evidence(row.completion_evidence)
        return ActivityView(
            activity_id=row.id,
            kind=row.kind,
            state=row.state,
            place_id=row.place_id,
            checkpoint=dict(row.checkpoint or {}),
            started_at=_utc(row.started_at),
            ended_at=_utc(row.ended_at),
            completion_evidence=evidence_text or None,
            evidence_kind=evidence_kind.value,
            origin=str(row.origin),
            plan_id=row.plan_id,
            visibility=str(row.visibility),
        )
