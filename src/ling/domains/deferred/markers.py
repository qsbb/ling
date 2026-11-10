"""``<<等会回 90>>``：她自己决定"过一会儿再回"的结构化标记。

## 用户裁决（2026-11-10，原话）

「做，但是只在她主动选择时才延后，且延后多久让她自由发挥，上限 5 分钟」

三件事落到这个模块：

1. **默认立即回复**。解析不出合法标记就没有延后，代码绝不替她决定。所以
   ``parse_defer_marker`` 的返回值里"没有意图"是**正常路径**，不是错误。
2. **时长自由发挥**。不是从 ``0/15/45/90`` 里挑——那是我原来写进设计文档的
   档位，已被这条裁决推翻。所以这里接受任意秒数/分数写法（见
   :func:`parse_duration_seconds`），只受上限约束。
3. **300 秒硬上限，代码强制**。超了钳到 300，并且**如实记下被钳过**
   （:class:`DeferIntent.clamped` + ``requested_seconds``）。

## 为什么"被钳过"必须一路传到用户眼前

她写 ``<<等会回 999>>``（原意 16 分半），代码钳到 300 秒。如果只在库里记一个
``clamped=true``，用户看到的仍然是"她等了 5 分钟"——而那不是她的选择，是系统的
限制。**把系统限制伪装成她的选择**，与"把槽位忙包装成人格动作"是同一条底线
（`docs/MEDIA-WILLINGNESS-DESIGN.md` §4 第 27 条）。所以这个标志要一路传到
SSE 的 ``turn.deferred`` 里，前端必须能说"她本来想等更久"。

## 标记是**说明书**，绝不能漏给用户

三处出口都要剥：流式（`stream_safe_deferred`）、落库/外发前
（`strip_defer_markers`）、以及"半截标记"的尾巴（共用
`kernel/marker_stream.PENDING_MARKER_RE`）。

## 只有"整条回复就是这个标记"才算数

标记 + 正文同时出现时**不延后**，按坏格式处理：剥掉标记、正文照发、立即回复，
`reason=body_present`。理由——"延后"的语义是"我现在不回"，她既然写了回复，
那就是回了。反过来（保留延后、丢掉正文）会让她辛苦生成的正文凭空消失，而且
用户看到的是一轮**空回复**。宁可让她多回一句，也不要静默丢内容。

提示词那边对应地告诉她：想晚点回就**只写标记、不要写正文**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ling.domains.deferred.state import MAX_DELAY_SECONDS

__all__ = [
    "BAD_FORMAT_BODY_PRESENT",
    "BAD_FORMAT_EMPTY",
    "BAD_FORMAT_MULTIPLE",
    "BAD_FORMAT_UNPARSABLE",
    "DEFERRED_MARKER_RE",
    "DeferIntent",
    "defer_marker_prefix",
    "parse_defer_marker",
    "parse_duration_seconds",
    "stream_safe_deferred",
    "strip_defer_markers",
]


#: 完整标记：``<<等会回 90>>``。中间那段是**时长表达式**，允许任意非尖括号字符，
#: 具体合法性交给 :func:`parse_duration_seconds`——正则只负责"这是不是一条标记"。
#:
#: 为什么时长不写进正则：``2分`` / ``90秒`` / ``1分30秒`` 的写法有无数种，写成
#: 正则以后每加一种都要动正则，而正则一改就可能把正常正文吃掉。分开之后，
#: "认不认得出这是标记"和"这个时长合不合法"是两个独立的判据，各自可测。
DEFERRED_MARKER_RE = re.compile(r"<<\s*等会回\s*([^<>\n]*?)\s*>>")

#: 时长表达式里的合法字符。用白名单而不是黑名单：时长只可能是数字 + 单位。
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(秒钟|秒|分钟|分)?\s*$")
_COMBINED_RE = re.compile(
    r"^\s*(\d+)\s*(?:分钟|分)\s*(?:(\d+)\s*(?:秒钟|秒))?\s*$"
)

#: 坏格式原因码（稳定字符串，会进行为日志/运行日志）。
BAD_FORMAT_EMPTY = "duration_empty"
BAD_FORMAT_UNPARSABLE = "duration_unparsable"
BAD_FORMAT_MULTIPLE = "multiple_markers"
BAD_FORMAT_BODY_PRESENT = "body_present"


@dataclass(frozen=True)
class DeferIntent:
    """一次**合法**的延后意愿。

    ``requested_seconds`` 是她**真正要求**的时长，``applied_seconds`` 是代码
    实际会用的时长。两者不等时 ``clamped`` 为真——这正是"不能把系统上限说成
    她的选择"那条底线在数据结构上的落点。
    """

    requested_seconds: float
    applied_seconds: int
    clamped: bool

    @property
    def applied_ms(self) -> int:
        return int(self.applied_seconds * 1000)

    @property
    def requested_ms(self) -> int:
        return int(self.requested_seconds * 1000)


def parse_duration_seconds(raw: str) -> float | None:
    """把时长表达式解析成秒数；解析不了返回 ``None``。

    接受：``90`` / ``90秒`` / ``90秒钟`` / ``2分`` / ``2分钟`` / ``1分30秒``。
    不接受：空、纯文字、负数、带符号或单位乱写。

    **不在这里做上限钳制**：钳制是策略，解析是事实。混在一起会让
    "她要求了 999 秒"这个事实在解析阶段就消失，而它恰恰是要如实上报的东西。
    """
    text = (raw or "").strip()
    if not text:
        return None
    combined = _COMBINED_RE.match(text)
    if combined:
        minutes = int(combined.group(1))
        seconds = int(combined.group(2) or 0)
        return float(minutes * 60 + seconds)
    match = _DURATION_RE.match(text)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2) or "秒"
    if value < 0:  # pragma: no cover - 正则已排除负号，留作显式契约
        return None
    return value * 60 if unit in {"分", "分钟"} else value


def clamp_seconds(seconds: float) -> tuple[int, bool]:
    """返回 ``(实际秒数, 是否被钳过)``。上限见 ``MAX_DELAY_SECONDS``。"""
    applied = min(float(MAX_DELAY_SECONDS), max(0.0, seconds))
    return int(applied), applied < seconds


def parse_defer_marker(text: str) -> tuple[str, DeferIntent | None, str | None]:
    """取出延后标记，返回 ``(剥掉标记后的正文, 意图, 坏格式原因)``。

    **幂等**：对已经剥过的文本再调一次，得到同样的正文与 ``(None, None)``。

    三种结果，语义互斥：

    - ``(正文, DeferIntent, None)``：合法延后。调用方应当**不投递**本轮，
      而是登记一条 ``deferred_replies`` 行。
    - ``(正文, None, 原因)``：出现了标记但**不延后**（坏格式、多个标记、
      或标记旁边还有正文）。调用方剥掉标记后**立即正常回复**——绝不静默挂起。
    - ``(正文, None, None)``：压根没有标记。正常路径。

    "标记 + 正文"归入坏格式（见模块注释）：她既然写了回复，那就是回了。
    """
    if not text:
        return "", None, None
    matches = list(DEFERRED_MARKER_RE.finditer(text))
    if not matches:
        return text, None, None

    cleaned = DEFERRED_MARKER_RE.sub("", text)
    # 标记被剥掉后常在原处留一串空格；收成单个空格。换行**不动**——那是她
    # 故意分段的地方（与 `domains/media/video.py::parse_watch_markers` 同规则）。
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()

    if len(matches) > 1:
        # 多个标记 = 她自己没说清要等多久。不能挑一个执行——那是替她决定。
        return cleaned, None, BAD_FORMAT_MULTIPLE

    raw = matches[0].group(1)
    if not raw.strip():
        return cleaned, None, BAD_FORMAT_EMPTY
    seconds = parse_duration_seconds(raw)
    if seconds is None:
        return cleaned, None, BAD_FORMAT_UNPARSABLE
    if cleaned:
        # 有正文：不延后（见模块注释）。标记已剥掉，正文照发。
        return cleaned, None, BAD_FORMAT_BODY_PRESENT
    applied, clamped = clamp_seconds(seconds)
    return cleaned, DeferIntent(
        requested_seconds=seconds, applied_seconds=applied, clamped=clamped
    ), None


def strip_defer_markers(text: str) -> str:
    """只剥标记（出口用）。幂等。"""
    if not text:
        return ""
    return DEFERRED_MARKER_RE.sub("", text)


def stream_safe_deferred(text: str) -> tuple[str, str]:
    """流式过滤：返回 (可以立刻发出去的部分, 需要暂扣的尾巴)。

    暂扣的只有"看起来是半个 ``<<等会回`` 标记"的尾巴。与
    ``domains/media/video.py::stream_safe`` 同语义：不暂扣的话，SSE 里会先把
    ``<<等会回 90>>`` 原样推给浏览器，用户看见说明书，然后再看它被删掉。

    **普通正文零影响**：不含 ``<<`` 的文本原样返回，不攒字、不延迟首字。
    """
    if not text:
        return "", ""
    head, sep, tail = text.rpartition("<<")
    if not sep:
        # 末尾孤零零一个 ``<`` 可能是被流式切开的 ``<<`` 前半截。压住它，
        # 下一片来了再判断（与共用常量同一取舍，见 marker_stream 的注释）。
        if text.endswith("<"):
            return text[:-1], "<"
        return text, ""
    # 尾部已是完整标记，或后面根本不像标记：都不用压
    if ">>" in tail or not _DEFERRED_PENDING_RE.match(sep + tail):
        return text, ""
    return head, sep + tail


#: "可能是半个延后标记"的形状。必须限定 ``等会回`` 前缀，否则 ``1<<2`` 这类正常
#: 正文会被静默吃掉（`kernel/marker_stream.py` 的模块注释记录过这个坑）。
#:
#: 动词那段是**逐字可选**的：流式先到 ``<``、再到 ``<<``、然后才是 ``等``。若
#: ``<<`` 本身不匹配就会立刻被放出去，等 ``等`` 到达时前半截已经在浏览器里了。
_DEFERRED_PENDING_RE = re.compile(r"<<\s*(?:等(?:会(?:回[^<>\n]*)?)?)?>?$")


def defer_marker_prefix() -> str:
    """给提示词用的标记写法示例。**唯一出处**——提示词和解析不能各写一份。"""
    return "<<等会回 90>>"
