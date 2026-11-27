"""实体归并与关联。

参照 MaiBot A-Memorix 的图谱关系召回、Mem0 的 entity linking。

**为什么需要**：现在记忆只记录"用户有什么属性"。用户问
「我妈提过的那个人后来怎样」时，只能指望文本碰巧命中——
没有实体概念就无法沿关系定位。

**别名归并是核心**：没有它，"妈 / 母亲 / 老妈"会被当成三个不同对象，
换个叫法仍然串不起来。归并用确定性规则（别名表 + 归一化），不调模型。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, get_args
from unicodedata import normalize

from sqlalchemy import String, any_, delete, literal, select
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import EntityRow, MemoryEntityRow, MemoryEpisode, MemoryRow, Window
from ling.infra.security import new_id

if TYPE_CHECKING:
    from ling.domains.memory.extract import ObjectMention

# 常见别名归并：同一实体不同叫法映射到一个规范名。
# 只收高频、无歧义的；宁可少归并，也不要把两个不同的人合成一个。
KINSHIP_ALIASES: dict[str, str] = {
    "母亲": "妈", "老妈": "妈", "妈妈": "妈", "娘": "妈",
    "父亲": "爸", "老爸": "爸", "爸爸": "爸", "爹": "爸",
    "祖母": "奶奶", "外祖母": "姥姥", "祖父": "爷爷", "外祖父": "姥爷",
    "妻子": "老婆", "丈夫": "老公", "儿子": "孩子", "女儿": "孩子",
    "兄长": "哥", "弟弟": "弟", "姐姐": "姐", "妹妹": "妹",
}

ObjectKind = Literal["person", "group", "place", "item", "work", "pet", "topic", "project", "org"]
RecordType = Literal["memory", "experience"]
OBJECT_KINDS = frozenset(get_args(ObjectKind))
ENTITY_KINDS = OBJECT_KINDS | {"other"}


@dataclass
class EntityRef:
    name: str
    kind: str = "other"
    aliases: list[str] = field(default_factory=list)


def canonical_name(name: str) -> str:
    """把别名归一到规范名（小写、去空白）。"""
    key = (name or "").strip().lower()
    return KINSHIP_ALIASES.get(key, key)


class EntityService:
    """实体的唯一写入方。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        *,
        subject_id: str,
        name: str,
        kind: str = "other",
        aliases: list[str] | None = None,
    ) -> EntityRow:
        """写入或更新一个实体（按规范名去重，别名累积）。"""
        canonical = canonical_name(name)
        if not canonical:
            raise ValueError("实体名不能为空")
        if kind not in ENTITY_KINDS:
            kind = "other"
        row = await self._find(subject_id, canonical)
        merged = {
            canonical_name(a) for a in (aliases or []) if canonical_name(a)
        } | {canonical}
        if row is None:
            row = EntityRow(
                id=new_id("ent"),
                subject_id=subject_id,
                name=canonical,
                kind=kind,
                aliases=sorted(merged),
            )
            self._session.add(row)
        else:
            row.kind = kind if kind != "other" else row.kind
            row.aliases = sorted(set(row.aliases or []) | merged)
        await self._session.flush()
        return row

    async def upsert_group(
        self,
        *,
        subject_id: str,
        window_id: str,
        group_id: str,
        display_name: str = "",
        aliases: list[str] | None = None,
    ) -> EntityRow:
        """Use the resolved group window as identity, including its connector instance."""
        source_key = f"group:{window_id}"
        target = await self._session.scalar(
            select(EntityRow).where(EntityRow.subject_id == subject_id, EntityRow.source_key == source_key)
        )
        if target is None:
            legacy = (
                select(MemoryRow.source_window_id)
                .join(MemoryEntityRow, MemoryEntityRow.memory_id == MemoryRow.id)
                .where(
                    MemoryEntityRow.entity_id == EntityRow.id,
                    MemoryEntityRow.record_type == "memory",
                    MemoryRow.subject_id == subject_id,
                )
            )
            # Reuse previous imports when their links identify one exact group.
            # The same QQ number on a different instance is a separate object.
            target = await self._session.scalar(select(EntityRow).where(
                EntityRow.subject_id == subject_id,
                EntityRow.source_key.is_(None),
                EntityRow.kind == "group",
                legacy.where(MemoryRow.source_window_id == window_id).exists(),
                ~legacy.where(MemoryRow.source_window_id.is_distinct_from(window_id)).exists(),
            ).order_by(EntityRow.id).limit(1))
        label = (display_name or "").strip()
        name = (f"{label[:44]} · {group_id}" if label else f"群 {group_id}")[:64]
        merged = {
            canonical_name(value)
            for value in [label, group_id, *(aliases or [])]
            if canonical_name(value)
        }
        if target is None:
            target = await self._upsert_scoped(
                subject_id=subject_id, source_key=source_key, name=name, kind="group",
            )
        if label:
            target.name = name
        target.aliases = sorted(set(target.aliases or []) | merged)
        await self._session.flush()
        return target

    async def evidence_label(
        self, *, subject_id: str, memory_id: str, record_type: RecordType, name: str, kind: str,
    ) -> EntityRow:
        """Keep a name anchored to this record until identity is explicitly linked."""
        key = normalize("NFKC", name).casefold()
        fingerprint = hashlib.sha256(f"{record_type}:{memory_id}:{kind}:{key}".encode()).hexdigest()
        return await self._upsert_scoped(
            subject_id=subject_id, source_key=f"label:{fingerprint}", name=name, kind=kind,
        )

    async def index_episode(
        self, episode: MemoryEpisode, *, source_text: str, window: Window, mentions: list[ObjectMention],
    ) -> None:
        if episode.archived_at is not None or episode.source_message_id is None:
            return
        for mention in dict.fromkeys(mentions):
            if mention.name not in source_text or mention.name not in episode.summary:
                continue
            entity = await self.evidence_label(
                subject_id=episode.subject_id, memory_id=episode.id, record_type="experience",
                name=mention.name, kind=mention.kind,
            )
            await self.link(
                memory_id=episode.id, entity_id=entity.id, role="mentioned", record_type="experience",
            )
        ref = window.connector_window_ref or ""
        parts = ref.rsplit(":", 2)
        if episode.source_visibility == "group_scope" and len(parts) == 3 and parts[1] == "group":
            entity = await self.upsert_group(
                subject_id=episode.subject_id, window_id=window.id,
                group_id=parts[2], display_name=window.title if window.title != ref else "",
            )
            await self.link(
                memory_id=episode.id, entity_id=entity.id, role="with", record_type="experience",
            )

    async def rehome_memory_objects(self, *, subject_id: str, memory_ids: list[str]) -> None:
        """Carry the moved records' evidence without inheriting the old owner's aliases."""
        if not memory_ids:
            return
        selected = MemoryEntityRow.memory_id == any_(literal(memory_ids, type_=ARRAY(String)))
        rows = (await self._session.execute(
            select(MemoryEntityRow, EntityRow, MemoryRow.source_window_id, MemoryRow.import_source)
            .join(EntityRow, EntityRow.id == MemoryEntityRow.entity_id)
            .join(MemoryRow, MemoryRow.id == MemoryEntityRow.memory_id)
            .where(selected, MemoryEntityRow.record_type == "memory", EntityRow.subject_id != subject_id)
            .order_by(MemoryRow.created_at.desc(), MemoryRow.id)
        )).all()
        targets: dict[str, str] = {}
        links = []
        old_entities = set()
        for link, entity, window_id, source in rows:
            key = entity.source_key
            if key is None:
                continue
            group_id = (source or {}).get("group_id")
            if key.startswith("group:"):
                if not group_id or not window_id or key != f"group:{window_id}":
                    continue
                if key not in targets:
                    group = await self.upsert_group(
                        subject_id=subject_id, window_id=window_id, group_id=group_id,
                        display_name=source.get("group_name") or "",
                    )
                    targets[key] = group.id
            elif key.startswith(("archive_speaker:", "label:")):
                if key not in targets:
                    scoped = await self._upsert_scoped(
                        subject_id=subject_id, source_key=key, kind=entity.kind, name=entity.name,
                    )
                    targets[key] = scoped.id
            else:
                continue
            links.append({
                "memory_id": link.memory_id, "entity_id": targets[key], "role": link.role,
                "source_reference": link.source_reference, "source_label": link.source_label,
            })
            old_entities.add(link.entity_id)
        if links:
            await self._session.execute(insert(MemoryEntityRow).on_conflict_do_nothing(), links)
            await self._session.execute(delete(MemoryEntityRow).where(
                selected, MemoryEntityRow.record_type == "memory",
                MemoryEntityRow.entity_id == any_(literal(list(old_entities), type_=ARRAY(String))),
            ))

    async def _upsert_scoped(
        self, *, subject_id: str, source_key: str, name: str, kind: str,
    ) -> EntityRow:
        await self._session.execute(
            insert(EntityRow).values(
                id=new_id("ent"), subject_id=subject_id, source_key=source_key, name=name, kind=kind,
            ).on_conflict_do_nothing(constraint="uq_entities_subject_source")
        )
        return (await self._session.scalars(select(EntityRow).where(
            EntityRow.subject_id == subject_id, EntityRow.source_key == source_key,
        ))).one()

    async def _find(self, subject_id: str, canonical: str) -> EntityRow | None:
        """按规范名或别名找已有实体。"""
        rows = list(
            (
                await self._session.execute(
                    select(EntityRow).where(EntityRow.subject_id == subject_id, EntityRow.source_key.is_(None))
                )
            ).scalars()
        )
        for row in rows:
            if canonical_name(row.name) == canonical:
                return row
            if canonical in {canonical_name(a) for a in (row.aliases or [])}:
                return row
        return None

    async def resolve(self, *, subject_id: str, text: str) -> list[EntityRow]:
        """从一段文本里找出已知实体（供检索时按实体扩展）。

        用确定性匹配：规范名或任一别名出现在文本里即命中。
        不做 NER——对个人规模，别名表足够且可解释。
        """
        lowered = (text or "").lower()
        if not lowered:
            return []
        rows = list(
            (
                await self._session.execute(
                    select(EntityRow).where(EntityRow.subject_id == subject_id)
                )
            ).scalars()
        )
        out: list[EntityRow] = []
        for row in rows:
            names = {canonical_name(row.name)} | {
                canonical_name(a) for a in (row.aliases or [])
            }
            if any(n and n in lowered for n in names):
                out.append(row)
        return out

    async def link(
        self, *, memory_id: str, entity_id: str, role: str = "about", record_type: RecordType = "memory",
    ) -> bool:
        new_link = await self._session.scalar(
            insert(MemoryEntityRow).values(
                memory_id=memory_id, entity_id=entity_id, record_type=record_type, role=role,
            ).on_conflict_do_nothing().returning(MemoryEntityRow.entity_id)
        )
        return new_link is not None

    async def related_memory_ids(
        self, entity_ids: list[str], *, record_type: RecordType = "memory",
    ) -> set[str]:
        if not entity_ids:
            return set()
        rows = (
            await self._session.execute(
                select(MemoryEntityRow.memory_id).where(
                    MemoryEntityRow.entity_id.in_(entity_ids),
                    MemoryEntityRow.record_type == record_type,
                )
            )
        ).scalars()
        return set(rows)


__all__ = [
    "ENTITY_KINDS",
    "KINSHIP_ALIASES",
    "OBJECT_KINDS",
    "EntityRef",
    "EntityService",
    "canonical_name",
]
