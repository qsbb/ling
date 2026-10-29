"""Agenda 域：角色的软作息、自己的小事，以及计划与事实的对账。

**唯一写入方**。三条硬规则：

1. **作息是软的。** 它给出"活动可以发生的宽窗口"，不是"到点必须执行"。
   用户来聊天可以打断，被打断的活动能恢复。
2. **计划不是事实。** ``AgendaPlanRow`` 永远不能被写成"已完成"——
   完成必须来自 ``ActivityRow`` 且有完成证据。
   把计划当经历说出去，就是在编造人生。
3. **tentative 不能说成"约好"。** 只有 ``confirmed`` 才能用确定语气。

作息与 ``CommitmentRow`` 严格分开：后者是对用户的硬承诺。
混在一起会让"没按作息起床"和"答应了用户却没做到"变成同一类失信，
而这两件事对用户的意义完全不同。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import ActorRoutineRow, AgendaPlanRow, utcnow
from ling.infra.security import new_id
from ling.ports.config import LifePolicy

PLAN_STATES = ("planned", "active", "done", "cancelled", "skipped")
COMMITMENT_LEVELS = ("tentative", "confirmed")
# 一次最多保留几件"可选小事"。多了会让角色像在念行程表，
# 也违反"不要每分钟生成一份完美日记"的原则。
MAX_LIVE_PLANS = 4

DEFAULT_WAKE_MINUTE = 8 * 60
DEFAULT_REST_MINUTE = 23 * 60


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # 时区非法时退回 UTC 而不是服务器本地时区——
        # 服务器本地时区会让"我的早上"变成机房所在时区的早上。
        return ZoneInfo("UTC")


@dataclass(frozen=True)
class RoutineView:
    timezone: str
    wake_minute: int
    rest_minute: int
    source: str

    def is_awake(self, now: datetime) -> bool:
        """按角色自己的时区判断此刻是否在"醒着"的窗口内。

        支持跨午夜：``wake_minute > rest_minute`` 表示例如
        02:00 睡、10:00 起。
        """
        local = now.astimezone(_tz(self.timezone))
        minute = local.hour * 60 + local.minute
        if self.wake_minute == self.rest_minute:
            return True  # 未配置有效窗口时默认醒着，不要凭空造出"在睡觉"
        if self.wake_minute < self.rest_minute:
            return self.wake_minute <= minute < self.rest_minute
        # 跨午夜：醒着的时间是 [wake, 24) ∪ [0, rest)
        return minute >= self.wake_minute or minute < self.rest_minute

    def describe(self) -> str:
        wake = f"{self.wake_minute // 60:02d}:{self.wake_minute % 60:02d}"
        rest = f"{self.rest_minute // 60:02d}:{self.rest_minute % 60:02d}"
        return f"你的作息大致是 {wake} 起、{rest} 睡"


@dataclass(frozen=True)
class PlanView:
    plan_id: str
    kind: str
    title: str
    start_at: datetime
    end_at: datetime
    commitment_level: str
    state: str

    def describe(self) -> str:
        """只描述"打算"，绝不说成已发生。

        tentative 与 confirmed 用词不同——用户会据此判断能不能当约定依赖。
        """
        if self.state == "active":
            # 正在进行：直接说在做什么，不用时间前缀
            return f"正在{self.title}"
        when = self.start_at.astimezone(_tz("Asia/Shanghai")).strftime("%H:%M")
        if self.commitment_level == "confirmed":
            return f"约好 {when} {self.title}"
        return f"可能 {when} 想{self.title}"


class AgendaService:
    """作息与计划的唯一写入方。"""

    def __init__(
        self, session: AsyncSession, *, actor_id: str, policy: LifePolicy | None = None
    ) -> None:
        self._session = session
        self._actor_id = actor_id
        self._policy = policy or LifePolicy()

    # ---------------------------------------------------------- 作息

    async def routine(self) -> RoutineView:
        row = await self._session.get(ActorRoutineRow, self._actor_id)
        if row is None:
            return RoutineView(
                timezone="Asia/Shanghai",
                wake_minute=DEFAULT_WAKE_MINUTE,
                rest_minute=DEFAULT_REST_MINUTE,
                source="default",
            )
        return RoutineView(
            timezone=str(row.timezone),
            wake_minute=int(row.wake_minute),
            rest_minute=int(row.rest_minute),
            source=str(row.source),
        )

    async def set_routine(
        self,
        *,
        timezone: str = "Asia/Shanghai",
        wake_minute: int = DEFAULT_WAKE_MINUTE,
        rest_minute: int = DEFAULT_REST_MINUTE,
        source: str = "configured",
    ) -> RoutineView:
        if not (0 <= wake_minute < 1440 and 0 <= rest_minute < 1440):
            raise ValueError("wake_minute/rest_minute 必须在 0..1439")
        row = await self._session.get(ActorRoutineRow, self._actor_id)
        if row is None:
            row = ActorRoutineRow(actor_id=self._actor_id)
            self._session.add(row)
        row.timezone = timezone
        row.wake_minute = wake_minute
        row.rest_minute = rest_minute
        row.source = source
        row.revision = int(row.revision or 0) + 1
        await self._session.flush()
        return await self.routine()

    # ---------------------------------------------------------- 计划

    async def propose_plan(
        self,
        *,
        kind: str,
        title: str,
        start_at: datetime,
        end_at: datetime,
        commitment_level: str = "tentative",
        place_id: str | None = None,
        source_event_ids: list[str] | None = None,
    ) -> PlanView | None:
        """新增一件小事。超过策略里的 ``max_live_plans`` 时返回 None（不排行程表）。"""
        if commitment_level not in COMMITMENT_LEVELS:
            raise ValueError(f"未知承诺等级: {commitment_level}")
        if end_at <= start_at:
            raise ValueError("end_at 必须晚于 start_at")
        live = await self._live_plans()
        if len(live) >= self._policy.max_live_plans:
            return None
        routine = await self.routine()
        row = AgendaPlanRow(
            id=new_id("plan"),
            actor_id=self._actor_id,
            kind=kind,
            title=title.strip()[:120],
            start_at=start_at,
            end_at=end_at,
            timezone=routine.timezone,
            commitment_level=commitment_level,
            state="planned",
            place_id=place_id,
            source_event_ids=list(source_event_ids or []),
        )
        self._session.add(row)
        await self._session.flush()
        return self._view(row)

    async def _live_plans(self) -> list[AgendaPlanRow]:
        rows = await self._session.execute(
            select(AgendaPlanRow)
            .where(
                AgendaPlanRow.actor_id == self._actor_id,
                # planned 与 active 都算"手头的事"。
                # 只查 planned 会让计划一被推进就从上下文里消失——
                # 表现为角色明明在做这件事，却说"我没在读什么"。
                AgendaPlanRow.state.in_(["planned", "active"]),
            )
            .order_by(AgendaPlanRow.start_at)
        )
        return list(rows.scalars())

    async def due_plans(self, *, now: datetime | None = None) -> list[PlanView]:
        """已经到点、可以开始进行的计划。"""
        now = now or utcnow()
        return [
            self._view(r) for r in await self._live_plans() if _aware(r.start_at) <= now
        ]

    async def upcoming(self, *, limit: int | None = None) -> list[PlanView]:
        cap = self._policy.max_live_plans if limit is None else limit
        return [self._view(r) for r in (await self._live_plans())[:cap]]

    async def set_state(self, plan_id: str, state: str, *, reason: str = "") -> None:
        """推进计划状态。

        **不接受把计划直接标成 done**：完成必须由 Activity 携带证据来推进，
        否则会出现"计划写完了就等于做过了"的假经历。
        调用方要标记完成，应当走 ``ActivityService.transition``。
        """
        if state not in PLAN_STATES:
            raise ValueError(f"未知计划状态: {state}")
        if state == "done":
            raise ValueError(
                "计划不能直接被标为 done；完成必须来自 Activity 的完成证据"
            )
        row = await self._session.get(AgendaPlanRow, plan_id)
        if row is None or row.actor_id != self._actor_id:
            return
        row.state = state
        row.revision = int(row.revision or 0) + 1
        _ = reason
        await self._session.flush()

    async def cancel(self, plan_id: str) -> None:
        await self.set_state(plan_id, "cancelled")

    async def on_activity_completed(self, plan_id: str) -> None:
        """由 Activity 侧在**有完成证据**时调用，把计划标为 done。

        单独开一个入口，与 ``set_state`` 分开：
        这样代码里一眼能看出"谁能把计划标成完成"。
        """
        row = await self._session.get(AgendaPlanRow, plan_id)
        if row is None or row.actor_id != self._actor_id:
            return
        row.state = "done"
        row.revision = int(row.revision or 0) + 1
        await self._session.flush()

    # ---------------------------------------------------------- 时段边界

    def next_boundary(self, routine: RoutineView, *, now: datetime) -> datetime:
        """下一次"作息切换时刻"（醒→歇 或 歇→醒）。

        worker 只在**边界**推进，不每分钟调用模型——
        这是路线图里"不要每分钟生成一份完美日记"的执行点。
        """
        tz = _tz(routine.timezone)
        local = now.astimezone(tz)
        day = local.date()
        candidates: list[datetime] = []
        for offset in (0, 1):
            current = day + timedelta(days=offset)
            for minute in (routine.wake_minute, routine.rest_minute):
                moment = datetime.combine(
                    current, time(hour=minute // 60, minute=minute % 60), tzinfo=tz
                )
                if moment > local:
                    candidates.append(moment)
        if not candidates:
            return now + timedelta(hours=1)
        return min(candidates)

    # ---------------------------------------------------------- 视图

    @staticmethod
    def _view(row: AgendaPlanRow) -> PlanView:
        return PlanView(
            plan_id=row.id,
            kind=str(row.kind),
            title=str(row.title),
            start_at=_aware(row.start_at),
            end_at=_aware(row.end_at),
            commitment_level=str(row.commitment_level),
            state=str(row.state),
        )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = [
    "COMMITMENT_LEVELS",
    "DEFAULT_REST_MINUTE",
    "DEFAULT_WAKE_MINUTE",
    "MAX_LIVE_PLANS",
    "PLAN_STATES",
    "AgendaService",
    "PlanView",
    "RoutineView",
]