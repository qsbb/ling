"""World 域：模拟地点、物品与虚拟世界变量。

唯一写入方。硬规则（设计文档 §6.3）：
- 这是**模拟世界**，不是现实地理位置；现实观察只能以有时效的 Perception 信号进入。
- 无食材不能说做完饭；虚拟变量不是生理指标。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import WorldPlace, utcnow
from ling.infra.security import new_id

DEFAULT_PLACES: list[dict[str, str | list[str]]] = [
    {
        "name": "小房间",
        "description": "ling 自己的房间，有书桌、台灯和一扇朝东的窗。",
        "items": ["书桌", "台灯", "窗", "杯子"],
    },
    {
        "name": "厨房",
        "description": "很小但够用的厨房。",
        "items": ["锅", "水壶", "砧板"],
    },
]


@dataclass(frozen=True)
class PlaceView:
    place_id: str
    name: str
    description: str | None
    items: list[str] = field(default_factory=list)


class WorldService:
    def __init__(self, session: AsyncSession, *, actor_id: str = "actor_ling") -> None:
        self._session = session
        self._actor_id = actor_id

    async def ensure_defaults(self) -> list[PlaceView]:
        existing = await self.list_places()
        if existing:
            return existing
        for spec in DEFAULT_PLACES:
            self._session.add(
                WorldPlace(
                    id=new_id("place"),
                    actor_id=self._actor_id,
                    name=str(spec["name"]),
                    description=str(spec["description"]),
                    items=list(spec["items"]) if isinstance(spec["items"], list) else [],
                )
            )
        await self._session.flush()
        return await self.list_places()

    async def list_places(self) -> list[PlaceView]:
        rows = list(
            (
                await self._session.execute(
                    select(WorldPlace).where(WorldPlace.actor_id == self._actor_id)
                )
            ).scalars()
        )
        return [self._view(r) for r in rows]

    async def get_place(self, place_id: str) -> PlaceView | None:
        row = await self._session.get(WorldPlace, place_id)
        if row is None or row.actor_id != self._actor_id:
            return None
        return self._view(row)

    async def add_item(self, place_id: str, item: str) -> PlaceView | None:
        row = await self._session.get(WorldPlace, place_id)
        if row is None or row.actor_id != self._actor_id:
            return None
        items = list(row.items or [])
        if item not in items:
            items.append(item)
        row.items = items
        row.updated_at = utcnow()
        await self._session.flush()
        return self._view(row)

    async def remove_item(self, place_id: str, item: str) -> PlaceView | None:
        row = await self._session.get(WorldPlace, place_id)
        if row is None or row.actor_id != self._actor_id:
            return None
        row.items = [i for i in (row.items or []) if i != item]
        row.updated_at = utcnow()
        await self._session.flush()
        return self._view(row)

    async def has_items(self, place_id: str, required: list[str]) -> bool:
        """活动前置条件：缺物品就不能声称完成。"""
        place = await self.get_place(place_id)
        if place is None:
            return False
        return all(item in place.items for item in required)

    @staticmethod
    def _view(row: WorldPlace) -> PlaceView:
        return PlaceView(
            place_id=row.id,
            name=row.name,
            description=row.description,
            items=list(row.items or []),
        )
