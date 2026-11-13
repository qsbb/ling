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
import contextlib
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ling.api.deps import (
    ConfigDep,
    ConversationDep,
    CurrentAuth,
    DbSession,
    RouterDep,
    SettingsDep,
)
from ling.api.errors import LingError, not_found
from ling.contracts.v1 import ErrorCode, MediaReadJobOut, TurnState
from ling.infra.models import Window
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
        settings=settings,
        actor_id=settings.actor_id,
        # ``None`` = 本轮用**当前激活**的人格（`domains/persona/current.py`）。
        # 原来是 `settings.persona_id`（环境变量），在设置里切了人格之后
        # Web 对话仍然按旧人设跑——而且不会报错。
        persona_id=None,
        cancels=request.app.state.cancels,
        arbiter=getattr(request.app.state, "arbiter", None),
        stream_log=getattr(request.app.state, "stream_log", None),
        turn_control=getattr(request.app.state, "turn_control", None),
        speak_lease=getattr(request.app.state, "speak_lease", None),
        # 嵌入/重排的 provider 构造时要同步拿密钥；本进程的缓存由启动预热与
        # `_refresh_config_if_stale` 维护。拿不到就退回环境变量（旧部署）。
        secret_reader=getattr(request.app.state, "secret_cache", {}).get,
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
        scenario_key_for,
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
            config_session,
            persona_id=None,  # None = 当前激活的人格
            scenario=scenario_key_for(ctx),  # type: ignore[arg-type]
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
                # 不走环境变量：没传就让 `ConfigService` 从 `Settings` 取
                # （唯一来源），避免"这里抄一份、那里抄一份"。
                persona_id=None,
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

#: 音色参考音频的**唯一**字节上限。
#:
#: 两个端点共用一个常量，因为**四处**必须同时成立，否则表现成"上传说成功、
#: 真到合成时静默降级发文字"：
#:
#: 1. 上传端点 `POST /settings/speech/voice-reference`（`settings.py`）；
#: 2. `worker._load_reference_audio`（读端，超限直接降级）；
#: 3. `models/mimo_tts.MAX_VOICE_BYTES`（适配器，data URL 会膨胀 4/3）；
#: 4. 客户端（设置页的提示文案）。
#:
#: 读端那一份仍在 `worker.py` 模块顶部：那里不能 import 本模块——`api` 是上层，
#: `worker` 拉它会把 HTTP 栈带进 worker 进程。所以两处相等这件事由
#: `tests/unit/test_speech_voice_config.py::test_upload_voice_size_limit_matches_read_side`
#: 盯着。
MAX_VOICE_BYTES = 10 * 1024 * 1024

#: 参考音频允许的容器 → MIME。**取内容嗅探的结果，不看客户端 ``content_type``**。
#:
#: 条目来自 `models/mimo_tts._MIME_BY_CONTAINER`（那家 voiceclone 接口真能吃的
#: 容器），而不是 `domains/media/assets._FORMAT_MIME`——后者是 TTS **产物**的格式
#: 表，含 silk/amr 这类入站参考素材用不上的项。
#:
#: 不收裸 PCM：`sniff_container` 对 PCM 只有启发式判断，一份 10MB 无头字节与
#: "上传了别的东西"无法可靠区分——宁可在入口拒绝，也不要在合成时表现为噪音。
VOICE_MIME_BY_CONTAINER: dict[str, str] = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "m4a": "audio/mp4",
}

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
        # **别指路到不存在的字段**。原来这里写着"把 evidence_id 放进
        # media_evidence_ids"，而 `SendTurnBody` 根本没有这个字段——照着做
        # 什么也不会发生。真实链路是：上传 → worker 用视觉模型描述 →
        # 下一轮自动进她的上下文（同一窗口，最多 3 张）。
        "note": "图片会在被识别后自动进入该会话的下一轮对话，无需额外传参",
    }


# ------------------------------------------------------------------ 视频后台取证作业
#
# 裁决 `docs/ASYNC-TOOL-DECISION.md` §3 第 1 步：**显式**的后台入口。
#
# 三条纪律，写在这里是因为它们决定了下面每一行的形状：
#
# 1. **不从自然语言猜"用户愿不愿意等"**。只有这个端点被真的调用才算授权了
#    一次后台读取。正文非空也不构成理由——"她说了一句话"和"她这句话跟视频
#    有关"是两件事。
# 2. **不改**现有 `<<看视频>>` 同步路径。那条路是"她在这一轮里等"，这个端点
#    是"安排到后台读"，两条互不影响。
# 3. **入队不等于读到结果**。202 的 `note` 只能是"已安排，尚未读到结果"，
#    绝不承诺"稍后告诉你"（裁决 §4 明确不做结果通知）。


class CreateReadJobBody(BaseModel):
    """`POST .../media/{evidence_id}/read-jobs` 的请求体。

    ## 为什么 `feedback_mode` 必须显式给 `evidence_only`

    它是"这个作业允不允许主动补话"的开关，而首期裁决**只允许**产出证据。
    做成默认值（`= "evidence_only"`）会让"少写一个字段"和"明确表示只写证据"
    变成同一件事，将来放开主动补话时，一批老客户端会**静默**升级成"会说话"。
    所以它是必填且只接受一个值：多一个值就是 422，逼迫调用方在升级时显式选择。

    ## 为什么 `request_key` 必填

    后台作业要能承受"用户双击了两次""前端重试了"。幂等键不能由服务端凭空
    生成（每次生成一个新 id 就永远不会幂等），必须调用方给。必填也避免了
    "有的请求带、有的不带"那种半幂等的状态。
    """

    model_config = ConfigDict(extra="forbid")

    #: 首期唯一合法值。放开时**必须**同时改 `domains/media/jobs.py` 与读侧。
    feedback_mode: Literal["evidence_only"]
    #: 调用方的幂等键。同一个人同一窗口同一个键 → 同一个作业。
    request_key: str = Field(min_length=1, max_length=128)

    @model_validator(mode="before")
    @classmethod
    def _explain_missing_fields(cls, data: object) -> object:
        """缺字段时给一句能指路的话。

        pydantic 的原话是 "Field required"，而这条链路上真正的坑是"以为不写
        feedback_mode 就等于 evidence_only"。所以要明说它是**必填**。

        多余字段在 ``extra="forbid"`` 里已经 422 掉了（例如客户端按旧文档传
        ``wait=true``），这里只补缺字段的解释。
        """
        if isinstance(data, dict) and "feedback_mode" not in data:
            raise ValueError(
                "feedback_mode 必填，首期只接受 evidence_only"
                "（这个入口只写证据，不主动补话；不写不等于默认）"
            )
        return data


#: 状态 → 人话。**唯一**的措辞来源：接口、测试、日志都从这里取。
#:
#: 三条硬纪律（裁决 §5 第 7 条）：
#: - 非终态只能说"已安排/正在读"，**不许**说"看到了"；
#: - `unknown` 必须说"不确定"，因为模型可能已经调用过、只是结果没落库——
#:   说成失败会让人以为可以安全重试，而重试可能第二次收费；
#: - 失败类要给出**下一步**（"可以再试一次"），不要只丢一个状态词。
READ_JOB_NOTES: dict[str, str] = {
    "pending": "已安排，尚未读到结果",
    "running": "正在读这个视频，还没读到结果",
    "succeeded": "读完了，结果已存为这条视频的内容",
    "failed": "这次没读成（可以再试一次）",
    "timeout": "读取超时，没读完（可以再试一次）",
    "unknown": "不确定读到没有：读取过程被中断，结果没存下来",
    "cancelled": "已取消，没有读",
    "expired": "排太久了，已失效（可以再试一次）",
    "budget_denied": "这次额度不够，没有读",
}


def _read_job_note(state: str) -> str:
    """状态 → 人话。未知状态给"不知道"，**不猜**成功。"""
    return READ_JOB_NOTES.get(state, "状态未知")


def _read_job_view(row: object) -> dict:
    """`MediaReadJob` → 响应体（`MediaReadJobOut` 的字段集）。

    ## 为什么 `note` 由服务端映射

    文案是**判定的一部分**：`pending` 说成"已安排"、`unknown` 说成"不确定"，
    这些区分如果交给前端拼，各家会拼出不同的意思，而 `unknown` 被拼成"失败"
    就会诱导用户在不该重试的时候重试（重复收费）。所以文案只有一个来源。
    """
    state = str(getattr(row, "state", ""))
    return {
        "call_id": str(getattr(row, "call_id", "")),
        "window_id": str(getattr(row, "window_id", "")),
        "evidence_id": str(getattr(row, "evidence_id", "")),
        "state": state,
        "note": _read_job_note(state),
        "error_code": getattr(row, "error_code", None),
        "result_ref": getattr(row, "result_ref", None),
        "requested_at": getattr(row, "requested_at", None),
        "started_at": getattr(row, "started_at", None),
        "completed_at": getattr(row, "completed_at", None),
        "deadline": getattr(row, "deadline", None),
        "cancel_requested_at": getattr(row, "cancel_requested_at", None),
        "feedback_mode": str(getattr(row, "feedback_mode", "") or "evidence_only"),
    }


async def _media_authorization_ref(
    session: AsyncSession,
    *,
    subject_id: str | None,
    window_id: str,
) -> str | None:
    """登记时生效的那条 `media.inbound` 授权修订，形如 ``subject:revision``。

    ## 为什么复用 `MediaEvidenceService` 的判据而不另写

    媒体授权有**两个**可能的主体：上传者本人，**或**这个窗口的主人
    （群聊里主人授权一次，别人发的图也能被描述）。这套规则写在
    `MediaEvidenceService._granted_consent` 里，顺序即优先级。这里再写一遍
    必然走散——而"哪一条判松了"是最难发现的错。

    所以直接调它（同一套 `ConsentService.get(subject, "media.inbound")` 循环），
    拿到的整条 `ConsentView` 既回答"能不能读"，又给出**应该记哪一条**的
    `revision`（撤权审计时要指回具体的人）。
    """
    from ling.domains.consent.service import ConsentService
    from ling.domains.media.service import INBOUND_SCOPE

    window = await session.get(Window, window_id)
    owner = getattr(window, "subject_id", None)
    seen: set[str] = set()
    for candidate in (subject_id, owner):
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        consent = await ConsentService(session).get(candidate, INBOUND_SCOPE)
        if consent.granted:
            return f"{candidate}:{consent.revision}"
    return None


async def _require_read_job_scope(
    row: object, *, window_id: str
) -> None:
    """作业必须在**这个窗口**里。跨窗口一律按"不存在"处理。

    「不存在」而不是 403：403 等于告诉调用方"这个 id 存在但你无权看"，
    那是一个探测口（可以拿它枚举别人有哪些作业）。
    """
    if str(getattr(row, "window_id", "")) != window_id:
        raise LingError(ErrorCode.not_found, "没有这个读取作业", status_code=404)


@router.post(
    "/sessions/{session_id}/media/{evidence_id}/read-jobs",
    response_model=MediaReadJobOut,
    status_code=202,
)
async def create_media_read_job(
    session_id: str,
    evidence_id: str,
    body: CreateReadJobBody,
    auth: CurrentAuth,
    session: DbSession,
    settings: SettingsDep,
) -> dict:
    """安排一次**后台**视频读取；202 表示"已安排"，不表示"读到了"。

    ## 顺序（每一步都必须在前一步之后）

    1. **窗口归属**：会话必须是当前登录者的。否则 404（不泄露存在性）。
    2. **证据必须是本窗口的已存在视频**：不是视频 → 422（"这个入口只读视频"，
       图片/音频走既有的自动描述）；不存在/跨窗口/已过期 → 404。
    3. **媒体授权**：复用 `MediaEvidenceService` 的判据（上传者本人或窗口主人
       的 `media.inbound`）。没有 → 403，并给出可执行的下一步（去哪儿授权）。
    4. **建作业 + 同事务投 outbox**：提交之后才返回 202。**先提交再返回**是
       硬要求——不然"202 说已安排"和"其实事务回滚了"会同时成立，用户等一个
       永远不会开始的读取。
    5. **幂等**：同一 `(actor, window, request_key)` 命中已有作业 → 返回**同一个**
       call_id（200 语义的 202 重放）；同键但参数不同 → 409。
    6. **在途互斥**：同一证据同一 `tool_version` 已有在途作业 → 409（那会两次
       视觉调用、两次收费，而结果写的是同一行证据）。

    ## 为什么不在这里做预算/额度检查

    裁决把它放在第 4 步（`budget_denied` 是**执行前**的闸）。入口这里做预算
    检查会有两个问题：检查与预留不是原子的（中间会被别的作业抢走额度），而且
    用户会看到"提交成功然后又失败"。所以入口只负责"排上队"，额度在执行前判。
    """
    from ling.domains.conversation.service import ConversationService
    from ling.domains.media.jobs import (
        MEDIA_READ_QUEUE_DEADLINE,
        MEDIA_READ_TOOL_VERSION,
        JobState,
        enqueue_read_job,
    )
    from ling.domains.media.service import MediaEvidenceService
    from ling.infra.models import MediaReadJob
    from ling.infra.security import new_id

    # 1) 窗口归属
    conversation = ConversationService(session, actor_id=settings.actor_id)
    window = await conversation.get_window(session_id, subject_id=auth.user.subject_id)
    if window is None:
        raise LingError(ErrorCode.not_found, "会话不存在", status_code=404)

    # 2) 证据：必须是本窗口的、已存在的、**视频**
    service = MediaEvidenceService(session, actor_id=settings.actor_id)
    evidence = await service.get(evidence_id, window_id=session_id)
    if evidence is None:
        raise LingError(ErrorCode.not_found, "这个会话里没有这条媒体", status_code=404)
    if evidence.kind != "video":
        raise LingError(
            ErrorCode.invalid_request,
            f"这个入口只读视频，收到的是 {evidence.kind}；"
            "图片和语音会自动进入对话，不需要后台读取",
            status_code=422,
        )

    # 3) 媒体授权（复用既有判据，不另写）
    authorization_ref = await _media_authorization_ref(
        session, subject_id=auth.user.subject_id, window_id=session_id
    )
    if authorization_ref is None:
        raise LingError(
            ErrorCode.forbidden,
            "未授权读取入站媒体（scope=media.inbound）。"
            "请先在「授权」页允许「接收媒体」",
            status_code=403,
        )

    now = datetime.now(UTC)
    job = MediaReadJob(
        call_id=new_id("mread"),
        actor_id=settings.actor_id,
        subject_id=auth.user.subject_id,
        window_id=session_id,
        evidence_id=evidence.evidence_id,
        # 来源留空：这个入口是用户**显式**点的，没有"哪一轮的模型想读它"。
        # 裁决明确不从自然语言猜，所以也不该伪造一个 turn 出来。
        source_message_id=None,
        source_turn_id=None,
        source_attempt=None,
        request_key=body.request_key,
        tool_version=MEDIA_READ_TOOL_VERSION,
        mode="video",
        feedback_mode=body.feedback_mode,
        authorization_ref=authorization_ref,
        state=JobState.pending.value,
        requested_at=now,
        deadline=now + MEDIA_READ_QUEUE_DEADLINE,
    )

    try:
        await enqueue_read_job(session, job=job, now=now)
        await session.commit()
    except IntegrityError as exc:
        # 两类唯一冲突，含义完全不同，必须分开说：
        await session.rollback()
        return await _resolve_read_job_conflict(
            session,
            exc,
            session_id=session_id,
            request_key=body.request_key,
            evidence_id=evidence.evidence_id,
            actor_id=settings.actor_id,
        )

    return _read_job_view(job)


async def _resolve_read_job_conflict(
    session: AsyncSession,
    exc: IntegrityError,
    *,
    session_id: str,
    request_key: str,
    evidence_id: str,
    actor_id: str,
) -> dict:
    """唯一冲突 → 幂等重放（同一个作业）或 409（在途互斥 / 同键不同参数）。

    ## 为什么分成两支

    - **`request_key` 唯一约束**：说明"同一个幂等键已经有一个作业了"。这时
      要判断"是不是同一次请求"：参数一致就返回**同一个** call_id（幂等的 202），
      参数不同就 409——悄悄返回旧作业会让调用方以为自己的新请求生效了。
    - **在途部分唯一索引**：同一个证据同一个工具版本已经有一个在跑的作业。
      这时**没有**可以重放的东西（是不同的 request_key 要求的），只能 409
      并告诉用户"这个视频正在读"。

    用约束名区分而不是"看哪条 SQL 报错"：约束名是我们自己起的，稳定；
    而 psycopg 的错误文本会随版本变。
    """
    from sqlalchemy import select

    from ling.domains.media.jobs import MEDIA_READ_TOOL_VERSION
    from ling.infra.models import MediaReadJob

    # psycopg 把约束名放在 `diag.constraint_name`；asyncpg 放在 `constraint_name`。
    # 两个都试，拿不到就退回整段文本——约束名是我们自己起的，比错误文本稳定。
    orig = exc.orig
    detail = str(
        getattr(orig, "constraint_name", None)
        or getattr(getattr(orig, "diag", None), "constraint_name", None)
        or exc
    )
    if "uq_media_read_job_request" in detail:
        existing = (
            await session.execute(
                select(MediaReadJob).where(
                    MediaReadJob.actor_id == actor_id,
                    MediaReadJob.window_id == session_id,
                    MediaReadJob.request_key == request_key,
                )
            )
        ).scalar_one_or_none()
        if existing is None:  # pragma: no cover - 刚撞过唯一键，行必然在
            raise LingError(ErrorCode.conflict, "重复的请求", status_code=409) from exc
        if existing.evidence_id != evidence_id:
            # 同键不同参数：必须分开说，否则调用方以为自己的新请求生效了。
            raise LingError(
                ErrorCode.conflict,
                "同一个 request_key 已经用另一条视频提交过；"
                "换一条视频请换一个 request_key",
                status_code=409,
                detail="request_key_conflict",
            ) from exc
        return _read_job_view(existing)

    if "uq_media_read_job_active_evidence" in detail:
        raise LingError(
            ErrorCode.conflict,
            "这个视频已经有一个正在排队的读取；等它结束再用新的 request_key 提交",
            status_code=409,
            detail="evidence_read_in_flight",
        ) from exc

    # 其它完整性错误（外键、CHECK 等）：是 bug，不是用户输入问题。
    logger.warning(
        "读取作业插入失败（未识别的约束）: %s | tool_version=%s",
        detail[:200],
        MEDIA_READ_TOOL_VERSION,
    )
    raise LingError(ErrorCode.internal, "没能安排这次读取", status_code=500) from exc


@router.get(
    "/sessions/{session_id}/media/read-jobs/{call_id}",
    response_model=MediaReadJobOut,
)
async def get_media_read_job(
    session_id: str,
    call_id: str,
    auth: CurrentAuth,
    session: DbSession,
    settings: SettingsDep,
) -> dict:
    """查一次后台读取的**当前状态**（同窗口作用域）。

    `note` 由服务端映射：`pending`/`running` 只说"已安排/正在读"，
    `unknown` 说"不确定"，**没有**任何状态会说"我看到了"直到 `succeeded`
    ——而入队本身永远不是成功。
    """
    from ling.domains.conversation.service import ConversationService
    from ling.infra.models import MediaReadJob

    conversation = ConversationService(session, actor_id=settings.actor_id)
    if await conversation.get_window(session_id, subject_id=auth.user.subject_id) is None:
        raise LingError(ErrorCode.not_found, "会话不存在", status_code=404)

    row = await session.get(MediaReadJob, call_id)
    if row is None or row.actor_id != settings.actor_id:
        raise LingError(ErrorCode.not_found, "没有这个读取作业", status_code=404)
    await _require_read_job_scope(row, window_id=session_id)
    return _read_job_view(row)


@router.post(
    "/sessions/{session_id}/media/read-jobs/{call_id}/cancel",
    response_model=MediaReadJobOut,
)
async def cancel_media_read_job(
    session_id: str,
    call_id: str,
    auth: CurrentAuth,
    session: DbSession,
    settings: SettingsDep,
) -> dict:
    """取消一次后台读取。

    ## 两种取消，语义不同（裁决 §3 第 2 步）

    - **还没开始**（`pending`）：直接 `cancelled`。什么都没发生，不用等。
    - **已经在跑**（`running`）：**不动 state**，只置 `cancel_requested_at`。
      执行方看到标记后尽力停；真的停下来才落 `cancelled`。这里不能直接写
      `cancelled`——那会让"作业还在跑、可能正在花视觉模型的钱"和"库里说它
      取消了"同时成立，而晚到的结果还会被写进证据。

    ## 已经是终态

    幂等返回当前状态（200），**不报错**：用户点第二次取消不该看到一个错误。
    但也不会改任何东西——终态不可再变（`transition()` 的唯一铁律）。
    """
    from ling.domains.conversation.service import ConversationService
    from ling.domains.media.jobs import JobState, is_terminal
    from ling.infra.models import MediaReadJob

    conversation = ConversationService(session, actor_id=settings.actor_id)
    if await conversation.get_window(session_id, subject_id=auth.user.subject_id) is None:
        raise LingError(ErrorCode.not_found, "会话不存在", status_code=404)

    row = await session.get(MediaReadJob, call_id)
    if row is None or row.actor_id != settings.actor_id:
        raise LingError(ErrorCode.not_found, "没有这个读取作业", status_code=404)
    await _require_read_job_scope(row, window_id=session_id)

    state = JobState(row.state)
    if is_terminal(state):
        return _read_job_view(row)

    now = datetime.now(UTC)
    if state is JobState.pending:
        row.state = JobState.cancelled.value
        row.completed_at = now
    else:
        # running：只置标记，状态不变（见 docstring）
        if row.cancel_requested_at is None:
            row.cancel_requested_at = now
    await session.commit()
    return _read_job_view(row)


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
    return await send_transcribed_turn(session_id, body, request, auth, conversation,
                                       router_, config, settings)


async def send_transcribed_turn(
    session_id: str,
    body: SendTurnBody,
    request: Request,
    auth: CurrentAuth,
    conversation: ConversationDep,
    router_: RouterDep,
    config: ConfigDep,
    settings: SettingsDep,
    *, source_message_id: str | None = None,
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
                        "text": actor_msg.to_contract().text if actor_msg else "",
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
    user_msg = await conversation.adopt_input(
        source_message_id, window.id,
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
        # Web 面板**没有外部连接器**：这条路径上"落库即送达"，没有平台可调，
        # 所以没有渠道能力可声明。显式传 None（不是忘了传）——提示词据此
        # 不列 `[poke:…]` 这类渠道动作，因为它们在 Web 上无处执行。
        channel_capabilities=None,
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
        started_at = time.perf_counter()
        async for chunk in _forward(queue, started=started):
            if b"event: delta" in chunk:
                produced = True
            yield chunk
        if produced:
            _mark_group_turn(request, window.visibility, window.id)
        # 流结束 = 这一轮结束。与 connector 路径用同一个事件名，
        # 这样一条 trace 的链路在两个入口长得一样。
        emit_event(
            logger,
            logging.INFO if produced else logging.WARNING,
            event_name="turn_completed",
            message="轮次完成" if produced else "轮次结束但没有产出",
            attributes={
                "turn_id": turn.id,
                "outcome": "delivered" if produced else "no_text",
                "source": "web",
                "duration_ms": round((time.perf_counter() - started_at) * 1000, 1),
            },
        )

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

    from sqlalchemy import select

    from ling.infra.models import Message

    actor_message = await conversation.session.scalar(
        select(Message)
        .where(
            Message.turn_id == turn_id,
            Message.role == "actor",
            Message.attempt == effective_attempt,
        )
        .order_by(Message.sequence.desc())
        .limit(1)
    )
    voice_reply = actor_message is not None and actor_message.output_modality == "voice"

    stream_log = getattr(request.app.state, "stream_log", None)
    chunks: list = []
    complete = False
    if stream_log is not None and not voice_reply:
        chunks, complete = stream_log.since(
            turn_id=turn_id, attempt=effective_attempt, last_sequence=max(0, since)
        )

    # 兜底文本：进程内存里的半成品（单实例）或已落库的 actor 消息（多实例）
    snapshot = ""
    if not complete and not voice_reply:
        snapshot = request.app.state.cancels.partial(turn_id)
        if not snapshot and actor_message is not None:
            snapshot = conversation._to_view(actor_message).to_contract().text

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
    # 停止信号的作用域是 **(turn_id, attempt)**，必须用数据库里当前的
    # attempt：轮次可能已经重试过（attempt≥2），写错 attempt 就等于写进
    # 别人的那一轮（旧 attempt 的行）——那正是"重试永远没回复"的成因之一。
    attempt = int(turn.attempt or 1)
    control = getattr(request.app.state, "turn_control", None)
    # 跨实例：本实例可能没有这个轮次（它跑在别的实例上），
    # 写一条持久化停止信号，由轮次侧轮询发现。
    #
    # **只在轮次仍未终结时写**：已终结的轮次没有任何读者，写进去只是
    # 给表里堆垃圾（历史上这些垃圾标记会永久卡住同 turn_id 的后续重试）。
    # 但"未终结"不能只看数据库状态：协调器可能已经提交终态、而本地
    # cancels 里还有 task 在收尾，这种情况标记仍然有用（收尾阶段也要能停）。
    write_marker = turn.state in {
        TurnState.pending.value,
        TurnState.streaming.value,
        TurnState.stopping.value,
    } or cancels.is_running(turn_id)
    if control is not None and write_marker:
        await control.request_stop(turn_id, attempt)
    elif control is not None and turn.state in {
        TurnState.completed.value,
        TurnState.stopped.value,
        TurnState.failed.value,
    }:
        # 迟到/重复的停止请求：顺手清掉本 attempt 的残留标记，
        # 让"点了停止但轮次已经结束"不再往表里留一条永不过期的行。
        with contextlib.suppress(Exception):
            await control.clear(turn_id, attempt)
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
        # Web 面板**没有外部连接器**：这条路径上"落库即送达"，没有平台可调，
        # 所以没有渠道能力可声明。显式传 None（不是忘了传）——提示词据此
        # 不列 `[poke:…]` 这类渠道动作，因为它们在 Web 上无处执行。
        channel_capabilities=None,
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
