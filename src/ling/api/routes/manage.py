"""管理/角色 API：记忆、关系、情绪、生活、目标、授权、渠道。

全部走鉴权后的命令/查询 API；管理台不能直接改表（设计文档 §3）。
这些端点属于 W2，**已实现未测试**：W0 回归不覆盖它们。
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ling.api.deps import ConfigDep, CurrentAuth, DbSession, SettingsDep
from ling.api.errors import LingError
from ling.audit import audit_event
from ling.config import Settings
from ling.contracts.v1 import (
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

router = APIRouter(tags=["manage"])


def _actor_id(settings: SettingsDep) -> str:
    return settings.actor_id


# ------------------------------------------------------------------ 记忆


class MemoryCreate(BaseModel):
    kind: str = Field(pattern="^(episode|assertion|procedure)$")
    text: str = Field(min_length=1, max_length=4000)
    origin: str = "user_statement"
    visibility: str = "subject_private"
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    source_event_ids: list[str] = Field(default_factory=list)


class MemoryRetract(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class MemorySupersede(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    reason: str = Field(default="用户纠正", max_length=500)


@router.get("/memory")
async def list_memory(auth: CurrentAuth, session: DbSession, limit: int = 50) -> dict:
    rows = await MemoryService(session).list_memories(auth.user.subject_id, limit=limit)
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
            }
            for r in rows
        ],
        "note": "已实现未测试：W2 记忆域，未运行专项测试",
    }


@router.post("/memory")
async def create_memory(body: MemoryCreate, auth: CurrentAuth, session: DbSession) -> dict:
    if not body.source_event_ids:
        raise LingError(
            ErrorCode.invalid_request, "记忆必须有 source_event_ids（经历有来源）", status_code=422
        )
    proposal = MemoryProposal(
        proposal_id="mp_manual",
        kind=body.kind,  # type: ignore[arg-type]
        text=body.text,
        origin=Origin(body.origin),
        source_event_ids=body.source_event_ids,
        visibility=Visibility(body.visibility),
        confidence=body.confidence,
    )
    memory_id = await MemoryService(session).propose(proposal, subject_id=auth.user.subject_id)
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
    memory_id: str, body: MemoryRetract, auth: CurrentAuth, session: DbSession
) -> dict:
    await _own_memory_or_404(session, memory_id, auth.user.subject_id)
    ok = await MemoryService(session).retract(memory_id, reason=body.reason)
    if not ok:
        raise LingError(ErrorCode.not_found, "记忆不存在", status_code=404)
    # 撤回是"数据不再被使用"的动作，必须可追溯。只记 ID 与原因码，不记内容。
    audit_event(
        "memory_retracted",
        actor=auth.user.id,
        target_type="memory",
        target_id=memory_id,
        reason_code=(body.reason or "")[:64],
    )
    return {"memory_id": memory_id, "retracted": True}


@router.post("/memory/{memory_id}/supersede")
async def supersede_memory(
    memory_id: str, body: MemorySupersede, auth: CurrentAuth, session: DbSession
) -> dict:
    await _own_memory_or_404(session, memory_id, auth.user.subject_id)
    new_id_value = await MemoryService(session).supersede(
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
    count = await MemoryService(session).forget_subject(auth.user.subject_id)
    # 一次清空整份记忆：只记数量，不记清掉了什么
    audit_event(
        "memory_forgotten",
        actor=auth.user.id,
        target_type="subject",
        target_id=auth.user.subject_id,
        attributes={"retracted_count": count},
    )
    return {"retracted": count}


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
        "all": [
            {"emotion": v.emotion, "intensity": v.intensity} for v in snapshot
        ],
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
async def list_commitments(auth: CurrentAuth, session: DbSession, settings: SettingsDep) -> dict:
    service = GoalsService(session, actor_id=_actor_id(settings))
    items = await service.list_commitments(auth.user.subject_id)
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
    auth: CurrentAuth, session: DbSession, settings: SettingsDep, config: ConfigDep
) -> dict:
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

    _connector, _why_not = proactive_connector_status()
    gate = await service.gate(
        subject_id=auth.user.subject_id,
        channel_can_proactive=_connector is not None,
        channel_proactive_reason=_why_not,
        # 必须传用户时区：勿扰时段和"今日已发几条"都按当地作息算。
        # 原来这里什么都没传，于是永远按 UTC——东八区用户的
        # "23:00-08:00 勿扰"实际是 07:00-16:00，白天静音、深夜可打扰。
        subject_timezone=settings.user_timezone,
    )
    return {
        "pending": [
            {
                "opportunity_id": o.opportunity_id,
                "kind": o.kind,
                "reason": o.reason,
                # 生成好的开场白；None 表示还没生成（不是"生成了空的"）
                "draft_text": o.draft_text,
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
            "主动发送已实现但**默认关闭**。开启前建议先看几天草稿，"
            "确认语气合适；未开启时这些话不会真的发出去，到时间会过期。"
            if not (
                effective_dialogue is not None
                and effective_dialogue.proactive_delivery_enabled
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
    return {"item_id": view.item_id, "valid_until": view.valid_until.isoformat() if view.valid_until else None}


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
async def set_isolation(
    body: IsolationBody, auth: CurrentAuth, session: DbSession
) -> dict:
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

    service = AwarenessService(session)
    policy = await service.policy(auth.user.subject_id)
    if window_id:
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
async def speech_status(auth: CurrentAuth) -> dict:
    from ling.ports.speech import (
        SPEECH_PROFILE_REQUIREMENTS,
        UnconfiguredSpeechProvider,
    )

    provider = UnconfiguredSpeechProvider()
    return {
        "configured": False,
        "provider_id": provider.provider_id,
        "requirements": SPEECH_PROFILE_REQUIREMENTS,
        "status": "已实现未测试：语音流水线与 provider 端口已就绪，未接凭据与设备",
        "blocked_by": "需要语音供应商凭据（ASR/TTS）与手机设备",
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
    scope: str = Field(default="private", pattern="^(private|group|global)$")


@router.get("/core-memory")
async def list_core_memory(auth: CurrentAuth, session: DbSession) -> dict:
    """核心记忆：每轮常驻，不依赖检索命中。

    用于放边界、禁忌、称呼、重要约定这类"忘了就出事"的内容。
    容量有限（每 subject 20 条），因为它进的是每一轮上下文。
    """
    from ling.domains.memory.core import MAX_PER_SUBJECT, CoreMemoryService

    items = await CoreMemoryService(session).list_active(auth.user.subject_id)
    return {
        "items": [
            {
                "core_id": v.core_id,
                "label": v.label,
                "text": v.text,
                "priority": v.priority,
                "scope": v.scope,
                "origin_kind": v.origin_kind,
            }
            for v in items
        ],
        "limit": MAX_PER_SUBJECT,
        "note": "核心记忆每轮注入，过多会挤占上下文；长尾事实用普通记忆即可",
    }


@router.put("/core-memory")
async def upsert_core_memory(
    body: CoreMemoryBody, auth: CurrentAuth, session: DbSession
) -> dict:
    """写入或更新核心记忆（同 label 覆盖，避免互相矛盾的重复条目）。"""
    from ling.domains.memory.core import CoreMemoryService

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
    return {"core_id": view.core_id, "label": view.label, "text": view.text}


@router.delete("/core-memory/{core_id}")
async def delete_core_memory(
    core_id: str, auth: CurrentAuth, session: DbSession
) -> dict:
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


@router.get("/accounts/subjects")
async def list_subjects(auth: CurrentAuth, session: DbSession) -> dict:
    """列出所有 subject 及其账号与合并状态，给"合并/拆分"页面用。

    只读。合并是**人的决定**，系统不自动做——这里只把现状摊开。
    """
    from sqlalchemy import select

    from ling.infra.models import ActorAccount, Subject

    subjects = (await session.execute(select(Subject))).scalars().all()
    accounts = (await session.execute(select(ActorAccount))).scalars().all()
    by_subject: dict[str, list[dict]] = {}
    for a in accounts:
        by_subject.setdefault(a.subject_id, []).append(
            {
                "account_id": a.id,
                "connector_instance": a.connector_instance,
                "external_account_id": a.external_account_id,
                "proof": a.proof,
                "revoked": a.revoked_at is not None,
            }
        )
    return {
        "subjects": [
            {
                "subject_id": s.id,
                "display_name": s.display_name,
                "trust_level": getattr(s, "trust_level", "known"),
                "merged_into": getattr(s, "merged_into", None),
                "accounts": by_subject.get(s.id, []),
            }
            for s in subjects
        ]
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
        result = await IdentityService(session).merge_subjects(
            body.source_id, body.target_id
        )
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
async def bind_account(
    body: BindAccountBody, auth: CurrentAuth, session: DbSession
) -> dict:
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
async def prove_account(
    account_id: str, auth: CurrentAuth, session: DbSession
) -> dict:
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
async def unbind_account(
    account_id: str, auth: CurrentAuth, session: DbSession
) -> dict:
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
    args: dict = Field(default_factory=dict)
    confirmed: bool = False
    window_id: str | None = None


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
    """执行工具。

    工具结果是**不可信数据**（untrusted=True），调用方必须包裹后再进提示。
    """
    registry = getattr(request.app.state, "tools", None)
    if registry is None:
        raise LingError(ErrorCode.unsupported_capability, "工具注册表未装配", status_code=503)

    spec = next((s for s in registry.list_specs() if s.tool_id == tool_id), None)
    if spec is None:
        raise LingError(ErrorCode.unsupported_capability, f"未注册的工具: {tool_id}", status_code=404)

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
    if verdict.requires_confirmation and not body.confirmed:
        # 需要二次确认时**不能**直接执行，把决定权交回用户
        return {
            "tool_id": tool_id,
            "call_id": "",
            "outcome": "needs_confirmation",
            "output": None,
            "detail": "该操作不可逆或会公开发布，需要二次确认（confirmed=true）",
            "untrusted": True,
            "observed_at": datetime.now(UTC).isoformat(),
        }

    result = await registry.call(tool_id, body.args, confirmed=body.confirmed)
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
            "共处会话已持久化（重启不丢）。进度只来自客户端真实回报，"
            "服务端不猜「看到哪了」。"
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
async def publishing_status(
    request: Request, auth: CurrentAuth, session: DbSession
) -> dict:
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
            select(AttentionPollLogRow.decision, func.count(AttentionPollLogRow.id))
            .group_by(AttentionPollLogRow.decision)
        )
    ).all()

    recent = (
        (
            await session.execute(
                select(AttentionPollLogRow)
                .order_by(AttentionPollLogRow.polled_at.desc())
                .limit(20)
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
        "webhook_signing_configured": bool(
            config.bot_secret or config.webhook_public_key_hex
        ),
    }
