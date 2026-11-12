"""音色参考音频上传：走**真实 HTTP**、**真实数据库**、**真实落盘**。

## 与单元测试的分工

`tests/unit/test_voice_reference_upload.py` 直接调路由函数，证明的是"门禁
本身"（大小 / 格式 / 内容 / 空文件 / owner / 同源）。这里证明的是另外三件
它**证明不了**的事：

1. 路由真的挂在 app 上、状态码真的以 413 / 403 / 422 出去（而不是被别的
   异常处理器吃掉变成 500）；
2. 上传真的在 `media_assets` 里留下一行、文件真的落在 `data_dir/generated_media/`
   下——也就是**读取端 `worker._load_reference_audio` 找得到**它；
3. 端到端：上传 → 拿 asset_id → 存进 `DialoguePolicy.voice.reference_asset_id`
   → 发布 → `_voice_for_tts` 把它解析成 `MimoReferenceVoice`。

第 3 条是这次接线的**唯一目的**。缺了它，"上传接口返回 200"与"她真的能用
这段声音说话"之间没有任何测试建立联系。

## 它**不能**证明什么

- 真实 MiMo 服务端能不能吃这份音频：全程不碰网络；
- 设置页的交互：那是前端（`clients/web-console/`）的活，本次不碰；
- 音频时长：没有解码器，`duration_ms` 一律 `None`（不估算）。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import ling.worker as worker_module
from ling.domains.media.assets import load_asset
from ling.domains.media.storage import GeneratedMediaStorage
from ling.infra.db import session_scope
from ling.infra.media_assets import MediaAssetRow
from ling.infra.security import SecretBox
from ling.ports.config import DialoguePolicy, EffectiveConfig, VoiceSetting
from ling.ports.speech import MimoReferenceVoice
from ling.settings.service import ConfigService
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

#: 最小合法 WAV。够 `sniff_container` 认出 `RIFF....WAVE`。
WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt " + bytes(32) + b"data\x00\x00\x00\x00"

#: 内容不是音频（PDF 头），但客户端会把它声明成 `audio/wav`。
NOT_AUDIO = b"%PDF-1.7\n" + bytes(128)

URL = "/v1/settings/speech/voice-reference"

#: 请求要带的同源头。
#:
#: **这是测试前缀，不是实现里的放宽**：这个端点走的是 cookie 会话 + 同源校验
#: （`_origin_problem`），而 `httpx` 的测试客户端**不会**像浏览器那样自动带
#: `Origin`。不带就必然 403 `bad_origin`——那是门禁在正确工作（实测过：整组
#: 测试第一次跑全是 403，直到补上这个头）。
#:
#: `base_url` 是 `http://testserver`（见 `tests/integration/conftest.py`），
#: 所以 Origin 必须是同一个 scheme+host+port。
ORIGIN = "http://testserver"
SAME_ORIGIN = {"Origin": ORIGIN}


async def _upload_without_origin(
    logged_in: TestEnv, data: bytes, *, content_type: str = "audio/wav"
):  # noqa: ANN202
    """**不带** Origin 的一次上传（cookie 会话）——用来证明它会被 403。

    浏览器里不存在这种请求：跨站表单提交一定会带发起方 Origin。这里构造它，
    是因为"缺 Origin"与"Origin 不对"是两条不同的拒绝路径，实现抄错时会只漏
    其中一条。
    """
    return await logged_in.client.post(
        URL,
        # 文件名故意带路径穿越：实现**一次都不该用**它。
        files={"upload": ("../../etc/evil.wav", data, content_type)},
    )


async def _upload_with_origin(logged_in: TestEnv, data: bytes, *, content_type: str = "audio/wav"):  # noqa: ANN202
    """带同源头的一次上传——成功路径与"过了门禁之后"的那些断言都用它。"""
    return await logged_in.client.post(
        URL,
        files={"upload": ("../../etc/evil.wav", data, content_type)},
        headers=SAME_ORIGIN,
    )


def _error(response) -> dict:  # noqa: ANN001
    return response.json()["error"]


# --------------------------------------------------------------------------- #
# 一、成功路径：返回形状 + 落库 + 落盘
# --------------------------------------------------------------------------- #


async def test_upload_returns_asset_id_and_persists_a_live_grant(logged_in: TestEnv) -> None:
    """上传成功返回不透明 asset_id，逻辑授权独立于 blob 的 SHA-256。

    必须失败于「只返回 id 但不落库」：那种实现下设置页填进去、发布也通过
    （校验只看形态），运行时 `_load_reference_audio` 读不到 → 静默降级发文字，
    而用户看到的是"上传成功"。

    asset_id 不暴露摘要或路径。读取时由 media asset service 验证 grant，
    再解析到内容 blob。
    """
    r = await _upload_with_origin(logged_in, WAV_BYTES)
    assert r.status_code == 200, r.text
    body = r.json()

    digest = hashlib.sha256(WAV_BYTES).hexdigest()
    asset_id = body["asset_id"]
    assert body["schema_version"] == "v1"
    assert asset_id.startswith("asset_")
    assert body["format"] == "wav"
    assert body["mime_type"] == "audio/wav"
    assert body["size_bytes"] == len(WAV_BYTES)
    # 不估算：没有解码器就不知道多久。
    assert body["duration_ms"] is None
    assert body["retention_days"] == 0

    async with session_scope() as session:
        row = await load_asset(session, asset_id)
    assert row is not None, "上传成功却没有媒体记录"
    assert row.expires_at is None, "刚上传的参考素材应当是长效 grant"
    assert row.kind == "audio"
    assert row.format == "wav"
    assert row.mime_type == "audio/wav"
    assert row.digest == digest
    assert row.purpose == "tts_reference"
    assert row.window_id == "__voice_reference__", row.window_id

    # 文件真的落在 `data_dir/generated_media/` 下——也就是读取端去的那个根。
    storage = GeneratedMediaStorage(logged_in.settings.data_dir)
    assert storage.exists(digest), "文件没有落到读取端的存储根下"
    assert storage.read(digest) == WAV_BYTES


async def test_uploaded_file_matches_what_the_read_side_loads(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用**读取端自己的代码**（`worker._load_reference_audio`）读回这段素材。

    必须失败于「上传写到 `data_dir/media/` 而读取端从 `data_dir/` 找」：上一轮
    踩过的坑就是这一层目录之差，症状是"合成成功、投递说找不到文件"。这条直接
    调读端，不自己拼路径，所以写入侧与读取侧只要差一层就红。

    ## 为什么要 patch `worker.get_settings`

    读端读的是 `get_settings()`（环境变量 → 默认 `./.data`），而测试的
    `data_dir` 是 fixture 给的临时目录。不 patch 的话这条会永远是红的，而且
    红在"读不到"，与"写入写错目录"是同一个症状——那就分不清了。

    **这是测试环境的接线，不是放宽断言**：patch 之后两侧用的是**同一个**
    `data_dir`，于是这条才真的在测"写入与读取的根是否一致"。仓库里
    `tests/integration/test_life_loop.py` 等处用的是同一个做法。
    """
    monkeypatch.setattr(worker_module, "get_settings", lambda: logged_in.settings)

    r = await _upload_with_origin(logged_in, WAV_BYTES)
    assert r.status_code == 200, r.text
    asset_id = r.json()["asset_id"]

    audio = await worker_module._load_reference_audio(asset_id)
    assert audio is not None, "读端读不到刚上传的素材（写入与读取的根不一致？）"
    assert audio.content == WAV_BYTES
    assert audio.spec.container == "wav"
    assert audio.spec.mime_type == "audio/wav"
    # 文件名由服务端生成，带真实容器扩展名。
    assert audio.filename == "reference.wav"


# --------------------------------------------------------------------------- #
# 二、超过大小上限 → 413（走真实 HTTP）
# --------------------------------------------------------------------------- #


async def test_oversized_upload_over_http_is_413(logged_in: TestEnv) -> None:
    """超上限走真实 HTTP 也是 413，文案里有 MB 数。

    必须失败于「路由没挂上 / 异常处理器把 LingError 吞成 500」：单测只证明
    raise 了 LingError，证明不了它以 413 出去。
    """
    oversized = WAV_BYTES + bytes(10 * 1024 * 1024)
    r = await _upload_with_origin(logged_in, oversized)
    assert r.status_code == 413, r.text
    assert "10MB" in _error(r)["message"], _error(r)["message"]


# --------------------------------------------------------------------------- #
# 三、mime 不在白名单 / 内容不是音频 → 422
# --------------------------------------------------------------------------- #


async def test_non_audio_content_is_rejected_over_http(logged_in: TestEnv) -> None:
    """内容是 PDF、声明成 audio/wav → 422，且**没有**留下任何记录。

    必须失败于「只看 content_type」：这条是变异验证的目标（删掉 magic number
    校验后必须红）。也钉住"被拒的请求不落盘"——`save_bytes` 之前的抛错不会
    在磁盘上留下文件。
    """
    r = await _upload_with_origin(logged_in, NOT_AUDIO, content_type="audio/wav")
    assert r.status_code == 422, r.text
    err = _error(r)
    assert err["detail"] == "voice_reference_invalid_audio"
    assert "wav" in err["message"] and "mp3" in err["message"], err["message"]

    digest = hashlib.sha256(NOT_AUDIO).hexdigest()
    async with session_scope() as session:
        assert await session.get(MediaAssetRow, digest) is None, "被拒的内容留下了记录"
    storage = GeneratedMediaStorage(logged_in.settings.data_dir)
    assert not storage.exists(digest), "被拒的内容落盘了"


# --------------------------------------------------------------------------- #
# 四、空文件 → 422
# --------------------------------------------------------------------------- #


async def test_empty_upload_over_http_is_422(logged_in: TestEnv) -> None:
    """0 字节 → 422 `voice_reference_empty`，不是"格式无法识别"。

    必须失败于「先把空文件当成嗅探失败」：用户看到"这不是可识别的音频"时，
    会去换格式，而他的问题只是选了一个空文件。
    """
    r = await _upload_with_origin(logged_in, b"")
    assert r.status_code == 422, r.text
    assert _error(r)["detail"] == "voice_reference_empty"


# --------------------------------------------------------------------------- #
# 五、非 owner → 403
# --------------------------------------------------------------------------- #


async def test_member_cannot_upload(logged_in: TestEnv) -> None:
    """非 owner 的登录用户 → 403 / not_owner。

    必须失败于「只校验登录不校验角色」：那样任何能登录的人都能往媒体库塞素材。

    做法：直接改库里的角色（owner 是引导出来的那个用户），再上传。
    改的是**同一个会话里的用户**，所以 cookie 依旧有效——这正是"登录了但不是
    owner"这个现实形状。
    """
    from ling.infra.db import session_scope as scope
    from ling.infra.models import User

    async with scope() as session:
        rows = (await session.execute(select(User))).scalars().all()
        assert rows, "引导用户不存在，这条测试的前提不成立"
        for user in rows:
            user.role = "member"

    r = await _upload_with_origin(logged_in, WAV_BYTES)
    assert r.status_code == 403, r.text
    assert _error(r)["detail"] == "not_owner"


async def test_anonymous_cannot_upload(web_env: TestEnv) -> None:
    """没有 cookie / 没有 Bearer → 401（不是 403，也不是 200）。

    必须失败于「路由忘了挂 `CurrentAuth`」：那会让这个写入端点完全公开。
    与上一条配对：一条证明"登录了但不是 owner 被拒"，这条证明"根本没登录被拒"，
    两条一起才能区分"角色判定"与"登录判定"各自在工作。
    """
    r = await web_env.client.post(URL, files={"upload": ("a.wav", WAV_BYTES, "audio/wav")})
    assert r.status_code == 401, r.text


# --------------------------------------------------------------------------- #
# 五·五、同源校验在 HTTP 层的两条（与单测的 `_Request` 替身配对）
# --------------------------------------------------------------------------- #


async def test_cookie_request_without_origin_is_403_over_http(logged_in: TestEnv) -> None:
    """cookie 会话 + 不带 Origin → 403 `bad_origin`。

    必须失败于「multipart 端点索性不做同源校验」：这条在实现删掉
    `_origin_problem` 之后会变成 200——也就是一个第三方页面用自动提交的
    ``<form enctype=multipart/form-data>`` 就能塞文件进她的媒体库。

    实测注记：整组测试第一次跑时**每一条成功路径都是 403**，原因就是 httpx
    不会像浏览器那样自动带 Origin。那不是实现错，是这条门的正常反应——顺带
    证明了它真的在拦（否则那些 200 的用例不会红）。
    """
    r = await _upload_without_origin(logged_in, WAV_BYTES)
    assert r.status_code == 403, r.text
    assert _error(r)["detail"] == "bad_origin"


async def test_cross_origin_upload_is_403_over_http(logged_in: TestEnv) -> None:
    """cookie 会话 + 别的站点 Origin → 403 `bad_origin`。

    与上一条**配对**：缺 Origin 与 Origin 不对是两条路径，只测一条时，
    "缺了就当同源放行"的实现照样全绿。这条要的是"带了、但是别人的"。
    """
    r = await logged_in.client.post(
        URL,
        files={"upload": ("a.wav", WAV_BYTES, "audio/wav")},
        headers={"Origin": "http://evil.example"},
    )
    assert r.status_code == 403, r.text
    assert _error(r)["detail"] == "bad_origin"


# --------------------------------------------------------------------------- #
# 六、长效保留：参考素材不能被 7 天清理带走
# --------------------------------------------------------------------------- #


async def test_uploaded_reference_never_expires(logged_in: TestEnv) -> None:
    """落库的 `expires_at` 必须是 `None`（= 不自动过期）。

    必须失败于「漏传 `retention_days`」：默认 7 天，一周后 `is_live()` 变假、
    `_voice_for_tts` 静默降级发文字。

    还要证明它**真的逃得过清理**：`purge_expired` 之后这一行仍然 live。
    只看 `expires_at is None` 是不够的——`purge_expired` 的判据是
    `expires_at <= now`，一条被别处写成 `datetime.min` 的记录也会"看起来
    没有保留期"但其实已经被标死。
    """
    from ling.domains.media.assets import GeneratedMediaService

    r = await _upload_with_origin(logged_in, WAV_BYTES)
    assert r.status_code == 200, r.text
    asset_id = r.json()["asset_id"]

    async with session_scope() as session:
        row = await load_asset(session, asset_id)
        assert row is not None
        assert row.expires_at is None, f"参考素材带了保留期：{row.expires_at}"

        # 真的跑一遍清理（用一年后的时间，这样任何 7 天保留期都会被扫到）。
        media = GeneratedMediaService(
            session,
            data_dir=logged_in.settings.data_dir,
            root_key=b"x" * 32,
        )
        await media.purge_expired(now=datetime.now(UTC) + timedelta(days=365))
        await session.commit()

    async with session_scope() as session:
        row = await load_asset(session, asset_id)
    assert row is not None, "参考素材被清理任务删掉了"
    assert row.expires_at is None, "参考素材被清理任务误判为可过期"


async def test_dedup_against_an_expiring_row_still_becomes_long_lived(
    logged_in: TestEnv,
) -> None:
    """同一份字节已有一条**7 天保留期**的记录 → 上传参考音频后它变成长效。

    必须失败于「只传 `retention_days=0` 就去重」：`save_bytes` 的去重分支
    遇到有效记录时**直接复用旧行**，既不动 `expires_at` 也不动 `window_id`。
    于是在"用户上传的参考音频恰好与某条已存在的 TTS 产物同字节"时，参考素材
    仍带着 7 天保留期，一周后静默失效。

    这条用一个**先于**上传写入的 7 天行来构造那个撞车点，然后断言上传之后
    这一行的 `expires_at` 变成了 `None`。只写"上传后 expires_at 是 None"的
    测试在去重分支上照样绿——因为它从来没有撞过。
    """
    from ling.domains.media.assets import GeneratedMediaService

    digest = hashlib.sha256(WAV_BYTES).hexdigest()
    async with session_scope() as session:
        media = GeneratedMediaService(
            session,
            data_dir=logged_in.settings.data_dir,
            root_key=b"x" * 32,
            actor_id=logged_in.settings.actor_id,
        )
        saved = await media.save_bytes(
            WAV_BYTES,
            actor_id=logged_in.settings.actor_id,
            window_id="some_real_window",
            fmt="wav",
            mime_type="audio/wav",
            retention_days=7,
        )
        await session.commit()
    assert saved.digest == digest
    async with session_scope() as session:
        row = await session.get(MediaAssetRow, digest)
        assert row is not None and row.expires_at is not None, "前提不成立：这不是一条会过期的行"

    r = await _upload_with_origin(logged_in, WAV_BYTES)
    assert r.status_code == 200, r.text
    reference_id = r.json()["asset_id"]

    async with session_scope() as session:
        row = await load_asset(session, reference_id)
    assert row is not None
    assert row.digest == digest
    assert row.expires_at is None, "新参考素材 grant 必须长效，即便磁盘 blob 被其他用途引用"


# --------------------------------------------------------------------------- #
# 七、端到端：上传 → 配置 → 发布 → `_voice_for_tts` 解析成 MimoReferenceVoice
# --------------------------------------------------------------------------- #


async def _config(session):  # noqa: ANN001,ANN202
    from ling.config import get_settings

    settings = get_settings()
    return ConfigService(
        session,
        SecretBox(settings),
        test_mode_default=settings.test_mode,
        persona_id=settings.persona_id,
    )


async def test_upload_then_configure_then_resolve_is_the_whole_gap(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-10 的后半句，端到端一次走完：

    上传参考音频 → 拿 asset_id → 存进 `DialoguePolicy.voice.reference_asset_id`
    → 发布 → `worker._voice_for_tts` 把它解析成 `MimoReferenceVoice`。

    **必须失败于**这次要补的那个缺口本身（"没有任何入口能把音频传上去"）：

    - 上传端点不存在 → 第一步 404，后面全废；
    - 上传返回的不是内容摘要 → 设置页校验把 `reference_asset_id` 判成非法
      （`test_..._returns_digest_and_persists_a_live_row` 先红）；
    - 上传的字节落错目录 → 这里 `_voice_for_tts` 返回 `None`（读端读不到）；
    - 配置 serde 漏 `reference_asset_id` → 发布后 `effective().dialogue.voice`
      里是 None → 同样返回 `None`。

    这条**不 patch** `_voice_for_tts`、不 patch `_load_reference_audio`、也不
    替换成假值对象，走的是生产上那条路：HTTP 上传 → `save_bytes` →
    `ConfigService.save_draft` → `publish` → `effective()` → 读库 → 读文件 →
    构造 `AudioInput` → `MimoReferenceVoice`。

    唯一的替身是 `worker.get_settings`（见
    `test_uploaded_file_matches_what_the_read_side_loads` 的说明）：读端按
    环境变量取 `data_dir`，而测试的目录是 fixture 给的临时目录。不换这一处，
    这条会红在"读不到"，与真正的写错目录无法区分。
    """
    monkeypatch.setattr(worker_module, "get_settings", lambda: logged_in.settings)

    r = await _upload_with_origin(logged_in, WAV_BYTES)
    assert r.status_code == 200, r.text
    asset_id = r.json()["asset_id"]

    async with session_scope() as session:
        config = await _config(session)
        result = await config.save_draft(
            dialogue={
                "voice": {"kind": "mimo_tts", "reference_asset_id": asset_id},
            },
            note="音色参考素材端到端",
        )
        voice_errors = [i for i in result.validation_errors if i.field.startswith("dialogue.voice")]
        assert not voice_errors, f"上传返回的 asset_id 被设置页校验拦下了：{voice_errors}"
        await config.publish(result.revision)

    async with session_scope() as session:
        config = await _config(session)
        effective = await config.effective()
        resolved = await worker_module._voice_for_tts("mimo_tts", effective=effective)

    assert effective.dialogue.voice.reference_asset_id == asset_id
    assert isinstance(resolved, MimoReferenceVoice), (
        f"上传的参考音频没有解析成 MimoReferenceVoice：{resolved!r}"
    )
    assert resolved.audio.content == WAV_BYTES
    assert resolved.audio.spec.mime_type == "audio/wav"


async def test_mimo_voice_without_upload_still_degrades(logged_in: TestEnv) -> None:
    """配了 `kind=mimo_tts` 但 `reference_asset_id` 指向不存在的素材 → `None`。

    与上一条**配对**：只测"上传后能解析"，一个"不管素材在不在都造一个
    `MimoReferenceVoice`"的实现同样全绿，而它会把一个空 `AudioInput` 送进请求
    ——那是"合成请求发出去了、对方报 400"或者更糟：一段噪音。

    这里刻意用一个**形状合法但库里没有**的 64 位十六进制串，证明降级发生在
    "读不到素材"这一环，而不是在"形态校验"那一环。
    """
    missing = hashlib.sha256(b"never-uploaded").hexdigest()

    async with session_scope() as session:
        config = await _config(session)
        result = await config.save_draft(
            dialogue={"voice": {"kind": "mimo_tts", "reference_asset_id": missing}},
            note="指向不存在的素材",
        )
        await config.publish(result.revision)

    async with session_scope() as session:
        config = await _config(session)
        effective = await config.effective()
        resolved = await worker_module._voice_for_tts("mimo_tts", effective=effective)

    assert resolved is None


async def test_voice_for_tts_uses_the_policy_from_effective_config() -> None:
    """`_voice_for_tts` 读的是 `effective.dialogue.voice`，不是别处的默认值。

    给一个**只有** `DialoguePolicy.voice` 里那份 id 的 `EffectiveConfig`，
    而库里没有任何素材 → 必须 `None`。如果实现回退到某个默认音色、
    或从环境变量读，这条会返回一个非 None 的值。

    这不是重复上面那条：上面那条证明"发布后能读到"，这条证明"只有那一处是
    来源"——两者在"读的是发布值"与"读的是别的什么"两种实现下给出**相反**的
    结果，而上面那条在"读环境变量"的实现下依然可能绿。
    """
    effective = EffectiveConfig(
        revision=1,
        dialogue=DialoguePolicy(
            voice=VoiceSetting(
                kind="mimo_tts",
                reference_asset_id=hashlib.sha256(b"absent").hexdigest(),
            )
        ),
    )
    resolved = await worker_module._voice_for_tts("mimo_tts", effective=effective)
    assert resolved is None
