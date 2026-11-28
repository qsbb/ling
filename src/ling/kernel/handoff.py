"""跨端交接（HandoffCapsule）的应用层：生成、读取、撤销。

设计文档 §5 的硬规则：
- 默认只在**本人已绑定的私聊端**间自动读取。
- 默认有效 30 分钟，可由用户关闭或调整。
- 长期重要事实仍走证据化记忆，不靠胶囊。
- 两个端同时发言时不盲目合并轮次：按到达顺序仲裁并标注当前窗口。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.consent.service import ConsentService
from ling.domains.conversation.service import ConversationService
from ling.domains.identity.service import IdentityService
from ling.domains.memory.experience import active_message, shares_topic
from ling.domains.memory.purge import has_open_purge
from ling.domains.memory.recall_query import opens_new_topic, should_skip_automatic_recall
from ling.infra.models import HandoffCapsuleRow, Message, Window

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
    target_window_id: str | None = None


class HandoffService:
    def __init__(self, session: AsyncSession, *, actor_id: str = "actor_ling") -> None:
        self._session = session
        self._actor_id = actor_id
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
        grant = await self._consent.get(subject_id, "memory.cross_device")
        if not grant.granted or (grant.valid_until is not None and grant.valid_until <= datetime.now(UTC)):
            raise PermissionError("未授权跨端交接（memory.cross_device），不能生成胶囊")
        ttl = max(1, min(ttl_minutes, MAX_TTL_MINUTES))
        row = await self._conversation.create_handoff_capsule(
            subject_id=subject_id,
            source_window_id=source_window_id,
            recent_topic=recent_topic,
            open_questions=open_questions,
            short_quote=short_quote,
            consent_revision=grant.revision,
            ttl_minutes=ttl,
        )
        return self._view(row)

    async def capture(
        self, *, subject_id: str, source_window: Window, message: Message,
        target_window: Window | None = None, public_text: str | None = None,
    ) -> bool:
        """Keep an eligible statement or a participant's explicitly approved excerpt."""
        if source_window.archived or source_window.actor_id != self._actor_id:
            return False
        if source_window.trusted_instance_key is None or source_window.channel not in {"onebot", "qqbot"}:
            return False
        if message.role != "user" or message.sender_subject_id != subject_id or message.window_id != source_window.id:
            return False
        if message.memory_hidden_at is not None or message.retracted_at is not None:
            return False
        if source_window.visibility not in {"group_scope", "subject_private"}:
            return False
        if source_window.visibility == "subject_private" and source_window.subject_id != subject_id:
            return False
        if await has_open_purge(self._session, subject_id=subject_id):
            return False
        now = datetime.now(UTC)
        grant = await self._consent.get(subject_id, "memory.cross_device")
        if not grant.granted or (grant.valid_until is not None and grant.valid_until <= now):
            return False
        text = (public_text if target_window is not None else message.text) or ""
        if should_skip_automatic_recall(text) or opens_new_topic(text):
            return False
        publish_revision = None
        if target_window is not None:
            publish = await self._consent.get(subject_id, "public_publish")
            if (
                source_window.visibility != "subject_private" or target_window.visibility != "group_scope"
                or target_window.archived or not public_text or public_text not in message.text
                or target_window.actor_id != self._actor_id
                or source_window.trusted_instance_key != target_window.trusted_instance_key
                or not publish.granted or (publish.valid_until is not None and publish.valid_until <= now)
                or not await IdentityService(self._session).owns_private_window(source_window, subject_id)
            ):
                return False
            member = await self._session.scalar(select(Message.id).where(
                Message.window_id == target_window.id, Message.role == "user",
                Message.sender_subject_id == subject_id, active_message(),
            ).limit(1))
            if member is None:
                return False
            publish_revision = publish.revision
        await self._session.execute(insert(HandoffCapsuleRow).values(
            id=f"cap_{message.id}", subject_id=subject_id, source_window_id=source_window.id,
            source_message_id=message.id, target_window_id=target_window.id if target_window else None,
            recent_topic=text.strip()[:180], open_questions=[], visibility="group_scope" if target_window else "subject_private",
            consent_revision=grant.revision, publish_consent_revision=publish_revision,
            created_at=message.created_at,
            expires_at=min(
                message.created_at + timedelta(minutes=DEFAULT_TTL_MINUTES),
                grant.valid_until or (now + timedelta(minutes=DEFAULT_TTL_MINUTES)),
            ),
        ).on_conflict_do_nothing(index_elements=[HandoffCapsuleRow.id]))
        return True

    async def read(
        self, *, subject_id: str, current_window_id: str, user_text: str | None = None,
        validate_window: bool = False,
    ) -> HandoffView | None:
        """Resolve a live capsule in the current audience and connector instance."""
        now = datetime.now(UTC)
        grant = await self._consent.get(subject_id, "memory.cross_device")
        if (not grant.granted or (grant.valid_until is not None and grant.valid_until <= now)
                or await has_open_purge(self._session, subject_id=subject_id)):
            return None
        target = await self._session.get(Window, current_window_id, populate_existing=True)
        if validate_window and (target is None or target.archived or target.actor_id != self._actor_id):
            return None
        rows = await self._session.scalars(select(HandoffCapsuleRow).where(
            HandoffCapsuleRow.subject_id == subject_id, HandoffCapsuleRow.source_window_id != current_window_id,
            HandoffCapsuleRow.expires_at > now,
            HandoffCapsuleRow.consent_revision.in_((0, 1) if grant.revision == 1 else (grant.revision,)),
        ).order_by(HandoffCapsuleRow.created_at.desc(), HandoffCapsuleRow.id).limit(20))
        for row in rows:
            source = await self._session.get(Window, row.source_window_id, populate_existing=True)
            if row.source_message_id is None:
                if validate_window and (
                    target is None or target.visibility != "subject_private" or target.subject_id != subject_id
                    or source is None or source.archived or source.subject_id != subject_id
                    or source.visibility != "subject_private" or source.actor_id != self._actor_id
                ):
                    continue
                return self._view(row)
            if (
                source is None or source.archived or source.actor_id != self._actor_id
                or target is None or target.archived or target.actor_id != self._actor_id
                or source.trusted_instance_key is None or source.trusted_instance_key != target.trusted_instance_key
            ):
                continue
            source_message = await self._session.scalar(select(Message.id).where(
                Message.id == row.source_message_id, Message.window_id == source.id, Message.role == "user",
                Message.sender_subject_id == subject_id, active_message(),
            ))
            if source_message is None:
                continue
            identity = IdentityService(self._session)
            if source.visibility == "subject_private" and not await identity.owns_private_window(source, subject_id):
                continue
            if target.visibility == "group_scope":
                publish = await self._consent.get(subject_id, "public_publish")
                if (
                    row.target_window_id != target.id or row.visibility != "group_scope" or user_text is None
                    or source.visibility != "subject_private"
                    or not publish.granted or publish.revision != row.publish_consent_revision
                    or (publish.valid_until is not None and publish.valid_until <= now)
                ):
                    continue
            elif (
                row.target_window_id is not None or source.visibility not in {"group_scope", "subject_private"}
                or not await identity.owns_private_window(target, subject_id)
            ):
                continue
            if user_text is not None and not (
                any(cue in user_text for cue in ("接着聊", "刚才那个", "群里说的"))
                or shares_topic(user_text, row.recent_topic or "")
            ):
                continue
            view = self._view(row)
            if row.target_window_id:
                consumed = await self._session.execute(update(HandoffCapsuleRow).where(
                    HandoffCapsuleRow.id == row.id, HandoffCapsuleRow.expires_at > now,
                ).values(expires_at=now))
                if getattr(consumed, "rowcount", 0) != 1:
                    continue
            return view
        return None

    async def revoke(self, capsule_id: str, *, subject_id: str | None = None) -> bool:
        """撤销：直接把过期时间设到过去，读取立即失效。"""
        row = await self._session.get(HandoffCapsuleRow, capsule_id)
        if row is None or (subject_id is not None and row.subject_id != subject_id):
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
            target_window_id=row.target_window_id,
        )


__all__ = ["DEFAULT_TTL_MINUTES", "MAX_TTL_MINUTES", "HandoffService", "HandoffView"]
