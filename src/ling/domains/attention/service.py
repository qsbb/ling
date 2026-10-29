"""群聊注意力轮询：按频率"刷一下"群，刷到才看。

## 它解决什么

用户需求：「群聊可以以一定频率轮询（模拟人），如果"设定"她目前在看这个群聊，
轮询频率加快（实时聊天但又不是每条都触发 llm 节省成本）」。

在这之前：普通群消息只落库标 `unseen`，**没有任何调度器去刷**——等于永远看不到。

## 三条硬规则

1. **一次轮询最多一次 LLM 调用**。20 条未读也是 1 次。这是省钱的唯一来源，
   不是"优化项"。
2. **先决策成功，再标 `seen`**。顺序反了会在模型失败时凭空"看过"——
   那批消息再也不会被看到，而且没人知道。
3. **失败不推进游标**。未读留在队列里下次再试。

## 聚焦加速为什么不退化成逐条调用

聚焦只让**轮询**变快（8 秒一次），但**决策**仍受每窗口最小 LLM 间隔约束
（默认 15 秒）。20 秒内来 10 条消息 → 可能轮询 2 次，但只有 1 次真调模型。

这条和 `min_llm_interval_seconds` 是绑在一起的：去掉它，聚焦就等于逐条调用。

## 成本可证

`PollResult.baseline_calls` 是"每条消息各调一次"的影子基线，
`llm_calls` 是实际调用数。省了多少可以算，不是靠感觉。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.conversation.service import ConversationService
from ling.infra.models import AttentionCursorRow, utcnow

# ---------------------------------------------------------------- 参数
# 全部是**经验初值，需实测**。设计说明见 docs/GROUP-POLLING-DESIGN.md。

BASE_INTERVAL_SECONDS = 30
"""没在看的群多久刷一次。"""

FOCUS_INTERVAL_SECONDS = 8
"""正在看的群多久刷一次。"""

MIN_LLM_INTERVAL_SECONDS = 15
"""同一窗口两次**模型调用**的最小间隔。

这是"聚焦但不逐条调用"的关键：轮询可以 8 秒一次，决策不行。
"""

FOCUS_TTL_MINUTES = 15
"""「正在看」的租约时长。是状态不是事件。"""

MENTION_WAIT_SECONDS = 8
"""群里被 @ 但没正文时，等多久看有没有补充。经验初值，需实测。"""

MENTION_HARD_DEADLINE_SECONDS = 12
"""等补充的硬截止。超时就用 @ 本身决策，**不能无限等**。"""

MENTION_ONLY_HINT = "（她在群里 @ 了你，没有正文）"
"""只有 @ 没有正文时，给决策用的一句话。"""

RELEVANCE_TTL_MINUTES = 360
"""群聊里的普通消息，超过这么久还没被看到就算**错过**，不再去回。

为什么要这条：她忙一下午，轮询最终还是会把那条 5 小时前的"下午三点开会"
捞出来回一句——真人不会。晚到的回复比没有回复更像故障。

6 小时是经验初值，需实测。私聊不走这条（私聊的沉默由主链路决定）。
"""

MAX_BATCH = 20
"""一次轮询最多看几条。超出的留到下一轮。"""

KEEP_HEAD = 2
KEEP_TAIL = 12
"""超出 MAX_BATCH 时保留首 2 + 末 12。

只留尾部会丢掉"最早说的重要事实"——`_rollup_summary` 踩过这个坑。
"""

LEASE_SECONDS = 60
"""租约时长。多 worker 靠它不重复处理同一窗口。"""

JITTER_SPAN_SECONDS = 6
"""每个窗口的固定抖动（0..JITTER_SPAN_SECONDS 秒）。

为什么要抖动：所有窗口的 `next_due_at` 会在同一时刻被设成"现在 + 间隔"，
不抖动的话它们永远同拍到期，每轮都一起挤进来，先看谁全看数据库返回顺序。

用 **窗口 id 的固定偏移**而不是随机数：同一窗口每次的偏移一致，
行为可预测、可测试；随机数会让"为什么先看这个群"变成不可复现的事。
"""

MAX_WINDOWS_PER_TICK = 5
"""单次 tick 最多处理几个窗口。防止一次 tick 里串行打太多次模型调用。"""


# 决策回调：批次文本 + 发言人 subject（可选）。
#
# 为什么带发言人：群窗口是多个人的，"要不要接"取决于**刚开口的那个人**
# 熟不熟。不带发言人就只能用窗口的 subject——那是主人，不是"对谁"。
Chooser = Callable[..., Awaitable[object]]
"""把「这一批消息」拼成一段文本，交给决策，返回带 .respond/.reason 的对象。"""


@dataclass(frozen=True)
class PollResult:
    window_id: str
    unseen_count: int = 0
    # **决策**调用次数（0 或 1）。
    # 注意：这里只统计"要不要说话"的判断。"说什么"的生成是另一次调用，
    # 发生在决定要回之后——所以省下来的是**决策次数**，不是总调用数。
    # 把两者混为一个数字会让"省了成本"变成一句无法核对的空话。
    llm_calls: int = 0
    # 影子基线：如果"每条消息各调一次决策"会调几次
    baseline_calls: int = 0
    # empty / ignored / replied / failed / skipped
    decision: str = "empty"
    reason: str | None = None

    @property
    def saved_calls(self) -> int:
        return max(0, self.baseline_calls - self.llm_calls)


class GroupAttentionScheduler:
    """群聊轮询的唯一写入方。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        actor_id: str,
        base_interval: int = BASE_INTERVAL_SECONDS,
        focus_interval: int = FOCUS_INTERVAL_SECONDS,
        min_llm_interval: int = MIN_LLM_INTERVAL_SECONDS,
    ) -> None:
        self._session = session
        self._actor_id = actor_id
        self._base = timedelta(seconds=base_interval)
        self._focus = timedelta(seconds=focus_interval)
        self._min_llm = timedelta(seconds=min_llm_interval)

    # ---------------------------------------------------------------- 调度

    async def ensure_cursor(
        self, window_id: str, *, now: datetime | None = None
    ) -> AttentionCursorRow:
        """窗口第一次进入调度时建游标。立即到期，让她尽快看一眼。"""
        moment = now or utcnow()
        row = await self._session.get(AttentionCursorRow, window_id)
        if row is not None:
            return row
        row = AttentionCursorRow(
            window_id=window_id, next_due_at=moment, last_sequence=0
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def due_windows(
        self, *, now: datetime | None = None, limit: int | None = None
    ) -> list[str]:
        """到期待处理的窗口 id，**按该先看谁排好序**。

        优先级（高到低）：

        1. **正在看的群**（`focused_until` 未过期）——她此刻就盯着这个群
        2. **有 @ 待处理的**（`mention_deadline` 未过期）——有人在等回应
        3. 其余按 `next_due_at` 先后（最久没看的先看）

        用 ``SKIP LOCKED`` + 租约：多 worker 同时跑不会重复处理同一个窗口
        （重复处理 = 重复调模型 = 白花钱）。
        """
        moment = now or utcnow()
        effective_limit = min(limit or MAX_WINDOWS_PER_TICK, MAX_WINDOWS_PER_TICK)
        rows = (
            (
                await self._session.execute(
                    select(AttentionCursorRow)
                    .where(
                        AttentionCursorRow.next_due_at <= moment,
                        (AttentionCursorRow.lease_until.is_(None))
                        | (AttentionCursorRow.lease_until <= moment),
                    )
                    .order_by(
                        # 聚焦中的优先。NULL 排最后（Postgres 里 DESC 时
                        # NULL 默认最大，所以要显式把 NULL 压到后面）
                        AttentionCursorRow.focused_until.desc().nulls_last(),
                        AttentionCursorRow.mention_deadline.desc().nulls_last(),
                        AttentionCursorRow.next_due_at.asc(),
                    )
                    .limit(effective_limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        lease_until = moment + timedelta(seconds=LEASE_SECONDS)
        ids: list[str] = []
        for row in rows:
            row.lease_until = lease_until
            row.updated_at = moment
            ids.append(row.window_id)
        if rows:
            await self._session.flush()
        return ids

    async def focus(self, window_id: str, *, now: datetime | None = None) -> None:
        """把某窗口标成"正在看"（带 TTL）。由 @ / 刚回复过 / 显式设定触发。"""
        moment = now or utcnow()
        row = await self.ensure_cursor(window_id, now=moment)
        row.focused_until = moment + timedelta(minutes=FOCUS_TTL_MINUTES)
        row.updated_at = moment
        await self._session.flush()

    async def await_supplement(
        self, window_id: str, *, now: datetime | None = None
    ) -> datetime:
        """群里被 @ 但没正文：把首次轮询推迟，等对方补一句正文。

        返回这一批的实际处理时刻。

        **多次 @ 不累加等待**：只在第一次设定硬截止，之后来的 @ 最多把
        `next_due_at` 推到那个截止。否则连续 @ 会让等待无限延长，
        变成"永远在等补充、永远不回"。
        """
        moment = now or utcnow()
        row = await self.ensure_cursor(window_id, now=moment)

        deadline = row.mention_deadline
        if deadline is not None and deadline.tzinfo is None:
            from datetime import UTC

            deadline = deadline.replace(tzinfo=UTC)

        if deadline is None or deadline <= moment:
            # 新的一轮等待
            deadline = moment + timedelta(seconds=MENTION_HARD_DEADLINE_SECONDS)
            row.mention_deadline = deadline

        target = moment + timedelta(seconds=MENTION_WAIT_SECONDS)
        # 不允许超过硬截止
        row.next_due_at = min(target, deadline)
        # 被 @ 立刻进入"正在看"——这也是聚焦加速的触发条件之一
        row.focused_until = moment + timedelta(minutes=FOCUS_TTL_MINUTES)
        row.updated_at = moment
        await self._session.flush()
        return row.next_due_at

    async def poll_window(
        self,
        window_id: str,
        *,
        chooser: Chooser,
        now: datetime | None = None,
    ) -> PollResult:
        """刷一次这个窗口。

        流程刻意是：读未读 → **先决策** → 成功后才标 seen → 推进游标。
        任何一步失败都不写"看过"。
        """
        moment = now or utcnow()
        conversation = ConversationService(
            self._session, actor_id=self._actor_id
        )
        unseen = await conversation.unseen_messages(window_id, limit=MAX_BATCH)
        cursor = await self.ensure_cursor(window_id, now=moment)

        if not unseen:
            cursor.next_due_at = moment + self._interval(cursor, moment)
            cursor.updated_at = moment
            await self._session.flush()
            return PollResult(window_id=window_id, decision="empty")

        # 冷却：同一窗口两次模型调用之间必须隔开。
        # 没有这条，聚焦就等于逐条调用，省钱无从谈起。
        if cursor.last_call_at is not None:
            last = cursor.last_call_at
            if last.tzinfo is None:
                from datetime import UTC

                last = last.replace(tzinfo=UTC)
            if moment - last < self._min_llm:
                cursor.next_due_at = last + self._min_llm
                await self._session.flush()
                return PollResult(
                    window_id=window_id,
                    unseen_count=len(unseen),
                    baseline_calls=len(unseen),
                    decision="skipped",
                )

        # 先丢掉过期的：她忙了一下午，不该现在去回那条 5 小时前的
        # "下午三点开会"。晚到的回复比没有回复更像故障。
        fresh, stale = self._split_stale(unseen, moment)
        if stale and not fresh:
            for message in stale:
                await conversation.mark_handled(
                    message.message_id,
                    handled_as="missed",
                    handled_reason="过了时效，没来得及看",
                )
            await conversation.mark_missed(
                window_id, message_ids=[m.message_id for m in stale]
            )
            cursor.last_sequence = max(
                int(cursor.last_sequence or 0), int(stale[-1].sequence or 0)
            )
            cursor.updated_at = moment
            cursor.next_due_at = moment + self._interval(cursor, moment)
            cursor.lease_until = None
            await self._session.flush()
            missed = PollResult(
                window_id=window_id,
                unseen_count=len(stale),
                llm_calls=0,
                baseline_calls=len(stale),
                decision="missed",
                reason="过了时效，没来得及看",
            )
            await self._log(missed, moment)
            return missed
        if stale:
            for message in stale:
                await conversation.mark_handled(
                    message.message_id,
                    handled_as="missed",
                    handled_reason="过了时效，没来得及看",
                )
            await conversation.mark_missed(
                window_id, message_ids=[m.message_id for m in stale]
            )
        unseen = fresh

        # 标上「谁说的」，并按**最近说话的人**取关系。
        # 群窗口是多个人的：不标发言人，她只能看到一堆没主的话；
        # 不按发言人取关系，就只能用窗口的 subject（那是主人，不是"对谁"）。
        senders = [m.sender_subject_id for m in unseen]
        names = await _sender_names(self._session, senders)
        batch_text = (
            build_batch_text(
                [m.text for m in unseen], names=names, senders=senders
            )
            or MENTION_ONLY_HINT
        )
        speaker = next(
            (s for s in reversed(senders) if s), None
        )

        try:
            choice = await chooser(batch_text, speaker_subject_id=speaker)
        except Exception:  # noqa: BLE001 - 任何失败都不能写"看过"
            # 不推进游标、不标 seen：未读留在队列里下次再试
            cursor.next_due_at = moment + self._base
            await self._session.flush()
            failed = PollResult(
                window_id=window_id,
                unseen_count=len(unseen),
                llm_calls=1,
                baseline_calls=len(unseen),
                decision="failed",
                reason="模型调用失败",
            )
            await self._log(failed, moment)
            return failed

        respond = bool(getattr(choice, "respond", False))
        reason = str(getattr(choice, "reason", "") or "")
        ids = [m.message_id for m in unseen]

        # 决策成功之后才标已看
        await conversation.mark_seen(window_id, message_ids=ids)
        target = unseen[-1].message_id
        for message in unseen:
            if respond and message.message_id == target:
                await conversation.mark_handled(
                    message.message_id, handled_as="replied", handled_reason=reason
                )
            else:
                await conversation.mark_handled(
                    message.message_id, handled_as="ignored", handled_reason=reason
                )

        cursor.last_sequence = max(
            int(cursor.last_sequence or 0), int(unseen[-1].sequence or 0)
        )
        cursor.last_call_at = moment
        cursor.updated_at = moment
        cursor.next_due_at = moment + self._interval(cursor, moment)
        cursor.lease_until = None
        await self._session.flush()

        result = PollResult(
            window_id=window_id,
            unseen_count=len(unseen),
            llm_calls=1,
            baseline_calls=len(unseen),
            decision="replied" if respond else "ignored",
            reason=reason,
        )
        await self._log(result, moment)
        return result

    def _split_stale(self, unseen: list, moment: datetime) -> tuple[list, list]:
        """按时效把未读分成（还没过期的，过了期的）。

        `created_at` 缺失时**当作新鲜**——宁可多看一眼，也不要因为读不到
        时间戳就把消息判成错过（那会静默吞掉她的消息）。
        """
        cutoff = moment - timedelta(minutes=RELEVANCE_TTL_MINUTES)
        fresh: list = []
        stale: list = []
        for message in unseen:
            created = getattr(message, "created_at", None)
            if created is None:
                fresh.append(message)
                continue
            if created.tzinfo is None:
                from datetime import UTC

                created = created.replace(tzinfo=UTC)
            (stale if created < cutoff else fresh).append(message)
        return fresh, stale

    async def _log(self, result: PollResult, moment: datetime) -> None:
        """记一次轮询。

        只在**有未读**（或失败）时写：空轮询每 30 秒一次，写库会淹掉表，
        而它什么信息都不含——成本已经在 `llm_calls == 0` 里体现了。
        """
        from ling.infra.models import AttentionPollLogRow
        from ling.infra.security import new_id

        self._session.add(
            AttentionPollLogRow(
                id=new_id("poll"),
                window_id=result.window_id,
                polled_at=moment,
                unseen_count=result.unseen_count,
                llm_calls=result.llm_calls,
                baseline_calls=result.baseline_calls,
                decision=result.decision,
                reason=result.reason,
            )
        )
        await self._session.flush()

    def _interval(self, cursor: AttentionCursorRow, moment: datetime) -> timedelta:
        """在看的群刷得勤，否则按基础间隔。两者都加**每窗口固定抖动**。"""
        base = self._base
        focused_until = cursor.focused_until
        if focused_until is not None:
            if focused_until.tzinfo is None:
                from datetime import UTC

                focused_until = focused_until.replace(tzinfo=UTC)
            if focused_until > moment:
                base = self._focus
        return base + timedelta(seconds=_jitter_seconds(cursor.window_id))


def _jitter_seconds(window_id: str) -> int:
    """每个窗口固定的 0..JITTER_SPAN_SECONDS 偏移。

    **确定性**，不用随机数：同一个窗口每次都一样，行为可复现。
    随机数会让"为什么先看这个群"变成无法复现的事。
    """
    import zlib

    return zlib.crc32(window_id.encode("utf-8")) % (JITTER_SPAN_SECONDS + 1)


async def recent_actor_turns(
    session: AsyncSession, window_id: str, *, minutes: int = 5
) -> int:
    """该窗口近期她已发言次数（防刷屏用）。

    轮询路径原来把这个写死成 0，等于**防刷屏上限永远不触发**——
    热门群她可以每 15 秒回一次。这个函数就是为了不再写死。
    """
    from datetime import timedelta

    from sqlalchemy import func

    from ling.infra.models import Message

    cutoff = utcnow() - timedelta(minutes=minutes)
    return int(
        (
            await session.execute(
                select(func.count(Message.id)).where(
                    Message.window_id == window_id,
                    Message.role == "actor",
                    Message.created_at >= cutoff,
                )
            )
        ).scalar()
        or 0
    )


def make_group_chooser(
    *,
    session: AsyncSession,
    actor_id: str,
    window_id: str,
    router: object,
    config_reader: object,
    persona_id: str = "persona_ling",
):
    """构造轮询用的决策回调。

    把 `DecisionContext` 的构造**收在这里**，而不是让每个调用方自己拼——
    worker 里原来那段是硬编码 `mentioned=False, recent_actor_turns=0`，
    结果防刷屏上限在轮询路径上永远不生效。收进来就不会再漏。
    """

    async def _choose(batch_text: str, speaker_subject_id: str | None = None):  # noqa: ANN202
        from ling.kernel.autonomy import (
            AutonomyTurnService,
            make_persona_reader,
            make_situation_reader,
        )
        from ling.kernel.decision import DecisionContext

        effective = await config_reader.effective()  # type: ignore[attr-defined]
        budget = int(effective.dialogue.group_turn_budget)
        recent = await recent_actor_turns(session, window_id)
        ctx = DecisionContext(
            is_group=True,
            # unseen 队列里的消息按构造都是"没点名"的（被 @ 的走即时路径），
            # 但仍然如实计算，不写死——写死过一次就够教训了。
            mentioned=False,
            addressed_to_actor=False,
            is_question=False,
            recent_actor_turns=recent,
            has_meaningful_text=True,
            group_turn_budget=budget,
        )
        return await AutonomyTurnService(
            router=router,
            config_reader=config_reader,
            situation_reader=make_situation_reader(
                session,
                actor_id=actor_id,
                window_id=window_id,
                # 群聊里按**最近说话的人**取关系：要不要接这句，
                # 取决于"刚开口的这个人"熟不熟。群窗口本身的 subject
                # 是主人（这个群属于谁），不能拿来当"对谁"。
                subject_id=speaker_subject_id,
            ),
            persona_reader=make_persona_reader(
                session, persona_id=persona_id
            ),
        ).choose(ctx, text=batch_text)

    return _choose


async def _sender_names(
    session: AsyncSession, senders: list[str | None]
) -> dict[str, str]:
    """发言人 subject → 显示名。查不到的就不标，不编一个假名字。"""
    from ling.infra.models import Subject

    wanted = {s for s in senders if s}
    if not wanted:
        return {}
    rows = (
        await session.execute(select(Subject).where(Subject.id.in_(list(wanted))))
    ).scalars().all()
    return {r.id: (r.display_name or "") for r in rows if r.display_name}


def build_batch_text(
    texts: list[str],
    *,
    limit: int = MAX_BATCH,
    names: dict[str, str] | None = None,
    senders: list[str | None] | None = None,
) -> str:
    """把一批消息拼成一段给决策用的文本。

    ``names``/``senders`` 给了就在每行前面标上是**谁**说的——
    群窗口是多个人的，不标的话她只能看到一堆没主的话，
    既没法回指（"你刚才问的"），也没法按人分亲疏。

    超出上限时保留**首 2 + 末 12**：最早的常是"第一次说的重要事实"，
    最后的接当前话题。只留尾部会丢掉最初的约定。
    """
    raw: list[tuple[str, str | None]] = [
        (text.strip(), (senders[i] if senders and i < len(senders) else None))
        for i, text in enumerate(texts)
        if text and text.strip()
    ]
    if not raw:
        return ""

    def _label(text: str, sender: str | None) -> str:
        if not sender or not names:
            return text
        name = names.get(sender)
        return f"{name}：{text}" if name else text

    cleaned = [_label(text, sender) for text, sender in raw]
    if len(cleaned) <= limit:
        return "\n".join(cleaned)
    omitted = len(cleaned) - KEEP_HEAD - KEEP_TAIL
    head = cleaned[:KEEP_HEAD]
    tail = cleaned[-KEEP_TAIL:]
    return "\n".join([*head, f"…（中间还有 {omitted} 条）…", *tail])


__all__ = [
    "BASE_INTERVAL_SECONDS",
    "MENTION_HARD_DEADLINE_SECONDS",
    "MENTION_ONLY_HINT",
    "MENTION_WAIT_SECONDS",
    "FOCUS_INTERVAL_SECONDS",
    "FOCUS_TTL_MINUTES",
    "KEEP_HEAD",
    "KEEP_TAIL",
    "LEASE_SECONDS",
    "JITTER_SPAN_SECONDS",
    "MAX_BATCH",
    "RELEVANCE_TTL_MINUTES",
    "MAX_WINDOWS_PER_TICK",
    "MIN_LLM_INTERVAL_SECONDS",
    "GroupAttentionScheduler",
    "make_group_chooser",
    "recent_actor_turns",
    "PollResult",
    "build_batch_text",
]
