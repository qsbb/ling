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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import CoreMemoryRow, utcnow
from ling.infra.security import new_id

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


class CoreMemoryService:
    """核心记忆的唯一写入方。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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
            row.text = text[:2000]
            row.priority = priority
            row.scope = scope
            row.active = True
            row.updated_at = utcnow()
        await self._session.flush()
        return self._view(row)

    async def list_active(
        self, subject_id: str, *, limit: int | None = None
    ) -> list[CoreMemoryView]:
        """列出 active 的核心记忆。

        ``limit`` 供 API 端点传 ``MAX_PER_SUBJECT``，让响应回显的硬上限与
        实际查询一致；不传时保持旧行为（内部容量检查/渲染需要看全量）。
        """
        stmt = (
            select(CoreMemoryRow)
            .where(
                CoreMemoryRow.subject_id == subject_id,
                CoreMemoryRow.active.is_(True),
            )
            .order_by(CoreMemoryRow.priority, CoreMemoryRow.label)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await self._session.execute(stmt)).scalars()
        return [self._view(r) for r in rows]

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
            "你必须始终记住的关于对方的要点（每轮都在，不依赖检索）：\n"
            + "\n".join(lines)
        )

    async def deactivate(self, core_id: str, subject_id: str) -> bool:
        row = await self._session.get(CoreMemoryRow, core_id)
        if row is None or row.subject_id != subject_id:
            return False
        row.active = False
        row.updated_at = utcnow()
        await self._session.flush()
        return True

    @staticmethod
    def _view(row: CoreMemoryRow) -> CoreMemoryView:
        return CoreMemoryView(
            core_id=row.id,
            label=row.label,
            text=row.text,
            priority=int(row.priority or 100),
            origin_kind=row.origin_kind,
            scope=row.scope,
            active=bool(row.active),
        )


__all__ = ["DEFAULT_CHAR_BUDGET", "MAX_PER_SUBJECT", "CoreMemoryService", "CoreMemoryView"]
