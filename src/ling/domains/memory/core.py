"""核心记忆服务：每轮常驻的少量高价值事实。

为什么不放进 ``memories`` 走检索：检索是概率性的。边界、禁忌、称呼、
重要约定这类内容一旦"没命中"，角色就显得忘了你是谁或忘了你划过的线——
后果比漏记一件小事严重得多。参照 Letta 的 core memory blocks 与
「我会牢牢记住你」的"核心记忆每轮常驻，不依赖 RAG 命中"。

容量约束是刻意的：核心记忆进的是**每一轮**的上下文，条数一多就等于
吃掉预算、并且把长尾记忆挤掉。所以这里限制条数并给出优先级。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.memory.purge import has_open_purge
from ling.domains.memory.service import MemoryService, active_source
from ling.infra.models import CoreMemoryRow, MemoryRow, Subject, utcnow
from ling.infra.security import new_id
from ling.ports.config import MemoryPolicy

# 每个 subject 的核心记忆上限。超出时按 priority 取舍，不静默丢弃——
# 调用方会收到提示，要求先清理再新增。
MAX_PER_SUBJECT = 20
# 注入时的字符预算（约等于 token），避免核心块吃掉太多上下文
DEFAULT_CHAR_BUDGET = 800


@dataclass
class CoreMemoryView:
    core_id: str
    label: str
    text: str
    priority: int
    origin_kind: str
    scope: str
    active: bool
    source_memory_id: str | None = None


def _pin_source(now: datetime):
    return and_(
        MemoryRow.kind == "assertion",
        MemoryRow.origin == "user_statement",
        MemoryRow.visibility == "subject_private",
        MemoryRow.source_visibility == "subject_private",
        MemoryRow.fact_phase == "current",
        MemoryRow.retracted.is_(False),
        MemoryRow.archived_at.is_(None),
        or_(MemoryRow.valid_from.is_(None), MemoryRow.valid_from <= now),
        or_(MemoryRow.valid_until.is_(None), MemoryRow.valid_until > now),
        active_source(MemoryRow),
    )


class CoreMemoryService:
    """核心记忆的唯一写入方。"""

    def __init__(self, session: AsyncSession, *, policy: MemoryPolicy | None = None) -> None:
        self._session = session
        self._retention = MemoryService(session, policy=policy)

    async def upsert(
        self,
        *,
        subject_id: str,
        label: str,
        text: str,
        priority: int = 100,
        origin_kind: str = "manual",
        scope: str = "private",
    ) -> CoreMemoryView:
        """写入或更新一条核心记忆（同 label 覆盖）。

        同 label 覆盖而不是新增：避免"关于边界记了十条互相矛盾"。
        这正是 某同类实现 强调的"单值属性改口接修订关系"的简化版——
        核心记忆数量少、由人管理，直接覆盖并能看到最新值即可。
        """
        label = label.strip()[:64]
        text = text.strip()
        if not label or not text:
            raise ValueError("核心记忆的 label 与 text 都不能为空")
        if scope not in {"private", "group", "global"}:
            raise ValueError(f"未知作用域: {scope}")
        await self._session.execute(select(Subject.id).where(Subject.id == subject_id).with_for_update())

        row = (
            await self._session.execute(
                select(CoreMemoryRow).where(
                    CoreMemoryRow.subject_id == subject_id,
                    CoreMemoryRow.label == label,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            count = len(await self.list_active(subject_id))
            if count >= MAX_PER_SUBJECT:
                raise ValueError(
                    f"核心记忆已达上限 {MAX_PER_SUBJECT} 条；"
                    "核心记忆每轮注入，过多会挤占上下文，请先删除不用的"
                )
            row = CoreMemoryRow(
                id=new_id("core"),
                subject_id=subject_id,
                label=label,
                text=text[:2000],
                priority=priority,
                origin_kind=origin_kind,
                scope=scope,
                active=True,
            )
            self._session.add(row)
        else:
            if row.source_memory_id is not None:
                raise ValueError("请在原记忆中纠正这条固定记忆")
            row.text = text[:2000]
            row.priority = priority
            row.scope = scope
            row.active = True
            row.updated_at = utcnow()
        await self._session.flush()
        return self._view(row)

    async def set_pin(self, subject_id: str, memory_id: str, pinned: bool) -> bool:
        await self._session.execute(select(Subject.id).where(Subject.id == subject_id).with_for_update())
        if await has_open_purge(self._session, subject_id=subject_id):
            return False
        pin = await self._session.scalar(select(CoreMemoryRow).where(
            CoreMemoryRow.subject_id == subject_id, CoreMemoryRow.source_memory_id == memory_id,
        ))
        if not pinned:
            if pin is not None:
                await self._session.delete(pin)
                return True
            return False
        now = utcnow()
        source = await self._session.scalar(select(MemoryRow).where(
            MemoryRow.id == memory_id, MemoryRow.subject_id == subject_id, _pin_source(now),
            self._retention._retained_memory(now),
        ).with_for_update())
        if source is None:
            return False
        if (pin is None or not pin.active) and len(await self.list_active(subject_id)) >= MAX_PER_SUBJECT:
            raise ValueError(f"核心记忆已达上限 {MAX_PER_SUBJECT} 条")
        if pin is None:
            core_id = new_id("core")
            self._session.add(CoreMemoryRow(
                id=core_id, subject_id=subject_id, label=core_id, text="",
                priority=100, origin_kind="derived", scope="private", active=True,
                source_memory_id=memory_id,
            ))
        else:
            pin.active = True
        await self._session.flush()
        return True

    async def list_active(
        self, subject_id: str, *, limit: int | None = None
    ) -> list[CoreMemoryView]:
        """列出 active 的核心记忆。

        ``limit`` 供 API 端点传 ``MAX_PER_SUBJECT``，让响应回显的硬上限与
        实际查询一致；不传时保持旧行为（内部容量检查/渲染需要看全量）。

        清理进行中返回空：核心记忆是**每轮常驻**的注入块，比检索更容易泄漏。
        用户在后台点了"删除"之后，下一轮回复里还会照常出现"她怕黑"——
        那比检索命中更难解释。
        """
        if await has_open_purge(self._session, subject_id=subject_id):
            return []
        now = utcnow()
        stmt = (
            select(CoreMemoryRow, MemoryRow)
            .outerjoin(MemoryRow, MemoryRow.id == CoreMemoryRow.source_memory_id)
            .where(
                CoreMemoryRow.subject_id == subject_id,
                CoreMemoryRow.active.is_(True),
                or_(
                    CoreMemoryRow.source_memory_id.is_(None),
                    and_(
                        MemoryRow.subject_id == subject_id, _pin_source(now), self._retention._retained_memory(now),
                    ),
                ),
            )
            .order_by(CoreMemoryRow.priority, CoreMemoryRow.label)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await self._session.execute(stmt)).all()
        return [self._view(row, source) for row, source in rows]

    async def render(
        self, *, subject_id: str, is_group: bool = False, budget: int = DEFAULT_CHAR_BUDGET
    ) -> str:
        """渲染成可注入的文本块。

        群聊里 ``scope=private`` 的核心记忆**不注入**：群里不该出现
        "用户怕黑""用户在吃抗抑郁药"这类私聊才该说的内容。
        """
        items = [
            v
            for v in await self.list_active(subject_id)
            if (not is_group) or v.scope in {"group", "global"}
        ]
        if not items:
            return ""
        lines: list[str] = []
        used = 0
        for item in items:
            line = f"- [{item.label}] {item.text}"
            if used + len(line) > budget:
                # 预算不够时按 priority 截断（列表已按 priority 排序）
                break
            lines.append(line)
            used += len(line)
        if not lines:
            return ""
        return (
            "和对方相处时记住这些：\n"
            + "\n".join(lines)
        )

    async def deactivate(self, core_id: str, subject_id: str) -> bool:
        await self._session.execute(select(Subject.id).where(Subject.id == subject_id).with_for_update())
        row = await self._session.get(CoreMemoryRow, core_id)
        if row is None or row.subject_id != subject_id:
            return False
        row.active = False
        row.updated_at = utcnow()
        await self._session.flush()
        return True

    @staticmethod
    def _view(row: CoreMemoryRow, source: MemoryRow | None = None) -> CoreMemoryView:
        return CoreMemoryView(
            core_id=row.id,
            label="固定记忆" if source is not None else row.label,
            text=source.text if source is not None else row.text,
            priority=int(row.priority or 100),
            origin_kind=row.origin_kind,
            scope=row.scope,
            active=bool(row.active),
            source_memory_id=row.source_memory_id,
        )


__all__ = ["DEFAULT_CHAR_BUDGET", "MAX_PER_SUBJECT", "CoreMemoryService", "CoreMemoryView"]
