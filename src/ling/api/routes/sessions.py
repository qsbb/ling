"""会话（窗口）路由：列表、创建、改名、归档、消息历史。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ling.api.deps import ConversationDep, CurrentAuth
from ling.contracts.v1 import SessionRecord
from ling.domains.conversation.service import ConversationService, window_target

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


async def _group_display_names(request: Request, windows: list) -> dict[str, str | None]:
    """给群窗口补上**群名**（采集不到就是 ``None``）。

    ## 为什么在接口层做，不在入站路径做

    群名要外呼（OneBot ``get_group_info``），而**入站是最不能慢的一条路**：
    平台等太久会重投，一次慢查询能变成一串重复消息。读侧则相反——列表页
    晚 100ms 没人察觉，采集失败也只是标题退回「群 100…」。
    连接器内部还有缓存（成功 6 小时 / 失败 5 分钟），所以这里不会每次
    开页面都去打 NapCat。

    ## 为什么按 ``{channel}_connector`` 找连接器

    ``app.py`` 就是这么挂的（``state.onebot_connector`` / ``state.qqbot_connector``）。
    找不到、或这个连接器没有采集能力（web 窗口 / 测试桩），返回 ``{}``：
    前端按 ``channel`` + ``peer_id`` 自己回退，功能不受影响。
    """
    plan: dict[str, tuple[object, str]] = {}
    for window in windows:
        channel, kind, peer_id = window_target(window)
        if kind != "group" or not peer_id:
            continue
        connector = getattr(request.app.state, f"{channel}_connector", None)
        fetch = getattr(connector, "fetch_group_name", None)
        if fetch is None:
            continue
        plan[window.id] = (fetch, peer_id)
    if not plan:
        return {}
    # 并发而不是逐个等：三个群各等 2 秒，串起来就是 6 秒。
    fetched = await asyncio.gather(
        *(fetch(peer) for fetch, peer in plan.values()), return_exceptions=True
    )
    # ``return_exceptions``：采集是尽力而为，任何一个连接器把异常漏出来
    # 也不能让"看一眼会话列表"失败。
    return {
        session_id: (result if isinstance(result, str) else None)
        for session_id, result in zip(plan, fetched, strict=True)
    }


async def _display_names(
    request: Request, conversation: ConversationService, windows: list
) -> dict[str, str | None]:
    """会话的**人话名字**：群窗口靠采集，私聊窗口靠库里已有的昵称。

    两种来源差别很大，所以分两段：

    - **私聊**：对端昵称入站时就写进了 ``subjects.display_name``，一次查询
      就够（见 ``ConversationService.peer_display_names``），不需要外呼；
    - **群**：群名只有平台知道，读侧外呼采集，失败静默 ``None``。

    都不编名字：采集不到/查不到就是 ``None``，由前端按 ``channel`` +
    ``peer_id`` 回退成「群 100…」「私聊 100200301」。
    """
    names = await conversation.peer_display_names(windows)
    names.update(await _group_display_names(request, windows))
    return names


async def _session_record(
    request: Request, conversation: ConversationService, window: object
) -> SessionRecord:
    names = await _display_names(request, conversation, [window])
    return conversation.to_session_record(
        window, display_name=names.get(getattr(window, "id", ""))
    )


@router.get("/sessions", response_model=SessionListOut)
async def list_sessions(
    request: Request, auth: CurrentAuth, conversation: ConversationDep
) -> SessionListOut:
    windows = await conversation.list_windows(auth.user.subject_id)
    if not windows:
        window = await conversation.ensure_default_window(auth.user.subject_id)
        windows = [window]
    names = await _display_names(request, conversation, windows)
    return SessionListOut(
        sessions=[
            conversation.to_session_record(w, display_name=names.get(w.id)) for w in windows
        ]
    )


@router.post("/sessions", response_model=SessionRecord, status_code=201)
async def create_session(
    body: CreateSessionBody,
    request: Request,
    auth: CurrentAuth,
    conversation: ConversationDep,
) -> SessionRecord:
    window = await conversation.create_window(
        subject_id=auth.user.subject_id, title=body.title
    )
    return await _session_record(request, conversation, window)


@router.get("/sessions/{session_id}", response_model=SessionRecord)
async def get_session(
    session_id: str,
    request: Request,
    auth: CurrentAuth,
    conversation: ConversationDep,
) -> SessionRecord:
    window = await conversation.get_window(session_id, subject_id=auth.user.subject_id)
    return await _session_record(request, conversation, window)


@router.patch("/sessions/{session_id}", response_model=SessionRecord)
async def update_session(
    session_id: str,
    body: UpdateSessionBody,
    request: Request,
    auth: CurrentAuth,
    conversation: ConversationDep,
) -> SessionRecord:
    window = await conversation.update_window(
        session_id,
        subject_id=auth.user.subject_id,
        title=body.title,
        archived=body.archived,
    )
    return await _session_record(request, conversation, window)


@router.delete("/sessions/{session_id}", response_model=SessionRecord)
async def archive_session(
    session_id: str,
    request: Request,
    auth: CurrentAuth,
    conversation: ConversationDep,
) -> SessionRecord:
    window = await conversation.update_window(
        session_id, subject_id=auth.user.subject_id, archived=True
    )
    return await _session_record(request, conversation, window)


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
