"""Together：共处会话、媒体进度与参与者。

用于"共读/共看/一起听"这类活动。硬规则：

- **进度来自客户端真实回报**；服务端不猜"看到哪了"
- 参与者列表有受众语义：多人同场时不能泄露他人私聊
- 会话必须落库：共处是"正在一起做一件事"的状态，重启就丢等于数据丢失

## 为什么从内存改成数据库

原实现是纯内存字典，注释写着"W3 联调时接持久化"。后果：

- 每次进程重启，所有会话全丢
- 多 worker 之间各有一份，互相看不见

用户看到的是"刚才那个共读没了"，而且**没有任何错误提示**。这是数据丢失。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.contracts.v1 import new_id
from ling.infra.models import TogetherParticipantRow, TogetherSessionRow, utcnow

KINDS = ("read", "watch", "listen", "game")
STATES = ("active", "paused", "ended")


@dataclass
class TogetherParticipant:
    subject_id: str
    display_name: str
    joined_at: datetime


@dataclass
class TogetherSession:
    session_id: str
    kind: str
    title: str
    host_subject_id: str
    participants: list[TogetherParticipant] = field(default_factory=list)
    progress: dict[str, object] = field(default_factory=dict)
    state: str = "active"
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TogetherService:
    """共处会话的唯一写入方。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, *, kind: str, title: str, host_subject_id: str, host_name: str
    ) -> TogetherSession:
        if kind not in KINDS:
            raise ValueError(f"未知共处类型: {kind}")
        row = TogetherSessionRow(
            id=new_id("tog"),
            kind=kind,
            title=title[:200],
            host_subject_id=host_subject_id,
            state="active",
            progress={},
        )
        self._session.add(row)
        # **必须先 flush 会话行**：participants 有外键指向它，
        # 而两行之间没有 relationship 声明，SQLAlchemy 不知道插入顺序，
        # 同一批 flush 会先插子表 → ForeignKeyViolation。
        await self._session.flush()
        self._session.add(
            TogetherParticipantRow(
                id=new_id("togp"),
                session_id=row.id,
                subject_id=host_subject_id,
                display_name=host_name[:128],
                joined_at=utcnow(),
            )
        )
        await self._session.flush()
        return await self._require(row.id)

    async def join(
        self, session_id: str, *, subject_id: str, display_name: str
    ) -> TogetherSession:
        session = await self._require(session_id)
        if session.state == "ended":
            raise ValueError("会话已结束，不能再加入")
        if any(p.subject_id == subject_id for p in session.participants):
            return session
        self._session.add(
            TogetherParticipantRow(
                id=new_id("togp"),
                session_id=session_id,
                subject_id=subject_id,
                display_name=display_name[:128],
                joined_at=utcnow(),
            )
        )
        await self._touch(session_id)
        return await self._require(session_id)

    async def report_progress(
        self, session_id: str, *, subject_id: str, position: dict[str, object]
    ) -> TogetherSession:
        """客户端回报真实进度。**未参与的人不能改进度**。

        这条不只是礼貌：不校验的话任何人都能替别人"看到第 30 分钟"，
        而进度是她据以判断"我们到哪了"的唯一依据。
        """
        session = await self._require(session_id)
        if not any(p.subject_id == subject_id for p in session.participants):
            raise PermissionError("非参与者不能上报进度")
        row = await self._session.get(TogetherSessionRow, session_id)
        if row is None:
            raise KeyError(f"共处会话不存在: {session_id}")
        # JSON 列是可变对象：必须整体重新赋值，原地改不会被追踪
        row.progress = {**dict(row.progress or {}), subject_id: position}
        await self._touch(session_id)
        return await self._require(session_id)

    async def end(self, session_id: str) -> TogetherSession:
        row = await self._session.get(TogetherSessionRow, session_id)
        if row is None:
            raise KeyError(f"共处会话不存在: {session_id}")
        row.state = "ended"
        await self._touch(session_id)
        return await self._require(session_id)

    async def get(self, session_id: str) -> TogetherSession | None:
        row = await self._session.get(TogetherSessionRow, session_id)
        if row is None:
            return None
        return await self._to_view(row)

    async def list_sessions(self, *, limit: int = 50) -> list[TogetherSession]:
        rows = (
            (
                await self._session.execute(
                    select(TogetherSessionRow)
                    .order_by(TogetherSessionRow.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [await self._to_view(r) for r in rows]

    # ---------------------------------------------------------------- 内部

    async def _touch(self, session_id: str) -> None:
        row = await self._session.get(TogetherSessionRow, session_id)
        if row is not None:
            row.updated_at = utcnow()
            await self._session.flush()

    async def _require(self, session_id: str) -> TogetherSession:
        session = await self.get(session_id)
        if session is None:
            raise KeyError(f"共处会话不存在: {session_id}")
        return session

    async def _to_view(self, row: TogetherSessionRow) -> TogetherSession:
        participants = (
            (
                await self._session.execute(
                    select(TogetherParticipantRow)
                    .where(TogetherParticipantRow.session_id == row.id)
                    .order_by(TogetherParticipantRow.joined_at)
                )
            )
            .scalars()
            .all()
        )
        return TogetherSession(
            session_id=row.id,
            kind=row.kind,
            title=row.title,
            host_subject_id=row.host_subject_id,
            participants=[
                TogetherParticipant(
                    subject_id=p.subject_id,
                    display_name=p.display_name,
                    joined_at=p.joined_at,
                )
                for p in participants
            ],
            progress=dict(row.progress or {}),
            state=row.state,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


__all__ = [
    "KINDS",
    "STATES",
    "TogetherParticipant",
    "TogetherService",
    "TogetherSession",
]
