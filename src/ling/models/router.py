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

from ling.models.quota import TokenQuotaStore, estimate_tokens_for
from ling.models.registry import ProviderRegistry, UnknownProviderError
from ling.ports.config import EffectiveConfig, ProfileBinding, ProviderConfig
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

    def __init__(
        self,
        registry: ProviderRegistry,
        quota: TokenQuotaStore | None = None,
    ) -> None:
        self._registry = registry
        self._health: dict[str, HealthRecord] = {}
        # 额度记账是可选的：worker 的记忆提炼、单元测试与离线脚本里没有
        # 数据库会话，也不该为了"记个数"去连库。没有 quota 时行为与从前一致
        # （不限额），而不是偷偷按某个默认值拦调用。
        self._quota = quota

    # ---------------------------------------------------------- 解析

    def _resolved_binding(
        self, config: EffectiveConfig, profile: str
    ) -> tuple[ProfileBinding, str]:
        """职责 → 真正要调的那家/那个模型。

        **判定只有一份**：`EffectiveConfig.resolve_profile`（显式 → 所属槽位 →
        不可用，只走一层）。这里不再自己判"有没有绑定"——上了默认槽位之后，
        "有没有绑定"和"有没有可用模型"已经不是一回事了，各判一次的结果是
        路由认为没有模型可调、而设置页显示一切正常。

        返回的 ``ProfileBinding`` 是**解析之后**的样子（provider/model 已经
        换成真正要调的），调用方不用再关心它是显式来的还是槽位来的。
        """
        resolved = config.resolve_profile(profile)
        if not resolved.usable or not resolved.provider_id or not resolved.model_id:
            raise ModelError(
                ModelErrorKind.unsupported,
                f"职责 {profile} 没有可用模型：{resolved.reason}。请在设置里配置后再试",
                retriable=False,
            )
        stored = config.profiles.get(profile)
        binding = ProfileBinding(
            profile=profile,
            provider_id=resolved.provider_id,
            model_id=resolved.model_id,
            # 调用时回退（这一家挂了换下一家）仍然只看显式配置那条。
            # 「跟随槽位」的绑定在保存时就把这两项清空了，所以它不会
            # 凭空多出一层回退——"只回退一层"对两条路都成立。
            fallback_provider_id=stored.fallback_provider_id if stored else None,
            fallback_model_id=stored.fallback_model_id if stored else None,
        )
        return binding, resolved.reason

    def resolve(self, config: EffectiveConfig, profile: str) -> tuple[ModelTarget, ProfileBinding]:
        binding, _reason = self._resolved_binding(config, profile)
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
        # 回退候选只换"谁"，不换超时/额度：那些跟着 provider 走，
        # 由 `_request_for` 从 ProviderConfig 里取。
        return ProfileBinding(
            profile=binding.profile,
            provider_id=binding.fallback_provider_id,
            model_id=binding.fallback_model_id,
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

    # ---------------------------------------------------------- 额度

    async def _quota_block(self, pcfg: ProviderConfig | None) -> ModelError | None:
        """今天的额度用完了就返回一个错误，否则 None。

        额度只拦"这一家"，不拦整轮：有备用供应商时调用方会换下一家。
        """
        if self._quota is None or pcfg is None:
            return None
        limit = pcfg.daily_limit_effective()
        if limit is None:
            return None
        used = await self._quota.used(pcfg.provider_id)
        if used < limit:
            return None
        logger.warning(
            "供应商 %s 今日额度已用完（%d/%d tokens），本轮跳过它",
            pcfg.provider_id,
            used,
            limit,
        )
        return ModelError(
            ModelErrorKind.quota_exhausted,
            f"供应商 {pcfg.provider_id} 今天的 token 额度已用完"
            f"（已用 {used} / 上限 {limit}）。换一家供应商、提高额度，或等明天重置。",
            retriable=False,
        )

    async def _record_usage(
        self,
        provider_id: str,
        provider: ModelProvider,
        messages: list[ChatMessageIn],
        outcome: StreamOutcome,
    ) -> None:
        """把这次调用的 token 记进当日额度。

        优先用供应商回执（``provider.last_usage``）；没有回执时按文本估算，
        并标记 ``estimated``——否则不支持 usage 的兼容端点会永远显示 0，
        额度就成了摆设。记账失败**不能**影响这一轮回复：额度是记账，
        回复才是用户要的东西。
        """
        if self._quota is None:
            return
        try:
            usage = getattr(provider, "last_usage", None)
            if usage is not None and usage.total_tokens > 0:
                await self._quota.record(provider_id, int(usage.total_tokens))
                return
            if outcome.text:
                tokens = estimate_tokens_for(messages, outcome.text)
                await self._quota.record(provider_id, tokens, estimated=True)
        except Exception as exc:  # noqa: BLE001 - 记账失败不影响回复
            logger.warning("token 用量记账失败（%s）：%s", provider_id, exc)

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
        binding, _reason = self._resolved_binding(config, profile)

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
            # 超时与输出上限属于**供应商**（"这家怎么连"），不属于职责。
            # 拿不到供应商配置时退回 ModelRequest 的默认值：那种情况本身
            # 会在 `_provider` 里报错，不该在这里再造一个静默分支。
            pcfg = config.providers.get(candidate.provider_id)
            quota_block = await self._quota_block(pcfg)
            if quota_block is not None:
                # 这一家今天的额度用完了：不调用，直接当作这一候选不可用。
                # 有回退就换下一家，没有就把原因交给上层——静默失败会让
                # 用户以为"她今天不想说话"。
                last_error = quota_block
                continue
            request = ModelRequest(
                model_id=candidate.model_id,
                messages=messages,
                # 温度不再由我们决定：不传 = 不发这个字段，用上游默认值。
                temperature=None,
                max_tokens=pcfg.max_output_effective() if pcfg is not None else None,
                profile=profile,
                config_revision=config.revision,
                trace_id=trace_id,
                timeout_seconds=pcfg.timeout_seconds if pcfg is not None else 60.0,
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
                await self._record_usage(candidate.provider_id, provider, messages, outcome)
                return
            except asyncio.CancelledError:
                # 用户主动停止：不算 provider 故障
                raise
            except ModelError as exc:
                health.consecutive_failures += 1
                health.last_error = exc.message
                if health.consecutive_failures >= self.BREAKER_THRESHOLD:
                    health.opened_until = time.monotonic() + self.BREAKER_COOLDOWN_SECONDS
                    logger.warning(
                        "%s 连续失败 %d 次，熔断 %s 秒：这期间不再尝试它",
                        health_key,
                        health.consecutive_failures,
                        int(self.BREAKER_COOLDOWN_SECONDS),
                    )
                # **供应商失败必须留一行日志。**
                #
                # 原来这里只更新进程内的 health 记录：一次 401 / 模型名不存在
                # / 供应商 5xx 之后，日志里**一个字都没有**——运维看到的只有
                # 上游那句「轮次结束但没有可交付文本」。而 health 是内存态，
                # 重启即失，事后完全无法回溯。
                #
                # 只记 code 与供应商/模型，不记 messages 正文（那里面有用户的
                # 聊天内容）。``_breaker`` 打开的那一刻也顺带说明，否则
                # "为什么接下来 20 秒都直接失败"又是一条无从解释的现象。
                logger.warning(
                    "模型调用失败 provider=%s model=%s profile=%s code=%s retriable=%s：%s",
                    target.provider_id,
                    target.model_id,
                    profile,
                    exc.kind.value,
                    exc.retriable,
                    exc.message[:200],
                )
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
                    logger.warning(
                        "%s 连续失败 %d 次，熔断 %s 秒：这期间不再尝试它",
                        health_key,
                        health.consecutive_failures,
                        int(self.BREAKER_COOLDOWN_SECONDS),
                    )
                logger.warning(
                    "模型调用抛了非 ModelError 的异常 provider=%s model=%s profile=%s：%s",
                    target.provider_id,
                    target.model_id,
                    profile,
                    str(exc)[:200],
                )
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
