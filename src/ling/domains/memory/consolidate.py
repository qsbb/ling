"""睡眠期记忆整理：一晚一次，**确定性、零模型**。

## 用户原话

「睡眠的时候应该要对记忆进行压缩或者什么样的，你可以让 Codex 设计一下」

设计见 `docs/SLEEP-MEMORY-CONSOLIDATION.md`。本模块是它的第一版实现：
**只做确定性整理**，不调模型、不生成摘要文本。

## 为什么第一版刻意不用模型

压缩的产物会**取代**原始记忆参与召回。用模型写摘要意味着"模型编的一句话
变成她记得的事"，而记忆是这个项目里最不能编的东西。

确定性整理能做的事已经不少，而且每一件都可解释：
- 同一晚把**低价值、长期没用**的记忆归档（`compact_low_value` 已有的能力）；
- 按**全部受众边界**分组（主体/可见范围/来源窗口/origin），保证合并不会
  把群里的内容洗进私聊、不会把推断洗成用户的陈述。

真正需要模型的"把这一天浓缩成一句话"留到以后——那时必须新增一个明确标注
"非事实、不可直接断言"的存储，而不是复用记忆行。

## 为什么挂在睡眠窗口而不是每日定时

"整理记忆"在拟人化语境里属于**她睡着之后**的事。挂在睡眠窗口有两个好处：
一是与她的生活节奏一致（用户能理解"她昨晚整理过"），二是自然错开白天的
写入高峰。但这只是**预计**睡眠窗口——`life.schedule` 算出来的，不宣称她
真的睡着（设计 §3 第 13 条），所以 `reason` 里写的是"预计入睡"。

## 幂等

`sleep_date` 唯一。一晚最多一行，重复投递只会读到已有记录然后什么都不做。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import MemoryConsolidationRow, utcnow
from ling.infra.security import new_id

logger = logging.getLogger("ling.domains.memory.consolidate")

#: 整理结果状态。`skipped` 不是失败——见模块注释。
STATE_OK = "ok"
STATE_SKIPPED = "skipped"
STATE_FAILED = "failed"


@dataclass(frozen=True)
class ConsolidationOutcome:
    """一晚整理的结果。给调度/日志/记忆页读。"""

    state: str
    reason: str
    groups_merged: int = 0
    rows_archived: int = 0
    #: 是不是这一晚本来就做过了（幂等命中，不代表失败）
    already_done: bool = False

    @property
    def did_something(self) -> bool:
        return self.groups_merged > 0 or self.rows_archived > 0


async def consolidate_night(
    session: AsyncSession,
    *,
    actor_id: str,
    subject_id: str,
    sleep_date: date,
    now: datetime | None = None,
) -> ConsolidationOutcome:
    """整理某一晚的记忆。**幂等**：同一 (actor, subject, sleep_date) 只做一次。

    调用方负责提交事务。

    顺序上先查记录再动手：整理会归档记忆，重复执行会把已经归档的再算一遍，
    虽然归档本身是幂等的，但 `rows_archived` 的计数会失真——而那个数字是
    用户唯一能看到的"昨晚发生了什么"。
    """
    from ling.domains.memory.service import MemoryService
    from ling.settings.service import published_memory_policy

    moment = now or utcnow()

    existing = (
        await session.execute(
            select(MemoryConsolidationRow).where(
                MemoryConsolidationRow.actor_id == actor_id,
                MemoryConsolidationRow.subject_id == subject_id,
                MemoryConsolidationRow.sleep_date == sleep_date,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # 这一晚已经整理过（含 skipped/failed）。**不重试**：
        # 失败重试会把"整理到一半"的状态反复覆盖，而归档是幂等的，
        # 重跑拿不到更多东西，只会让计数来回跳。
        return ConsolidationOutcome(
            state=str(existing.state),
            reason=str(existing.reason or ""),
            groups_merged=int(existing.groups_merged or 0),
            rows_archived=int(existing.rows_archived or 0),
            already_done=True,
        )

    row = MemoryConsolidationRow(
        id=new_id("cons"),
        actor_id=actor_id,
        subject_id=subject_id,
        sleep_date=sleep_date,
        state=STATE_SKIPPED,
        reason="",
        created_at=moment,
    )
    session.add(row)
    await session.flush()

    try:
        service = MemoryService(
            session, policy=await published_memory_policy(session)
        )
        policy = service.policy
        # **按人整理**：只动本主体的记忆。分组键里虽有 subject_id（不会跨
        # 主体合并），但那不等于"只处理这一个"——不加 scoped 过滤，给 A
        # 整理的一晚会把 B 的记忆也归档（实测复现）。
        result = await service.compact_low_value(now=moment, subject_id=subject_id)
        groups = int(result.get("groups", 0) or 0)
        archived = int(result.get("archived", 0) or 0)
    except Exception as exc:  # noqa: BLE001 - 整理失败不能拖垮睡眠调度
        # 记录失败态：用户看记忆页时要知道"昨晚整理没做完"，
        # 而不是以为一切正常。
        row.state = STATE_FAILED
        row.reason = f"整理失败（{type(exc).__name__}）"[:200]
        row.finished_at = moment
        await session.flush()
        logger.warning("睡眠期记忆整理失败 subject=%s：%s", subject_id, type(exc).__name__)
        return ConsolidationOutcome(state=STATE_FAILED, reason=row.reason)

    row.finished_at = moment
    row.groups_merged = groups
    row.rows_archived = archived

    if groups == 0 and archived == 0:
        # 最常见的情况：这一晚没有够老、够低价值的同槽位记忆可并。
        # 记 skipped + 人话原因，不记 ok——否则"整理了几条"这个数字失去意义。
        row.state = STATE_SKIPPED
        row.reason = (
            f"预计入睡（{sleep_date.isoformat()}）后整理过：没有达到合并条件的记忆"
        )
    else:
        row.state = STATE_OK
        row.reason = (
            f"预计入睡（{sleep_date.isoformat()}）后整理："
            f"合并 {groups} 组、归档 {archived} 条"
        )
    await session.flush()

    logger.info(
        "睡眠期记忆整理完成 subject=%s sleep_date=%s state=%s 合并=%d 归档=%d "
        "（策略 half_life=%s 天）",
        subject_id,
        sleep_date,
        row.state,
        groups,
        archived,
        policy.recency_half_life_days,
    )
    return ConsolidationOutcome(
        state=row.state, reason=row.reason, groups_merged=groups, rows_archived=archived
    )


async def recent_consolidations(
    session: AsyncSession, *, actor_id: str, subject_id: str, limit: int = 5
) -> list[MemoryConsolidationRow]:
    """最近几晚的整理记录（记忆页展示"她整理过什么"）。"""
    rows = (
        await session.execute(
            select(MemoryConsolidationRow)
            .where(
                MemoryConsolidationRow.actor_id == actor_id,
                MemoryConsolidationRow.subject_id == subject_id,
            )
            .order_by(MemoryConsolidationRow.sleep_date.desc())
            .limit(limit)
        )
    ).scalars()
    return list(rows)


__all__ = [
    "STATE_FAILED",
    "STATE_OK",
    "STATE_SKIPPED",
    "ConsolidationOutcome",
    "consolidate_night",
    "recent_consolidations",
]
