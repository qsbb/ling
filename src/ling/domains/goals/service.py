"""Goals 域：目标、承诺与履约状态。

唯一写入方。硬规则（设计文档 §1 第 6 条 / §5）：
- 承诺是**持久任务**，直到完成/取消/失败。
- 到期投递必须记录真实回执；平台不支持主动投递时标"无法投递"，不能记成已发送。
- 改期会让旧定时任务失效（由 due_at + state 派生，不需要额外取消）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.api.errors import not_found
from ling.infra.models import CommitmentRow, GoalRow, Window, utcnow
from ling.infra.security import new_id

COMMITMENT_STATES = {"active", "delivered", "missed", "cancelled", "undeliverable"}
# 仍在等履约的状态。``delivered`` 已完成、``missed``/``cancelled``/
# ``undeliverable`` 是已经结束的失败/撤回——它们都是事实，需要可查，
# 但不属于"还欠着对方的事"，默认可见集不能把它们算进来。
PENDING_COMMITMENT_STATES = ("active",)


@dataclass(frozen=True)
class GoalView:
    goal_id: str
    title: str
    horizon: str
    state: str
    progress: float
    note: str | None


@dataclass(frozen=True)
class CommitmentView:
    commitment_id: str
    subject_id: str
    text: str
    due_at: datetime
    timezone: str
    state: str
    delivery_operation_id: str | None
    delivery_state: str | None
    # 取消原因（撤回承诺时要能解释"为什么没做到"）
    cancel_reason: str | None = None
    # 承诺**属于哪个会话**（`Window.id`，不是平台 `window_ref`）。
    #
    # 必须交出去：到期投递要知道"发到哪"。此前视图不含这一项，而 worker
    # 的 handler 又只读 payload 里一个**生产端从不写**的
    # `proactive_window_ref`，于是每条到期承诺都拿不到会话定位、被标
    # `unsupported` → 行状态推进到 `undeliverable`；而
    # `due_commitments()` 只取 `active`——承诺一旦到期就永久失效。
    # 这个值在库里一直存着（`CommitmentRow.window_id`），只是没被读出来。
    window_id: str | None = None


class GoalsService:
    def __init__(self, session: AsyncSession, *, actor_id: str = "actor_ling") -> None:
        self._session = session
        self._actor_id = actor_id

    # ---------------------------------------------------------- 目标

    async def create_goal(
        self, *, title: str, horizon: str = "short", note: str | None = None
    ) -> GoalView:
        row = GoalRow(
            id=new_id("goal"),
            actor_id=self._actor_id,
            title=title.strip()[:200],
            horizon=horizon if horizon in {"short", "long"} else "short",
            state="active",
            progress=0.0,
            note=note,
        )
        self._session.add(row)
        await self._session.flush()
        return self._goal_view(row)

    async def update_goal(
        self, goal_id: str, *, progress: float | None = None, state: str | None = None
    ) -> GoalView:
        row = await self._session.get(GoalRow, goal_id)
        if row is None or row.actor_id != self._actor_id:
            raise KeyError(f"目标不存在: {goal_id}")
        if progress is not None:
            row.progress = max(0.0, min(1.0, progress))
        if state is not None and state in {"active", "done", "dropped"}:
            row.state = state
        row.updated_at = utcnow()
        await self._session.flush()
        return self._goal_view(row)

    async def list_goals(self, *, state: str | None = None) -> list[GoalView]:
        stmt = select(GoalRow).where(GoalRow.actor_id == self._actor_id)
        if state:
            stmt = stmt.where(GoalRow.state == state)
        rows = list((await self._session.execute(stmt)).scalars())
        return [self._goal_view(r) for r in rows]

    # ---------------------------------------------------------- 承诺

    async def _ensure_deliverable_window(self, *, window_id: str, subject_id: str) -> None:
        """校验 ``window_id`` 可以作为这条承诺的投递目标。

        为什么创建时就要校验：A3 起 ``CommitmentRow.window_id`` 不再只是
        惰性字段，worker 会把它解析成平台 ``window_ref``（``Window.title``）
        并**真的投递**。若这里不校验归属，任何已登录用户只要能拿到别人的
        ``Window.id``，就能让角色把任意文本发进对方私聊。

        归属判断与 ``ConversationService.get_window`` 一致：窗口不存在与
        窗口不属于当前 subject 一律 404，不泄露"这个 id 存在"。
        ``actor_id`` 也一并校验——会话属于某个 actor，不能被另一个 actor
        的承诺借道；当前所有窗口都由本 actor 创建，这项检查不会误伤。

        群窗口在**创建时**就拒绝：投递侧（``_commitment_window_ref``）虽然
        也会挡 ``group_scope``，但等到到期才失败会把一条承诺变成
        ``undeliverable``——用户以为提醒设好了，实际永远不会发。早失败
        比晚失败清楚。
        """
        window = await self._session.get(Window, window_id)
        if (
            window is None
            or window.actor_id != self._actor_id
            or window.subject_id != subject_id
        ):
            raise not_found("会话不存在")
        if window.archived:
            raise ValueError(
                "会话已归档，不能作为承诺提醒的投递目标；"
                "请选择未归档的会话，或留空使用最近私聊"
            )
        if window.visibility == "group_scope":
            raise ValueError("承诺提醒只能发往私聊会话，群聊窗口不能作为投递目标")

    async def create_commitment(
        self,
        *,
        subject_id: str,
        text: str,
        due_at: datetime,
        timezone: str = "Asia/Shanghai",
        window_id: str | None = None,
        source_event_ids: list[str] | None = None,
    ) -> CommitmentView:
        if due_at.tzinfo is None:
            raise ValueError("承诺到期时间必须带时区")
        # 空 window_id 保持原语义：到期时回落到"这个人最近活跃的同渠道私聊"。
        # 只有显式指定了窗口才校验；空值（None / ``""``）不是错误——``""``
        # 在旧数据与客户端里都按"没指定"处理，worker 也是按真值判断回落的。
        if window_id:
            await self._ensure_deliverable_window(
                window_id=window_id, subject_id=subject_id
            )
        row = CommitmentRow(
            id=new_id("cmt"),
            actor_id=self._actor_id,
            subject_id=subject_id,
            window_id=window_id,
            text=text.strip()[:1000],
            due_at=due_at,
            timezone=timezone,
            state="active",
            source_event_ids=list(source_event_ids or []),
        )
        self._session.add(row)
        await self._session.flush()
        return self._commitment_view(row)

    async def reschedule(self, commitment_id: str, new_due: datetime) -> CommitmentView:
        """改期：旧时间失效，仍是同一条承诺（保留证据链）。"""
        if new_due.tzinfo is None:
            raise ValueError("改期时间必须带时区")
        row = await self._session.get(CommitmentRow, commitment_id)
        if row is None or row.actor_id != self._actor_id:
            raise KeyError(f"承诺不存在: {commitment_id}")
        if row.state in {"cancelled"}:
            raise ValueError("已取消的承诺不能改期")
        row.due_at = new_due
        row.state = "active"
        row.delivery_operation_id = None
        row.delivery_state = None
        row.updated_at = utcnow()
        await self._session.flush()
        return self._commitment_view(row)

    async def cancel(self, commitment_id: str, *, reason: str | None = None) -> CommitmentView:
        """取消承诺（终态）。

        ``COMMITMENT_STATES`` 里声明了 ``cancelled``，``reschedule`` 也检查
        "已取消不能改期"，但**原先没有任何方法能把它设成 cancelled**——
        用户无法撤回一个自己许下的承诺，取消是条死状态。

        取消保留记录（不删除）：承诺是"我说过要做的事"，
        撤回本身也是事实，需要可解释。
        """
        row = await self._session.get(CommitmentRow, commitment_id)
        if row is None or row.actor_id != self._actor_id:
            raise KeyError(f"承诺不存在: {commitment_id}")
        if row.state == "delivered":
            raise ValueError("已履行的承诺不能取消")
        if row.state == "cancelled":
            return self._commitment_view(row)  # 幂等
        row.state = "cancelled"
        row.delivery_state = "cancelled"
        row.updated_at = utcnow()
        if reason:
            row.cancel_reason = reason[:500]
        await self._session.flush()
        return self._commitment_view(row)

    async def mark_delivery(
        self,
        commitment_id: str,
        *,
        operation_id: str | None,
        delivery_state: str,
    ) -> CommitmentView:
        """记录投递结果。只有 ``sent``/``delivered`` 才算已履行。"""
        row = await self._session.get(CommitmentRow, commitment_id)
        if row is None or row.actor_id != self._actor_id:
            raise KeyError(f"承诺不存在: {commitment_id}")
        row.delivery_operation_id = operation_id
        row.delivery_state = delivery_state
        if delivery_state in {"sent", "delivered"}:
            row.state = "delivered"
        elif delivery_state == "unsupported":
            row.state = "undeliverable"
        elif delivery_state == "failed":
            row.state = "missed"
        row.updated_at = utcnow()
        await self._session.flush()
        return self._commitment_view(row)

    async def due_commitments(self, *, within: timedelta = timedelta(0)) -> list[CommitmentView]:
        """到期且仍 active 的承诺（供 worker 调度）。"""
        cutoff = datetime.now(UTC) + within
        rows = list(
            (
                await self._session.execute(
                    select(CommitmentRow)
                    .where(
                        CommitmentRow.actor_id == self._actor_id,
                        CommitmentRow.state == "active",
                        CommitmentRow.due_at <= cutoff,
                    )
                    .order_by(CommitmentRow.due_at)
                )
            ).scalars()
        )
        return [self._commitment_view(r) for r in rows]

    async def current_commitment(self, commitment_id: str) -> CommitmentView:
        """读单条承诺当前状态（用于验证投递是否真的推进了）。"""
        row = await self._session.get(CommitmentRow, commitment_id)
        if row is None or row.actor_id != self._actor_id:
            raise KeyError(f"承诺不存在: {commitment_id}")
        return self._commitment_view(row)

    async def list_commitments(
        self, subject_id: str | None = None, *, state: str | None = None
    ) -> list[CommitmentView]:
        """列出承诺。**默认只返回仍待履行的 ``active``**。

        契约变更（2026-10）：此前这里不加任何 ``state`` 过滤，于是
        ``cancelled`` / ``undeliverable`` 会和待履行的一起返回，
        "已取消的事"在界面上长得和"还欠着的事"一模一样。现在默认可见集
        只含 ``PENDING_COMMITMENT_STATES``；历史状态仍保留在库里，
        需要时显式查询：

        - ``state=None``（默认）→ ``active``
        - ``state="cancelled"`` / ``"delivered"`` / ``"missed"`` /
          ``"undeliverable"`` → 该状态的记录
        - ``state="all"`` → 全部状态（含历史）

        非法状态抛 ``ValueError``（API 层翻译成 422），不静默返回空列表——
        拼错状态却看到空结果会被误读成"真没有"。
        """
        if state is not None and state != "all" and state not in COMMITMENT_STATES:
            raise ValueError(
                f"未知承诺状态: {state}（可选: {', '.join(sorted(COMMITMENT_STATES))}, all）"
            )
        stmt = select(CommitmentRow).where(CommitmentRow.actor_id == self._actor_id)
        if subject_id:
            stmt = stmt.where(CommitmentRow.subject_id == subject_id)
        if state is None:
            stmt = stmt.where(CommitmentRow.state.in_(PENDING_COMMITMENT_STATES))
        elif state != "all":
            stmt = stmt.where(CommitmentRow.state == state)
        stmt = stmt.order_by(CommitmentRow.due_at)
        rows = list((await self._session.execute(stmt)).scalars())
        return [self._commitment_view(r) for r in rows]

    @staticmethod
    def _goal_view(row: GoalRow) -> GoalView:
        return GoalView(
            goal_id=row.id,
            title=row.title,
            horizon=row.horizon,
            state=row.state,
            progress=float(row.progress),
            note=row.note,
        )

    @staticmethod
    def _commitment_view(row: CommitmentRow) -> CommitmentView:
        due = row.due_at
        if due.tzinfo is None:
            due = due.replace(tzinfo=UTC)
        return CommitmentView(
            commitment_id=row.id,
            subject_id=row.subject_id,
            text=row.text,
            due_at=due,
            timezone=row.timezone,
            state=row.state,
            delivery_operation_id=row.delivery_operation_id,
            delivery_state=row.delivery_state,
            cancel_reason=row.cancel_reason,
            window_id=row.window_id,
        )
