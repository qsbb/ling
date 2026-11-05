"""Presence 域：角色此刻的生活状态与当前场景。

**唯一写入方**。两条硬规则：

1. **状态只影响表达，不影响权利。** 精力低时回复短一些、少主动，
   但用户直接提问仍然回答；绝不能拿"我累了"当拒绝的借口，
   也不能声称真实生理状况。
2. **过期状态不能当现状。** ``scene_commits`` 带 ``valid_until``，
   过期后读出来是 unknown，而不是继续沿用旧场景——
   否则会出现"我还在读书"说了三天。

设计参照同类实现
的 commit/valid_until 思路，但**不照抄它的进程内字典**：那在重启与多副本
下不一致，这里落库并用 CAS 提交。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import PresenceState, SceneCommitRow, utcnow
from ling.ports.config import LifePolicy

# 场景状态。``unknown`` 是**合法且常用**的答案：
# 没有证据时就说不知道，不硬编一个"我在看书"。
SCENE_STATES = ("chat", "focus", "rest", "errand", "unknown")

# 场景默认有效期。太短会让"刚才在干什么"反复变成 unknown；
# 太长会让旧场景残留。15 分钟是一个普通"手头这件事"的量级。
DEFAULT_SCENE_TTL = timedelta(minutes=15)

# 精力衰减：每小时掉多少。取小值——它是背景缓慢变化，不是每轮剧变。
ENERGY_DECAY_PER_HOUR = 1.5
# 社交负担：每次对话上涨，每小时回落。负担涨得快、落得慢，
# 这样"聊了很久"能被感知到，而不会一轮就"社交疲劳"。
SOCIAL_LOAD_PER_TURN = 3
SOCIAL_LOAD_DECAY_PER_HOUR = 6.0

MIN_ENERGY = 0
MAX_ENERGY = 100
MIN_LOAD = 0
MAX_LOAD = 100


def _clamp(value: float, low: int, high: int) -> int:
    return int(max(low, min(high, round(value))))


@dataclass(frozen=True)
class PresenceView:
    energy: int
    social_load: int
    # 由精力与负担推出的可打扰程度。这是给表达层用的**唯一**结论，
    # 不让调用方各自解读 energy 数值（那样每个地方都会有一套阈值）。
    availability: str  # available / low / resting

    def describe(self) -> str:
        """给提示词的一句话。刻意简短且不断言生理。"""
        if self.availability == "resting":
            return "你现在有点累，说话会比平时更短、更少主动开口"
        if self.availability == "low":
            return "你精神一般，回复可以短一点"
        return ""


@dataclass(frozen=True)
class SceneView:
    state: str
    activity_id: str | None
    place_id: str | None
    origin: str
    committed_at: datetime | None
    expired: bool

    def describe(self) -> str:
        """只在有真实状态时给出一句；unknown/过期返回空字符串。

        空字符串很关键：调用方拿到空就不注入，避免提示词里出现
        "你现在在做：unknown"这种既占预算又不像人的内容。
        """
        if self.expired or self.state == "unknown":
            return ""
        label = {
            "chat": "在跟人说话",
            "focus": "手头有事在做",
            "rest": "在歇着",
            "errand": "在外面办事",
        }.get(self.state, "")
        if not label:
            return ""
        # 模拟生活必须明示，不能冒充真实经历
        suffix = "（这是你的模拟生活，不是真实身体经历）" if self.origin == "simulated" else ""
        return f"你此刻的状态：{label}{suffix}"


# 与 ``view()`` 里判定 resting 的阈值**必须是同一个数**：
# 低于它开始恢复精力，高于它继续衰减。两处漂开会出现"判定为累、
# 却在扣精力"这种自相矛盾的状态。
REST_RECOVER_THRESHOLD = 25


class PresenceService:
    """生活状态的唯一写入方。"""

    def __init__(
        self, session: AsyncSession, *, actor_id: str, policy: LifePolicy | None = None
    ) -> None:
        self._session = session
        self._actor_id = actor_id
        # 生活节奏来自已发布配置的 life 段。不传时用原常量，行为不变。
        self._policy = policy or LifePolicy()

    # ---------------------------------------------------------- 精力与负担

    async def _row(self) -> PresenceState:
        row = await self._session.get(PresenceState, self._actor_id)
        if row is None:
            row = PresenceState(
                actor_id=self._actor_id,
                energy=70,
                social_load=20,
                settled_at=utcnow(),
                revision=0,
            )
            self._session.add(row)
            await self._session.flush()
        return row

    async def settle(self, *, now: datetime | None = None) -> PresenceState:
        """按**经过时间**结算衰减。

        幂等的关键：结算后把 ``settled_at`` 推到 ``now``。
        重复调用（重放、双 worker）只会按真正流逝的时间扣一次，
        而不是每次读都扣一次。
        """
        now = now or utcnow()
        row = await self._row()
        last = row.settled_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        elapsed_hours = max(0.0, (now - last).total_seconds() / 3600.0) if last else 0.0
        if elapsed_hours > 0:
            # 低精力时是**恢复**而不是继续衰减：没有这条，精力只减不增，
            # 迟早卡在 resting 上永久不接话（见 REST_RECOVER_THRESHOLD 注释）。
            rate = (
                self._policy.energy_recover_per_hour
                if float(row.energy) <= REST_RECOVER_THRESHOLD
                else -self._policy.energy_decay_per_hour
            )
            row.energy = _clamp(
                float(row.energy) + rate * elapsed_hours,
                MIN_ENERGY,
                MAX_ENERGY,
            )
            row.social_load = _clamp(
                float(row.social_load) - self._policy.social_load_decay_per_hour * elapsed_hours,
                MIN_LOAD,
                MAX_LOAD,
            )
            row.settled_at = now
            row.revision = int(row.revision or 0) + 1
            await self._session.flush()
        return row

    async def on_turn(self, *, now: datetime | None = None) -> PresenceState:
        """一轮对话对状态的影响：社交负担上升。"""
        row = await self.settle(now=now)
        row.social_load = _clamp(
            float(row.social_load) + self._policy.social_load_per_turn, MIN_LOAD, MAX_LOAD
        )
        row.revision = int(row.revision or 0) + 1
        await self._session.flush()
        return row

    async def view(self, *, now: datetime | None = None) -> PresenceView:
        row = await self.settle(now=now)
        energy = int(row.energy)
        load = int(row.social_load)
        if energy <= REST_RECOVER_THRESHOLD:
            availability = "resting"
        elif energy <= 50 or load >= 75:
            availability = "low"
        else:
            availability = "available"
        return PresenceView(energy=energy, social_load=load, availability=availability)

    # ---------------------------------------------------------- 当前场景

    async def commit_scene(
        self,
        *,
        state: str,
        origin: str = "simulated",
        activity_id: str | None = None,
        place_id: str | None = None,
        source_event_ids: list[str] | None = None,
        ttl: timedelta | None = None,
        now: datetime | None = None,
    ) -> SceneCommitRow:
        """提交"此刻在做什么"。

        ``origin`` 必须如实：``simulated`` 是模拟生活（不能冒充现实），
        ``tool_receipt`` 是有真实回执的动作，``user_stated`` 是用户刚说的。
        """
        if state not in SCENE_STATES:
            raise ValueError(f"未知场景状态: {state}")
        now = now or utcnow()
        row = await self._session.get(SceneCommitRow, self._actor_id)
        if row is None:
            row = SceneCommitRow(actor_id=self._actor_id)
            self._session.add(row)
        row.state = state
        row.activity_id = activity_id
        row.place_id = place_id
        row.origin = origin
        row.source_event_ids = list(source_event_ids or [])
        row.committed_at = now
        row.valid_until = now + (
            ttl or timedelta(minutes=self._policy.scene_ttl_minutes)
        )
        await self._session.flush()
        return row

    async def clear_scene(self) -> None:
        """打断/结束时清空——留着旧场景会让它继续自述。"""
        row = await self._session.get(SceneCommitRow, self._actor_id)
        if row is not None:
            row.state = "unknown"
            row.activity_id = None
            row.place_id = None
            row.valid_until = utcnow()
            await self._session.flush()

    async def scene(self, *, now: datetime | None = None) -> SceneView:
        """读当前场景。**过期即 unknown**，不沿用旧值。"""
        now = now or utcnow()
        row = (
            await self._session.execute(
                select(SceneCommitRow).where(SceneCommitRow.actor_id == self._actor_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return SceneView("unknown", None, None, "simulated", None, expired=False)
        valid_until = row.valid_until
        if valid_until is not None and valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=UTC)
        expired = valid_until is not None and valid_until <= now
        if expired:
            # 过期不返回旧状态，避免"我还在读书"说三天
            return SceneView(
                "unknown", None, None, row.origin, row.committed_at, expired=True
            )
        return SceneView(
            state=str(row.state),
            activity_id=row.activity_id,
            place_id=row.place_id,
            origin=str(row.origin),
            committed_at=row.committed_at,
            expired=False,
        )


__all__ = [
    "DEFAULT_SCENE_TTL",
    "SCENE_STATES",
    "PresenceService",
    "PresenceView",
    "SceneView",
]
