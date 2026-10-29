"""跨端交接（HandoffCapsule）的应用层：生成、读取、撤销。

设计文档 §5 的硬规则：
- 默认只在**本人已绑定的私聊端**间自动读取。
- 默认有效 30 分钟，可由用户关闭或调整。
- 长期重要事实仍走证据化记忆，不靠胶囊。
- 两个端同时发言时不盲目合并轮次：按到达顺序仲裁并标注当前窗口。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ling.contracts.v1 import HandoffCapsule, Visibility
from ling.domains.consent.service import ConsentService
from ling.domains.conversation.service import ConversationService
from ling.infra.models import HandoffCapsuleRow

DEFAULT_TTL_MINUTES = 30
MAX_TTL_MINUTES = 240


@dataclass(frozen=True)
class HandoffView:
    capsule_id: str
    subject_id: str
    source_window_id: str
    recent_topic: str | None
    open_questions: list[str]
    short_quote: str | None
    expires_at: datetime
    remaining_seconds: int

    def to_contract(self) -> HandoffCapsule:
        return HandoffCapsule(
            capsule_id=self.capsule_id,
            subject_id=self.subject_id,
            source_window_id=self.source_window_id,
            recent_topic=self.recent_topic,
            open_questions=list(self.open_questions),
            short_quote=self.short_quote,
            visibility=Visibility.subject_private,
            consent_revision=0,
            created_at=self.expires_at,
            expires_at=self.expires_at,
        )


class HandoffService:
    def __init__(self, session: AsyncSession, *, actor_id: str = "actor_ling") -> None:
        self._session = session
        self._conversation = ConversationService(session, actor_id=actor_id)
        self._consent = ConsentService(session)

    async def create(
        self,
        *,
        subject_id: str,
        source_window_id: str,
        recent_topic: str | None = None,
        open_questions: list[str] | None = None,
        short_quote: str | None = None,
        ttl_minutes: int = DEFAULT_TTL_MINUTES,
    ) -> HandoffView:
        """生成胶囊。

        必须先有 ``memory.cross_device`` 授权：没有授权就不生成，
        避免"偷偷准备好可以跨端读的东西"。
        """
        if not await self._consent.is_active(subject_id, "memory.cross_device"):
            raise PermissionError("未授权跨端交接（memory.cross_device），不能生成胶囊")
        ttl = max(1, min(ttl_minutes, MAX_TTL_MINUTES))
        row = await self._conversation.create_handoff_capsule(
            subject_id=subject_id,
            source_window_id=source_window_id,
            recent_topic=recent_topic,
            open_questions=open_questions,
            short_quote=short_quote,
            ttl_minutes=ttl,
        )
        return self._view(row)

    async def read(
        self, *, subject_id: str, current_window_id: str
    ) -> HandoffView | None:
        """读取最近一条未过期、且**不是当前窗口自己**的胶囊。"""
        if not await self._consent.is_active(subject_id, "memory.cross_device"):
            return None
        row = await self._conversation.read_latest_capsule(
            subject_id=subject_id, exclude_window_id=current_window_id
        )
        if row is None:
            return None
        return self._view(row)

    async def revoke(self, capsule_id: str) -> bool:
        """撤销：直接把过期时间设到过去，读取立即失效。"""
        row = await self._session.get(HandoffCapsuleRow, capsule_id)
        if row is None:
            return False
        row.expires_at = datetime.now(UTC)
        await self._session.flush()
        return True

    @staticmethod
    def _view(row: HandoffCapsuleRow) -> HandoffView:
        expires = row.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        remaining = int(max(0, (expires - datetime.now(UTC)).total_seconds()))
        return HandoffView(
            capsule_id=row.id,
            subject_id=row.subject_id,
            source_window_id=row.source_window_id,
            recent_topic=row.recent_topic,
            open_questions=list(row.open_questions or []),
            short_quote=row.short_quote,
            expires_at=expires,
            remaining_seconds=remaining,
        )


__all__ = ["DEFAULT_TTL_MINUTES", "MAX_TTL_MINUTES", "HandoffService", "HandoffView"]
