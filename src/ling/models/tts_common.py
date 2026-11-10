"""TTS 适配器的共享部件：统一错误分类、结果表示、密钥脱敏。

三家 TTS（MiniMax / FishAudio / MiMo）的 **wire 协议** 差异极大——JSON+hex、
msgpack+二进制流、chat/completions+base64——所以它们各自一个文件、互不复用
请求构造。但有三件事必须**只有一份**，否则迟早各写一份然后互相漂：

1. **失败怎么分类**（:func:`classify_http_error` / :class:`TtsError`）：
   用户要看到的是"去检查密钥"还是"稍后重试"，取决于 401 与 503 被分到哪一类。
   三家各判一次的结果，是同一个 401 在 A 家说"检查密钥"、在 B 家说"网络问题"。
2. **结果长什么样**（:class:`SynthesizedAudio`）：音频字节 + 容器格式 + 时长。
   调用方不该关心某家返回的是 hex 还是 base64。
3. **密钥怎么不进日志**（:func:`redact`）：三家都是 Bearer token，
   而**上游的错误正文里经常回显它**。

## 为什么错误类型用 ``TtsError`` 而不是复用 ``SpeechError``

`ports/speech.py::SpeechError` 是**端口**的类型，只有 5 个 kind，缺
`authentication` / `rate_limited` / `invalid_request` 这几个 TTS 必需的区分
（`docs/design/SPEECH-IO-ARCHITECTURE.md` §3.2 明确要求扩展，且端口由 SP00
独占，本任务不得修改 `ports/`）。所以这里先定义适配器**内部**的错误类型，
端口冻结后由编排层统一转换（那时 `TtsError.kind` 到 `SpeechErrorKind` 是一张
查表，不需要改适配器）。

把转换放在编排层而不是适配器里，理由是：适配器不该知道"端口这一版有几个
kind"。端口什么时候加 `authentication`，加完这张表跟着变，三家适配器一行
都不用动。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class TtsErrorKind(StrEnum):
    """合成失败的**确定性**分类。用户提示与重试策略都由它决定。"""

    # 密钥无效/无权限（401/403）。**不可重试**，且必须提示"去检查密钥"——
    # 归到 error 里会让用户去查网络和地址，方向完全错。
    authentication = "authentication"
    # 供应商限流（429）。可重试，但要等，不是立刻重打。
    rate_limited = "rate_limited"
    # 请求本身不合法（400/422）：音色 ID 格式错、文本过长、格式不支持。
    # **不可重试**——重发同一个请求只会得到同一个 400。
    invalid_request = "invalid_request"
    # 供应商侧故障（5xx）或服务不可达。可重试。
    unavailable = "unavailable"
    # 超时（连接/读取）。可重试。
    timeout = "timeout"
    # 供应商 200 了，但响应不符合协议（缺字段、hex 解不开、不是音频）。
    # 这类**必须与 error 分开**：它是"对方没按约定说话"，往往是版本变更，
    # 而不是我们的参数错。
    protocol = "protocol"
    # 没配密钥/没配音色。**不可重试**，提示去设置里补。
    not_configured = "not_configured"


class TtsError(Exception):
    """一次合成失败。``message`` 已脱敏，可直接展示给用户。

    ``retriable`` 与 ``kind`` 分开：``rate_limited`` 与 ``timeout`` 都可重试，
    但退避策略不同；而 ``invalid_request`` 看着像"再试一次就好"，其实不。
    """

    def __init__(
        self,
        kind: TtsErrorKind,
        message: str,
        *,
        retriable: bool | None = None,
        status_code: int = 0,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status_code = int(status_code or 0)
        # 没显式给就按 kind 的默认语义推：**只把真正可能自愈的算可重试**。
        self.retriable = (
            kind in _RETRIABLE_KINDS if retriable is None else bool(retriable)
        )


_RETRIABLE_KINDS = frozenset(
    {
        TtsErrorKind.rate_limited,
        TtsErrorKind.unavailable,
        TtsErrorKind.timeout,
    }
)


@dataclass(frozen=True)
class SynthesizedAudio:
    """合成结果——**统一的内部表示**，与是哪一家无关。

    原始音频字节只在内存里传，不落日志、不进领域对象（领域层只存
    ``AudioRef``：引用 + 哈希 + 时长）。所以 ``audio`` 上的 ``repr`` 被关掉，
    免得有人顺手 ``logger.info("%s", result)`` 就把整段音频打印出来。
    """

    audio: bytes
    # 容器格式：``wav`` / ``mp3`` / ``pcm`` / ``flac`` / ``opus``。
    # 校验过实际字节头（见 :func:`sniff_container`），不是照抄请求参数——
    # 供应商回错格式的时候，"请求什么就说什么"会让下游转码器拿到假信息。
    container: str
    # 时长（毫秒）。拿不到时为 ``None``，**不估算**：假时长会让播放回执
    # 与"说完了没"的判断建立在编造的数字上。
    duration_ms: int | None = None
    # 采样率（Hz），拿不到为 ``None``。
    sample_rate: int | None = None
    #: 供应商回执里的请求 id（排障用；与密钥无关，可进日志）。
    provider_request_id: str | None = None

    def __repr__(self) -> str:
        # 刻意不含 audio 的内容，只给长度。
        return (
            f"SynthesizedAudio(container={self.container!r}, "
            f"bytes={len(self.audio)}, duration_ms={self.duration_ms!r}, "
            f"sample_rate={self.sample_rate!r})"
        )


# ------------------------------------------------------------------ 脱敏


#: 各家密钥的**常见**形态。这只是"见过的那几种"，不是穷举——密钥格式由
#: 厂商和用户决定（自建/中转的 token 可以任意命名），靠前缀猜永远漏。
#:
#: **为什么不指望 `logging_setup.redact_value`**：那里的 `_VALUE_PATTERNS`
#: 按设计**不猜厂商前缀**，只留有固定写法的形态（HTTP 鉴权头、URL 口令、长 hex），
#: 所以 `sk-...` 不会被它命中。日志层真正的防线是按**字段名**拦
#: （`SENSITIVE_KEYS` 里的 `api_key` / `token` / `authorization`），但那条防线
#: 要求密钥是作为字段值传进去的——上游把密钥回显在错误正文里时它拦不到，
#: 所以这里必须先抹一道。真正兜底的是 :func:`scrub`（按已知原文精确替换）。
_SECRET_PATTERNS = (
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9._\-]{6,}"),
    re.compile(r"\bsk_[A-Za-z0-9._\-]{6,}"),
    re.compile(r"\beyJ[A-Za-z0-9._\-]{10,}"),  # JWT: eyJ<header>.<payload>.<sig>
    re.compile(r"data:audio/[A-Za-z0-9.+\-]+;base64,[A-Za-z0-9+/=]{16,}"),
)

REDACTED = "[REDACTED]"


def redact(value: Any, *, limit: int = 240) -> str:
    """把任意值压成**单行、截断、已脱敏**的字符串，安全进异常消息与日志。

    三件事一起做，因为分开做总会漏一处：

    1. 抹掉密钥形态（Bearer / sk- / JWT / 音频 data URL）；
    2. 压成单行（上游的 JSON 错误常带换行，日志一条变多行会破坏解析）；
    3. 截断（避免把上游整页 HTML 回显给用户）。
    """
    text = str(value if value is not None else "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def scrub(text: str, secret: str) -> str:
    """把**这一家的实际密钥**从文本里精确抹掉。

    :func:`redact` 靠形态猜，遇到不常见的密钥格式会漏；这里用已知原文兜底。
    两道一起上：形态拦得住"没见过的 key"，精确替换兜得住"长得不像 key 的 key"。

    ``secret`` 太短时不替换——把 ``"a"`` 全局替换会把整段文本毁掉，
    反而让排障信息全丢。
    """
    if not secret or len(secret) < 6:
        return text
    return text.replace(secret, REDACTED)


# ------------------------------------------------------------------ HTTP 错误分类


#: HTTP 状态码 → 错误分类。**只收"确定"的映射**，其余落到 unavailable/error。
_STATUS_MAP: dict[int, TtsErrorKind] = {
    400: TtsErrorKind.invalid_request,
    401: TtsErrorKind.authentication,
    403: TtsErrorKind.authentication,
    404: TtsErrorKind.invalid_request,  # 路径或音色不存在：重试无用
    413: TtsErrorKind.invalid_request,  # 正文过长
    422: TtsErrorKind.invalid_request,
    429: TtsErrorKind.rate_limited,
}


def classify_http_error(
    status_code: int,
    body: str,
    *,
    secret: str = "",
    provider: str = "",
    hint: str = "",
) -> TtsError:
    """把一个 HTTP 失败响应变成 :class:`TtsError`。

    ``hint`` 是给用户的**可执行**补充（例如"去设置里检查 Voice ID"），
    因为同一个 400 在三家意味着不同的东西。
    """
    kind = _STATUS_MAP.get(int(status_code))
    if kind is None:
        kind = TtsErrorKind.unavailable if status_code >= 500 else TtsErrorKind.invalid_request
    detail = scrub(redact(body, limit=200), secret)
    who = f"{provider} " if provider else ""
    message = f"{who}合成失败（HTTP {status_code}）：{detail}"
    if hint:
        message = f"{message}。{hint}"
    return TtsError(kind, message, status_code=status_code)


# ------------------------------------------------------------------ 容器嗅探


def sniff_container(data: bytes) -> str | None:
    """按字节头判断音频容器。认不出返回 ``None``。

    **为什么不信请求参数**：供应商会在不支持的组合上静默回退（请求 mp3、
    回 wav），或者返回一整页 HTML 却带 200。下游转码器拿到错误的格式标签
    会解出噪音或直接崩，而报错会报在转码器那一层——离真正的原因很远。
    """
    if len(data) < 12:
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"fLaC":
        return "flac"
    if data[:4] == b"OggS":
        return "ogg"
    if data[4:8] == b"ftyp":
        return "m4a"
    # MP3: ID3 标签，或帧同步 0xFF 0xEx/0xFx
    if data[:3] == b"ID3":
        return "mp3"
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    # 裸 PCM（无头）：MiniMax 的 pcm 就是这种情况，叫不出容器名。
    if _looks_like_pcm(data):
        return "pcm"
    return None


#: 裸 PCM 至少要有这么多字节才敢认。太短的样本统计不可靠——任意 32 字节的
#: 随机数据都"不像文本"，但那不能说明它是音频。
_MIN_PCM_BYTES = 256


def _looks_like_pcm(data: bytes) -> bool:
    """裸 16-bit PCM 的启发式。**刻意保守到近乎悲观**。

    宁可不认（返回 None，调用方去报"格式无法识别"），也不要错认：
    把一个非音频 blob 当成 PCM，后续播放出来是噪音，而**没有任何错误**——
    这种"成功但听不出是话"的故障，比一个明确的报错难查一百倍。

    三条判据必须同时成立：

    1. 长度够（≥256 字节），且是**偶数**（16-bit 采样不会出现半个样本）；
    2. 可打印字符比例低（排除 HTML/JSON 错误正文）；
    3. 高字节（每第 2 字节）**取值集中**：16-bit 小端 PCM 的高字节是符号
       扩展位，真实语音里绝大多数落在 `0x00`/`0xFF` 附近。随机二进制的
       高字节是均匀分布的，过不了这一关——这正是"任意 blob 都像 PCM"
       那个错误的来源。

    判据 3 是本函数真正的价值所在：仅靠 1、2 两项时，`b"\x99\x88\x77\x66" * 8`
    这类随机数据会被误判成 PCM（实测过）。
    """
    if len(data) < _MIN_PCM_BYTES or len(data) % 2:
        return False
    sample = data[:8192]
    sample = sample[: len(sample) - (len(sample) % 2)]
    printable = sum(1 for byte in sample if 32 <= byte < 127 or byte in (9, 10, 13))
    if printable / len(sample) >= 0.75:
        return False
    high = sample[1::2]
    if not high:
        return False
    # 高字节落在 0x00/0x01/0xFE/0xFF 之一的比例。真实语音接近 1.0。
    near_rail = sum(1 for byte in high if byte <= 0x01 or byte >= 0xFE)
    return near_rail / len(high) >= 0.90


__all__ = [
    "REDACTED",
    "SynthesizedAudio",
    "TtsError",
    "TtsErrorKind",
    "classify_http_error",
    "redact",
    "scrub",
    "sniff_container",
]
