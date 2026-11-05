"""模型端口（port）。领域与内核只依赖这里的协议，不依赖任何供应商 SDK。

关键约束（设计文档 §8）：
- 一次轮次固定一个 config revision 与一个 model target。
- 流式失败要保留已交付部分，不能用新模型重放已执行的工具。
- 供应商密钥只在服务端；这里只接收已解密的内存字符串。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class ProviderKind(StrEnum):
    fake = "fake"
    openai_compatible = "openai_compatible"
    local = "local"


@dataclass(frozen=True)
class ImagePart:
    """一张图。``url`` 是 https 地址或 data URL（base64）。

    为什么不用 bytes：不同 provider 接受的形式不同（有的只收公网 URL，
    有的收 data URL），转换责任放在构造这一侧，端口只负责传递。
    用 ``from_bytes`` 从原始字节造 data URL，调用方不必自己拼格式。
    """

    url: str
    mime_type: str | None = None

    @classmethod
    def from_bytes(cls, data: bytes, mime_type: str = "image/jpeg") -> ImagePart:
        import base64

        encoded = base64.b64encode(data).decode("ascii")
        return cls(url=f"data:{mime_type};base64,{encoded}", mime_type=mime_type)


@dataclass(frozen=True)
class ChatMessageIn:
    role: str  # system | user | assistant
    content: str
    # 图像随消息一起送。空表示纯文本。
    # **不放进 content 里**：把 base64 塞进正文字符串会让日志、截断、
    # 字数预算全部失真，而且"这条消息有多长"变得没法算。
    images: tuple[ImagePart, ...] = ()


@dataclass(frozen=True)
class ModelRequest:
    """一次模型调用的完整输入。"""

    model_id: str
    messages: list[ChatMessageIn]
    temperature: float = 0.8
    max_tokens: int | None = None
    # 追踪与复现：记录到 turn 上，便于审计“为什么这样说”
    profile: str = "dialogue.fast"
    config_revision: int = 0
    trace_id: str | None = None
    timeout_seconds: float = 60.0


def has_images(messages: list[ChatMessageIn]) -> bool:
    return any(m.images for m in messages)


class ModelErrorKind(StrEnum):
    unavailable = "provider_unavailable"
    timeout = "provider_timeout"
    error = "provider_error"
    # 必须与 error 不同的值。原先两者都是 "provider_error"，
    # 于是 `auth` 成了 `error` 的**别名**：
    #   ModelErrorKind.auth is ModelErrorKind.error  → True
    # 后果是 401（密钥无效/无权限）被当成通用 provider 错误，
    # 经 ErrorCode(kind.value) 映射后丢掉"鉴权失败"语义，
    # 用户拿不到"去检查密钥"这类可执行提示。
    auth = "unauthorized"
    missing_secret = "secret_missing"
    unsupported = "unsupported_capability"


class ModelError(Exception):
    """provider 失败的确定性分类，内核据此给用户可执行的提示。"""

    def __init__(self, kind: ModelErrorKind, message: str, retriable: bool = True) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retriable = retriable


@dataclass
class ModelChunk:
    """流式增量。文本增量与结束原因是两个语义。"""

    delta: str = ""
    done: bool = False
    finish_reason: str | None = None


@dataclass
class ProbeResult:
    ok: bool
    detail: str
    latency_ms: int | None = None
    models: list[str] = field(default_factory=list)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_estimate: float | None = None


@runtime_checkable
class ModelProvider(Protocol):
    """所有 provider 适配器实现同一组方法。"""

    provider_id: str
    kind: ProviderKind

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        """逐段产出文本（异步生成器）。实现方必须在取消时抛 asyncio.CancelledError。"""
        ...

    async def probe(self) -> ProbeResult:
        """连接检查：不做真实生成，只验证鉴权与端点可达。"""
        ...


@dataclass(frozen=True)
class ModelTarget:
    """路由结果：一次轮次实际使用的 provider/model。"""

    provider_id: str
    model_id: str
    profile: str
    config_revision: int
    fallback_used: bool = False

    @property
    def label(self) -> str:
        return f"{self.provider_id}/{self.model_id}"


__all__ = [
    "ChatMessageIn",
    "ModelChunk",
    "ModelError",
    "ModelErrorKind",
    "ModelProvider",
    "ModelRequest",
    "ModelTarget",
    "ProbeResult",
    "ProviderKind",
    "Usage",
]
