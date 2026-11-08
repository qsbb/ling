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

from ling.domains.fallback import (
    DependencyState,
    DependencyValue,
    ResolvedInput,
    is_usable,
    log_fallback,
    resolve_input,
    state_from_timeline,
)
from ling.infra.models import utcnow

DEFAULT_IDLE_MINUTES = 10
"""没别的事情时，默认多久看一次手机。

经验值：真人一天主动解锁 80–150 次，清醒 15 小时的话约 10 分钟一次。
写在代码里而不是配置里，是因为它是"人的生理常识"而不是"部署参数"；
要调就在日程/调度那一层调。
"""

IDLE = "idle"
WATCHING = "watching"
# 正在做的活动类别 → 多久看一次手机。日程给不出"现在在做什么"时用
# `DEFAULT_IDLE_MINUTES`；给了就按这条映射调整（"在忙就拉长"）。
ACTIVITY_INTERVAL_MINUTES: dict[str, int] = {
    "sleep": 0,  # 睡着不排下一次（醒来再说）
    "work": 45,
    "focus": 45,
    "study": 45,
    "meal": 30,
    "leisure": 15,
    "errand": 20,
    "chat": 5,
}
UPSTREAM_SCHEDULE = "schedule"


@dataclass(frozen=True)
class ScheduleNow:
    """从日程读到的"此刻在做什么"——**只是计划**，不是事实。

    `kind` 是她计划里的活动类别（meal/work/...），`ends_at` 是这一段计划的
    结束时刻。两者都只用来决定"多久看一次手机"这类**行为参数**；
    任何要断言"她正在做 X"的地方都不该用这里的数据（那是 presence 的事）。
    """

    kind: str
    title: str
    ends_at: datetime | None


def schedule_dependency(
    view: object, *, now: datetime, upstream: str = UPSTREAM_SCHEDULE
) -> DependencyValue[ScheduleNow]:
    """把 `ActorDayView`（`api.routes.life_timeline.load_actor_day` 的返回值）
    折成回退契约要的 `DependencyValue`。

    为什么在这里而不是在 API 里：判定"什么算可用"必须只有一份
    （`domains/fallback.py`），否则每个调用点都会各自解释一遍 `TimelineState`。
    """
    state = state_from_timeline(getattr(view, "state", None))
    segments = tuple(getattr(view, "segments", ()) or ())
    if state not in {DependencyState.ready, DependencyState.degraded}:
        # 空/待生成/失败/读不到：**不拿段凑**——那些段可能属于别的计划日。
        return DependencyValue(state=state, upstream=upstream)
    moment = now
    current = None
    for seg in segments:
        start = getattr(seg, "start_at", None)
        end = getattr(seg, "end_at", None)
        if start is None or end is None:
            continue
        if start <= moment < end:
            current = seg
            break
    if current is None:
        # 计划是 ready 的，但此刻没有段覆盖——这是"没安排"，不是读不到。
        return DependencyValue(state=state, upstream=upstream)
    return DependencyValue(
        state=state,
        upstream=upstream,
        value=ScheduleNow(
            kind=str(getattr(current, "kind", "") or ""),
            title=str(getattr(current, "title", "") or ""),
            ends_at=getattr(current, "end_at", None),
        ),
    )


def _default_reason(state: DependencyState) -> str:
    """回退时给用户看的一句话：**说清上游当时是什么状态**。

    一眼能看懂的四类：还没生成 / 没生成出来 / 读不到 / 规则兜底。
    不写内部错误码（那是日志与测试用的）。
    """
    return {
        DependencyState.empty: f"今天还没有日程，先按默认每 {DEFAULT_IDLE_MINUTES} 分钟看一次手机",
        DependencyState.pending: f"今天的日程还没生成，先按默认每 {DEFAULT_IDLE_MINUTES} 分钟看一次手机",
        DependencyState.failed: f"今天的日程没生成成功，先按默认每 {DEFAULT_IDLE_MINUTES} 分钟看一次手机",
        DependencyState.unavailable: f"日程暂时读不到，先按默认每 {DEFAULT_IDLE_MINUTES} 分钟看一次手机",
        DependencyState.degraded: (
            "日程是规则兜底出来的，只当计划参考，先按默认节奏看手机"
        ),
        DependencyState.unknown: "日程状态不明，先按默认节奏看手机",
        DependencyState.ready: f"日程里没有此刻的安排，先按默认每 {DEFAULT_IDLE_MINUTES} 分钟看一次手机",
    }[state]


@dataclass(frozen=True)
class PhoneInterval:
    """一次"下次多久看手机"的结论：分钟数 + 它从哪来。

    ``minutes == 0`` 的语义是**不排下一次**（睡着），与 `stop_watching`
    里 ``next_in_minutes=0`` 一致。
    """

    minutes: int
    resolved: ResolvedInput[int]

    @property
    def used_default(self) -> bool:
        return self.resolved.used_default

    def notice(self) -> dict[str, object]:
        return self.resolved.notice()


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

    # ------------------------------------------------------------ 上游回退
    #
    # 「多久看一次手机」本来该看日程（在忙拉长、睡着不排），但日程是**上游**：
    # 它可能还没生成（21:00 才到期）、生成失败、或者干脆读不到。这三种情况下
    # 用本领域自己的 `DEFAULT_IDLE_MINUTES` 跑这一次，并说清楚"这是默认值"。
    #
    # **不缓存上游结果**：上游恢复后下一次调用直接读新的；也不把默认值写进
    # 任何事实表——这里只算一个分钟数，`_apply_interval` 之外不碰数据库。

    def interval_from_schedule(
        self, dependency: DependencyValue[ScheduleNow]
    ) -> PhoneInterval:
        """按日程决定"下次多久看"；日程不可用就用自己的默认值。

        纯函数：不读库、不写库、不记日志（日志由 `schedule_next_from_upstream`
        在真正应用时记一次，避免同一个决定记两遍）。
        """
        default_minutes = DEFAULT_IDLE_MINUTES
        # **degraded 可以用在这里**：规则兜底的日程仍然是"她大概要做什么"，
        # 拿它调"多久看一次手机"这种**行为参数**是安全的。但它绝不能拿来
        # 断言"她正在做 X"——那条线在 `domains/presence`（事实层）里是严格禁止的。
        # 这就是 `allow_degraded` 只出现在本函数的原因。
        if is_usable(dependency.state, allow_degraded=True) and dependency.value is not None:
            kind = dependency.value.kind
            if kind in ACTIVITY_INTERVAL_MINUTES:
                minutes = ACTIVITY_INTERVAL_MINUTES[kind]
                resolved: ResolvedInput[int] = resolve_input(
                    DependencyValue(
                        state=dependency.state,
                        upstream=dependency.upstream,
                        value=minutes,
                    ),
                    default=default_minutes,
                    default_reason=_default_reason(dependency.state),
                    # 与上面的判断同一口径：degraded 允许当**计划参考**。
                    # 漏传这个参数会让上面刚判定"可用"的值又被判成不可用。
                    allow_degraded=True,
                )
                return PhoneInterval(minutes=resolved.value, resolved=resolved)
            # 可用但不是我们认识的类别：**不猜**，按默认节奏，但标明是默认。
            resolved = resolve_input(
                DependencyValue(state=dependency.state, upstream=dependency.upstream),
                default=default_minutes,
                default_reason=(
                    f"日程里此刻的活动类别「{kind or '空'}」没有对应的看手机节奏，"
                    f"按默认每 {default_minutes} 分钟一次"
                ),
            )
            return PhoneInterval(minutes=resolved.value, resolved=resolved)
        # 走「不可用」这条：把上游值摘掉，只保留它的状态（值绝不能顶上去）。
        resolved = resolve_input(
            DependencyValue(state=dependency.state, upstream=dependency.upstream),
            default=default_minutes,
            default_reason=_default_reason(dependency.state),
        )
        return PhoneInterval(minutes=resolved.value, resolved=resolved)

    async def schedule_next_from_upstream(
        self,
        dependency: DependencyValue[ScheduleNow],
        *,
        now: datetime | None = None,
    ) -> tuple[PhoneView, PhoneInterval]:
        """读上游算出的节奏 → 应用到"下次什么时候看"，并记录是否回退。

        回退时记一条**人话**行为日志（`upstream_fallback`，INFO 级）：
        上游为空是正常状态，不该打成告警。
        """
        interval = self.interval_from_schedule(dependency)
        log_fallback(interval.resolved, consumer="phone.next_look")
        view = await self.schedule_next(minutes=interval.minutes, now=now)
        return view, interval


__all__ = [
    "ACTIVITY_INTERVAL_MINUTES",
    "DEFAULT_IDLE_MINUTES",
    "IDLE",
    "UPSTREAM_SCHEDULE",
    "WATCHING",
    "PhoneInterval",
    "PhoneService",
    "PhoneView",
    "ScheduleNow",
    "schedule_dependency",
]
