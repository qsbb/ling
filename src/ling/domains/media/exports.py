"""生成媒体的**授权导出**：谁能取、怎么取、取多久。

## 为什么不能复用入站证据的授权

`media_evidence` 的授权模型是为**用户发来的图片**设计的：

- 它绑定 `window_id`，只有这个窗口能读；
- 是否能**描述**由 `media.inbound` 授权决定，**默认拒绝**；
- 撤权后按 `consent_revision` 失效。

拿它来拦 TTS 输出会直接坏掉正常功能：音频是我们自己生成的、要发给**当前
这条消息的收件人**听，而"默认拒绝 + 绑定授权窗口"会让它在大多数情况下取不
出来——用户反馈就是"语音发不出去"。

所以生成媒体有一套**自己的、更窄也更好推理**的规则：

1. **归属即授权**：文件的 `actor_id` + `window_id` 是唯一凭据。同一个窗口
   里的人可以取；别人不行。
2. **不需要额外同意**：内容是我们自己生成的，不含用户私有素材的推断——
   它不携带"用户给我看了一张照片"那种需要同意的信息。
3. **一切外部访问都带凭据**：本地路径 / base64 只在**进程内**用；跨进程
   一律走签名 URL（短时效、单文件、只读、不含身份）。

## 三种 transport

渠道要拿到音频有三种方式，对应三种真实存在的 OneBot/QQ 实现差异：

| transport | 形态 | 用途 | 授权载体 |
|---|---|---|---|
| `local_path` | 绝对文件路径 | 同机部署（NapCat 与 ling 同容器/同机） | 进程内，无 URL |
| `base64` | `base64://<...>` | OneBot 的 base64 段 | 进程内，内容进内存 |
| `signed_url` | `https://…/v1/media/generated/<id>?exp=…&sig=…` | 跨机部署 | 签名本身 |

**本地路径是危险的**：它绕过一切授权，拿到字符串就能读。所以它只在
"调用方就是本进程"时才给——`ExportTarget` 显式要求调用方声明这一点。

## 签名方案

签名覆盖 **(file_id, expires_at) 二元组**，不是整个 URL：

```
payload = f"{file_id}\n{expires_at}"
sig     = base64url(HMAC-SHA256(derive(root_key, "generated-media-url"), payload))
```

三个设计点：

- **单文件**：签名里含 `file_id`，改一个字符就失效。不能拿一个签名去取别的
  音频——那是越权的经典入口（IDOR）。
- **短时效**：`expires_at` 进签名，服务端只比较时间，不需要存"已发出的
  签名"（那会变成一张要清理的表，还会被重放打满）。默认 5 分钟：够渠道
  下载，不够被人转发出去再利用。
- **不含身份**：签名里只有 file_id 与过期时间。URL 里带 `actor_id` 会让
  它变成"谁点的链接就是谁"的伪凭据，也会把用户标识泄漏到渠道日志里。

**派生密钥而不是直接用根密钥**：根密钥还用于加密供应商密钥。用同一把钥匙
干两件事，一旦签名侧出现（哪怕只是理论上的）密钥恢复/侧信道，就波及密钥库。
`hmac(root_key, "generated-media-url")` 派生出一把只能签 URL 的子密钥。

## 签名不进日志

`SignedUrl.__str__` 返回的是**打码后**的形态（`sig=` 值被替换），所以任何
`logger.info("%s", url)`、异常消息、审计记录里都不会出现可用的签名。想要
完整的 URL 只能用 `.url` 属性，而这一点在代码里是显式的。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from enum import StrEnum

#: 派生签名子密钥用的标签。改这个值等于让所有已发出的签名立即失效。
_SIGNING_LABEL = b"generated-media-url"

#: 默认有效期。够一次下载（渠道通常几秒内取走），又不至于被转发后长期可用。
DEFAULT_URL_TTL_SECONDS = 300

#: TTL 上限。调用方不能要到"一年有效"的链接——那就等于永久公开。
MAX_URL_TTL_SECONDS = 3600


class ExportTransport(StrEnum):
    """渠道取音频的三种方式。"""

    local_path = "local_path"
    base64 = "base64"
    signed_url = "signed_url"


class ExportDenied(Exception):
    """授权失败（不是"文件不存在"）。"""


class SignatureError(Exception):
    """签名缺失/格式错误/不匹配/过期。"""


def derive_signing_key(root_key: bytes) -> bytes:
    """从根密钥派生**只能用来签 URL** 的子密钥。"""
    return hmac.new(root_key, _SIGNING_LABEL, hashlib.sha256).digest()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class SignedUrl:
    """签名 URL。`url` 含可用签名；`str()` 给的是打码版本。"""

    url: str
    file_id: str
    expires_at: int

    def __str__(self) -> str:
        """打码形态：`sig` 值替换成 `***`。

        这是**默认**的字符串表示，因此任何日志/异常/审计路径只要用 `%s`
        就自动安全。要真正发给渠道时必须显式写 `.url`——故意让"泄漏签名"
        成为一个需要主动选择的动作。
        """
        return _mask_signature(self.url)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"SignedUrl(file_id={self.file_id!r}, expires_at={self.expires_at}, url={self})"


def _mask_signature(url: str) -> str:
    if "sig=" not in url:
        return url
    head, _, _tail = url.partition("sig=")
    return f"{head}sig=***"


def sign_file_url(
    *,
    root_key: bytes,
    base_url: str,
    file_id: str,
    ttl_seconds: int = DEFAULT_URL_TTL_SECONDS,
    now: float | None = None,
) -> SignedUrl:
    """为**单个**文件签发短时效只读 URL。"""
    if ttl_seconds <= 0:
        raise ValueError("签名 URL 的有效期必须为正")
    if ttl_seconds > MAX_URL_TTL_SECONDS:
        raise ValueError(f"签名 URL 的有效期不能超过 {MAX_URL_TTL_SECONDS} 秒")

    issued = int(now if now is not None else time.time())
    expires_at = issued + int(ttl_seconds)
    payload = f"{file_id}\n{expires_at}".encode()
    sig = _b64url(hmac.new(derive_signing_key(root_key), payload, hashlib.sha256).digest())
    url = f"{base_url.rstrip('/')}/v1/media/generated/{file_id}?exp={expires_at}&sig={sig}"
    return SignedUrl(url=url, file_id=file_id, expires_at=expires_at)


def verify_file_url(
    *,
    root_key: bytes,
    file_id: str,
    expires_at: int,
    signature: str,
    now: float | None = None,
) -> None:
    """校验签名与时效。失败一律抛 `SignatureError`（**不区分**原因）。

    为什么错误信息不区分"签名不对"和"已过期"：对外只有"这个链接不能用"。
    区分开等于告诉攻击者"签名算法猜对了、只是时间不对"，可以顺势调整重试。
    """
    current = int(now if now is not None else time.time())
    if expires_at <= current:
        raise SignatureError("媒体链接已过期")
    payload = f"{file_id}\n{expires_at}".encode()
    expected = _b64url(hmac.new(derive_signing_key(root_key), payload, hashlib.sha256).digest())
    # compare_digest：不要在比较上泄漏"前几位对不对"
    if not hmac.compare_digest(expected, signature):
        raise SignatureError("媒体链接无效")


def check_window_access(
    *,
    requester_window: str | None,
    requester_actor: str | None,
    file_window: str,
    file_actor: str,
) -> None:
    """判断请求者能不能取这个文件，不能就抛 `ExportDenied`。

    规则（**与入站证据的授权无关**，见模块文档）：

    - 同 `actor_id` **且** 同 `window_id` → 放行（就是这条消息的收件人）；
    - 只有 `actor_id` 相同、窗口不同 → **拒绝**。同一个人的两个群是两个
      语境：在 A 群生成给 A 群听的语音不该能在 B 群里被取走。
    - 其余一律拒绝。

    两个参数都允许为 `None`（未登录/系统内部调用），此时**拒绝**——
    "没有身份"不能等于"通行"。
    """
    if not requester_window or not requester_actor:
        raise ExportDenied("缺少调用方身份，拒绝导出生成媒体")
    if requester_window != file_window or requester_actor != file_actor:
        raise ExportDenied("该媒体不属于当前窗口")


@dataclass(frozen=True)
class ExportTarget:
    """一次导出的结果描述。"""

    transport: ExportTransport
    #: `local_path` / `base64` 时是 `None`（内容走别的字段）。
    url: str | None
    #: `local_path` 时的绝对路径。
    path: str | None
    #: `base64` 时的 `base64://…` 字符串。
    data_uri: str | None
    size_bytes: int
    mime_type: str


def to_base64_uri(data: bytes, mime_type: str) -> str:
    """OneBot 的 base64 段形态：``base64://<无填充 base64>``。

    不带 `data:` 前缀，因为 OneBot 规范要的就是 `base64://`。
    """
    return f"base64://{base64.b64encode(data).decode('ascii')}"


def export_as(
    *,
    transport: ExportTransport,
    digest: str,
    data: bytes,
    abs_path: str,
    mime_type: str,
    root_key: bytes,
    base_url: str,
    ttl_seconds: int = DEFAULT_URL_TTL_SECONDS,
) -> ExportTarget:
    """按 transport 组装导出结果。**授权在此之前完成**（`check_window_access`）。

    三种形态都从**同一份**已授权的输入构造，所以不存在"某种 transport 绕过
    了检查"的可能——检查不在这个函数里，而在它的调用者身上，且与 transport
    无关。
    """
    if transport is ExportTransport.local_path:
        return ExportTarget(
            transport=transport,
            url=None,
            path=abs_path,
            data_uri=None,
            size_bytes=len(data),
            mime_type=mime_type,
        )
    if transport is ExportTransport.base64:
        return ExportTarget(
            transport=transport,
            url=None,
            path=None,
            data_uri=to_base64_uri(data, mime_type),
            size_bytes=len(data),
            mime_type=mime_type,
        )
    signed = sign_file_url(
        root_key=root_key, base_url=base_url, file_id=digest, ttl_seconds=ttl_seconds
    )
    return ExportTarget(
        transport=transport,
        url=signed.url,
        path=None,
        data_uri=None,
        size_bytes=len(data),
        mime_type=mime_type,
    )
