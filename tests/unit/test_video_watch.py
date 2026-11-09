"""按需看视频：标记、短号、预算闸、抽帧、退回路径。

用户的硬要求是"**她主动想看才读**"，所以这一组用例守的都是"别多读、别假读"：

- 标记只认 ``<<看视频 mX>>``，不猜自然语言；剥标记必须幂等（出口会剥多次）；
- 短号对不上就静默退化（她可能编了个不存在的号）；
- 预算三档（每窗每小时 / 每天 / 同一条只读一次）都要拦得住，且**拦住时不产出观察**；
- 原生模式供应商不支持 → 退回抽帧并带回原因，不许静默、不许伪装成功；
- 视频不进自动描述队列。
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

from ling.domains.media.describe import MediaDescriber
from ling.domains.media.service import EvidenceView
from ling.domains.media.video import (
    MAX_FRAMES,
    extract_frames,
    ffmpeg_available,
    parse_watch_markers,
    plan_sampling,
    resolve_watch_refs,
    short_ref_for,
    stream_safe,
    strip_watch_markers,
)
from ling.domains.media.watch import VideoWatchService
from ling.ports.model import ModelError, ModelErrorKind

# ------------------------------------------------------------------ 标记


def test_marker_is_parsed_and_stripped() -> None:
    text, refs = parse_watch_markers("我看看 <<看视频 m3>> 啊")
    assert refs == ["m3"]
    assert "<<" not in text and "看视频" not in text


def test_marker_tolerates_spacing() -> None:
    _, refs = parse_watch_markers("<< 看视频  m3f2a1 >>")
    assert refs == ["m3f2a1"]


def test_natural_language_is_not_a_marker() -> None:
    """用户明确要求：不靠自然语言猜意图。"""
    for text in ("我想看看那个视频", "看看视频", "刚才那个视频里是啥"):
        cleaned, refs = parse_watch_markers(text)
        assert refs == []
        assert cleaned == text


def test_strip_is_idempotent() -> None:
    once = strip_watch_markers("看 <<看视频 m3>> 了")
    assert strip_watch_markers(once) == once
    assert once == "看 了"


def test_stream_safe_holds_partial_marker() -> None:
    """流式里标记被切开时，前半截不能发给浏览器。"""
    emit, hold = stream_safe("我看看 <<看视")
    assert emit == "我看看 "
    assert hold == "<<看视"
    # 普通的小于号不受影响
    assert stream_safe("a < b") == ("a < b", "")


def test_short_ref_is_stable_and_window_scoped_resolution() -> None:
    assert short_ref_for("media_ab12cd34") == short_ref_for("media_ab12cd34")
    videos = [EvidenceView(
        evidence_id="media_ab12cd34", window_id="w", kind="video", media_ref="x",
        origin="user_upload", visibility="window_private", description=None,
        description_origin=None, expires_at=None,
    )]
    assert resolve_watch_refs([short_ref_for("media_ab12cd34")], videos) == videos
    assert resolve_watch_refs(["mzzzz"], videos) == []


# ------------------------------------------------------------------ 抽帧参数


def test_long_video_is_sampled_across_the_whole_clip() -> None:
    """超过 120 秒不从头读：全片等间隔取 16 帧，并标注只看了部分。"""
    fps, long_video = plan_sampling(600.0)
    assert long_video is True
    assert fps * 600.0 == pytest.approx(MAX_FRAMES)


def test_short_video_uses_fixed_fps() -> None:
    fps, long_video = plan_sampling(10.0)
    assert (fps, long_video) == (0.5, False)
    assert plan_sampling(None) == (0.5, False)


@pytest.mark.skipif(not ffmpeg_available(), reason="本机没有 ffmpeg")
def test_extract_frames_from_a_real_clip(tmp_path) -> None:  # noqa: ANN001
    """用 ffmpeg 现造一个 2 秒的小视频，真的抽帧（不 mock 抽帧逻辑）。"""
    clip = tmp_path / "clip.mp4"
    subprocess.run(  # noqa: S603 - 参数数组，无 shell
        [
            shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=2",
            "-pix_fmt", "yuv420p", str(clip),
        ],
        check=True,
        capture_output=True,
    )
    sample = asyncio.run(extract_frames(clip.read_bytes()))
    assert 1 <= sample.count <= MAX_FRAMES
    assert sample.frames[0][:2] == b"\xff\xd8", "必须是 JPEG"
    assert sample.long_video is False


# ------------------------------------------------------------------ 额度闸（token 账本）


@pytest.mark.asyncio
async def test_quota_allows_when_no_limit() -> None:
    """没设日限额 = 放行（与 router 的 `daily_limit_effective()` 同一个口径）。"""
    from ling.domains.media.watch import video_quota_denial

    assert await video_quota_denial(
        config_reader=_QuotaConfigReader(daily_limit=None),
        quota=_Quota(used=10_000_000),
        profile="perception.video",
    ) is None


@pytest.mark.asyncio
async def test_quota_denies_when_used_reaches_limit() -> None:
    """用完了就拒绝，且**人话里带已用/上限**（排障与用户都看这两个数）。"""
    from ling.domains.media.watch import video_quota_denial

    note = await video_quota_denial(
        config_reader=_QuotaConfigReader(daily_limit=1000),
        quota=_Quota(used=1000),
        profile="perception.video",
    )
    assert note is not None
    assert "额度" in note
    assert "1000" in note, f"没给出已用/上限的数字：{note}"
    assert "tokens" in note


@pytest.mark.asyncio
async def test_quota_allows_when_below_limit() -> None:
    from ling.domains.media.watch import video_quota_denial

    assert await video_quota_denial(
        config_reader=_QuotaConfigReader(daily_limit=1000),
        quota=_Quota(used=999),
        profile="perception.video",
    ) is None


@pytest.mark.asyncio
async def test_quota_without_store_allows() -> None:
    """没装配账本 = 不设闸（与 router 在 `quota is None` 时放行一致）。"""
    from ling.domains.media.watch import video_quota_denial

    assert await video_quota_denial(
        config_reader=_QuotaConfigReader(daily_limit=1),
        quota=None,
        profile="perception.video",
    ) is None


@pytest.mark.asyncio
async def test_quota_reads_the_video_provider_not_another() -> None:
    """查的是 `perception.video` 槽位绑的那家，不是别的职责的家。"""
    from ling.domains.media.watch import video_quota_denial

    quota = _Quota(used=500)
    await video_quota_denial(
        config_reader=_QuotaConfigReader(daily_limit=1000, provider_id="video-provider"),
        quota=quota,
        profile="perception.video",
    )
    assert quota.asked == ["video-provider"]


class _Quota:
    """假账本：只实现 `used()`。"""

    def __init__(self, *, used: int) -> None:
        self._used = used
        self.asked: list[str] = []

    async def used(self, provider_id: str) -> int:
        self.asked.append(provider_id)
        return self._used


class _QuotaConfigReader:
    """假配置读取器：一个职责绑一家供应商，带或不带日限额。

    名字带 `_Quota` 前缀是因为本文件后面还有另一个 `_ConfigReader`
    （原生/抽帧那条路径用的最小桩）。同名会让**后定义的那个**静默生效，
    而报错信息只会说"参数不对"，查起来要绕一圈。
    """

    def __init__(self, *, daily_limit: int | None, provider_id: str = "p1") -> None:
        self._limit = daily_limit
        self._provider_id = provider_id

    async def effective(self):  # noqa: ANN201
        """一份最小的已发布配置：只有一个职责绑一家供应商。

        **不 monkey-patch `resolve_profile`**：`EffectiveConfig` 是 frozen
        dataclass，赋值直接 `FrozenInstanceError`；而且额度判据要走的本来就是
        真实解析（"这个职责最终跑在哪家"是它的唯一裁决）。用真解析也就多
        拼几个对象，换来的是"测试测的就是生产那条路"。
        """
        from ling.ports.config import (
            EffectiveConfig,
            ProfileBinding,
            ProviderConfig,
            ProviderModel,
        )

        provider = ProviderConfig(
            provider_id=self._provider_id,
            kind="fake",
            models=(ProviderModel(model_id="deterministic"),),
            daily_token_limit=self._limit,
        )
        return EffectiveConfig(
            revision=1,
            providers={self._provider_id: provider},
            profiles={
                "perception.video": ProfileBinding(
                    profile="perception.video",
                    provider_id=self._provider_id,
                    model_id="deterministic",
                )
            },
            test_mode=True,
        )


# ------------------------------------------------------------------ 读视频


class _FakeDescriber:
    def __init__(self, *, frames: int = 4, fallback: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._frames = frames
        self._fallback = fallback

    async def describe_video(self, evidence, *, mode: str = "frames"):  # noqa: ANN001, ANN201
        from ling.domains.media.describe import VideoRead

        self.calls.append((evidence.evidence_id, mode))
        return VideoRead(
            text="一只猫从桌上跳下去",
            mode="frames" if self._fallback else mode,
            frames=self._frames,
            fps=0.5,
            fallback_reason=self._fallback,
        )


def _video(evidence_id: str, *, description: str | None = None) -> EvidenceView:
    return EvidenceView(
        evidence_id=evidence_id,
        window_id="w1",
        kind="video",
        media_ref="https://example.test/a.mp4",
        origin="user_upload",
        visibility="window_private",
        description=description,
        description_origin="model_inference" if description else None,
        expires_at=None,
    )


class _FakeService:
    def __init__(self, videos: list[EvidenceView]) -> None:
        self._videos = videos

    async def undescribed_videos(self, *, window_id: str, limit: int = 5):  # noqa: ANN001, ANN201
        return [v for v in self._videos if v.description is None]

    async def described_in_window(self, *, window_id: str, limit: int = 5):  # noqa: ANN001, ANN201
        return [v for v in self._videos if v.description is not None]


async def _watch(videos: list[EvidenceView], refs: list[str], *, mode: str = "frames", quota_denial=None):  # noqa: ANN001, ANN202
    from ling.infra.db import session_scope  # noqa: F401 - 只为保持导入形态与生产一致

    service = VideoWatchService.__new__(VideoWatchService)
    service._actor_id = "actor_ling"  # noqa: SLF001
    service._describer = _FakeDescriber()  # noqa: SLF001
    service._quota_denial = quota_denial  # noqa: SLF001
    service._service = _FakeService(videos)  # noqa: SLF001
    service._session = None  # noqa: SLF001
    return await service.watch(window_id="w1", refs=refs, mode=mode)


@pytest.mark.asyncio
async def test_unknown_ref_degrades_silently() -> None:
    outcome = await _watch([_video("media_aaaa1111")], ["mzzzz"])
    assert outcome.observed == ""
    assert outcome.skipped
    assert outcome.reads == 0


@pytest.mark.asyncio
async def test_watch_reads_once_then_uses_cache() -> None:
    video = _video("media_aaaa1111")
    ref = short_ref_for(video.evidence_id)
    first = await _watch([video], [ref])
    assert first.ok and first.reads == 1

    cached = await _watch(
        [_video("media_aaaa1111", description="一只猫从桌上跳下去")], [ref]
    )
    assert cached.ok
    assert cached.reads == 0, "已经有描述就不该再调模型"


@pytest.mark.asyncio
async def test_quota_denied_produces_no_observation_and_marks_kind() -> None:
    """额度不足：`observed` 必须为空（不能让她以为自己看了），
    且 `denied_kind == "quota"`（调用方据此说"今天不想看了"而不是"打不开"）。"""

    async def _denied() -> str:
        return "今天的看视频额度用完了（已用 1000 / 上限 1000 tokens）"

    describer = _FakeDescriber()
    service = VideoWatchService.__new__(VideoWatchService)
    service._actor_id = "actor_ling"  # noqa: SLF001
    service._describer = describer  # noqa: SLF001
    service._quota_denial = _denied  # noqa: SLF001
    service._service = _FakeService([_video("media_bbbb2222")])  # noqa: SLF001
    service._session = None  # noqa: SLF001
    outcome = await service.watch(
        window_id="w1", refs=[short_ref_for("media_bbbb2222")], mode="frames"
    )

    assert outcome.observed == "", "额度不足不能让她以为自己看了"
    assert outcome.skipped
    assert outcome.denied_kind == "quota"
    assert describer.calls == [], "额度不足时**不该**调视觉模型"


@pytest.mark.asyncio
async def test_no_observation_when_reading_fails() -> None:
    """读失败：`observed` 空 + `denied_kind == "error"`（区别于额度不足）。"""

    class _Boom:
        def __init__(self) -> None:
            self.calls = 0

        async def describe_video(self, evidence, *, mode: str = "frames"):  # noqa: ANN001, ANN201
            from ling.domains.media.describe import MediaReadError

            self.calls += 1
            raise MediaReadError("视频下载失败: ConnectError")

    describer = _Boom()
    service = VideoWatchService.__new__(VideoWatchService)
    service._actor_id = "actor_ling"  # noqa: SLF001
    service._describer = describer  # noqa: SLF001
    service._quota_denial = None  # noqa: SLF001
    service._service = _FakeService([_video("media_cccc3333")])  # noqa: SLF001
    service._session = None  # noqa: SLF001
    outcome = await service.watch(
        window_id="w1", refs=[short_ref_for("media_cccc3333")], mode="frames"
    )

    assert outcome.observed == ""
    assert outcome.denied_kind == "error"
    assert describer.calls == 1


# ------------------------------------------------------------------ 原生退回


class _Router:
    """假路由：收到视频段就说"不支持"，收到图片就给描述。"""

    def __init__(self) -> None:
        self.seen_videos = 0
        self.seen_images = 0

    async def stream(self, config, *, profile, messages, allow_fallback=False):  # noqa: ANN001, ANN003, ANN202
        from ling.models.router import StreamOutcome
        from ling.ports.model import ModelChunk

        message = messages[0]
        if getattr(message, "videos", ()):
            self.seen_videos += 1
            raise ModelError(
                ModelErrorKind.unsupported, "该供应商不支持原生视频输入", retriable=False
            )
        self.seen_images += len(getattr(message, "images", ()))
        yield ModelChunk(delta="画面里有个人在走路"), StreamOutcome()


class _ConfigReader:
    async def effective(self):  # noqa: ANN202
        return object()


class _Svc:
    def __init__(self) -> None:
        self.written: dict[str, str] = {}

    async def describe(self, evidence_id, *, window_id, description, description_origin):  # noqa: ANN001, ANN003, ANN201
        self.written[evidence_id] = description
        return EvidenceView(
            evidence_id=evidence_id, window_id=window_id, kind="video",
            media_ref="https://example.test/a.mp4", origin="user_upload",
            visibility="window_private", description=description,
            description_origin=description_origin, expires_at=None,
        )


@pytest.mark.asyncio
async def test_native_unsupported_falls_back_to_frames() -> None:
    """供应商不支持原生视频 → 退回抽帧，**并把原因带回来**（不静默、不伪装）。"""
    if not ffmpeg_available():
        pytest.skip("本机没有 ffmpeg")
    router = _Router()
    svc = _Svc()

    async def _fetch(ref: str) -> tuple[bytes, str]:
        import shutil as _shutil
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "c.mp4"
            subprocess.run(  # noqa: S603
                [
                    _shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel",
                    "error", "-f", "lavfi", "-i",
                    "testsrc=size=160x120:rate=10:duration=1", "-pix_fmt", "yuv420p",
                    str(clip),
                ],
                check=True,
                capture_output=True,
            )
            return clip.read_bytes(), "video/mp4"

    describer = MediaDescriber(
        service=svc,  # type: ignore[arg-type]
        fetch_media=_fetch,
        router=router,
        config_reader=_ConfigReader(),
    )
    read = await describer.describe_video(_video("media_cccc3333"), mode="native")

    assert router.seen_videos == 1
    assert router.seen_images >= 1, "退回后必须真的把帧送进去"
    assert read.mode == "frames"
    assert read.fallback_reason and "不支持原生视频" in read.fallback_reason
    assert svc.written["media_cccc3333"] == "画面里有个人在走路"


# ------------------------------------------------------------------ 自动队列


@pytest.mark.asyncio
async def test_video_is_not_in_the_auto_describe_queue() -> None:
    """后台描述队列必须**跳过视频**（用户要求：只有她主动想看才读）。"""
    seen: dict[str, tuple[str, ...]] = {}

    class _Service:
        async def pending(self, *, window_id: str, limit: int = 20, exclude_kinds=()):  # noqa: ANN001, ANN201
            seen["exclude"] = tuple(exclude_kinds)
            return []

    describer = MediaDescriber(
        service=_Service(),  # type: ignore[arg-type]
        fetch_media=lambda ref: None,  # type: ignore[arg-type]
        router=object(),
        config_reader=_ConfigReader(),
    )
    await describer.describe_pending(window_id="w1")
    assert seen["exclude"] == ("video",)


# ------------------------------------------------------------------ 视频内容段


def test_openai_compatible_encodes_native_video() -> None:
    from ling.models.openai_compatible import _encode_message
    from ling.ports.model import ChatMessageIn, VideoPart

    encoded = _encode_message(
        ChatMessageIn(
            role="user",
            content="看看这个",
            videos=(VideoPart(url="https://example.test/a.mp4"),),
        )
    )
    assert {"type": "video_url", "video_url": {"url": "https://example.test/a.mp4"}} in (
        encoded["content"]
    )


def test_adapters_reject_native_video_explicitly() -> None:
    """不支持的适配器必须**明确报错**，让调用方退回抽帧。"""
    from ling.models.anthropic import _encode_turn
    from ling.models.chat_responses import _encode_item
    from ling.ports.model import ChatMessageIn, VideoPart

    message = ChatMessageIn(
        role="user", content="看", videos=(VideoPart(url="https://example.test/a.mp4"),)
    )
    for encode in (_encode_turn, _encode_item):
        with pytest.raises(ModelError) as exc:
            encode(message)
        assert exc.value.kind is ModelErrorKind.unsupported


def test_marker_never_reaches_stored_text() -> None:
    """落库/出口前会再剥一次（幂等），正文里不可能出现标记。"""
    assert strip_watch_markers(strip_watch_markers("<<看视频 m3>>嗯")) == "嗯"
    assert strip_watch_markers("嗯<<看视频 m3>>") == "嗯"


@pytest.mark.asyncio
async def test_collision_is_not_read() -> None:
    """短号撞车（同一窗口里 ≥2 条视频末 6 位相同）→ **不读**，只留日志。

    取第一条'凑合'既多烧一次模型调用，又可能读了另一个人发的视频——
    "她看到了别人看不到的东西"比"没看成"糟得多。
    """
    from ling.domains.media.video import short_ref_for

    a = _video("media_aaaa111111")
    b = _video("media_bbbb111111")  # 末 6 位相同 → 撞车
    assert short_ref_for(a.evidence_id) == short_ref_for(b.evidence_id)
    outcome = await _watch([a, b], [short_ref_for(a.evidence_id)])
    assert outcome.reads == 0
    assert outcome.observed == ""
    assert "撞车" in (outcome.skipped or "")


def test_short_ref_is_six_chars() -> None:
    """短号取末 6 位（撞车概率再降两个数量级），上下文提示与解析同源。"""
    assert short_ref_for("media_00ab12cd34ef") == "mcd34ef"


# ------------------------------------------------- QA 回归（P1 / P2 / P3）


def test_local_mp4_is_mapped_to_video_mime(tmp_path) -> None:
    """QA P1：本地落盘的 .mp4 曾经被标成 image/jpeg。

    后果不是"报错"，而是原生视频模式发出 `data:image/jpeg;base64,<整段 mp4>`：
    供应商"看到了坏字节"，日志里一切正常。所以后缀映射必须有视频项，
    而且不认识的后缀**不许猜成 jpeg**。
    """
    from ling.worker import _MEDIA_SUFFIX_MIME, _UNKNOWN_MEDIA_MIME

    assert _MEDIA_SUFFIX_MIME[".mp4"] == "video/mp4"
    assert _MEDIA_SUFFIX_MIME[".mov"] == "video/quicktime"
    assert _MEDIA_SUFFIX_MIME[".webm"] == "video/webm"
    assert _MEDIA_SUFFIX_MIME[".mkv"] == "video/x-matroska"
    assert _UNKNOWN_MEDIA_MIME == "application/octet-stream"
    assert "image/jpeg" not in _MEDIA_SUFFIX_MIME.values() or _MEDIA_SUFFIX_MIME[".jpg"] == "image/jpeg"


def test_partial_marker_never_reaches_stored_or_sent_text() -> None:
    """QA P2：半截标记（``<<看视频 m3``）必须被剥掉，不能落库/外发。"""
    from ling.domains.media.video import finalize_reply_text

    dirty = "好呀我看看 <<看视频 m3"
    cleaned = finalize_reply_text(dirty)
    assert "<<" not in cleaned
    assert "看视频" not in cleaned
    assert cleaned == "好呀我看看"
    # 幂等：出口会剥多次
    assert finalize_reply_text(cleaned) == cleaned


def test_stream_safe_holds_single_angle_brackets() -> None:
    """QA P3：流式恰好在 ``<``|``<`` 或 ``>``|``>`` 之间切开时不能漏字。"""
    from ling.domains.media.video import stream_safe

    # 第一片只有 "<"：压住
    emit, hold = stream_safe("我看看 <")
    assert emit == "我看看 "
    assert hold == "<"
    # 第二片补齐：整段标记都还在手里，没漏出去
    emit2, hold2 = stream_safe(hold + "<看视频 m3")
    assert emit2 == ""
    assert hold2 == "<<看视频 m3"
    # 尾部只有一个 ">" 时仍压住（等第二个 ">"）
    emit3, hold3 = stream_safe(hold2 + ">")
    assert emit3 == ""
    assert hold3 == "<<看视频 m3>"
    # 补齐后是完整标记：交出去，由 strip 负责剥
    emit4, hold4 = stream_safe(hold3 + ">")
    assert emit4 == "<<看视频 m3>>"
    assert hold4 == ""
    # 单个小于号在**中间**不受影响（"a < b" 是正常文本）
    assert stream_safe("a < b") == ("a < b", "")


def test_partial_marker_in_native_mime_guard() -> None:
    """非视频 MIME 不许走原生模式（QA P1 的另一半：坏字节不许被"看见"）。"""
    from ling.domains.media.video import guess_video_suffix

    assert guess_video_suffix("application/octet-stream") == ".mp4"
