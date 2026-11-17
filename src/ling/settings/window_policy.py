"""Web 私聊会话的对话设置覆盖。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from sqlalchemy import select, text, update

from ling.api.errors import LingError, not_found
from ling.contracts.v1 import ErrorCode
from ling.domains.persona.service import PersonaService, PersonaSnapshot
from ling.infra.models import (
    ConversationAttemptSnapshot,
    DialogueConfigGuard,
    InstanceDialogueOverride,
    Window,
    WindowDialogueOverride,
    utcnow,
)
from ling.settings.conversation_inheritance import (
    ResolvedConversationSettings,
    resolve_conversation_settings,
    validate_conversation_overrides,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ling.ports.config import EffectiveConfig
    from ling.settings.service import ConfigService

logger = logging.getLogger(__name__)


async def lock_dialogue_config(session: AsyncSession, *, changed: bool = False) -> None:
    """串行化短事务中的全局发布、预览、保存和运行时解析。"""
    dialect = session.get_bind().dialect.name
    if dialect != "postgresql":
        raise RuntimeError(f"不支持的数据库类型: {dialect}")
    from sqlalchemy.dialects.postgresql import insert as postgres_insert

    statement = postgres_insert(DialogueConfigGuard).values(id=1, generation=0)
    await session.execute(
        statement.on_conflict_do_nothing(index_elements=[DialogueConfigGuard.id])
    )
    await session.execute(
        update(DialogueConfigGuard)
        .where(DialogueConfigGuard.id == 1)
        .values(generation=DialogueConfigGuard.generation + (1 if changed else 0))
    )


@dataclass(frozen=True)
class WindowPolicySnapshot:
    config: EffectiveConfig
    instance_overrides: dict[str, object]
    session_overrides: dict[str, object]
    instance_revision: int
    session_revision: int
    origin_status: Literal["known", "unknown", "not_applicable"]
    instance_scope: str | None
    instance_id: str | None
    generation: int

    @property
    def resolved(self) -> ResolvedConversationSettings:
        return resolve_conversation_settings(
            self.config,
            instance_overrides=self.instance_overrides,
            session_overrides=self.session_overrides,
        )

    @property
    def overrides(self) -> dict[str, object]:
        """Compatibility name for the session-level override object."""
        return self.session_overrides

    @property
    def override_revision(self) -> int:
        """Compatibility name for the session-level revision."""
        return self.session_revision

    @property
    def global_resolved(self) -> ResolvedConversationSettings:
        return resolve_conversation_settings(self.config)

    @property
    def global_value(self) -> bool:
        return self.global_resolved.private_may_stay_silent.value

    @property
    def override(self) -> bool | None:
        value = self.overrides.get("private_may_stay_silent")
        return value if type(value) is bool else None

    @property
    def value(self) -> bool:
        return self.resolved.private_may_stay_silent.value

    @property
    def source(self) -> Literal["global", "instance", "session"]:
        return self.resolved.private_may_stay_silent.source


async def locked_window_policy(
    session: AsyncSession, config: ConfigService, window_id: str
) -> WindowPolicySnapshot:
    """在锁或一致性读取事务中读取全局、实例与会话设置。"""
    effective = await config.effective()
    window = await session.get(Window, window_id)
    if window is None:
        raise not_found("会话不存在")

    instance_scope: str | None = None
    instance_id: str | None = None
    instance_row = None
    origin_status = cast(
        Literal["known", "unknown", "not_applicable"], window.origin_status
    )
    if origin_status == "known":
        if (
            window.origin_scope not in {"onebot", "qqbot"}
            or not window.origin_instance_id
        ):
            raise RuntimeError("会话的平台实例来源不完整")
        instance_scope = window.origin_scope
        instance_id = window.origin_instance_id
        instance_row = await session.get(
            InstanceDialogueOverride, (instance_scope, instance_id)
        )
    elif origin_status not in {"unknown", "not_applicable"}:
        raise RuntimeError("会话的平台实例来源状态无效")

    session_row = await session.get(WindowDialogueOverride, window_id)
    generation = await session.scalar(select(DialogueConfigGuard.generation))
    instance_overrides = (
        {} if instance_row is None else dict(instance_row.overrides or {})
    )
    session_overrides = (
        {} if session_row is None else dict(session_row.overrides or {})
    )
    validate_conversation_overrides(instance_overrides)
    validate_conversation_overrides(session_overrides)
    return WindowPolicySnapshot(
        config=effective,
        instance_overrides=instance_overrides,
        session_overrides=session_overrides,
        instance_revision=0 if instance_row is None else instance_row.revision,
        session_revision=0 if session_row is None else session_row.revision,
        origin_status=origin_status,
        instance_scope=instance_scope,
        instance_id=instance_id,
        generation=generation if generation is not None else 0,
    )


def apply_window_policy(snapshot: WindowPolicySnapshot) -> EffectiveConfig:
    """得到本次处理使用的全局配置副本。"""
    return snapshot.resolved.apply_dialogue(snapshot.config)


async def policy_window(
    session: AsyncSession,
    *,
    window_id: str,
    subject_id: str,
    actor_id: str,
    allow_managed_groups: bool = False,
) -> Window:
    """校验主体私聊或 owner 管理的群窗口配置范围。"""
    window = await session.scalar(
        select(Window)
        .where(Window.id == window_id)
        .with_for_update()
    )
    if window is None:
        raise not_found("会话不存在")

    own_private = (
        window.visibility == "subject_private"
        and window.subject_id == subject_id
        and (window.channel == "web" or window.actor_id == actor_id)
    )
    managed_group = (
        allow_managed_groups
        and window.visibility == "group_scope"
        and window.actor_id == actor_id
        and window.channel in {"onebot", "qqbot"}
    )
    if not own_private and not managed_group:
        raise not_found("会话不存在")
    return window


def require_active_window(window: Window) -> None:
    if window.archived:
        raise LingError(ErrorCode.conflict, "会话已归档，请恢复后再修改设置", status_code=409)


async def save_window_policy(
    session: AsyncSession,
    *,
    window_id: str,
    expected_revision: int,
    set_values: dict[str, object],
    clear_fields: list[str],
    user_id: str,
) -> WindowDialogueOverride | None:
    """保留清空后的修订号，避免覆盖行删除重建。"""
    row = await session.get(WindowDialogueOverride, window_id)
    revision = 0 if row is None else row.revision
    if revision != expected_revision:
        raise LingError(ErrorCode.conflict, "设置已变更，请重新预览", status_code=409)

    current = {} if row is None else dict(row.overrides or {})
    updated = {**current, **set_values}
    for field in clear_fields:
        updated.pop(field, None)
    validate_conversation_overrides(updated)
    if updated == current:
        return row

    if row is None:
        row = WindowDialogueOverride(
            window_id=window_id,
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


@dataclass(frozen=True)
class FixedConfigReader:
    """让一次处理中的所有决策读取同一份配置。"""

    config: EffectiveConfig

    async def effective(self) -> EffectiveConfig:
        return self.config


@dataclass(frozen=True)
class RuntimeWindowSnapshot:
    config: EffectiveConfig
    persona: PersonaSnapshot
    resolved: ResolvedConversationSettings
    global_revision: int
    instance_revision: int
    session_revision: int
    generation: int
    origin_status: Literal["known", "unknown", "not_applicable"]
    instance_scope: str | None
    instance_id: str | None

    @property
    def persona_revision(self) -> int | None:
        return None if self.persona.view is None else self.persona.view.revision


async def runtime_window_snapshot(
    config: ConfigService, window_id: str
) -> RuntimeWindowSnapshot:
    """在同一短事务中固定配置与人格，不在模型调用期间持有事务。"""
    from ling.infra.db import short_session_scope

    async with short_session_scope() as session:
        if session.get_bind().dialect.name != "postgresql":
            raise RuntimeError("会话配置快照需要 PostgreSQL")
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        snapshot = await locked_window_policy(session, config.with_session(session), window_id)
        return await _runtime_snapshot_from_policy(session, snapshot)


async def runtime_window_snapshot_in_session(
    session: AsyncSession, config: ConfigService, window_id: str
) -> RuntimeWindowSnapshot:
    """读取当前事务内尚未提交的窗口，并串行化受策略锁保护的配置写入。"""
    await lock_dialogue_config(session)
    snapshot = await locked_window_policy(session, config, window_id)
    return await _runtime_snapshot_from_policy(session, snapshot)


async def _runtime_snapshot_from_policy(
    session: AsyncSession, snapshot: WindowPolicySnapshot
) -> RuntimeWindowSnapshot:
    effective = apply_window_policy(snapshot)
    selected_persona_id = snapshot.resolved.persona_id.value
    persona = PersonaSnapshot(selected_persona_id or "")
    try:
        async with session.begin_nested():
            await PersonaService(session, persona).get_or_none(persona)
    except Exception:  # noqa: BLE001 - 人格不可读时本轮按无人格处理
        persona.view = None
        persona.view_loaded = True
        logger.info("读取会话人格失败（本轮按无人格处理）")
    return RuntimeWindowSnapshot(
        config=effective,
        persona=persona,
        resolved=snapshot.resolved,
        global_revision=snapshot.config.revision,
        instance_revision=snapshot.instance_revision,
        session_revision=snapshot.session_revision,
        generation=snapshot.generation,
        origin_status=snapshot.origin_status,
        instance_scope=snapshot.instance_scope,
        instance_id=snapshot.instance_id,
    )


async def record_conversation_attempt_snapshot(
    session: AsyncSession,
    *,
    turn_id: str,
    attempt: int,
    snapshot: RuntimeWindowSnapshot,
) -> ConversationAttemptSnapshot:
    """记录一次生成实际采用的配置来源，不覆盖已有 attempt 证据。"""
    key = (turn_id, attempt)
    existing = await session.get(ConversationAttemptSnapshot, key)
    if existing is not None:
        return existing

    resolved = snapshot.resolved
    row = ConversationAttemptSnapshot(
        turn_id=turn_id,
        attempt=attempt,
        global_revision=snapshot.global_revision,
        test_mode=snapshot.config.test_mode,
        instance_scope=snapshot.instance_scope,
        instance_id=snapshot.instance_id,
        instance_revision=snapshot.instance_revision,
        session_revision=snapshot.session_revision,
        impact_epoch=snapshot.generation,
        origin_status=snapshot.origin_status,
        persona_id=resolved.persona_id.value,
        persona_revision=snapshot.persona_revision,
        effective_values={
            "private_may_stay_silent": resolved.private_may_stay_silent.value,
            "group_mention_requires_reply": resolved.group_mention_requires_reply.value,
            "group_attention_deferred": resolved.group_attention_deferred.value,
            "group_turn_budget": resolved.group_turn_budget.value,
            "rest_may_skip": resolved.rest_may_skip.value,
        },
        field_sources={
            "persona_id": resolved.persona_id.source,
            "private_may_stay_silent": resolved.private_may_stay_silent.source,
            "group_mention_requires_reply": resolved.group_mention_requires_reply.source,
            "group_attention_deferred": resolved.group_attention_deferred.source,
            "group_turn_budget": resolved.group_turn_budget.source,
            "rest_may_skip": resolved.rest_may_skip.source,
        },
    )
    session.add(row)
    await session.flush()
    return row


async def runtime_window_config(config: ConfigService, window_id: str) -> EffectiveConfig:
    """兼容只需要会话生效配置的调用方。"""
    return (await runtime_window_snapshot(config, window_id)).config
