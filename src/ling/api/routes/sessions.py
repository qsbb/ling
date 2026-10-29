"""会话（窗口）路由：列表、创建、改名、归档、消息历史。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ling.api.deps import ConversationDep, CurrentAuth
from ling.contracts.v1 import SessionRecord

router = APIRouter(tags=["sessions"])


class SessionListOut(BaseModel):
    sessions: list[SessionRecord]


class CreateSessionBody(BaseModel):
    title: str | None = Field(default=None, max_length=60)


class UpdateSessionBody(BaseModel):
    title: str | None = Field(default=None, max_length=60)
    archived: bool | None = None


class MessagesOut(BaseModel):
    messages: list[dict]
    active_turn: dict | None = None


@router.get("/sessions", response_model=SessionListOut)
async def list_sessions(auth: CurrentAuth, conversation: ConversationDep) -> SessionListOut:
    windows = await conversation.list_windows(auth.user.subject_id)
    if not windows:
        window = await conversation.ensure_default_window(auth.user.subject_id)
        windows = [window]
    return SessionListOut(
        sessions=[conversation.to_session_record(w) for w in windows]
    )


@router.post("/sessions", response_model=SessionRecord, status_code=201)
async def create_session(
    body: CreateSessionBody, auth: CurrentAuth, conversation: ConversationDep
) -> SessionRecord:
    window = await conversation.create_window(
        subject_id=auth.user.subject_id, title=body.title
    )
    return conversation.to_session_record(window)


@router.get("/sessions/{session_id}", response_model=SessionRecord)
async def get_session(
    session_id: str, auth: CurrentAuth, conversation: ConversationDep
) -> SessionRecord:
    window = await conversation.get_window(session_id, subject_id=auth.user.subject_id)
    return conversation.to_session_record(window)


@router.patch("/sessions/{session_id}", response_model=SessionRecord)
async def update_session(
    session_id: str, body: UpdateSessionBody, auth: CurrentAuth, conversation: ConversationDep
) -> SessionRecord:
    window = await conversation.update_window(
        session_id,
        subject_id=auth.user.subject_id,
        title=body.title,
        archived=body.archived,
    )
    return conversation.to_session_record(window)


@router.delete("/sessions/{session_id}", response_model=SessionRecord)
async def archive_session(
    session_id: str, auth: CurrentAuth, conversation: ConversationDep
) -> SessionRecord:
    window = await conversation.update_window(
        session_id, subject_id=auth.user.subject_id, archived=True
    )
    return conversation.to_session_record(window)


@router.get("/sessions/{session_id}/messages", response_model=MessagesOut)
async def list_messages(
    session_id: str,
    request: Request,
    auth: CurrentAuth,
    conversation: ConversationDep,
    limit: int = 100,
    before_sequence: int | None = None,
) -> MessagesOut:
    await conversation.get_window(session_id, subject_id=auth.user.subject_id)
    views = await conversation.list_messages(
        session_id, limit=limit, before_sequence=before_sequence
    )
    active = await conversation.active_turn(session_id)
    active_view = None
    if active is not None:
        partial = request.app.state.cancels.partial(active.id)
        active_view = (await conversation.turn_view(active, partial_text=partial)).to_dict()
    return MessagesOut(
        messages=[v.to_contract().model_dump(mode="json") for v in views],
        active_turn=active_view,
    )
