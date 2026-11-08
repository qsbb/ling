"""会话（窗口）路由：列表、创建、改名、归档、消息历史、提示词快照。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from ling.api.deps import ConversationDep, CurrentAuth, DbSession
from ling.api.errors import not_found
from ling.contracts.v1 import (
    PromptBlock,
    PromptSnapshotItem,
    PromptSnapshotModel,
    PromptSnapshotOut,
    PromptSnapshotSection,
    PromptSnapshotState,
    SessionRecord,
)
from ling.domains.conversation.service import ConversationService, window_target
from ling.infra.models import Message, PromptSnapshot, Window
from ling.kernel.context import KEY_LABELS, KEY_ORDER, estimate_tokens

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


async def _group_display_names(
    request: Request, windows: Sequence[Window]
) -> dict[str, str | None]:
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
    # 值里存的是**绑定方法本身**（不是它返回的结果）：先攒齐再一次性
    # `gather`，所以这里必须写成可调用的类型。写成 `object` 会让下面
    # `fetch(peer)` 变成"对着 object 调用"。
    plan: dict[str, tuple[Callable[[str], Awaitable[str | None]], str]] = {}
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
    request: Request, conversation: ConversationService, windows: Sequence[Window]
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
    request: Request, conversation: ConversationService, window: Window
) -> SessionRecord:
    names = await _display_names(request, conversation, [window])
    return conversation.to_session_record(
        window, display_name=names.get(window.id)
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


# ---------------------------------------------------------------- 提示词快照
#
# 「查看本次提示词」：某条回复当时**真正发给模型的上下文**。
#
# 两条不变量（读侧）：
# 1. **非 owner 一律 404**，不是 403——403 等于告诉对方"这条消息存在"。
#    消息不存在与不属于你，返回**完全相同**的 404 体。
# 2. **三态不能合并**：available / expired / unavailable 各有各的含义，
#    合并任意两个都会让"打不开"变成不可解释的。


# 「不存在」与「不属于你」的**唯一**文案：两条路径逐字节相同。
_PROMPT_MISSING = "消息不存在"


def _item_at(raw: object) -> datetime | None:
    """快照里的 ``at`` 是 ISO 字符串（JSON 里没有 datetime）。坏值按 None。"""
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _snapshot_sections(payload: dict) -> list[PromptSnapshotSection]:
    """把扁平的消息列表按语义块分组（顺序与 key 都是**编译时**定的）。

    分组只做"聚合 + 排序"，不做任何推断：``key`` 缺失/不认识时落到 ``other``，
    绝不按正文猜它属于哪一块。
    """
    buckets: dict[str, list[PromptSnapshotItem]] = {}
    for raw in payload.get("messages") or []:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("key") or "other")
        if key not in KEY_LABELS:
            key = "other"
        content = str(raw.get("content") or "")
        tokens = raw.get("tokens")
        buckets.setdefault(key, []).append(
            PromptSnapshotItem(
                index=int(raw.get("index") or 0),
                role=str(raw.get("role") or ""),
                content=content,
                tokens=int(tokens) if isinstance(tokens, int) else estimate_tokens(content),
                sender=(str(raw["sender"]) if raw.get("sender") else None),
                at=_item_at(raw.get("at")),
                truncated=bool(raw.get("truncated")),
                lane=(str(raw["lane"]) if raw.get("lane") else None),
            )
        )

    order = list(KEY_ORDER) + [k for k in buckets if k not in KEY_ORDER]
    sections: list[PromptSnapshotSection] = []
    for key in order:
        items = buckets.get(key)
        if not items:
            continue
        # 组内按真实发送顺序（index）。截断后 index 会跳号——那正是"中间被
        # 丢掉了"的证据，不要重新编号把它抹平。
        items.sort(key=lambda item: item.index)
        roles = {item.role for item in items}
        sections.append(
            PromptSnapshotSection(
                key=PromptBlock(key),
                label=KEY_LABELS[key],
                # 混合角色不要挑一个代表：历史里 user/assistant 交替，
                # 挑一个会让读者以为这一段结构单一。
                role=(roles.pop() if len(roles) == 1 else "mixed"),
                tokens=sum(item.tokens for item in items),
                # 历史默认折叠：它通常是最长的一段，而且一眼看完没有意义。
                collapsed=key == "history",
                items=items,
            )
        )
    return sections


@router.get("/messages/{message_id}/prompt", response_model=PromptSnapshotOut)
async def get_message_prompt(
    message_id: str,
    auth: CurrentAuth,
    session: DbSession,
) -> PromptSnapshotOut:
    """`GET /v1/messages/{message_id}/prompt` —— 这条回复当时看到的上下文。

    只读、owner 限定。**非 owner 与不存在返回同一个 404**：两者的响应体
    完全一致，不泄露"这条消息存在但你没权限"。
    """
    message = await session.get(Message, message_id)
    if message is None:
        # 与"不属于你"共用同一条文案（见下），不要在这里泄露任何区别。
        raise not_found(_PROMPT_MISSING)
    # 归属：窗口的主人。群窗口按设计归属主人，因此群里别人发出的消息也
    # 只有主人能看到快照——提示词里包含她自己的人设与记忆，不是群成员的。
    #
    # 越权与不存在必须返回**逐字节相同**的 404：`get_window` 抛的是
    # 「会话不存在」，而下面消息不存在抛的是「消息不存在」——两条文案不统一
    # 就等于泄露"这条消息存在，只是你没权限"。所以这里自己判归属，
    # 所有失败路径共用同一个错误文案。
    window = await session.get(Window, message.window_id)
    if window is None or window.subject_id != auth.user.subject_id:
        raise not_found(_PROMPT_MISSING)

    turn_id = message.turn_id
    # 先按消息找（回填成功的那条）；找不到再按 turn 找：回填是同一事务里的
    # best-effort 旁路，失败时快照仍然存在，只是没指到这条消息。
    row: PromptSnapshot | None = (
        await session.execute(
            select(PromptSnapshot).where(PromptSnapshot.message_id == message_id)
        )
    ).scalar_one_or_none()
    if row is None and turn_id:
        row = (
            await session.execute(
                select(PromptSnapshot).where(PromptSnapshot.turn_id == turn_id)
            )
        ).scalar_one_or_none()

    if row is None:
        # 从没存下来：捕获失败，或这条消息早于本功能。
        return PromptSnapshotOut(
            state=PromptSnapshotState.unavailable,
            message_id=message_id,
            turn_id=turn_id,
            note=(
                "这一轮没有存下提示词快照"
                "（可能是捕获失败，或这条回复早于该功能上线）"
            ),
        )

    now = datetime.now(UTC)
    expires_at = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(
        tzinfo=UTC
    )
    payload = row.payload if isinstance(row.payload, dict) else None
    scrubbed = bool(payload and payload.get("scrubbed"))
    raw_model = payload.get("model") if payload else None
    # 拿不到任何一项就整块给 null（不要回一个全是 null 的对象：前端会当成
    # "查到了模型但字段缺失"，而事实是"当时没记下来"）。
    metadata_model = (
        PromptSnapshotModel(
            provider_id=(raw_model.get("provider_id") or None),
            model_id=(raw_model.get("model_id") or None),
            profile=(raw_model.get("profile") or None),
            config_revision=(
                int(raw_model["config_revision"])
                if isinstance(raw_model.get("config_revision"), int)
                else None
            ),
            source=(raw_model.get("source") or None),
        )
        if isinstance(raw_model, dict)
        and any(raw_model.get(key) for key in ("provider_id", "model_id", "profile"))
        else None
    )

    if payload is None or scrubbed or expires_at <= now:
        return PromptSnapshotOut(
            state=PromptSnapshotState.expired,
            message_id=message_id,
            turn_id=row.turn_id,
            created_at=row.created_at,
            expires_at=row.expires_at,
            model=metadata_model,
            token_total=int((payload or {}).get("token_total") or 0),
            truncated=bool(row.truncated),
            note=(
                # 措辞要覆盖两种过期：保留期到了（正文可能还在库里，但我们
                # 不再提供）与正文已被维护任务清空。
                "这一轮的提示词快照已过保留期（7 天），正文不再提供。"
                "只保留「当时有过快照」这一事实与当时的模型信息。"
            ),
        )

    return PromptSnapshotOut(
        state=PromptSnapshotState.available,
        message_id=message_id,
        turn_id=row.turn_id,
        created_at=row.created_at,
        expires_at=row.expires_at,
        model=metadata_model,
        token_total=int(payload.get("token_total") or 0),
        truncated=bool(row.truncated or payload.get("truncated")),
        dropped_items=int(payload.get("dropped_items") or 0),
        sections=_snapshot_sections(payload),
    )

