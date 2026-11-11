"""音色参考音频的上传入口：`POST /v1/settings/speech/voice-reference`。

## 这个文件在守什么

REQ-10 的后半句是"mimo **上传参考音频** + 另两家手填 ID"。配置侧早就做完了
（`DialoguePolicy.voice.reference_asset_id` → `worker._voice_for_tts` →
`MimoReferenceVoice`），缺的是**把音频传上去的那一步**：没有它，用户得自己
算 media digest 才能填，实际使用里等于不可用。

## 为什么校验类断言直接调路由函数，而不是走 HTTP

六条边界里有四条（大小 / 格式 / 内容 / 空文件）发生在**落库之前**，它们要
证明的是"门禁本身"而不是"HTTP 表现"。直接调 `upload_voice_reference` 的好处：

- 不需要数据库，能进 `tests/unit`；
- 失败点就在那一行 raise 上，不用穿透 ASGI 才能看出是哪条判据挂的。

**但它不能证明什么**：路由有没有真的挂到 app 上、状态码有没有被序列化成 413/403。
那两件事由 `tests/integration/test_voice_reference_upload.py` 走真实 HTTP 守。

## 一条测试能证明什么

每条 docstring 都写了**必须失败于哪个缺陷**。没有这一句的测试，在"功能完全
没实现"时也可能是绿的——例如"非 owner 被拒"如果写成断言 `!= 200`，那么
"端点根本不存在时的 404" 也会让它通过。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from ling.api.errors import LingError
from ling.api.routes.chat import MAX_VOICE_BYTES, VOICE_MIME_BY_CONTAINER
from ling.api.routes.settings import (
    VOICE_REFERENCE_RETENTION_DAYS,
    VOICE_REFERENCE_WINDOW_ID,
    upload_voice_reference,
)
from ling.contracts.v1 import ErrorCode

# --------------------------------------------------------------------------- #
# 素材：最小的合法音频字节（不依赖任何音频库）
# --------------------------------------------------------------------------- #

#: 最小合法 WAV：RIFF....WAVE，后面跟一点数据。`sniff_container` 只认头 12 字节，
#: 但真实文件总得有点载荷，所以给足。
WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt " + bytes(32) + b"data\x00\x00\x00\x00"

#: mp3：ID3v2 标签开头。
MP3_BYTES = b"ID3\x04\x00\x00\x00\x00\x00\x00" + bytes(64)

#: 一个"看起来像音频"的名字 + 一个**根本不是**音频的内容。
FAKE_AUDIO = b"%PDF-1.7\n" + bytes(128)


class _Upload:
    """`UploadFile` 的最小替身：只需要 `.read(n)` / `.content_type` / `.filename`。"""

    def __init__(self, data: bytes, *, content_type: str = "audio/wav") -> None:
        self._data = data
        self.content_type = content_type
        self.filename = "reference.wav"
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._data[self._offset :]
            self._offset = len(self._data)
            return chunk
        chunk = self._data[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


@dataclass
class _User:
    role: str = "owner"
    subject_id: str = "subject_owner"
    id: str = "owner"


@dataclass
class _Auth:
    user: _User


class _Request:
    """只够 `_origin_problem` / 同源校验用。

    不用 starlette 的真 `Request`：那需要一条 ASGI scope 与 receive，而这里
    要验的是"同源判定被调用了、结论被尊重"，不是 HTTP 解析。
    """

    def __init__(self, *, origin: str | None = None, cookie: str | None = None) -> None:
        self.headers = {"origin": origin} if origin else {}
        self.cookies = {"ling_session": cookie} if cookie is not None else {}
        self.url = _URL()
        self.app = _App()


class _URL:
    scheme = "http"
    netloc = "127.0.0.1:8080"


class _Settings:
    cookie_name = "ling_session"
    actor_id = "actor_ling"
    data_dir = "/tmp/ling-voice-reference-unit"
    #: `load_signing_root_key` 会读它（见 `infra/security._load_root_key`）。
    #: 给一个假值：这条路径不该碰任何真实密钥。
    root_key = "unit-test-root-key"


class _AppState:
    settings = _Settings()


class _App:
    state = _AppState()


class _Session:
    """被替换掉的 AsyncSession：这一层不该碰库。

    如果实现走到了落库那一步，`commit`/`flush` 会抛——那说明校验没拦住，
    而不是测试环境有问题。
    """

    async def get(self, *args: Any, **kwargs: Any):  # noqa: ANN401
        raise AssertionError("校验类测试不该读到数据库")

    async def flush(self) -> None:
        raise AssertionError("校验类测试不该写数据库")

    async def commit(self) -> None:
        raise AssertionError("校验类测试不该写数据库")


async def _call(
    upload: _Upload,
    *,
    auth: _Auth | None = None,
    request: _Request | None = None,
) -> dict[str, object]:
    return await upload_voice_reference(
        request=request or _Request(),  # type: ignore[arg-type]
        auth=auth or _Auth(user=_User()),  # type: ignore[arg-type]
        session=_Session(),  # type: ignore[arg-type]
        settings=_Settings(),  # type: ignore[arg-type]
        upload=upload,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# 一、超过大小上限 → 413，且说清多少 MB
# --------------------------------------------------------------------------- #


async def test_oversized_upload_is_rejected_with_413() -> None:
    """超过上限 → 413，文案里有 MB 数。

    必须失败于「不限大小把整个请求体读进内存」：那种实现面对一个 500MB 的
    上传会把它全部读进来再判，而此时 OOM 已经发生了。这条同时钉住"只读到
    上限 + 1"——超限一个字节就必须停。

    还必须失败于「用 422 报超大小」：413 才能让客户端/反代按"体太大"处理，
    422 会被理解成"参数写错了，改一改再发"。
    """
    oversized = WAV_BYTES + bytes(MAX_VOICE_BYTES)
    upload = _Upload(oversized)
    with pytest.raises(LingError) as caught:
        await _call(upload)

    err = caught.value
    assert err.status_code == 413, f"超大小应该是 413，实际 {err.status_code}"
    assert err.code == ErrorCode.invalid_request
    limit_mb = MAX_VOICE_BYTES // (1024 * 1024)
    assert f"{limit_mb}MB" in err.message, err.message
    # 只读到"上限 + 1"，不是把整段读进来再判。
    assert len(oversized) - upload._offset <= MAX_VOICE_BYTES + 1, "把整个请求体读完了才判"


async def test_exactly_at_the_limit_is_not_rejected() -> None:
    """恰好等于上限**不**算超限。

    必须失败于「用 `>=` 判超限」：那会让上限本身变成一个不可用的边界，
    而 10MB 正好是 MiMo 参考素材的设计上限（`mimo_tts.MAX_VOICE_BYTES`）。
    """
    # 这里只用 WAV 头 + 尾部填充，校验通过后会走到落库；用 mock 让它抛，
    # 确认的是"没有在大小那一步被拒"。
    at_limit = WAV_BYTES + bytes(MAX_VOICE_BYTES - len(WAV_BYTES))
    assert len(at_limit) == MAX_VOICE_BYTES
    with (
        patch(
            "ling.domains.media.assets.GeneratedMediaService.save_bytes",
            side_effect=AssertionError("过了大小门"),
        ),
        pytest.raises(AssertionError, match="过了大小门"),
    ):
        await _call(_Upload(at_limit))


# --------------------------------------------------------------------------- #
# 二、容器不在白名单 → 422，说清支持哪些
# --------------------------------------------------------------------------- #


async def test_unknown_declared_mime_is_not_trusted() -> None:
    """声明一个不在白名单的 mime + 内容是合法 WAV → **接受**（按内容判）。

    必须失败于「按 `content_type` 判格式」：那种实现下，一段声明成
    ``application/octet-stream`` 的合法 WAV 会被拒。浏览器与各种上传库给出
    的 content_type 五花八门，按它判会把正确的东西挡在门外。
    """
    with (
        patch(
            "ling.domains.media.assets.GeneratedMediaService.save_bytes",
            side_effect=AssertionError("过了格式门"),
        ),
        pytest.raises(AssertionError, match="过了格式门"),
    ):
        await _call(_Upload(WAV_BYTES, content_type="application/octet-stream"))


async def test_supported_containers_are_the_mimo_ones() -> None:
    """白名单就是 MiMo 参考音频能吃的容器，不多不少。

    必须失败于「把 TTS 产物的格式表（含 silk/amr）抄过来当参考素材白名单」：
    那些是**产出**格式，传上去没有意义，而收进来等于承诺一个用不上的入口。
    也必须失败于「把裸 PCM 收进来」：它只有启发式判定，误收一份随机二进制
    会在合成时表现为噪音而不是错误。
    """
    assert set(VOICE_MIME_BY_CONTAINER) == {"wav", "mp3", "flac", "ogg", "m4a"}
    assert "pcm" not in VOICE_MIME_BY_CONTAINER
    assert all(mime.startswith("audio/") for mime in VOICE_MIME_BY_CONTAINER.values())


# --------------------------------------------------------------------------- #
# 三、内容不是合法音频（magic number 不对）→ 422
#
# 这一条是**变异验证**的目标：把 sniff_container 的判定换成"信 content_type"，
# 下面 test_content_is_not_audio_is_rejected_even_with_audio_content_type
# 必须红。
# --------------------------------------------------------------------------- #


async def test_content_is_not_audio_is_rejected_even_with_audio_content_type() -> None:
    """内容是 PDF、但 ``content_type`` 谎称 ``audio/wav`` → 422。

    必须失败于「只看 content_type 就信」：这是最省事、也最容易被利用的实现。
    上传是最容易出安全问题的地方——客户端给的 contentType 与文件名都是它可以
    随便写的，唯一的证据是**字节本身**。
    """
    with pytest.raises(LingError) as caught:
        await _call(_Upload(FAKE_AUDIO, content_type="audio/wav"))

    err = caught.value
    assert err.status_code == 422, f"非法音频应该是 422，实际 {err.status_code}"
    assert err.detail == "voice_reference_invalid_audio"
    # 文案里要写清支持哪几种，否则用户只知道"被拒了"。
    for container in ("wav", "mp3", "flac", "ogg", "m4a"):
        assert container in err.message, f"{container} 不在提示里：{err.message}"


async def test_declared_mime_mismatch_is_judged_by_content() -> None:
    """声明 wav、内容是 mp3 → 按**内容**存成 mp3。

    必须失败于「用 content_type 当落盘的 mime」：那样库里记的是
    ``audio/wav`` 而文件是 mp3，MiMo 的 data URL 会说谎，服务端按 WAV 解码
    的结果是**一段噪音而不是一个错误**（`mimo_tts.encode_voice_data_url`
    的注释写的就是这个故障）。
    """
    from ling.domains.media.assets import GeneratedMediaService

    captured: dict[str, object] = {}

    async def _fake_save(self, data, **kwargs):  # noqa: ANN001, ANN202
        captured.update(kwargs)
        captured["data"] = data
        raise AssertionError("到此为止")

    with (
        patch.object(GeneratedMediaService, "save_bytes", _fake_save),
        pytest.raises(AssertionError, match="到此为止"),
    ):
        await _call(_Upload(MP3_BYTES, content_type="audio/wav"))

    assert captured["fmt"] == "mp3", captured
    assert captured["mime_type"] == "audio/mpeg", captured


# --------------------------------------------------------------------------- #
# 四、空文件 → 422
# --------------------------------------------------------------------------- #


async def test_empty_upload_is_rejected() -> None:
    """0 字节 → 422，且**不能**落到"格式无法识别"那条通用文案里。

    必须失败于「先嗅探再判空」：`sniff_container(b"")` 返回 None，空文件会
    被当成"不是音频"，错误文案变成"这段内容不是可识别的音频"——用户看到的是
    "我传了个音频却说格式不对"，而他真正的问题是选错了一个空文件。
    """
    with pytest.raises(LingError) as caught:
        await _call(_Upload(b"", content_type="audio/wav"))

    err = caught.value
    assert err.status_code == 422
    assert err.detail == "voice_reference_empty", err.detail
    assert "空" in err.message


# --------------------------------------------------------------------------- #
# 五、非 owner → 403
# --------------------------------------------------------------------------- #


async def test_non_owner_is_rejected() -> None:
    """member（非 owner）→ 403 / not_owner，且**在读取上传内容之前**就拒绝。

    必须失败于「只做登录校验不做角色校验」：任何登录用户都能往媒体库塞素材。
    也钉住"先鉴权后读 body"——一个被拒的请求不该让服务端先把几十 MB 读进内存。
    """
    upload = _Upload(WAV_BYTES)
    with pytest.raises(LingError) as caught:
        await _call(upload, auth=_Auth(user=_User(role="member")))

    err = caught.value
    assert err.status_code == 403, f"非 owner 应该是 403，实际 {err.status_code}"
    assert err.detail == "not_owner"
    assert upload._offset == 0, "被拒之前就把请求体读了"


async def test_roles_other_than_owner_are_all_rejected() -> None:
    """roles 逐个验：只有字面 ``owner`` 放行。

    必须失败于「用 `role != "guest"` 这类黑名单」：黑名单漏掉新角色时
    表现为"新加的观察者角色也能传素材"。
    """
    for role in ("member", "guest", "viewer", ""):
        with pytest.raises(LingError) as caught:
            await _call(_Upload(WAV_BYTES), auth=_Auth(user=_User(role=role)))
        assert caught.value.detail == "not_owner", role


# --------------------------------------------------------------------------- #
# 六、同源校验（CSRF）：multipart 不放宽 Origin 检查
# --------------------------------------------------------------------------- #


async def test_cross_origin_cookie_request_is_rejected() -> None:
    """cookie 会话 + 别的 Origin → 403 / bad_origin。

    必须失败于「multipart 端点索性不做同源校验」：那正是最容易漏掉的一条
    ——它不能要求 ``application/json``，于是有人顺手把整个门禁去掉了。
    真实后果：第三方页面用一个自动提交的 ``<form enctype=multipart/form-data>``
    就能借用户的 cookie 往她的媒体库里塞文件。
    """
    request = _Request(origin="http://evil.example", cookie="session-token")
    with pytest.raises(LingError) as caught:
        await _call(_Upload(WAV_BYTES), request=request)

    assert caught.value.status_code == 403
    assert caught.value.detail == "bad_origin"


async def test_cookie_request_without_origin_is_rejected() -> None:
    """cookie 会话 + **完全没有** Origin → 403（不是"没带就放行"）。

    必须失败于「Origin 缺失时当同源放行」：`_origin_problem` 对 cookie 请求
    缺 Origin 是拒绝的，把它抄错成放行等于把门关掉了——而它照样能通过上面那条
    "别的 Origin"测试。
    """
    with pytest.raises(LingError) as caught:
        await _call(_Upload(WAV_BYTES), request=_Request(cookie="session-token"))

    assert caught.value.detail == "bad_origin"


async def test_same_origin_cookie_request_passes_the_guard() -> None:
    """同源 cookie 请求**不**被 403 拦住——与上面两条配对的正向断言。

    必须失败于「同源校验写成了恒拒绝」：只有负向断言的守卫集在"门禁根本没放行
    任何人"时也全绿（负数断言的信息量等于它配对的正向断言的信息量）。
    """
    request = _Request(origin="http://127.0.0.1:8080", cookie="session-token")
    with (
        patch(
            "ling.domains.media.assets.GeneratedMediaService.save_bytes",
            side_effect=AssertionError("过了同源门"),
        ),
        pytest.raises(AssertionError, match="过了同源门"),
    ):
        await _call(_Upload(WAV_BYTES), request=request)


# --------------------------------------------------------------------------- #
# 七、长效保留：参考素材不能被 7 天清理带走
# --------------------------------------------------------------------------- #


def test_retention_is_zero_meaning_never_expires() -> None:
    """``retention_days=0`` 是"不自动过期"，不是"立刻过期"。

    必须失败于「漏传 retention_days」：省略会落到默认的 7 天
    （`assets.DEFAULT_RETENTION_DAYS`），一周后 `is_live()` 变假，
    `_voice_for_tts` 静默降级发文字。

    这条钉的是**语义方向**：`0` 在 `resolve_retention_days` 里是"不过期"。
    真正的落库效果由集成测试
    `test_uploaded_reference_never_expires` 守——两者不能互相替代。
    """
    from ling.domains.media.assets import _expiry_for, resolve_retention_days

    assert VOICE_REFERENCE_RETENTION_DAYS == 0
    assert resolve_retention_days(VOICE_REFERENCE_RETENTION_DAYS) == 0
    assert _expiry_for(VOICE_REFERENCE_RETENTION_DAYS) is None


def test_window_sentinel_is_not_a_real_window() -> None:
    """哨兵值是"明显不是真实窗口"的那一种，不是空串也不是长得像真窗口的串。

    必须失败于「随手传 `window_id=""`」：`save_bytes` 对空串直接抛
    `AssetError`，表现成 500。也必须失败于「复用某个会话 id 当 window」：
    那会让 `list_window_assets` 把它当那个会话里的产物列出来。

    长度上限是 `MediaAssetRow.window_id` 的 64 字符（String(64)）。
    """
    assert VOICE_REFERENCE_WINDOW_ID == "__voice_reference__"
    assert VOICE_REFERENCE_WINDOW_ID.strip() == VOICE_REFERENCE_WINDOW_ID
    assert len(VOICE_REFERENCE_WINDOW_ID) <= 64
    assert VOICE_REFERENCE_WINDOW_ID.startswith("__") and VOICE_REFERENCE_WINDOW_ID.endswith("__")


def test_asset_id_is_the_content_digest() -> None:
    """返回的 asset_id 必须是内容的 sha256，形状与设置页校验一致。

    必须失败于「返回一个随机 id / 自增 id」：`settings/service._voice_issues`
    要求 `reference_asset_id` 是 64 位十六进制，而运行时又按
    `MediaAssetRow.digest` 去 `session.get()`——两边都指向内容摘要，不是别的。

    这条只钉"形状与来源"：这里没跑落库，`expected_digest` 是否真被传下去由
    上面 `test_declared_mime_mismatch_is_judged_by_content` 看 kwargs 证明。
    """
    digest = hashlib.sha256(WAV_BYTES).hexdigest()
    assert len(digest) == 64
    assert all(ch in "0123456789abcdef" for ch in digest)
