"""按需「看视频」：标记解析、窗口短号、抽帧、预算闸。

## 为什么是**标记**而不是自然语言

"我想看看那个视频"和"那个视频我在别处看过了"在词面上几乎分不开，靠自然
语言判断要不要花一次视频模型调用，误判成本是双向的：误判成"要看"就白烧
token，误判成"不用看"她就装作看不见。所以只认一种**机器可判**的标记：

    <<看视频 m3>>

- 宽容：标记前后的空格、``<< 看视频  m3 >>`` 这种排版都认；
- 严格：**不**从自然语言里猜意图（第一句里那些话就是反例）。

## 为什么短号按"窗口内"分配

上下文里要给她一个能写回来的引用，而 ``media_xxx`` 这种 id 既长又不可读。
短号 = ``m`` + evidence id 的末 4–6 位十六进制，**由 id 决定**（不额外存表），
所以在同一个窗口里对同一条证据永远得到同一个短号；解析时按窗口内的视频
列表核对，对不上就是"没这条"——静默退化，不猜。

## 标记永远不外发

出站文本、落库正文、历史渲染里都不能出现标记（用户不该看到说明书）。
两点保证：
1. ``strip_watch_markers`` 是**幂等**的，任何出口都可以再剥一次；
2. 流式输出用 ``stream_safe``：把可能是半个标记的尾巴**压住不发**，
   否则用户会先看到 ``<<看视频 m3>>`` 再看到它消失。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ling.kernel.marker_stream import (
    PENDING_MARKER_RE as _SHARED_PENDING_MARKER_RE,
)

logger = logging.getLogger("ling.domains.media.video")

# 唯一被支持的标记语法。``[A-Za-z0-9_-]`` 覆盖 m3 / m3f2a1 这类短号。
MARKER_RE = re.compile(r"<<\s*看视频\s+([A-Za-z0-9_-]{1,16})\s*>>")

# 流式过滤用：以 ``<<`` 开头、还没等到 ``>>`` 的尾巴（可能是半个标记）。
#
# **唯一的定义在 `kernel/marker_stream.py`**：撤回标记（`domains/conversation
# /recall.py`）必须用同一个形状。两个域各写一份的结果是必有一处先被改松，而
# "哪一处松了"极难发现（历史上两份都是 ``r"<<[^<>]*>?$"``，把
# ``1<<2 就是 4`` 洗成了 ``1``）。域之间不许互相 import，所以它住在 kernel。
PENDING_MARKER_RE = _SHARED_PENDING_MARKER_RE
# 旧名字（本模块内部引用）保留，避免外部导入点全部改一遍。
_PENDING_MARKER_RE = PENDING_MARKER_RE

# 读视频的额度**不再按次数算**（2026-11-09 用户拍板「次数限制去掉」）。
#
# 原来这里是"每窗口每小时 ≤2、每天 ≤20"，两份进程内计数器。删掉的理由：
#
# 1. 次数与成本脱钩。一段 5 秒的视频和一段 3 分钟的视频都记"1 次"，而后者
#    抽帧更多、token 多出一个量级。按次数限，等于把成本管错了对象。
# 2. 进程内计数重启即清零，注释里自己写着"可以接受"——那不是额度，是限流。
# 3. 真正该管的是 token，而那份账**已经存在**：`models/quota.py` 的
#    `TokenQuotaStore` 按 (供应商, 本地日) 记真实用量，`router` 每次调用前
#    都会查。看视频走的是同一个 router、同一家供应商，所以额度天然共用——
#    不需要、也不该再维护第二套计数器（两套必然对不上，而对不上的时候
#    用户看到的是"额度显示还有、视频却看不了"）。
#
# 现在的口径见 `watch.py::video_quota_denial`：查 `perception.video` 槽位
# 绑定那家供应商的当日已用 token 与上限。

# 抽帧参数（依据用户要求：0.5 fps、最多 16 帧、长边 ≤512、单帧 JPEG）。
TARGET_FPS = 0.5
MAX_FRAMES = 16
LONG_SIDE = 512
# 超过这个时长就不从头读到尾，而是在全片等间隔取样，并在描述里标注。
LONG_VIDEO_SECONDS = 120.0
# 视频下载上限。视频比图片大得多，但 20MB 已经能覆盖手机拍的短视频；
# 再大就超出"看一眼"的合理成本。
MAX_VIDEO_BYTES = 20 * 1024 * 1024
# 单次 ffmpeg 抽帧的超时。抽 16 帧是秒级的事，卡住说明文件坏了。
FFMPEG_TIMEOUT = 60.0


# ---------------------------------------------------------------- 标记与短号


def short_ref_for(evidence_id: str) -> str:
    """证据 id → 窗口内短号（``m`` + 末 6 位字母数字）。

    纯函数：同一个 id 永远得到同一个短号，不需要额外存一张映射表，
    重启/多副本也不会漂。
    """
    tail = "".join(ch for ch in evidence_id.rsplit("_", 1)[-1] if ch.isalnum())
    # 末 6 位（不是 4 位）：同一个窗口里视频一多，4 位撞车概率就不再可忽略，
    # 而撞车意味着"读错人"——多错一位十六进制比多一次模型调用便宜得多。
    return "m" + (tail[-6:] or "0").lower()


def parse_watch_markers(text: str) -> tuple[str, list[str]]:
    """取出所有 ``<<看视频 mX>>`` 标记，返回 (剥掉标记后的文本, 短号列表)。

    幂等：对已经剥过的文本再调一次，得到同样的文本与空列表。
    """
    if not text:
        return "", []
    refs = [match.group(1) for match in MARKER_RE.finditer(text)]
    cleaned = MARKER_RE.sub("", text)
    # 标记被剥掉后常在原处留一串空格；收成单个空格，否则正文里会出现
    # "我看看  再告诉你"这种双空格。换行**不动**：那是她故意分段的地方。
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned, refs


def strip_watch_markers(text: str) -> str:
    """只剥标记（出口用）。幂等。"""
    return parse_watch_markers(text)[0]


def stream_safe(text: str) -> tuple[str, str]:
    """流式过滤：返回 (可以立刻发出去的部分, 需要暂扣的尾巴)。

    暂扣的只有"看起来是半个标记"的尾巴（``<<`` 之后还没出现 ``>>``）。
    不暂扣的话，SSE 里会先把 ``<<看视频 m3>>`` 原样推给浏览器——
    用户看见说明书，然后再看它被删掉。
    """
    if not text:
        return "", ""
    head, sep, tail = text.rpartition("<<")
    if not sep:
        # 末尾孤零零一个 ``<`` 也可能是 ``<`` + ``<`` 被流式切开的前半截。
        # 压住它，下一片来了再一起判断；流结束时由 ``drop_partial_marker`` 兜底。
        if text.endswith("<"):
            return text[:-1], "<"
        return text, ""
    # 尾部已经是完整标记，或后面根本不像标记：都不用压
    if ">>" in tail or not _PENDING_MARKER_RE.match(sep + tail):
        return text, ""
    return head, sep + tail


def drop_partial_marker(text: str) -> str:
    """去掉**没写完的**标记尾巴（``<<看视频 m3`` 这种半截）。

    流式最后一段、或者模型被打断时会出现半截标记。它是说明书的残片，
    既不能发出去也不能落库。
    """
    if not text:
        return ""
    match = _PENDING_MARKER_RE.search(text)
    return text[: match.start()] if match else text


def finalize_reply_text(text: str) -> str:
    """出口的最终清洗：剥标记 + 去半截标记 + 去掉行尾空白。幂等。

    只去**行尾的空格/制表符**，不动换行——换行是她决定怎么分段的依据。
    去掉半截标记后会留下一串尾随空格，不清掉就会落进数据库、进到气泡里。
    """
    return drop_partial_marker(strip_watch_markers(text)).rstrip(" \t")


def ref_hits(refs: list[str], videos: list[object]) -> dict[str, list[object]]:
    """短号 → 命中的视频列表（0 / 1 / ≥2 由调用方区分）。

    **撞车（≥2）不能"取第一条"**：那既多烧一次视频模型调用，又可能读了
    另一个人发的视频——"她看到了别人看不到的东西"比"没看成"糟得多。
    """
    wanted = {ref.lower() for ref in refs}
    hits: dict[str, list[object]] = {ref: [] for ref in wanted}
    if not hits:
        return {}
    for video in videos:
        ref = short_ref_for(str(getattr(video, "evidence_id", "")))
        if ref in hits:
            hits[ref].append(video)
    return hits


def resolve_watch_refs(refs: list[str], videos: list[object]) -> list[object]:
    """只返回**唯一命中**的那些证据（撞车与不存在的都不返回）。"""
    resolved: list[object] = []
    for found in ref_hits(refs, videos).values():
        if len(found) == 1:
            resolved.append(found[0])
    return resolved


def ambiguous_refs(refs: list[str], videos: list[object]) -> list[str]:
    """命中了 ≥2 条的短号（撞车）。"""
    return [ref for ref, found in ref_hits(refs, videos).items() if len(found) > 1]


# ---------------------------------------------------------------- 预算闸


# ---------------------------------------------------------------- 抽帧


class VideoReadError(Exception):
    """读视频失败。``reason`` 是给人看的一句话，进行为日志。"""

    def __init__(self, reason: str, *, code: str = "unavailable") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass(frozen=True)
class FrameSample:
    frames: list[bytes]
    fps: float
    long_video: bool

    @property
    def count(self) -> int:
        return len(self.frames)


def ffmpeg_available() -> bool:
    """本机有没有 ffmpeg。测试里没有就 skip，**不** mock 掉抽帧逻辑。"""
    return shutil.which("ffmpeg") is not None


def plan_sampling(duration: float | None) -> tuple[float, bool]:
    """(fps, 是否长视频)。

    - 短视频按 0.5 fps 抽，最多 16 帧；
    - 超过 ``LONG_VIDEO_SECONDS`` 就**全片等间隔**取 16 帧（并标注只看了
      一部分）——从头读 120 秒之后的内容等于没看，用户问的那件事常在后半段。
    """
    if duration is None or duration <= 0:
        return TARGET_FPS, False
    if duration <= LONG_VIDEO_SECONDS:
        return TARGET_FPS, False
    return MAX_FRAMES / duration, True


async def probe_duration(path: Path) -> float | None:
    """用 ffprobe 取时长；取不到返回 None（按短视频处理）。"""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=FFMPEG_TIMEOUT)
    except (OSError, TimeoutError, ValueError):
        return None
    try:
        return float(stdout.decode("utf-8", "ignore").strip() or "0") or None
    except ValueError:
        return None


async def extract_frames(data: bytes, *, suffix: str = ".mp4") -> FrameSample:
    """把视频抽成 JPEG 帧。失败一律抛 ``VideoReadError``（调用方转成"没看成"）。

    安全：ffmpeg 用**参数数组**调用（``create_subprocess_exec``），不经过
    shell，文件名由我们自己产生；临时目录用完即删。
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise VideoReadError("本机没有 ffmpeg，取不到视频内容")
    if not data:
        raise VideoReadError("视频内容为空")
    if len(data) > MAX_VIDEO_BYTES:
        raise VideoReadError(f"视频太大（{len(data) // (1024 * 1024)}MB）")

    with tempfile.TemporaryDirectory(prefix="ling-video-") as tmp:
        source = Path(tmp) / f"input{suffix}"
        source.write_bytes(data)
        duration = await probe_duration(source)
        fps, long_video = plan_sampling(duration)
        pattern = str(Path(tmp) / "frame%03d.jpg")
        args = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            # 长边 ≤512：视频模型按帧计费，缩到这个尺寸仍然看得清"在做什么"
            "-vf",
            f"fps={fps:.6f},scale='min({LONG_SIDE},iw)':'min({LONG_SIDE},ih)'"
            ":force_original_aspect_ratio=decrease",
            "-frames:v",
            str(MAX_FRAMES),
            "-q:v",
            "5",
            pattern,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            _stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=FFMPEG_TIMEOUT
            )
        except (OSError, TimeoutError) as exc:
            raise VideoReadError(f"抽帧失败: {type(exc).__name__}") from exc
        if proc.returncode != 0:
            raise VideoReadError(
                f"抽帧失败: {stderr.decode('utf-8', 'ignore').strip()[:200] or 'ffmpeg 退出异常'}"
            )
        frames = [path.read_bytes() for path in sorted(Path(tmp).glob("frame*.jpg"))]
    if not frames:
        raise VideoReadError("抽不出任何画面")
    return FrameSample(frames=frames, fps=fps, long_video=long_video)


def guess_video_suffix(mime: str | None) -> str:
    """临时文件名后缀：只用来让 ffmpeg 认出容器，不用它决定信任。"""
    mapping = {
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/webm": ".webm",
        "video/x-matroska": ".mkv",
        "video/x-msvideo": ".avi",
        "video/mpeg": ".mpeg",
    }
    return mapping.get((mime or "").lower(), ".mp4")


def video_url_for(data: bytes, mime: str | None) -> str:
    """原生视频输入用的 data URL（OpenAI 兼容的 ``video_url``）。"""
    if len(data) > MAX_VIDEO_BYTES:
        raise VideoReadError(f"视频太大（{len(data) // (1024 * 1024)}MB）")
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime or 'video/mp4'};base64,{encoded}"


__all__ = [
    "FFMPEG_TIMEOUT",
    "LONG_SIDE",
    "LONG_VIDEO_SECONDS",
    "MARKER_RE",
    "MAX_FRAMES",
    "MAX_VIDEO_BYTES",
    "TARGET_FPS",
    "FrameSample",
    "VideoReadError",
    "drop_partial_marker",
    "ambiguous_refs",
    "extract_frames",
    "ref_hits",
    "finalize_reply_text",
    "ffmpeg_available",
    "guess_video_suffix",
    "parse_watch_markers",
    "plan_sampling",
    "probe_duration",
    "resolve_watch_refs",
    "short_ref_for",
    "stream_safe",
    "strip_watch_markers",
    "video_url_for",
]
