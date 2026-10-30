"""Proactivity 域：主动机会、勿扰、限额与可达性硬门。

唯一写入方。硬规则（设计文档 §6.3）：
- 生活、天气、目标和记忆只产生**有时效的机会**；不直接发消息。
- 统一检查：勿扰、受众、当前占用、渠道可达、联系负担、预算、真实回执。
- 只有一个调度器拥有候选提交权。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta, tzinfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import ProactiveOpportunity, utcnow
from ling.infra.security import new_id
from ling.ports.config import DialoguePolicy

# 联系负担：每 24 小时最多主动发起次数（产品初值，可由用户调低）
DEFAULT_DAILY_LIMIT = 2
# 勿扰默认窗口（本地时间）
DEFAULT_QUIET_START = time(23, 0)
DEFAULT_QUIET_END = time(8, 0)


@dataclass(frozen=True)
class OpportunityView:
    opportunity_id: str
    subject_id: str
    kind: str
    reason: str
    state: str
    expires_at: datetime
    # 生成好的开场白。None = 还没生成（不是"生成了空的"）。
    draft_text: str | None = None


@dataclass
class GateDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)


def _resolve_timezone(name: str | None) -> tuple[tzinfo, bool]:
    """解析用户时区，返回 (时区, 是否显式指定)。

    第二个返回值让调用方能**说出**"我其实不知道你的时区"，
    而不是静默按 UTC 算——后者会让勿扰时段在错误的时间生效，
    而用户只看到"她怎么这个点找我"，无从判断是配置问题。

    注意缺省**不能**用 `datetime.now().astimezone()`（服务器本地时区）：
    那会让同一份代码在不同部署机器上行为不同，且与用户实际作息无关。
    """
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name), True
        except Exception:  # noqa: BLE001 - 无效时区名回退到 UTC
            pass
    return UTC, False


class ProactivityService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        actor_id: str = "actor_ling",
        policy: DialoguePolicy | None = None,
    ) -> None:
        self._session = session
        self._actor_id = actor_id
        # 免打扰与日限额来自已发布配置的 dialogue 段。
        # 不传时用原常量，行为不变。
        self._policy = policy or DialoguePolicy()

    async def propose(
        self,
        *,
        subject_id: str,
        kind: str,
        reason: str,
        ttl_minutes: int = 60,
        # --- 因果字段（阶段 4）。旧调用方不传也能工作，但那样产生的
        # 机会缺少"为什么是现在"，会在收集器里被当成无来源候选跳过。
        source_event_id: str | None = None,
        dedupe_key: str | None = None,
        reason_code: str | None = None,
        not_before: datetime | None = None,
        best_until: datetime | None = None,
        visibility: str = "subject_private",
        reply_expected: bool = False,
        channel_hint: str | None = None,
    ) -> OpportunityView | None:
        """登记一个主动机会。``dedupe_key`` 已存在时返回 None（不重复登记）。"""
        if dedupe_key:
            existing = (
                await self._session.execute(
                    select(ProactiveOpportunity).where(
                        ProactiveOpportunity.dedupe_key == dedupe_key
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return None
        now = utcnow()
        row = ProactiveOpportunity(
            id=new_id("opp"),
            actor_id=self._actor_id,
            subject_id=subject_id,
            kind=kind,
            reason=reason[:1000],
            state="candidate",
            expires_at=(best_until or now) + timedelta(minutes=max(1, ttl_minutes)),
            created_at=now,
            source_event_id=source_event_id,
            dedupe_key=dedupe_key,
            reason_code=reason_code,
            not_before=not_before,
            best_until=best_until,
            visibility=visibility,
            reply_expected=reply_expected,
            channel_hint=channel_hint,
        )
        self._session.add(row)
        await self._session.flush()
        return self._view(row)

    async def gate(
        self,
        *,
        subject_id: str,
        now: datetime | None = None,
        quiet_start: time | None = None,
        quiet_end: time | None = None,
        daily_limit: int | None = None,
        channel_can_proactive: bool = False,
        # 不能主动推送的**具体原因**（调用方知道是环境变量没开还是没配）。
        # 不给就只能说笼统的"不支持"，用户会找错方向。
        channel_proactive_reason: str | None = None,
        actor_busy: bool = False,
        user_opted_out: bool = False,
        subject_timezone: str | None = None,
    ) -> GateDecision:
        """统一硬门。任何一项不满足都不允许主动联系。

        ``subject_timezone`` 必须是**用户**的时区（IANA 名，如
        ``Asia/Shanghai``）。原实现用 ``now.astimezone()``——那是**服务器**
        本地时区。服务器在 UTC、用户在东八区时，"22:00-08:00 勿扰"
        会按 UTC 算，等于在用户清醒时静音、在凌晨打扰。
        "今日已发几条"同理，必须按用户当地的"今天"划分。
        """
        now = now or datetime.now(UTC)
        # 调用方显式传的优先；没传才用策略。这样既能让设置生效，
        # 又不妨碍单测/特殊场景覆盖。
        qs = self._policy.quiet_start
        qe = self._policy.quiet_end
        quiet_start = quiet_start or time(qs[0], qs[1])
        quiet_end = quiet_end or time(qe[0], qe[1])
        if daily_limit is None:
            daily_limit = self._policy.proactive_daily_limit
        tz, tz_explicit = _resolve_timezone(subject_timezone)
        if not tz_explicit:
            # 时区未知时**直接拦下**，不再往下算。
            #
            # 取舍：勿扰时段的全部意义就是"不在错误的时间打扰人"。不知道
            # 用户时区还照常投递，等于这条保护根本不存在——而用户只会觉得
            # "她怎么这个点找我"，无从判断是配置问题。
            #
            # **不再继续算**也很重要：继续按 UTC 算会让 gate_reasons 里出现
            # "处于勿扰时段（23:00-08:00）"这种我们根本不知道真假的断言。
            # 不知道就只说不不知道的那一件事。
            return GateDecision(
                allowed=False,
                reasons=[
                    "用户时区未设置（LING_USER_TIMEZONE），"
                    "无法判断勿扰时段，已暂停主动联系"
                ],
            )
        reasons: list[str] = []
        if user_opted_out:
            reasons.append("用户已关闭主动联系")
        if actor_busy:
            reasons.append("角色当前正在与用户对话，避免打断")
        local = now.astimezone(tz)
        local_time = local.timetz().replace(tzinfo=None)
        if _in_quiet_hours(local_time, quiet_start, quiet_end):
            reasons.append(f"处于勿扰时段（{quiet_start:%H:%M}-{quiet_end:%H:%M}）")
        if not channel_can_proactive:
            # 调用方能给精确原因就用它。原来这里只有一句
            # 「需账号权限探测通过」，用户会去找一个不存在的平台权限。
            reasons.append(
                channel_proactive_reason or "当前渠道不支持主动投递"
            )
        sent_today = await self._count_sent_today(subject_id, now, tz=tz)
        if sent_today >= daily_limit:
            reasons.append(f"今日主动联系已达上限（{sent_today}/{daily_limit}）")
        return GateDecision(allowed=not reasons, reasons=reasons)

    async def _count_sent_today(
        self, subject_id: str, now: datetime, *, tz: tzinfo | None = None
    ) -> int:
        # "今天"必须是**用户当地**的今天。按 UTC 划分会让跨时区用户的
        # 每日限额在错误时刻重置（例如东八区早上 8 点重置）。
        local_now = now.astimezone(tz) if tz is not None else now
        start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        count = (
            await self._session.execute(
                select(func.count(ProactiveOpportunity.id)).where(
                    ProactiveOpportunity.actor_id == self._actor_id,
                    ProactiveOpportunity.subject_id == subject_id,
                    ProactiveOpportunity.state == "sent",
                    # 按**实际送达时间**计日，不是创建时间。
                    # 23:50 创建、00:10 发送的机会不该占掉前一天的额度。
                    ProactiveOpportunity.sent_at.is_not(None),
                    ProactiveOpportunity.sent_at >= start,
                )
            )
        ).scalar_one()
        return int(count or 0)

    async def mark(
        self,
        opportunity_id: str,
        *,
        state: str,
        reason: str | None = None,
        operation_id: str | None = None,
        delivery_state: str | None = None,
    ) -> None:
        """结算一条机会。

        ``operation_id`` / ``delivery_state`` 是 0027 迁移加的结算字段，之前
        一直是空的：投递失败时只写 ``state`` 和一句文案，事后既查不到"是哪
        一次投递"，也分不清"确定没发"和"可能已经发了"。这两个字段在**失败
        路径**上比成功路径上更重要——成功有回执可查，失败时至少还能对上同一
        轮那条 warning 日志。

        ``operation_id`` 是**本地** operation 行 ID（`DeliveryService` 新建的
        那一条），不是平台侧幂等键：onebot/qqbot/qq 的 ``connector.send`` 都
        不把它透传给平台，平台也无从按它去重或对账。

        ``uncertain`` 与 ``failed`` 是两件事：前者是"平台可能已经收下，结果
        未知，等对账"，后者是"确定没发出去"。两者都**不能回头再发**（发消息
        撤不回），但报给运维的话必须说清是哪一种。

        ``operation_id`` **不保证本地有对应行**：回执写库失败时，那次投递的
        operation / 回执会随 SAVEPOINT 回滚，这里记下的只是本地 operation ID
        的残留。本地查不到对应记录，它只能和同一轮那条 warning
        （``operation=``）对上；**不能**用它向平台去重/对账，也**不代表现在有
        可用的对账**。
        所以这两列是**审计线索**，不是外键——读的一方必须容忍查不到 operation
        （见 `ling.domains.proactive.dispatch` 的「已知限制」）。
        """
        allowed = {"candidate", "sent", "rejected", "expired", "failed", "uncertain"}
        if state not in allowed:
            raise ValueError(f"未知机会状态: {state}")
        row = await self._session.get(ProactiveOpportunity, opportunity_id)
        if row is None:
            raise KeyError(opportunity_id)
        row.state = state
        # 只有真正送到才记送达时间；"标记发送"却没发出时也该留空，
        # 否则限额会算错（详见 0024 迁移说明）。
        if state == "sent" and row.sent_at is None:
            row.sent_at = datetime.now(UTC)
        if reason:
            row.rejected_reason = reason[:500]
        # 结算字段按列宽截断：宁可截断也不能让"记一笔结算"自己抛异常，
        # 那会把一轮投递的回滚范围放大。
        if operation_id:
            row.delivery_operation_id = operation_id[:64]
        if delivery_state:
            row.delivery_state = delivery_state[:32]
        await self._session.flush()

    async def set_draft(self, opportunity_id: str, *, text: str) -> None:
        """写入生成好的开场白。

        单独一个方法而不是塞进 `mark`：草稿和状态是两件事，
        混在一起会让"生成好了但还没发"和"发过了"难以区分。
        """
        row = await self._session.get(ProactiveOpportunity, opportunity_id)
        if row is None:
            raise KeyError(opportunity_id)
        row.draft_text = text[:2000]
        await self._session.flush()

    async def collect_candidates(
        self, *, now: datetime | None = None, shadow: bool = True
    ) -> dict:
        """从可追溯事件收集主动候选。

        ``shadow=True``（默认）只**记录**、不发送。
        先验证候选质量，再决定要不要开真发送——一上来就发会立刻
        变成定时骚扰，而且用户没法判断"它今天为什么突然找我"。

        返回统计，便于 worker 日志与运维观察。
        """
        from ling.domains.proactive.sources import CausalSource, dedupe_key_for

        now = now or utcnow()
        source = CausalSource(self._session, actor_id=self._actor_id)
        candidates = await source.collect(now=now)

        created = 0
        skipped_duplicate = 0
        skipped_not_due = 0
        for candidate in candidates:
            if candidate.not_before > now:
                # 还没到该说的时候：不登记，避免占着候选位
                skipped_not_due += 1
                continue
            row = await self.propose(
                subject_id=candidate.subject_id,
                kind=candidate.reason_code,
                reason=candidate.reason,
                source_event_id=candidate.source_event_id,
                dedupe_key=dedupe_key_for(candidate),
                reason_code=candidate.reason_code,
                not_before=candidate.not_before,
                best_until=candidate.best_until,
                visibility=candidate.visibility,
                reply_expected=candidate.reply_expected,
                channel_hint=candidate.channel_hint,
            )
            if row is None:
                skipped_duplicate += 1
            else:
                created += 1
        return {
            "collected": len(candidates),
            "created": created,
            "skipped_duplicate": skipped_duplicate,
            "skipped_not_due": skipped_not_due,
            "shadow": shadow,
        }

    async def expire_stale(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        rows = list(
            (
                await self._session.execute(
                    select(ProactiveOpportunity).where(
                        ProactiveOpportunity.state == "candidate",
                        ProactiveOpportunity.expires_at <= now,
                    )
                )
            ).scalars()
        )
        for row in rows:
            row.state = "expired"
            row.rejected_reason = "机会已过期（时效性）"
        await self._session.flush()
        return len(rows)

    async def pending(self, subject_id: str | None = None) -> list[OpportunityView]:
        stmt = select(ProactiveOpportunity).where(
            ProactiveOpportunity.actor_id == self._actor_id,
            ProactiveOpportunity.state == "candidate",
        )
        if subject_id:
            stmt = stmt.where(ProactiveOpportunity.subject_id == subject_id)
        rows = list((await self._session.execute(stmt)).scalars())
        return [self._view(r) for r in rows]

    @staticmethod
    def _view(row: ProactiveOpportunity) -> OpportunityView:
        expires = row.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return OpportunityView(
            opportunity_id=row.id,
            subject_id=row.subject_id,
            kind=row.kind,
            reason=row.reason,
            state=row.state,
            expires_at=expires,
            draft_text=row.draft_text,
        )


def _in_quiet_hours(now: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= now < end
    # 跨午夜，例如 23:00 → 08:00
    return now >= start or now < end
