"""Awareness 域：会话隔离模式与跨会话意识（ADR-0004）。

唯一写入方。两条铁律：

1. **跨窗口只传事实与事件，绝不传对话状态。**
   - 会跨窗口：这个人说过的事实、角色自己做过的事
   - 绝不跨窗口：那个窗口的上一句、指代、未答问题、草稿

2. **真正的门在数据层，不在提示词。**
   群聊窗口组装提示时，`subject_fact` 与 `actor_activity` 根本不进候选集，
   所以模型看不到、也就不可能说出来。不是靠它自觉。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import AwarenessItemRow, SessionPolicyRow, utcnow
from ling.infra.security import new_id

DEFAULT_TTL_DAYS = 7
# 同一 subject 最多保留的条目数，避免无限增长
MAX_ITEMS_PER_SUBJECT = 200


class IsolationMode(StrEnum):
    """会话隔离模式。"""

    sealed = "sealed"
    per_window = "per_window"
    continuous = "continuous"


class AwarenessKind(StrEnum):
    actor_activity = "actor_activity"  # 角色自己做过的事
    subject_fact = "subject_fact"  # 这个人说过的事实
    group_event = "group_event"  # 某个群发生过的事


class Audience(StrEnum):
    actor_private = "actor_private"  # 只能影响角色自身，不得原样说出
    subject_scoped = "subject_scoped"  # 该 subject 的私聊
    group_scoped = "group_scoped"  # 产生它的那个群
    public = "public"


# 跨窗口不传递的内容类型（显式列出，便于审计"我们刻意没有做什么"）
FORBIDDEN_CROSS_WINDOW = {
    "last_utterance",  # 上一句原话
    "coreference",  # 指代/代词
    "open_question",  # 别处未答的提问
    "draft",  # 草稿/半成品
    "other_group_content",  # 别的群的内容
}


@dataclass(frozen=True)
class AwarenessView:
    item_id: str
    kind: AwarenessKind
    text: str
    audience: Audience
    created_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True)
class PolicyView:
    subject_id: str
    mode: IsolationMode
    revision: int

    @property
    def allows_awareness(self) -> bool:
        return self.mode is IsolationMode.continuous

    @property
    def allows_long_term_memory(self) -> bool:
        # sealed 模式下连长期记忆都不读：严格区隔的硬要求
        return self.mode is not IsolationMode.sealed


class AwarenessService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---------------------------------------------------------- 模式

    async def policy(self, subject_id: str) -> PolicyView:
        row = await self._session.get(SessionPolicyRow, subject_id)
        if row is None:
            # 默认 per_window：与已实测行为一致，不改变现有用户预期
            return PolicyView(
                subject_id=subject_id, mode=IsolationMode.per_window, revision=0
            )
        return PolicyView(
            subject_id=row.subject_id,
            mode=IsolationMode(row.mode),
            revision=int(row.revision or 1),
        )

    async def set_mode(self, subject_id: str, mode: IsolationMode) -> PolicyView:
        """切换隔离模式。

        - 任何模式切换都会使**旧修订**的派生条目失效（不只是切到 sealed）：
          否则 continuous → per_window → continuous 会让旧条目悄悄复活。
        - 不物理删除：保留审计；重新启用 continuous 后需用户主动积累新条目。
        """
        row = await self._session.get(SessionPolicyRow, subject_id)
        new_revision = int(row.revision if row else 0) + 1
        if row is None:
            row = SessionPolicyRow(
                subject_id=subject_id, mode=mode.value, revision=new_revision
            )
            self._session.add(row)
        else:
            row.mode = mode.value
            row.revision = new_revision
        await self._session.flush()

        # 任何模式切换都让旧修订的派生失效（注意：否则切回来会悄悄复活）。
        # 不物理删除，保留审计；读取时还会再核对 policy_revision 兜底。
        await self._session.execute(
            update(AwarenessItemRow)
            .where(
                AwarenessItemRow.subject_id == subject_id,
                AwarenessItemRow.invalidated_at.is_(None),
            )
            .values(invalidated_at=utcnow())
        )
        await self._session.flush()

        return PolicyView(subject_id=subject_id, mode=mode, revision=new_revision)

    # ---------------------------------------------------------- 条目写入

    async def record(
        self,
        *,
        subject_id: str,
        kind: AwarenessKind,
        text: str,
        source_window_id: str | None,
        source_visibility: str,
        ttl_days: int = DEFAULT_TTL_DAYS,
        policy_revision: int = 1,
    ) -> str | None:
        """写入一条意识条目。

        - 只在 `continuous` 模式下写（其他模式不积累，避免"偷偷准备"）
        - `origin` 只接受事实来源；模型推测不进入意识
        """
        policy = await self.policy(subject_id)
        if not policy.allows_awareness:
            return None
        cleaned = text.strip()
        if not cleaned:
            return None

        audience = self._audience_for(kind, source_visibility)
        row = AwarenessItemRow(
            id=new_id("aw"),
            subject_id=subject_id,
            kind=kind.value,
            text=cleaned[:1000],
            audience=audience.value,
            source_window_id=source_window_id,
            source_visibility=source_visibility,
            origin="user_statement",
            policy_revision=policy_revision,
            created_at=utcnow(),
            expires_at=utcnow() + timedelta(days=max(1, ttl_days)),
        )
        self._session.add(row)
        await self._session.flush()
        await self._trim(subject_id)
        return row.id

    @staticmethod
    def _audience_for(kind: AwarenessKind, source_visibility: str) -> Audience:
        """确定性映射：条目类型决定受众，不由模型选。"""
        if kind is AwarenessKind.actor_activity:
            # 角色自己的动作：只能影响自身状态，不得原样说出
            return Audience.actor_private
        if kind is AwarenessKind.group_event:
            return Audience.group_scoped
        if kind is AwarenessKind.subject_fact:
            # 群里说的"事实"不能当 subject 级事实——群里有其他人
            if source_visibility == "group_scope":
                return Audience.group_scoped
            return Audience.subject_scoped
        return Audience.actor_private

    async def _trim(self, subject_id: str) -> None:
        rows = list(
            (
                await self._session.execute(
                    select(AwarenessItemRow)
                    .where(AwarenessItemRow.subject_id == subject_id)
                    .order_by(AwarenessItemRow.created_at.desc())
                )
            ).scalars()
        )
        for stale in rows[MAX_ITEMS_PER_SUBJECT:]:
            await self._session.delete(stale)
        await self._session.flush()

    # ---------------------------------------------------------- 读取（数据层过滤）

    async def visible_for_window(
        self,
        *,
        subject_id: str,
        window_visibility: str,
        window_id: str,
        now: datetime | None = None,
    ) -> list[AwarenessView]:
        """按**当前窗口类型**过滤候选集。

        这是 ADR-0004 里最关键的一步：
        - 群聊窗口 → 只可能拿到 group_scoped（且必须是本群）
        - 私聊窗口 → 只可能拿到 subject_scoped
        `actor_activity` 只在私聊里作为"背景"出现，且措辞要求不复述。
        """
        policy = await self.policy(subject_id)
        if not policy.allows_awareness:
            return []
        now = now or datetime.now(UTC)
        rows = list(
            (
                await self._session.execute(
                    select(AwarenessItemRow).where(
                        AwarenessItemRow.subject_id == subject_id,
                        AwarenessItemRow.invalidated_at.is_(None),
                    )
                )
            ).scalars()
        )
        is_group = window_visibility == "group_scope"
        out: list[AwarenessView] = []
        for row in rows:
            # 模式切换过就不再采信旧修订的派生（ADR-0004 修正）：
            # continuous → per_window → continuous 时旧条目不得复活。
            if int(getattr(row, "policy_revision", 0) or 0) != policy.revision:
                continue
            if row.expires_at is not None:
                expires = row.expires_at
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=UTC)
                if expires <= now:
                    continue
            audience = Audience(row.audience)
            if is_group:
                # 群聊：只能本群的 group_scoped；subject_fact / actor_activity 一律不进
                if audience is not Audience.group_scoped:
                    continue
                if row.source_window_id != window_id:
                    continue
            else:
                # 私聊：只能用 subject_scoped；别的群的内容不进私聊
                if audience is not Audience.subject_scoped:
                    continue
            out.append(
                AwarenessView(
                    item_id=row.id,
                    kind=AwarenessKind(row.kind),
                    text=row.text,
                    audience=audience,
                    created_at=row.created_at,
                    expires_at=row.expires_at,
                )
            )
        out.sort(key=lambda v: v.created_at)
        return out

    async def actor_activity_for_private(self, subject_id: str) -> list[AwarenessView]:
        """角色自身经历：**只在私聊**作为极弱背景出现，且禁止复述。"""
        policy = await self.policy(subject_id)
        if not policy.allows_awareness:
            return []
        rows = list(
            (
                await self._session.execute(
                    select(AwarenessItemRow)
                    .where(
                        AwarenessItemRow.subject_id == subject_id,
                        AwarenessItemRow.kind == AwarenessKind.actor_activity.value,
                        AwarenessItemRow.invalidated_at.is_(None),
                    )
                    .order_by(AwarenessItemRow.created_at.desc())
                    .limit(3)
                )
            ).scalars()
        )
        return [
            AwarenessView(
                item_id=r.id,
                kind=AwarenessKind(r.kind),
                text=r.text,
                audience=Audience(r.audience),
                created_at=r.created_at,
                expires_at=r.expires_at,
            )
            for r in rows
        ]

    # ---------------------------------------------------------- 派生（从轮次）

    async def derive_from_turn(
        self,
        *,
        subject_id: str,
        window_id: str,
        window_visibility: str,
        user_text: str,
    ) -> list[str]:
        """从一轮用户输入派生意识条目。

        只派生**明确的事实陈述**，且第三人称中性化——不保留原话。
        """
        import re

        policy = await self.policy(subject_id)
        if not policy.allows_awareness:
            return []
        created: list[str] = []
        patterns = (
            (r"我(?:的名字)?叫\s*([^\s，。,.！!？?]{1,20})", "这个人叫 {0}"),
            (r"我(?:很)?喜欢\s*([^\s，。,.！!？?]{1,20})", "这个人喜欢 {0}"),
            (r"我住在\s*([^\s，。,.！!？?]{1,20})", "这个人住在 {0}"),
            (r"我养了\s*([^\s，。,.！!？?]{1,20})", "这个人养了 {0}"),
        )
        for pattern, template in patterns:
            match = re.search(pattern, user_text)
            if not match:
                continue
            item_id = await self.record(
                subject_id=subject_id,
                kind=AwarenessKind.subject_fact,
                text=template.format(match.group(1)),
                source_window_id=window_id,
                source_visibility=window_visibility,
                policy_revision=policy.revision,
            )
            if item_id:
                created.append(item_id)
        return created


__all__ = [
    "DEFAULT_TTL_DAYS",
    "FORBIDDEN_CROSS_WINDOW",
    "MAX_ITEMS_PER_SUBJECT",
    "Audience",
    "AwarenessKind",
    "AwarenessService",
    "AwarenessView",
    "IsolationMode",
    "PolicyView",
]
