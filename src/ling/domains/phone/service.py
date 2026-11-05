"""看一眼手机的状态机。

## 它解决什么

没有这个，她只能两种状态：秒回，或者装死。真人有第三种——**在忙别的**。
她会隔一阵子拿起手机扫一眼，看有没有人找她，然后放下继续做手头的事。

## 三条规则

1. **下次看手机从"这次看完"开始算**，不是从"开始看"算。
   否则一次看得久了，下次就会提前到来。
2. **正在看的时候不排下一次**（`next_look_at` 为空）。界面显示「等她收起」，
   而不是一个会跳的数字。
3. **间隔不是固定的**：在忙就拉长，在睡就不排（醒来再说），刚聊完就缩短。
   真实的间隔由调用方（日程/调度）决定并传进来，这里只负责状态流转。
   这个模块**不读配置**——它只记账，怎么算间隔是外面的事。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import utcnow

DEFAULT_IDLE_MINUTES = 10
"""没别的事情时，默认多久看一次手机。

经验值：真人一天主动解锁 80–150 次，清醒 15 小时的话约 10 分钟一次。
写在代码里而不是配置里，是因为它是"人的生理常识"而不是"部署参数"；
要调就在日程/调度那一层调。
"""

IDLE = "idle"
WATCHING = "watching"


@dataclass(frozen=True)
class PhoneView:
    """她此刻跟手机的关系，给界面和调度读。"""

    state: str
    watching_app: str | None = None
    watching_since: datetime | None = None
    last_looked_at: datetime | None = None
    next_look_at: datetime | None = None
    last_result: str | None = None
    last_context: str | None = None

    @property
    def is_watching(self) -> bool:
        return self.state == WATCHING


class PhoneService:
    def __init__(self, session: AsyncSession, *, actor_id: str = "actor_ling") -> None:
        self._session = session
        self._actor_id = actor_id

    async def _row(self, *, create: bool = False):  # noqa: ANN202
        from ling.infra.models import PhoneStateRow

        row = await self._session.get(PhoneStateRow, self._actor_id)
        if row is None and create:
            row = PhoneStateRow(actor_id=self._actor_id, state=IDLE)
            self._session.add(row)
            await self._session.flush()
        return row

    def _view(self, row) -> PhoneView:  # noqa: ANN001
        if row is None:
            return PhoneView(state=IDLE)
        return PhoneView(
            state=row.state or IDLE,
            watching_app=row.watching_app,
            watching_since=row.watching_since,
            last_looked_at=row.last_looked_at,
            next_look_at=row.next_look_at,
            last_result=row.last_result,
            last_context=row.last_context,
        )

    async def current(self) -> PhoneView:
        return self._view(await self._row())

    async def due(self, *, now: datetime | None = None) -> bool:
        """到点该看了吗。

        `next_look_at` 为空表示**正在看**（或从没排过），两种情况都不触发。
        """
        view = await self.current()
        if view.is_watching or view.next_look_at is None:
            return False
        return view.next_look_at <= (now or utcnow())

    async def start_watching(
        self,
        *,
        app: str | None = None,
        ref: str | None = None,
        context: str | None = None,
        now: datetime | None = None,
    ) -> PhoneView:
        """拿起手机。**清空下次时间**——正在看的时候不排下一次。"""
        moment = now or utcnow()
        row = await self._row(create=True)
        row.state = WATCHING
        row.watching_app = app
        row.watching_ref = ref
        row.watching_since = moment
        row.next_look_at = None
        if context is not None:
            row.last_context = context[:128]
        await self._session.flush()
        return self._view(row)

    async def stop_watching(
        self,
        *,
        result: str | None = None,
        next_in_minutes: int | None = None,
        now: datetime | None = None,
    ) -> PhoneView:
        """收起手机。**下次时间从这一刻开始算**，不是从开始看那一刻。

        这条是用户明说的：「刷新下次看手机时间，从本次看手机结束之后开始重新计时」。
        从开始算的话，一次看得久了下次就会提前到来，人会觉得不对劲。
        """
        moment = now or utcnow()
        row = await self._row(create=True)
        row.state = IDLE
        row.watching_app = None
        row.watching_ref = None
        row.watching_since = None
        row.last_looked_at = moment
        if result is not None:
            row.last_result = result[:64]
        minutes = DEFAULT_IDLE_MINUTES if next_in_minutes is None else next_in_minutes
        row.next_look_at = moment + timedelta(minutes=minutes) if minutes > 0 else None
        await self._session.flush()
        return self._view(row)

    async def schedule_next(
        self, *, minutes: int, now: datetime | None = None
    ) -> PhoneView:
        """只改"下次什么时候看"，不动状态。

        睡着时用：醒来之前不排，或者排到醒来的时刻。
        """
        row = await self._row(create=True)
        if row.state != WATCHING:
            moment = now or utcnow()
            row.next_look_at = (
                moment + timedelta(minutes=minutes) if minutes > 0 else None
            )
            await self._session.flush()
        return self._view(row)


__all__ = ["DEFAULT_IDLE_MINUTES", "IDLE", "WATCHING", "PhoneService", "PhoneView"]
