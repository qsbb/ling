"""Web 私聊的会话级对话策略。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import replace
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.api.deps import ConfigDep, CurrentAuth, DbSession
from ling.api.errors import LingError
from ling.api.routes.version import _origin_problem, _require_json_content_type
from ling.audit import audit_event
from ling.contracts.v1 import (
    ErrorCode,
    InstanceDialoguePolicyPreview,
    InstanceDialoguePolicyProposed,
    InstanceDialoguePolicyView,
    WindowDialoguePolicyFields,
    WindowDialoguePolicyPreview,
    WindowDialoguePolicyProposed,
    WindowDialoguePolicyView,
    WindowOriginBindingCandidate,
    WindowOriginBindingOptions,
    WindowOriginBindingPreview,
)
from ling.infra.conversation_policy_audit import ConversationPolicyAudit
from ling.infra.models import (
    DEFAULT_INSTANCE_ID,
    ConnectorInstance,
    DialogueConfigGuard,
    InstanceDialogueOverride,
    Persona,
    Window,
    WindowDialogueOverride,
    utcnow,
)
from ling.infra.security import load_signing_root_key
from ling.ports.config import EffectiveConfig
from ling.settings.conversation_inheritance import (
    resolve_conversation_settings,
    validate_conversation_overrides,
)
from ling.settings.window_policy import (
    WindowPolicySnapshot,
    lock_dialogue_config,
    locked_window_policy,
    policy_window,
    require_active_window,
    save_window_policy,
)

router = APIRouter(tags=["sessions"])
InstanceScope = Literal["onebot", "qqbot"]
_FIELD: Literal["private_may_stay_silent"] = "private_may_stay_silent"
_FIELDS = (
    "persona_id",
    "private_may_stay_silent",
    "group_mention_requires_reply",
    "group_attention_deferred",
    "group_turn_budget",
    "rest_may_skip",
)
_PREVIEW_SECONDS = 300
_POLICY_SIGNING_CONTEXT = b"ling-conversation-policy-preview-v2"


class PolicyChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    set: dict[StrictStr, object] = Field(default_factory=dict)
    clear: list[StrictStr] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_operations(self) -> PolicyChange:
        if not self.set and not self.clear:
            raise ValueError("至少选择一个设置操作")
        if len(set(self.clear)) != len(self.clear):
            raise ValueError("clear 中不能重复同一个字段")
        if set(self.set).intersection(self.clear):
            raise ValueError("同一字段不能同时 set 和 clear")
        unknown = (set(self.set) | set(self.clear)) - set(_FIELDS)
        if unknown:
            raise ValueError(f"不支持的对话设置字段: {', '.join(sorted(unknown))}")
        validate_conversation_overrides(self.set)
        return self

    @property
    def normalized(self) -> dict[str, object]:
        return {"set": dict(sorted(self.set.items())), "clear": sorted(self.clear)}

    def apply(self, current: dict[str, object]) -> dict[str, object]:
        updated = {**current, **self.set}
        for field in self.clear:
            updated.pop(field, None)
        return updated


class PolicyCommit(PolicyChange):
    preview_token: str = Field(min_length=1, max_length=2048)


class OriginBindingChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: InstanceScope | None
    instance_id: StrictStr | None = Field(max_length=16)

    @model_validator(mode="after")
    def validate_target(self) -> OriginBindingChange:
        if (self.scope is None) != (self.instance_id is None):
            raise ValueError("平台和实例必须同时选择或同时清除")
        if self.instance_id is not None and not self.instance_id.strip():
            raise ValueError("请选择平台实例")
        return self

    @property
    def target(self) -> dict[str, str | None]:
        return {"scope": self.scope, "instance_id": self.instance_id}


class OriginBindingCommit(OriginBindingChange):
    preview_token: str = Field(min_length=1, max_length=2048)


def _write_guards(request: Request, auth: CurrentAuth) -> None:
    if auth.user.role != "owner":
        raise LingError(ErrorCode.forbidden, "只有所有者可以修改会话设置", status_code=403)
    _require_json_content_type(request)
    if _origin_problem(request, request.app.state.settings) is not None:
        raise LingError(
            ErrorCode.forbidden, "这个请求的来源不可信", status_code=403, detail="bad_origin"
        )


def _record_policy_audit(
    session: AsyncSession,
    *,
    action: str,
    actor_id: str,
    target_type: str,
    target_id: str,
    set_fields: list[str],
    clear_fields: list[str],
    revision: int,
) -> None:
    session.add(ConversationPolicyAudit(
        id=f"paudit_{uuid.uuid4().hex}",
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        set_fields=sorted(set_fields),
        clear_fields=sorted(clear_fields),
        revision=revision,
        created_at=utcnow(),
    ))


def _require_instance_scope(scope: str) -> InstanceScope:
    if scope == "onebot":
        return "onebot"
    if scope == "qqbot":
        return "qqbot"
    raise LingError(ErrorCode.not_found, "没有这个平台实例", status_code=404)


def _instance_field_views(
    config: EffectiveConfig, overrides: dict[str, object]
) -> WindowDialoguePolicyFields:
    global_values = resolve_conversation_settings(config)
    resolved = resolve_conversation_settings(config, instance_overrides=overrides)
    fields: dict[str, dict[str, object]] = {}
    for field in _FIELDS:
        global_field = getattr(global_values, field)
        resolved_field = getattr(resolved, field)
        fields[field] = {
            "global_value": global_field.value,
            "instance_override": overrides.get(field),
            "has_instance_override": field in overrides,
            "session_override": None,
            "has_session_override": False,
            "effective": resolved_field.value,
            "source": resolved_field.source,
        }
    return WindowDialoguePolicyFields.model_validate(fields)


async def _require_live_connector_instance(
    session: AsyncSession,
    *,
    scope: InstanceScope,
    instance_id: str,
    create_default: bool,
) -> ConnectorInstance | None:
    _require_instance_scope(scope)
    if instance_id == DEFAULT_INSTANCE_ID and create_default:
        from ling.settings.connector_instances import ConnectorInstanceService

        row: ConnectorInstance | None = await ConnectorInstanceService(session).ensure_default(
            scope=scope
        )
    else:
        row = await session.get(ConnectorInstance, (scope, instance_id))
    if row is None and instance_id == DEFAULT_INSTANCE_ID and not create_default:
        return None
    if row is None or row.deleted_at is not None:
        raise LingError(ErrorCode.not_found, "平台实例不存在", status_code=404)
    return row


async def _instance_impact(
    session: AsyncSession,
    *,
    scope: InstanceScope,
    instance_id: str,
    actor_id: str,
    subject_id: str,
    config: EffectiveConfig,
    current: dict[str, object],
    proposed: dict[str, object] | None = None,
) -> tuple[int, int, int]:
    stmt = (
        select(Window.archived, WindowDialogueOverride.overrides)
        .outerjoin(WindowDialogueOverride, WindowDialogueOverride.window_id == Window.id)
        .where(
            Window.origin_status == "known",
            Window.origin_scope == scope,
            Window.origin_instance_id == instance_id,
            Window.actor_id == actor_id,
            or_(
                and_(Window.visibility == "subject_private", Window.subject_id == subject_id),
                Window.visibility == "group_scope",
            ),
        )
    )
    rows = (await session.execute(stmt)).all()
    if proposed is None:
        active = 0
        archived = 0
        shadowed = 0
        for is_archived, raw_session in rows:
            session_overrides = dict(raw_session or {})
            resolved = resolve_conversation_settings(
                config,
                instance_overrides=current,
                session_overrides=session_overrides,
            )
            if any(getattr(resolved, field).source == "instance" for field in _FIELDS):
                if is_archived:
                    archived += 1
                else:
                    active += 1
            if any(field in session_overrides and field in current for field in _FIELDS):
                shadowed += 1
        return active, archived, shadowed

    before_instance = resolve_conversation_settings(config, instance_overrides=current)
    after_instance = resolve_conversation_settings(config, instance_overrides=proposed)
    changed_fields = {
        field
        for field in _FIELDS
        if getattr(before_instance, field).value != getattr(after_instance, field).value
    }
    active = 0
    archived = 0
    shadowed = 0
    for is_archived, raw_session in rows:
        session_overrides = dict(raw_session or {})
        before = resolve_conversation_settings(
            config,
            instance_overrides=current,
            session_overrides=session_overrides,
        )
        after = resolve_conversation_settings(
            config,
            instance_overrides=proposed,
            session_overrides=session_overrides,
        )
        if any(
            getattr(before, field).value != getattr(after, field).value
            for field in _FIELDS
        ):
            if is_archived:
                archived += 1
            else:
                active += 1
        if changed_fields and any(field in session_overrides for field in changed_fields):
            shadowed += 1
    return active, archived, shadowed


async def _instance_policy_view(
    session: AsyncSession,
    *,
    scope: InstanceScope,
    instance_id: str,
    actor_id: str,
    subject_id: str,
    config: EffectiveConfig,
    instance_revision: int,
    overrides: dict[str, object],
    generation: int,
) -> InstanceDialoguePolicyView:
    active, archived, shadowed = await _instance_impact(
        session,
        scope=scope,
        instance_id=instance_id,
        actor_id=actor_id,
        subject_id=subject_id,
        config=config,
        current=overrides,
    )
    return InstanceDialoguePolicyView(
        scope=scope,
        instance_id=instance_id,
        fields=_instance_field_views(config, overrides),
        global_revision=config.revision,
        instance_revision=instance_revision,
        impact_epoch=generation,
        affected_active_windows=active,
        affected_archived_windows=archived,
        session_shadowed_windows=shadowed,
    )


def _field_views(
    snapshot: WindowPolicySnapshot,
    session_overrides: dict[str, object],
    *,
    instance_overrides: dict[str, object] | None = None,
) -> WindowDialoguePolicyFields:
    instance = snapshot.instance_overrides if instance_overrides is None else instance_overrides
    global_values = snapshot.global_resolved
    resolved = resolve_conversation_settings(
        snapshot.config,
        instance_overrides=instance,
        session_overrides=session_overrides,
    )
    result: dict[str, dict[str, object]] = {}
    for field in _FIELDS:
        global_field = getattr(global_values, field)
        resolved_field = getattr(resolved, field)
        result[field] = {
            "global_value": global_field.value,
            "instance_override": instance.get(field),
            "has_instance_override": field in instance,
            "session_override": session_overrides.get(field),
            "has_session_override": field in session_overrides,
            "effective": resolved_field.value,
            "source": resolved_field.source,
        }
    return WindowDialoguePolicyFields.model_validate(result)


def _view(snapshot: WindowPolicySnapshot) -> WindowDialoguePolicyView:
    fields = _field_views(snapshot, snapshot.session_overrides)
    private_field = fields.private_may_stay_silent
    return WindowDialoguePolicyView(
        field=_FIELD,
        global_value=private_field.global_value,
        override=private_field.session_override,
        effective=private_field.effective,
        source=private_field.source,
        fields=fields,
        origin_status=snapshot.origin_status,
        instance_scope=snapshot.instance_scope,
        instance_id=snapshot.instance_id,
        global_revision=snapshot.config.revision,
        instance_revision=snapshot.instance_revision,
        session_revision=snapshot.session_revision,
    )


def _proposed(
    snapshot: WindowPolicySnapshot, overrides: dict[str, object]
) -> WindowDialoguePolicyProposed:
    fields = _field_views(snapshot, overrides)
    private_field = fields.private_may_stay_silent
    return WindowDialoguePolicyProposed(
        override=private_field.session_override,
        effective=private_field.effective,
        source=private_field.source,
        fields=fields,
    )


async def _persona_revisions(
    session: AsyncSession,
    snapshot: WindowPolicySnapshot,
    proposed_overrides: dict[str, object],
    *,
    lock: bool,
) -> dict[str, int | None]:
    persona_ids = {
        persona_id
        for persona_id in (
            snapshot.resolved.persona_id.value,
            resolve_conversation_settings(
                snapshot.config,
                instance_overrides=snapshot.instance_overrides,
                session_overrides=proposed_overrides,
            ).persona_id.value,
        )
        if persona_id is not None
    }
    if not persona_ids:
        return {}
    statement = (
        select(Persona.id, Persona.revision)
        .where(Persona.id.in_(persona_ids))
        .order_by(Persona.id)
    )
    if lock:
        statement = statement.with_for_update()
    rows = (await session.execute(statement)).all()
    revisions = {persona_id: revision for persona_id, revision in rows}
    return {persona_id: revisions.get(persona_id) for persona_id in sorted(persona_ids)}


def _state(
    snapshot: WindowPolicySnapshot,
    window: Window,
    persona_revisions: dict[str, int | None],
) -> dict:
    return {
        "global_revision": snapshot.config.revision,
        "generation": snapshot.generation,
        "instance_revision": snapshot.instance_revision,
        "session_revision": snapshot.session_revision,
        "instance_overrides": snapshot.instance_overrides,
        "session_overrides": snapshot.session_overrides,
        "origin_status": snapshot.origin_status,
        "instance_scope": snapshot.instance_scope,
        "instance_id": snapshot.instance_id,
        "persona_revisions": persona_revisions,
        "window_updated_at": window.updated_at.isoformat(),
    }


async def _validate_persona_selection(
    session: AsyncSession, overrides: dict[str, object]
) -> None:
    persona_id = overrides.get("persona_id")
    if "persona_id" in overrides and persona_id is not None and await session.get(Persona, persona_id) is None:
        raise LingError(
            ErrorCode.invalid_request,
            "persona_id 指向的人格不存在，请重新选择",
            status_code=422,
        )


async def _persona_revision_map(
    session: AsyncSession,
    persona_ids: set[str | None],
    *,
    lock: bool,
) -> dict[str, int | None]:
    selected = {persona_id for persona_id in persona_ids if persona_id is not None}
    if not selected:
        return {}
    statement = (
        select(Persona.id, Persona.revision)
        .where(Persona.id.in_(selected))
        .order_by(Persona.id)
    )
    if lock:
        statement = statement.with_for_update()
    rows = (await session.execute(statement)).all()
    revisions = {persona_id: revision for persona_id, revision in rows}
    return {persona_id: revisions.get(persona_id) for persona_id in sorted(selected)}


async def _instance_policy_state(
    session: AsyncSession,
    *,
    scope: InstanceScope,
    instance_id: str,
    config: EffectiveConfig,
    row: InstanceDialogueOverride | None,
    persona_revisions: dict[str, int | None],
) -> dict[str, object]:
    generation = await session.scalar(select(DialogueConfigGuard.generation))
    return {
        "scope": scope,
        "instance_id": instance_id,
        "global_revision": config.revision,
        "generation": generation if generation is not None else 0,
        "instance_revision": 0 if row is None else row.revision,
        "instance_overrides": {} if row is None else dict(row.overrides or {}),
        "persona_revisions": persona_revisions,
    }


async def _load_instance_policy(
    session: AsyncSession,
    *,
    scope: InstanceScope,
    instance_id: str,
    config: ConfigDep,
    create_default: bool,
) -> tuple[ConnectorInstance | None, EffectiveConfig, InstanceDialogueOverride | None, int]:
    connector = await _require_live_connector_instance(
        session,
        scope=scope,
        instance_id=instance_id,
        create_default=create_default,
    )
    effective = await config.with_session(session).effective()
    row = await session.get(InstanceDialogueOverride, (scope, instance_id))
    generation = await session.scalar(select(DialogueConfigGuard.generation))
    return connector, effective, row, generation if generation is not None else 0


async def _save_instance_override(
    session: AsyncSession,
    *,
    scope: InstanceScope,
    instance_id: str,
    expected_revision: int,
    set_values: dict[str, object],
    clear_fields: list[str],
    user_id: str,
) -> InstanceDialogueOverride | None:
    row = await session.get(InstanceDialogueOverride, (scope, instance_id))
    current = {} if row is None else dict(row.overrides or {})
    revision = 0 if row is None else row.revision
    if revision != expected_revision:
        raise LingError(ErrorCode.conflict, "设置已变更，请重新预览", status_code=409)
    updated = {**current, **set_values}
    for field in clear_fields:
        updated.pop(field, None)
    validate_conversation_overrides(updated)
    if updated == current:
        return row
    if row is None:
        row = InstanceDialogueOverride(
            scope=scope,
            instance_id=instance_id,
            revision=1,
            overrides=updated,
            updated_by=user_id,
        )
        session.add(row)
    else:
        row.revision += 1
        row.overrides = updated
        row.updated_by = user_id
    row.updated_at = utcnow()
    await session.flush()
    return row


def _signing_key(request: Request) -> bytes:
    return hmac.new(
        load_signing_root_key(request.app.state.settings),
        _POLICY_SIGNING_CONTEXT,
        hashlib.sha256,
    ).digest()


def _token(request: Request, payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).rstrip(b"=")
    signature = hmac.new(_signing_key(request), encoded, hashlib.sha256).hexdigest()
    return f"{encoded.decode()}.{signature}"


def _read_token(request: Request, token: str) -> dict:
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(_signing_key(request), encoded.encode("ascii"), hashlib.sha256)
        if not hmac.compare_digest(signature, expected.hexdigest()):
            raise ValueError("bad signature")
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if not isinstance(payload, dict) or type(payload.get("expires")) is not int:
            raise ValueError("bad payload")
        if payload["expires"] <= int(time.time()):
            raise ValueError("expired")
        return payload
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise LingError(
            ErrorCode.conflict, "预览已失效，请重新预览", status_code=409
        ) from exc


def _binding_reference(window: Window) -> str:
    reference = window.connector_window_ref
    if reference is None:
        reference = window.title
    parts = str(reference or "").split(":", 2)
    expected_kind = "group" if window.visibility == "group_scope" else "private"
    if len(parts) != 3 or parts[1] != expected_kind or not parts[0] or not parts[2]:
        raise LingError(ErrorCode.conflict, "会话缺少可核对的平台标识", status_code=409)
    return str(reference)


def _require_binding_window(window: Window) -> None:
    if window.channel not in {"onebot", "qqbot"}:
        raise LingError(ErrorCode.conflict, "此会话没有平台实例来源", status_code=409)
    if window.origin_status != "unknown" and not window.origin_manually_bound:
        raise LingError(ErrorCode.conflict, "此会话已有可信平台来源", status_code=409)


async def _binding_connector(
    session: AsyncSession,
    request: Request,
    *,
    scope: InstanceScope,
    instance_id: str,
) -> tuple[WindowOriginBindingCandidate, str, int]:
    from ling.capabilities.channels import onebot_config, qqbot_config
    from ling.settings.connector_overrides import resolve_field_values

    connector = await _require_live_connector_instance(
        session, scope=scope, instance_id=instance_id, create_default=False
    )
    settings = request.app.state.settings
    resolved = await resolve_field_values(
        session,
        scope=scope,
        instance_id=instance_id,
        settings=settings,
        onebot=onebot_config(),
        qqbot=qqbot_config(settings),
    )
    namespace = str(resolved.get(f"LING_{scope.upper()}_INSTANCE_ID") or f"{scope}-default")
    candidate = WindowOriginBindingCandidate(
        scope=scope,
        instance_id=instance_id,
        display_name=connector.display_name if connector is not None else DEFAULT_INSTANCE_ID,
        enabled=connector.enabled if connector is not None else True,
    )
    return candidate, namespace, resolved.generation


async def _binding_plan(
    session: AsyncSession,
    request: Request,
    config: ConfigDep,
    window: Window,
    body: OriginBindingChange,
) -> tuple[WindowDialoguePolicyView, WindowDialoguePolicyView, WindowOriginBindingCandidate | None, str]:
    _require_binding_window(window)
    require_active_window(window)
    reference = _binding_reference(window)
    if body.scope is not None and body.scope != window.channel:
        raise LingError(ErrorCode.invalid_request, "平台类型与会话不一致", status_code=422)
    if body.instance_id is None and not window.origin_manually_bound:
        raise LingError(ErrorCode.conflict, "此会话还没有人工绑定", status_code=409)
    if (
        body.instance_id is not None
        and window.origin_manually_bound
        and (window.origin_scope, window.origin_instance_id) == (body.scope, body.instance_id)
    ):
        raise LingError(ErrorCode.conflict, "此会话已经绑定该实例", status_code=409)

    current = await locked_window_policy(session, config, window.id)
    candidate = None
    connector_generation = 0
    namespace = None
    target_overrides: dict[str, object] = {}
    target_revision = 0
    if body.scope is not None and body.instance_id is not None:
        candidate, namespace, connector_generation = await _binding_connector(
            session, request, scope=body.scope, instance_id=body.instance_id
        )
        if reference.split(":", 1)[0] != namespace:
            raise LingError(ErrorCode.conflict, "会话平台标识与目标实例不匹配", status_code=409)
        reference_match = or_(
            Window.connector_window_ref == reference,
            and_(Window.connector_window_ref.is_(None), Window.title == reference),
        )
        existing = select(Window.id).where(
            Window.id != window.id,
            Window.actor_id == window.actor_id,
            Window.channel == window.channel,
            Window.origin_status == "known",
            Window.origin_scope == body.scope,
            Window.origin_instance_id == body.instance_id,
            reference_match,
        )
        if window.visibility != "group_scope":
            existing = existing.where(Window.subject_id == window.subject_id)
        if await session.scalar(existing.limit(1)) is not None:
            raise LingError(ErrorCode.conflict, "目标实例已有相同的平台会话", status_code=409)
        target = await session.get(InstanceDialogueOverride, (body.scope, body.instance_id))
        if target is not None:
            target_overrides = dict(target.overrides or {})
            target_revision = target.revision
            await _validate_persona_selection(session, target_overrides)

    proposed = replace(
        current,
        instance_overrides=target_overrides,
        instance_revision=target_revision,
        origin_status="unknown" if candidate is None else "known",
        instance_scope=None if candidate is None else candidate.scope,
        instance_id=None if candidate is None else candidate.instance_id,
    )
    persona_revisions = await _persona_revision_map(
        session,
        {current.resolved.persona_id.value, proposed.resolved.persona_id.value},
        lock=False,
    )
    state = {
        "current": _state(current, window, persona_revisions),
        "manually_bound": window.origin_manually_bound,
        "reference": reference,
        "archived": window.archived,
        "target": None if candidate is None else candidate.model_dump(),
        "target_namespace": namespace,
        "target_connector_generation": connector_generation,
        "target_overrides": target_overrides,
        "target_revision": target_revision,
    }
    serialized = json.dumps(state, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    state_digest = hashlib.sha256(serialized.encode()).hexdigest()
    return _view(current), _view(proposed), candidate, state_digest


@router.get("/sessions/{session_id}/origin-binding", response_model=WindowOriginBindingOptions)
async def get_origin_binding_options(
    session_id: str,
    request: Request,
    auth: CurrentAuth,
    session: DbSession,
) -> WindowOriginBindingOptions:
    if auth.user.role != "owner":
        raise LingError(ErrorCode.forbidden, "只有所有者可以管理平台来源", status_code=403)
    await lock_dialogue_config(session)
    window = await policy_window(
        session,
        window_id=session_id,
        subject_id=auth.user.subject_id,
        actor_id=request.app.state.settings.actor_id,
        allow_managed_groups=True,
    )
    _require_binding_window(window)
    scope = _require_instance_scope(window.channel)
    reference = _binding_reference(window)
    from ling.settings.connector_instances import ConnectorInstanceService

    rows = await ConnectorInstanceService(session).list_instances(scope=window.channel)
    instance_ids = [row.instance_id for row in rows]
    if DEFAULT_INSTANCE_ID not in instance_ids:
        instance_ids.insert(0, DEFAULT_INSTANCE_ID)
    candidates = []
    for instance_id in instance_ids:
        candidate, namespace, _ = await _binding_connector(
            session, request, scope=scope, instance_id=instance_id
        )
        if reference.split(":", 1)[0] == namespace:
            candidates.append(candidate)
    return WindowOriginBindingOptions(
        scope=scope,
        candidates=candidates,
        manually_bound=window.origin_manually_bound,
    )


@router.post(
    "/sessions/{session_id}/origin-binding/preview", response_model=WindowOriginBindingPreview
)
async def preview_origin_binding(
    session_id: str,
    body: OriginBindingChange,
    request: Request,
    auth: CurrentAuth,
    session: DbSession,
    config: ConfigDep,
) -> WindowOriginBindingPreview:
    _write_guards(request, auth)
    await lock_dialogue_config(session)
    window = await policy_window(
        session,
        window_id=session_id,
        subject_id=auth.user.subject_id,
        actor_id=request.app.state.settings.actor_id,
        allow_managed_groups=True,
    )
    current, proposed, candidate, digest = await _binding_plan(session, request, config, window, body)
    expires = int(time.time()) + _PREVIEW_SECONDS
    payload = {
        "purpose": "window_origin_binding",
        "user_id": auth.user.id,
        "subject_id": auth.user.subject_id,
        "window_id": session_id,
        "target": body.target,
        "state_digest": digest,
        "expires": expires,
    }
    return WindowOriginBindingPreview(
        current=current,
        proposed=proposed,
        target=candidate,
        preview_token=_token(request, payload),
        expires_at=expires,
    )


@router.put("/sessions/{session_id}/origin-binding", response_model=WindowDialoguePolicyView)
async def put_origin_binding(
    session_id: str,
    body: OriginBindingCommit,
    request: Request,
    auth: CurrentAuth,
    session: DbSession,
    config: ConfigDep,
) -> WindowDialoguePolicyView:
    _write_guards(request, auth)
    token = _read_token(request, body.preview_token)
    if (
        token.get("purpose") != "window_origin_binding"
        or token.get("user_id") != auth.user.id
        or token.get("subject_id") != auth.user.subject_id
        or token.get("window_id") != session_id
        or token.get("target") != body.target
    ):
        raise LingError(ErrorCode.conflict, "预览与请求不一致，请重新预览", status_code=409)
    await lock_dialogue_config(session)
    window = await policy_window(
        session,
        window_id=session_id,
        subject_id=auth.user.subject_id,
        actor_id=request.app.state.settings.actor_id,
        allow_managed_groups=True,
    )
    try:
        _, _, candidate, digest = await _binding_plan(session, request, config, window, body)
    except LingError as exc:
        raise LingError(ErrorCode.conflict, "设置已变更，请重新预览", status_code=409) from exc
    if digest != token.get("state_digest") or token["expires"] <= int(time.time()):
        raise LingError(ErrorCode.conflict, "设置已变更，请重新预览", status_code=409)
    if candidate is not None and candidate.instance_id == DEFAULT_INSTANCE_ID:
        await _require_live_connector_instance(
            session, scope=candidate.scope, instance_id=candidate.instance_id, create_default=True
        )
    window.origin_scope = None if candidate is None else candidate.scope
    window.origin_instance_id = None if candidate is None else candidate.instance_id
    window.origin_status = "unknown" if candidate is None else "known"
    window.origin_manually_bound = candidate is not None
    await session.flush()
    updated = await locked_window_policy(session, config, session_id)
    response = _view(updated)
    _record_policy_audit(
        session,
        action="window_origin_binding_changed",
        actor_id=str(auth.user.id),
        target_type="session",
        target_id=session_id,
        set_fields=["origin_scope", "origin_instance_id"] if candidate else [],
        clear_fields=["origin_scope", "origin_instance_id"] if candidate is None else [],
        revision=updated.generation,
    )
    await session.commit()
    audit_event(
        "window_origin_binding_changed",
        actor=str(auth.user.id),
        target_type="session",
        target_id=session_id,
        attributes={"bound": candidate is not None},
    )
    return response


@router.get("/sessions/{session_id}/dialogue-policy", response_model=WindowDialoguePolicyView)
async def get_policy(
    session_id: str,
    request: Request,
    auth: CurrentAuth,
    session: DbSession,
    config: ConfigDep,
) -> WindowDialoguePolicyView:
    await lock_dialogue_config(session)
    await policy_window(
        session,
        window_id=session_id,
        subject_id=auth.user.subject_id,
        actor_id=request.app.state.settings.actor_id,
        allow_managed_groups=auth.user.role == "owner",
    )
    snapshot = await locked_window_policy(session, config, session_id)
    return _view(snapshot)


@router.post(
    "/sessions/{session_id}/dialogue-policy/preview", response_model=WindowDialoguePolicyPreview
)
async def preview_policy(
    session_id: str,
    body: PolicyChange,
    request: Request,
    auth: CurrentAuth,
    session: DbSession,
    config: ConfigDep,
) -> WindowDialoguePolicyPreview:
    _write_guards(request, auth)
    await lock_dialogue_config(session)
    window = await policy_window(
        session,
        window_id=session_id,
        subject_id=auth.user.subject_id,
        actor_id=request.app.state.settings.actor_id,
        allow_managed_groups=True,
    )
    require_active_window(window)
    snapshot = await locked_window_policy(session, config, session_id)
    proposed_overrides = body.apply(snapshot.session_overrides)
    await _validate_persona_selection(session, proposed_overrides)
    persona_revisions = await _persona_revisions(
        session, snapshot, proposed_overrides, lock=False
    )
    expires = int(time.time()) + _PREVIEW_SECONDS
    payload = {
        "user_id": auth.user.id,
        "subject_id": auth.user.subject_id,
        "window_id": session_id,
        "change": body.normalized,
        "state": _state(snapshot, window, persona_revisions),
        "expires": expires,
    }
    return WindowDialoguePolicyPreview(
        current=_view(snapshot),
        proposed=_proposed(snapshot, proposed_overrides),
        preview_token=_token(request, payload),
        expires_at=expires,
    )


@router.put("/sessions/{session_id}/dialogue-policy", response_model=WindowDialoguePolicyView)
async def put_policy(
    session_id: str,
    body: PolicyCommit,
    request: Request,
    auth: CurrentAuth,
    session: DbSession,
    config: ConfigDep,
) -> WindowDialoguePolicyView:
    _write_guards(request, auth)
    token = _read_token(request, body.preview_token)
    if (
        token.get("user_id") != auth.user.id
        or token.get("subject_id") != auth.user.subject_id
        or token.get("window_id") != session_id
        or token.get("change") != body.normalized
    ):
        raise LingError(ErrorCode.conflict, "预览与请求不一致，请重新预览", status_code=409)
    await lock_dialogue_config(session)
    window = await policy_window(
        session,
        window_id=session_id,
        subject_id=auth.user.subject_id,
        actor_id=request.app.state.settings.actor_id,
        allow_managed_groups=True,
    )
    require_active_window(window)
    snapshot = await locked_window_policy(session, config, session_id)
    proposed_overrides = body.apply(snapshot.session_overrides)
    persona_revisions = await _persona_revisions(
        session, snapshot, proposed_overrides, lock=True
    )
    if token.get("state") != _state(snapshot, window, persona_revisions) or token["expires"] <= int(time.time()):
        raise LingError(ErrorCode.conflict, "设置已变更，请重新预览", status_code=409)
    await _validate_persona_selection(session, proposed_overrides)
    await save_window_policy(
        session,
        window_id=session_id,
        expected_revision=snapshot.session_revision,
        set_values=body.set,
        clear_fields=body.clear,
        user_id=auth.user.id,
    )
    updated = await locked_window_policy(session, config, session_id)
    response = _view(updated)
    if updated.session_revision != snapshot.session_revision:
        _record_policy_audit(
            session,
            action="window_dialogue_policy_changed",
            actor_id=str(auth.user.id),
            target_type="session",
            target_id=session_id,
            set_fields=list(body.set),
            clear_fields=body.clear,
            revision=updated.session_revision,
        )
    await session.commit()
    if updated.session_revision != snapshot.session_revision:
        audit_event(
            "window_dialogue_policy_changed",
            actor=str(auth.user.id),
            target_type="session",
            target_id=session_id,
            attributes={
                "set_fields": sorted(body.set),
                "clear_fields": sorted(body.clear),
                "revision": updated.session_revision,
            },
        )
    return response


@router.get(
    "/settings/connectors/{scope}/instances/{instance_id}/dialogue-policy",
    response_model=InstanceDialoguePolicyView,
)
async def get_instance_policy(
    scope: str,
    instance_id: str,
    request: Request,
    auth: CurrentAuth,
    session: DbSession,
    config: ConfigDep,
) -> InstanceDialoguePolicyView:
    if auth.user.role != "owner":
        raise LingError(ErrorCode.forbidden, "只有所有者可以查看平台实例设置", status_code=403)
    scope = _require_instance_scope(scope)
    await lock_dialogue_config(session)
    _, effective, row, generation = await _load_instance_policy(
        session,
        scope=scope,
        instance_id=instance_id,
        config=config,
        create_default=False,
    )
    return await _instance_policy_view(
        session,
        scope=scope,
        instance_id=instance_id,
        actor_id=request.app.state.settings.actor_id,
        subject_id=auth.user.subject_id,
        config=effective,
        instance_revision=0 if row is None else row.revision,
        overrides={} if row is None else dict(row.overrides or {}),
        generation=generation,
    )


@router.post(
    "/settings/connectors/{scope}/instances/{instance_id}/dialogue-policy/preview",
    response_model=InstanceDialoguePolicyPreview,
)
async def preview_instance_policy(
    scope: str,
    instance_id: str,
    body: PolicyChange,
    request: Request,
    auth: CurrentAuth,
    session: DbSession,
    config: ConfigDep,
) -> InstanceDialoguePolicyPreview:
    _write_guards(request, auth)
    scope = _require_instance_scope(scope)
    await lock_dialogue_config(session)
    _, effective, row, generation = await _load_instance_policy(
        session,
        scope=scope,
        instance_id=instance_id,
        config=config,
        create_default=False,
    )
    current_overrides = {} if row is None else dict(row.overrides or {})
    proposed_overrides = body.apply(current_overrides)
    await _validate_persona_selection(session, proposed_overrides)
    current_persona = resolve_conversation_settings(
        effective, instance_overrides=current_overrides
    ).persona_id.value
    proposed_persona = resolve_conversation_settings(
        effective, instance_overrides=proposed_overrides
    ).persona_id.value
    persona_revisions = await _persona_revision_map(
        session, {current_persona, proposed_persona}, lock=False
    )
    state = await _instance_policy_state(
        session,
        scope=scope,
        instance_id=instance_id,
        config=effective,
        row=row,
        persona_revisions=persona_revisions,
    )
    proposed_active, proposed_archived, proposed_shadowed = await _instance_impact(
        session,
        scope=scope,
        instance_id=instance_id,
        actor_id=request.app.state.settings.actor_id,
        subject_id=auth.user.subject_id,
        config=effective,
        current=current_overrides,
        proposed=proposed_overrides,
    )
    expires = int(time.time()) + _PREVIEW_SECONDS
    payload = {
        "purpose": "instance_dialogue_policy",
        "user_id": auth.user.id,
        "subject_id": auth.user.subject_id,
        "scope": scope,
        "instance_id": instance_id,
        "change": body.normalized,
        "state": state,
        "expires": expires,
    }
    current = await _instance_policy_view(
        session,
        scope=scope,
        instance_id=instance_id,
        actor_id=request.app.state.settings.actor_id,
        subject_id=auth.user.subject_id,
        config=effective,
        instance_revision=0 if row is None else row.revision,
        overrides=current_overrides,
        generation=generation,
    )
    proposed = InstanceDialoguePolicyProposed(
        fields=_instance_field_views(effective, proposed_overrides),
        affected_active_windows=proposed_active,
        affected_archived_windows=proposed_archived,
        session_shadowed_windows=proposed_shadowed,
    )
    return InstanceDialoguePolicyPreview(
        current=current,
        proposed=proposed,
        preview_token=_token(request, payload),
        expires_at=expires,
    )


@router.put(
    "/settings/connectors/{scope}/instances/{instance_id}/dialogue-policy",
    response_model=InstanceDialoguePolicyView,
)
async def put_instance_policy(
    scope: str,
    instance_id: str,
    body: PolicyCommit,
    request: Request,
    auth: CurrentAuth,
    session: DbSession,
    config: ConfigDep,
) -> InstanceDialoguePolicyView:
    _write_guards(request, auth)
    scope = _require_instance_scope(scope)
    token = _read_token(request, body.preview_token)
    if (
        token.get("purpose") != "instance_dialogue_policy"
        or token.get("user_id") != auth.user.id
        or token.get("subject_id") != auth.user.subject_id
        or token.get("scope") != scope
        or token.get("instance_id") != instance_id
        or token.get("change") != body.normalized
    ):
        raise LingError(ErrorCode.conflict, "预览与请求不一致，请重新预览", status_code=409)

    await lock_dialogue_config(session)
    connector, effective, row, _ = await _load_instance_policy(
        session,
        scope=scope,
        instance_id=instance_id,
        config=config,
        create_default=False,
    )
    current_overrides = {} if row is None else dict(row.overrides or {})
    proposed_overrides = body.apply(current_overrides)
    await _validate_persona_selection(session, proposed_overrides)
    current_persona = resolve_conversation_settings(
        effective, instance_overrides=current_overrides
    ).persona_id.value
    proposed_persona = resolve_conversation_settings(
        effective, instance_overrides=proposed_overrides
    ).persona_id.value
    persona_revisions = await _persona_revision_map(
        session, {current_persona, proposed_persona}, lock=True
    )
    state = await _instance_policy_state(
        session,
        scope=scope,
        instance_id=instance_id,
        config=effective,
        row=row,
        persona_revisions=persona_revisions,
    )
    if state != token.get("state") or token["expires"] <= int(time.time()):
        raise LingError(ErrorCode.conflict, "设置已变更，请重新预览", status_code=409)

    previous_revision = 0 if row is None else row.revision
    if connector is None and proposed_overrides != current_overrides:
        await _require_live_connector_instance(
            session, scope=scope, instance_id=instance_id, create_default=True
        )
    await _save_instance_override(
        session,
        scope=scope,
        instance_id=instance_id,
        expected_revision=previous_revision,
        set_values=body.set,
        clear_fields=body.clear,
        user_id=auth.user.id,
    )
    updated_row = await session.get(InstanceDialogueOverride, (scope, instance_id))
    generation = await session.scalar(select(DialogueConfigGuard.generation))
    view = await _instance_policy_view(
        session,
        scope=scope,
        instance_id=instance_id,
        actor_id=request.app.state.settings.actor_id,
        subject_id=auth.user.subject_id,
        config=effective,
        instance_revision=0 if updated_row is None else updated_row.revision,
        overrides={} if updated_row is None else dict(updated_row.overrides or {}),
        generation=generation if generation is not None else 0,
    )
    if view.instance_revision != previous_revision:
        _record_policy_audit(
            session,
            action="instance_dialogue_policy_changed",
            actor_id=str(auth.user.id),
            target_type="connector_instance",
            target_id=f"{scope}/{instance_id}",
            set_fields=list(body.set),
            clear_fields=body.clear,
            revision=view.instance_revision,
        )
    await session.commit()
    if view.instance_revision != previous_revision:
        audit_event(
            "instance_dialogue_policy_changed",
            actor=str(auth.user.id),
            target_type="connector_instance",
            target_id=f"{scope}/{instance_id}",
            attributes={
                "scope": scope,
                "instance_id": instance_id,
                "set_fields": sorted(body.set),
                "clear_fields": sorted(body.clear),
                "revision": view.instance_revision,
            },
        )
    return view
