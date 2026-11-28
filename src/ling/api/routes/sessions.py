"""会话（窗口）路由：列表、创建、改名、归档、消息历史、提示词快照。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from ling.api.deps import (
    AuthContext,
    ConversationDep,
    CurrentAuth,
    DbSession,
    authorize_message_view,
)
from ling.api.errors import LingError, not_found
from ling.api.guards import OwnerGuard
from ling.capabilities.channels import connector_for_instance, connector_for_namespace
from ling.contracts.v1 import (
    ContactAvailability,
    ErrorCode,
    IdStr,
    PromptBlock,
    PromptSnapshotItem,
    PromptSnapshotModel,
    PromptSnapshotOut,
    PromptSnapshotSection,
    PromptSnapshotState,
    PromptToolResult,
    SessionRecord,
)
from ling.domains.conversation.service import (
    ConversationService,
    split_window_ref,
    window_target,
)
from ling.infra.db import independent_session_scope
from ling.infra.models import Message, PromptSnapshot, Window
from ling.kernel.context import KEY_LABELS, KEY_ORDER, estimate_tokens
from ling.ports.channel import ChannelConnector

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


def _window_connector(request: Request, window: Window) -> ChannelConnector | None:
    parts = split_window_ref(window.platform_reference)
    if parts is None:
        return None
    namespace, _, _ = parts
    key = window.trusted_instance_key
    if key is not None:
        connector = connector_for_instance(request.app, *key)
        return connector if connector is not None and connector.instance_id == namespace else None
    return connector_for_namespace(request.app, window.channel, namespace)


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

    用窗口的账号命名空间选择所属实例。找不到、或该实例没有采集能力时，
    前端按渠道与 peer_id 回退显示。
    """
    # 值里存的是**绑定方法本身**（不是它返回的结果）：先攒齐再一次性
    # `gather`，所以这里必须写成可调用的类型。写成 `object` 会让下面
    # `fetch(peer)` 变成"对着 object 调用"。
    plan: dict[str, tuple[Callable[[str], Awaitable[str | None]], str]] = {}
    for window in windows:
        _, kind, peer_id = window_target(window)
        if kind != "group" or not peer_id:
            continue
        connector = _window_connector(request, window)
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


async def _friend_details(
    request: Request, windows: Sequence[Window]
) -> tuple[dict[str, tuple[str, str]], dict[str, ContactAvailability]]:
    """用每个实例的一份好友表查昵称和联系状态。"""
    tables: dict[int, dict[str, tuple[str | None, str | None]] | None] = {}
    names: dict[str, tuple[str, str]] = {}
    contacts: dict[str, ContactAvailability] = {}
    for window in windows:
        _, kind, peer_id = window_target(window)
        if kind != "private" or not peer_id:
            continue
        connector = _window_connector(request, window)
        fetch = getattr(connector, "fetch_friend_names", None)
        if fetch is None:
            continue
        instance = id(connector)
        if instance not in tables:
            try:
                fetched = await fetch()
            except Exception:  # noqa: BLE001 - 平台故障时仍然列出会话
                fetched = None
            tables[instance] = fetched if isinstance(fetched, dict) else None
        table = tables[instance]
        if table is None:
            continue
        if peer_id in table:
            remark, nickname = table[peer_id]
            names[window.id] = ((remark or "").strip()[:128], (nickname or "").strip()[:128])
        else:
            was_friend = getattr(connector, "was_friend", None)
            if was_friend is not None and was_friend(peer_id):
                contacts[window.id] = ContactAvailability.friend_deleted
    return names, contacts


async def _session_records(
    request: Request, conversation: ConversationService, windows: Sequence[Window]
) -> list[SessionRecord]:
    private_names, contacts = await _friend_details(request, windows)
    unnamed = [window for window in windows if not window.display_name_override]
    names = await conversation.peer_display_names(unnamed, private_names)
    names.update(await _group_display_names(request, unnamed))
    familiarity = await conversation.familiarities(windows)
    return [
        conversation.to_session_record(
            window,
            display_name=names.get(window.id),
            familiarity=familiarity.get(str(window.subject_id)),
            contact_unavailable=contacts.get(window.id),
        )
        for window in windows
    ]


async def _session_record(
    request: Request, conversation: ConversationService, window: Window
) -> SessionRecord:
    return (await _session_records(request, conversation, [window]))[0]


@router.get("/sessions", response_model=SessionListOut)
async def list_sessions(
    request: Request, auth: CurrentAuth, conversation: ConversationDep
) -> SessionListOut:
    """按置顶、手动顺序和最近收到的消息列出会话。"""
    windows = await conversation.list_windows(auth.user.subject_id)
    if not windows:
        # Do not commit another dependency's work or hold the lock for network I/O.
        async with independent_session_scope() as session:
            window = await conversation.with_session(session).ensure_default_window(
                auth.user.subject_id
            )
        windows = [window]
    return SessionListOut(sessions=await _session_records(request, conversation, windows))


class PinSessionBody(BaseModel):
    pinned: bool


class MoveSessionBody(BaseModel):
    anchor_id: IdStr
    before: bool


@router.post("/sessions/{session_id}/move", status_code=204)
async def move_session(
    session_id: str,
    body: MoveSessionBody,
    auth: CurrentAuth,
    conversation: ConversationDep,
) -> None:
    if auth.user.role not in {"owner", "admin"}:
        raise LingError(ErrorCode.forbidden, "需要管理员权限", status_code=403)
    await conversation.move_window(
        session_id, subject_id=auth.user.subject_id, anchor_id=body.anchor_id, before=body.before
    )


@router.post("/sessions/{session_id}/pin", response_model=SessionRecord)
async def pin_session(
    session_id: str,
    body: PinSessionBody,
    request: Request,
    auth: CurrentAuth,
    conversation: ConversationDep,
) -> SessionRecord:
    """置顶 / 取消置顶。

    ## 幂等

    重复置顶不报错，只是把 ``pinned_at`` 刷新成现在——于是"我刚置顶的那个"
    排到置顶组的第一位，这正是用户点它时想要的。取消一个没置顶的会话同理：
    把已经是 ``NULL`` 的值再写成 ``NULL``。

    ## 归属校验

    ``get_window`` 对跨用户一律 404（不是 403）：403 等于告诉调用方
    "这个 id 存在但你没权限"，那本身就是信息泄露。

    ## 返回整条 `SessionRecord`

    列表的更新顺序可通过 `GET /sessions` 获取。
    """
    async with independent_session_scope() as session:
        window = await conversation.with_session(session).set_window_pinned(
            session_id,
            subject_id=auth.user.subject_id,
            pinned=body.pinned,
            operator=auth.user.role in {"owner", "admin"},
        )
    return await _session_record(request, conversation, window)


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
        operator=auth.user.role in {"owner", "admin"},
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
    view: str = "participant",
) -> MessagesOut:
    operator = authorize_message_view(auth, view)
    await conversation.get_window(session_id, subject_id=auth.user.subject_id)
    views = await conversation.list_messages(
        session_id, limit=limit, before_sequence=before_sequence
    )
    active = await conversation.active_turn(session_id)
    active_view = None
    if active is not None:
        partial = request.app.state.cancels.partial(active.id)
        if not operator:
            voice_message = await conversation.session.scalar(select(Message.id).where(
                Message.turn_id == active.id, Message.output_modality == "voice",
            ).limit(1))
            if voice_message is not None:
                partial = ""
        active_view = (await conversation.turn_view(active, partial_text=partial)).to_dict()
    return MessagesOut(
        messages=[v.to_contract(operator=operator).model_dump(mode="json") for v in views],
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
_PromptOwner = Annotated[AuthContext, Depends(OwnerGuard(missing_message=_PROMPT_MISSING))]


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


def _snapshot_tool_results(payload: dict) -> list[PromptToolResult]:
    raw_results = payload.get("tool_transcript")
    if not isinstance(raw_results, list):
        return []
    results = []
    for raw in raw_results:
        try:
            results.append(PromptToolResult.model_validate(raw))
        except ValidationError:
            continue
    return results


@router.get("/messages/{message_id}/prompt", response_model=PromptSnapshotOut)
async def get_message_prompt(
    message_id: str,
    auth: _PromptOwner,
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
    if row is not None:
        captured_attempt = row.payload.get("attempt") if isinstance(row.payload, dict) else None
        if row.message_id not in {None, message_id} or (
            isinstance(captured_attempt, int) and captured_attempt != message.attempt
        ):
            row = None

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

    raw_context = payload.get("context")
    memory_recall = None
    note = None
    if isinstance(raw_context, dict):
        from ling.contracts.v1 import MemoryRecallDiagnostic

        recall_state = raw_context.get("experience_recall")
        if recall_state in {"not_extracted", "not_visible", "no_match", "not_injected", "injected", "used_source"}:
            sources = raw_context.get("experience_source_ids", [])
            memory_recall = MemoryRecallDiagnostic.model_validate({
                "state": recall_state,
                "source_message_ids": [
                    source.removeprefix("message:") for source in sources
                    if isinstance(source, str) and source.startswith("message:")
                ] if isinstance(sources, list) else [],
                "expression": raw_context.get("experience_expression"),
            })
        if raw_context.get("experience_direct_reply") is True:
            note = "这条回复来自记忆回查，本轮没有调用生成模型。"
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
        tool_transcript=_snapshot_tool_results(payload),
        tool_transcript_truncated=bool(payload.get("tool_transcript_truncated")),
        memory_recall=memory_recall,
        note=note,
    )

