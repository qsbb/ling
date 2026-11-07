"""只读生活时间轴：`GET /v1/life/timeline`（设计 §7、§9 阶段 5）。

## 这个端点只读

不建表、不落行、不触发模型：它把**已经存在**的 `day_plans` /
`schedule_segments` / `schedule_intentions` / `activities` / `scene_commits` /
`dream_reports` 投影成一份可解释的视图。写入方仍然是 `AgendaService` /
`ActivityService` / `DreamService`，这里一个 `add()` 都没有。

## 三个容易做错的地方，这里各自单独处理

### 1) 双时区：展示日按**用户**，计划日按**角色**

`display_*` 用当前登录者的有效时区（`settings/user_preferences.py` 的
`load_user_timezone`：DB 偏好 > env > `Asia/Shanghai`），`plan_*` 与
`actor_timezone` 用角色规范时区（owner subject 的同一套解析）。
**时区一律服务端解析**：前端不许用浏览器本地时区再判一次"今天"，
否则同一个请求在两台机器上会落到两个日期。

### 2) 裁剪：按**相交**取段，按相交区间裁剪

段表里存的是 UTC 瞬时区间。展示日窗口通常与计划日窗口对齐，但两套时区
不同、或段跨过午夜时就不对齐——所以查询条件是 `start_at < window_end AND
end_at > window_start`（相交），**不是**"按 start_at 归到某一天"。跨午夜的段
在两天各自被裁剪，并用 `continues_from_previous_day` / `continues_next_day`
说明"还有另一半"。父子段都返回，前端才能折叠层级。

### 3) 计划不是事实

`segments` 是"打算/可能"，`facts` 是 `activities` 里已发生的进程。计划时段
到点结束**不会**把段改名成"已做"——那需要非 `clock_only` 的完成证据，属于
事实层，写在 `facts` 里（含 `evidence_kind`）。两层字段不共用，别合并。

## 受众裁决

库里 `visibility='actor_private'` 的段/活动/意向与梦只对 owner 返回正文：
非 owner 拿不到正文，但会看到"存在私有内容"的标记（`has_private_content` +
三个 `hidden_*` 计数 + `dream.restricted`）。`visibility` 为 `NULL` 的旧段
**不猜**来源，按可见处理（与 `ScheduleSegmentRow.visibility` 的注释一致）。

## 四态可区分

`state` 的取值见 :class:`ling.contracts.v1.TimelineState`：`empty`（没有计划行）/
`degraded`（规则兜底）/ `failed`（生成失败，带 `last_error_code`）/
`unavailable`（读取出错）。**不要**把它们压成"没有安排"——这四种的下一步
完全不同（等生成 / 接受兜底 / 查原因码 / 查故障）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.api.deps import CurrentAuth, DbSession, SettingsDep
from ling.contracts.v1 import (
    DayIntentSummary,
    DreamCard,
    IntentTeaserItem,
    LifeTimelineFact,
    LifeTimelineOut,
    LifeTimelineSegment,
    OutlineRelation,
    TimelineState,
    TodayOutlineItem,
)
from ling.domains.agenda.intents import IntentService, IntentView
from ling.domains.agenda.timeline import (
    COMMITMENT_ANCHOR,
    display_day_for,
    display_day_window,
    outline_due_at,
    plan_day_for,
    plan_day_window,
)
from ling.domains.evidence import activity_evidence_kind
from ling.infra.models import (
    ActivityRow,
    DayPlanRow,
    DreamReportRow,
    SceneCommitRow,
    ScheduleSegmentRow,
)
from ling.settings.user_preferences import load_user_timezone, owner_subject_id

logger = logging.getLogger("ling.api.life_timeline")

router = APIRouter(tags=["life"])

#: 展望窗口：角色计划日 D+1 … D+7（设计 §3A）。未来日**没有** `day_plans` 行。
INTENT_HORIZON_DAYS = 7
#: 展望读侧一次最多返回多少条（与"每天 2 条、总 10 条"的 prompt 截断不是一回事：
#: 那是装配模型输入的预算，这里是给人看的列表上限）。
INTENT_HORIZON_LIMIT = 20

#: 仪表盘摘要最多几条（设计 §7「2–3 条最有信息量的」：当前段 + 下一条 + 锚点）。
OUTLINE_MAX_ITEMS = 3
#: 意向预告：最多 2 条。
TEASER_MAX_ITEMS = 2

#: 事实层只取"真的发生过"的活动状态。`proposed` 还没发生，`cancelled` 是撤销。
FACT_STATES: tuple[str, ...] = ("running", "paused", "completed", "failed")
#: 单次请求最多返回多少条事实（一天里正常不会有这么多活动）。
FACTS_LIMIT = 50

#: 观众可见性：非 owner 看不到 `actor_private` 的正文。
PRIVATE_VISIBILITY = "actor_private"

# 给人看的状态说明（纯文本，前端直接进 JSX）。
NOTE_EMPTY = "这一天还没有安排"
NOTE_PENDING = "这一天的安排还没生成"
NOTE_DEGRADED = "这一天的安排来自规则兜底（模型不可用或输出被拒收）"
NOTE_FAILED = "这一天的安排生成失败"
NOTE_UNAVAILABLE = "暂时无法读取时间轴"
NOTE_DREAM_RESTRICTED = "梦是角色私有内容，你看不到正文"


# ---------------------------------------------------------------- 纯函数
#
# 这一段不碰数据库、不读全局：裁剪、双时区换算、摘要选条都直接打表
# （tests/unit/test_life_timeline.py）。


def _aware(moment: datetime) -> datetime:
    """统一成 UTC aware；naive 输入按 UTC 解释（与 agenda.timeline 同规则）。"""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def clip_to_window(
    start_at: datetime,
    end_at: datetime,
    window_start: datetime,
    window_end: datetime,
) -> tuple[datetime, datetime] | None:
    """把 ``[start_at, end_at)`` 裁到窗口内；完全不相交返回 ``None``。

    返回的是**相交区间**，不是段的真实起止：跨午夜的段在两天各自得到不同的
    裁剪结果，前端不要把裁剪值当成"她 00:00 才开始做这件事"。
    """
    start = max(_aware(start_at), _aware(window_start))
    end = min(_aware(end_at), _aware(window_end))
    if start >= end:
        return None
    return start, end


def continues_flags(
    start_at: datetime, end_at: datetime, window_start: datetime, window_end: datetime
) -> tuple[bool, bool]:
    """``(continues_from_previous_day, continues_next_day)``。

    判据是**真实区间**与窗口比较（不是裁剪后的值）：裁剪后两端必然落在窗口内，
    拿它比永远得到 False。
    """
    return _aware(start_at) < _aware(window_start), _aware(end_at) > _aware(window_end)


def visible_to(visibility: str | None, *, is_owner: bool) -> bool:
    """``actor_private`` 只对 owner 可见；``NULL``（旧段未知）按可见处理。"""
    if is_owner:
        return True
    return str(visibility or "") != PRIVATE_VISIBILITY


def viewer_is_owner(
    *, role: str, subject_id: str, owner_subject: str | None
) -> bool:
    """当前登录者是不是这个角色的主人。

    两条判据都要认：``role=owner`` 的账号，以及绑在 owner subject 上的账号
    ——个人部署里"同一个人"可能有两个登录名，隐私判断该按人（subject）而不是
    按账号名。两条都不满足才算非 owner。
    """
    if str(role) == "owner":
        return True
    return owner_subject is not None and str(subject_id) == str(owner_subject)


@dataclass(frozen=True)
class DualWindow:
    """展示日窗口与角色计划日窗口（两套时区各自换算成 UTC 瞬时）。"""

    display_date: date
    display_timezone: str
    display_start_at: datetime
    display_end_at: datetime
    plan_day: date
    plan_start_at: datetime
    plan_end_at: datetime


def resolve_dual_window(
    display_day: date, display_timezone: str, actor_timezone: str
) -> DualWindow:
    """把"用户看到的这一天"换算成两套 UTC 半开区间。

    - 展示窗口：``[D 00:00, D+1 00:00)`` 在**用户**时区；
    - 计划日：包含展示窗口**起点**的那个**角色**计划日（`plan_day_for`），
      窗口同样是角色本地午夜到午夜。

    两套时区不同时这两个窗口可以只部分重叠，甚至落在不同的日期上——这正是
    "用户看到的今天"与"她的今天"必须分开返回的原因，不是数据错误。
    DST 的 23/25 小时由 `display_day_window` / `plan_day_window` 处理（本地墙钟
    组合再转 UTC，不做 ``+24h`` 算术）。
    """
    display_start, display_end = display_day_window(display_day, display_timezone)
    plan_day = plan_day_for(display_start, actor_timezone)
    plan_start, plan_end = plan_day_window(plan_day, actor_timezone)
    return DualWindow(
        display_date=display_day,
        display_timezone=display_timezone,
        display_start_at=display_start,
        display_end_at=display_end,
        plan_day=plan_day,
        plan_start_at=plan_start,
        plan_end_at=plan_end,
    )


def derive_state(plan: DayPlanRow | None) -> TimelineState:
    """把计划行投影成对外状态（四态 + ready/pending）。

    ``failed`` 的定义是"这一版**没生成出来**"：行还在 `pending`（没有被任何
    版本提交过），但已经记下了失败原因码。`degraded` 不算失败——规则兜底
    **成功**了，内容可读，只是来源是 rule 而不是模型；原因码照样放
    `last_error_code` 里供界面标"降级/待复核"。
    """
    if plan is None:
        return TimelineState.empty
    state = str(plan.state)
    if state == "degraded":
        return TimelineState.degraded
    if state == "ready":
        return TimelineState.ready
    # 剩下的只可能是 pending：有原因码 = 生成失败，没有 = 还没到期/还没跑。
    if str(plan.last_error_code or "").strip():
        return TimelineState.failed
    return TimelineState.pending


@dataclass(frozen=True)
class SegmentProjection:
    """一次段投影的结果（当前段 + 被隐藏数 + 已替代数）。"""

    segments: tuple[LifeTimelineSegment, ...] = ()
    hidden_count: int = 0
    superseded_count: int = 0


def project_segments(
    rows: Sequence[tuple[ScheduleSegmentRow, date]],
    *,
    window_start: datetime,
    window_end: datetime,
    is_owner: bool,
) -> SegmentProjection:
    """裁剪、过滤受众、算出 ``continues_*``；当前段与已替代段分开计数。

    ``rows`` 是 ``(段行, 该段所属计划日)``：计划日由 join 拿到，不从
    `start_at` 反推（两套时区不一时反推会算错日期）。
    """
    visible: list[LifeTimelineSegment] = []
    hidden = 0
    superseded = 0
    for row, plan_day in rows:
        clipped = clip_to_window(
            row.start_at, row.end_at, window_start, window_end
        )
        if clipped is None:
            continue
        if not visible_to(row.visibility, is_owner=is_owner):
            # 私有段：当前段计入"存在私有内容"，历史段既不返回也不计数
            # （它已经不是可见时间轴的一部分，多算一个数字只会误导）。
            if str(row.status) == "current":
                hidden += 1
            continue
        if str(row.status) != "current":
            # 已替代的段不进时间轴（会与当前段在时间上重叠），只计数：
            # 前端据此标"计划改动过"，而不是把两版都画出来。
            superseded += 1
            continue
        from_previous, to_next = continues_flags(
            row.start_at, row.end_at, window_start, window_end
        )
        visible.append(
            LifeTimelineSegment(
                segment_id=row.id,
                plan_id=row.plan_id,
                plan_day=plan_day,
                parent_id=row.parent_id,
                layer=row.layer,
                is_leaf=bool(row.is_leaf),
                status=str(row.status),
                revision=int(row.revision_created or 0),
                source=str(row.source),
                anchor_kind=str(row.anchor_kind or ""),
                commitment_level=str(row.commitment_level),
                place_id=row.place_id,
                title=row.title,
                start_at=_aware(row.start_at),
                end_at=_aware(row.end_at),
                display_start_at=clipped[0],
                display_end_at=clipped[1],
                continues_from_previous_day=from_previous,
                continues_next_day=to_next,
            )
        )
    visible.sort(key=lambda s: (s.display_start_at, s.display_end_at, s.segment_id))
    return SegmentProjection(
        segments=tuple(visible),
        hidden_count=hidden,
        superseded_count=superseded,
    )


def is_confirmed_anchor(segment: object) -> bool:
    """这一条段是不是"已确认的不可移动锚点"。

    `anchor_kind='commitment'` 来自已确认约定/周期规则；`commitment_level=
    confirmed` 的可变段也算（用户拍板过）。**预计睡眠不算**——它只是"预计休息
    窗"，不能当承诺展示（设计 §2）。
    """
    anchor_kind = str(getattr(segment, "anchor_kind", "") or "")
    if anchor_kind == COMMITMENT_ANCHOR:
        return True
    return anchor_kind == "" and str(getattr(segment, "commitment_level", "")) == "confirmed"


def select_today_outline(
    segments: Sequence[ScheduleSegmentRow],
    *,
    now: datetime,
    limit: int = OUTLINE_MAX_ITEMS,
) -> tuple[TodayOutlineItem, ...]:
    """从**角色当天**的段里挑 2–3 条最有信息量的摘要（当前段 + 下一条 + 锚点）。

    选条是确定性的代码规则，不是模型判断（设计 §7：仪表盘只做投影）：

    1. ``now``：覆盖此刻的**叶**段（正在进行的计划）。没有就是没有——不拿
       最近结束的那条冒充"正在做"；
    2. ``next``：此刻之后最近开始、且**不是**已确认锚点的叶段（"接下来可能
       做什么"）；
    3. ``anchor``：此刻之后最近的已确认锚点（见 :func:`is_confirmed_anchor`）。

    ``next`` 与 ``anchor`` 分开选，而不是先到先得：一条已确认约定既是最早的
    未来段、又是唯一的硬锚点，若只按时间取，``anchor`` 永远被去重掉——
    界面上就再也看不出"这条是约好的"。

    三者按需去重（同一段只出现一次），最多 ``limit`` 条；数量不足就是不足，
    **不**用已过去的段填满——那会让"下午还没定"看起来像"安排满了"。
    """
    moment = _aware(now)
    leaves = [
        seg
        for seg in segments
        if bool(getattr(seg, "is_leaf", False)) and str(getattr(seg, "status", "")) == "current"
    ]
    leaves.sort(key=lambda seg: (_aware(seg.start_at), _aware(seg.end_at), str(seg.id)))

    picks: list[TodayOutlineItem] = []
    used: set[str] = set()

    def _add(segment: ScheduleSegmentRow, relation: OutlineRelation) -> None:
        segment_id = str(segment.id)
        if segment_id in used or len(picks) >= limit:
            return
        used.add(segment_id)
        picks.append(
            TodayOutlineItem(
                segment_id=segment_id,
                relation=relation,
                title=str(segment.title),
                start_at=_aware(segment.start_at),
                end_at=_aware(segment.end_at),
            )
        )

    current = next(
        (
            seg
            for seg in leaves
            if _aware(seg.start_at) <= moment < _aware(seg.end_at)
        ),
        None,
    )
    if current is not None:
        _add(current, OutlineRelation.now)

    upcoming = [
        seg
        for seg in leaves
        if _aware(seg.start_at) > moment and not is_confirmed_anchor(seg)
    ]
    if upcoming:
        _add(upcoming[0], OutlineRelation.next)

    anchors = [
        seg
        for seg in leaves
        if is_confirmed_anchor(seg) and _aware(seg.end_at) > moment
    ]
    if anchors:
        _add(anchors[0], OutlineRelation.anchor)

    return tuple(picks)


def intent_teaser_items(
    intents: Sequence[IntentView], *, limit: int = TEASER_MAX_ITEMS
) -> tuple[IntentTeaserItem, ...]:
    """未来意向的预告条目：**只给有来源的**（``source_ref`` 为空的一律不出现）。

    已经按 `IntentService.list_active` 的展望顺序排好（状态优先、再比 priority、
    目标日、id），这里只做截断，不重排。
    """
    items: list[IntentTeaserItem] = []
    for view in intents:
        if not str(view.source_ref or "").strip():
            continue
        items.append(
            IntentTeaserItem(
                intention_id=view.intention_id,
                title=view.title,
                target_start_day=view.target_start_day,
                target_end_day=view.target_end_day,
                state=view.state,
                source_kind=view.source_kind,
            )
        )
        if len(items) >= limit:
            break
    return tuple(items)


# ---------------------------------------------------------------- 时区解析


@dataclass(frozen=True)
class TimezonePair:
    """展示侧（用户）与角色侧的时区解析结果，含来源。"""

    display_timezone: str
    display_timezone_source: str
    actor_timezone: str
    actor_timezone_source: str


async def resolve_timezones(
    session: AsyncSession, *, subject_id: str | None, env: str | None
) -> TimezonePair:
    """一次解析两套时区：登录者的（展示）与角色的（计划）。

    两套都走 `settings/user_preferences.py`：DB 偏好 > env > 默认。角色的取
    owner subject 的偏好——单角色部署里这就是那一个人；多用户共享角色时，
    非 owner 的展示时区**不改变**角色计划（设计 §2）。
    """
    display_name, display_source = await load_user_timezone(
        session, subject_id=subject_id, env=env
    )
    owner = await owner_subject_id(session)
    actor_name, actor_source = await load_user_timezone(
        session, subject_id=owner, env=env
    )
    return TimezonePair(
        display_timezone=display_name,
        display_timezone_source=display_source.value,
        actor_timezone=actor_name,
        actor_timezone_source=actor_source.value,
    )


async def resolve_actor_timezone(
    session: AsyncSession, *, env: str | None
) -> tuple[str, str]:
    """只要角色规范时区 ``(IANA 名, 来源)``（仪表盘用：它已单独解析过展示时区）。"""
    owner = await owner_subject_id(session)
    name, source = await load_user_timezone(session, subject_id=owner, env=env)
    return name, source.value


# ---------------------------------------------------------------- 只读查询


async def _load_plan(
    session: AsyncSession, *, actor_id: str, plan_day: date
) -> DayPlanRow | None:
    """这一计划日的版本头；没有行返回 ``None``（空态，不是故障）。"""
    return (
        await session.execute(
            select(DayPlanRow).where(
                DayPlanRow.actor_id == actor_id, DayPlanRow.plan_day == plan_day
            )
        )
    ).scalar_one_or_none()


async def _load_segment_rows(
    session: AsyncSession, *, actor_id: str, window_start: datetime, window_end: datetime
) -> list[tuple[ScheduleSegmentRow, date]]:
    """与窗口**相交**的段（含已替代段——它们只用于计数）。

    条件写成 ``start_at < window_end AND end_at > window_start`` 而不是按
    ``start_at`` 归日：跨午夜的段要在两天都出现，各自裁剪。
    """
    stmt = (
        select(ScheduleSegmentRow, DayPlanRow.plan_day)
        .join(DayPlanRow, ScheduleSegmentRow.plan_id == DayPlanRow.id)
        .where(
            ScheduleSegmentRow.actor_id == actor_id,
            ScheduleSegmentRow.status.in_(("current", "superseded")),
            ScheduleSegmentRow.start_at < window_end,
            ScheduleSegmentRow.end_at > window_start,
        )
        .order_by(ScheduleSegmentRow.start_at, ScheduleSegmentRow.end_at, ScheduleSegmentRow.id)
    )
    result = await session.execute(stmt)
    return [(row, plan_day) for row, plan_day in result.all()]


async def _load_facts(
    session: AsyncSession,
    *,
    actor_id: str,
    window_start: datetime,
    window_end: datetime,
    now: datetime,
    is_owner: bool,
) -> tuple[tuple[LifeTimelineFact, ...], int]:
    """已发生活动的投影 + 被受众隐藏的条数。

    只看真的发生过的状态（:data:`FACT_STATES`）：``proposed`` 还没发生，
    ``cancelled`` 是撤销。`scene_valid_until` 只在情景仍有效时给出——过期即
    unknown（设计 §8），不要把它读成"她还在做"。
    """
    scene = await session.get(SceneCommitRow, actor_id)
    scene_activity = str(scene.activity_id) if scene is not None and scene.activity_id else None
    scene_until = scene.valid_until if scene is not None else None
    if scene_until is not None and _aware(scene_until) <= _aware(now):
        scene_until = None  # 过期即 unknown，不冒充现状

    stmt = (
        select(ActivityRow)
        .where(
            ActivityRow.actor_id == actor_id,
            ActivityRow.state.in_(FACT_STATES),
            or_(ActivityRow.started_at.is_(None), ActivityRow.started_at < window_end),
            or_(ActivityRow.ended_at.is_(None), ActivityRow.ended_at >= window_start),
        )
        .order_by(ActivityRow.started_at, ActivityRow.id)
        .limit(FACTS_LIMIT)
    )
    rows = list((await session.execute(stmt)).scalars())

    facts: list[LifeTimelineFact] = []
    hidden = 0
    for row in rows:
        if not visible_to(row.visibility, is_owner=is_owner):
            hidden += 1
            continue
        facts.append(
            LifeTimelineFact(
                activity_id=row.id,
                state=str(row.state),
                origin=str(row.origin),
                evidence_kind=activity_evidence_kind(row).value,
                scene_valid_until=scene_until
                if scene_activity is not None and scene_activity == row.id
                else None,
                started_at=_aware(row.started_at) if row.started_at else None,
                ended_at=_aware(row.ended_at) if row.ended_at else None,
                place_id=row.place_id,
            )
        )
    return tuple(facts), hidden


async def _load_dream_report(
    session: AsyncSession, *, actor_id: str, sleep_date: date
) -> DreamReportRow | None:
    """苏醒日对应的梦（唯一键 ``(actor_id, sleep_date)``，至多一行）。"""
    return (
        await session.execute(
            select(DreamReportRow).where(
                DreamReportRow.actor_id == actor_id,
                DreamReportRow.sleep_date == sleep_date,
            )
        )
    ).scalar_one_or_none()


def dream_card(report: DreamReportRow | None, *, is_owner: bool) -> DreamCard | None:
    """梦的受众裁决：非 owner 只拿状态与来源，正文一律不返回。

    `dream=null` 表示"没有记录"；返回卡片但 `restricted=true` 表示"有记录但
    你看不到"。两者不能合并——前者是事实，后者是权限。
    """
    if report is None:
        return None
    if not is_owner:
        return DreamCard(
            status=str(report.status),
            origin=str(report.origin),
            visibility=str(report.visibility),
            title=None,
            narrative=None,
            restricted=True,
        )
    return DreamCard(
        status=str(report.status),
        origin=str(report.origin),
        visibility=str(report.visibility),
        title=report.title,
        narrative=report.narrative,
        restricted=False,
    )


def _intent_summary(view: IntentView) -> DayIntentSummary:
    return DayIntentSummary(
        intention_id=view.intention_id,
        target_start_day=view.target_start_day,
        target_end_day=view.target_end_day,
        state=view.state,
        source_kind=view.source_kind,
        visibility=view.visibility,
        priority=int(view.priority or 0),
    )


def intent_horizon(plan_day: date) -> tuple[date, date]:
    """展望的日窗口：角色计划日 **D+1 … D+7**（闭区间）。

    未来日**没有** `day_plans` 行——这一段只回答"接下来一周她心里有哪些
    有来源的目标"，不是提前生成的日计划（设计 §3A）。
    """
    return plan_day + timedelta(days=1), plan_day + timedelta(days=INTENT_HORIZON_DAYS)


async def _load_intents(
    session: AsyncSession, *, actor_id: str, plan_day: date, is_owner: bool
) -> tuple[tuple[DayIntentSummary, ...], int]:
    view = IntentService(session, actor_id=actor_id)
    start_day, end_day = intent_horizon(plan_day)
    items = await view.list_active(
        start_day=start_day, end_day=end_day, limit=INTENT_HORIZON_LIMIT
    )
    visible: list[DayIntentSummary] = []
    hidden = 0
    for item in items:
        if not visible_to(item.visibility, is_owner=is_owner):
            hidden += 1
            continue
        visible.append(_intent_summary(item))
    return tuple(visible), hidden


# ---------------------------------------------------------------- 装配


@dataclass(frozen=True)
class TimelineInputs:
    """装配一份时间轴投影所需的全部输入（都由只读查询/纯换算得到）。"""

    display_day: date
    display_timezone: str
    display_timezone_source: str
    actor_timezone: str
    actor_timezone_source: str
    is_owner: bool = False
    plan: DayPlanRow | None = None
    segment_rows: tuple[tuple[ScheduleSegmentRow, date], ...] = ()
    facts: tuple[LifeTimelineFact, ...] = ()
    hidden_fact_count: int = 0
    intents: tuple[DayIntentSummary, ...] = ()
    hidden_intent_count: int = 0
    dream: DreamCard | None = None
    note: str | None = None


def state_note(state: TimelineState, *, dream: DreamCard | None = None) -> str | None:
    """每种状态说一句人话。空态/降级/失败/不可用必须给不同的句子。"""
    if state is TimelineState.empty:
        return NOTE_EMPTY
    if state is TimelineState.pending:
        return NOTE_PENDING
    if state is TimelineState.degraded:
        return NOTE_DEGRADED
    if state is TimelineState.failed:
        return NOTE_FAILED
    if state is TimelineState.unavailable:
        return NOTE_UNAVAILABLE
    if dream is not None and dream.restricted:
        return NOTE_DREAM_RESTRICTED
    return None


def build_timeline(
    inputs: TimelineInputs, *, state: TimelineState | None = None
) -> LifeTimelineOut:
    """把输入装配成契约对象（本函数无 IO，单测可只喂内存行）。

    ``state`` 只给"读取出错"这一种情况覆盖用（:func:`unavailable_timeline`）；
    正常情况下由计划行推导（:func:`derive_state`）。
    """
    window = resolve_dual_window(
        inputs.display_day, inputs.display_timezone, inputs.actor_timezone
    )
    projection = project_segments(
        inputs.segment_rows,
        window_start=window.display_start_at,
        window_end=window.display_end_at,
        is_owner=inputs.is_owner,
    )
    state = state or derive_state(inputs.plan)
    plan = inputs.plan
    has_private = bool(
        projection.hidden_count
        or inputs.hidden_fact_count
        or inputs.hidden_intent_count
        or (inputs.dream is not None and inputs.dream.restricted)
    )
    return LifeTimelineOut(
        state=state,
        plan_state=str(plan.state) if plan is not None else None,
        revision=int(plan.revision or 0) if plan is not None else 0,
        last_error_code=str(plan.last_error_code) if plan is not None else None,
        display_date=window.display_date,
        display_timezone=inputs.display_timezone,
        display_timezone_source=inputs.display_timezone_source,
        display_window_start_at=window.display_start_at,
        display_window_end_at=window.display_end_at,
        actor_timezone=inputs.actor_timezone,
        actor_timezone_source=inputs.actor_timezone_source,
        plan_day=window.plan_day,
        plan_window_start_at=window.plan_start_at,
        plan_window_end_at=window.plan_end_at,
        outline_due_at=outline_due_at(window.plan_day, inputs.actor_timezone),
        timezone_source=str(plan.timezone_source)
        if plan is not None
        else inputs.actor_timezone_source,
        generated_at=_aware(plan.updated_at) if plan is not None and plan.updated_at else None,
        segments=list(projection.segments),
        facts=list(inputs.facts),
        intents=list(inputs.intents),
        horizon_unplanned=True,
        dream=inputs.dream,
        superseded_segment_count=projection.superseded_count,
        hidden_segment_count=projection.hidden_count,
        hidden_fact_count=inputs.hidden_fact_count,
        hidden_intent_count=inputs.hidden_intent_count,
        has_private_content=has_private,
        note=state_note(state, dream=inputs.dream) or inputs.note,
    )


def unavailable_timeline(
    *, display_day: date, display_timezone: str, display_timezone_source: str,
    actor_timezone: str, actor_timezone_source: str,
) -> LifeTimelineOut:
    """读取出错时的投影：**保留双时区换算结果**，只把数据部分标成不可用。

    为什么不是 500：界面要能区分"这一天没安排"和"这次读不到"。时区是纯换算，
    与数据库无关，所以它照常给出——否则前端连"看的是哪一天"都显示不出来。
    """
    return build_timeline(
        TimelineInputs(
            display_day=display_day,
            display_timezone=display_timezone,
            display_timezone_source=display_timezone_source,
            actor_timezone=actor_timezone,
            actor_timezone_source=actor_timezone_source,
        ),
        state=TimelineState.unavailable,
    )


# ---------------------------------------------------------------- 仪表盘用


@dataclass(frozen=True)
class ActorDayView:
    """角色**今天**（角色时区）的计划头与段：仪表盘摘要的输入。"""

    plan_day: date
    state: TimelineState
    plan: DayPlanRow | None = None
    segments: tuple[ScheduleSegmentRow, ...] = field(default_factory=tuple)


async def load_actor_day(
    session: AsyncSession, *, actor_id: str, now: datetime, actor_timezone: str
) -> ActorDayView:
    """只读：角色当天（``now`` 落在哪个计划日）的计划与段。"""
    plan_day = plan_day_for(now, actor_timezone)
    window_start, window_end = plan_day_window(plan_day, actor_timezone)
    plan = await _load_plan(session, actor_id=actor_id, plan_day=plan_day)
    rows = await _load_segment_rows(
        session, actor_id=actor_id, window_start=window_start, window_end=window_end
    )
    segments = tuple(row for row, _day in rows if str(row.status) == "current")
    return ActorDayView(
        plan_day=plan_day,
        state=derive_state(plan),
        plan=plan,
        segments=segments,
    )


# ---------------------------------------------------------------- 路由


@router.get("/life/timeline", response_model=LifeTimelineOut)
async def get_life_timeline(
    auth: CurrentAuth,
    session: DbSession,
    settings: SettingsDep,
    day: date | None = Query(
        default=None,
        alias="date",
        description="展示日（YYYY-MM-DD）；不传 = 当前登录者时区的今天",
    ),
) -> LifeTimelineOut:
    """一天的只读时间轴：双时区、段、事实、未来意向、梦卡片。"""
    actor_id = settings.actor_id
    now = datetime.now(UTC)

    pair = await resolve_timezones(
        session, subject_id=auth.user.subject_id, env=settings.user_timezone
    )
    owner_subject = await owner_subject_id(session)
    is_owner = viewer_is_owner(
        role=auth.user.role,
        subject_id=auth.user.subject_id,
        owner_subject=owner_subject,
    )
    display_day = day or display_day_for(now, pair.display_timezone)
    window = resolve_dual_window(
        display_day, pair.display_timezone, pair.actor_timezone
    )

    try:
        plan = await _load_plan(session, actor_id=actor_id, plan_day=window.plan_day)
        segment_rows = await _load_segment_rows(
            session,
            actor_id=actor_id,
            window_start=window.display_start_at,
            window_end=window.display_end_at,
        )
        facts, hidden_facts = await _load_facts(
            session,
            actor_id=actor_id,
            window_start=window.display_start_at,
            window_end=window.display_end_at,
            now=now,
            is_owner=is_owner,
        )
        report = await _load_dream_report(
            session, actor_id=actor_id, sleep_date=window.plan_day
        )
        intents, hidden_intents = await _load_intents(
            session, actor_id=actor_id, plan_day=window.plan_day, is_owner=is_owner
        )
    except Exception as exc:  # noqa: BLE001 - 读失败要能被界面区分，不是 500
        logger.warning(
            "生活时间轴读取失败（actor=%s day=%s）：%s: %s",
            actor_id,
            display_day,
            type(exc).__name__,
            exc,
        )
        try:
            # 一条失败的查询会让请求级 session 进入"需回滚"状态；
            # 不回滚的话后续请求会一起报错（仪表盘踩过同一个坑）。
            await session.rollback()
        except Exception:  # noqa: BLE001 - 回滚失败交给请求生命周期
            logger.warning("生活时间轴回滚失败")
        return unavailable_timeline(
            display_day=display_day,
            display_timezone=pair.display_timezone,
            display_timezone_source=pair.display_timezone_source,
            actor_timezone=pair.actor_timezone,
            actor_timezone_source=pair.actor_timezone_source,
        )

    return build_timeline(
        TimelineInputs(
            display_day=display_day,
            display_timezone=pair.display_timezone,
            display_timezone_source=pair.display_timezone_source,
            actor_timezone=pair.actor_timezone,
            actor_timezone_source=pair.actor_timezone_source,
            is_owner=is_owner,
            plan=plan,
            segment_rows=tuple(segment_rows),
            facts=facts,
            hidden_fact_count=hidden_facts,
            intents=intents,
            hidden_intent_count=hidden_intents,
            dream=dream_card(report, is_owner=is_owner),
        )
    )


__all__ = [
    "FACTS_LIMIT",
    "INTENT_HORIZON_DAYS",
    "INTENT_HORIZON_LIMIT",
    "OUTLINE_MAX_ITEMS",
    "TEASER_MAX_ITEMS",
    "ActorDayView",
    "DualWindow",
    "SegmentProjection",
    "TimelineInputs",
    "TimezonePair",
    "build_timeline",
    "clip_to_window",
    "continues_flags",
    "derive_state",
    "dream_card",
    "intent_horizon",
    "intent_teaser_items",
    "is_confirmed_anchor",
    "load_actor_day",
    "project_segments",
    "resolve_actor_timezone",
    "resolve_dual_window",
    "resolve_timezones",
    "router",
    "select_today_outline",
    "state_note",
    "unavailable_timeline",
    "viewer_is_owner",
    "visible_to",
]
