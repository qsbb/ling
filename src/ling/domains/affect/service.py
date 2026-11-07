"""Affect 域：短时情绪、动机与衰减。

唯一写入方。硬规则：
- 情绪是**短时衰减状态**，不是人格；不能放宽权限。
- 强度随时间线性衰减；无事件不空转调用模型。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import AffectState, utcnow
from ling.infra.security import new_id

EMOTIONS = ("calm", "warm", "tired", "curious", "uneasy", "content")


@dataclass(frozen=True)
class AffectView:
    emotion: str
    intensity: float
    updated_at: datetime
    cause: str | None

    def tone_hint(self) -> str:
        """给 ContextCompiler 的**表达提示**，不是权限。"""
        if self.intensity < 0.15:
            return ""
        mapping = {
            "calm": "语气平稳",
            "warm": "语气偏暖",
            "tired": "有点疲倦，回复更短",
            "curious": "好奇，愿意多问一句",
            "uneasy": "有点不安，措辞更谨慎",
            "content": "心情不错",
        }
        return mapping.get(self.emotion, "")


class AffectService:
    def __init__(self, session: AsyncSession, *, actor_id: str = "actor_ling") -> None:
        self._session = session
        self._actor_id = actor_id

    async def _rows(self) -> list[AffectState]:
        """同一轮里只查一次。

        `current()` 与 `for_window()` 读的是**同一张表同一批行**，而上下文
        装配会两个都调——原来等于每轮白扫两遍。服务实例按请求创建，缓存在
        实例上不会跨轮串味。

        写入后必须 `invalidate()`，否则会读到旧情绪。
        """
        cached = self.__dict__.get("_rows_cache")
        if cached is not None:
            return cached  # type: ignore[return-value]
        rows = list(
            (
                await self._session.execute(
                    select(AffectState).where(AffectState.actor_id == self._actor_id)
                )
            ).scalars()
        )
        self._rows_cache = rows
        return rows

    def invalidate(self) -> None:
        """写操作后调用，避免继续读到缓存里的旧情绪。"""
        self.__dict__.pop("_rows_cache", None)

    async def current(self) -> AffectView:
        """读取**全局**语气趋势（跨窗口可用）。

        window 作用域的情绪带具体原因，绝不跨窗口，因此不在此返回。
        """
        now = datetime.now(UTC)
        rows = [r for r in await self._rows() if (r.scope or "global") == "global"]
        best: AffectView | None = None
        for row in rows:
            updated = row.updated_at
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            hours = max(0.0, (now - updated).total_seconds() / 3600.0)
            decayed = max(0.0, float(row.intensity) - float(row.decay_per_hour) * hours)
            view = AffectView(
                emotion=row.emotion,
                intensity=round(decayed, 3),
                updated_at=updated,
                cause=row.cause,
            )
            if best is None or view.intensity > best.intensity:
                best = view
        if best is None:
            return AffectView(emotion="calm", intensity=0.0, updated_at=now, cause=None)
        return best

    async def nudge(
        self,
        emotion: str,
        delta: float,
        *,
        cause: str | None = None,
        decay_per_hour: float = 0.15,
        scope: str = "global",
        window_id: str | None = None,
        turn_id: str | None = None,
    ) -> AffectView:
        """改变某种情绪强度（0..1）。同一时刻允许多种情绪共存，读取时取主导。"""
        if emotion not in EMOTIONS:
            raise ValueError(f"未知情绪: {emotion}")
        if scope not in {"global", "window"}:
            raise ValueError(f"未知情绪作用域: {scope}")
        if scope == "window" and not window_id:
            raise ValueError("window 作用域必须提供 window_id")
        query = select(AffectState).where(
            AffectState.actor_id == self._actor_id, AffectState.emotion == emotion
        )
        if scope == "window":
            query = query.where(
                AffectState.scope == "window", AffectState.window_id == (window_id or "")
            )
        else:
            # global 用空串存储（见下方 upsert 注释）
            query = query.where(
                AffectState.scope == "global",
                sa_func.coalesce(AffectState.window_id, "") == "",
            )
        # 行锁：nudge 是"读出 → 按时间衰减 → 加增量 → 写回"，
        # 并发下会丢失更新（此前实现的问题）。加锁保证同一情绪的调整串行。
        row = (await self._session.execute(query.with_for_update())).scalar_one_or_none()
        if row is None:
            # 并发下多个事务同时发现"没有这条情绪"→ 都 INSERT → 唯一约束冲突。
            # 行锁对不存在的行无效，用 ON CONFLICT DO NOTHING + 重读加锁。
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            await self._session.execute(
                pg_insert(AffectState)
                .values(
                    id=new_id("aff"),
                    actor_id=self._actor_id,
                    emotion=emotion,
                    intensity=0.0,
                    decay_per_hour=decay_per_hour,
                    scope=scope,
                    # global 用空串而不是 NULL：PostgreSQL 中 NULL 互不相等，
                    # 唯一约束会被绕过（并发实测暴露）。空串让约束真正生效。
                    window_id=(window_id or "") if scope == "window" else "",
                    updated_at=utcnow(),
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        AffectState.actor_id,
                        AffectState.emotion,
                        AffectState.scope,
                        AffectState.window_id,
                    ]
                )
            )
            await self._session.flush()
            row = (await self._session.execute(query.with_for_update())).scalar_one()

        now = datetime.now(UTC)
        if turn_id and (row.last_turn_id or "") == turn_id:
            # 同一轮重复处理：不再叠加强度，直接返回当前视图（幂等）
            return await self.current()

        # 用**原子 UPDATE** 计算，而不是"读旧值 → Python 加 → 写回"。
        # 此前实现的丢失更新问题：并发事务各读各的基线，后写的覆盖先写的。
        # SQL 里做衰减 + 增量，数据库保证读-改-写是单条语句的原子操作。
        decay_expr = AffectState.intensity - (
            AffectState.decay_per_hour
            * sa_func.greatest(
                0.0,
                sa_func.extract("epoch", sa.literal(now) - AffectState.updated_at) / 3600.0,
            )
        )
        new_intensity = sa_func.least(1.0, sa_func.greatest(0.0, decay_expr + delta))
        values: dict = {
            "intensity": new_intensity,
            "decay_per_hour": decay_per_hour,
            "updated_at": utcnow(),
        }
        if turn_id:
            values["last_turn_id"] = turn_id
        if cause:
            values["cause"] = cause
        await self._session.execute(
            sa_update(AffectState).where(AffectState.id == row.id).values(**values)
        )
        self.invalidate()
        await self._session.flush()
        return await self.current()
        await self._session.flush()
        return await self.current()

    async def for_window(self, window_id: str) -> AffectView | None:
        """读取某窗口的局部情绪（不跨窗口）。"""
        now = datetime.now(UTC)
        rows = [
            r
            for r in await self._rows()
            if (r.scope or "global") == "window" and r.window_id == window_id
        ]
        best: AffectView | None = None
        for row in rows:
            updated = row.updated_at
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            hours = max(0.0, (now - updated).total_seconds() / 3600.0)
            decayed = max(0.0, float(row.intensity) - float(row.decay_per_hour) * hours)
            if best is None or decayed > best.intensity:
                best = AffectView(
                    emotion=row.emotion,
                    intensity=round(decayed, 3),
                    updated_at=updated,
                    cause=row.cause,
                )
        return best

    async def snapshot(self) -> list[AffectView]:
        now = datetime.now(UTC)
        out: list[AffectView] = []
        for row in await self._rows():
            updated = row.updated_at
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            hours = max(0.0, (now - updated).total_seconds() / 3600.0)
            out.append(
                AffectView(
                    emotion=row.emotion,
                    intensity=round(max(0.0, float(row.intensity) - float(row.decay_per_hour) * hours), 3),
                    updated_at=updated,
                    cause=row.cause,
                )
            )
        out.sort(key=lambda v: -v.intensity)
        return out

    async def apply_sleep_settlement(
        self, *, bed_at: datetime, wake_at: datetime
    ) -> SleepSettlement:
        """把 global 情绪按「睡一觉」的规则结算一次。

        **不**判断这一晚是否存在、也**不**写 ``sleep_settlements``：那是
        ``domains/dream`` 的 ``SleepSettlementService`` 的职责（唯一键/幂等）。
        这里只做一件事：改 ``affect_states`` 的 global 分量。

        两条隔离：
        - ``scope=window`` 的情绪带具体原因，**不跨窗口搬运**（设计 §5/§6）；
        - ``updated_at`` 晚于 ``wake_at`` 的行**不碰**——结算迟到（比如 worker
          重启后补算）时，不能把醒来之后的新心情回滚成睡前值。
        """
        bed = _aware(bed_at)
        wake = _aware(wake_at)
        before: dict[str, dict[str, float | None]] = {}
        after: dict[str, dict[str, float | None]] = {}
        settled: list[str] = []
        untouched: list[str] = []
        skipped: list[str] = []

        for row in await self._rows():
            if (row.scope or "global") != "global":
                continue
            updated = _aware(row.updated_at)
            emotion = str(row.emotion)
            if updated > wake:
                skipped.append(emotion)
                continue
            raw = float(row.intensity)
            decay = float(row.decay_per_hour)
            # 睡前之后又被改过：拿当前值当上限，不假装知道 bed 时的值。
            bed_intensity = (
                linear_intensity_at(
                    intensity=raw, decay_per_hour=decay, updated_at=updated, at=bed
                )
                if updated <= bed
                else raw
            )
            linear = linear_intensity_at(
                intensity=raw, decay_per_hour=decay, updated_at=updated, at=wake
            )
            floor = sleep_floor_applies(
                emotion=emotion,
                valence=row.valence,
                arousal=row.arousal,
                bed_intensity=bed_intensity,
            )
            target = overnight_intensity(
                linear=linear, bed=bed_intensity, floor_applies=floor
            )
            new_valence = regress_toward_zero(row.valence)
            new_arousal = regress_toward_zero(row.arousal)
            before[emotion] = _component_payload(
                intensity=bed_intensity, valence=row.valence, arousal=row.arousal
            )
            after[emotion] = _component_payload(
                intensity=target, valence=new_valence, arousal=new_arousal
            )
            if target <= 0.0 and new_valence is None and new_arousal is None:
                # 已经归零：写不写结果一样，别制造无意义的 UPDATE。
                untouched.append(emotion)
                continue
            values: dict[str, object] = {
                "intensity": round(target, 4),
                "updated_at": wake,
            }
            if new_valence is not None:
                values["valence"] = round(new_valence, 4)
            if new_arousal is not None:
                values["arousal"] = round(new_arousal, 4)
            await self._session.execute(
                sa_update(AffectState)
                .where(AffectState.id == row.id)
                .values(**values)
            )
            settled.append(emotion)

        if settled:
            self.invalidate()
            await self._session.flush()
        return SleepSettlement(
            before=before,
            after=after,
            settled=tuple(settled),
            untouched=tuple(untouched),
            skipped=tuple(skipped),
        )


# ------------------------------------------------------------------ 跨夜结算
#
# 设计 §5「跨夜情绪结算」：与梦**解耦**，也与当天计划是否 ready 无关。
#
# 睡前 global 分量强度记作 ``I_bed``（由当前行 + 现有线性衰减推算到 bed 瞬时），
# 醒时的线性推算值记作 ``I_linear``，则
#
#     I_wake = min(0.35, max(I_linear, 0.6 × I_bed))
#
# **只对"睡前显著、且负性或高唤醒"的 global 分量**套这个下限：普通的好心情
# 不该因为睡了一觉被抬高。否则 ``0.6 × I_bed`` 会把"昨天有点开心"变成"今天
# 还在"，与"隔夜让高唤醒负性情绪强度下降、但不删除"的方向相反。
#
# 0.35 / 0.6 / 0.2 / 0.5 / 40% 全是**工程初值**：调研 §2.3 只给方向
# （[Walker & van der Helm 2009](https://pubmed.ncbi.nlm.nih.gov/19702380/)），
# 没有给系数，也不是论文测出的数值。
#
# 为什么必须显式算 ``I_bed``：现有 ``decay_per_hour≈0.15`` 的线性衰减下，
# 8 小时足以把多数情绪归零；只对醒时值乘 0.6 完全不起作用（设计 §5 原话）。
SLEEP_FLOOR_RATIO = 0.6
SLEEP_INTENSITY_CAP = 0.35
SLEEP_SIGNIFICANT_MIN = 0.2
SLEEP_HIGH_AROUSAL_MIN = 0.5
SLEEP_AXIS_REGRESSION = 0.4

# 「负性」「高唤醒」没有专门的列：旧行的 ``valence``/``arousal`` 是 NULL，
# 所以要先退回按情绪标签判定（即 OCC 映射产出的那一套标签）。
NEGATIVE_EMOTIONS: frozenset[str] = frozenset({"uneasy", "tired"})
HIGH_AROUSAL_EMOTIONS: frozenset[str] = frozenset({"uneasy", "curious"})


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def linear_intensity_at(
    *, intensity: float, decay_per_hour: float, updated_at: datetime, at: datetime
) -> float:
    """把一条情绪的强度按**现有线性衰减**推算到 ``at`` 瞬时。

    与 :meth:`AffectService.current` 同一口径（不是指数衰减——调研建议指数，
    但源码现在是线性，换公式会让旧情绪突然变长/变短）。
    """
    hours = max(0.0, (_aware(at) - _aware(updated_at)).total_seconds() / 3600.0)
    return max(0.0, float(intensity) - float(decay_per_hour) * hours)


def is_negative(emotion: str, valence: float | None) -> bool:
    if valence is not None:
        return float(valence) < 0.0
    return str(emotion) in NEGATIVE_EMOTIONS


def is_high_arousal(emotion: str, arousal: float | None) -> bool:
    if arousal is not None:
        return float(arousal) >= SLEEP_HIGH_AROUSAL_MIN
    return str(emotion) in HIGH_AROUSAL_EMOTIONS


def sleep_floor_applies(
    *, emotion: str, valence: float | None, arousal: float | None, bed_intensity: float
) -> bool:
    """睡前这一分量是否够"显著且负性/高唤醒"，值得套跨夜下限。"""
    if float(bed_intensity) < SLEEP_SIGNIFICANT_MIN:
        return False
    return is_negative(emotion, valence) or is_high_arousal(emotion, arousal)


def overnight_intensity(*, linear: float, bed: float, floor_applies: bool) -> float:
    """``I_wake = min(0.35, max(I_linear, 0.6 × I_bed))``（不满足条件退线性值）。"""
    value = max(0.0, min(1.0, float(linear)))
    if not floor_applies:
        return value
    return min(SLEEP_INTENSITY_CAP, max(value, SLEEP_FLOOR_RATIO * max(0.0, float(bed))))


def regress_toward_zero(
    value: float | None, *, ratio: float = SLEEP_AXIS_REGRESSION
) -> float | None:
    """把 ``valence``/``arousal`` 朝 0 回归 ``ratio``（40% → 只剩 0.6 倍）。"""
    if value is None:
        return None
    return float(value) * (1.0 - float(ratio))


@dataclass(frozen=True)
class SleepSettlement:
    """一次跨夜结算的可审计结果：只有粗粒度数值，没有正文。

    ``before`` 是**推算到 bed 瞬时**的 global 快照（``I_bed`` 的来源），
    ``after`` 是醒来时的值。``skipped`` 是醒来后又被改过、故意不碰的分量。
    """

    before: dict[str, dict[str, float | None]]
    after: dict[str, dict[str, float | None]]
    settled: tuple[str, ...]
    untouched: tuple[str, ...]
    skipped: tuple[str, ...]


def _component_payload(
    *, intensity: float, valence: float | None, arousal: float | None
) -> dict[str, float | None]:
    return {
        "intensity": round(float(intensity), 4),
        "valence": None if valence is None else round(float(valence), 4),
        "arousal": None if arousal is None else round(float(arousal), 4),
    }
