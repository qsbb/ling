"""模型路由：按职责 profile 选主/备，统一超时、熔断与错误分类。

约束（设计文档 §8.2）：
- 一次轮次固定一个 config revision。
- 只有**尚未产生外部副作用与已发送文本**时才允许透明回退。
- 流式中途失败必须把失败告诉调用方，并保留已交付部分。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from ling.models.registry import ProviderRegistry, UnknownProviderError
from ling.ports.config import EffectiveConfig, ProfileBinding
from ling.ports.model import (
    ChatMessageIn,
    ModelChunk,
    ModelError,
    ModelErrorKind,
    ModelProvider,
    ModelRequest,
    ModelTarget,
    ProbeResult,
    ProviderKind,
)

logger = logging.getLogger("ling.models.router")


@dataclass
class HealthRecord:
    consecutive_failures: int = 0
    opened_until: float = 0.0
    last_error: str | None = None
    last_latency_ms: int | None = None

    def is_open(self) -> bool:
        return time.monotonic() < self.opened_until


@dataclass
class StreamOutcome:
    """一次流式调用的结果汇总（供 turn 记录与费用统计）。"""

    text: str = ""
    target: ModelTarget | None = None
    finish_reason: str | None = None
    error: ModelError | None = None
    fallback_used: bool = False
    delivered_any: bool = False


class ModelRouter:
    """把 profile → provider/model 的解析与调用集中在一处。"""

    BREAKER_THRESHOLD = 3
    BREAKER_COOLDOWN_SECONDS = 20.0

    def __init__(self, registry: ProviderRegistry) -> None:
        self._registry = registry
        self._health: dict[str, HealthRecord] = {}

    # ---------------------------------------------------------- 解析

    def resolve(self, config: EffectiveConfig, profile: str) -> tuple[ModelTarget, ProfileBinding]:
        binding = config.binding(profile)
        if binding is None or not binding.enabled:
            raise ModelError(
                ModelErrorKind.unsupported,
                f"职责 {profile} 尚未绑定模型，请在设置里配置后再试",
                retriable=False,
            )
        target = ModelTarget(
            provider_id=binding.provider_id,
            model_id=binding.model_id,
            profile=profile,
            config_revision=config.revision,
        )
        return target, binding

    def _binding_for(self, config: EffectiveConfig, binding: ProfileBinding, *, fallback: bool):
        if not fallback:
            return binding
        if not binding.fallback_provider_id or not binding.fallback_model_id:
            return None
        return ProfileBinding(
            profile=binding.profile,
            provider_id=binding.fallback_provider_id,
            model_id=binding.fallback_model_id,
            temperature=binding.temperature,
            max_tokens=binding.max_tokens,
            timeout_seconds=binding.timeout_seconds,
        )

    def _provider(self, config: EffectiveConfig, binding: ProfileBinding) -> ModelProvider:
        try:
            provider = self._registry.provider_for(config, binding)
        except UnknownProviderError as exc:
            raise ModelError(ModelErrorKind.unavailable, str(exc), retriable=False) from exc
        # 硬门：非测试模式下绝不允许用 fake provider 冒充模型。
        # 否则用户会把确定性假回复当成角色真实发言（设计文档 §8.2 披露等级一致）。
        if not config.test_mode and getattr(provider, "kind", None) is ProviderKind.fake:
            raise ModelError(
                ModelErrorKind.unsupported,
                f"{binding.provider_id} 是测试用 fake provider，非测试模式下不能使用；"
                "请在设置里绑定真实供应商",
                retriable=False,
            )
        return provider

    # ---------------------------------------------------------- 调用

    async def stream(
        self,
        config: EffectiveConfig,
        *,
        profile: str,
        messages: list[ChatMessageIn],
        trace_id: str | None = None,
        allow_fallback: bool = True,
    ) -> AsyncIterator[tuple[ModelChunk, StreamOutcome]]:
        """产出 ``(chunk, outcome)``；outcome 在最后一帧包含完整文本与目标。

        回退只发生在**尚未产出任何文本**时。
        """
        binding = config.binding(profile)
        if binding is None or not binding.enabled:
            raise ModelError(
                ModelErrorKind.unsupported,
                f"职责 {profile} 尚未绑定模型，请在设置里配置后再试",
                retriable=False,
            )

        candidates = [binding]
        if allow_fallback:
            fb = self._binding_for(config, binding, fallback=True)
            if fb is not None:
                # 回退候选同样要过"不得用 fake 冒充真实模型"的硬门
                fb_provider = config.providers.get(fb.provider_id)
                if (
                    not config.test_mode
                    and fb_provider is not None
                    and fb_provider.kind == ProviderKind.fake.value
                ):
                    logger.warning(
                        "忽略回退候选 %s：非测试模式下不能回退到 fake provider",
                        fb.provider_id,
                    )
                else:
                    candidates.append(fb)

        last_error: ModelError | None = None
        for index, candidate in enumerate(candidates):
            target = ModelTarget(
                provider_id=candidate.provider_id,
                model_id=candidate.model_id,
                profile=profile,
                config_revision=config.revision,
                fallback_used=index > 0,
            )
            health_key = f"{candidate.provider_id}/{candidate.model_id}"
            health = self._health.setdefault(health_key, HealthRecord())
            if health.is_open():
                last_error = ModelError(
                    ModelErrorKind.unavailable,
                    f"{target.label} 连续失败，已暂时熔断",
                    retriable=True,
                )
                continue

            provider = self._provider(config, candidate)
            request = ModelRequest(
                model_id=candidate.model_id,
                messages=messages,
                temperature=candidate.temperature,
                max_tokens=candidate.max_tokens,
                profile=profile,
                config_revision=config.revision,
                trace_id=trace_id,
                timeout_seconds=candidate.timeout_seconds,
            )
            outcome = StreamOutcome(target=target, fallback_used=index > 0)
            started = time.monotonic()
            try:
                async for chunk in self._timed_stream(provider, request):
                    if chunk.delta:
                        outcome.text += chunk.delta
                        outcome.delivered_any = True
                    if chunk.done:
                        outcome.finish_reason = chunk.finish_reason
                    yield chunk, outcome
                health.consecutive_failures = 0
                health.last_latency_ms = int((time.monotonic() - started) * 1000)
                health.last_error = None
                return
            except asyncio.CancelledError:
                # 用户主动停止：不算 provider 故障
                raise
            except ModelError as exc:
                health.consecutive_failures += 1
                health.last_error = exc.message
                if health.consecutive_failures >= self.BREAKER_THRESHOLD:
                    health.opened_until = time.monotonic() + self.BREAKER_COOLDOWN_SECONDS
                outcome.error = exc
                last_error = exc
                if outcome.delivered_any:
                    # 已经交付过文本：不换模型重放，直接把错误交给上层
                    raise
                continue
            except Exception as exc:  # noqa: BLE001 - provider 边界统一归类
                wrapped = ModelError(ModelErrorKind.error, f"provider 异常: {exc}", retriable=True)
                health.consecutive_failures += 1
                health.last_error = wrapped.message
                if health.consecutive_failures >= self.BREAKER_THRESHOLD:
                    health.opened_until = time.monotonic() + self.BREAKER_COOLDOWN_SECONDS
                last_error = wrapped
                outcome.error = wrapped
                if outcome.delivered_any:
                    raise wrapped from exc
                continue

        raise last_error or ModelError(
            ModelErrorKind.unavailable, "没有可用的模型候选", retriable=True
        )

    @staticmethod
    async def _timed_stream(
        provider: ModelProvider, request: ModelRequest
    ) -> AsyncIterator[ModelChunk]:
        """把 provider 的异常归一化成 ModelError，并保留取消语义。"""
        agen = provider.stream(request)
        try:
            async for chunk in agen:
                yield chunk
        except asyncio.CancelledError:
            raise
        except ModelError:
            raise
        except TimeoutError as exc:
            raise ModelError(ModelErrorKind.timeout, "模型响应超时", retriable=True) from exc
        except Exception as exc:  # noqa: BLE001
            raise ModelError(ModelErrorKind.error, f"provider 调用失败: {exc}", retriable=True) from exc
        finally:
            aclose = getattr(agen, "aclose", None)
            if aclose is not None:
                await aclose()

    # ---------------------------------------------------------- 诊断

    async def probe(self, config: EffectiveConfig, provider_id: str) -> ProbeResult:
        cfg = config.providers.get(provider_id)
        if cfg is None:
            return ProbeResult(ok=False, detail=f"provider 未登记: {provider_id}")
        try:
            provider = self._registry.get(cfg)
        except UnknownProviderError as exc:
            return ProbeResult(ok=False, detail=str(exc))
        try:
            return await asyncio.wait_for(provider.probe(), timeout=15.0)
        except TimeoutError:
            return ProbeResult(ok=False, detail="连接检查超时")
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(ok=False, detail=f"连接检查失败: {exc}")

    def health_snapshot(self) -> dict[str, dict[str, object]]:
        return {
            key: {
                "consecutive_failures": rec.consecutive_failures,
                "open": rec.is_open(),
                "last_error": rec.last_error,
                "last_latency_ms": rec.last_latency_ms,
            }
            for key, rec in self._health.items()
        }

    def reset_health(self) -> None:
        self._health.clear()


__all__ = ["HealthRecord", "ModelRouter", "StreamOutcome"]
