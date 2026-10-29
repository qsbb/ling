"""确定性 fake provider：仅用于 W0 的界面与链路测试。

它**不是模型能力**。UI 必须显著标注“测试模式”，并且回复内容带有可识别的
测试标记，避免把固定假回复当成真实对话能力。
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator

from ling.ports.model import (
    ChatMessageIn,
    ModelChunk,
    ModelError,
    ModelErrorKind,
    ModelRequest,
    ProbeResult,
    ProviderKind,
)

TEST_BANNER = "〔测试模式·确定性 fake 回复，非真实模型输出〕"


def _persona_name(messages: list[ChatMessageIn]) -> str:
    for msg in messages:
        if msg.role == "system":
            # 从系统提示里找角色名，找不到就用 ling
            for line in msg.content.splitlines():
                if line.startswith("角色名："):
                    return line.split("：", 1)[1].strip() or "ling"
    return "ling"


def build_fake_reply(messages: list[ChatMessageIn], *, model_id: str) -> str:
    """根据输入生成稳定、可预测的回复（同一输入必得同一输出）。"""
    last_user = ""
    image_count = 0
    for msg in reversed(messages):
        if msg.role == "user":
            last_user = msg.content
            image_count = len(msg.images)
            break
    name = _persona_name(messages)
    digest = hashlib.sha256(f"{model_id}|{last_user}".encode()).hexdigest()[:6]
    snippet = last_user.strip().replace("\n", " ")
    if len(snippet) > 40:
        snippet = snippet[:40] + "…"
    # 图像计数写进回复，测试才能断言"图真的送到了模型这一层"。
    # 只说"看到 X 张图"而不描述内容——fake 不该假装自己看懂了。
    images_note = f"（收到 {image_count} 张图）" if image_count else ""
    if not snippet:
        return f"{TEST_BANNER}\n{name}收到一条空消息{images_note}，先等你说话。"
    return (
        f"{TEST_BANNER}\n"
        f"{name}听到你说「{snippet}」{images_note}。"
        f"这是链路测试回复（指纹 {digest}），它证明发送、流式、停止与重试都工作，"
        f"但不代表真实模型的对话能力。"
    )


class FakeProvider:
    """逐段流式、可取消、可注入故障的假供应商。"""

    provider_id = "fake"
    kind = ProviderKind.fake

    def __init__(
        self,
        *,
        chunk_delay: float = 0.05,
        fail_after_chunks: int | None = None,
        fail_kind: ModelErrorKind = ModelErrorKind.error,
    ) -> None:
        self._chunk_delay = chunk_delay
        self._fail_after = fail_after_chunks
        self._fail_kind = fail_kind

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        reply = build_fake_reply(request.messages, model_id=request.model_id)
        # 按字符切分，但保持中文语感：每 4 个字符一段
        chunks = [reply[i : i + 4] for i in range(0, len(reply), 4)]
        for index, chunk in enumerate(chunks):
            if self._fail_after is not None and index >= self._fail_after:
                raise ModelError(self._fail_kind, "fake provider 注入的故障（测试用）", retriable=True)
            await asyncio.sleep(self._chunk_delay)
            yield ModelChunk(delta=chunk)
        yield ModelChunk(done=True, finish_reason="stop")

    async def probe(self) -> ProbeResult:
        return ProbeResult(ok=True, detail="fake provider 始终可用（测试模式）", latency_ms=0,
                           models=["deterministic"])


class FailingFakeProvider(FakeProvider):
    """始终失败的假供应商，用于验证前端错误提示与重试。"""

    provider_id = "fake-failing"

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        raise ModelError(ModelErrorKind.unavailable, "fake provider 被配置为不可用", retriable=True)
        yield ModelChunk(done=True)  # pragma: no cover - 让类型检查认为这是异步生成器


__all__ = [
    "TEST_BANNER",
    "FailingFakeProvider",
    "FakeProvider",
    "build_fake_reply",
]
