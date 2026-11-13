"""管理/角色 API：记忆、关系、情绪、生活、目标、授权、渠道。

全部走鉴权后的命令/查询 API；管理台不能直接改表（设计文档 §3）。
这些端点属于 W2，**已实现未测试**：W0 回归不覆盖它们。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, time
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ling.api.deps import ConfigDep, CurrentAuth, DbSession, SettingsDep
from ling.api.errors import LingError
from ling.audit import audit_event
from ling.config import Settings
from ling.contracts.v1 import (
    SCHEMA_VERSION,
    ActionIntent,
    ErrorCode,
    MemoryProposal,
    Origin,
    Visibility,
)
from ling.domains.activity.service import ActivityService, InvalidTransition
from ling.domains.affect.service import AffectService
from ling.domains.consent.service import KNOWN_SCOPES, ConsentService
from ling.domains.conversation.service import ConversationService
from ling.domains.goals.service import GoalsService
from ling.domains.knowledge.service import KnowledgeService
from ling.domains.memory.service import MemoryService
from ling.domains.proactive.service import ProactivityService
from ling.domains.relationship.service import RelationshipService
from ling.domains.world.service import WorldService

if TYPE_CHECKING:  # 仅为类型标注：core 在端点内按需导入，避免装配期多一层依赖
    from ling.domains.memory.core import CoreMemoryView
    from ling.infra.models import Subject

router = APIRouter(tags=["manage"])


def _actor_id(settings: SettingsDep) -> str:
    return settings.actor_id


# ------------------------------------------------------------------ 记忆


# 列表可见集：默认只看现行记忆；墓碑必须显式 include。
# 值本身进契约（docs/API-WEB-V1.md §10），不要在客户端猜。
# 用有序元组：取值顺序就是 /openapi.json 里 `enum` 的顺序，生成客户端
# 不会看到集合的随机排列。
MEMORY_INCLUDE_MODES = ("retracted", "archived", "all")
MEMORY_KINDS = ("episode", "assertion", "procedure")
# pattern 从 MEMORY_KINDS 派生：端点接受的取值与 /openapi.json 写出的取值
# 只有一处来源，不会漂移。
MEMORY_KIND_PATTERN = f"^({'|'.join(MEMORY_KINDS)})$"
MEMORY_LIMIT_MIN = 1
MEMORY_LIMIT_MAX = 200
MEMORY_LIMIT_DEFAULT = 50


class MemoryCreate(BaseModel):
    # 约束一律留在 pydantic 里，**不要**为了错误形状把它挪进端点：
    # 挪走会让 /openapi.json 丢掉 required/pattern，生成客户端据此拼出
    # 必被服务端拒绝的请求（task-105 的 Medium 回归）。
    # 记忆端点上的 RequestValidationError 由 create_app 注册的、按路径前缀
    # 生效的处理器统一成 ErrorResponse（docs/API-WEB-V1.md §10）。
    kind: str = Field(pattern=MEMORY_KIND_PATTERN)
    text: str = Field(min_length=1, max_length=4000)
    origin: str = "user_statement"
    visibility: str = "subject_private"
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    source_event_ids: list[str] = Field(default_factory=list)


class MemoryRetract(BaseModel):
    # 必填 + 1..500：机器契约与运行时是同一条约束。整个 body 缺失时
    # pydantic 报的也是 RequestValidationError，走同一个前缀处理器。
    reason: str = Field(min_length=1, max_length=500)


class MemorySupersede(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    reason: str = Field(default="用户纠正", max_length=500)


@router.get("/memory")
async def list_memory(
    auth: CurrentAuth,
    session: DbSession,
    # `limit` 的范围只在端点判定，OpenAPI 刻意**不**写 minimum/maximum
    # （见 docs/API-WEB-V1.md §10）：加进 schema 会改变契约口径。
    limit: Annotated[
        int,
        Query(description="单次最多返回条数，范围 1..200；超出范围返回 422。"),
    ] = MEMORY_LIMIT_DEFAULT,
    # `include` 的三值写进 OpenAPI 的 `enum`（`json_schema_extra` 只影响生成的
    # 文档，不参与运行时校验）；非法值仍由下面的端点级 LingError 统一成 422，
    # 因此它的 `detail` 是 null 而不是 pydantic 原文。
    include: Annotated[
        str | None,
        Query(
            description="在默认可见集上追加哪类墓碑：retracted、archived 或 all；其它值返回 422。",
            json_schema_extra={"enum": list(MEMORY_INCLUDE_MODES)},
        ),
    ] = None,
) -> dict:
    """列出当前自然人**现行**的长期记忆。

    默认不返回已撤回（``retracted=true``）或已归档（``archived_at`` 非空）
    的行——它们是墓碑，仍留在库里供证据链追溯，但不该混进"还记得什么"。
    要取回墓碑用 ``?include=retracted|archived|all``。数据没有删除。
    """
    if not MEMORY_LIMIT_MIN <= limit <= MEMORY_LIMIT_MAX:
        raise LingError(
            ErrorCode.invalid_request,
            f"limit 必须在 {MEMORY_LIMIT_MIN}..{MEMORY_LIMIT_MAX} 之间",
            status_code=422,
        )
    if include is not None and include not in MEMORY_INCLUDE_MODES:
        raise LingError(
            ErrorCode.invalid_request,
            "include 只能是 retracted、archived 或 all",
            status_code=422,
        )
    effective = include or "active"
    service = MemoryService(session)
    rows = await service.list_memories(auth.user.subject_id, limit=limit, include=effective)
    total = await service.count_memories(auth.user.subject_id, include=effective)
    return {
        "memories": [
            {
                "memory_id": r.id,
                "kind": r.kind,
                "text": r.text,
                "origin": r.origin,
                "visibility": r.visibility,
                "confidence": r.confidence,
                "retracted": r.retracted,
                "retract_reason": r.retract_reason,
                "supersedes": r.supersedes,
                "superseded_by": r.superseded_by,
                "source_event_ids": r.source_event_ids or [],
                "valid_until": r.valid_until.isoformat() if r.valid_until else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "archived_at": r.archived_at.isoformat() if r.archived_at else None,
            }
            for r in rows
        ],
        # 无分页（没有 offset/cursor）；limit 只截断，total/has_more 说明还有没有。
        "limit": limit,
        "total": total,
        "has_more": total > len(rows),
        "include": include,
        "note": "默认只含未撤回且未归档的记忆；墓碑用 ?include=retracted|archived|all 取回",
    }


@router.post("/memory")
async def create_memory(
    body: MemoryCreate, auth: CurrentAuth, session: DbSession, config: ConfigDep
) -> dict:
    if not body.source_event_ids:
        raise LingError(
            ErrorCode.invalid_request, "记忆必须有 source_event_ids（经历有来源）", status_code=422
        )
    # Origin/Visibility 是契约枚举。此前直接 Origin(body.origin)，
    # 非法字符串的 ValueError 不在 LingError 体系里，真实服务返回 500。
    # 参数问题必须是 422 + 统一错误形状，不能把用户输入变成"服务器错误"。
    try:
        origin = Origin(body.origin)
        visibility = Visibility(body.visibility)
    except ValueError as exc:
        raise LingError(
            ErrorCode.invalid_request,
            "origin/visibility 不在契约枚举内",
            status_code=422,
            detail=str(exc),
        ) from exc
    proposal = MemoryProposal(
        proposal_id="mp_manual",
        kind=body.kind,  # type: ignore[arg-type]
        text=body.text,
        origin=origin,
        source_event_ids=body.source_event_ids,
        visibility=visibility,
        confidence=body.confidence,
    )
    # 手工写入也要按已发布策略盖保留期（写侧统一在 MemoryService.propose 里盖章）；
    # 不传 policy 的话这条记忆会落回列默认 365，与设置里的 max_retention_days 无关。
    memory_id = await MemoryService(session, policy=(await config.effective()).memory).propose(
        proposal, subject_id=auth.user.subject_id
    )
    if memory_id is None:
        raise LingError(
            ErrorCode.invalid_request,
            "该来源不允许写入长期记忆（模型推测不能作为事实）",
            status_code=422,
        )
    return {"memory_id": memory_id}


async def _own_memory_or_404(session: object, memory_id: str, subject_id: str):  # noqa: ANN202
    """确认这条记忆属于当前自然人。

    不校验归属的话，任何已登录用户只要猜到/拿到 memory_id 就能撤回或
    改写别人的记忆。返回 404 而不是 403——403 会泄漏"这个 id 存在"。
    """
    from ling.infra.models import MemoryRow

    row = await session.get(MemoryRow, memory_id)  # type: ignore[attr-defined]
    if row is None or row.subject_id != subject_id:
        raise LingError(ErrorCode.not_found, "记忆不存在", status_code=404)
    return row


@router.post("/memory/{memory_id}/retract")
async def retract_memory(
    memory_id: str,
    body: MemoryRetract,
    auth: CurrentAuth,
    session: DbSession,
) -> dict:
    # reason 的必填/长度由 MemoryRetract 的 pydantic 约束保证（含 body 整体
    # 缺失），错误形状由 create_app 的前缀处理器统一；这里不再重复校验。
    reason = body.reason
    await _own_memory_or_404(session, memory_id, auth.user.subject_id)
    ok = await MemoryService(session).retract(memory_id, reason=reason)
    if not ok:
        raise LingError(ErrorCode.not_found, "记忆不存在", status_code=404)
    # 撤回是"数据不再被使用"的动作，必须可追溯。只记 ID 与原因码，不记内容。
    audit_event(
        "memory_retracted",
        actor=auth.user.id,
        target_type="memory",
        target_id=memory_id,
        reason_code=reason[:64],
    )
    return {"memory_id": memory_id, "retracted": True}


@router.post("/memory/{memory_id}/supersede")
async def supersede_memory(
    memory_id: str,
    body: MemorySupersede,
    auth: CurrentAuth,
    session: DbSession,
    config: ConfigDep,
) -> dict:
    await _own_memory_or_404(session, memory_id, auth.user.subject_id)
    # 纠正出来的新行沿用旧行的保留期，再被当前策略上限压一次（见 service.supersede）。
    new_id_value = await MemoryService(session, policy=(await config.effective()).memory).supersede(
        memory_id, body.text, reason=body.reason
    )
    if new_id_value is None:
        raise LingError(ErrorCode.not_found, "记忆不存在", status_code=404)
    audit_event(
        "memory_superseded",
        actor=auth.user.id,
        target_type="memory",
        target_id=memory_id,
        attributes={"new_memory_id": new_id_value},
    )
    return {"memory_id": new_id_value, "supersedes": memory_id}


@router.post("/memory/forget")
async def forget_all(auth: CurrentAuth, session: DbSession) -> dict:
    """**真删**这个账号的记忆：请求阶段（同步）+ 清理阶段（后台）。

    用户原话三句合起来只有一个意思：这些东西不该继续存在。所以这个端点
    不再走 ``MemoryService.forget_subject()`` 的软清除（只打 retracted /
    archived 标记、原文仍在库里），而是走 ``purge.request_purge()``。

    ## 为什么请求阶段与"投递清理任务"必须在同一个事务里

    墓碑与清理任务分开提交，就会出现一个**两个方向都坏**的中间态：

    - 墓碑写了、事件没写 → 读侧按墓碑封住一切，但永远没有人去物理删除，
      数据留在库里直到下一次请求（而用户可能再也不来）；
    - 事件写了、墓碑没写 → 清理任务找不到墓碑，直接 no-op。

    同一事务提交保证「**墓碑存在 ⇒ 清理任务必然存在**」。这是整条链能收敛
    的唯一前提，不能靠"先写一个再写另一个、失败了重试"。

    ## 为什么清理不在这里同步跑

    数据量可能几十万行（记忆 + 情节 + 向量 + 实体 + 检索日志），卡在请求里
    会让前端转圈几十秒。而且删除必须**幂等可重试**：删到一半崩了要能续，
    这在 HTTP 请求的生命周期里做不到。

    返回值里 ``state`` 恒为 ``pending``：它描述的是**墓碑**此刻的状态，不是
    "已经删完了"。进度看 ``GET /v1/memory/purge/status``。
    """
    from ling.domains.memory.purge import MemoryPurgeError, request_purge
    from ling.infra.models import OutboxEvent
    from ling.infra.security import new_id

    try:
        request = await request_purge(
            session, subject_id=auth.user.subject_id, requested_by=auth.user.id
        )
    except MemoryPurgeError as exc:
        # 主体不存在 = 没有记忆可删。这不是错误，如实告诉调用方"无事可做"，
        # 而不是编一个 200 出来让前端以为删过了。
        raise LingError(ErrorCode.not_found, exc.message, status_code=404) from exc

    session.add(
        OutboxEvent(
            id=new_id("obx"),
            topic="memory.purge",
            # subject_id / forget_generation 一起写上：前者让清理阶段能按主体
            # 兜底脱敏，后者让代次栅栏在调度层有一份可读的快照。
            subject_id=auth.user.subject_id,
            forget_generation=request.generation,
            payload={"tombstone_id": request.tombstone_id},
        )
    )
    # 一次清空整份记忆：只记数量与代次，不记清掉了什么。
    #
    # 字段口径变更（相对软清除时代）：`retracted_count` 换成了
    # `events_cancelled` + `generation` + `tombstone_id`。软清除的"影响行数"
    # 在真删里没有意义——真实删除数在清理阶段才知道，那时另记一条
    # `memory_purged`（见 worker.handle_memory_purge）。
    audit_event(
        "memory_forgotten",
        actor=auth.user.id,
        target_type="subject",
        target_id=auth.user.subject_id,
        attributes={
            "tombstone_id": request.tombstone_id,
            "generation": request.generation,
            "events_cancelled": request.events_cancelled,
            "mode": "purge",
        },
    )
    return {
        "tombstone_id": request.tombstone_id,
        "generation": request.generation,
        "events_cancelled": request.events_cancelled,
        "state": "pending",
    }


@router.get("/memory/purge/status")
async def memory_purge_status(auth: CurrentAuth, session: DbSession) -> dict:
    """最近的记忆清理进度（**不含任何被删内容**）。

    为什么要有这个端点：真删是两段式的，请求返回 `state=pending` 之后，
    用户需要能回答"到底删完了没有"。只说"已提交"等于把不确定性丢给用户。

    返回的每一项都只有计数与状态码：墓碑表本身就不存正文（见
    `MemoryPurgeTombstone` 的注释），这里也不额外拼任何文本。
    """
    from ling.domains.memory.purge import list_tombstones

    rows = await list_tombstones(session, subject_id=auth.user.subject_id, limit=5)
    return {
        "items": [
            {
                "tombstone_id": row.id,
                "state": row.state,
                "requested_at": row.requested_at.isoformat()
                if row.requested_at
                else None,
                "completed_at": row.completed_at.isoformat()
                if row.completed_at
                else None,
                "memories_deleted": int(row.memories_deleted or 0),
                "episodes_deleted": int(row.episodes_deleted or 0),
                "core_deleted": int(row.core_deleted or 0),
                "awareness_deleted": int(row.awareness_deleted or 0),
                "embeddings_deleted": int(row.embeddings_deleted or 0),
                "entities_deleted": int(row.entities_deleted or 0),
                # 检索日志存的是用户原话，单独报——这是"原文真的清掉了吗"的证据。
                "retrieval_logs_deleted": int(row.retrieval_logs_deleted or 0),
                "events_cancelled": int(row.events_cancelled or 0),
                "events_scrubbed": int(row.events_scrubbed or 0),
                "error_code": row.error_code,
            }
            for row in rows
        ],
        "note": (
            "真删是两段式的：请求阶段（同步）已封住读写，清理阶段（后台）物理删除。"
            "state=done 才是真的删完了。"
        ),
    }


# ------------------------------------------------------------------ 关系与情绪


class RelationshipEventBody(BaseModel):
    kind: str
    origin: str = "user_statement"
    source_event_ids: list[str] = Field(min_length=1)
    note: str | None = None
    weight: float = 1.0


@router.get("/relationship")
async def get_relationship(auth: CurrentAuth, session: DbSession, settings: SettingsDep) -> dict:
    service = RelationshipService(session, actor_id=_actor_id(settings))
    state = await service.state(auth.user.subject_id)
    view = service.view(state)
    events = await service.recent_events(auth.user.subject_id, limit=10)
    return {
        "familiarity": view.familiarity,
        "trust": view.trust,
        "boundaries": view.boundaries,
        "last_interaction_at": view.last_interaction_at.isoformat()
        if view.last_interaction_at
        else None,
        "external_hint": service.external_hint(view),
        "recent_events": [
            {
                "kind": e.kind,
                "origin": e.origin,
                "weight": e.weight,
                "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
            }
            for e in events
        ],
    }


@router.post("/relationship/events")
async def record_relationship_event(
    body: RelationshipEventBody, auth: CurrentAuth, session: DbSession, settings: SettingsDep
) -> dict:
    service = RelationshipService(session, actor_id=_actor_id(settings))
    try:
        event = await service.record_event(
            subject_id=auth.user.subject_id,
            kind=body.kind,
            origin=Origin(body.origin),
            source_event_ids=body.source_event_ids,
            note=body.note,
            weight=body.weight,
        )
    except ValueError as exc:
        raise LingError(ErrorCode.invalid_request, str(exc), status_code=422) from exc
    return {"event_id": event.id, "kind": event.kind}


@router.get("/affect")
async def get_affect(auth: CurrentAuth, session: DbSession, settings: SettingsDep) -> dict:
    service = AffectService(session, actor_id=_actor_id(settings))
    current = await service.current()
    snapshot = await service.snapshot()
    return {
        "current": {
            "emotion": current.emotion,
            "intensity": current.intensity,
            "tone_hint": current.tone_hint(),
            "cause": current.cause,
        },
        "all": [{"emotion": v.emotion, "intensity": v.intensity} for v in snapshot],
        "note": "情绪只影响措辞，不放宽权限",
    }


# ------------------------------------------------------------------ 世界与活动


@router.get("/world/places")
async def list_places(auth: CurrentAuth, session: DbSession, settings: SettingsDep) -> dict:
    service = WorldService(session, actor_id=_actor_id(settings))
    places = await service.ensure_defaults()
    return {
        "places": [
            {"place_id": p.place_id, "name": p.name, "description": p.description, "items": p.items}
            for p in places
        ]
    }


@router.get("/activity")
async def list_activity(auth: CurrentAuth, session: DbSession, settings: SettingsDep) -> dict:
    service = ActivityService(session, actor_id=_actor_id(settings))
    current = await service.current()
    history = await service.history(limit=10)
    return {
        "current": _activity_dict(current) if current else None,
        "history": [_activity_dict(a) for a in history],
        "note": "模拟活动不是现实人体经历；完成必须有证据",
    }


class ActivityTransition(BaseModel):
    to_state: str
    completion_evidence: str | None = None
    checkpoint: dict | None = None


@router.post("/activity/{activity_id}/transition")
async def transition_activity(
    activity_id: str,
    body: ActivityTransition,
    auth: CurrentAuth,
    session: DbSession,
    settings: SettingsDep,
) -> dict:
    service = ActivityService(session, actor_id=_actor_id(settings))
    try:
        view = await service.transition(
            activity_id,
            body.to_state,
            completion_evidence=body.completion_evidence,
            checkpoint=body.checkpoint,
        )
    except (InvalidTransition, KeyError) as exc:
        raise LingError(ErrorCode.conflict, str(exc), status_code=409) from exc
    return _activity_dict(view)


def _activity_dict(view) -> dict:
    return {
        "activity_id": view.activity_id,
        "kind": view.kind,
        "state": view.state,
        "place_id": view.place_id,
        "checkpoint": view.checkpoint,
        "started_at": view.started_at.isoformat() if view.started_at else None,
        "ended_at": view.ended_at.isoformat() if view.ended_at else None,
        "completion_evidence": view.completion_evidence,
        # 前端 ActivityRecord 必填 origin；漏掉会让生活页渲染出空徽标。
        "origin": view.origin,
    }


# ------------------------------------------------------------------ 目标与承诺


class GoalCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    horizon: str = "short"
    note: str | None = None


class CommitmentCreate(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    due_at: datetime
    timezone: str = "Asia/Shanghai"
    window_id: str | None = None
    source_event_ids: list[str] = Field(default_factory=list)


@router.get("/goals")
async def list_goals(auth: CurrentAuth, session: DbSession, settings: SettingsDep) -> dict:
    service = GoalsService(session, actor_id=_actor_id(settings))
    goals = await service.list_goals()
    return {
        "goals": [
            {
                "goal_id": g.goal_id,
                "title": g.title,
                "horizon": g.horizon,
                "state": g.state,
                "progress": g.progress,
                "note": g.note,
            }
            for g in goals
        ]
    }


@router.post("/goals")
async def create_goal(
    body: GoalCreate, auth: CurrentAuth, session: DbSession, settings: SettingsDep
) -> dict:
    service = GoalsService(session, actor_id=_actor_id(settings))
    goal = await service.create_goal(title=body.title, horizon=body.horizon, note=body.note)
    return {"goal_id": goal.goal_id, "title": goal.title}


@router.get("/commitments")
async def list_commitments(
    auth: CurrentAuth,
    session: DbSession,
    settings: SettingsDep,
    state: str | None = None,
) -> dict:
    """承诺列表。**默认只返回仍待履行的 ``active``**。

    契约变更（2026-10）：此前不加状态过滤，``cancelled`` / ``undeliverable``
    会和待履行的一起返回，界面上"已取消的事"看起来和"还欠着的事"一样。
    记录**没有删除**，只是默认可见集收窄；查历史要显式传 ``state``：

    - 不传 → ``active``（待履行）
    - ``?state=cancelled`` / ``undeliverable`` / ``delivered`` / ``missed``
      → 该状态的记录
    - ``?state=all`` → 全部状态（含历史）
    - 其它值 → 422 ``invalid_request``（不静默返回空列表）

    响应新增 ``state_filter`` 回显实际生效的默认值，方便客户端区分
    "没有待履行"和"我查的是历史"。
    """
    service = GoalsService(session, actor_id=_actor_id(settings))
    try:
        items = await service.list_commitments(auth.user.subject_id, state=state)
    except ValueError as exc:
        raise LingError(ErrorCode.invalid_request, str(exc), status_code=422) from exc
    return {
        "commitments": [
            {
                "commitment_id": c.commitment_id,
                "text": c.text,
                "due_at": c.due_at.isoformat(),
                "timezone": c.timezone,
                "state": c.state,
                "delivery_operation_id": c.delivery_operation_id,
                "delivery_state": c.delivery_state,
            }
            for c in items
        ],
        "state_filter": state or "active",
        "note": "计划不是结果：只有真实回执才算已履行",
    }


@router.post("/commitments")
async def create_commitment(
    body: CommitmentCreate, auth: CurrentAuth, session: DbSession, settings: SettingsDep
) -> dict:
    service = GoalsService(session, actor_id=_actor_id(settings))
    try:
        view = await service.create_commitment(
            subject_id=auth.user.subject_id,
            text=body.text,
            due_at=body.due_at,
            timezone=body.timezone,
            window_id=body.window_id,
            source_event_ids=body.source_event_ids,
        )
    except ValueError as exc:
        raise LingError(ErrorCode.invalid_request, str(exc), status_code=422) from exc
    return {"commitment_id": view.commitment_id, "due_at": view.due_at.isoformat()}


# ------------------------------------------------------------------ 主动性


class CommitmentCancelBody(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


@router.post("/commitments/{commitment_id}/cancel")
async def cancel_commitment(
    commitment_id: str,
    body: CommitmentCancelBody,
    auth: CurrentAuth,
    session: DbSession,
) -> dict:
    """取消承诺（终态，不可再改期）。

    取消是 设计文档 §6.2 里 Commitment 的合法终态之一，
    但原实现没有任何路径能到达它——用户撤回不了自己的承诺。
    """
    try:
        view = await GoalsService(session).cancel(commitment_id, reason=body.reason)
    except KeyError as exc:
        raise LingError(ErrorCode.not_found, str(exc), status_code=404) from exc
    except ValueError as exc:
        raise LingError(ErrorCode.invalid_request, str(exc), status_code=422) from exc
    return {
        "commitment_id": view.commitment_id,
        "state": view.state,
        "cancel_reason": view.cancel_reason,
    }


@router.get("/proactive")
async def list_proactive(
    request: Request,
    auth: CurrentAuth,
    session: DbSession,
    settings: SettingsDep,
    config: ConfigDep,
    view: str = "participant",
) -> dict:
    from ling.api.deps import authorize_message_view

    operator = authorize_message_view(auth, view)
    service = ProactivityService(session, actor_id=_actor_id(settings))
    try:
        effective_dialogue = (await config.effective()).dialogue
    except Exception:  # noqa: BLE001 - 读不到配置就按"未开启"报，不假装已开
        effective_dialogue = None
    pending = await service.pending(auth.user.subject_id)
    # **必须真的探测**渠道能力，不能硬编码 False。
    # 原来这里写死 False，于是 gate 永远说"当前渠道不支持主动投递"——
    # 无论 OneBot 配置得多完整，主动联系都不可能出现。
    # `proactive_connector()` 看的是能力自检结果：配了变量但平台不给权限
    # 同样返回 None（那种情况下如实标记不可达，不能假装能发）。
    from ling.capabilities.channels import proactive_connector_status

    _connector, _why_not = proactive_connector_status(request.app)
    # 免打扰窗必须来自**已发布配置**，不能留给服务里的默认值：
    # 面板上改了 0-0（关闭）却仍然报「处于勿扰时段（23:00-08:00）」，
    # 用户会以为设置没保存。这里把生效值显式传下去。
    # 日限额同理：不传 policy 时 `gate()` 用默认值 2，于是面板上"今日已达
    # 上限（n/2）"会和用户配的值（例如 5）对不上——一个只在**面板**里生效
    # 的数字，比不显示更糟。免打扰仍显式传（下面两个参数优先于 policy）。
    _qs = getattr(effective_dialogue, "quiet_start_minute", None) if effective_dialogue else None
    _qe = getattr(effective_dialogue, "quiet_end_minute", None) if effective_dialogue else None
    # 必须传用户时区：勿扰时段和"今日已发几条"都按当地作息算。
    # 原来这里什么都没传，于是永远按 UTC——东八区用户的
    # "23:00-08:00 勿扰"实际是 07:00-16:00，白天静音、深夜可打扰。
    #
    # 现在走**唯一解析出口**（DB 偏好 > LING_USER_TIMEZONE > Asia/Shanghai）。
    # 直接读 `settings.user_timezone` 时，没配环境变量就传 None，`gate` 会
    # 把整条主动联系停掉——生活页那句"用户时区未设置"就是这么来的。
    from ling.settings.user_preferences import effective_user_timezone

    subject_timezone = await effective_user_timezone(
        session, subject_id=auth.user.subject_id, env=settings.user_timezone
    )
    gate = await service.gate(
        subject_id=auth.user.subject_id,
        quiet_start=time(_qs // 60, _qs % 60) if _qs is not None else None,
        quiet_end=time(_qe // 60, _qe % 60) if _qe is not None else None,
        daily_limit=(
            getattr(effective_dialogue, "proactive_daily_limit", None)
            if effective_dialogue is not None
            else None
        ),
        channel_can_proactive=_connector is not None,
        channel_proactive_reason=_why_not,
        subject_timezone=subject_timezone,
    )
    return {
        "pending": [
            {
                "opportunity_id": o.opportunity_id,
                "kind": o.kind,
                "reason": o.reason,
                # 生成好的开场白；None 表示还没生成（不是"生成了空的"）
                "draft_text": o.draft_text if operator or o.output_modality != "voice" else "",
                "output_modality": o.output_modality,
                "expires_at": o.expires_at.isoformat(),
            }
            for o in pending
        ],
        "gate": gate.allowed,
        "gate_reasons": gate.reasons,
        # **诚实标记**：候选会被收集，但目前没有任何代码把它变成消息。
        # 没有这个字段时，gate 一旦放行（渠道能力探测通过），界面就会
        # 看起来"允许主动联系"，而实际什么都发不出去——那是假的完整感。
        # 投递已经实现了，但**默认关闭**（proactive_delivery_enabled）。
        # 分开报"实现了吗"和"开了吗"：混成一个布尔会让"能发但没开"
        # 和"根本不能发"看起来一样。
        "delivery_implemented": True,
        "draft_implemented": True,
        "delivery_enabled": bool(
            effective_dialogue.proactive_delivery_enabled
            if effective_dialogue is not None
            else False
        ),
        "note": (
            "主动发送已实现，但默认关闭。开启前建议先看几天草稿，"
            "确认语气合适；未开启时这些话不会真的发出去，到时间会过期。"
            if not (
                effective_dialogue is not None and effective_dialogue.proactive_delivery_enabled
            )
            else "主动发送已开启。受勿扰时段、每日限额与渠道能力约束。"
        ),
    }


# ------------------------------------------------------------------ 授权


class ConsentBody(BaseModel):
    scope: str
    granted: bool
    valid_until: datetime | None = None
    note: str | None = None


@router.get("/consents")
async def list_consents(auth: CurrentAuth, session: DbSession) -> dict:
    views = await ConsentService(session).list_all(auth.user.subject_id)
    return {
        "consents": [
            {
                "scope": v.scope,
                "granted": v.granted,
                "revision": v.revision,
                "valid_until": v.valid_until.isoformat() if v.valid_until else None,
                "note": v.note,
            }
            for v in views
        ],
        "known_scopes": sorted(KNOWN_SCOPES),
        "defaults": {
            "camera": False,
            "location": False,
            "public_publish": False,
            "memory.cross_device": False,
        },
    }


@router.put("/consents")
async def set_consent(body: ConsentBody, auth: CurrentAuth, session: DbSession) -> dict:
    service = ConsentService(session)
    try:
        if body.granted:
            view = await service.grant(
                subject_id=auth.user.subject_id,
                scope=body.scope,
                valid_until=body.valid_until,
                note=body.note,
            )
        else:
            view = await service.revoke(
                subject_id=auth.user.subject_id, scope=body.scope, note=body.note
            )
    except ValueError as exc:
        raise LingError(ErrorCode.invalid_request, str(exc), status_code=422) from exc
    # 授权变更直接决定"她能看什么/能做什么"，是审计里最该有的一类
    audit_event(
        "consent_changed",
        actor=auth.user.id,
        target_type="consent",
        target_id=view.scope,
        attributes={"granted": view.granted, "revision": view.revision},
    )
    return {"scope": view.scope, "granted": view.granted, "revision": view.revision}


# ------------------------------------------------------------------ 知识


class KnowledgeCreate(BaseModel):
    topic: str = Field(min_length=1, max_length=200)
    claim: str = Field(min_length=1, max_length=4000)
    source_url: str | None = None
    source_kind: str = "verified_source"
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    ttl_hours: int = 24


@router.get("/knowledge")
async def list_knowledge(auth: CurrentAuth, session: DbSession, limit: int = 50) -> dict:
    items = await KnowledgeService(session).list_all(limit=limit)
    return {
        "items": [
            {
                "item_id": k.item_id,
                "topic": k.topic,
                "claim": k.claim,
                "source_url": k.source_url,
                "source_kind": k.source_kind,
                "observed_at": k.observed_at.isoformat(),
                "valid_until": k.valid_until.isoformat() if k.valid_until else None,
                "expired": k.expired,
                "confidence": k.confidence,
            }
            for k in items
        ]
    }


@router.post("/knowledge")
async def add_knowledge(body: KnowledgeCreate, auth: CurrentAuth, session: DbSession) -> dict:
    try:
        view = await KnowledgeService(session).add(
            topic=body.topic,
            claim=body.claim,
            source_url=body.source_url,
            source_kind=body.source_kind,
            confidence=body.confidence,
            ttl_hours=body.ttl_hours,
        )
    except ValueError as exc:
        raise LingError(ErrorCode.invalid_request, str(exc), status_code=422) from exc
    return {
        "item_id": view.item_id,
        "valid_until": view.valid_until.isoformat() if view.valid_until else None,
    }


# ------------------------------------------------------------------ 会话隔离与跨会话意识


class IsolationBody(BaseModel):
    mode: str = Field(pattern="^(sealed|per_window|continuous)$")


@router.get("/isolation")
async def get_isolation(auth: CurrentAuth, session: DbSession) -> dict:
    """当前隔离模式与它对各项能力的影响。"""
    from ling.domains.awareness.service import AwarenessService, IsolationMode

    policy = await AwarenessService(session).policy(auth.user.subject_id)
    return {
        "mode": policy.mode.value,
        "revision": policy.revision,
        "modes": [m.value for m in IsolationMode],
        "effects": {
            "long_term_memory": policy.allows_long_term_memory,
            "cross_window_awareness": policy.allows_awareness,
            "handoff_capsule": policy.mode is not IsolationMode.sealed,
        },
        "notes": {
            "sealed": "会话之间完全隔离；不读长期记忆、不读交接胶囊、不积累意识",
            "per_window": "默认：窗口读自己消息 + 本人长期记忆；群聊只读群内可见内容",
            "continuous": "像一个人：知道别处发生过的事实与自己的动作，但按受众裁剪后才可能说出",
        },
    }


@router.put("/isolation")
async def set_isolation(body: IsolationBody, auth: CurrentAuth, session: DbSession) -> dict:
    from ling.domains.awareness.service import AwarenessService, IsolationMode

    policy = await AwarenessService(session).set_mode(
        auth.user.subject_id, IsolationMode(body.mode)
    )
    return {
        "mode": policy.mode.value,
        "revision": policy.revision,
        "invalidated": body.mode == "sealed",
        "note": "切到 sealed 时已有跨会话意识立即失效（不物理删除，保留审计）",
    }


@router.get("/awareness")
async def list_awareness(
    auth: CurrentAuth, session: DbSession, window_id: str | None = None
) -> dict:
    """列出跨会话意识条目。

    ``window_id`` 给出时按该窗口的可见性过滤——这正好演示"数据层拦截"：
    群聊窗口查不到 subject_fact，私聊窗口查不到别的群的内容。
    """
    from ling.domains.awareness.service import AwarenessService
    from ling.domains.memory.purge import has_open_purge

    service = AwarenessService(session)
    policy = await service.policy(auth.user.subject_id)
    # 清理进行中不返回任何条目：它同样是派生记忆，请求到物理删除之间还在库里。
    # 判断放在路由层（而不是 awareness 域里）是因为域边界闸门不允许
    # awareness import memory（ADR-0001 §2）。
    if await has_open_purge(session, subject_id=auth.user.subject_id):
        items = []
        scope = "purge_pending"
    elif window_id:
        window = await ConversationService(session).get_window(
            window_id, subject_id=auth.user.subject_id
        )
        items = await service.visible_for_window(
            subject_id=auth.user.subject_id,
            window_visibility=window.visibility,
            window_id=window_id,
        )
        scope = window.visibility
    else:
        items = [
            *await service.visible_for_window(
                subject_id=auth.user.subject_id,
                window_visibility="subject_private",
                window_id="",
            )
        ]
        scope = "subject_private"
    return {
        "mode": policy.mode.value,
        "scope": scope,
        "items": [
            {
                "item_id": i.item_id,
                "kind": i.kind.value,
                "text": i.text,
                "audience": i.audience.value,
                "created_at": i.created_at.isoformat(),
                "expires_at": i.expires_at.isoformat() if i.expires_at else None,
            }
            for i in items
        ],
        "forbidden_cross_window": sorted(
            __import__(
                "ling.domains.awareness.service", fromlist=["FORBIDDEN_CROSS_WINDOW"]
            ).FORBIDDEN_CROSS_WINDOW
        ),
    }


# ------------------------------------------------------------------ 跨端交接


class HandoffCreate(BaseModel):
    source_window_id: str
    recent_topic: str | None = None
    open_questions: list[str] = Field(default_factory=list)
    short_quote: str | None = None
    ttl_minutes: int = 30


@router.get("/handoff")
async def read_handoff(
    auth: CurrentAuth, session: DbSession, settings: SettingsDep, current_window_id: str
) -> dict:
    from ling.kernel.handoff import HandoffService

    service = HandoffService(session, actor_id=_actor_id(settings))
    view = await service.read(subject_id=auth.user.subject_id, current_window_id=current_window_id)
    if view is None:
        return {
            "capsule": None,
            "note": "没有可用的交接胶囊（未授权跨端、已过期，或只有当前窗口）",
        }
    return {
        "capsule": {
            "capsule_id": view.capsule_id,
            "source_window_id": view.source_window_id,
            "recent_topic": view.recent_topic,
            "open_questions": view.open_questions,
            "short_quote": view.short_quote,
            "expires_at": view.expires_at.isoformat(),
            "remaining_seconds": view.remaining_seconds,
        }
    }


@router.post("/handoff")
async def create_handoff(
    body: HandoffCreate, auth: CurrentAuth, session: DbSession, settings: SettingsDep
) -> dict:
    from ling.kernel.handoff import HandoffService

    service = HandoffService(session, actor_id=_actor_id(settings))
    try:
        view = await service.create(
            subject_id=auth.user.subject_id,
            source_window_id=body.source_window_id,
            recent_topic=body.recent_topic,
            open_questions=body.open_questions,
            short_quote=body.short_quote,
            ttl_minutes=body.ttl_minutes,
        )
    except PermissionError as exc:
        raise LingError(ErrorCode.forbidden, str(exc), status_code=403) from exc
    return {"capsule_id": view.capsule_id, "expires_at": view.expires_at.isoformat()}


@router.delete("/handoff/{capsule_id}")
async def revoke_handoff(
    capsule_id: str, auth: CurrentAuth, session: DbSession, settings: SettingsDep
) -> dict:
    from ling.kernel.handoff import HandoffService

    ok = await HandoffService(session, actor_id=_actor_id(settings)).revoke(capsule_id)
    if not ok:
        raise LingError(ErrorCode.not_found, "交接胶囊不存在", status_code=404)
    return {"capsule_id": capsule_id, "revoked": True}


# ------------------------------------------------------------------ 语音


@router.get("/speech")
async def speech_status(auth: CurrentAuth, config: ConfigDep) -> dict:
    """语音现状。**TTS 一栏是按当前已发布配置真的装配出来的。**

    从前这里硬编码 `UnconfiguredSpeechProvider()` 并返回写死的
    `configured: False`——绑了 `speech.tts` 也没有任何代码去读它。
    现在走 `build_tts_provider`，`tts.available` 反映的是「那家适配器到底
    造没造出来」，不再是常量。

    **它仍不等于「她会说话」。** 出站触发链路（`[speak]` → 合成 → 投递）
    还没有生产调用点，`speech.tts` 也仍留在 `RESERVED_PROFILES` 里；
    `tts.outbound_wired` 把这个事实写进响应，别让 `available: true`
    被读成"绑上就能听见"。
    """
    from ling.domains.speech.providers import build_tts_provider
    from ling.ports.speech import SPEECH_PROFILE_REQUIREMENTS

    effective = await config.effective()

    # 密钥**只从库里现读**：`app.state.secret_cache` 是同一个 provider_id 的
    # 一份进程内副本，用 `.get` 兜底会让「缓存里还是旧密钥」盖住「库里已经换成
    # 新密钥」——而这里的代价是拿一把废密钥去碰供应商。查询本身就是一次
    # 主键查找，没有缓存它的理由。
    async def secret_reader(provider_id: str) -> str | None:
        return await config.read_secret(provider_id)

    tts = await build_tts_provider(effective, secret_reader=secret_reader)
    from ling.domains.speech.asr_providers import build_asr_provider

    asr = await build_asr_provider(effective, secret_reader=secret_reader)

    return {
        "configured": tts.ok or asr.ok,
        "provider_id": tts.provider.provider_id,
        "requirements": SPEECH_PROFILE_REQUIREMENTS,
        "status": "语音识别与合成按已发布配置独立装配",
        "asr": {"available": asr.ok, "provider_id": asr.provider_id,
                "model_id": asr.model_id, "kind": asr.kind, "reason": asr.reason,
                "file_input": asr.ok, "realtime_input": False},
        "tts": {
            # 装配结论：真的造出了那家的适配器才为 True。
            "available": tts.ok,
            "implementation": tts.implementation,
            "provider_id": tts.provider_id,
            "model_id": tts.model_id,
            "kind": tts.kind,
            "source": tts.source,
            "reason": tts.reason,
            # 表示 `[speak]` 到合成与渠道投递的生产调用已接通。
            "outbound_wired": True,
        },
        "rules": [
            "ASR partial 只做字幕，不写记忆、不触发工具",
            "只有 final 进入主链",
            "TTS 必须有播放回执才能声称已播放",
            "插话取消旧轮，迟到音频按 turn_id 丢弃",
        ],
    }


# ------------------------------------------------------------------ 核心记忆


class CoreMemoryBody(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=2000)
    priority: int = Field(default=100, ge=1, le=999)
    # 作用域是契约枚举，用 pattern 写进 /openapi.json；非法值由记忆端点上
    # 的 RequestValidationError 前缀处理器转成统一 ErrorResponse。
    scope: str = Field(default="private", pattern="^(private|group|global)$")


def _core_memory_dict(
    view: CoreMemoryView, created_at: datetime | None, updated_at: datetime | None
) -> dict:
    """核心记忆的公开字段（列表与 PUT 共用，避免两处形状漂移）。"""
    return {
        "core_id": view.core_id,
        "label": view.label,
        "text": view.text,
        "priority": view.priority,
        "scope": view.scope,
        "origin_kind": view.origin_kind,
        "created_at": created_at.isoformat() if created_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


@router.get("/core-memory")
async def list_core_memory(auth: CurrentAuth, session: DbSession) -> dict:
    """核心记忆：每轮常驻，不依赖检索命中。

    用于放边界、禁忌、称呼、重要约定这类"忘了就出事"的内容。
    容量有限（每 subject 20 条），因为它进的是每一轮上下文。
    """
    from ling.domains.memory.core import MAX_PER_SUBJECT, CoreMemoryService

    # 响应回显 limit=20，查询就必须真的带上硬 LIMIT：正常写入受 20 条上限
    # 保护，但历史/并发数据可能超限，不然会出现"回显 20 却返回 21 条"。
    items = await CoreMemoryService(session).list_active(
        auth.user.subject_id, limit=MAX_PER_SUBJECT
    )
    # 时间戳只在表里（CoreMemoryRow.created_at/updated_at），CoreMemoryView
    # 不带它们；这里按 id 补查一次，避免改动 core.py 的视图契约。
    timestamps: dict[str, tuple[datetime | None, datetime | None]] = {}
    if items:
        from sqlalchemy import select

        from ling.infra.models import CoreMemoryRow

        stamp_rows = (
            await session.execute(
                select(
                    CoreMemoryRow.id,
                    CoreMemoryRow.created_at,
                    CoreMemoryRow.updated_at,
                ).where(CoreMemoryRow.id.in_([v.core_id for v in items]))
            )
        ).all()
        timestamps = {row.id: (row.created_at, row.updated_at) for row in stamp_rows}

    payload = []
    for v in items:
        created_at, updated_at = timestamps.get(v.core_id, (None, None))
        payload.append(_core_memory_dict(v, created_at, updated_at))
    return {
        "items": payload,
        "limit": MAX_PER_SUBJECT,
        "note": "核心记忆每轮注入，过多会挤占上下文；长尾事实用普通记忆即可",
    }


@router.put("/core-memory")
async def upsert_core_memory(body: CoreMemoryBody, auth: CurrentAuth, session: DbSession) -> dict:
    """写入或更新核心记忆（同 label 覆盖，避免互相矛盾的重复条目）。"""
    from ling.domains.memory.core import CoreMemoryService
    from ling.infra.models import CoreMemoryRow

    # 响应直接带完整记录（含 priority/scope/时间戳）：页面写完不必再 GET
    # 一次才能刷新优先级/作用域/时间戳。放在注释里而不是 docstring，是为了
    # 不让 OpenAPI description 出现与本轮无关的变化。
    try:
        view = await CoreMemoryService(session).upsert(
            subject_id=auth.user.subject_id,
            label=body.label,
            text=body.text,
            priority=body.priority,
            scope=body.scope,
        )
    except ValueError as exc:
        raise LingError(ErrorCode.invalid_request, str(exc), status_code=422) from exc
    # upsert 内部已 flush，时间戳在同一个 identity map 里可直接读；
    # 与 GET 列表走同一个字段构造，避免两处形状漂移。
    row = await session.get(CoreMemoryRow, view.core_id)
    created_at = row.created_at if row is not None else None
    updated_at = row.updated_at if row is not None else None
    return _core_memory_dict(view, created_at, updated_at)


@router.delete("/core-memory/{core_id}")
async def delete_core_memory(core_id: str, auth: CurrentAuth, session: DbSession) -> dict:
    from ling.domains.memory.core import CoreMemoryService

    ok = await CoreMemoryService(session).deactivate(core_id, auth.user.subject_id)
    if not ok:
        raise LingError(ErrorCode.not_found, "核心记忆不存在", status_code=404)
    return {"core_id": core_id, "active": False}


# ------------------------------------------------------------------ 渠道账号绑定


class BindAccountBody(BaseModel):
    connector_instance: str = Field(min_length=1, max_length=64)
    external_account_id: str = Field(min_length=1, max_length=128)
    # 是否已完成控制证明。默认 pending：绑定不等于证明，
    # 未证明的绑定不能用于身份解析（否则"自称"某账号就拿到那个人）。
    proof: str = Field(default="pending", pattern="^(pending|proven|admin)$")
    note: str | None = Field(default=None, max_length=200)


@router.get("/accounts")
async def list_accounts(request: Request, auth: CurrentAuth, session: DbSession) -> dict:
    """列出当前自然人的渠道账号绑定。"""
    from ling.domains.identity.service import IdentityService

    rows = await IdentityService(session).list_accounts(auth.user.subject_id)
    return {
        "accounts": [
            {
                "account_id": row.id,
                "connector_instance": row.connector_instance,
                "external_account_id": row.external_account_id,
                "proof": row.proof,
                "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
            }
            for row in rows
        ]
    }


class MergeSubjectBody(BaseModel):
    """把 source 合并到 target（同一个人）。"""

    source_id: str
    target_id: str


# ------------------------------------------------------------------ 关系（subjects）
#
# 这个列表**天然无界**：`IdentityService.resolve_or_provision` 在群里见到任何
# 一个新账号就建一个 known 级 subject，跑几个群、几周下来就是几千行。原来它
# 一次全吐（2000 行 = 561 KB），前端又给每张卡片渲染一个含全部候选的下拉
# （n² 个 `<option>`），于是「点开关系页就卡死」。
# 现在：**默认只给一页**（50 条），要更多用 `offset` 翻；`total` / `has_more`
# 让调用方知道还剩多少，不用自己猜。
SUBJECTS_LIMIT_DEFAULT = 50
SUBJECTS_LIMIT_MAX = 200


def _escape_like(text: str) -> str:
    """把用户输入转成 `LIKE` 的字面量片段（`\\`、`%`、`_` 都当普通字符）。

    不转义的话，搜 `100%` 会变成"以 100 开头的所有人"，搜 `_` 会匹配任意字符——
    用户看到的结果和他输入的字面意思对不上。
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _account_row(a: object) -> dict:
    return {
        "account_id": a.id,  # type: ignore[attr-defined]
        "connector_instance": a.connector_instance,  # type: ignore[attr-defined]
        "external_account_id": a.external_account_id,  # type: ignore[attr-defined]
        "proof": a.proof,  # type: ignore[attr-defined]
        "revoked": a.revoked_at is not None,  # type: ignore[attr-defined]
    }


async def _accounts_by_subject(session: DbSession, subject_ids: list[str]) -> dict[str, list[dict]]:
    """只取这一页的账号，仍然是一条 SQL（不是逐条再查）。"""
    from sqlalchemy import select

    from ling.infra.models import ActorAccount

    if not subject_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(ActorAccount)
                .where(ActorAccount.subject_id.in_(subject_ids))
                .order_by(ActorAccount.connector_instance, ActorAccount.external_account_id)
            )
        )
        .scalars()
        .all()
    )
    by_subject: dict[str, list[dict]] = {}
    for a in rows:
        by_subject.setdefault(a.subject_id, []).append(_account_row(a))
    return by_subject


def _subject_row(s: object, by_subject: dict[str, list[dict]]) -> dict:
    """一行的对外形状（与 `SubjectRow` 契约同源，见 contracts/v1.py）。"""
    return {
        "subject_id": s.id,  # type: ignore[attr-defined]
        "display_name": s.display_name,  # type: ignore[attr-defined]
        "trust_level": getattr(s, "trust_level", "known"),
        "merged_into": getattr(s, "merged_into", None),
        "accounts": by_subject.get(s.id, []),  # type: ignore[attr-defined]
    }


async def _subjects_page(
    session: DbSession, where: list, limit: int, offset: int
) -> tuple[Sequence[Subject], int]:
    """按 `Subject.created_at, Subject.id` 的**稳定全序**取一页，并给出总数。

    必须带 `id` 兜底排序：`created_at` 在同一批自动建档里会撞（同一毫秒 / 同一批
    INSERT 的默认值），只按时间排的话翻页会重复或漏行。
    """
    from sqlalchemy import func, select

    from ling.infra.models import Subject

    stmt = select(Subject)
    count_stmt = select(func.count()).select_from(Subject)
    for cond in where:
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    rows = (
        (
            await session.execute(
                stmt.order_by(Subject.created_at, Subject.id).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    total = int((await session.execute(count_stmt)).scalar() or 0)
    return rows, total


@router.get("/accounts/subjects")
async def list_subjects(
    auth: CurrentAuth,
    session: DbSession,
    # 范围在端点里判定（与 `list_memory` 同一个口径）：OpenAPI 只写说明、
    # 不写 minimum/maximum，非法值统一走 422 + 统一错误形状。
    limit: Annotated[
        int,
        Query(description="单次最多返回条数，范围 1..200，默认 50；超出范围返回 422。"),
    ] = SUBJECTS_LIMIT_DEFAULT,
    offset: Annotated[
        int,
        Query(description="跳过前多少条（按创建时间正序），默认 0；负数返回 422。"),
    ] = 0,
    q: Annotated[
        str | None,
        Query(
            description=(
                "按昵称、subject_id 或任一绑定账号的外部 id 在服务端做子串筛选；留空不筛选。"
            ),
        ),
    ] = None,
) -> dict:
    """列出 subject 及其账号与合并状态（分页），给"关系"页面用。

    只读。合并是**人的决定**，系统不自动做——这里只把现状摊开。

    分页是必需的而不是优化：这张表会随"群里见过的人"无界增长。默认一页 50 条，
    `limit` 上限 200；`total` 是**筛选后**的总行数（不是这一页的行数），
    `has_more` 说明 `offset + len(subjects) < total`。

    「合并到谁」的候选**不随整页返回**：`q` 在服务端搜（昵称 / subject_id /
    账号 id），页面按需取前 20 条。把全部候选塞进首屏正是这次卡死的形态之一，
    重演一遍只是把 n 从"几行"换成"几万字节"。
    """
    from sqlalchemy import exists, or_

    from ling.infra.models import ActorAccount, Subject

    if not 1 <= limit <= SUBJECTS_LIMIT_MAX:
        raise LingError(
            ErrorCode.invalid_request,
            f"limit 必须在 1..{SUBJECTS_LIMIT_MAX} 之间",
            status_code=422,
        )
    if offset < 0:
        raise LingError(ErrorCode.invalid_request, "offset 不能为负", status_code=422)

    where: list = []
    needle = (q or "").strip()
    if needle:
        pattern = f"%{_escape_like(needle)}%"
        # 账号匹配用 EXISTS 而不是 JOIN：JOIN 会让"一个人挂两个号"变成两行，
        # 分页和 total 就都错了。
        where.append(
            or_(
                Subject.display_name.ilike(pattern, escape="\\"),
                Subject.id.ilike(pattern, escape="\\"),
                exists().where(
                    ActorAccount.subject_id == Subject.id,
                    ActorAccount.external_account_id.ilike(pattern, escape="\\"),
                ),
            )
        )

    page, total = await _subjects_page(session, where, limit, offset)
    by_subject = await _accounts_by_subject(session, [s.id for s in page])

    return {
        "schema_version": SCHEMA_VERSION,
        "subjects": [_subject_row(s, by_subject) for s in page],
        "limit": limit,
        "offset": offset,
        "total": total,
        "has_more": offset + len(page) < total,
        "q": needle or None,
    }


@router.post("/accounts/subjects/merge")
async def merge_subjects(body: MergeSubjectBody, auth: CurrentAuth, session: DbSession) -> dict:
    """把两个 subject 认成同一个人。**只写一个指针，不搬数据。**

    数据不搬动是为了能拆：拆开时只需要清掉指针，账号和记忆立刻回到原主。
    把 B 的记忆"搬"到 A 名下之后，一行写错就再也分不清谁是谁。
    """
    from ling.api.errors import LingError
    from ling.contracts.v1 import ErrorCode
    from ling.domains.identity.service import IdentityService

    try:
        result = await IdentityService(session).merge_subjects(body.source_id, body.target_id)
    except ValueError as exc:
        raise LingError(ErrorCode.invalid_request, str(exc), status_code=422) from exc
    return result


@router.post("/accounts/subjects/{source_id}/unmerge")
async def unmerge_subject(source_id: str, auth: CurrentAuth, session: DbSession) -> dict:
    """把某个 subject 从合并里拆出来。

    除了清指针，还会按 `source_account` 把**合并期间写在别人名下**的记忆
    送回原主——否则拆开之后 A 还会想起 B 说过的话。
    """
    from ling.api.errors import LingError
    from ling.contracts.v1 import ErrorCode
    from ling.domains.identity.service import IdentityService

    try:
        result = await IdentityService(session).unmerge_subject(source_id)
    except ValueError as exc:
        raise LingError(ErrorCode.invalid_request, str(exc), status_code=422) from exc
    return result


@router.post("/accounts/bind")
async def bind_account(body: BindAccountBody, auth: CurrentAuth, session: DbSession) -> dict:
    """绑定渠道账号（QQ 号 / 微信 openid 等）。

    **这一步是 OneBot/QQ 接入的前置条件**：入站事件要先解析成自然人，
    未绑定的账号会被拒（不猜身份）。

    ``proof=pending`` 只能建关系、不能用于身份解析；
    确认账号确实属于本人后再改成 ``proven``（控制证明有人工确认、
    验证码、既有渠道互证等做法，本服务只管记录结果）。
    """
    from ling.api.errors import LingError
    from ling.contracts.v1 import ErrorCode
    from ling.domains.identity.service import IdentityService

    try:
        row = await IdentityService(session).bind_account(
            subject_id=auth.user.subject_id,
            connector_instance=body.connector_instance,
            external_account_id=body.external_account_id,
            proof=body.proof,
        )
    except LingError:
        raise
    except ValueError as exc:
        raise LingError(ErrorCode.invalid_request, str(exc), status_code=422) from exc
    return {
        "account_id": row.id,
        "connector_instance": row.connector_instance,
        "external_account_id": row.external_account_id,
        "proof": row.proof,
    }


@router.post("/accounts/{account_id}/prove")
async def prove_account(account_id: str, auth: CurrentAuth, session: DbSession) -> dict:
    """把绑定标记为已证明（人工确认账号归属后调用）。"""
    from ling.api.errors import LingError
    from ling.contracts.v1 import ErrorCode
    from ling.domains.identity.service import IdentityService

    service = IdentityService(session)
    accounts = await service.list_accounts(auth.user.subject_id)
    match = next((a for a in accounts if a.id == account_id), None)
    if match is None:
        raise LingError(ErrorCode.not_found, "账号绑定不存在", status_code=404)
    row = await service.bind_account(
        subject_id=auth.user.subject_id,
        connector_instance=match.connector_instance,
        external_account_id=match.external_account_id,
        proof="proven",
    )
    return {"account_id": row.id, "proof": row.proof}


@router.delete("/accounts/{account_id}")
async def unbind_account(account_id: str, auth: CurrentAuth, session: DbSession) -> dict:
    """解绑。保留 revoked_at 供审计，解绑后不再解析成该自然人。"""
    from ling.api.errors import LingError
    from ling.contracts.v1 import ErrorCode
    from ling.domains.identity.service import IdentityService

    service = IdentityService(session)
    accounts = await service.list_accounts(auth.user.subject_id)
    if not any(a.id == account_id for a in accounts):
        raise LingError(ErrorCode.not_found, "账号绑定不存在", status_code=404)
    await service.revoke_account(account_id)
    return {"account_id": account_id, "revoked": True}


# ------------------------------------------------------------------ 工具


class ToolCallBody(BaseModel):
    """`POST /v1/tools/{id}/call` 的请求体。

    ## 为什么这里**没有** `confirmed` 字段

    原来有一个 ``confirmed: bool = False``，直接透传给
    ``ToolRegistry.call(confirmed=...)``——也就是**请求体自己给自己发确认**。
    那等于把"不可逆动作需要二次确认"这条硬门做成摆设：任何拿到 token 的调用方
    在 body 里写 ``"confirmed": true`` 就能执行撤回这类不可逆工具。今天打不穿
    纯属巧合（``message.recall`` 的 handler 需要 `session` / `connector` 两个
    非 JSON 对象，wire 上给不出来），而"靠 handler 恰好要不到参数"不是防线。

    改成**结构性拒绝**而不是"读到了但忽略"：忽略意味着字段仍在、仍出现在
    ``/openapi.json`` 里，下一个人接一条"顺手用 body.confirmed"的代码时不会有
    任何提示。``extra="forbid"`` 让带这个字段的请求直接 422，且**连 handler 都
    不进**。

    真正的确认入口只有一个：``POST /v1/sessions/{id}/recall-proposals/{pid}/confirm``
    ——那里由**服务端**在用户点击后传 ``confirmed=True``，事实来源是"一条待确认
    的提议行 + owner 本人"，不是请求体里的一个 bool。
    """

    model_config = ConfigDict(extra="forbid")

    args: dict = Field(default_factory=dict)
    window_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_self_confirmation(cls, data: object) -> object:
        """带 ``confirmed`` 的请求给一句**能指路**的拒绝。

        结构性拒绝已经在 ``extra="forbid"`` 里了（422、连 handler 都不进），
        但 pydantic 的原话是 "Extra inputs are not permitted"——调用方看到的是
        "你多传了一个字段"，而不是"确认不能在这种请求里给"。这两句的差别决定
        下一个人是去改请求体还是去改确认流程。

        拒绝仍然**必须是拒绝**：这里绝不"读到就忽略"，也不会替调用方改成
        ``False`` 然后继续——那样请求会 200，调用方会以为自己的确认生效了。
        """
        if isinstance(data, dict) and "confirmed" in data:
            raise ValueError(
                "这个端点不能自证确认（confirmed 不是请求体字段）："
                "不可逆动作的确认必须走提议端点，"
                "即 POST /v1/sessions/{id}/recall-proposals/{proposal_id}/confirm"
            )
        return data


@router.get("/tools")
async def list_tools(request: Request, auth: CurrentAuth) -> dict:
    """列出已注册工具及其风险等级。

    只读/诊断工具默认可用；不可逆与公开发布默认**不注册**，
    需要显式授权（见 capabilities/tools.py）。
    """
    registry = getattr(request.app.state, "tools", None)
    if registry is None:
        return {"tools": [], "note": "工具注册表未装配"}
    return {
        "tools": [
            {
                "tool_id": spec.tool_id,
                "version": spec.version,
                "description": spec.description,
                "risk": spec.risk.value,
                "requires_confirmation": spec.requires_confirmation,
                "reversible": spec.reversible,
                "timeout_seconds": spec.timeout_seconds,
            }
            for spec in registry.list_specs()
        ]
    }


@router.post("/tools/{tool_id}/call")
async def call_tool(
    tool_id: str,
    body: ToolCallBody,
    request: Request,
    auth: CurrentAuth,
    session: DbSession,
) -> dict:
    """执行工具（**永远以"未确认"身份执行**）。

    工具结果是**不可信数据**（untrusted=True），调用方必须包裹后再进提示。

    ## 只能"提议"，不能"确认"

    这个端点的身份是**模型侧/诊断侧的执行入口**，所以它永远传
    ``confirmed=False``。``requires_confirmation`` 的工具在这里最多走到
    ``needs_confirmation``，绝不真的执行——不可逆动作的确认只能由用户在
    ``/v1/sessions/{id}/recall-proposals/{pid}/confirm`` 点出来。

    请求体里带 ``confirmed`` 会直接 422（``ToolCallBody`` 的
    ``extra="forbid"``），所以"在 wire 上自证确认"这条路口是**关着的**。
    """
    registry = getattr(request.app.state, "tools", None)
    if registry is None:
        raise LingError(ErrorCode.unsupported_capability, "工具注册表未装配", status_code=503)

    spec = next((s for s in registry.list_specs() if s.tool_id == tool_id), None)
    if spec is None:
        raise LingError(
            ErrorCode.unsupported_capability, f"未注册的工具: {tool_id}", status_code=404
        )

    # 工具执行必须过权限硬门（设计文档 §1 第 7 条）：
    # 不可逆动作与公开发布由系统裁决，不由模型或调用方决定。
    # 风险等级映射到动作类别，再按 ConsentService 的实际授权填充上下文。
    from ling.domains.consent.service import ConsentService
    from ling.kernel.policy import PolicyContext, PolicyGate

    grants = await ConsentService(session).granted_scopes(auth.user.subject_id)
    verdict = PolicyGate().evaluate(
        ActionIntent(intent_id=f"tool:{tool_id}", kind=f"tool.{spec.risk.value}"),
        PolicyContext(
            subject_id=auth.user.subject_id,
            window_id=body.window_id or "",
            visibility=Visibility.subject_private,
            is_group=False,
            device_granted="device" in grants,
            publish_granted="public_publish" in grants,
            cross_device_granted="memory.cross_device" in grants,
            camera_granted="camera" in grants,
            location_granted="location" in grants,
        ),
    )
    if not verdict.allowed:
        raise LingError(
            ErrorCode.forbidden,
            "该工具需要额外授权：" + "；".join(verdict.reasons),
            status_code=403,
        )
    # **这个端点永远传 confirmed=False**，不给任何例外。
    #
    # 为什么写死而不是"读一个请求体字段"：见 `ToolCallBody` 的注释。确认这件事
    # 只能由**服务端在用户点击之后**给出，来源是"有一条待确认的提议行 + owner
    # 本人"，而不是调用方在 body 里写的一个 bool。请求体里就算带了
    # ``confirmed``，模型层（``extra="forbid"``）也已经 422 掉了，走不到这里。
    #
    # 需要"用户已确认"的工具必须在 `verdict.requires_confirmation` 这一步就
    # 返回 `needs_confirmation`，并指向 `/v1/sessions/{id}/recall-proposals/{pid}/confirm`。
    if verdict.requires_confirmation:
        # 需要二次确认时**不能**直接执行，把决定权交回用户
        return {
            "tool_id": tool_id,
            "call_id": "",
            "outcome": "needs_confirmation",
            "output": None,
            "detail": (
                "该操作不可逆或会公开发布，需要二次确认；"
                "确认要走提议端点（如 /v1/sessions/{id}/recall-proposals/{pid}/confirm），"
                "不能在这里自证"
            ),
            "untrusted": True,
            "observed_at": datetime.now(UTC).isoformat(),
        }

    result = await registry.call(tool_id, body.args, confirmed=False)
    return {
        "tool_id": result.tool_id,
        "call_id": result.call_id,
        "outcome": result.outcome.value,
        "output": result.output,
        "detail": result.detail,
        "untrusted": result.untrusted,
        "observed_at": result.observed_at.isoformat(),
    }


# ------------------------------------------------------------------ 共处 / 发布 / 语音


class TogetherCreate(BaseModel):
    kind: str = Field(pattern="^(read|watch|listen|game)$")
    title: str = Field(min_length=1, max_length=200)


class PublishDraftBody(BaseModel):
    channel: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)


class SpeechSegmentBody(BaseModel):
    window_id: str
    text: str = Field(min_length=1, max_length=8000)
    kind: str = Field(default="final", pattern="^(partial|final)$")
    turn_id: str | None = None
    model_label: str | None = None


def _together(session: DbSession) -> object:
    """共处服务。

    不再缓存到 `app.state`：服务现在绑在数据库会话上，
    缓存一个实例等于让所有请求共用一个 session——那是并发的坑。
    """
    from ling.capabilities.together import TogetherService

    return TogetherService(session)


def _publishing(request: Request) -> object:
    service = getattr(request.app.state, "publishing", None)
    if service is None:
        from ling.capabilities.publishing import PublishingService

        service = PublishingService(
            capability_enabled=request.app.state.settings.allow_public_publish
        )
        request.app.state.publishing = service
    return service


@router.get("/together")
async def list_together(request: Request, auth: CurrentAuth, session: DbSession) -> dict:
    service = _together(session)
    sessions = await service.list_sessions()  # type: ignore[attr-defined]
    return {
        "sessions": [
            {
                "session_id": s.session_id,
                "kind": s.kind,
                "title": s.title,
                "state": s.state,
                "participants": [p.display_name for p in s.participants],
                "progress": s.progress,
            }
            for s in sessions
        ],
        "note": (
            "共处会话已持久化（重启不丢）。进度只来自客户端真实回报，服务端不猜「看到哪了」。"
        ),
    }


@router.post("/together")
async def create_together(
    body: TogetherCreate, request: Request, auth: CurrentAuth, session: DbSession
) -> dict:
    service = _together(session)
    try:
        created = await service.create(  # type: ignore[attr-defined]
            kind=body.kind,
            title=body.title,
            host_subject_id=auth.user.subject_id,
            host_name=auth.user.display_name,
        )
    except ValueError as exc:
        raise LingError(ErrorCode.invalid_request, str(exc), status_code=422) from exc
    return {"session_id": created.session_id, "kind": created.kind}


@router.get("/publishing")
async def publishing_status(request: Request, auth: CurrentAuth, session: DbSession) -> dict:
    """发布的授权状态。两层分开报，避免把"宿主有能力"误读成"我已授权"。"""
    from ling.capabilities.publishing import PUBLISH_SCOPE
    from ling.domains.consent.service import ConsentService

    service = _publishing(request)
    capability_enabled = bool(getattr(service, "capability_enabled", False))
    consent = await ConsentService(session).get(auth.user.subject_id, PUBLISH_SCOPE)
    can_publish = capability_enabled and consent.granted
    return {
        "capability_enabled": capability_enabled,
        "consent_granted": consent.granted,
        "consent_valid_until": (
            consent.valid_until.isoformat() if consent.valid_until is not None else None
        ),
        "can_publish": can_publish,
        "note": (
            "两层都要满足：宿主启用发布能力（启动配置 LING_ALLOW_PUBLIC_PUBLISH）"
            f"且你已获授权（授权记录 scope={PUBLISH_SCOPE}）。"
            "授权后每条内容仍需二次确认 + 真实平台回执。"
        ),
    }


@router.post("/publishing/drafts")
async def create_publish_draft(
    body: PublishDraftBody, request: Request, auth: CurrentAuth, session: DbSession
) -> dict:
    from ling.capabilities.publishing import PublishError
    from ling.domains.consent.service import ConsentService

    service = _publishing(request)
    try:
        draft = await service.create_draft(  # type: ignore[attr-defined]
            subject_id=auth.user.subject_id,
            channel=body.channel,
            title=body.title,
            body=body.body,
            consent=ConsentService(session),
        )
    except PublishError as exc:
        raise LingError(ErrorCode.forbidden, str(exc), status_code=403) from exc
    return {"draft_id": draft.draft_id, "state": draft.state.value}


@router.post("/speech/segments")
async def record_speech_segment(
    body: SpeechSegmentBody, auth: CurrentAuth, session: DbSession
) -> dict:
    """记录语音分段。

    **partial 只做字幕，不写记忆、不触发工具**；只有 final 进主链。
    这个约束在 SpeechService 里是硬编码的，不靠调用方自觉。
    """
    from ling.domains.speech.service import SpeechService

    service = SpeechService(session)
    try:
        if body.kind == "final":
            view = await service.record_final(
                window_id=body.window_id,
                text=body.text,
                turn_id=body.turn_id,
                model_label=body.model_label,
            )
        else:
            view = await service.record_partial(
                window_id=body.window_id,
                text=body.text,
                turn_id=body.turn_id,
                model_label=body.model_label,
            )
    except ValueError as exc:
        # 领域层的校验错误必须翻译成契约错误，否则会以 500 漏出去
        raise LingError(ErrorCode.invalid_request, str(exc), status_code=422) from exc
    return {
        "segment_id": view.segment_id,
        "kind": view.kind,
        "text": view.text,
        "note": "partial 不进入认知管线；只有 final 会被当作正式输入",
    }


# ------------------------------------------------------------------ 渠道


@router.get("/attention/stats")
async def attention_stats(auth: CurrentAuth, session: DbSession) -> dict:
    """群聊轮询的影子数据：她的判断分布 + 省了多少次决策调用。

    轮询目前是**影子模式**（只决策不发送），这一页用来判断
    "她的判断像不像人"、要不要开真实发送。
    """
    from sqlalchemy import func, select

    from ling.infra.models import AttentionPollLogRow

    totals = (
        await session.execute(
            select(
                func.count(AttentionPollLogRow.id),
                func.coalesce(func.sum(AttentionPollLogRow.llm_calls), 0),
                func.coalesce(func.sum(AttentionPollLogRow.baseline_calls), 0),
                func.coalesce(func.sum(AttentionPollLogRow.unseen_count), 0),
            )
        )
    ).one()
    polls, calls, baseline, unseen = (int(v or 0) for v in totals)

    by_decision = (
        await session.execute(
            select(AttentionPollLogRow.decision, func.count(AttentionPollLogRow.id)).group_by(
                AttentionPollLogRow.decision
            )
        )
    ).all()

    recent = (
        (
            await session.execute(
                select(AttentionPollLogRow).order_by(AttentionPollLogRow.polled_at.desc()).limit(20)
            )
        )
        .scalars()
        .all()
    )
    return {
        "polls": polls,
        "unseen_total": unseen,
        "llm_calls": calls,
        # 逐条处理的影子基线；两者相减就是省下的决策次数
        "baseline_calls": baseline,
        "saved_calls": max(0, baseline - calls),
        "by_decision": {str(k): int(v) for k, v in by_decision},
        "recent": [
            {
                "window_id": r.window_id,
                "polled_at": r.polled_at.isoformat() if r.polled_at else None,
                "unseen_count": r.unseen_count,
                "decision": r.decision,
                "reason": r.reason,
            }
            for r in recent
        ],
        "note": (
            "影子模式：只决策不发送。llm_calls 只统计「要不要说」的判断；"
            "「说什么」的生成是另一次调用。"
        ),
    }


@router.get("/channels")
async def list_channels(auth: CurrentAuth, settings: SettingsDep) -> dict:
    from connectors.wechat import describe_account_types

    from ling.capabilities.channels import onebot_config

    # OneBot 的能力位是**平台探测结果**，不是用户偏好：
    # 配了 LING_ONEBOT_PROACTIVE=1 但平台不给权限，照样发不出去。
    # 因此这里只报只读状态（editable=False），设置界面不得把它做成开关。
    onebot = onebot_config()
    return {
        "web": {"enabled": True, "status": "Web 已实测", "capabilities": ["text", "streaming"]},
        "onebot": {
            "enabled": settings.qq_enabled,
            "status": "适配器已配置" if onebot.api_base else "未配置 api_base",
            "capabilities_readonly": {
                "supports_group": onebot.supports_group,
                "supports_proactive": onebot.supports_proactive,
            },
            "note": (
                "能力位是平台权限探测结果，只读展示、不可在设置里开关。"
                "要改请调整部署环境变量后重启；声明了但平台不给权限，仍按不可达处理。"
            ),
        },
        "qq": {
            "enabled": settings.qq_enabled,
            "status": "已实现未测试（W1 只开发）",
            "blocked_by": "需要 QQ 官方 appid/secret 与已开通的私聊/群聊权限",
            "note": "QQ 官方自 2025-04-21 起不再提供主动推送，主动能力必须以账号探测为准",
        },
        # QQ 官方机器人是**另一条**接入路径（openid 身份 + Webhook），
        # 与上面的 `qq`（旧的 W1 骨架）不是同一个东西，也不共用入口。
        "qqbot": {
            "enabled": settings.qqbot_enabled,
            "status": (
                "Webhook 模式，未与真实平台联调（本仓库无凭据）"
                if settings.qqbot_enabled
                else "未启用（LING_QQBOT_ENABLED=0）"
            ),
            "capabilities_readonly": _qqbot_capabilities(settings),
            "blocked_by": (
                "需要开放平台的 app_id/client_secret 与 BotSecret；"
                "被动回复有次数与时效限制，主动推送需单独申请权限"
            ),
            "note": (
                "openid 不是 QQ 号：同一个人在不同机器人下 openid 不同，也拿不到 QQ 号。"
                "能力位是权限探测结果，只读展示。"
            ),
        },
        "wechat": {
            "enabled": settings.wechat_enabled,
            "status": "已实现未测试（W2 只开发）",
            "account_types": describe_account_types(),
            "blocked_by": "需要确定账号类型并取得对应资质",
        },
        "embodiment": {
            "enabled": True,
            "status": "已实现未测试（协议层）",
            "blocked_by": "需要手机/VR/桌面设备联调",
        },
    }


def _qqbot_capabilities(settings: Settings) -> dict:
    """QQ 官方机器人的能力位（只读）。

    从连接器自检读，而不是把开关原样抄一遍：`supports_proactive` 反映的是
    平台权限，配置里写 1 也未必真能发——界面必须看到同一份判定结果。
    凭据不全时返回"未配置"，而不是编一组默认值。
    """
    from ling.capabilities.channels import qqbot_config

    try:
        config = qqbot_config(settings)
    except Exception as exc:  # noqa: BLE001 - 读配置失败按未配置处理
        return {"configured": False, "detail": f"配置读取失败（{type(exc).__name__}）"}
    return {
        "configured": config.configured,
        "sandbox": config.sandbox,
        "supports_private": True,
        "supports_group": config.supports_group,
        "supports_proactive": config.supports_proactive,
        "supports_media": config.supports_media,
        "max_text_length": config.max_text_length,
        "webhook_signing_configured": bool(config.bot_secret or config.webhook_public_key_hex),
    }
