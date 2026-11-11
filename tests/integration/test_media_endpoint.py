"""SP03：生成媒体的 HTTP 读取端点（真实 PostgreSQL + 真实磁盘 + 真实 ASGI）。

## 这个文件在守什么

音频从"存在盘上"到"浏览器能听"之间，有四条容易写错、写错又很难发现的东西：

1. **Range**：不实现它**不会报错**——服务器照样 200 回整个文件，Chrome 能播。
   坏掉的是拖动进度条（重新下载整个文件）和 Safari（要求 206，直接不播）。
   所以这里的断言必须精确到 `206` / `Content-Range` 的具体字节区间。
2. **越权 404 而不是 403**：403 等于确认"这个 id 存在"。生成媒体的 id 是
   **内容 sha256，可构造**——同一句 TTS 文本会得到同一份文件——所以存在性
   本身就是要保护的信息。这里逐个路径断言 **404**，并断言响应体与"真的
   不存在"**逐字节相同**。
3. **播放 ≠ 送达**：送达是渠道回执（`delivery_receipts`），播放是主人点了
   播放（`media_play_events`）。合并之后"发出去的没人听"和"没发出去但主人
   自己听了"都无法表达。这里断言两边的行**互不影响**：播放若干次不会新增
   任何 `delivery_receipts`，也不会改 `operations.state`。
4. **「语音标签 + 文字」**：后台必须同时拿到"这是一条语音"（`kind=audio`）与
   **文字原文**。只给播放器等于把内容藏起来。

## 为什么每个断言都对着真实 HTTP

这些行为全部发生在 ASGI 层与头部处理上（`Range` 解析、状态码、响应头），
用 mock 替换调用路径就测不到它们。所以走 `AsyncClient` + 真实 app + 真库。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import select

from ling.domains.media.assets import GeneratedMediaService
from ling.domains.media.exports import sign_file_url
from ling.infra.db import session_scope
from ling.infra.media_assets import MediaAssetRow
from ling.infra.media_playback import MediaPlayEventRow
from ling.infra.models import DeliveryReceiptRow, Message, Operation, Window
from ling.infra.security import load_signing_root_key

# 夹具以**同名参数**注入（pytest 按名字查找 fixture，不按 import 的符号），
# 所以这里把它们在本模块里重新导出给 pytest 发现。
from tests.integration.conftest import TestEnv

# noqa 说明：下面两个 import 只为让 pytest 找到夹具，模块内并不直接调用。
from tests.integration.conftest import logged_in as logged_in  # noqa: F401
from tests.integration.conftest import web_env as web_env  # noqa: F401

pytestmark = pytest.mark.w0_web

#: 一段"像音频"的字节。测试不关心它能不能播，只关心被原样、按区间存取。
AUDIO = bytes(range(256)) * 8  # 2048 字节，区间边界好算


def _comparable(body: dict) -> dict:
    """去掉 `trace_id` 再比较两个 404 体。

    `trace_id` 是**每请求唯一**的诊断 id（`api/errors.py` 的回写），两条 404
    必然不同——它不携带任何"这个 id 存不存在"的信息，所以不参与比较。
    除此之外必须逐字段相同：任何一条更具体的 `message`/`detail` 都是存在性探针。
    """
    import copy

    clone = copy.deepcopy(body)
    error = clone.get("error")
    if isinstance(error, dict):
        error.pop("trace_id", None)
    return clone


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _make_window(env: TestEnv, subject_id: str, *, title: str = "语音会话") -> str:
    async with session_scope() as session:
        from ling.domains.conversation.service import ConversationService

        window = await ConversationService(
            session, actor_id=env.settings.actor_id
        ).create_window(subject_id=subject_id, title=title)
        await session.commit()
        return window.id


async def _make_voice_message(
    env: TestEnv,
    window_id: str,
    *,
    text: str = "晚安，早点睡。",
    data: bytes = AUDIO,
    digest: str | None = None,
    role: str = "actor",
    with_asset: bool = True,
) -> tuple[str, str]:
    """造一条语音消息（+ 对应资产），返回 (message_id, digest)。"""
    payload = data if digest is None else data
    async with session_scope() as session:
        message = Message(
            id=f"msg_{hashlib.sha256(text.encode() + window_id.encode()).hexdigest()[:16]}",
            window_id=window_id,
            role=role,
            text=text,
            state="completed",
            sequence=1,
            origin="model_inference",
        )
        session.add(message)
        await session.flush()
        saved_digest = digest or _digest(payload)
        if with_asset:
            await GeneratedMediaService(
                session,
                data_dir=env.settings.data_dir,
                root_key=load_signing_root_key(env.settings),
            ).save_bytes(
                payload,
                actor_id=env.settings.actor_id,
                window_id=window_id,
                kind="audio",
                fmt="mp3",
                duration_ms=2400,
                message_id=message.id,
                producer="tts.test",
                retention_days=0,  # 测试里不要过期
            )
        await session.commit()
    return message.id, saved_digest


def _signed(env: TestEnv, digest: str, *, ttl: int = 300, file_id: str | None = None) -> str:
    signed = sign_file_url(
        root_key=load_signing_root_key(env.settings),
        base_url="http://testserver",
        file_id=file_id or digest,
        ttl_seconds=ttl,
    )
    # 只取 path+query：base_url 由签名函数拼好，这里换成相对路径交给 client。
    return signed.url[len("http://testserver") :]


# ===================================================================== 签名 URL


async def test_signed_url_full_body_200(web_env: TestEnv) -> None:
    """不带 Range → 200 + 完整字节 + `Accept-Ranges: bytes`。"""
    env = web_env
    window_id = await _make_window(env, "subj_owner")
    _, digest = await _make_voice_message(env, window_id)

    response = await env.client.get(_signed(env, digest))

    assert response.status_code == 200
    assert response.content == AUDIO
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-type"].startswith("audio/")
    assert "content-range" not in response.headers


async def test_signed_url_range_prefix_206(web_env: TestEnv) -> None:
    """`bytes=0-`：浏览器首次加载的形态 → 206，覆盖整个文件。"""
    env = web_env
    window_id = await _make_window(env, "subj_owner")
    _, digest = await _make_voice_message(env, window_id)

    response = await env.client.get(_signed(env, digest), headers={"Range": "bytes=0-"})

    assert response.status_code == 206
    assert response.content == AUDIO
    assert response.headers["content-range"] == f"bytes 0-{len(AUDIO) - 1}/{len(AUDIO)}"
    assert response.headers["content-length"] == str(len(AUDIO))


async def test_signed_url_range_middle_206(web_env: TestEnv) -> None:
    """拖动进度条：`bytes=1000-1099` 必须回这 100 个字节，且与原文同位置一致。"""
    env = web_env
    window_id = await _make_window(env, "subj_owner")
    _, digest = await _make_voice_message(env, window_id)

    response = await env.client.get(
        _signed(env, digest), headers={"Range": "bytes=1000-1099"}
    )

    assert response.status_code == 206
    assert response.content == AUDIO[1000:1100]
    assert response.headers["content-range"] == f"bytes 1000-1099/{len(AUDIO)}"
    assert response.headers["content-length"] == "100"


async def test_signed_url_range_suffix_206(web_env: TestEnv) -> None:
    """`bytes=-256`：读尾部元数据的形态 → 最后 256 字节。"""
    env = web_env
    window_id = await _make_window(env, "subj_owner")
    _, digest = await _make_voice_message(env, window_id)

    response = await env.client.get(_signed(env, digest), headers={"Range": "bytes=-256"})

    assert response.status_code == 206
    assert response.content == AUDIO[-256:]
    assert response.headers["content-range"] == (
        f"bytes {len(AUDIO) - 256}-{len(AUDIO) - 1}/{len(AUDIO)}"
    )


async def test_signed_url_range_end_beyond_size_is_clamped(web_env: TestEnv) -> None:
    """结束位置超长要**夹到文件尾**，而不是 416：RFC 允许，浏览器也这么发。"""
    env = web_env
    window_id = await _make_window(env, "subj_owner")
    _, digest = await _make_voice_message(env, window_id)

    response = await env.client.get(
        _signed(env, digest), headers={"Range": f"bytes=2000-{len(AUDIO) * 4}"}
    )

    assert response.status_code == 206
    assert response.content == AUDIO[2000:]
    assert response.headers["content-range"] == f"bytes 2000-{len(AUDIO) - 1}/{len(AUDIO)}"


async def test_signed_url_range_start_past_end_416(web_env: TestEnv) -> None:
    """起点越界 → **416** + `Content-Range: bytes */<size>`（不能回 200）。"""
    env = web_env
    window_id = await _make_window(env, "subj_owner")
    _, digest = await _make_voice_message(env, window_id)

    response = await env.client.get(
        _signed(env, digest), headers={"Range": f"bytes={len(AUDIO)}-"}
    )

    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(AUDIO)}"


async def test_unknown_range_header_is_ignored(web_env: TestEnv) -> None:
    """不认识的 Range 按 RFC 当作没带：回 200 全文，而不是 416。"""
    env = web_env
    window_id = await _make_window(env, "subj_owner")
    _, digest = await _make_voice_message(env, window_id)
    url = _signed(env, digest)

    for header in ("bytes=0-1,5-6", "items=0-10", "bytes=abc-def"):
        response = await env.client.get(url, headers={"Range": header})
        assert response.status_code == 200, header
        assert response.content == AUDIO, header


# ================================================================= 越权与失效


async def test_bad_signature_is_404_not_403(web_env: TestEnv) -> None:
    """签名不对 → 404，且与"资产不存在"**逐字节相同**。"""
    env = web_env
    window_id = await _make_window(env, "subj_owner")
    _, digest = await _make_voice_message(env, window_id)

    signed = sign_file_url(
        root_key=load_signing_root_key(env.settings),
        base_url="http://testserver",
        file_id=digest,
        ttl_seconds=300,
    )
    forged = signed.url.replace("sig=", "sig=AAAA")[len("http://testserver") :]
    response = await env.client.get(forged)
    missing = await env.client.get(_signed(env, "f" * 64, file_id="f" * 64))

    assert response.status_code == 404
    assert missing.status_code == 404
    assert _comparable(response.json()) == _comparable(
        missing.json()
    ), "签名错与不存在必须不可区分（除 trace_id）"


async def test_expired_signature_is_404(web_env: TestEnv) -> None:
    """过期 → 404（不区分"过期"与"签名错"，否则等于确认算法猜对了）。"""
    env = web_env
    window_id = await _make_window(env, "subj_owner")
    _, digest = await _make_voice_message(env, window_id)

    from ling.domains.media.exports import sign_file_url as _sign

    past = _sign(
        root_key=load_signing_root_key(env.settings),
        base_url="http://testserver",
        file_id=digest,
        ttl_seconds=300,
        now=1_600_000_000,
    )
    response = await env.client.get(past.url[len("http://testserver") :])

    assert response.status_code == 404


async def test_signature_for_other_file_does_not_work(web_env: TestEnv) -> None:
    """拿 A 的签名去取 B（IDOR 的经典入口）→ 404。"""
    env = web_env
    window_id = await _make_window(env, "subj_owner")
    _, digest = await _make_voice_message(env, window_id)

    other = _digest(b"another audio")
    # 签名里 file_id 是 A，但路径换成 B。
    signed = sign_file_url(
        root_key=load_signing_root_key(env.settings),
        base_url="http://testserver",
        file_id=digest,
        ttl_seconds=300,
    )
    url = signed.url[len("http://testserver") :].replace(digest, other)
    response = await env.client.get(url)

    assert response.status_code == 404


async def test_soft_deleted_asset_is_404(web_env: TestEnv) -> None:
    """记录被软删（过期清理）后，旧签名立刻失效（404），不是继续给字节。"""
    env = web_env
    window_id = await _make_window(env, "subj_owner")
    _, digest = await _make_voice_message(env, window_id)
    url = _signed(env, digest)

    assert (await env.client.get(url)).status_code == 200
    async with session_scope() as session:
        row = await session.get(MediaAssetRow, digest)
        assert row is not None
        from ling.infra.media_assets import utcnow

        row.deleted_at = utcnow()
        await session.commit()

    assert (await env.client.get(url)).status_code == 404


# ============================================================= 会话语音列表


async def test_voice_messages_lists_label_and_text(
    logged_in: TestEnv,
) -> None:
    """用户要求：后台看得见「语音标签 + 文字」，不是只有一个播放器。"""
    env = logged_in
    window_id = await _make_window(env, env.subject_id)
    message_id, digest = await _make_voice_message(
        env, window_id, text="我到楼下了，你下来吧。"
    )

    response = await env.client.get(f"/v1/sessions/{window_id}/voice-messages")

    assert response.status_code == 200
    body = response.json()
    assert body["window_id"] == window_id
    items = body["voice_messages"]
    assert len(items) == 1
    item = items[0]
    assert item["message_id"] == message_id
    # 「这是语音」：标签靠 kind + mime + 播放地址
    assert item["kind"] == "audio"
    assert item["mime_type"].startswith("audio/")
    assert "/v1/media/generated/" in item["url"]
    assert "sig=" in item["url"]
    # 「文字是什么」：正文原文必须在，且与消息表一致
    assert item["text"] == "我到楼下了，你下来吧。"
    assert item["duration_ms"] == 2400
    # 两种记录各自独立（此刻都没发生）
    assert item["delivered"] is False
    assert item["played"] is False
    assert item["play_count"] == 0


async def test_voice_messages_excludes_non_voice_and_foreign_windows(
    logged_in: TestEnv,
) -> None:
    """没有音频的消息不出现；**别人的窗口**返回空列表（不泄漏窗口是否存在）。"""
    env = logged_in
    mine = await _make_window(env, env.subject_id)
    await _make_voice_message(env, mine, text="这条有音频")
    # 一条纯文字消息（无音频）
    async with session_scope() as session:
        session.add(
            Message(
                id="msg_plain_text",
                window_id=mine,
                role="actor",
                text="这条没有音频",
                state="completed",
                sequence=2,
                origin="model_inference",
            )
        )
        await session.commit()

    response = await env.client.get(f"/v1/sessions/{mine}/voice-messages")
    texts = [item["text"] for item in response.json()["voice_messages"]]
    assert texts == ["这条有音频"]

    foreign = await _make_window(env, "subj_someone_else", title="别人的")
    foreign_body = await env.client.get(f"/v1/sessions/{foreign}/voice-messages")
    assert foreign_body.status_code == 200
    assert foreign_body.json()["voice_messages"] == []


async def test_user_voice_messages_are_not_in_this_list(
    logged_in: TestEnv,
) -> None:
    """用户的语音属于入站媒体（另一套授权），不进这张列表。

    混进来会让"播放"同时表示"她说的"和"你说的"，统计立刻失去意义。
    """
    env = logged_in
    window_id = await _make_window(env, env.subject_id)
    await _make_voice_message(env, window_id, text="她说的", role="actor")
    await _make_voice_message(env, window_id, text="用户说的", role="user")

    response = await env.client.get(f"/v1/sessions/{window_id}/voice-messages")
    assert [i["text"] for i in response.json()["voice_messages"]] == ["她说的"]


def test_signed_url_ttl_is_short_for_web() -> None:
    """Web 播放地址的有效期必须短：它落在浏览器历史/日志里。"""
    from ling.api.routes.media import WEB_URL_TTL_SECONDS
    from ling.domains.media.exports import MAX_URL_TTL_SECONDS

    assert 0 < WEB_URL_TTL_SECONDS <= MAX_URL_TTL_SECONDS
    assert WEB_URL_TTL_SECONDS <= 300


# =================================================== 播放记录 vs 送达记录


async def test_playback_is_recorded_separately_from_delivery(
    logged_in: TestEnv,
) -> None:
    """**核心设计断言**：播放写 `media_play_events`，送达写 `delivery_receipts`。

    构造"渠道已送达"的事实（一条 `operations` + `delivery_receipts`），然后
    只做播放，验证：

    1. 播放次数与 `play_count` 累加；
    2. **`delivery_receipts` 行数不变**（播放不改送达）；
    3. **`operations.state` 不变**（自听不能把失败的发送洗成成功）。
    """
    env = logged_in
    window_id = await _make_window(env, env.subject_id)
    message_id, digest = await _make_voice_message(env, window_id, text="听我说")

    # 造一条"已送达"的投递事实。
    async with session_scope() as session:
        session.add(
            Operation(
                id="op_deliver_1",
                kind="speech.deliver",
                actor_id=env.settings.actor_id,
                state="succeeded",
                payload={"message_id": message_id},
            )
        )
        session.add(
            DeliveryReceiptRow(
                id="rcpt_1",
                operation_id="op_deliver_1",
                channel="onebot",
                state="sent",
                platform_message_id="100200301",
            )
        )
        await session.commit()

    before = await env.client.get(f"/v1/messages/{message_id}/voice")
    assert before.json()["delivered"] is True
    assert before.json()["played"] is False
    assert before.json()["play_count"] == 0

    # 播放两次
    first = await env.client.post(f"/v1/messages/{message_id}/playback", json={})
    second = await env.client.post(
        f"/v1/messages/{message_id}/playback", json={"position_ms": 2400, "completed": True}
    )
    assert first.status_code == 200
    assert first.json()["play_count"] == 1
    assert second.status_code == 200
    assert second.json()["play_count"] == 2
    assert second.json()["completed"] is True
    assert second.json()["delivered"] is True  # 送达事实没被播放覆盖

    async with session_scope() as session:
        plays = list((await session.execute(select(MediaPlayEventRow))).scalars())
        receipts = list((await session.execute(select(DeliveryReceiptRow))).scalars())
        op = await session.get(Operation, "op_deliver_1")
        asset = await session.get(MediaAssetRow, digest)

    # 播放：一行（一人一消息），累加计数
    assert len(plays) == 1
    assert plays[0].message_id == message_id
    assert plays[0].play_count == 2
    assert plays[0].completed is True
    assert plays[0].asset_digest == digest
    # 送达：仍然是那一行，没被播放改动
    assert [r.state for r in receipts] == ["sent"]
    assert op is not None and op.state == "succeeded"
    # 资产表**不记录播放**（它刻意不记是否发送/播放）
    assert asset is not None
    assert not hasattr(asset, "play_count")


async def test_playback_does_not_flip_failed_delivery(
    logged_in: TestEnv,
) -> None:
    """投递失败但主人在本机听了 → operation 仍必须是 failed。"""
    env = logged_in
    window_id = await _make_window(env, env.subject_id)
    message_id, _digest_value = await _make_voice_message(env, window_id, text="失败也要能听")

    async with session_scope() as session:
        session.add(
            Operation(
                id="op_deliver_failed",
                kind="speech.deliver",
                actor_id=env.settings.actor_id,
                state="failed",
                payload={"message_id": message_id},
            )
        )
        session.add(
            DeliveryReceiptRow(
                id="rcpt_failed",
                operation_id="op_deliver_failed",
                channel="onebot",
                state="failed",
                detail="连接超时",
            )
        )
        await session.commit()

    played = await env.client.post(f"/v1/messages/{message_id}/playback", json={})
    assert played.status_code == 200
    assert played.json()["played"] is True
    assert played.json()["delivered"] is False

    async with session_scope() as session:
        op = await session.get(Operation, "op_deliver_failed")
    assert op is not None and op.state == "failed", "自听不能把失败的投递洗成成功"


async def test_playback_appears_in_session_list(
    logged_in: TestEnv,
) -> None:
    """播放之后，会话列表里这条的 `played` 变真（前端据此换图标）。"""
    env = logged_in
    window_id = await _make_window(env, env.subject_id)
    message_id, _ = await _make_voice_message(env, window_id, text="点我")

    await env.client.post(f"/v1/messages/{message_id}/playback", json={})
    body = (await env.client.get(f"/v1/sessions/{window_id}/voice-messages")).json()
    item = body["voice_messages"][0]
    assert item["played"] is True
    assert item["play_count"] == 1
    assert item["last_played_at"] is not None


# ================================================================= 单条端点


async def test_messages_voice_endpoint_404_for_plain_message(
    logged_in: TestEnv,
) -> None:
    """没有音频的消息 → 404，与"消息不存在"逐字节相同。"""
    env = logged_in
    window_id = await _make_window(env, env.subject_id)
    async with session_scope() as session:
        session.add(
            Message(
                id="msg_no_audio",
                window_id=window_id,
                role="actor",
                text="纯文字",
                state="completed",
                sequence=1,
                origin="model_inference",
            )
        )
        await session.commit()

    plain = await env.client.get("/v1/messages/msg_no_audio/voice")
    missing = await env.client.get("/v1/messages/msg_does_not_exist/voice")

    assert plain.status_code == 404
    assert missing.status_code == 404
    assert _comparable(plain.json()) == _comparable(missing.json())


async def test_messages_voice_endpoint_404_for_foreign_message(
    logged_in: TestEnv,
) -> None:
    """别人的语音消息 → 404（不是 403），且与不存在不可区分。"""
    env = logged_in
    foreign = await _make_window(env, "subj_someone_else", title="别人的")
    message_id, _ = await _make_voice_message(env, foreign, text="不该被听到")

    response = await env.client.get(f"/v1/messages/{message_id}/voice")
    missing = await env.client.get("/v1/messages/msg_nope/voice")

    assert response.status_code == 404
    assert _comparable(response.json()) == _comparable(missing.json())
    # 播放上报也必须 404，不能凭 message_id 写进别人的记录
    playback = await env.client.post(f"/v1/messages/{message_id}/playback", json={})
    assert playback.status_code == 404

    async with session_scope() as session:
        rows = list((await session.execute(select(MediaPlayEventRow))).scalars())
    assert rows == [], "越权播放不能留下任何记录"


async def test_voice_url_actually_plays_back(
    logged_in: TestEnv,
) -> None:
    """闭环：列表里的 url **不带 cookie** 也能取到完整字节，且支持 Range。"""
    env = logged_in
    window_id = await _make_window(env, env.subject_id)
    await _make_voice_message(env, window_id, text="闭环")

    body = (await env.client.get(f"/v1/sessions/{window_id}/voice-messages")).json()
    url = body["voice_messages"][0]["url"]
    path = url[len("http://testserver") :]

    # 用**另一个**没有任何 cookie 的客户端取：模拟 <audio src>
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=env.app), base_url="http://testserver"
    ) as anon:
        full = await anon.get(path)
        ranged = await anon.get(path, headers={"Range": "bytes=0-99"})

    assert full.status_code == 200
    assert full.content == AUDIO
    assert ranged.status_code == 206
    assert ranged.content == AUDIO[:100]


async def test_window_row_untouched(web_env: TestEnv) -> None:
    """播放只写播放表：窗口行本身不该被改动（避免"读操作写脏"）。"""
    env = web_env
    window_id = await _make_window(env, "subj_owner")
    async with session_scope() as session:
        before = await session.get(Window, window_id)
        before_stamp = (before.created_at, before.title)  # type: ignore[union-attr]

    await _make_voice_message(env, window_id)

    async with session_scope() as session:
        after = await session.get(Window, window_id)
    assert after is not None
    assert (after.created_at, after.title) == before_stamp


async def test_storage_path_never_leaks(web_env: TestEnv) -> None:
    """响应里不能出现磁盘路径：那是绕过一切授权的入口。"""
    env = web_env
    window_id = await _make_window(env, "subj_owner")
    _, digest = await _make_voice_message(env, window_id)

    response = await env.client.get(_signed(env, digest))
    root = Path(env.settings.data_dir).as_posix()
    assert root not in response.text if response.text else True
    # 头部也不许带文件系统路径
    joined = " ".join(f"{k}:{v}" for k, v in response.headers.items())
    assert root not in joined
    assert "generated_media" not in joined
