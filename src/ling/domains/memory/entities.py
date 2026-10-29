"""实体归并与关联。

参照 MaiBot A-Memorix 的图谱关系召回、Mem0 的 entity linking。

**为什么需要**：现在记忆只记录"用户有什么属性"。用户问
「我妈提过的那个人后来怎样」时，只能指望文本碰巧命中——
没有实体概念就无法沿关系定位。

**别名归并是核心**：没有它，"妈 / 母亲 / 老妈"会被当成三个不同对象，
换个叫法仍然串不起来。归并用确定性规则（别名表 + 归一化），不调模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import EntityRow, MemoryEntityRow
from ling.infra.security import new_id

# 常见别名归并：同一实体不同叫法映射到一个规范名。
# 只收高频、无歧义的；宁可少归并，也不要把两个不同的人合成一个。
KINSHIP_ALIASES: dict[str, str] = {
    "母亲": "妈", "老妈": "妈", "妈妈": "妈", "娘": "妈",
    "父亲": "爸", "老爸": "爸", "爸爸": "爸", "爹": "爸",
    "祖母": "奶奶", "外祖母": "姥姥", "祖父": "爷爷", "外祖父": "姥爷",
    "妻子": "老婆", "丈夫": "老公", "儿子": "孩子", "女儿": "孩子",
    "兄长": "哥", "弟弟": "弟", "姐姐": "姐", "妹妹": "妹",
}

ENTITY_KINDS = {"person", "place", "project", "pet", "org", "other"}


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

    async def _find(self, subject_id: str, canonical: str) -> EntityRow | None:
        """按规范名或别名找已有实体。"""
        rows = list(
            (
                await self._session.execute(
                    select(EntityRow).where(EntityRow.subject_id == subject_id)
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
        self, *, memory_id: str, entity_id: str, role: str = "about"
    ) -> bool:
        existing = await self._session.get(MemoryEntityRow, (memory_id, entity_id))
        if existing is not None:
            return False
        self._session.add(
            MemoryEntityRow(memory_id=memory_id, entity_id=entity_id, role=role)
        )
        await self._session.flush()
        return True

    async def related_memory_ids(self, entity_ids: list[str]) -> set[str]:
        if not entity_ids:
            return set()
        rows = (
            await self._session.execute(
                select(MemoryEntityRow.memory_id).where(
                    MemoryEntityRow.entity_id.in_(entity_ids)
                )
            )
        ).scalars()
        return set(rows)


__all__ = [
    "ENTITY_KINDS",
    "KINSHIP_ALIASES",
    "EntityRef",
    "EntityService",
    "canonical_name",
]
