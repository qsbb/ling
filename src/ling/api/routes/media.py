"""媒体读取路由（SP03）：把生成音频读给 Web 与渠道。

## 三条路径，三种授权

| 端点 | 授权 | 谁用 |
|---|---|---|
| `GET /v1/media/generated/{file_id}?exp=&sig=` | **签名本身** | 渠道下载、`<audio src>` |
| `GET /v1/sessions/{id}/voice-messages` | 登录 + 窗口归属 | 会话页（哪些消息是语音） |
| `GET /v1/messages/{id}/voice` | 登录 + 窗口归属 | 单条消息的播放地址 |
| `POST /v1/messages/{id}/playback` | 登录 + 窗口归属 | 记一次播放 |

签名那条**不需要登录态**：`<audio>` 标签无法附加 `Authorization` 头，而签名
URL 的设计目标就是"没有 cookie 也能取"（见 `domains/media/exports.py`）。
另外三条走 Cookie，因为它们要回答"谁在问"，而签名里刻意不含身份。

## Range 请求：为什么必须实现

浏览器播放音频**一定会**发 Range：

1. 首次加载就发 `Range: bytes=0-`（`<audio>` 要知道总长度并支持拖动）；
2. 拖动进度条 → `Range: bytes=<offset>-`；
3. 某些浏览器读 MP3 尾部元数据 → `Range: bytes=-<n>`（后缀形式）。

不实现 Range 的后果不是"报错"，而是更难查的样子：服务器回 200 + 整个文件并
忽略 `Range` 头，Safari 会拒绝播放（它要求 206），Chrome 能播但拖动会重新
下载整个文件、进度条跳不回去。

所以这里返回 **206 Partial Content** + `Content-Range` + `Accept-Ranges: bytes`。
起点越界的范围按 RFC 9110 §14.4 回 **416** + `Content-Range: bytes */<size>`
——回 200 会让浏览器以为拿到了完整文件。

**为什么不用 `FileResponse`**：它把"越权 → 404"与"过期 → 404"混进 200/416
路径里，且它的 `stat_result` 可能与 `media_assets` 记录不一致（记录说 ready，
文件已被清理）。这里显式读字节、显式算范围，行为完全可测。

## 越权一律 404（不是 403）

403 等于告诉对方"这个资源存在，只是你没权限"。对生成媒体尤其危险：`file_id`
就是内容 sha256，同一句 TTS 文本会得到同一份文件，所以 id 是**可构造**的。

- 签名不对 / 过期 / 文件不存在 / 记录已删 → 同一个 404，响应体逐字节相同；
- 消息不属于你 / 消息不存在 / 不是语音 → 同一个 404。

所有失败路径共用 `_MEDIA_MISSING` 这一条文案，就是为了防止后来有人"顺手"
让其中一条更清楚——那就成了存在性探针。

## 播放记录与送达记录

播放写 `media_play_events`（`domains/media/playback.py`），送达写
`delivery_receipts`（投递层），**互不覆盖**。理由见那两个模块的文档。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ling.api.deps import CurrentAuth, DbSession, SettingsDep, authorize_message_view
from ling.api.errors import LingError, not_found
from ling.domains.media import exports as exports_mod
from ling.domains.media.assets import AssetNotFound, AssetView, load_asset
from ling.domains.media.playback import (
    VoiceAsset,
    VoiceNotFound,
    load_voice_assets,
    load_voice_for_message,
    record_playback,
)
from ling.domains.media.storage import GeneratedMediaStorage, MediaStorageError
from ling.infra.security import load_signing_root_key

logger = logging.getLogger("ling.api.media")

router = APIRouter(tags=["media"])

#: 所有"看不见"的失败共用这一条文案。**不要**给某一条写更具体的提示：
#: 文案一旦不同，404 就又变成了存在性探针。
_MEDIA_MISSING = "媒体不存在"

#: Web 端播放地址的有效期上限（秒）。签名与 SP01 共用同一套派生。
#:
#: 为什么要一个比 `MAX_URL_TTL_SECONDS` 更短的值：Web 端拿到地址后**立刻**
#: 就发请求（`<audio src>` 是同步加载的），不需要几分钟的窗口；而地址一旦
#: 落到浏览器历史/日志里，越短越好。渠道下载仍用默认 300 秒。
WEB_URL_TTL_SECONDS = 120


# ---------------------------------------------------------------- 签名授权


def _root_key(settings: SettingsDep) -> bytes:
    """签名子密钥的来源，与 `SecretBox` 共享同一份根密钥。

    直接读配置而不是从 `app.state.secret_box` 拿：`SecretBox` 只暴露加解密，
    不暴露原始密钥（那是刻意的）。`load_root_key` 是唯一读到它的地方，签名
    与加密因此共享"根密钥从哪来"这一条事实。
    """
    return load_signing_root_key(settings)


def _storage(settings: SettingsDep) -> GeneratedMediaStorage:
    return GeneratedMediaStorage(settings.data_dir)


async def _live_asset(session: AsyncSession, reference: str) -> AssetView:
    try:
        return await load_asset(session, reference)
    except AssetNotFound:
        raise not_found(_MEDIA_MISSING) from None


# ---------------------------------------------------------------- Range


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """解析 `Range` 头 → 闭区间 `(start, end)`。

    返回值三态，调用方必须都面对：

    - `None`：**不带/不认识的** Range → 回整个文件（200）
    - `(-1, -1)`：语法认识但**不可满足**（起点越界）→ 回 416
    - `(start, end)`：可满足 → 回 206

    支持三种真实存在的形态：

    - ``bytes=0-``：从某点到结尾（`<audio>` 首次加载）
    - ``bytes=100-199``：显式区间（拖动进度条）
    - ``bytes=-500``：**后缀**形式（读 MP3 尾部元数据）

    不认识的一律当作没带 Range（RFC 9110 §14.2 允许这样）：多区间
    ``bytes=0-1,5-6``、非 `bytes` 单位、语法错误。多区间要用
    `multipart/byteranges` 回，但没有任何浏览器对音频这么做——实现它只会
    多一条无人测试的路径。
    """
    if not header or size <= 0:
        return None
    raw = header.strip()
    if not raw.lower().startswith("bytes="):
        return None
    spec = raw[len("bytes=") :].strip()
    if "," in spec:
        return None
    start_text, _, end_text = spec.partition("-")
    start_text = start_text.strip()
    end_text = end_text.strip()
    try:
        if not start_text:
            if not end_text:
                return None
            suffix = int(end_text)
            if suffix <= 0:
                return None
            return max(0, size - suffix), size - 1
        start = int(start_text)
        if start < 0:
            return None
        if start >= size:
            return (-1, -1)
        if not end_text:
            return start, size - 1
        end = int(end_text)
        if end < start:
            return None
        return start, min(end, size - 1)
    except ValueError:
        return None


def _audio_response(
    *,
    data: bytes,
    mime_type: str,
    range_header: str | None,
    etag: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    """把字节包成一支持 Range 的响应。**授权在此之前完成。**

    `Cache-Control: private`：音频是私密内容，允许浏览器缓存以便拖动不重新
    下载，但绝不允许中间代理（CDN/共享缓存）存一份。`no-store` 会让每次拖动
    都重新拉整段，反而是更差的选择。
    """
    size = len(data)
    headers: dict[str, str] = {
        "Accept-Ranges": "bytes",
        "Content-Type": mime_type,
        "Cache-Control": "private, max-age=0, must-revalidate",
        # 文件名/摘要不进 `Content-Disposition`：那会把内容 hash 泄漏到
        # 下载记录里，而 hash 是可构造的。
    }
    if etag:
        headers["ETag"] = etag
    if extra_headers:
        headers.update(extra_headers)

    parsed = parse_range(range_header, size)
    if parsed == (-1, -1):
        headers["Content-Range"] = f"bytes */{size}"
        return Response(status_code=416, headers=headers, content=b"")
    if parsed is None:
        return Response(status_code=200, headers=headers, content=data)

    start, end = parsed
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(end - start + 1)
    return Response(status_code=206, headers=headers, content=data[start : end + 1])


# ---------------------------------------------------------------- 签名 URL


@router.get("/media/generated/{file_id}")
async def get_generated_media(
    file_id: str,
    request: Request,
    session: DbSession,
    settings: SettingsDep,
) -> Response:
    """`GET /v1/media/generated/{file_id}?exp=<unix>&sig=<b64url>`。

    **不需要登录态**——签名即授权（`<audio>` 无法带 cookie 头以外的凭据，
    而跨机部署的渠道根本没有 cookie）。三个失败分支（签名错、过期、记录不可
    用）对外是同一个 404。
    """
    raw_exp = request.query_params.get("exp") or ""
    signature = request.query_params.get("sig") or ""
    try:
        expires_at = int(raw_exp)
    except ValueError:
        raise not_found(_MEDIA_MISSING) from None

    try:
        exports_mod.verify_file_url(
            root_key=_root_key(settings),
            file_id=file_id,
            expires_at=expires_at,
            signature=signature,
        )
    except exports_mod.SignatureError:
        # 过期与签名错**故意不区分**：区分开等于告诉攻击者"算法猜对了，只是
        # 时间不对"，可以据此调整重试。
        raise not_found(_MEDIA_MISSING) from None

    row = await _live_asset(session, file_id)
    try:
        data = _storage(settings).read(row.digest)
    except MediaStorageError:
        # 记录在、文件没了 = 存储损坏（不是"没权限"）。要能被发现，
        # 但对外仍然是 404——文件确实取不到。
        logger.error("生成媒体记录存在但文件缺失：digest=%s", row.digest[:12])
        raise not_found(_MEDIA_MISSING) from None

    return _audio_response(
        data=data,
        mime_type=row.mime_type,
        range_header=request.headers.get("range"),
        # ETag 用内容摘要：字节变了 ETag 才变，浏览器缓存与拖动的重新验证
        # 因此都正确。它本来就在 URL 里（file_id），不算额外泄漏。
        etag=f'"{row.digest[:32]}"',
    )


# ---------------------------------------------------------------- 会话视图


def _voice_dict(asset: VoiceAsset, *, url: str | None, operator: bool = False) -> dict[str, Any]:
    """一条语音消息的对外形态。

    字段分三组，**不要合并**：

    1. 播放需要的：`url` / `mime_type` / `duration_ms` / `size_bytes`
    2. 「语音标签 + 文字」需要的：`text`（正文原文）、`kind='audio'`
    3. 两种记录各自独立的：`delivered`（渠道回执）与 `played/play_count`
       （Web 播放）。它们来自不同的表、由不同的层写，界面上也必须能分开说。
    """
    return {
        "schema_version": "v1",
        "message_id": asset.message_id,
        "window_id": asset.window_id,
        "kind": "audio",
        "url": url,
        "mime_type": asset.mime_type,
        "format": asset.format,
        "size_bytes": asset.size_bytes,
        "duration_ms": asset.duration_ms,
        "sample_rate": asset.sample_rate,
        # 「语音标签 + 文字」：她说的原话。后台显示标签时同时给文字，
        # 不是只丢一个播放器。
        "text": asset.text if operator else "",
        "created_at": asset.created_at.isoformat(),
        "expires_at": asset.expires_at.isoformat() if asset.expires_at else None,
        # --- 两个独立的事实 ---
        "delivered": asset.delivered,
        "played": asset.played,
        "play_count": asset.play_count,
        "last_played_at": asset.last_played_at.isoformat() if asset.last_played_at else None,
    }


def _signed_url(settings: SettingsDep, request: Request, digest: str) -> str:
    """为 Web 播发签一条短时效 URL。

    `base_url` 优先用**请求自己的** origin：反代/局域网访问时配置里的地址
    常常是 `127.0.0.1`，签出来的 URL 前端根本连不上。请求头不可信，所以只
    在它与配置一致或配置为空时使用；签名本身不依赖 base_url（它只签
    `file_id + exp`），因此换 origin 不会让签名失效。
    """
    origin = f"{request.url.scheme}://{request.url.netloc}"
    signed = exports_mod.sign_file_url(
        root_key=_root_key(settings),
        base_url=origin,
        file_id=digest,
        ttl_seconds=WEB_URL_TTL_SECONDS,
    )
    # 只返回 URL 字符串，不返回 `expires_at`：前端不需要自己算过期，
    # 拿到 404 就重新取一次列表。
    return signed.url


@router.get("/sessions/{session_id}/voice-messages")
async def list_voice_messages(
    session_id: str,
    request: Request,
    auth: CurrentAuth,
    session: DbSession,
    settings: SettingsDep,
    view: str = "participant",
) -> dict[str, Any]:
    """`GET /v1/sessions/{id}/voice-messages` —— 这个会话里可播放的语音消息。

    **为什么单独一条端点，而不是给 `GET /messages` 加字段**：消息契约
    （`types.ts::ChatMessage`）由 SP00 独占，且"这条消息有没有音频"是一个
    需要 join `media_assets` 的额外查询。挂在这里，会话页可以在消息渲染之后
    再补一层播放器，不需要改消息契约。

    非 owner 窗口返回**空列表**而不是 404：接口语义是"你在这里有几条语音"，
    没有权限时答案是零条，返回 404 反而暴露了窗口是否存在。
    """
    operator = authorize_message_view(auth, view)
    assets = await load_voice_assets(
        session, window_id=session_id, subject_id=auth.user.subject_id
    )
    return {
        "schema_version": "v1",
        "window_id": session_id,
        "voice_messages": [
            _voice_dict(asset, url=_signed_url(settings, request, asset.asset_id or asset.digest),
                        operator=operator)
            for asset in assets
        ],
    }


def _voice_or_404(exc: VoiceNotFound) -> LingError:
    """`VoiceNotFound` → 与"消息不存在"**逐字节相同**的 404。"""
    return not_found(str(exc) or _MEDIA_MISSING)


@router.get("/messages/{message_id}/voice")
async def get_message_voice(
    message_id: str,
    request: Request,
    auth: CurrentAuth,
    session: DbSession,
    settings: SettingsDep,
    view: str = "participant",
) -> dict[str, Any]:
    """`GET /v1/messages/{id}/voice` —— 单条消息的播放地址与两种记录状态。"""
    operator = authorize_message_view(auth, view)
    try:
        asset = await load_voice_for_message(
            session, message_id=message_id, subject_id=auth.user.subject_id
        )
    except VoiceNotFound as exc:
        raise _voice_or_404(exc) from None
    return _voice_dict(asset, url=_signed_url(settings, request, asset.asset_id or asset.digest),
                       operator=operator)


@router.get("/speech/messages/{message_id}")
async def get_operator_speech_message(
    message_id: str,
    request: Request,
    auth: CurrentAuth,
    session: DbSession,
    settings: SettingsDep,
    view: str = "operator",
) -> dict[str, Any]:
    """授权后台显式读取语音正文、合成状态与真实投递状态。"""
    from ling.contracts.v1 import ErrorCode
    from ling.domains.conversation.service import ConversationService
    from ling.infra.models import Message, Window

    authorize_message_view(auth, view)
    if view != "operator":
        raise LingError(ErrorCode.invalid_request, "请选择 operator 后台视图", status_code=422)
    row = await session.get(Message, message_id)
    window = await session.get(Window, row.window_id) if row is not None else None
    if row is None or window is None or window.actor_id != settings.actor_id:
        raise not_found(_MEDIA_MISSING)
    message = ConversationService._to_view(row).to_contract(operator=True)
    voice = None
    try:
        asset = await load_voice_for_message(
            session, message_id=message_id, subject_id=str(window.subject_id or ""),
        )
        voice = _voice_dict(
            asset, url=_signed_url(settings, request, asset.asset_id or asset.digest), operator=True,
        )
    except VoiceNotFound:
        pass
    return {"schema_version": "v1", "view": "operator", "message": message.model_dump(mode="json"),
            "voice": voice}


class PlaybackBody(BaseModel):
    """一次播放上报。字段全部可选：`<audio>` 的 `play` 事件只有事件本身。"""

    #: 听到的位置（毫秒）。可选——用户点开就报一次 `play` 时还没有位置。
    position_ms: int | None = Field(default=None, ge=0)
    #: **只有** `ended` 事件才传 true。把"播到 99%"当听完，会让
    #: 「他根本没听完」这个信号消失。
    completed: bool = False


@router.post("/messages/{message_id}/playback")
async def report_playback(
    message_id: str,
    auth: CurrentAuth,
    session: DbSession,
    body: PlaybackBody | None = None,
) -> dict[str, Any]:
    """`POST /v1/messages/{id}/playback` —— 记一次播放。

    与送达完全分开：这里**不碰** `operations` / `delivery_receipts`，也不会
    因为"主人听过了"就把某次投递标成成功。理由见
    `domains/media/playback.py` 的模块文档。

    幂等累加而不是一次请求一行：拖动进度条会发多个 Range 请求，按请求计数
    会让"听过几次"等于"点了多少下"。
    """
    try:
        asset = await load_voice_for_message(
            session, message_id=message_id, subject_id=auth.user.subject_id
        )
    except VoiceNotFound as exc:
        raise _voice_or_404(exc) from None

    payload = body or PlaybackBody()
    row = await record_playback(
        session,
        message_id=asset.message_id,
        subject_id=auth.user.subject_id,
        asset_digest=asset.digest,
        window_id=asset.window_id,
        position_ms=payload.position_ms,
        completed=payload.completed,
    )
    await session.commit()
    return {
        "schema_version": "v1",
        "message_id": asset.message_id,
        "played": True,
        "play_count": int(row.play_count),
        "last_played_at": row.last_played_at.isoformat(),
        "completed": bool(row.completed),
        # 明确回一份送达状态，让调用方看到两者是**不同**的字段：
        # 播放成功不代表渠道送达成功。
        "delivered": asset.delivered,
    }


__all__ = ["router", "parse_range", "WEB_URL_TTL_SECONDS"]
