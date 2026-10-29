"""聊天路由：发送（SSE 流式）、停止、重试。

关键语义（docs/API-WEB-V1.md §3）：
- ``client_turn_id`` 是幂等键；重复提交返回既有轮次状态，不重复扣费。
- 失败/停止后已交付文本保留，但状态明确标记，前端不得当作完整回复。
- 只有 ``failed`` / ``stopped`` 的轮次可以重试。

连接生命周期：轮次任务持有自己的数据库会话；客户端断线时后台任务继续跑完并落库，
前端重连后从 ``GET /messages`` 取回权威结果。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ling.api.deps import (
    ConfigDep,
    ConversationDep,
    CurrentAuth,
    DbSession,
    RouterDep,
    SettingsDep,
)
from ling.api.errors import LingError, not_found
from ling.contracts.v1 import ErrorCode, TurnState
from ling.kernel.autonomy import TurnChoice
from ling.kernel.decision import TurnDecision
from ling.kernel.turn import TurnCoordinator, TurnStreamEvent
from ling.logging_setup import emit_event

logger = logging.getLogger("ling.api.chat")

router = APIRouter(tags=["chat"])

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class SendTurnBody(BaseModel):
    client_turn_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=8000)


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def _coordinator(request: Request, settings: SettingsDep, router_: RouterDep) -> TurnCoordinator:
    return TurnCoordinator(
        router=router_,
        actor_id=settings.actor_id,
        persona_id=settings.persona_id,
        cancels=request.app.state.cancels,
        arbiter=getattr(request.app.state, "arbiter", None),
        stream_log=getattr(request.app.state, "stream_log", None),
        turn_control=getattr(request.app.state, "turn_control", None),
        speak_lease=getattr(request.app.state, "speak_lease", None),
    )


def _mark_group_turn(request: Request, window_visibility: str, window_id: str) -> None:
    """只在**真正产出回复**时记账。

    注意：决定回复就记账，会让失败轮次也占掉额度。
    """
    import time

    budget = getattr(request.app.state, "group_budget", None)
    if budget is not None and window_visibility == "group_scope" and window_id:
        budget.budget(window_id).mark(time.time())


async def _group_turn_budget(config: object) -> int:
    """群聊发言预算来自已发布配置；读不到就用契约默认值。

    读配置失败不该阻断整轮对话——那属于"设置服务挂了"，
    不应该表现成"群里永远不接话"。

    早先这里读的是 ``app.state.config_service``——**那个属性根本不存在**，
    于是永远静默退回默认值，设置等于没生效。配置必须从依赖注入传进来。
    """
    from ling.kernel.decision import DecisionContext

    default = DecisionContext.group_turn_budget
    try:
        effective = await config.effective()  # type: ignore[attr-defined]
        return int(effective.dialogue.group_turn_budget)
    except Exception:  # noqa: BLE001 - 配置读失败退回默认
        return default


async def _choose_by_autonomy(
    request: Request,
    ctx: object,
    text: str,
    config: object,
    *,
    config_session: object,
    actor_id: str,
    window_id: str,
    subject_id: str,
) -> TurnChoice:
    """Web 路径的「回不回」——与 QQ 用**同一个**选择器。

    router 从 `app.state.router` 取（那个真实存在），配置由依赖注入传进来。
    上一版读的 `app.state.config_service` 不存在，导致模型从未被调用。
    """
    from ling.kernel.autonomy import (
        AutonomyTurnService,
        make_persona_reader,
        make_situation_reader,
    )

    return await AutonomyTurnService(
        router=getattr(request.app.state, "router", None),
        config_reader=config,
        situation_reader=make_situation_reader(
            config_session,
            actor_id=actor_id,
            window_id=window_id,
            subject_id=subject_id,
        ),
        persona_reader=make_persona_reader(
            config_session, persona_id=__import__("ling.config", fromlist=["get_settings"]).get_settings().persona_id
        ),
    ).choose(ctx, text=text)  # type: ignore[arg-type]


async def _evaluate_turn_decision(
    request: Request,
    window: object,
    text: str,
    subject_id: str,
    config: object,
    *,
    config_session: object,
    actor_id: str,
) -> TurnChoice:
    """计算本轮接话决策。

    - 硬规则结论直接采用（模型无权推翻）
    - 弹性区间（hard=False）才交给模型判断；模型不可用时退回确定性默认值
    """
    from ling.kernel.decision import DecisionContext, DecisionEngine, classify_text

    is_group = getattr(window, "visibility", "subject_private") == "group_scope"
    is_question, mentioned, explicitly_asked = classify_text(text)
    recent = 0
    budget_registry = getattr(request.app.state, "group_budget", None)
    window_id = getattr(window, "id", "")
    import time

    if budget_registry is not None and window_id:
        recent = budget_registry.budget(window_id).count(time.time())

    ctx = DecisionContext(
        is_group=is_group,
        mentioned=mentioned,
        explicitly_asked=explicitly_asked,
        is_question=is_question,
        recent_actor_turns=recent,
        has_meaningful_text=DecisionEngine.has_meaningful_text(text),
        group_turn_budget=await _group_turn_budget(config),
    )
    # 与 QQ 路径共用同一个选择器（kernel/autonomy.py）。
    # 原来这里是 `_ask_group_model`，它读 `app.state.config_service`——
    # **该属性不存在**，于是永远返回 fallback，模型从未参与过。
    return await _choose_by_autonomy(
        request,
        ctx,
        text,
        config,
        config_session=config_session,
        actor_id=actor_id,
        window_id=window_id,
        subject_id=subject_id,
    )


async def _forward(
    queue: asyncio.Queue[TurnStreamEvent | None], *, started: bytes
) -> AsyncIterator[bytes]:
    yield started
    while True:
        item = await queue.get()
        if item is None:
            return
        yield _sse(item.event, item.data)


async def _refresh_config_if_stale(request: Request) -> None:
    """配置修订号变了就把密钥重载进本进程缓存（跨副本传播）。

    每个进程各有一份内存缓存；不做这一步的话，在 A 副本保存的新密钥
    对 B 副本不可见，worker 更是只在启动时读过一次。
    """
    watcher = getattr(request.app.state, "revision_watcher", None)
    if watcher is None:
        return
    from ling.infra.db import session_scope
    from ling.settings.refresh import current_revision, refresh_secrets

    try:
        async with session_scope() as session:
            if not await watcher.stale(session):
                return
            settings = request.app.state.settings
            await refresh_secrets(
                session,
                cache=request.app.state.secret_cache,
                box=request.app.state.secret_box,
                persona_id=settings.persona_id,
                test_mode_default=settings.test_mode,
                diagnosis=getattr(request.app.state, "secret_diagnosis", None),
            )
            watcher.mark_loaded(await current_revision(session))
    except Exception as exc:  # noqa: BLE001 - 刷新失败不能挡住本轮对话
        import logging

        logging.getLogger(__name__).info(
            "配置刷新失败（沿用旧缓存）: %s", type(exc).__name__
        )


# 图片上传：Web 端唯一的入站媒体入口。
#
# 为什么需要它：QQ 那边有媒体通道，Web 这边**完全没有**——用户在网页上
# 根本没法给她看任何东西。而"她能看见你发的东西"是拟人的一部分。
#
# 只收图片、只落本地、只登记证据。**不在这里生成描述**：描述要视觉模型，
# 由 worker 的 media.describe 异步做，且拿不到就留空、下次再试。
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
_IMAGE_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


@router.post("/sessions/{session_id}/media")
async def upload_media(
    session_id: str,
    request: Request,
    auth: CurrentAuth,
    session: DbSession,
    settings: SettingsDep,
    upload: UploadFile = File(...),
) -> dict:
    """上传一张图片，返回可在发消息时引用的 evidence id。"""
    from ling.domains.conversation.service import ConversationService
    from ling.domains.media.service import MediaEvidenceService

    mime = (upload.content_type or "").split(";")[0].strip().lower()
    if mime not in _IMAGE_MIME:
        raise LingError(
            ErrorCode.invalid_request,
            f"只支持图片（png/jpeg/webp/gif），收到 {mime or '未知类型'}",
            status_code=422,
        )
    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise LingError(
            ErrorCode.invalid_request,
            f"图片超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB",
            status_code=422,
        )
    if not data:
        raise LingError(ErrorCode.invalid_request, "上传内容为空", status_code=422)

    conversation = ConversationService(session, actor_id=settings.actor_id)
    window = await conversation.get_window(session_id, subject_id=auth.user.subject_id)
    if window is None:
        raise LingError(ErrorCode.not_found, "会话不存在", status_code=404)

    media_dir = settings.data_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    # 用 evidence id 当文件名：不信任客户端给的文件名（路径穿越）
    from ling.domains.media.service import MediaEvidenceError

    try:
        evidence = await MediaEvidenceService(
            session, actor_id=settings.actor_id
        ).record(
            subject_id=auth.user.subject_id,
            window_id=session_id,
            # 占位 ref **必须带内容指纹**：去重键由它算出，写成固定字符串的话
            # 从第二张图起会被判成"已登记"，直接返回**第一张图**的证据——
            # 消息里引用的就不是用户刚发的那张。
            media_ref=f"pending:upload:{hashlib.sha256(data).hexdigest()[:32]}",
            kind="image",
        )
    except MediaEvidenceError as exc:
        # 入站媒体要有 `media.inbound` 授权。这是隐私门，不能绕过；
        # 但**必须给人可执行的下一步**，而不是一个 500。
        raise LingError(
            ErrorCode.forbidden,
            f"{exc}。请先在「授权」页允许「接收媒体」（scope=media.inbound）",
            status_code=403,
        ) from exc
    path = media_dir / f"{evidence.evidence_id}{_IMAGE_MIME[mime]}"
    path.write_bytes(data)
    # 文件名用 evidence id 生成，所以只能先 record 拿到 id 再回填 ref。
    # 不接受客户端文件名：那是一条路径穿越入口。
    from ling.infra.models import MediaEvidenceRow

    row = await session.get(MediaEvidenceRow, evidence.evidence_id)
    if row is not None:
        row.media_ref = f"file:{path}"
        await session.flush()
    await session.commit()
    return {
        "evidence_id": evidence.evidence_id,
        "kind": "image",
        "mime": mime,
        "bytes": len(data),
        "note": "发消息时把这个 evidence_id 放进 media_evidence_ids 即可",
    }


@router.post("/sessions/{session_id}/turns")
async def send_turn(
    session_id: str,
    body: SendTurnBody,
    request: Request,
    auth: CurrentAuth,
    conversation: ConversationDep,
    router_: RouterDep,
    config: ConfigDep,
    settings: SettingsDep,
) -> StreamingResponse:
    # 配置可能刚被另一个副本改过：按修订号懒刷新密钥，
    # 否则本进程会继续用旧密钥，表现为"有时能聊有时不能"。
    await _refresh_config_if_stale(request)
    window = await conversation.get_window(session_id, subject_id=auth.user.subject_id)
    effective = await config.effective()
    text = body.text.strip()
    if not text:
        raise LingError(ErrorCode.invalid_request, "消息不能为空", status_code=422)

    existing = await conversation.find_turn(window.id, body.client_turn_id)
    if existing is not None:
        view = await conversation.turn_view(existing)
        msgs = await conversation.list_messages(window.id, limit=200)
        actor_msg = next(
            (m for m in msgs if m.turn_id == existing.id and m.role == "actor"), None
        )

        async def replay() -> AsyncIterator[bytes]:
            yield _sse(
                "turn.started",
                {
                    "turn_id": existing.id,
                    "state": view.state.value,
                    "replayed": True,
                    "test_mode": effective.test_mode,
                },
            )
            if view.state is TurnState.completed and actor_msg is not None:
                yield _sse(
                    "turn.completed",
                    {
                        "turn_id": existing.id,
                        "state": view.state.value,
                        "model_label": actor_msg.model_label,
                        "message": actor_msg.to_contract().model_dump(mode="json"),
                        "replayed": True,
                    },
                )
            elif view.state in {TurnState.pending, TurnState.streaming, TurnState.stopping}:
                yield _sse(
                    "turn.failed",
                    {
                        "turn_id": existing.id,
                        "state": view.state.value,
                        "error": {
                            "code": ErrorCode.conflict.value,
                            "message": "该轮次仍在生成中，请等待或先停止",
                            "retriable": False,
                        },
                    },
                )
            else:
                yield _sse(
                    "turn.failed",
                    {
                        "turn_id": existing.id,
                        "state": view.state.value,
                        "text": actor_msg.text if actor_msg else "",
                        "error": {
                            "code": (view.error_code or ErrorCode.cancelled).value,
                            "message": "该轮次此前未完成，可点击重试",
                            "retriable": view.retriable,
                        },
                    },
                )

        return StreamingResponse(replay(), media_type="text/event-stream", headers=SSE_HEADERS)

    # --- 接话决策：确定性硬规则优先，模型只在弹性区间参与 ---
    # 设计文档 §6.1 候选：回答/澄清/沉默/延后；§1 第 7 条：确定性代码最终裁决。
    decision = await _evaluate_turn_decision(
        request,
        window,
        text,
        auth.user.subject_id,
        config,
        config_session=conversation.session,
        actor_id=settings.actor_id,
    )

    turn = await conversation.create_turn(
        window.id, client_turn_id=body.client_turn_id, config_revision=effective.revision
    )
    user_msg = await conversation.add_message(
        window.id,
        role="user",
        text=text,
        turn_id=turn.id,
        # 处置结果与"回了什么"分开记：沉默时这条消息仍然在库里，
        # 她仍然算"看见过"。没有这个字段，两种情况在外面完全一样。
        handled_as="replied" if decision.respond else "ignored",
        handled_reason=decision.reason,
    )

    # 她说"稍后再提"时落一条意图；来源固定为这条消息。
    if getattr(decision, "followup", None) is not None:
        from ling.domains.intent.service import record_followup

        await record_followup(
            conversation.session,
            actor_id=settings.actor_id,
            followup=decision.followup,
            subject_id=auth.user.subject_id,
            window_id=window.id,
            source_ref=f"msg:{user_msg.message_id}",
        )

    emit_event(
        logger,
        logging.INFO,
        event_name="turn_started",
        message="轮次开始",
        attributes={
            "turn_id": turn.id,
            "window_id": window.id,
            "is_group": getattr(window, "visibility", "") == "group_scope",
            "source": "web",
            "config_revision": effective.revision,
        },
    )

    # 「她为什么没回」的唯一权威记录（Web 路径）。
    # 与连接器入站路径用同一个事件名与同一套受控 skip_code，
    # 这样"为什么没回"在两个入口的日志里是同一个查询。
    emit_event(
        logger,
        logging.INFO,
        event_name="reply_decision_made",
        message="决定回应" if decision.respond else "决定不回应",
        attributes={
            "decision": "reply" if decision.respond else "silence",
            # 机器可读的「为什么」，与日志里的同一套枚举
            "skip_code": decision.skip_code,
            "hard_rule": decision.hard,
            "source": "web",
            # 与连接器路径字段对齐：同一个查询要看得出群聊还是私聊
            "is_group": getattr(window, "visibility", "") == "group_scope",
            "model_status": getattr(decision.model_status, "value", ""),
            "reason": decision.reason,
        },
    )

    if not decision.respond:
        # 沉默不是丢弃：消息已落库，只是不产生 actor 回复。
        # 前端需要知道"角色选择了不说话"，而不是永远等一个不会来的回复。
        await conversation.set_turn_state(turn.id, TurnState.completed)
        await conversation.session.commit()

        async def silent() -> AsyncIterator[bytes]:
            yield _sse(
                "turn.started",
                {
                    "turn_id": turn.id,
                    "state": TurnState.streaming.value,
                    "user_message": user_msg.to_contract().model_dump(mode="json"),
                    "test_mode": effective.test_mode,
                    "config_revision": effective.revision,
                    # 沉默也是一个**决定**，要说清是谁决定的。
                    # 少了这个字段，"她选择不回"和"她根本没看见"在外面看
                    # 一模一样——那正是上一版模型从未被调用却没人发现的原因。
                    "decision": TurnDecision.silence.value,
                    "decision_reason": decision.reason,
                    "decision_hard": decision.hard,
                    "model_status": decision.model_status.value,
                    "silent_ack": decision.silent_ack,
                },
            )
            yield _sse(
                "turn.completed",
                {
                    "turn_id": turn.id,
                    "state": TurnState.completed.value,
                    "model_label": None,
                    "decision": TurnDecision.silence.value,
                    "decision_reason": decision.reason,
                    "decision_hard": decision.hard,
                    "model_status": decision.model_status.value,
                    "silent_ack": decision.silent_ack,
                    "message": None,
                },
            )

        return StreamingResponse(
            silent(), media_type="text/event-stream", headers=SSE_HEADERS
        )
    # 轮次任务使用自己的数据库会话：必须先提交请求会话，否则后台看不到 turn 行
    await conversation.session.commit()
    coordinator = _coordinator(request, settings, router_)
    queue = await coordinator.run_turn(
        window_id=window.id,
        subject_id=auth.user.subject_id,
        turn_id=turn.id,
        user_text=text,
        config=effective,
    )
    started = _sse(
        "turn.started",
        {
            "turn_id": turn.id,
            "state": TurnState.streaming.value,
            "user_message": user_msg.to_contract().model_dump(mode="json"),
            "test_mode": effective.test_mode,
            "config_revision": effective.revision,
            "decision": "reply" if decision.respond else "silence",
            "decision_reason": decision.reason,
            # 机器可读的「为什么」，与日志里同一套受控枚举
            "skip_code": decision.skip_code,
            "decision_hard": decision.hard,
            "model_status": decision.model_status.value,
        },
    )
    async def _guarded() -> AsyncIterator[bytes]:
        produced = False
        async for chunk in _forward(queue, started=started):
            if b"event: delta" in chunk:
                produced = True
            yield chunk
        if produced:
            _mark_group_turn(request, window.visibility, window.id)

    return StreamingResponse(
        _guarded(), media_type="text/event-stream", headers=SSE_HEADERS
    )


@router.get("/turns/{turn_id}/resume")
async def resume_turn(
    turn_id: str,
    request: Request,
    auth: CurrentAuth,
    conversation: ConversationDep,
    since: int = 0,
    attempt: int | None = None,
) -> StreamingResponse:
    """断线续传：从游标 ``since`` 之后继续推送已产生的片段。

    - ``complete=false``（游标过期/片段已清理）时，先给一段权威文本快照，
      不假装能续传（取舍：能明确说"接不上"比静默丢字更重要）。
    - 终态轮次直接回放完整文本后结束，前端不需要再轮询。
    """
    turn = await conversation.get_turn(turn_id)
    if turn is None:
        raise not_found("轮次不存在")
    await conversation.get_window(turn.window_id, subject_id=auth.user.subject_id)
    effective_attempt = attempt if attempt is not None else int(turn.attempt or 1)

    stream_log = getattr(request.app.state, "stream_log", None)
    chunks: list = []
    complete = False
    if stream_log is not None:
        chunks, complete = stream_log.since(
            turn_id=turn_id, attempt=effective_attempt, last_sequence=max(0, since)
        )

    # 兜底文本：进程内存里的半成品（单实例）或已落库的 actor 消息（多实例）
    snapshot = ""
    if not complete:
        snapshot = request.app.state.cancels.partial(turn_id)
        if not snapshot:
            msgs = await conversation.list_messages(turn.window_id, limit=200)
            for msg in reversed(msgs):
                if msg.turn_id == turn_id and msg.role == "actor":
                    snapshot = msg.text
                    break

    async def replay() -> AsyncIterator[bytes]:
        yield _sse(
            "turn.resumed",
            {
                "turn_id": turn_id,
                "attempt": effective_attempt,
                "from": max(0, since),
                "complete": complete,
                "state": turn.state,
            },
        )
        if not complete and snapshot:
            # 明确告诉前端：这是快照，不是续传
            yield _sse(
                "turn.snapshot",
                {"turn_id": turn_id, "text": snapshot, "reason": "片段已清理，返回权威快照"},
            )
        for chunk in chunks:
            yield _sse(
                "delta",
                {
                    "text": chunk.text,
                    "turn_id": turn_id,
                    "sequence": chunk.sequence,
                    "attempt": chunk.attempt,
                },
            )
        if turn.state in {"failed", "stopped", "completed"}:
            yield _sse(
                "turn.finished",
                {"turn_id": turn_id, "state": turn.state, "attempt": effective_attempt},
            )

    return StreamingResponse(replay(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/turns/{turn_id}/stop")
async def stop_turn(
    turn_id: str, request: Request, auth: CurrentAuth, conversation: ConversationDep
) -> dict[str, object]:
    turn = await conversation.get_turn(turn_id)
    if turn is None:
        raise not_found("轮次不存在")
    await conversation.get_window(turn.window_id, subject_id=auth.user.subject_id)
    cancels = request.app.state.cancels
    signalled = cancels.stop(turn_id)
    # 跨实例：本实例可能没有这个轮次（它跑在别的实例上），
    # 写一条持久化停止信号，由轮次侧轮询发现。
    control = getattr(request.app.state, "turn_control", None)
    if control is not None:
        await control.request_stop(turn_id)
    # 条件更新：只允许从"未终结"状态迁到 stopping。
    # 协调器可能已经先一步提交 stopped/failed/completed，这里绝不能覆盖它。
    moved = await conversation.transition_turn_if(
        turn_id,
        from_states={TurnState.pending, TurnState.streaming},
        to_state=TurnState.stopping,
    )
    if moved:
        return {
            "turn_id": turn_id,
            "state": TurnState.stopping.value,
            "was_running": signalled,
        }
    current = await conversation.get_turn(turn_id)
    state = current.state if current is not None else turn.state
    if state == TurnState.stopping.value and not signalled:
        # 进程重启等导致的悬空 stopping：落库修正，避免前端永远转圈且无法重试
        fixed = await conversation.transition_turn_if(
            turn_id,
            from_states={TurnState.stopping},
            to_state=TurnState.stopped,
        )
        if fixed:
            return {"turn_id": turn_id, "state": TurnState.stopped.value, "was_running": False}
    return {"turn_id": turn_id, "state": state, "was_running": signalled}


@router.post("/turns/{turn_id}/retry")
async def retry_turn(
    turn_id: str,
    request: Request,
    auth: CurrentAuth,
    conversation: ConversationDep,
    router_: RouterDep,
    config: ConfigDep,
    settings: SettingsDep,
) -> StreamingResponse:
    turn = await conversation.get_turn(turn_id)
    if turn is None:
        raise not_found("轮次不存在")
    window = await conversation.get_window(turn.window_id, subject_id=auth.user.subject_id)
    if turn.state == TurnState.completed.value:
        raise LingError(ErrorCode.conflict, "该轮次已完成，不能重试", status_code=409)
    if turn.state in {TurnState.pending.value, TurnState.streaming.value}:
        raise LingError(ErrorCode.conflict, "该轮次仍在生成中", status_code=409)
    cancels = request.app.state.cancels
    if cancels.is_running(turn_id):
        # 协调器仍在收尾：给它一个有界时间结束，而不是立刻拒绝
        for _ in range(30):
            await asyncio.sleep(0.1)
            if not cancels.is_running(turn_id):
                break
        if cancels.is_running(turn_id):
            raise LingError(ErrorCode.conflict, "正在停止，请稍候再重试", status_code=409)
        refreshed = await conversation.get_turn(turn_id)
        if refreshed is not None:
            turn = refreshed
    if turn.state == TurnState.stopping.value:
        # 悬空 stopping（进程重启或协调器已结束）：修正为 stopped 后再重试
        await conversation.transition_turn_if(
            turn_id, from_states={TurnState.stopping}, to_state=TurnState.stopped
        )

    effective = await config.effective()
    messages = await conversation.list_messages(window.id, limit=400)
    user_text = ""
    for msg in reversed(messages):
        if msg.turn_id == turn_id and msg.role == "user":
            user_text = msg.text
            break
    if not user_text:
        raise LingError(ErrorCode.conflict, "找不到原始输入，无法重试", status_code=409)

    # 不删除上一轮已交付的文本：设计文档 §4.3「已经送达的句子不能假装撤回」。
    # 重试产生一条新的 actor 消息（attempt+1），界面按 attempt 编组展示，
    # 用户能看到"上一段未完成"和"重新回答"，而不是旧文本凭空消失。
    bumped = await conversation.bump_attempt(turn_id)
    new_attempt = int(bumped.attempt or 1)
    await conversation.session.commit()

    coordinator = _coordinator(request, settings, router_)
    queue = await coordinator.run_turn(
        window_id=window.id,
        subject_id=auth.user.subject_id,
        turn_id=turn_id,
        user_text=user_text,
        config=effective,
        attempt=new_attempt,
    )
    started = _sse(
        "turn.started",
        {
            "turn_id": turn_id,
            "state": TurnState.streaming.value,
            "retried": True,
            "attempt": new_attempt,
        },
    )
    async def _guarded() -> AsyncIterator[bytes]:
        produced = False
        async for chunk in _forward(queue, started=started):
            if b"event: delta" in chunk:
                produced = True
            yield chunk
        if produced:
            _mark_group_turn(request, window.visibility, window.id)

    return StreamingResponse(
        _guarded(), media_type="text/event-stream", headers=SSE_HEADERS
    )
