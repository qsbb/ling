"""Web 私聊会话的对话设置覆盖。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select, text, update

from ling.api.errors import LingError, not_found
from ling.contracts.v1 import ErrorCode
from ling.domains.persona.service import PersonaService, PersonaSnapshot
from ling.infra.models import DialogueConfigGuard, Window, WindowDialogueOverride, utcnow
from ling.settings.conversation_inheritance import (
    ResolvedConversationSettings,
    resolve_conversation_settings,
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
    global_value: bool
    override: bool | None
    override_revision: int
    generation: int

    @property
    def resolved(self) -> ResolvedConversationSettings:
        overrides = (
            {} if self.override is None else {"private_may_stay_silent": self.override}
        )
        return resolve_conversation_settings(self.config, session_overrides=overrides)

    @property
    def value(self) -> bool:
        return self.resolved.private_may_stay_silent.value

    @property
    def source(self) -> Literal["global", "session"]:
        # Web has no instance layer.
        return "global" if self.override is None else "session"


async def locked_window_policy(
    session: AsyncSession, config: ConfigService, window_id: str
) -> WindowPolicySnapshot:
    """在锁或一致性读取事务中读取全局设置与本会话覆盖。"""
    effective = await config.effective()
    row = await session.get(WindowDialogueOverride, window_id)
    generation = await session.scalar(select(DialogueConfigGuard.generation))
    return WindowPolicySnapshot(
        config=effective,
        global_value=effective.dialogue.private_may_stay_silent,
        override=None if row is None else row.private_may_stay_silent,
        override_revision=0 if row is None else row.revision,
        generation=generation if generation is not None else 0,
    )


def apply_window_policy(snapshot: WindowPolicySnapshot) -> EffectiveConfig:
    """得到本次处理使用的全局配置副本。"""
    return snapshot.resolved.apply_dialogue(snapshot.config)


async def policy_window(
    session: AsyncSession, *, window_id: str, subject_id: str
) -> Window:
    """只暴露当前主体自己的 Web 私聊设置。"""
    window = await session.scalar(
        select(Window)
        .where(
            Window.id == window_id,
            Window.subject_id == subject_id,
            Window.channel == "web",
            Window.visibility == "subject_private",
        )
        .with_for_update()
    )
    if window is None:
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
    value: bool | None,
    user_id: str,
) -> WindowDialogueOverride:
    """保留清空后的修订号，避免覆盖行删除重建。"""
    row = await session.get(WindowDialogueOverride, window_id)
    if row is None:
        if expected_revision != 0:
            raise LingError(ErrorCode.conflict, "设置已变更，请重新预览", status_code=409)
        row = WindowDialogueOverride(window_id=window_id, revision=1, updated_by=user_id)
        session.add(row)
    else:
        if row.revision != expected_revision:
            raise LingError(ErrorCode.conflict, "设置已变更，请重新预览", status_code=409)
        row.revision += 1
        row.updated_by = user_id
    row.private_may_stay_silent = value
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


async def runtime_window_snapshot(config: ConfigService, window_id: str) -> RuntimeWindowSnapshot:
    """在同一短事务中固定配置与人格，不在模型调用期间持有事务。"""
    from ling.infra.db import short_session_scope

    async with short_session_scope() as session:
        if session.get_bind().dialect.name != "postgresql":
            raise RuntimeError("会话配置快照需要 PostgreSQL")
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        snapshot = await locked_window_policy(session, config.with_session(session), window_id)
        effective = apply_window_policy(snapshot)
        persona = PersonaSnapshot(str(effective.persona_id or ""))
        try:
            # Persona 读取失败不应破坏配置读取事务，也不应让本轮后续动态换人格。
            async with session.begin_nested():
                await PersonaService(session, persona).get_or_none(persona)
        except Exception:  # noqa: BLE001 - 人格不可读时本轮按无人格处理
            persona.view = None
            persona.view_loaded = True
            logger.info("读取会话人格失败（本轮按无人格处理）")
        return RuntimeWindowSnapshot(config=effective, persona=persona)


async def runtime_window_config(config: ConfigService, window_id: str) -> EffectiveConfig:
    """兼容只需要会话生效配置的调用方。"""
    return (await runtime_window_snapshot(config, window_id)).config
