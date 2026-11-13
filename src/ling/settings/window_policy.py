"""Web 私聊会话的对话设置覆盖。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from sqlalchemy import select, text, update

from ling.api.errors import LingError, not_found
from ling.contracts.v1 import ErrorCode
from ling.infra.models import DialogueConfigGuard, Window, WindowDialogueOverride, utcnow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ling.ports.config import EffectiveConfig
    from ling.settings.service import ConfigService


async def lock_dialogue_config(session: AsyncSession, *, changed: bool = False) -> None:
    """串行化短事务中的全局发布、预览、保存和运行时解析。"""
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError(f"不支持的数据库类型: {dialect}")
    await session.execute(
        insert(DialogueConfigGuard)
        .values(id=1, generation=0)
        .on_conflict_do_nothing(index_elements=[DialogueConfigGuard.id])
    )
    # SQLite 的 SELECT FOR UPDATE 不提供互斥，UPDATE 在两种数据库中都持锁至提交。
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
    def value(self) -> bool:
        return self.global_value if self.override is None else self.override

    @property
    def source(self) -> str:
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
    return replace(
        snapshot.config,
        dialogue=replace(
            snapshot.config.dialogue,
            private_may_stay_silent=snapshot.value,
        ),
    )


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


async def runtime_window_config(config: ConfigService, window_id: str) -> EffectiveConfig:
    """用独立短事务固定配置，避免模型调用时持有配置锁。"""
    from ling.infra.db import session_scope

    async with session_scope() as session:
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        elif dialect == "sqlite":
            await session.execute(text("BEGIN"))
        else:
            raise RuntimeError(f"不支持的数据库类型: {dialect}")
        snapshot = await locked_window_policy(session, config.with_session(session), window_id)
        return apply_window_policy(snapshot)
