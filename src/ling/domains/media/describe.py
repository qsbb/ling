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

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ling.domains.media.service import (
    ORIGIN_MODEL,
    EvidenceView,
    MediaEvidenceError,
    MediaEvidenceService,
)
from ling.ports.model import ChatMessageIn, ImagePart, ModelError

# 视觉能力绑定的职责名。没绑就不该硬编一个模型去猜。
VISION_PROFILE = "perception.vision"

# 只描述看到的，不做推断以外的加工。**不要**让它"顺便猜用户在做什么"——
# 描述越界就会把猜测伪装成观察。
DESCRIBE_PROMPT = (
    "你在看用户发来的一张图片。请只描述图上直接可见的内容："
    "有哪些人/物、在做什么、场景是什么。"
    "看不清就说不确定，不要猜测图片之外的背景、地点或人物身份。"
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


class MediaDescribeError(Exception):
    """描述失败。可重试的用 retriable 区分，但调用方一律保持待办即可。"""

    def __init__(self, message: str, *, retriable: bool = True) -> None:
        super().__init__(message)
        self.retriable = retriable


@dataclass
class DescribeOutcome:
    described: int = 0
    skipped: int = 0
    failed: int = 0
    last_error: str | None = None


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
        """
        if evidence.description is not None:
            return evidence.description

        try:
            data, mime = await self._fetch(evidence.media_ref)
        except Exception as exc:  # noqa: BLE001 - 下载失败种类很多，统一转成可重试
            raise MediaDescribeError(f"媒体下载失败: {type(exc).__name__}") from exc

        if not data:
            raise MediaDescribeError("媒体内容为空", retriable=False)

        text = await self._ask_vision(data, mime)
        if not text.strip():
            # 空回复等于没看到。留空，不写占位符。
            raise MediaDescribeError("模型没有返回任何描述")

        try:
            view = await self._service.describe(
                evidence.evidence_id,
                window_id=evidence.window_id,
                description=text,
                description_origin=ORIGIN_MODEL,
            )
        except MediaEvidenceError as exc:
            # 撤权、过期、窗口不匹配都走这里——是业务拒绝，不是系统故障
            raise MediaDescribeError(str(exc), retriable=False) from exc
        return view.description or ""

    async def describe_pending(self, *, window_id: str, limit: int = 5) -> DescribeOutcome:
        """跑一个窗口的待办。单条失败不中断其余。"""
        outcome = DescribeOutcome()
        pending = await self._service.pending(window_id=window_id, limit=limit)
        for evidence in pending:
            try:
                await self.describe_one(evidence)
            except MediaDescribeError as exc:
                outcome.failed += 1
                outcome.last_error = str(exc)
            else:
                outcome.described += 1
        return outcome

    async def _ask_vision(self, data: bytes, mime: str) -> str:
        config = await self._config_reader.effective()  # type: ignore[attr-defined]
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
        messages = [
            ChatMessageIn(
                role="user",
                content=prompt,
                images=tuple(
                    ImagePart.from_bytes(payload, m or "image/jpeg")
                    for payload, m in frames
                ),
            )
        ]
        chunks: list[str] = []
        try:
            async for chunk, outcome in self._router.stream(  # type: ignore[attr-defined]
                config, profile=self._profile, messages=messages, allow_fallback=False
            ):
                if chunk.delta:
                    chunks.append(chunk.delta)
                if outcome.error is not None:
                    raise MediaDescribeError(
                        f"视觉模型调用失败: {outcome.error.message}",
                        retriable=outcome.error.retriable,
                    )
        except ModelError as exc:
            # 没绑定 perception.vision 时走这里。**不要**退回 fake 去编一段。
            raise MediaDescribeError(f"视觉模型不可用: {exc.message}") from exc
        return "".join(chunks).strip()


__all__ = [
    "DESCRIBE_PROMPT",
    "VISION_PROFILE",
    "DescribeOutcome",
    "MediaDescribeError",
    "MediaDescriber",
]
