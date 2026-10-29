"""交互负担：「回烦了」的量化与恢复。

## 它回答什么

用户需求：「群聊回消息回烦了也会选择不回」。

在这里它不是"心情"，是一个**可计算、会恢复的数值**：

```
local_load(t) = min(100, local_load(t₀) × 2^(-经过分钟数 / 45) + 本次成本)
```

按时间**惰性**衰减——读的时候用时间戳推算，不需要后台任务。

## 三条设计约束

1. **按窗口隔离**。`scope_key` 用窗口 id。群里的烦躁绝不能影响对私聊对象的态度，
   否则就是跨受众泄露。
2. **只管"要不要回、回多长"**。语气由人设决定——用户明确要求给模型高自由度，
   所以这里不产生任何"冷淡值""攻击性"之类的信号。
3. **紧急求助不受它影响**。这不是语气问题，是功能失败：把一条求救默默吞掉，
   和"语气不好"不是一个量级。

## 为什么不用 `actor_state.social_load`

那是**整体**负担、跨窗口累计。混用会让"被这个群烦到"污染"和那个人的关系"。
两者各司其职，同一事件不重复计。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import InteractionLoadRow, utcnow
from ling.infra.security import new_id

# 恢复半衰期（分钟）。试运行初值，**未经真实会话验证**。
RECOVERY_HALF_LIFE_MINUTES = 45.0

MIN_LOAD = 0.0
MAX_LOAD = 100.0

# 各类事件的成本。**全部是试运行初值**，需真实回放调整。
COST_SAW_MESSAGE = 1.0
COST_FINISHED_REPLY = 5.0
COST_INTERRUPTED = 2.0
COST_REPEATED_URGE = 6.0


@dataclass(frozen=True)
class LoadView:
    scope_key: str
    level: float

    @property
    def band(self) -> str:
        """三档分桶。给模型一个粗粒度信号，而不是裸数字。

        裸数字（"你的烦躁值是 63.4"）会让模型过度解读成情绪指令；
        分桶更像人自己体感的描述。
        """
        if self.level >= 70:
            return "high"
        if self.level >= 40:
            return "medium"
        return "low"

    def describe(self) -> str:
        """给提示词的一句话。空字符串表示不值得注入。"""
        if self.band == "high":
            return "你在这个对话里已经有些倦了，容易懒得接话"
        if self.band == "medium":
            return "这个对话已经持续了一阵，你比刚开始时更少主动展开"
        return ""


class InteractionLoadService:
    """交互负担的唯一写入方。"""

    def __init__(self, session: AsyncSession, *, actor_id: str) -> None:
        self._session = session
        self._actor_id = actor_id

    async def level(self, *, scope_key: str, now: datetime | None = None) -> LoadView:
        """读当前负担（含按时间推算的衰减）。没有记录就是 0。"""
        moment = now or utcnow()
        row = await self._row(scope_key)
        if row is None:
            return LoadView(scope_key=scope_key, level=0.0)
        return LoadView(scope_key=scope_key, level=_decayed(row, moment))

    async def add(
        self,
        *,
        scope_key: str,
        cost: float = COST_SAW_MESSAGE,
        now: datetime | None = None,
    ) -> LoadView:
        """累加一次成本。先结算到此刻，再加。"""
        moment = now or utcnow()
        row = await self._row(scope_key)
        if row is None:
            row = InteractionLoadRow(
                id=new_id("load"),
                actor_id=self._actor_id,
                scope_key=scope_key,
                local_load=min(MAX_LOAD, max(MIN_LOAD, cost)),
                settled_at=moment,
            )
            self._session.add(row)
            await self._session.flush()
            return LoadView(scope_key=scope_key, level=float(row.local_load))

        current = _decayed(row, moment)
        row.local_load = min(MAX_LOAD, max(MIN_LOAD, current + cost))
        row.settled_at = moment
        row.updated_at = moment
        await self._session.flush()
        return LoadView(scope_key=scope_key, level=float(row.local_load))

    async def _row(self, scope_key: str) -> InteractionLoadRow | None:
        return (
            await self._session.execute(
                select(InteractionLoadRow).where(
                    InteractionLoadRow.actor_id == self._actor_id,
                    InteractionLoadRow.scope_key == scope_key,
                )
            )
        ).scalar_one_or_none()


def _decayed(row: InteractionLoadRow, moment: datetime) -> float:
    """把存量按经过时间衰减到 ``moment``。

    ``settled_at`` 缺失或时间倒流（时钟回拨）时**不衰减**——
    宁可高估负担也不要在时间异常时凭空清零，那会让"回烦了"被一次时钟
    跳变抹掉。
    """
    settled = row.settled_at
    if settled is None:
        return float(row.local_load or 0.0)
    if settled.tzinfo is None:
        from datetime import UTC

        settled = settled.replace(tzinfo=UTC)
    elapsed_minutes = (moment - settled).total_seconds() / 60.0
    if elapsed_minutes <= 0:
        return float(row.local_load or 0.0)
    factor = 2 ** (-elapsed_minutes / RECOVERY_HALF_LIFE_MINUTES)
    return float(row.local_load or 0.0) * factor


__all__ = [
    "COST_FINISHED_REPLY",
    "COST_INTERRUPTED",
    "COST_REPEATED_URGE",
    "COST_SAW_MESSAGE",
    "MAX_LOAD",
    "MIN_LOAD",
    "RECOVERY_HALF_LIFE_MINUTES",
    "InteractionLoadService",
    "LoadView",
]
