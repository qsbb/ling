"""仪表盘：一次请求返回首屏需要的所有片段。

## 为什么是一个聚合接口

首屏要显示六七块数据（她此刻在干嘛、情绪、有没有想找你、最近记住什么、
关系、渠道通不通）。如果每块各发一个请求，首屏就会被最慢的那块拖住，
而且任何一块失败都会让整页变成错误页。

所以这里一次返回，并且**每块单独标状态**：某块查失败只影响它自己。

## 分段结构

```json
{
  "schema_version": "v1",
  "generated_at": "...",
  "timezone": "Asia/Shanghai",
  "sections": {
    "activity":   {"status": "ok|empty|unavailable", "as_of": "...", "data": {...}},
    ...
  }
}
```

`status` 三态的意义：

- `ok`：查到了且非空
- `empty`：查到了，但确实没有内容（"她此刻没有登记中的活动"）
- `unavailable`：查询本身失败

**空数据和查询失败不能混在一起**：前者是事实，后者是故障。混在一起会让
"她没在做什么"和"读不到她在做什么"长得一模一样，而这两件事的处理方式完全不同。

`as_of` 是这块数据的采集时刻，前端据此标注"最近更新于"，不要写成"实时"。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter

from ling.api.deps import ConfigDep, CurrentAuth, DbSession, SettingsDep
from ling.api.routes.life_timeline import (
    INTENT_HORIZON_LIMIT,
    OUTLINE_MAX_ITEMS,
    TEASER_MAX_ITEMS,
    intent_horizon,
    intent_teaser_items,
    load_actor_day,
    resolve_actor_timezone,
    select_today_outline,
    state_note,
    viewer_is_owner,
    visible_to,
)
from ling.contracts.v1 import (
    FallbackNotice,
    IntentTeaserOut,
    TimelineState,
    TodayOutlineOut,
)
from ling.domains.activity.service import ActivityService
from ling.domains.affect.service import AffectService
from ling.domains.agenda.intents import IntentService
from ling.domains.agenda.service import AgendaService, local_today
from ling.domains.agenda.timeline import display_day_for, plan_day_for
from ling.domains.fallback import (
    UPSTREAM_READ_BUDGET_SECONDS,
    DependencyState,
    DependencyValue,
    read_within_budget,
)
from ling.domains.goals.service import GoalsService
from ling.domains.memory.self_timeline import SelfTimelineService
from ling.domains.memory.service import MemoryService
from ling.domains.phone.service import PhoneService, ScheduleNow, schedule_dependency
from ling.domains.presence.service import PresenceService
from ling.domains.proactive.service import ProactivityService
from ling.domains.relationship.service import RelationshipService
from ling.settings.user_preferences import owner_subject_id

logger = logging.getLogger("ling.api.dashboard")

router = APIRouter(tags=["dashboard"])

# 首屏读日程的预算。单独命名是为了**可测**（测试把它调小来验证超时回退），
# 也为了让"首屏最多等多久"这件事有一个能 grep 到的常量。
SCHEDULE_READ_BUDGET_SECONDS = UPSTREAM_READ_BUDGET_SECONDS

# 首屏只需要摘要，不需要整页数据
MEMORY_LIMIT = 3
SECTION_OK = "ok"
SECTION_EMPTY = "empty"
SECTION_UNAVAILABLE = "unavailable"


def _section(status: str, data: Any = None) -> dict:
    return {
        "status": status,
        "as_of": datetime.now(UTC).isoformat(),
        "data": data,
    }


async def _schedule_dependency(
    session: Any, *, actor_id: str, settings: Any
) -> DependencyValue[ScheduleNow]:
    """在**有界预算**内读一次日程，折成回退契约的输入。

    为什么要有界：这一段挂在首屏（以及"下次什么时候看手机"）。日程可能因为
    生成/查询变慢，而"她什么时候看手机"是轻量信息——不能因为上游慢就整页
    卡住。超时按 `unavailable` 回退到本域默认值（见 `domains/fallback.py`）。
    """
    moment = datetime.now(UTC)
    actor_timezone, _source = await resolve_actor_timezone(
        session, env=getattr(settings, "user_timezone", None)
    )

    async def _read() -> object:
        return await load_actor_day(
            session, actor_id=actor_id, now=moment, actor_timezone=actor_timezone
        )

    read = await read_within_budget(
        _read(), upstream="schedule", budget=SCHEDULE_READ_BUDGET_SECONDS
    )
    if read.state is not DependencyState.ready:
        return DependencyValue(
            state=read.state, upstream="schedule", reason=read.reason
        )
    return schedule_dependency(read.value, now=moment)


def _fallback_payload(notice: dict[str, object]) -> dict[str, object]:
    """用契约模型把标记定型（形状只有一份，测试也照着它断言）。"""
    return FallbackNotice.model_validate(notice).model_dump(mode="json")


async def _guard(name: str, coro: Any, session: Any) -> dict:
    """跑一个片段的查询；失败只标记这一块，不影响整页。

    **失败后必须 rollback**：所有片段共用同一个请求级 session，一条失败的
    查询会让它进入"需要回滚"状态，后面每一块都会跟着报错——表面上像
    "每块都读不到"，实际只坏了一块（实测在部署实例上：记忆那块的失败把
    会话搞脏，于是模型绑定也被读成空）。
    """
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 - 单块失败不能拖垮首屏
        logger.warning(
            "仪表盘片段 %s 查询失败：%s: %s", name, type(exc).__name__, exc
        )
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 - 回滚都失败就交给上层的请求生命周期
            logger.warning("仪表盘片段 %s 回滚失败", name)
        return _section(SECTION_UNAVAILABLE)


@router.get("/dashboard")
async def get_dashboard(
    auth: CurrentAuth,
    session: DbSession,
    settings: SettingsDep,
    config: ConfigDep,
) -> dict:
    actor_id = settings.actor_id
    subject_id = auth.user.subject_id
    # "今天"按**用户配置的本地时区**算，而不是 UTC 或服务器时区：
    # 响应顶层的 `timezone` 已经是这个值，日程的日界必须与它一致，
    # 否则东八区 00:00-08:00 的"今天"会退回前一天。
    #
    # 时区**只从这里取**：`ling/settings/user_preferences.py`（DB 偏好 > env
    # > Asia/Shanghai）。原来这里 `or "UTC"`，于是设置里根本没有时区却按 UTC
    # 划日界——用户看到的"今天"和她的勿扰时段是两个时区算出来的。
    from ling.settings.user_preferences import load_user_timezone

    user_timezone, timezone_source = await load_user_timezone(
        session, subject_id=subject_id, env=settings.user_timezone
    )

    async def activity() -> dict:
        current = await ActivityService(session, actor_id=actor_id).current()
        if current is None:
            return _section(SECTION_EMPTY)
        return _section(
            SECTION_OK,
            {
                "activity_id": current.activity_id,
                "kind": current.kind,
                "state": current.state,
                "place_id": current.place_id,
                # 来源必须带上：simulated（她自己的生活模拟）与 tool_receipt
                # （真实回执）在界面上不能长得一样
                "origin": current.origin,
                "started_at": current.started_at.isoformat()
                if current.started_at
                else None,
            },
        )

    async def affect() -> dict:
        view = await AffectService(session, actor_id=actor_id).current()
        return _section(
            SECTION_OK,
            {
                "emotion": view.emotion,
                "intensity": view.intensity,
                "cause": view.cause,
                "tone_hint": view.tone_hint(),
                "updated_at": view.updated_at.isoformat() if view.updated_at else None,
            },
        )

    async def proactive() -> dict:
        service = ProactivityService(session, actor_id=actor_id)
        pending = await service.pending(subject_id)

        # **不能写死 delivery_enabled**：原来写死 False，于是门控永远说
        # "当前渠道不支持主动投递"，无论渠道配得多完整都不可能主动联系。
        delivery_enabled = False
        gate_reason = "未探测渠道能力"
        try:
            from ling.capabilities.channels import proactive_connector_status

            connector, reason = proactive_connector_status()
            delivery_enabled = connector is not None
            gate_reason = reason or ""
        except Exception as exc:  # noqa: BLE001 - 探测失败按不可投递
            gate_reason = f"渠道探测失败（{type(exc).__name__}）"

        if not pending:
            return _section(SECTION_EMPTY, {"delivery_enabled": delivery_enabled,
                                            "gate_reason": gate_reason})
        top = pending[0]
        return _section(
            SECTION_OK,
            {
                "pending_count": len(pending),
                "delivery_enabled": delivery_enabled,
                "gate_reason": gate_reason,
                # 候选不是"已经联系过"，文案必须区分
                "top": {
                    "opportunity_id": top.opportunity_id,
                    "kind": top.kind,
                    "reason": top.reason,
                    "state": top.state,
                    # None = 还没生成（不是"生成了空的"），前端要能区分
                    "draft_text": top.draft_text,
                    "expires_at": top.expires_at.isoformat(),
                },
            },
        )

    async def memories() -> dict:
        rows = await MemoryService(session).list_memories(
            subject_id, limit=MEMORY_LIMIT
        )
        live = [r for r in rows if not r.retracted]
        if not live:
            return _section(SECTION_EMPTY)
        return _section(
            SECTION_OK,
            {
                "items": [
                    {
                        "memory_id": r.id,
                        "kind": r.kind,
                        "text": r.text,
                        "origin": r.origin,
                        "confidence": r.confidence,
                    }
                    for r in live
                ]
            },
        )

    async def relationship() -> dict:
        service = RelationshipService(session, actor_id=actor_id)
        state = await service.state(subject_id)
        view = service.view(state)
        return _section(
            SECTION_OK,
            {
                "familiarity": view.familiarity,
                "trust": view.trust,
                "boundaries": view.boundaries,
                "last_interaction_at": view.last_interaction_at.isoformat()
                if view.last_interaction_at
                else None,
            },
        )

    async def presence() -> dict:
        """可用性：精力、社交负担，以及**唯一的**结论 availability。

        不暴露原始数值的判断方式给前端各自解读——那会出现每处一套阈值。
        """
        service = PresenceService(session, actor_id=actor_id)
        view = await service.view()
        # 场景（她此刻在做什么）是有有效期的：过期就是**不知道**，不是"还在做"。
        # 这里用 presence 域的默认回退（`unknown`），并把"这次是不是回退"标出来，
        # 免得前端把 unknown 读成"她闲着"。见 `domains/fallback.py`。
        scene, scene_input = service.resolve_scene(await service.scene())
        return _section(
            SECTION_OK,
            {
                "availability": view.availability,
                "energy": view.energy,
                "social_load": view.social_load,
                "scene": {
                    "state": scene.state,
                    "expired": scene.expired,
                    # 只给状态，不给自由文本：scene 的 activity/place 属于事实层，
                    # 首屏不需要，也不该在这里被读成"她正在做 X"。
                },
                "fallback": _fallback_payload(scene_input.notice()),
                # `describe()` 是给表达层的一句话：正常时它**故意返回空串**
                # （正常不需要额外约束）。首屏上留白会让人以为没读到，
                # 所以这里补一句人话，但实质仍是那个唯一结论。
                "describe": view.describe() or "状态正常，可以随时说话",
            },
        )

    async def agenda() -> dict:
        """今天的安排。日程、承诺、目标**分开**给，不许互相冒充。

        合成一个"今天要做什么"的列表会让人以为计划等于承诺：计划是她
        "打算"做的事（tentative 甚至可能变），承诺是她答应了对方的事。

        **"今天"的边界**：按配置的用户本地时区（``user_timezone``）取当地
        日历日 ``[00:00, 次日 00:00)``，只返回 ``start_at`` 落在当天的日程。
        此前这里直接取 ``upcoming()`` 的前 5 条、不加任何时间窗，于是过期
        日程会一直留在"今天的安排"里（TASK84 §4.3 实测 10-31 返回 10-30）。

        承诺默认只给仍待履行的 ``active``；``cancelled`` / ``undeliverable``
        等历史状态不再混进来（见 ``GoalsService.list_commitments``）。

        响应里计划仍带 ``start_at``（ISO-8601、UTC 偏移）：前端用它显示
        日期并做"不是今天"的第二道兜底，字段名与语义都保持不变。
        """
        service = AgendaService(session, actor_id=actor_id)
        today = local_today(user_timezone)
        plans = await service.upcoming(limit=5, on_date=today, timezone=user_timezone)
        goals_service = GoalsService(session, actor_id=actor_id)
        commitments = await goals_service.list_commitments(subject_id)
        goals = await goals_service.list_goals(state="active")

        if not plans and not commitments and not goals:
            return _section(SECTION_EMPTY)
        return _section(
            SECTION_OK,
            {
                "plans": [
                    {
                        "plan_id": v.plan_id,
                        "title": v.title,
                        "start_at": v.start_at.isoformat(),
                        "end_at": v.end_at.isoformat(),
                        "commitment_level": v.commitment_level,
                        "state": v.state,
                    }
                    for v in plans
                ],
                "commitments": [
                    {
                        "commitment_id": c.commitment_id,
                        "text": c.text,
                        "due_at": c.due_at.isoformat(),
                        "state": c.state,
                    }
                    for c in commitments[:5]
                ],
                "goals": [
                    {"goal_id": g.goal_id, "title": g.title, "state": g.state}
                    for g in goals[:5]
                ],
            },
        )

    async def today_outline() -> dict:
        """「今天她可能做什么」：时间轴上的 2–3 条摘要。

        与旧的 ``agenda`` **并列、互不改义**：``agenda`` 是"最多几件小事"
        （计划/承诺/目标），这里回答的是"她这一天的时间轴现在到哪儿了"。

        ## 为什么不是"当前活动"

        `activity` 那块说的是**已发生**的模拟活动进程；这块说的是**计划**。
        两者可以同时为空、也可以互相矛盾（计划 14:00 看书，实际在聊天），
        所以必须分开显示——把计划时段说成"正在做"是本项目明令禁止的
        事实伪造（设计 §1）。``relation`` 字段（now/next/anchor）就是这个
        区分的载体：``next`` 是"可能"，不是"在做"。

        ## 为什么给两个日期

        ``display_date`` 是**你**的今天（展示时区），``plan_day`` 是**她**的
        今天（角色规范时区）。两套时区不同时它们可以不是同一天，前端不许
        用浏览器本地时区再算一次。
        """
        moment = datetime.now(UTC)
        actor_timezone, _source = await resolve_actor_timezone(
            session, env=settings.user_timezone
        )
        view = await load_actor_day(
            session, actor_id=actor_id, now=moment, actor_timezone=actor_timezone
        )
        items = select_today_outline(
            view.segments, now=moment, limit=OUTLINE_MAX_ITEMS
        )
        data = TodayOutlineOut(
            state=view.state,
            display_date=display_day_for(moment, user_timezone),
            display_timezone=user_timezone,
            plan_day=view.plan_day,
            actor_timezone=actor_timezone,
            items=list(items),
            note=state_note(view.state),
        )
        status = SECTION_EMPTY if not items else SECTION_OK
        if not items and view.state is not TimelineState.empty:
            # 有计划行却没有可展示的段（还没排、或全被替代）：这不是"没安排"，
            # 而是"今天暂时没什么特别安排"——状态由 data.state 说清。
            status = SECTION_OK
        return _section(status, data.model_dump(mode="json"))

    async def intent_teaser() -> dict:
        """未来意向的 1–2 条预告，并明确"未来日未生成大纲"。

        为什么只给有来源的：``schedule_intentions`` 里 ``proposed`` 是模型猜的
        候选；把没有来源的候选放到首屏，等于把"可能"包装成"安排"。这里只取
        `source_ref` 非空的条目，并把 ``source_kind`` 一起给出，用户能看出
        是谁提的。
        """
        moment = datetime.now(UTC)
        actor_timezone, _source = await resolve_actor_timezone(
            session, env=settings.user_timezone
        )
        plan_day = plan_day_for(moment, actor_timezone)
        start_day, end_day = intent_horizon(plan_day)
        owner_subject = await owner_subject_id(session)
        is_owner = viewer_is_owner(
            role=auth.user.role,
            subject_id=auth.user.subject_id,
            owner_subject=owner_subject,
        )
        views = await IntentService(session, actor_id=actor_id).list_active(
            start_day=start_day, end_day=end_day, limit=INTENT_HORIZON_LIMIT
        )
        visible = [
            view
            for view in views
            if visible_to(view.visibility, is_owner=is_owner)
        ]
        items = intent_teaser_items(visible, limit=TEASER_MAX_ITEMS)
        data = IntentTeaserOut(
            horizon_unplanned=True,
            items=list(items),
            note="未来日尚未生成大纲；这里是意向，不是那一天的安排"
            if items
            else "未来 7 天还没有有来源的意向",
        )
        return _section(SECTION_OK if items else SECTION_EMPTY, data.model_dump(mode="json"))

    async def timeline() -> dict:
        """近期生活：已发生的事件，带来源。

        不是"她最近的念头"——内部推理不能当角色经历展示。
        """
        service = SelfTimelineService(session, actor_id=actor_id)
        events = await service.recent(days=3, limit=5)
        if not events:
            return _section(SECTION_EMPTY)
        return _section(
            SECTION_OK,
            {
                "items": [
                    {
                        "experience_id": e.experience_id,
                        "kind": e.kind,
                        "summary": e.summary,
                        "origin": e.origin,
                        "occurred_at": e.occurred_at.isoformat(),
                    }
                    for e in events
                ]
            },
        )

    async def connectivity() -> dict:
        from ling.capabilities.channels import (
            qqbot_config,
            qqbot_connector_status,
        )

        # **"配置齐备"与"能主动推送"是两件事**。
        # `qqbot_connector_status()` 在"凭据齐全但没申请主动推送"时会返回
        # (None, 原因)——用它判断 configured 会把配好的渠道误报成未配置
        # （实测在部署实例上就报错了）。configured 只看凭据，主动能力单独说。
        channels: list[dict] = []
        try:
            qq_config = qqbot_config()
            _connector, status_reason = qqbot_connector_status()
            channels.append(
                {
                    "channel": "qqbot",
                    "configured": qq_config.configured,
                    "reason": ""
                    if qq_config.configured
                    else "凭据不全（APP_ID / CLIENT_SECRET）",
                    # 主动推送能力是平台权限，不是配置
                    "proactive_ready": _connector is not None,
                    "proactive_reason": status_reason or "",
                }
            )
        except Exception as exc:  # noqa: BLE001
            channels.append(
                {
                    "channel": "qqbot",
                    "configured": False,
                    "reason": f"探测失败（{type(exc).__name__}）",
                }
            )

        # OneBot 的配置是纯环境变量，没有"连接器对象"可探测——这里只报
        # 配置是否齐备，**不冒充连通性**（真正能不能收消息只有日志知道）。
        onebot_configured = bool(
            settings.onebot_ws_port or settings.onebot_ws_url or settings.onebot_ws_token
        )
        channels.append(
            {
                "channel": "onebot",
                "configured": onebot_configured,
                "reason": "" if onebot_configured else "未配置 LING_ONEBOT_*",
            }
        )

        # 模型职责有没有绑定：没绑 = 她说不出话，这是首屏必须能看出来的
        bound: list[str] = []
        try:
            effective = await config.effective()
            bound = [
                name
                for name, binding in effective.profiles.items()
                if getattr(binding, "provider_id", None)
            ]
        except Exception as exc:  # noqa: BLE001 - 读不到就按未绑定报
            # 不静默：这条一旦吞掉，界面会显示"没绑定模型"，
            # 而真实原因是读配置出错——两者排查方向完全相反。
            logger.warning(
                "仪表盘读模型绑定失败：%s: %s", type(exc).__name__, exc
            )
            bound = []

        return _section(
            SECTION_OK,
            {"channels": channels, "bound_model_roles": sorted(bound)},
        )

    async def phone() -> dict:
        """她此刻跟手机的关系（在不在看、看什么、下次什么时候看）。

        为什么要单列一块：她的状态不只是"在做什么活动"，还有一层更日常的
        ——**她是不是正在低头看手机**。真人一天解锁几十上百次，那段"在忙别的"
        的时间里她不回消息，不是因为不想理人。把这件事显式画出来，用户才不会
        把"没秒回"读成"装死"。

        空态与故障必须分开：从没看过手机（没有记录）是**事实**，返回 empty；
        读取出错由 `_guard` 标成 unavailable。两者在界面上长得不一样。
        """
        view = await PhoneService(session, actor_id=actor_id).current()
        # 「下次什么时候看手机」由日程决定（在忙拉长、睡着不排）。日程是**上游**：
        # 还没生成 / 生成失败 / 读不到时，用本域自己的默认节奏跑这一次，
        # 并在 `fallback` 里说清"这次用了默认值，因为……"。见 `domains/fallback.py`。
        interval = PhoneService(
            session, actor_id=actor_id
        ).interval_from_schedule(
            await _schedule_dependency(session, actor_id=actor_id, settings=settings)
        )
        fallback = _fallback_payload(interval.notice())
        never_looked = (
            view.last_looked_at is None
            and view.next_look_at is None
            and view.watching_since is None
        )
        if never_looked:
            # 从没看过手机是**事实**（empty），但"下次该隔多久"是**参数**：
            # 它照样有值（默认节奏或日程给的），不该跟着状态一起消失——
            # 否则用户在最常见的空态里反而看不到回退说明。
            # **不放 `state`**：从没看过就是"没有状态"，编一个 idle 会把
            # "她没拿过手机"说成"她此刻闲置"。只放参数与它的来源。
            data: dict[str, object] = {
                "next_look_interval_minutes": interval.minutes,
                "fallback": fallback,
            }
            return _section(SECTION_EMPTY, data)
        return _section(
            SECTION_OK,
            {
                "state": view.state,
                # 上游给出的节奏（或本域默认值）与它的来源。**这是参数，不是事实**：
                # 它只说"她大概多久看一眼手机"，不说她此刻在做什么。
                "next_look_interval_minutes": interval.minutes,
                "fallback": fallback,
                "watching_app": view.watching_app,
                "watching_since": (
                    view.watching_since.isoformat() if view.watching_since else None
                ),
                "last_looked_at": (
                    view.last_looked_at.isoformat() if view.last_looked_at else None
                ),
                "next_look_at": (
                    view.next_look_at.isoformat() if view.next_look_at else None
                ),
                "last_result": view.last_result,
                # **不**投影 `last_context`：它是 128 字符的自由文本，可能是
                # "群里在聊……" 这类会带出聊天内容的东西。首屏只需要知道
                # "她在看/看什么 app/下次什么时候看"，不需要那一段。
            },
        )

    sections = {
        "activity": await _guard("activity", activity(), session),
        "affect": await _guard("affect", affect(), session),
        "proactive": await _guard("proactive", proactive(), session),
        "memories": await _guard("memories", memories(), session),
        "relationship": await _guard("relationship", relationship(), session),
        "connectivity": await _guard("connectivity", connectivity(), session),
        "presence": await _guard("presence", presence(), session),
        "agenda": await _guard("agenda", agenda(), session),
        # 新增两块与 agenda 并列：旧 agenda 的"最多几件小事"语义一点不改。
        "today_outline": await _guard("today_outline", today_outline(), session),
        "intent_teaser": await _guard("intent_teaser", intent_teaser(), session),
        "timeline": await _guard("timeline", timeline(), session),
        "phone": await _guard("phone", phone(), session),
    }

    return {
        "schema_version": "v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "timezone": user_timezone,
        # 这个时区是用户自己设的、环境变量给的，还是默认值？界面上的
        # "今天"就按它算，用户有权知道它从哪来（分不清时就以为是自己的设置）。
        "timezone_source": timezone_source.value,
        "sections": sections,
    }
