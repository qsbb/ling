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

# 证据种类的判定放在跨域共享的纯规则模块（`domains/evidence.py`，不是某个域）：
# presence 只读它，不复制"什么算事实"的第二套判据。
from ling.domains.evidence import FACT_KINDS, EvidenceKind
from ling.domains.fallback import DependencyState, DependencyValue, ResolvedInput
from ling.infra.models import PresenceState, SceneCommitRow, utcnow
from ling.ports.config import LifePolicy

# 场景状态。``unknown`` 是**合法且常用**的答案：
# 没有证据时就说不知道，不硬编一个"我在看书"。
SCENE_STATES = ("chat", "focus", "rest", "errand", "unknown")

# 回退契约里"上游"的名字（行为日志与响应标记都用它）。
SCENE_UPSTREAM = "scene"

# 场景不可用（过期 / 未知 / 从没提交过）时的**领域默认**：不冒充任何场景。
#
# 为什么默认是 ``unknown`` 而不是 "rest"/"chat"：设计 §4 明令"不把 unknown
# 改写为 idle/rest"。她只是**不知道**自己在做什么，不是"在休息"：
# 写成一个具体状态会让表达层说出"我在歇着"这种没有证据的话。
SCENE_DEFAULT_STATE = "unknown"
SCENE_DEFAULT_REASON = "场景不明（没有有效场景 / 已过期），按未知处理，不冒充她在做什么"

#: 哪些上游状态值得记一条 `upstream_fallback` 行为日志。
#:
#: **故意不含 `empty` 与 `unknown`**：前者是"从没提交过场景/已过期"（最常见
#: 的正常状态），后者是"本来就不明"。把这两个记成回退，会让 500 条环形日志
#: 被正常状态刷满，真正的故障反而看不见——这是 `domains/fallback.py` 开头
#: 明写的取向（"上游为空是正常状态，不是故障"）。
_SCENE_FALLBACK_WORTH_LOGGING: frozenset[DependencyState] = frozenset(
    {DependencyState.failed, DependencyState.unavailable}
)

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

# ---------------------------------------------------------------- 饱腹（阶段 2）
#
# 设计 §3：饱腹是**模拟值**，与精力共用同一个 `settled_at` 惰性结算，
# 不另起"饥饿 tick"。醒时 −5/小时、睡时 −2/小时，clamp 到 0..100，
# 单次结算最多跨 24 小时（避免停机几天后一觉醒来饿到 0 那种极端跳变）。
# 这些是工程初值，不是生理模型。
SATIETY_DECAY_PER_HOUR_AWAKE = 5.0
SATIETY_DECAY_PER_HOUR_ASLEEP = 2.0
SATIETY_MAX_SETTLE_HOURS = 24.0
SATIETY_MIN = 0
SATIETY_MAX = 100
# 餐食只有在**完成且有证据**（observed / user_confirmed）时才加：
# 计划了早餐不等于吃过。区间 20..35 是设计 §3 的工程初值。
MEAL_SATIETY_GAIN: dict[str, int] = {
    "breakfast": 20,
    "lunch": 30,
    "dinner": 35,
    "snack": 20,
    "meal": 25,
}
MEAL_KINDS: tuple[str, ...] = tuple(MEAL_SATIETY_GAIN)

# 场景 TTL 续期的**合法原因**（设计 §3）：真实边界事件，或活动仍在运行。
# 没有这两条之一就不许续期——否则停机后过期场景会被"每分钟一次的空 tick"
# 续成永久现状，又变成"我还在读书"说三天。
SCENE_REFRESH_REASON_SEGMENT = "segment_boundary"
SCENE_REFRESH_REASON_SLEEP = "sleep_boundary"
SCENE_REFRESH_REASON_COMMITMENT = "commitment_boundary"
SCENE_REFRESH_REASON_RUNNING = "running_activity"
SCENE_REFRESH_REASONS = frozenset(
    {
        SCENE_REFRESH_REASON_SEGMENT,
        SCENE_REFRESH_REASON_SLEEP,
        SCENE_REFRESH_REASON_COMMITMENT,
        SCENE_REFRESH_REASON_RUNNING,
    }
)
# 剩余 TTL 少于一半时才续：既不让场景在活跃期过期，也不每分钟写一行。
SCENE_REFRESH_THRESHOLD_RATIO = 0.5


def _clamp(value: float, low: int, high: int) -> int:
    return int(max(low, min(high, round(value))))


@dataclass(frozen=True)
class PresenceView:
    energy: int
    social_load: int
    # 由精力与负担推出的可打扰程度。这是给表达层用的**唯一**结论，
    # 不让调用方各自解读 energy 数值（那样每个地方都会有一套阈值）。
    availability: str  # available / low / resting
    # 模拟饱腹（0–100）。低饱腹只影响选择，不阻断直接答复。
    satiety: int = 70

    def describe(self) -> str:
        """给提示词的一句话。刻意简短且不断言生理。"""
        if self.availability == "resting":
            return "你现在有点累，说话会比平时更短、更少主动开口"
        if self.availability == "low":
            return "你精神一般，回复可以短一点"
        return ""


@dataclass(frozen=True)
class SatietyUpdate:
    """一次"餐食 → 饱腹"的结果（不写表以外的任何地方）。

    ``applied`` 为假时 ``reason_code`` 说明为什么没加：
    ``evidence_clock_only``（只有时钟/计划，不算吃过）、``unknown_meal_kind``。
    """

    applied: bool
    reason_code: str
    satiety_before: int
    satiety_after: int

    @property
    def delta(self) -> int:
        return self.satiety_after - self.satiety_before


@dataclass(frozen=True)
class SceneView:
    state: str
    activity_id: str | None
    place_id: str | None
    origin: str
    committed_at: datetime | None
    expired: bool

    def dependency(self) -> DependencyValue[SceneView]:
        """投影成回退契约要的上游状态。

        - 有有效场景（未过期、不是 unknown）→ ``ready``；
        - 过期 → ``empty``（**不**是"她还在做"，也不是故障）；
        - 记录本身缺失 → ``empty``；
        - ``unknown`` → ``unknown``。

        这三种不可用状态**都不带值**：读侧拿不到任何可断言的场景，
        只能走领域默认（「不知道」/不注入提示词），见 `scene_default`。
        """
        state = str(self.state)
        if self.expired:
            return DependencyValue(
                state=DependencyState.empty,
                upstream=SCENE_UPSTREAM,
                reason="场景已过期",
            )
        if state == "unknown" or not state:
            return DependencyValue(
                state=DependencyState.unknown,
                upstream=SCENE_UPSTREAM,
                reason="场景未知",
            )
        return DependencyValue(state=DependencyState.ready, upstream=SCENE_UPSTREAM, value=self)

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

    async def settle(
        self, *, now: datetime | None = None, asleep: bool = False
    ) -> PresenceState:
        """按**经过时间**结算衰减（精力、社交负担、饱腹）。

        幂等的关键：结算后把 ``settled_at`` 推到 ``now``。
        重复调用（重放、双 worker）只会按真正流逝的时间扣一次，
        而不是每次读都扣一次。

        ``asleep=True`` 只影响**饱腹**的衰减速度（睡时 −2/小时、醒时 −5/小时，
        设计 §3）；精力仍旧沿用已发布配置里的费率，不在本任务里改口径。
        饱腹的单次结算最多跨 :data:`SATIETY_MAX_SETTLE_HOURS`（24 小时）：
        停机几天后醒来不该从 70 直接掉到 0。
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
            satiety_rate = (
                SATIETY_DECAY_PER_HOUR_ASLEEP
                if asleep
                else SATIETY_DECAY_PER_HOUR_AWAKE
            )
            satiety_hours = min(elapsed_hours, SATIETY_MAX_SETTLE_HOURS)
            row.satiety = _clamp(
                float(row.satiety) - satiety_rate * satiety_hours,
                SATIETY_MIN,
                SATIETY_MAX,
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
        return PresenceView(
            energy=energy,
            social_load=load,
            availability=availability,
            satiety=int(row.satiety),
        )

    async def record_meal(
        self,
        *,
        evidence_kind: EvidenceKind | str,
        meal_kind: str = "meal",
        now: datetime | None = None,
    ) -> SatietyUpdate:
        """餐食**完成且有证据**时才加饱腹（设计 §3）。

        硬门：只有 ``observed`` / ``user_confirmed`` 两种证据才加。计划了餐食、
        或只有时钟证据（"中午到了"）时**不加**——那正是"把计划当事实"的入口。

        幂等由调用方保证：本方法只处理"一次真实的完成事件"，活动终态本身就
        是一次性的；``presence_states`` 没有可存事件键的列（迁移不归本任务），
        所以这里不另造去重表，**也不假装有**去重。
        """
        try:
            kind = EvidenceKind(str(evidence_kind))
        except ValueError as exc:
            raise ValueError(f"未知的证据种类: {evidence_kind}") from exc
        row = await self.settle(now=now)
        before = int(row.satiety)
        if kind not in FACT_KINDS:
            # clock_only：时间到了不等于吃过。原样返回，不写表。
            return SatietyUpdate(
                applied=False,
                reason_code="evidence_clock_only",
                satiety_before=before,
                satiety_after=before,
            )
        gain = MEAL_SATIETY_GAIN.get(str(meal_kind).strip())
        if gain is None:
            return SatietyUpdate(
                applied=False,
                reason_code="unknown_meal_kind",
                satiety_before=before,
                satiety_after=before,
            )
        row.satiety = _clamp(float(before) + gain, SATIETY_MIN, SATIETY_MAX)
        row.revision = int(row.revision or 0) + 1
        await self._session.flush()
        return SatietyUpdate(
            applied=True,
            reason_code="",
            satiety_before=before,
            satiety_after=int(row.satiety),
        )

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

    async def clear_scene(self, *, now: datetime | None = None) -> None:
        """打断/结束时清空——留着旧场景会让它继续自述。

        ``now`` 可显式传入（调度/测试用固定时刻）；不传就是真实时钟。
        """
        row = await self._session.get(SceneCommitRow, self._actor_id)
        if row is not None:
            row.state = "unknown"
            row.activity_id = None
            row.place_id = None
            row.valid_until = now or utcnow()
            await self._session.flush()

    async def refresh_scene_ttl(
        self,
        *,
        reason: str,
        activity_running: bool = False,
        ttl: timedelta | None = None,
        now: datetime | None = None,
    ) -> bool:
        """给**当前**场景续期；不合法就不写（返回 ``False``）。

        设计 §3 的唯一允许条件：**真实边界事件**（段开始/结束、进入睡眠窗、
        已确认约定到期）或**活动仍在运行**。所以：

        - ``reason`` 必须在 :data:`SCENE_REFRESH_REASONS` 里，或 ``activity_running``
          为真；两者都没有 → 拒绝续期（这是"停机上三天还说在读书"的洞）；
        - 场景是 ``unknown`` / 已过期 / 本来就没有 → 不续（过期就是 unknown，
          不能靠续期把过期状态救回来）；
        - 剩余 TTL 还超过一半 → 不写（避免每 60 秒一行 churn）。

        返回 ``True`` 表示这一次真的写了 ``valid_until``。
        """
        if reason not in SCENE_REFRESH_REASONS and not activity_running:
            return False
        moment = now or utcnow()
        row = await self._session.get(SceneCommitRow, self._actor_id)
        if row is None:
            return False
        valid_until = row.valid_until
        if valid_until is not None and valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=UTC)
        if str(row.state) == "unknown" or valid_until is None or valid_until <= moment:
            return False
        window = ttl or timedelta(minutes=self._policy.scene_ttl_minutes)
        remaining = valid_until - moment
        if remaining > window * SCENE_REFRESH_THRESHOLD_RATIO:
            return False
        row.valid_until = moment + window
        await self._session.flush()
        return True

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


    def resolve_scene(
        self, view: SceneView
    ) -> tuple[SceneView, ResolvedInput[SceneView]]:
        """场景不可用（过期 / 未知）时回退到**领域默认**，而不是让调用点各自判断。

        回退值的来源是 `SCENE_DEFAULT_STATE`（`unknown`），**不是**旧场景：
        过期场景塞回去就是"我还在读书"说三天，那是本项目明令禁止的事实伪造。

        ``ResolvedInput.value`` 在回退时是一个**合成**的 `SceneView`：
        `state=unknown`、`origin=unknown`、`expired=True`。调用方拿它渲染
        「不知道」即可，也不该把它写回 `scene_commits`（本方法只读不写）。
        """
        from ling.domains.fallback import log_fallback, resolve_input

        dependency = view.dependency()
        default = SceneView(
            state=SCENE_DEFAULT_STATE,
            activity_id=None,
            place_id=None,
            origin="unknown",
            committed_at=None,
            expired=True,
        )
        resolved = resolve_input(
            dependency, default=default, default_reason=SCENE_DEFAULT_REASON
        )
        # **只在上游"该有却没有"时记回退日志，不把"从没提交过场景"记成故障。**
        #
        # 这一条是修真实噪声：`scene()` 在库里没有 `scene_commits` 行时返回
        # `state=unknown, expired=False`（这是**最常见**的默认状态——新装、
        # 长期没跑过场景投影、或者刚清理过）。原来这里无条件调
        # `log_fallback`，于是每一次首屏读仪表盘、每一次看手机，都往行为
        # 日志里塞一条 `upstream_fallback`。日志是 500 条环形缓冲，很快就被
        # "一切正常"刷满，真正的回退反而看不见了。
        #
        # 这与 `fallback.py` 开头的取向本来是一致的：「上游为空（今天没安排）
        # 是**正常状态**，不是故障」。过期也一样——过期即未知是设计，不是故障。
        # 所以只有 `dependency.state` 是**故障类**（failed/unavailable）才记；
        # `empty`（从没有/已过期）与 `unknown`（本来就不明）都不记。
        if dependency.state in _SCENE_FALLBACK_WORTH_LOGGING:
            log_fallback(resolved, consumer="presence.scene")
        return resolved.value, resolved


__all__ = [
    "DEFAULT_SCENE_TTL",
    "MEAL_KINDS",
    "MEAL_SATIETY_GAIN",
    "SATIETY_DECAY_PER_HOUR_ASLEEP",
    "SATIETY_DECAY_PER_HOUR_AWAKE",
    "SCENE_REFRESH_REASONS",
    "SCENE_REFRESH_REASON_COMMITMENT",
    "SCENE_REFRESH_REASON_RUNNING",
    "SCENE_REFRESH_REASON_SEGMENT",
    "SCENE_REFRESH_REASON_SLEEP",
    "SCENE_DEFAULT_REASON",
    "SCENE_DEFAULT_STATE",
    "SCENE_STATES",
    "SCENE_UPSTREAM",
    "PresenceService",
    "PresenceView",
    "SatietyUpdate",
    "SceneView",
]
