"""媒体描述：把一张图交给视觉模型，拿回一段**标注为推断**的文字。

为什么单独一个模块而不是塞进 bridge：描述是**异步、可失败、可重试**的
后台任务，跟"这一轮要不要回话"的实时路径没有关系。塞进实时路径会让
一次视觉模型超时直接拖住整轮对话。

三条硬规则（前两条来自阶段 6 验收，第三条是本模块自己的）：

1. **只描述授权窗口里的证据**——写入走 ``MediaEvidenceService.describe``，
   它会在描述时**重新检查**授权（中途撤权就不许再写）。
2. **描述永远是推断**。``description_origin`` 固定
   ``model_inference``，``fact_eligible`` 因此恒为 ``False``。
   模型看图说"这是一只猫"只是猜测，不能变成"用户养了一只猫"。
3. **拿不到就留空，绝不编造**。模型不可用、返回空、抛错——一律保持
   ``description=None``，让它留在待办队列里下次再试。
   写一句"（无法描述）"会把"没看到"伪装成"看到了但没内容"，
   比留空更糟：它既污染了证据，又让重试永远发生不了。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ling.domains.media.errors import (
    SOURCE_BUSINESS,
    SOURCE_MODEL,
    SOURCE_OTHER,
    MediaDescribeError,
    MediaRefError,
    MediaRefPermanentError,
)
from ling.domains.media.service import (
    ORIGIN_MODEL,
    EvidenceView,
    MediaEvidenceError,
    MediaEvidenceService,
)
from ling.domains.media.video import (
    MAX_VIDEO_BYTES,
    extract_frames,
    guess_video_suffix,
)
from ling.ports.model import ChatMessageIn, ImagePart, ModelError, ModelErrorKind, VideoPart

logger = logging.getLogger("ling.domains.media.describe")

# 视觉能力绑定的职责名。没绑就不该硬编一个模型去猜。
VISION_PROFILE = "perception.vision"

# 视频职责名（`ModelRole.perception.video` / 「视频」默认槽位）。
#
# **这里没有"kind=video 就自动走它"的路由**，这是刻意的：用户的要求是
# "只有她主动想读那个视频时才去读"（逐条视频描述会白烧 token，而她当时
# 可能根本用不上）。所以视频描述要由**调用方显式指定**这个职责才会发生
# （构造 `MediaDescriber(profile=VIDEO_PROFILE)`，或未来的"她决定看一眼"
# 入口），默认路径与以前完全一致——`self._profile`（识图）。
#
# 为什么与识图分成两个职责：能看图片的模型大多**看不了视频**，合成一个
# 用户就只能二选一（给图片配便宜的，视频跟着废；或给视频配贵的，图片也贵）。
VIDEO_PROFILE = "perception.video"

# 只描述看到的，不做推断以外的加工。**不要**让它"顺便猜用户在做什么"——
# 描述越界就会把猜测伪装成观察。
DESCRIBE_PROMPT = (
    "你在看用户发来的一张图片。请只描述图上直接可见的内容："
    "有哪些人/物、在做什么、场景是什么。"
    "看不清就说不确定，不要猜测图片之外的背景、地点或人物身份。"
    "用一两句中文回答，不要加解释、不要提问。"
)

# 视频专用提示。**不能沿用图片那句**：对着视频说"这是一张图片"会让模型
# 按单帧作答（描述成一张截图），"在动什么、大概是什么内容"整个丢掉。
#
# 目前**没有调用方**：等"她想看才看"的入口接线时，那一侧必须用这句，
# 而不是 `DESCRIBE_PROMPT`。先留在这里，是因为它属于"怎么问"的一部分，
# 写在未来的调用方只会被抄错。
VIDEO_DESCRIBE_PROMPT = (
    "你在看用户发来的一段视频。请描述这段视频里发生了什么："
    "画面里有谁/什么、在做什么、场景是什么。"
    "如果是按时间先后给出的几帧，请把它们当成同一段视频来描述。"
    "看不清就说不确定，不要猜测视频之外的背景、地点或人物身份。"
    "用一两句中文回答，不要加解释、不要提问。"
)

# 动图最多取几帧。取多了没有额外信息量，只是在烧视觉模型的 token；
# 取 1 帧又看不出"动"这件事。4 帧足够判断"这是一段什么动画"。
MAX_ANIMATION_FRAMES = 4

# 每帧转 PNG 后的字节上限。超了就不再放更多帧——宁可少看两帧，
# 也不要把请求撑爆导致整条描述失败。
MAX_FRAME_BYTES = 1_500_000


def expand_animation(data: bytes, mime: str) -> list[tuple[bytes, str]]:
    """把动图拆成关键帧；静态图原样返回。

    ## 为什么必须拆

    视觉接口对 GIF/APNG/动态 WebP 基本只取**第一帧**。第一帧是片头、
    是空白、是黑场的情况太常见了——不拆帧就会出现"她看了动图说
    「这是一张黑色图片」"。

    ## 为什么用 RGB 转一遍

    GIF 是索引色 + 局部帧 + 透明色。直接取帧会拿到半透明残片或上一帧的
    残留，模型看到的是"鬼影"。整帧合成并转 RGB 之后才是人眼看到的样子。

    解析失败（损坏文件、Pillow 不支持）时**原样返回**——不因为拆帧失败
    就连描述都不做了。
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - 依赖缺失时降级
        return [(data, mime)]

    import io

    try:
        with Image.open(io.BytesIO(data)) as img:
            frames = int(getattr(img, "n_frames", 1) or 1)
            if frames <= 1:
                return [(data, mime)]
            count = min(frames, MAX_ANIMATION_FRAMES)
            # 均匀取样，含首尾：片头和结尾往往最能说明这段动图在做什么
            picks = (
                [0]
                if count == 1
                else [round(i * (frames - 1) / (count - 1)) for i in range(count)]
            )
            out: list[tuple[bytes, str]] = []
            for idx in picks:
                img.seek(idx)
                buffer = io.BytesIO()
                img.convert("RGB").save(buffer, format="PNG", optimize=True)
                payload = buffer.getvalue()
                if len(payload) > MAX_FRAME_BYTES:
                    continue
                out.append((payload, "image/png"))
            return out or [(data, mime)]
    except Exception:  # noqa: BLE001 - 拆帧失败不影响单帧描述
        return [(data, mime)]


# fetch_media(media_ref) -> (bytes, mime_type)
MediaFetcher = Callable[[str], Awaitable[tuple[bytes, str]]]


@dataclass(frozen=True)
class VideoRead:
    """一次"看视频"的结果。字段都是给行为日志与调用方解释用的。"""

    text: str
    mode: str  # native | frames | cached
    frames: int = 0
    fps: float = 0.0
    long_video: bool = False
    # 原生模式失败后退回抽帧的原因（None = 没用退回路径）
    fallback_reason: str | None = None
    cached: bool = False


class MediaReadError(Exception):
    """视频太大/取不到内容这类"读不了"的错误（不是模型故障）。"""


@dataclass
class DescribeOutcome:
    described: int = 0
    skipped: int = 0
    failed: int = 0
    # **永久失败**单独计数：引用越界、超限、形式不支持——重试没有意义。
    # 混进 ``failed`` 里的话，"每轮都在失败"和"每轮都在重试"看起来一样。
    # 只统计**引用本身**不可用（``permanent_ref``），不含业务拒绝。
    permanent_failures: int = 0
    # 业务拒绝（撤权、过期）也落终态——否则每 60 秒重试一条已被禁止描述的
    # 记录。但它不说明"这个引用永远不可用"，所以**不**混进 permanent_failures：
    # 否则日志里"引用本身不可用"这句话会指向错误的原因。
    blocked: int = 0
    last_error: str | None = None
    # 整轮中止（模型/配置侧错误：401、密钥缺失、职责未绑定）。中止时其余
    # 证据**原样留在队列里**，等配置修好后下一轮重来——不是永久失败。
    aborted: bool = False
    abort_reason: str | None = None


class MediaDescriber:
    """把待描述的媒体证据跑一遍视觉模型。"""

    def __init__(
        self,
        *,
        service: MediaEvidenceService,
        fetch_media: MediaFetcher,
        router: object,
        config_reader: object,
        profile: str = VISION_PROFILE,
    ) -> None:
        self._service = service
        self._fetch = fetch_media
        self._router = router
        self._config_reader = config_reader
        self._profile = profile

    async def describe_one(self, evidence: EvidenceView) -> str:
        """描述一条证据并写回。返回描述文本。

        失败一律抛 ``MediaDescribeError`` 且**不写库**——留空让它下次重试。

        ## 失败分成四类，``terminal`` 只对"引用本身不可用"为 True

        | 来源 | 例子 | retriable | terminal |
        |---|---|---|---|
        | 引用 ``SOURCE_REF`` | 越界、超限、形式不支持、内容为空 | False | **True** |
        | 模型 ``SOURCE_MODEL`` | 401、密钥缺失、职责未绑定、超时 | 视情况 | False |
        | 业务 ``SOURCE_BUSINESS`` | 撤权、过期、窗口不匹配 | False | True（非 ref） |
        | 其余 ``SOURCE_OTHER`` | 网络抖动、模型返回空 | True | False |

        模型侧**永远**不标终态：密钥配错一次不该把队列里所有证据标死，
        否则修好密钥后它们已不在 ``pending()`` 里，再也不会重试。
        """
        if evidence.description is not None:
            return evidence.description

        try:
            data, mime = await self._fetch(evidence.media_ref)
        except MediaRefError as exc:
            # **引用本身永久错误**：越界、超限、形式不支持。重试同一个引用
            # 只会得到同一个结果，而 `media.describe` 每轮都会把
            # `description IS NULL` 的行重新捞出来——标成可重试就是每 60 秒
            # 重试一次、永远重试不完，日志里一条"下载失败"重复到把真问题淹掉。
            # 原实现把它和网络故障一起包成 ``retriable=True``，正是这个 bug。
            raise MediaRefPermanentError(f"媒体引用不可用: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - 下载失败种类很多，统一转成可重试
            raise MediaDescribeError(
                f"媒体下载失败: {type(exc).__name__}", source=SOURCE_OTHER
            ) from exc

        if not data:
            # 内容为空也是**引用本身**的问题：这个引用指向的是一份空数据，
            # 重试它不会变出内容来。
            raise MediaRefPermanentError("媒体内容为空")

        # 职责**不按 kind 自动切换**：视频要由调用方显式指定 VIDEO_PROFILE
        # （见该常量的注释——"她想看才看"，逐条自动描述会白烧 token）。
        text = await self._ask_vision(data, mime)
        if not text.strip():
            # 空回复等于没看到。留空，不写占位符，下一轮再试。
            # 这是**模型**的临时状态（限流、截断、空 delta），不是引用坏了。
            raise MediaDescribeError("模型没有返回任何描述", source=SOURCE_OTHER)

        try:
            view = await self._service.describe(
                evidence.evidence_id,
                window_id=evidence.window_id,
                description=text,
                description_origin=ORIGIN_MODEL,
            )
        except MediaEvidenceError as exc:
            # 撤权、过期、窗口不匹配都走这里——是业务拒绝，不是系统故障。
            # 该停掉重试（每 60 秒重试一条已被禁止描述的记录没有意义），
            # 但它**不是**"引用本身不可用"，所以不记进 permanent_failures。
            raise MediaDescribeError(
                str(exc), retriable=False, terminal=True, source=SOURCE_BUSINESS
            ) from exc
        return view.description or ""

    async def describe_video(self, evidence: EvidenceView, *, mode: str = "frames") -> VideoRead:
        """**按需**读一段视频（她在回复里写了 ``<<看视频 mX>>``）。

        与 ``describe_one`` 的区别：视频默认不进自动队列（见 ``describe_pending``），
        只在她说要看时调用。两条硬规则不变：写回时仍然走
        ``MediaEvidenceService.describe``（撤权/过期会拒），描述仍然是推断。

        ``mode``：
        - ``frames``（默认）：抽帧后当多张图交给视频模型，任何供应商都能用；
        - ``native``：把视频原样交给模型。供应商不支持时**自动退回抽帧**，
          并把原因带回去（``fallback_reason``）——不静默、也不伪装成功。

        **同一条证据只读一次**：已经有描述就直接用缓存，不再调模型。
        """
        if evidence.description:
            return VideoRead(
                text=evidence.description, mode="cached", cached=True
            )

        try:
            data, mime = await self._fetch(evidence.media_ref)
        except MediaRefError as exc:
            raise MediaRefPermanentError(f"媒体引用不可用: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - 下载失败种类很多
            raise MediaDescribeError(
                f"视频下载失败: {type(exc).__name__}", source=SOURCE_OTHER
            ) from exc
        if not data:
            raise MediaRefPermanentError("视频内容为空")
        if len(data) > MAX_VIDEO_BYTES:
            raise MediaReadError(f"视频太大（{len(data) // (1024 * 1024)}MB）")

        fallback_reason: str | None = None
        # 媒体类型不是视频（例如本地落盘的后缀不认识、或引用缺 mime）时
        # **不要**硬塞给原生模式：那会发出 `data:application/octet-stream`，
        # 供应商要么报错要么"看到坏字节"。退回抽帧——ffmpeg 自己会看容器头，
        # 比我们猜后缀准。
        mime = (mime or "").strip().lower()
        if mode == "native" and not mime.startswith("video/"):
            fallback_reason = f"媒体类型不是视频（{mime or '未知'}），改走抽帧"
        if mode == "native" and fallback_reason is None:
            try:
                text = await self._ask(
                    prompt=VIDEO_DESCRIBE_PROMPT,
                    videos=(VideoPart.from_bytes(data, mime),),
                    profile=VIDEO_PROFILE,
                )
                read = VideoRead(text=text, mode="native")
            except MediaDescribeError as exc:
                # ``_ask`` 会把 ModelError 统一转成 MediaDescribeError，
                # 所以这里按 ``model_kind`` 判断"是不是不支持"。
                if exc.model_kind not in (
                    ModelErrorKind.unsupported,
                    ModelErrorKind.unsupported.value,
                ):
                    raise
                # 供应商明确说不支持原生视频：退回抽帧，并记下原因。
                fallback_reason = f"供应商不支持原生视频输入（{exc}）"
        if mode != "native" or fallback_reason is not None:
            sample = await extract_frames(data, suffix=guess_video_suffix(mime))
            prompt = VIDEO_DESCRIBE_PROMPT
            if sample.long_video:
                prompt = (
                    f"{VIDEO_DESCRIBE_PROMPT}"
                    f"（这段视频较长，你只看到全片等间隔取样的 {sample.count} 帧，"
                    "请在描述里说明这只是其中一部分。）"
                )
            text = await self._ask(
                prompt=prompt,
                images=tuple(
                    ImagePart.from_bytes(frame, "image/jpeg") for frame in sample.frames
                ),
                profile=VIDEO_PROFILE,
            )
            read = VideoRead(
                text=text,
                mode="frames",
                frames=sample.count,
                fps=sample.fps,
                long_video=sample.long_video,
                fallback_reason=fallback_reason,
            )

        if not read.text.strip():
            raise MediaDescribeError("模型没有返回任何描述", source=SOURCE_OTHER)
        try:
            view = await self._service.describe(
                evidence.evidence_id,
                window_id=evidence.window_id,
                description=read.text,
                description_origin=ORIGIN_MODEL,
            )
        except MediaEvidenceError as exc:
            raise MediaDescribeError(
                str(exc), retriable=False, terminal=True, source=SOURCE_BUSINESS
            ) from exc
        return VideoRead(
            text=view.description or read.text,
            mode=read.mode,
            frames=read.frames,
            fps=read.fps,
            long_video=read.long_video,
            fallback_reason=read.fallback_reason,
        )

    async def describe_pending(self, *, window_id: str, limit: int = 5) -> DescribeOutcome:
        """跑一个窗口的待办。单条失败不中断其余。

        **只有"引用本身不可用"会被落库成终态**（``describe_failed_at``），
        不再进待办队列。

        只把 ``retriable`` 标志设成 False 是**不够的**：那个标志原来没有任何
        读者，`pending()` 只按 `description IS NULL` 过滤，于是永久错误的
        引用每一轮都被重新捞出来重试（每 60 秒一次，永远停不下来）。
        终态必须写进库，队列才真的会停下来。

        可重试的失败（网络抖动、模型超时）**不标终态**，照旧下一轮重试。

        ## 为什么模型侧 ``auth`` / ``missing_secret`` 不标终态、而且整轮中止

        它们 ``retriable=False``（重试同一个请求确实没用），但**不是这个引用
        的问题**：密钥配错一次，队列里所有证据都会以同样方式失败。逐条跳过
        等于把 limit 条全试一遍，还全都白试；更糟的是如果按 ``retriable``
        标终态，修好密钥后这些证据再也回不到队列里。

        正确语义是"这一轮跳过，等配置修好后重来"：所以第一条这类错误就
        ``break``，记一条 WARNING，其余证据原样留在队列里。
        """
        outcome = DescribeOutcome()
        # **视频不进自动描述队列**：读视频是"她主动说要看"才发生的事
        # （见 `domains/media/video.py`）。一视同仁地跑，等于把群里每一段
        # 视频都送进视频模型——白烧 token，而她当时可能根本用不上。
        pending = await self._service.pending(
            window_id=window_id, limit=limit, exclude_kinds=("video",)
        )
        for evidence in pending:
            try:
                await self.describe_one(evidence)
            except MediaDescribeError as exc:
                outcome.failed += 1
                outcome.last_error = str(exc)

                if exc.is_config_failure:
                    # 整轮都会以同样方式失败：立刻中止，别把 limit 条全试一遍。
                    # **不标终态**——证据要留在队列里，等配置修好后重来。
                    outcome.aborted = True
                    outcome.abort_reason = str(exc)
                    logger.warning(
                        "媒体描述整轮中止 window=%s（模型/配置错误 %s，修好后会重试）: %s",
                        window_id,
                        exc.model_kind,
                        exc,
                    )
                    break

                if not exc.terminal:
                    continue

                if exc.permanent_ref:
                    outcome.permanent_failures += 1
                else:
                    outcome.blocked += 1
                # 落库失败不该让整轮崩掉：这条已经失败了，标记失败原因
                # 再报一次错只是把同一件事说两遍。但要留下日志。
                try:
                    await self._service.mark_describe_failed(
                        evidence.evidence_id,
                        window_id=evidence.window_id,
                        reason=str(exc),
                    )
                except MediaEvidenceError:
                    # 窗口不匹配 / 记录不在了：终态没有意义，跳过即可
                    continue
            else:
                outcome.described += 1
        return outcome

    async def _ask_vision(self, data: bytes, mime: str) -> str:
        frames = expand_animation(data, mime)
        # 多帧时明确告诉模型这是同一段动图的先后帧。不说的话它会把四张图
        # 当成四件不同的事，描述成"图一…图二…"，读起来像在逐张报菜名。
        prompt = DESCRIBE_PROMPT
        if len(frames) > 1:
            prompt = (
                f"这是一段动图的 {len(frames)} 个关键帧，按时间先后排列。"
                "请把它们当成**同一段动画**来描述：在动什么、大致是什么内容。"
                "只说直接看得见的，看不清就说不确定，不要编。用一两句中文回答。"
            )
        return await self._ask(
            prompt=prompt,
            images=tuple(
                ImagePart.from_bytes(payload, m or "image/jpeg") for payload, m in frames
            ),
        )

    async def _ask(
        self,
        *,
        prompt: str,
        images: tuple[ImagePart, ...] = (),
        videos: tuple[VideoPart, ...] = (),
        profile: str | None = None,
    ) -> str:
        """一次视觉/视频调用。``profile`` 不给就用构造时绑定的职责。"""
        config = await self._config_reader.effective()  # type: ignore[attr-defined]
        messages = [
            ChatMessageIn(role="user", content=prompt, images=images, videos=videos)
        ]
        chunks: list[str] = []
        try:
            async for chunk, outcome in self._router.stream(  # type: ignore[attr-defined]
                config,
                profile=profile or self._profile,
                messages=messages,
                allow_fallback=False,
            ):
                if chunk.delta:
                    chunks.append(chunk.delta)
                if outcome.error is not None:
                    raise _model_failure(outcome.error)
        except ModelError as exc:
            # 没绑定 perception.vision 时走这里。**不要**退回 fake 去编一段。
            raise _model_failure(exc) from exc
        return "".join(chunks).strip()


def _model_failure(error: ModelError) -> MediaDescribeError:
    """把模型错误转成**不标终态**的描述失败。

    ## 为什么不传 ``terminal=not error.retriable``

    ``auth``（401）和 ``missing_secret`` 都是 ``retriable=False``，但它们是
    **模型/配置**的问题，不是**这个引用**的问题。按 ``retriable`` 标终态
    的后果是：运维把视觉模型密钥配错一次，队列里所有待描述证据被一次性
    标死，修好密钥后再也不会重试（它们已不在 ``pending()`` 队列里）。

    所以这里 ``terminal`` 恒为 False，``source=SOURCE_MODEL``；
    ``retriable`` 照实传递，让调用方能区分"网络抖动"和"重试也没用"。
    """
    return MediaDescribeError(
        f"视觉模型调用失败: {error.message}",
        retriable=error.retriable,
        terminal=False,
        source=SOURCE_MODEL,
        model_kind=error.kind,
    )


__all__ = [
    "DESCRIBE_PROMPT",
    "VIDEO_DESCRIBE_PROMPT",
    "VIDEO_PROFILE",
    "VISION_PROFILE",
    "DescribeOutcome",
    "MediaReadError",
    "VideoRead",
    "MediaDescribeError",
    "MediaDescriber",
]
