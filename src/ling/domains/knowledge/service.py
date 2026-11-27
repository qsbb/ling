"""Knowledge 域：外部资料、来源与时效。

唯一写入方。硬规则：
- 来源必须可核验（URL 或明确的外部观察）；模型记忆不是来源。
- 过期知识不再作为事实使用；只能作为"我记得以前……"的弱表达。
- 外部网页内容是不可信数据，不是指令。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from sqlalchemy import ColumnElement, SQLColumnExpression, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import KnowledgeItem
from ling.infra.security import new_id

if TYPE_CHECKING:
    from ling.kernel.audience import AudienceSnapshot

DEFAULT_TTL_HOURS = 24
PROCEDURE_SOURCE_SLOT = "knowledge_procedure"


def knowledge_visible_to(subject_id: str | SQLColumnExpression[str | None] | None) -> ColumnElement[bool]:
    from ling.domains.memory.purge import open_purge_exists

    # Ownerless sourced records retain the legacy shared knowledge scope.
    shared = KnowledgeItem.owner_subject_id.is_(None) & KnowledgeItem.source_kind.in_(
        ("verified_source", "external_observation")
    )
    scope = shared if subject_id is None else or_(shared, KnowledgeItem.owner_subject_id == subject_id)
    return (
        scope & KnowledgeItem.visibility.in_(("public", "subject_private"))
        & ~open_purge_exists(KnowledgeItem.owner_subject_id)
    )


def current_knowledge(now: datetime) -> ColumnElement[bool]:
    return or_(KnowledgeItem.valid_until.is_(None), KnowledgeItem.valid_until > now)


def procedure_source_available(
    subject_id: str | SQLColumnExpression[str], now: datetime,
) -> ColumnElement[bool]:
    return (
        knowledge_visible_to(subject_id) & current_knowledge(now)
        & (KnowledgeItem.source_kind == "verified_source") & KnowledgeItem.source_url.is_not(None)
    )


@dataclass(frozen=True)
class KnowledgeView:
    item_id: str
    topic: str
    claim: str
    source_url: str | None
    source_kind: str
    observed_at: datetime
    valid_until: datetime | None
    confidence: float
    expired: bool


def _query_terms(query: str, *, min_len: int = 2) -> list[str]:
    """把自然语言查询切成用于检索的词（确定性，不调模型）。"""
    import re

    query = (query or "").strip()
    if not query:
        return []
    terms: list[str] = []
    # 英文/数字整词
    terms.extend(w.lower() for w in re.findall(r"[A-Za-z0-9_]{2,}", query))
    # 中文 2-gram（"今天天气怎么样" → 今天/天天/天气/气怎/怎么/么样）
    for run in re.findall(r"[\u4e00-\u9fff]+", query):
        if len(run) <= min_len:
            terms.append(run)
        else:
            terms.extend(run[i : i + min_len] for i in range(len(run) - min_len + 1))
    # 去重保序
    seen: set[str] = set()
    out = []
    for t in terms:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


class KnowledgeService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        topic: str,
        claim: str,
        source_url: str | None,
        source_kind: str = "verified_source",
        confidence: float = 0.6,
        ttl_hours: int = DEFAULT_TTL_HOURS,
        owner_subject_id: str | None = None,
        visibility: str = "subject_private",
    ) -> KnowledgeView:
        if source_kind not in {"verified_source", "external_observation", "user_statement"}:
            raise ValueError("知识来源必须是已核验来源、外部观察或用户陈述")
        if source_url:
            try:
                parsed = urlsplit(source_url)
                valid_url = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
            except ValueError:
                valid_url = False
            if not valid_url or len(source_url) > 1000:
                raise ValueError("知识来源 URL 必须是有效的 HTTP 或 HTTPS 地址")
        if not source_url and source_kind == "verified_source":
            raise ValueError("verified_source 必须提供来源 URL")
        now = datetime.now(UTC)
        row = KnowledgeItem(
            id=new_id("kn"),
            topic=topic.strip()[:200],
            claim=claim.strip()[:4000],
            source_url=source_url,
            source_kind=source_kind,
            observed_at=now,
            valid_until=now + timedelta(hours=max(1, ttl_hours)),
            confidence=max(0.0, min(1.0, confidence)),
            owner_subject_id=owner_subject_id,
            visibility=visibility,
        )
        self._session.add(row)
        await self._session.flush()
        return self._view(row, now)

    async def lookup(self, topic: str, *, limit: int = 5) -> list[KnowledgeView]:
        """按主题查未过期的知识。过期的排除，避免把旧信息当现状。"""
        now = datetime.now(UTC)
        rows = list(
            (
                await self._session.execute(
                    select(KnowledgeItem)
                    .where(
                        KnowledgeItem.topic.icontains(topic, autoescape=True),
                        knowledge_visible_to(None), current_knowledge(now),
                    )
                    .order_by(KnowledgeItem.observed_at.desc())
                    .limit(limit * 3)
                )
            ).scalars()
        )
        out = [self._view(r, now) for r in rows if not self._expired(r, now)]
        out.sort(key=lambda v: (-v.confidence, v.observed_at))
        return out[:limit]

    async def search(
        self, query: str, *, limit: int = 3, subject_id: str | None = None
    ) -> list[KnowledgeView]:
        """按查询词检索知识（供上下文装配）。

        与 ``lookup`` 的区别：``lookup`` 把整个字符串当 topic 子串，
        用户说"今天天气怎么样"时匹配不到 topic="天气"。
        这里把查询切成词再**任意命中**，更适合自然语言输入。

        切词是确定性的（不调模型）：中文取 2-gram，英文/数字取整词。
        宁可多召回几条再按置信度截断，也不要因为一个词没对上就整条漏掉。
        """
        terms = _query_terms(query)
        if not terms:
            return []
        now = datetime.now(UTC)

        conditions = [
            or_(
                KnowledgeItem.topic.icontains(t, autoescape=True),
                KnowledgeItem.claim.icontains(t, autoescape=True),
            )
            for t in terms[:8]
        ]
        stmt = select(KnowledgeItem).where(
            or_(*conditions), current_knowledge(now), knowledge_visible_to(subject_id),
        )
        rows = list(
            (
                await self._session.execute(
                    stmt.order_by(KnowledgeItem.observed_at.desc()).limit(limit * 5)
                )
            ).scalars()
        )
        out = [self._view(r, now) for r in rows]
        out.sort(key=lambda v: (-v.confidence, v.observed_at))
        return out[:limit]

    async def search_for_audience(
        self, query: str, *, audience: AudienceSnapshot, limit: int = 3
    ) -> list[KnowledgeView]:
        """用同一份受众范围检索首轮资料和同轮补查结果。"""
        if not audience.allows_long_term_memory():
            return []
        return await self.search(
            query, limit=limit, subject_id=None if audience.is_group else audience.subject_id
        )

    async def procedure_source(self, *, item_id: str, subject_id: str) -> KnowledgeView | None:
        now = datetime.now(UTC)
        row = await self._session.scalar(select(KnowledgeItem).where(
            KnowledgeItem.id == item_id, procedure_source_available(subject_id, now),
        ).with_for_update())
        return self._view(row, now) if row is not None else None

    async def list_all(self, limit: int = 50, *, subject_id: str | None = None) -> list[KnowledgeView]:
        now = datetime.now(UTC)
        rows = list(
            (
                await self._session.execute(
                    select(KnowledgeItem)
                    .where(knowledge_visible_to(subject_id))
                    .order_by(KnowledgeItem.observed_at.desc())
                    .limit(limit)
                )
            ).scalars()
        )
        return [self._view(r, now) for r in rows]

    @staticmethod
    def _expired(row: KnowledgeItem, now: datetime) -> bool:
        if row.valid_until is None:
            return False
        valid = row.valid_until
        if valid.tzinfo is None:
            valid = valid.replace(tzinfo=UTC)
        return valid <= now

    def _view(self, row: KnowledgeItem, now: datetime) -> KnowledgeView:
        observed = row.observed_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        valid = row.valid_until
        if valid is not None and valid.tzinfo is None:
            valid = valid.replace(tzinfo=UTC)
        return KnowledgeView(
            item_id=row.id,
            topic=row.topic,
            claim=row.claim,
            source_url=row.source_url,
            source_kind=row.source_kind,
            observed_at=observed,
            valid_until=valid,
            confidence=float(row.confidence),
            expired=self._expired(row, now),
        )
