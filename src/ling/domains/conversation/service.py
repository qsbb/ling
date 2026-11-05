"""Conversation 域：窗口、轮次、消息、窗口短记忆、交接胶囊。

唯一写入方。浏览器缓存不是事实源；所有历史以本域的表为准。

幂等：
- 轮次用 ``(window_id, client_turn_id)`` 唯一约束去重。
- 入站外部事件用 ``IdempotencyKey(scope=connector:instance, key=external_event_id)``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.api.errors import not_found
from ling.contracts.v1 import ChatMessage, ErrorCode, Origin, SessionRecord, TurnState
from ling.infra.models import (
    HandoffCapsuleRow,
    IdempotencyKey,
    Message,
    Turn,
    Window,
    WindowContext,
    utcnow,
)
from ling.infra.security import new_id

DEFAULT_WINDOW_TITLE = "新的对话"
MAX_TITLE_LEN = 60


@dataclass(frozen=True)
class MessageView:
    message_id: str
    turn_id: str | None
    role: str
    text: str
    state: TurnState
    created_at: datetime
    sequence: int
    origin: Origin
    error_code: ErrorCode | None
    model_label: str | None
    attempt: int = 1
    # 她怎么处置的。沉默时要让前端能解释，不能让消息看起来凭空消失。
    handled_as: str | None = None
    handled_reason: str | None = None
    # 谁说的（群聊里必须有，否则"刚才那句是谁说的"无从得知）
    sender_subject_id: str | None = None

    def to_contract(self) -> ChatMessage:
        return ChatMessage(
            message_id=self.message_id,
            turn_id=self.turn_id,
            role=self.role,  # type: ignore[arg-type]
            text=self.text,
            state=self.state,
            created_at=self.created_at,
            sequence=self.sequence,
            origin=self.origin,
            error_code=self.error_code,
            model_label=self.model_label,
            attempt=self.attempt,
            handled_as=self.handled_as,
            handled_reason=self.handled_reason,
        )


@dataclass(frozen=True)
class TurnView:
    turn_id: str
    state: TurnState
    attempt: int
    model_label: str | None
    error_code: ErrorCode | None
    retriable: bool
    partial_text: str = ""
    """流式中已产出的文本（来自当前进程的内存缓冲）。

    刷新页面后前端用它继续显示，而不是只看到"服务端仍在生成"。
    进程重启后为空——那时轮次会被标记 failed 并允许重试。
    """

    def to_dict(self) -> dict[str, object]:
        return {
            "turn_id": self.turn_id,
            "state": self.state.value,
            "attempt": self.attempt,
            "model_label": self.model_label,
            "error_code": self.error_code.value if self.error_code else None,
            "retriable": self.retriable,
            "partial_text": self.partial_text,
        }


RETRIABLE_STATES = {TurnState.failed, TurnState.stopped}


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class ConversationService:
    def __init__(
        self, session: AsyncSession, *, actor_id: str = "actor_ling", persona_id: str = "persona_ling"
    ) -> None:
        self._session = session
        self._actor_id = actor_id
        self._persona_id = persona_id

    @property
    def session(self) -> AsyncSession:
        """本域使用的数据库会话（供内核在同事务内提交角色事件）。"""
        return self._session

    # ---------------------------------------------------------- 窗口

    async def create_window(
        self, *, subject_id: str, title: str | None = None, channel: str = "web"
    ) -> Window:
        window = Window(
            id=new_id("win"),
            actor_id=self._actor_id,
            persona_id=self._persona_id,
            subject_id=subject_id,
            title=(title or DEFAULT_WINDOW_TITLE)[:MAX_TITLE_LEN],
            channel=channel,
            visibility="subject_private",
        )
        self._session.add(window)
        await self._session.flush()
        return window

    async def list_windows(self, subject_id: str, *, include_archived: bool = False) -> list[Window]:
        stmt = select(Window).where(Window.subject_id == subject_id)
        if not include_archived:
            stmt = stmt.where(Window.archived.is_(False))
        stmt = stmt.order_by(Window.updated_at.desc(), Window.created_at.desc())
        return list((await self._session.execute(stmt)).scalars())

    async def get_window(self, window_id: str, *, subject_id: str) -> Window:
        """读取窗口并校验归属；跨用户一律 404（不泄露存在性）。"""
        window = await self._session.get(Window, window_id)
        if window is None or window.subject_id != subject_id:
            raise not_found("会话不存在")
        return window

    async def get_window_for_turn(self, window_id: str, *, subject_id: str) -> Window:
        """轮次用的窗口读取：**群窗口对所有成员开放**，私聊仍严格校验归属。

        ## 为什么需要单独一个方法

        `get_window` 要求 `window.subject_id == subject_id`。这对私聊是对的
        （跨用户一律 404，不泄露存在性），但**群聊会误伤**：

        群窗口按设计归属主人（这个群属于谁），而轮次是用**发言人的** subject
        跑的。于是群里任何人（不是主人）说话时，一旦走到生成阶段就
        `not_found("会话不存在")`，整轮失败——她"决定回复了但什么都没发出来"。

        实测复现（2026-10-29）：群里陌生人求助 →
        `{"decision":"reply","delivered":false,"state":"failed"}`。

        群窗口本来就是共享的（同一个群所有人落到一个窗口），所以放行不降低
        隐私——私聊窗口仍然只有本人能读。
        """
        window = await self._session.get(Window, window_id)
        if window is None:
            raise not_found("会话不存在")
        if window.subject_id == subject_id:
            return window
        if window.visibility == "group_scope":
            return window
        raise not_found("会话不存在")

    async def update_window(
        self,
        window_id: str,
        *,
        subject_id: str,
        title: str | None = None,
        archived: bool | None = None,
    ) -> Window:
        window = await self.get_window(window_id, subject_id=subject_id)
        if title is not None:
            window.title = title.strip()[:MAX_TITLE_LEN] or DEFAULT_WINDOW_TITLE
        if archived is not None:
            window.archived = archived
        window.updated_at = utcnow()
        await self._session.flush()
        return window

    async def ensure_default_window(self, subject_id: str) -> Window:
        windows = await self.list_windows(subject_id)
        if windows:
            return windows[0]
        return await self.create_window(subject_id=subject_id)

    @staticmethod
    def to_session_record(window: Window) -> SessionRecord:
        return SessionRecord(
            session_id=window.id,
            actor_id=window.actor_id,
            subject_id=window.subject_id,
            window_id=window.id,
            title=window.title,
            created_at=_as_utc(window.created_at),
            updated_at=_as_utc(window.updated_at),
            message_count=window.message_count or 0,
            archived=bool(window.archived),
        )

    # ---------------------------------------------------------- 轮次

    async def find_turn(self, window_id: str, client_turn_id: str) -> Turn | None:
        return (
            await self._session.execute(
                select(Turn).where(
                    Turn.window_id == window_id, Turn.client_turn_id == client_turn_id
                )
            )
        ).scalar_one_or_none()

    async def create_turn(
        self,
        window_id: str,
        *,
        client_turn_id: str,
        config_revision: int,
        model_label: str | None = None,
    ) -> Turn:
        turn = Turn(
            id=new_id("turn"),
            window_id=window_id,
            client_turn_id=client_turn_id[:128],
            state=TurnState.pending.value,
            config_revision=config_revision,
            model_label=model_label,
            attempt=1,
        )
        self._session.add(turn)
        await self._session.flush()
        return turn

    async def set_turn_state(
        self,
        turn_id: str,
        state: TurnState,
        *,
        error_code: ErrorCode | None = None,
        error_detail: str | None = None,
        model_label: str | None = None,
    ) -> Turn:
        turn = await self._session.get(Turn, turn_id)
        if turn is None:
            raise not_found("轮次不存在")
        turn.state = state.value
        turn.error_code = error_code.value if error_code else None
        turn.error_detail = error_detail
        if model_label is not None:
            turn.model_label = model_label
        turn.updated_at = utcnow()
        await self._session.flush()
        return turn

    async def transition_turn_if(
        self, turn_id: str, *, from_states: set[TurnState], to_state: TurnState
    ) -> bool:
        """条件状态迁移：只有当前状态在 ``from_states`` 里才写入。

        用于停止信号这类"并发写"：不能覆盖已经由协调器提交的终态
        （否则 stopped 会被 stopping 盖回去，导致该轮永远不可重试）。
        """
        from sqlalchemy import update

        result = await self._session.execute(
            update(Turn)
            .where(
                Turn.id == turn_id,
                Turn.state.in_([s.value for s in from_states]),
            )
            .values(state=to_state.value, updated_at=utcnow())
        )
        await self._session.flush()
        return bool(getattr(result, "rowcount", 0))

    async def bump_attempt(self, turn_id: str) -> Turn:
        turn = await self._session.get(Turn, turn_id)
        if turn is None:
            raise not_found("轮次不存在")
        # 同一会话里可能已加载过该实例（如路由先 get_turn 做校验），
        # 直接 `expire` 保证 attempt 基于数据库当前值递增，而不是内存旧值。
        await self._session.refresh(turn)
        turn.attempt = int(turn.attempt or 1) + 1
        turn.state = TurnState.pending.value
        turn.error_code = None
        turn.error_detail = None
        turn.updated_at = utcnow()
        await self._session.flush()
        return turn

    async def get_turn(self, turn_id: str) -> Turn | None:
        return await self._session.get(Turn, turn_id)

    async def turn_view(self, turn: Turn, *, partial_text: str = "") -> TurnView:
        state = TurnState(turn.state)
        code = ErrorCode(turn.error_code) if turn.error_code else None
        return TurnView(
            turn_id=turn.id,
            state=state,
            attempt=int(turn.attempt or 1),
            model_label=turn.model_label,
            error_code=code,
            retriable=state in RETRIABLE_STATES,
            partial_text=partial_text,
        )

    # ---------------------------------------------------------- 消息

    async def next_sequence(self, window_id: str) -> int:
        current = (
            await self._session.execute(
                select(func.max(Message.sequence)).where(Message.window_id == window_id)
            )
        ).scalar_one_or_none()
        return int(current or 0) + 1

    async def add_message(
        self,
        window_id: str,
        *,
        role: str,
        text: str,
        turn_id: str | None = None,
        state: TurnState = TurnState.completed,
        origin: Origin = Origin.user_statement,
        error_code: ErrorCode | None = None,
        model_label: str | None = None,
        sequence: int | None = None,
        attempt: int = 1,
        handled_as: str | None = None,
        handled_reason: str | None = None,
        attention_state: str | None = None,
        sender_subject_id: str | None = None,
    ) -> MessageView:
        seq = sequence if sequence is not None else await self.next_sequence(window_id)
        row = Message(
            id=new_id("msg"),
            window_id=window_id,
            turn_id=turn_id,
            role=role,
            text=text,
            state=state.value,
            sequence=seq,
            origin=origin.value,
            error_code=error_code.value if error_code else None,
            model_label=model_label,
            attempt=attempt,
            handled_as=handled_as,
            handled_reason=handled_reason,
            attention_state=attention_state,
            sender_subject_id=sender_subject_id,
            seen_at=utcnow() if attention_state == "seen" else None,
        )
        self._session.add(row)
        window = await self._session.get(Window, window_id)
        if window is not None:
            window.message_count = int(window.message_count or 0) + 1
            window.last_message_at = utcnow()
            window.updated_at = utcnow()
            if window.title == DEFAULT_WINDOW_TITLE and role == "user" and text.strip():
                window.title = text.strip().replace("\n", " ")[:MAX_TITLE_LEN]
        await self._session.flush()
        return self._to_view(row)

    async def unseen_messages(
        self, window_id: str, *, limit: int = 20
    ) -> list[MessageView]:
        """这个窗口里**还没看到**的用户消息，按时间正序。

        只取 ``role='user'``：她自己说的话不存在"没看见"。
        历史记录 ``attention_state`` 为 NULL 的**不算未读**——
        那时还没有这个字段，无法判断，武断当成未读会让她突然"补看"一批老消息。
        """
        rows = (
            (
                await self._session.execute(
                    select(Message)
                    .where(
                        Message.window_id == window_id,
                        Message.role == "user",
                        Message.attention_state == "unseen",
                    )
                    .order_by(Message.sequence)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [self._to_view(r) for r in rows]

    async def mark_seen(self, window_id: str, *, message_ids: list[str]) -> int:
        """把这些消息标成"已看见"。返回实际改动条数。

        刻意接受显式 id 列表而不是"把该窗口全部标已看"：
        看见是一个**有范围的事实**，不能因为顺手就扩大。
        """
        if not message_ids:
            return 0
        rows = (
            (
                await self._session.execute(
                    select(Message).where(
                        Message.window_id == window_id,
                        Message.id.in_(message_ids),
                        Message.attention_state == "unseen",
                    )
                )
            )
            .scalars()
            .all()
        )
        now = utcnow()
        for row in rows:
            row.attention_state = "seen"
            row.seen_at = now
        if rows:
            await self._session.flush()
        return len(rows)

    async def correct_handled_for_turn(
        self, turn_id: str, *, outcome: str, reason: str | None = None
    ) -> None:
        """按**实际结果**校正这一轮用户消息的 `handled_as`。

        为什么需要：`handled_as` 是在消息**创建时**按"她的决定"写的
        （决定回 → replied）。但决定回不等于真的回了——用户可能在生成到一半
        时按了停止，那时一个字都没发出去。

        记"意图"却让外面读成"结果"，是假的完整感。这里按结局改写：
        - `replied`：真的有文本发出去
        - `stopped`：她要回，但被中断，没有内容
        - `failed`：她要回，但生成失败
        - `ignored`：她自己决定不回（保持）
        """
        if outcome not in {"replied", "stopped", "failed", "ignored"}:
            return
        rows = (
            (
                await self._session.execute(
                    select(Message).where(
                        Message.turn_id == turn_id,
                        Message.role == "user",
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            # 不回的一条不该被改成"回了"
            if row.handled_as == "ignored" and outcome == "replied":
                continue
            row.handled_as = outcome
            if reason:
                row.handled_reason = reason[:500]
        if rows:
            await self._session.flush()

    async def mark_missed(self, window_id: str, *, message_ids: list[str]) -> int:
        """把消息标成**错过**（一直没机会看，已过时效）。

        与 `mark_seen` 分开是为了保留"她到底有没有看到过"这个事实：
        错过 ≠ 看过但没回。只有 `seen` 才能进 `handled_as=ignored`，
        错过的是 `handled_as=missed`。
        """
        if not message_ids:
            return 0
        rows = (
            (
                await self._session.execute(
                    select(Message).where(
                        Message.window_id == window_id,
                        Message.id.in_(message_ids),
                        Message.attention_state == "unseen",
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.attention_state = "missed"
        if rows:
            await self._session.flush()
        return len(rows)

    async def mark_handled(
        self, message_id: str, *, handled_as: str, handled_reason: str | None = None
    ) -> None:
        """回写「这条消息后来被怎么处置了」。

        用户消息先落库、后决定回不回，所以处置结果只能事后补写。
        """
        row = await self._session.get(Message, message_id)
        if row is None:
            return
        row.handled_as = handled_as
        row.handled_reason = handled_reason
        await self._session.flush()

    async def update_message(
        self,
        message_id: str,
        *,
        text: str | None = None,
        state: TurnState | None = None,
        error_code: ErrorCode | None = None,
    ) -> MessageView:
        row = await self._session.get(Message, message_id)
        if row is None:
            raise not_found("消息不存在")
        if text is not None:
            row.text = text
        if state is not None:
            row.state = state.value
        if error_code is not None:
            row.error_code = error_code.value
        await self._session.flush()
        return self._to_view(row)

    async def list_messages(
        self, window_id: str, *, limit: int = 100, before_sequence: int | None = None
    ) -> list[MessageView]:
        stmt = select(Message).where(Message.window_id == window_id)
        if before_sequence is not None:
            stmt = stmt.where(Message.sequence < before_sequence)
        stmt = stmt.order_by(Message.sequence.desc()).limit(max(1, min(limit, 500)))
        rows = list((await self._session.execute(stmt)).scalars())
        rows.reverse()
        return [self._to_view(r) for r in rows]

    async def active_turn(self, window_id: str) -> Turn | None:
        return (
            await self._session.execute(
                select(Turn)
                .where(
                    Turn.window_id == window_id,
                    Turn.state.in_([TurnState.pending.value, TurnState.streaming.value]),
                )
                .order_by(Turn.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def messages_for_prompt(
        self,
        window_id: str,
        *,
        limit: int = 200,
        after_sequence: int = 0,
    ) -> list[MessageView]:
        """取**尚未摘要过**的已完成消息作为模型上下文。

        ``after_sequence`` 由窗口的 ``summary_through_sequence`` 决定：
        已经压进摘要的消息不必再取。

        ``limit`` 的默认值故意放宽（200）而不是 16/20：
        取太少会让更早的消息**既进不了上下文、也永远压不进摘要**——
        它们直接消失，用户感知是"刚才聊的事它都忘了"。
        真正决定用多少的是 token 预算，不是条数。
        """
        conditions = [
            Message.window_id == window_id,
            Message.state == TurnState.completed.value,
            Message.role.in_(["user", "actor"]),
        ]
        if after_sequence > 0:
            conditions.append(Message.sequence > after_sequence)
        stmt = (
            select(Message)
            .where(*conditions)
            .order_by(Message.sequence.desc())
            .limit(limit)
        )
        rows = list((await self._session.execute(stmt)).scalars())
        rows.reverse()
        return [self._to_view(r) for r in rows]

    async def delete_turn_messages(self, turn_id: str, *, role: str | None = None) -> int:
        """删除某轮次的部分消息（重试前清掉失败残留）。"""
        stmt = delete(Message).where(Message.turn_id == turn_id)
        if role is not None:
            stmt = stmt.where(Message.role == role)
        result = await self._session.execute(stmt)
        await self._session.flush()
        return int(getattr(result, "rowcount", 0) or 0)

    async def clear_messages(self, window_id: str) -> int:
        result = await self._session.execute(delete(Message).where(Message.window_id == window_id))
        window = await self._session.get(Window, window_id)
        if window is not None:
            window.message_count = 0
            window.updated_at = utcnow()
        await self._session.flush()
        return int(getattr(result, "rowcount", 0) or 0)

    @staticmethod
    def _to_view(row: Message) -> MessageView:
        return MessageView(
            message_id=row.id,
            turn_id=row.turn_id,
            role=row.role,
            text=row.text,
            state=TurnState(row.state),
            created_at=_as_utc(row.created_at),
            sequence=int(row.sequence or 0),
            origin=Origin(row.origin),
            error_code=ErrorCode(row.error_code) if row.error_code else None,
            model_label=row.model_label,
            attempt=int(row.attempt or 1),
            handled_as=row.handled_as,
            handled_reason=row.handled_reason,
            sender_subject_id=row.sender_subject_id,
        )

    # ---------------------------------------------------------- 窗口短记忆

    async def upsert_window_context(
        self,
        window_id: str,
        *,
        topic: str | None = None,
        open_questions: list[str] | None = None,
        ttl_minutes: int = 120,
    ) -> WindowContext:
        row = (
            await self._session.execute(
                select(WindowContext).where(WindowContext.window_id == window_id)
            )
        ).scalar_one_or_none()
        expires_at = datetime.now(UTC) + timedelta(minutes=ttl_minutes)
        if row is None:
            row = WindowContext(
                id=new_id("wctx"),
                window_id=window_id,
                topic=topic,
                open_questions=open_questions or [],
                expires_at=expires_at,
            )
            self._session.add(row)
        else:
            if topic is not None:
                row.topic = topic
            if open_questions is not None:
                row.open_questions = open_questions
            row.expires_at = expires_at
            row.updated_at = utcnow()
        await self._session.flush()
        return row

    async def save_summary(
        self,
        window_id: str,
        *,
        summary: str,
        through_sequence: int,
        message_count: int,
    ) -> None:
        """保存滚动摘要。调用方保证 ``through_sequence`` 之前的内容已被压缩。"""
        row = (
            await self._session.execute(
                select(WindowContext).where(WindowContext.window_id == window_id)
            )
        ).scalar_one_or_none()
        if row is None:
            row = WindowContext(id=new_id("wctx"), window_id=window_id, open_questions=[])
            self._session.add(row)
        row.summary = summary[:8000]
        row.summary_through_sequence = max(0, through_sequence)
        row.summary_message_count = max(0, message_count)
        row.updated_at = utcnow()
        await self._session.flush()

    async def read_window_context(self, window_id: str) -> WindowContext | None:
        """读取未过期的窗口短记忆。过期即视为不存在（不返回陈旧指代）。"""
        row = (
            await self._session.execute(
                select(WindowContext).where(WindowContext.window_id == window_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        if row.expires_at is not None and _as_utc(row.expires_at) <= datetime.now(UTC):
            return None
        return row

    # ---------------------------------------------------------- 交接胶囊

    async def create_handoff_capsule(
        self,
        *,
        subject_id: str,
        source_window_id: str,
        recent_topic: str | None,
        open_questions: list[str] | None = None,
        short_quote: str | None = None,
        consent_revision: int = 0,
        ttl_minutes: int = 30,
    ) -> HandoffCapsuleRow:
        now = datetime.now(UTC)
        row = HandoffCapsuleRow(
            id=new_id("cap"),
            subject_id=subject_id,
            source_window_id=source_window_id,
            recent_topic=recent_topic,
            open_questions=open_questions or [],
            short_quote=short_quote,
            visibility="subject_private",
            consent_revision=consent_revision,
            created_at=now,
            expires_at=now + timedelta(minutes=ttl_minutes),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def read_latest_capsule(
        self, *, subject_id: str, exclude_window_id: str | None = None
    ) -> HandoffCapsuleRow | None:
        stmt = select(HandoffCapsuleRow).where(HandoffCapsuleRow.subject_id == subject_id)
        if exclude_window_id:
            stmt = stmt.where(HandoffCapsuleRow.source_window_id != exclude_window_id)
        stmt = stmt.order_by(HandoffCapsuleRow.created_at.desc()).limit(5)
        for row in (await self._session.execute(stmt)).scalars():
            if _as_utc(row.expires_at) > datetime.now(UTC):
                return row
        return None

    # ---------------------------------------------------------- 入站幂等

    async def release_event(self, *, scope: str, key: str) -> bool:
        """撤销一次事件认领，让 OneBot 的重投能重新处理。

        为什么需要：`claim_event` 先落库是为了防并发重复处理，
        但如果**处理本身失败**（模型报错、发送失败），那条认领就成了
        "这件事办过"的错误记录 —— 重投会被当成重复直接丢掉，
        用户永远收不到回复。失败时必须能撤回认领。
        """
        from sqlalchemy import delete

        result = await self._session.execute(
            delete(IdempotencyKey).where(
                IdempotencyKey.scope == scope, IdempotencyKey.key == key
            )
        )
        await self._session.flush()
        return bool(getattr(result, "rowcount", 0))

    async def claim_event(self, *, scope: str, key: str) -> str | None:
        """登记入站事件。返回已有 result_ref 表示重复事件（调用方应短路）。"""
        existing = (
            await self._session.execute(
                select(IdempotencyKey).where(
                    IdempotencyKey.scope == scope, IdempotencyKey.key == key
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing.result_ref or "duplicate"
        self._session.add(IdempotencyKey(id=new_id("idem"), scope=scope, key=key))
        await self._session.flush()
        return None
