"""设置控制面：草稿 → 校验 → 发布 → 回滚。

唯一权威。环境变量只负责启动必需项，不能覆盖这里发布的修订。

不变式：
- 同一时刻只有一个 ``published`` 修订。
- 已发布修订不可变；改动一律新建草稿。
- 密钥只存密文 + 后四位 + 指纹；快照里只有 ``has_secret`` 与 ``secret_last4``。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ling.api.errors import LingError
from ling.contracts.v1 import ErrorCode, ModelRole
from ling.infra.models import ConfigRevision, ProviderCredential, utcnow
from ling.infra.security import SecretBox, new_id
from ling.models.registry import bootstrap_config
from ling.ports.config import (
    DialoguePolicy,
    EffectiveConfig,
    LifePolicy,
    MemoryPolicy,
    ProfileBinding,
    ProviderConfig,
    ProviderModel,
)

KNOWN_PROVIDER_KINDS = {"fake", "openai_compatible", "local"}
KNOWN_PROFILES = {role.value for role in ModelRole}

# 这些角色在设置页**不再展示**：绑定了也没有任何效果。
#
# 两类原因，不能混着说：
# - 无实现：reflection / planning / moderation.advisory 至今没有任何代码
#   请求它们。展示出来等于承诺一个不存在的能力。
# - 配置分裂：memory.embed / memory.rerank 的能力**是存在的**，但走
#   `build_embedding()` / `build_reranker()` 读环境变量（LING_EMBEDDING_*、
#   LING_RERANK_*），完全不经过模型绑定。在界面上绑一个模型给它，
#   不会有任何变化——而用户会以为生效了。
#
# `KNOWN_PROFILES` 仍然接受它们（兼容已存在的草稿），只是不再主动提供。
# 某个职责没绑定模型时，**具体会失去什么**。
#
# 为什么必须明说：系统不会因此报错，它会走降级路径——看起来一切正常，
# 只是能力悄悄变窄。图片就是活例子：perception.vision 没绑时媒体描述
# 始终为空，用户只会觉得"她看不见我发的图"，无从判断是没配模型。
#
# 注意这里混了两类，不能混着说：
# - 能力缺失（没绑就真的做不到）：vision
# - 质量降级（能工作，但用不到专用模型）：memory.extract（会退回
#   dialogue.fast 代跑）、dialogue.group（会退回 fast）
#
# 只列**有明确降级后果**的职责；没有列的角色缺绑定不会静默降级。
#
# 语音三项（speech.asr / speech.tts / realtime.voice）**不在这里**，
# 也不在可绑清单里：它们的绑定路径压根不存在。见 RESERVED_PROFILES。
PROFILE_DEGRADATIONS: dict[str, str] = {
    "memory.extract": (
        "提炼会改用主对话模型代跑（仍能工作，但换不了更适合提炼的模型）"
    ),
    "perception.vision": "看不到用户发来的图片内容，媒体描述始终为空",
    "dialogue.fast": "无法对话：这是主回复走的路由",
    "dialogue.group": "群聊没有可用的对话模型（会退回 fast）",
}

RESERVED_PROFILES: dict[str, str] = {
    "reflection": "尚无实现：目前没有任何代码请求这个角色",
    "planning": "尚无实现：目前没有任何代码请求这个角色",
    "moderation.advisory": "尚无实现：目前没有任何代码请求这个角色",
    # 语音三项：**入站链路是真的**（`POST /speech/segments` 落库，
    # `SpeechService.record_partial/record_final`），但**没有任何代码按
    # 模型职责去构造 provider**——`AsrProvider` / `TtsProvider` 全仓只有
    # `UnconfiguredSpeechProvider` 一个实现，`/v1/speech` 直接返回
    # `configured: False`。也就是说，在这个位置绑一个模型，不会有任何代码
    # 去读它。展示出来等于承诺"绑上就能听见/说话"，而实际零效果——
    # 比没有这个入口更糟：用户会以为自己配错了。
    # 等真的接上出站 TTS / ASR provider 时，把这三项挪回可绑清单。
    "speech.asr": "尚无实现：入站语音分段已落库，但没有按模型职责构造的 ASR provider",
    "speech.tts": "尚无实现：没有出站 TTS provider，绑定后不会有任何朗读",
    "realtime.voice": "尚无实现：没有实时语音会话实现，绑定后不会有任何效果",
    "memory.embed": "由环境变量配置（LING_EMBEDDING_*），不走模型绑定",
    "memory.rerank": "由环境变量配置（LING_RERANK_*），不走模型绑定",
}


@dataclass
class ValidationIssue:
    """配置校验问题。

    ``severity=error`` 阻断发布；``warning`` 只提示（例如供应商还没存密钥，
    发布后对话会给出可执行失败提示，而不是把整份配置锁死）。
    """

    field: str
    message: str
    severity: str = "error"

    @property
    def blocking(self) -> bool:
        return self.severity == "error"

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "message": self.message, "severity": self.severity}


@dataclass
class DraftResult:
    revision: int
    state: str
    validation_errors: list[ValidationIssue] = field(default_factory=list)


class ConfigService:
    def __init__(
        self, session: AsyncSession, secret_box: SecretBox, *, test_mode_default: bool = True,
        persona_id: str = "persona_ling", log_denials_default: bool = True
    ) -> None:
        self._session = session
        self._box = secret_box
        self._test_mode_default = test_mode_default
        self._persona_id = persona_id
        # `LING_LOG_ACCESS_DENIALS`。只在"没有已发布修订"或"修订里没有这个键"
        # （旧修订）时兜底；发布过带该键的修订之后以修订为准。
        self._log_denials_default = log_denials_default

    # ---------------------------------------------------------- 读取

    async def published_revision(self) -> ConfigRevision | None:
        return (
            await self._session.execute(
                select(ConfigRevision)
                .where(ConfigRevision.state == "published")
                .order_by(ConfigRevision.revision.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def effective(self) -> EffectiveConfig:
        """生效快照。没有已发布修订时返回引导配置（测试模式下绑定 fake）。"""
        published = await self.published_revision()
        if published is None:
            return bootstrap_config(
                test_mode=self._test_mode_default,
                persona_id=self._persona_id,
                log_access_denials=self._log_denials_default,
            )
        payload: dict = dict(published.payload or {})
        providers: dict[str, ProviderConfig] = {}
        for raw in list(payload.get("providers") or []):
            pid = raw.get("provider_id")
            if not pid:
                continue
            providers[pid] = ProviderConfig(
                provider_id=pid,
                kind=raw.get("kind", "fake"),
                base_url=raw.get("base_url"),
                has_secret=bool(raw.get("has_secret")),
                secret_last4=raw.get("secret_last4", ""),
                enabled=bool(raw.get("enabled", True)),
                extra=dict(raw.get("extra") or {}),
                models=_provider_models(raw.get("models")),
            )
        profiles: dict[str, ProfileBinding] = {}
        for raw in list(payload.get("profiles") or []):
            prof = raw.get("profile")
            if not prof:
                continue
            profiles[prof] = ProfileBinding(
                profile=prof,
                provider_id=raw.get("provider_id", ""),
                model_id=raw.get("model_id", ""),
                fallback_provider_id=raw.get("fallback_provider_id"),
                fallback_model_id=raw.get("fallback_model_id"),
                enabled=bool(raw.get("enabled", True)),
                max_tokens=raw.get("max_tokens"),
                temperature=float(raw.get("temperature", 0.8)),
                timeout_seconds=float(raw.get("timeout_seconds", 60.0)),
                daily_cost_limit=raw.get("daily_cost_limit"),
            )
        return EffectiveConfig(
            revision=published.revision,
            providers=providers,
            profiles=profiles,
            test_mode=bool(payload.get("test_mode", False)),
            # 旧修订没有这个键：退回环境变量给的默认值（默认 True），
            # 而不是把"没配过"当成"关掉"。
            log_access_denials=bool(
                payload.get("log_access_denials", self._log_denials_default)
            ),
            persona_id=payload.get("persona_id", self._persona_id),
            memory=_memory_policy(payload.get("memory")),
            life=_life_policy(payload.get("life")),
            dialogue=_dialogue_policy(payload.get("dialogue")),
        )

    async def list_revisions(self, limit: int = 20) -> list[ConfigRevision]:
        return list(
            (
                await self._session.execute(
                    select(ConfigRevision)
                    .order_by(ConfigRevision.revision.desc())
                    .limit(limit)
                )
            ).scalars()
        )

    # ---------------------------------------------------------- 写入

    async def save_draft(
        self,
        *,
        providers: list[dict] | None = None,
        profiles: list[dict] | None = None,
        test_mode: bool | None = None,
        log_access_denials: bool | None = None,
        memory: dict | None = None,
        life: dict | None = None,
        dialogue: dict | None = None,
        note: str | None = None,
        created_by: str | None = None,
    ) -> DraftResult:
        base = await self.effective()
        current_payload: dict = {
            "providers": [
                {
                    "provider_id": p.provider_id,
                    "kind": p.kind,
                    "base_url": p.base_url,
                    "has_secret": p.has_secret,
                    "secret_last4": p.secret_last4,
                    "enabled": p.enabled,
                    "extra": dict(p.extra),
                    "models": [m.to_dict() for m in p.models],
                }
                for p in base.providers.values()
            ],
            "profiles": [
                {
                    "profile": b.profile,
                    "provider_id": b.provider_id,
                    "model_id": b.model_id,
                    "fallback_provider_id": b.fallback_provider_id,
                    "fallback_model_id": b.fallback_model_id,
                    "enabled": b.enabled,
                    "max_tokens": b.max_tokens,
                    "temperature": b.temperature,
                    "timeout_seconds": b.timeout_seconds,
                    "daily_cost_limit": b.daily_cost_limit,
                }
                for b in base.profiles.values()
            ],
            "test_mode": base.test_mode,
            "log_access_denials": base.log_access_denials,
            "persona_id": base.persona_id,
            "memory": _policy_to_dict(base.memory),
            "life": _life_to_dict(base.life),
            "dialogue": _dialogue_to_dict(base.dialogue),
        }

        if memory is not None:
            current_payload["memory"] = _merge_memory(current_payload["memory"], memory)

        if life is not None:
            current_payload["life"] = _merge_fields(
                current_payload["life"], life, _LIFE_BOUNDS
            )

        if dialogue is not None:
            current_payload["dialogue"] = _merge_fields(
                current_payload["dialogue"], dialogue, _DIALOGUE_BOUNDS
            )

        if providers is not None:
            merged: dict[str, dict] = {p["provider_id"]: p for p in current_payload["providers"]}
            for raw in providers:
                pid = raw.get("provider_id")
                if not pid:
                    continue
                existing = merged.get(pid, {})
                # 模型清单：**没传就是"没改"**，不能把旧清单清空。
                # 旧版控制台 / 第三方客户端保存供应商时不会带 models，
                # 若按 pydantic 默认值 [] 处理，一次无关的保存就会把目录清掉。
                models = (
                    _clean_models(raw["models"])
                    if "models" in raw
                    else list(existing.get("models", []))
                )
                # 密钥字段不由前端传入；保留库中已知状态
                merged[pid] = {
                    "provider_id": pid,
                    "kind": raw.get("kind", existing.get("kind", "fake")),
                    "base_url": raw.get("base_url", existing.get("base_url")),
                    "has_secret": existing.get("has_secret", False),
                    "secret_last4": existing.get("secret_last4", ""),
                    "enabled": bool(raw.get("enabled", existing.get("enabled", True))),
                    "extra": dict(raw.get("extra", existing.get("extra", {}))),
                    "models": models,
                }
            current_payload["providers"] = list(merged.values())

        if profiles is not None:
            merged_profiles: dict[str, dict] = {
                p["profile"]: p for p in current_payload["profiles"]
            }
            for raw in profiles:
                prof = raw.get("profile")
                if not prof:
                    continue
                existing = merged_profiles.get(prof, {"profile": prof})
                merged_profiles[prof] = {
                    "profile": prof,
                    "provider_id": raw.get("provider_id", existing.get("provider_id", "")),
                    "model_id": raw.get("model_id", existing.get("model_id", "")),
                    "fallback_provider_id": raw.get(
                        "fallback_provider_id", existing.get("fallback_provider_id")
                    ),
                    "fallback_model_id": raw.get(
                        "fallback_model_id", existing.get("fallback_model_id")
                    ),
                    "enabled": bool(raw.get("enabled", existing.get("enabled", True))),
                    "max_tokens": raw.get("max_tokens", existing.get("max_tokens")),
                    "temperature": float(raw.get("temperature", existing.get("temperature", 0.8))),
                    "timeout_seconds": float(
                        raw.get("timeout_seconds", existing.get("timeout_seconds", 60.0))
                    ),
                    "daily_cost_limit": raw.get(
                        "daily_cost_limit", existing.get("daily_cost_limit")
                    ),
                }
            current_payload["profiles"] = list(merged_profiles.values())

        if test_mode is not None:
            current_payload["test_mode"] = test_mode

        if log_access_denials is not None:
            current_payload["log_access_denials"] = bool(log_access_denials)

        issues = await self.validate_payload(current_payload)
        next_rev = await self._next_revision_number()
        row = ConfigRevision(
            id=new_id("cfg"),
            revision=next_rev,
            state="draft",
            payload=current_payload,
            validation_errors=[i.to_dict() for i in issues],
            created_by=created_by,
            note=note,
        )
        self._session.add(row)
        await self._session.flush()
        return DraftResult(revision=row.revision, state=row.state, validation_errors=issues)

    async def validate_payload(self, payload: dict) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        providers = {p.get("provider_id"): p for p in payload.get("providers", [])}
        for pid, cfg in providers.items():
            kind = cfg.get("kind")
            if kind not in KNOWN_PROVIDER_KINDS:
                issues.append(ValidationIssue(f"providers.{pid}.kind", f"未知供应商类型: {kind}"))
                continue
            if kind in {"openai_compatible", "local"}:
                base_url = (cfg.get("base_url") or "").strip()
                if not base_url:
                    issues.append(
                        ValidationIssue(f"providers.{pid}.base_url", "兼容接口必须提供 base_url")
                    )
                elif not (base_url.startswith("http://") or base_url.startswith("https://")):
                    issues.append(
                        ValidationIssue(
                            f"providers.{pid}.base_url", "base_url 必须以 http:// 或 https:// 开头"
                        )
                    )
                if not cfg.get("has_secret"):
                    issues.append(
                        ValidationIssue(
                            f"providers.{pid}.secret",
                            "该供应商还没有保存密钥，发布后调用会返回 secret_missing",
                            severity="warning",
                        )
                    )

            # 模型清单：空 id 是错误（前端不会提交，但 API 可能）；
            # 重复 id 在 `_clean_models` 里已按"最后一条生效"归一化，
            # 所以这里只需要拦住空值。
            models = cfg.get("models") or []
            if not isinstance(models, list):
                issues.append(
                    ValidationIssue(f"providers.{pid}.models", "models 必须是数组")
                )
            else:
                for index, model in enumerate(models):
                    if not isinstance(model, dict) or not str(
                        model.get("model_id", "")
                    ).strip():
                        issues.append(
                            ValidationIssue(
                                f"providers.{pid}.models[{index}].model_id",
                                "模型 ID 不能为空",
                            )
                        )

        # 同一时刻只启用一家：这是设置页的默认心智模型（另一家留着备用，
        # 而不是同时分流）。做成 warning 而不是 error，是因为跨供应商回退
        # 需要两家都启用；直接阻断会让已有配置无法再保存。
        enabled_providers = [
            pid for pid, cfg in providers.items() if cfg.get("enabled", True)
        ]
        if len(enabled_providers) > 1:
            issues.append(
                ValidationIssue(
                    "providers",
                    f"同时启用了 {len(enabled_providers)} 家供应商"
                    f"（{'、'.join(enabled_providers)}）。同一时刻只建议启用一家；"
                    "只有在配置跨供应商回退时才需要多家同时启用。",
                    severity="warning",
                )
            )

        seen_profiles: set[str] = set()
        for raw in payload.get("profiles", []):
            prof = raw.get("profile", "")
            if prof not in KNOWN_PROFILES:
                issues.append(ValidationIssue(f"profiles.{prof}", f"未知职责: {prof}"))
                continue
            if prof in seen_profiles:
                issues.append(ValidationIssue(f"profiles.{prof}", "同一职责只能绑定一次"))
            seen_profiles.add(prof)
            pid = raw.get("provider_id")
            if not pid or pid not in providers:
                issues.append(
                    ValidationIssue(f"profiles.{prof}.provider_id", f"未登记的 provider: {pid}")
                )
                continue
            if not payload.get("test_mode") and providers[pid].get("kind") == "fake":
                issues.append(
                    ValidationIssue(
                        f"profiles.{prof}.provider_id",
                        "非测试模式下不能把职责绑定到 fake provider（会冒充模型能力）",
                    )
                )
            # 停用 ≠ 解绑：旧绑定还在，调用会失败。发布时不拦（用户可能
            # 只是临时停用再切回来），但必须在设置页说清"需要检查"。
            if not providers[pid].get("enabled", True):
                issues.append(
                    ValidationIssue(
                        f"profiles.{prof}.provider_id",
                        f"供应商 {pid} 已停用，这个职责调用时会失败",
                        severity="warning",
                    )
                )
            registered = {
                str(m.get("model_id", "")).strip(): m
                for m in providers[pid].get("models") or []
                if isinstance(m, dict)
            }
            bound_model = (raw.get("model_id") or "").strip()
            if bound_model and bound_model in registered and not registered[
                bound_model
            ].get("enabled", True):
                issues.append(
                    ValidationIssue(
                        f"profiles.{prof}.model_id",
                        f"模型 {bound_model} 已在 {pid} 的清单里停用，这个职责仍指向它",
                        severity="warning",
                    )
                )
            if not (raw.get("model_id") or "").strip():
                issues.append(ValidationIssue(f"profiles.{prof}.model_id", "必须填写模型 ID"))
            temp = raw.get("temperature", 0.8)
            try:
                temp_val = float(temp)
            except (TypeError, ValueError):
                issues.append(ValidationIssue(f"profiles.{prof}.temperature", "temperature 必须是数字"))
            else:
                if not 0.0 <= temp_val <= 2.0:
                    issues.append(
                        ValidationIssue(f"profiles.{prof}.temperature", "temperature 必须在 0~2 之间")
                    )
            timeout = raw.get("timeout_seconds", 60.0)
            try:
                timeout_val = float(timeout)
            except (TypeError, ValueError):
                issues.append(
                    ValidationIssue(f"profiles.{prof}.timeout_seconds", "timeout_seconds 必须是数字")
                )
            else:
                if not 1.0 <= timeout_val <= 600.0:
                    issues.append(
                        ValidationIssue(
                            f"profiles.{prof}.timeout_seconds", "timeout_seconds 必须在 1~600 秒之间"
                        )
                    )
            fb_pid = raw.get("fallback_provider_id")
            if fb_pid:
                if fb_pid not in providers:
                    issues.append(
                        ValidationIssue(
                            f"profiles.{prof}.fallback_provider_id",
                            f"回退 provider 未登记: {fb_pid}",
                        )
                    )
                elif not payload.get("test_mode") and providers[fb_pid].get("kind") == "fake":
                    issues.append(
                        ValidationIssue(
                            f"profiles.{prof}.fallback_provider_id",
                            "非测试模式下不能用 fake provider 作为回退（会冒充模型能力）",
                        )
                    )
                elif not providers[fb_pid].get("enabled", True):
                    issues.append(
                        ValidationIssue(
                            f"profiles.{prof}.fallback_provider_id",
                            f"回退供应商 {fb_pid} 已停用，回退不会生效",
                            severity="warning",
                        )
                    )
                if not (raw.get("fallback_model_id") or "").strip():
                    issues.append(
                        ValidationIssue(
                            f"profiles.{prof}.fallback_model_id", "配置了回退 provider 就必须给回退模型"
                        )
                    )

        if not payload.get("test_mode") and "dialogue.fast" not in seen_profiles:
            issues.append(
                ValidationIssue("profiles.dialogue.fast", "非测试模式下必须绑定 dialogue.fast")
            )
        return issues

    async def publish(self, revision: int, *, published_by: str | None = None) -> int:
        row = (
            await self._session.execute(
                select(ConfigRevision).where(ConfigRevision.revision == revision)
            )
        ).scalar_one_or_none()
        if row is None:
            raise LingError(ErrorCode.not_found, f"修订 {revision} 不存在", status_code=404)
        issues = await self.validate_payload(dict(row.payload or {}))
        blocking = [i for i in issues if i.blocking]
        row.validation_errors = [i.to_dict() for i in issues]
        if blocking:
            await self._session.flush()
            raise LingError(
                ErrorCode.config_invalid,
                "配置校验未通过，未发布",
                status_code=422,
                detail="; ".join(f"{i.field}: {i.message}" for i in blocking),
            )
        await self._session.execute(
            update(ConfigRevision)
            .where(ConfigRevision.state == "published")
            .values(state="superseded")
        )
        row.state = "published"
        row.published_at = utcnow()
        row.validation_errors = []
        if published_by:
            row.created_by = row.created_by or published_by
        await self._session.flush()
        return row.revision

    async def rollback(self, to_revision: int) -> int:
        """回滚 = 把历史修订重新发布为当前生效（不复制 payload 内容）。"""
        return await self.publish(to_revision)

    async def _next_revision_number(self) -> int:
        latest = (
            await self._session.execute(
                select(ConfigRevision).order_by(ConfigRevision.revision.desc()).limit(1)
            )
        ).scalar_one_or_none()
        return int(latest.revision) + 1 if latest else 1

    # ---------------------------------------------------------- 密钥

    async def set_secret(self, provider_id: str, secret: str) -> dict[str, str]:
        if not provider_id:
            raise LingError(ErrorCode.invalid_request, "provider_id 不能为空", status_code=422)
        secret = secret.strip()
        if not secret:
            raise LingError(ErrorCode.invalid_request, "密钥不能为空", status_code=422)
        row = (
            await self._session.execute(
                select(ProviderCredential).where(ProviderCredential.provider_id == provider_id)
            )
        ).scalar_one_or_none()
        ciphertext = self._box.encrypt(secret)
        last4 = SecretBox.last4(secret)
        fingerprint = SecretBox.fingerprint(secret)
        if row is None:
            row = ProviderCredential(
                id=new_id("cred"),
                provider_id=provider_id,
                ciphertext=ciphertext,
                last4=last4,
                fingerprint=fingerprint,
            )
            self._session.add(row)
        else:
            row.ciphertext = ciphertext
            row.last4 = last4
            row.fingerprint = fingerprint
            row.updated_at = utcnow()
        await self._session.flush()
        await self._sync_secret_flags(provider_id, has_secret=True, last4=last4)
        await self._bump_for_secret_change(provider_id, note=f"密钥变更：{provider_id}")
        return {"last4": last4, "fingerprint": fingerprint}

    async def delete_secret(self, provider_id: str) -> None:
        row = (
            await self._session.execute(
                select(ProviderCredential).where(ProviderCredential.provider_id == provider_id)
            )
        ).scalar_one_or_none()
        if row is not None:
            await self._session.delete(row)
            await self._session.flush()
        await self._sync_secret_flags(provider_id, has_secret=False, last4="")
        await self._bump_for_secret_change(provider_id, note=f"密钥删除：{provider_id}")

    async def _bump_for_secret_change(self, provider_id: str, *, note: str) -> int | None:
        """密钥变更后**发布一个新修订**，好让其他进程能感知到。

        为什么必须升修订号，而不是只写凭据表：跨进程传播的唯一信号就是
        ``refresh.current_revision()`` 的返回值——API 的 ``RevisionWatcher``
        与 worker 的 ``refresh_memory_router_if_stale()`` 都比对修订号。
        只 upsert 凭据表的话，别的副本与 worker 永远认为"配置没变"，继续
        用旧密钥：表现是换密钥后一部分请求失败、worker 的记忆提炼静默退回
        正则（用户看到的是"它有时能聊有时不能，还记不住话"）。

        为什么选"新建已发布修订"而不是"在 ``stale()`` 里比对密钥指纹"：
        指纹方案要改 ``RevisionWatcher`` 的调用约定（``mark_loaded`` 需要
        拿到当时的指纹，调用点在 ``app.py`` / ``chat.py``），而且 worker
        那条路径根本不用 ``RevisionWatcher``，它只看修订号——指纹方案对
        worker 完全无效。修订号是两条路径共用的唯一信号，改在源头最省。

        代价：每次换密钥会多出一条"内容与上一版相同"的修订。这是有意的——
        修订历史里留下"谁在何时换了密钥"是可审计的事实，不是噪音。
        ``note`` 写明是密钥变更，避免运维以为是用户改了配置。

        没有已发布修订时**不造修订**：此时进程都跑在引导配置上，没有生效
        配置可改，凭空发布一份反而会把 ``test_mode`` 等引导行为固化下来。
        """
        published = await self.published_revision()
        if published is None:
            return None
        payload = copy.deepcopy(dict(published.payload or {}))
        next_rev = await self._next_revision_number()
        # 先降级旧的，再插入新的——顺序与 ``publish()`` 一致。反过来写的话，
        # ``execute()`` 的自动 flush 会把新行按 ``published`` 落库，
        # 紧接着的 update 又会把它自己降级成 superseded。
        await self._session.execute(
            update(ConfigRevision)
            .where(ConfigRevision.state == "published")
            .values(state="superseded")
        )
        row = ConfigRevision(
            id=new_id("cfg"),
            revision=next_rev,
            state="published",
            payload=payload,
            validation_errors=[],
            published_at=utcnow(),
            note=note,
        )
        self._session.add(row)
        await self._session.flush()
        return next_rev

    async def read_secret(self, provider_id: str) -> str | None:
        row = (
            await self._session.execute(
                select(ProviderCredential).where(ProviderCredential.provider_id == provider_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        try:
            return self._box.decrypt(row.ciphertext)
        except Exception:  # noqa: BLE001 - 根密钥变化后旧密文不可解
            return None

    async def secret_diagnosis(self, provider_id: str) -> str:
        """密钥状态：``ok`` / ``missing`` / ``undecryptable``。

        为什么要区分：两者都让 ``read_secret`` 返回 None，但原因完全不同。
        - ``missing``：从来没存过 → "去设置里填密钥"
        - ``undecryptable``：存过但解不开（根密钥变了，通常是数据目录被换过）
          → "密钥还在，但根密钥不匹配"，让用户去查 root.key 而不是重复填密钥

        不区分的话，第二种会显示成"还没有保存密钥"——用户明明存过，
        会被这句话带偏，反复重填也修不好。
        """
        row = (
            await self._session.execute(
                select(ProviderCredential).where(
                    ProviderCredential.provider_id == provider_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return "missing"
        try:
            self._box.decrypt(row.ciphertext)
        except Exception:  # noqa: BLE001 - 根密钥变化后旧密文不可解
            return "undecryptable"
        return "ok"

    async def secret_status(self, provider_id: str) -> dict[str, object]:
        row = (
            await self._session.execute(
                select(ProviderCredential).where(ProviderCredential.provider_id == provider_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return {"has_secret": False, "last4": "", "fingerprint": ""}
        return {"has_secret": True, "last4": row.last4, "fingerprint": row.fingerprint}

    async def _sync_secret_flags(self, provider_id: str, *, has_secret: bool, last4: str) -> None:
        """把密钥状态同步进当前草稿与已发布修订的 provider 描述（不含密文）。

        必须**深拷贝 + 重建内层结构**，浅拷贝在这里是静默失效的：
        ``ConfigRevision.payload`` 是普通 ``JSON`` 列，**没有** ``MutableDict``，
        SQLAlchemy 判"变没变"看的是"有没有被赋新对象 + 内容是否相等"。
        ``dict(row.payload)`` 只换了外层 dict，``providers`` 列表里的内层
        ``dict`` 还是原来那些对象；原地改它们之后，新赋的 ``payload`` 与
        原值内容"相等" → **不产生 UPDATE**。

        后果全是静默的：``has_secret`` / ``secret_last4`` 永远停在旧值，
        ``validate_payload`` 的"还没保存密钥"warning 永远为真，前端"已配置/
        未配置"状态错误，而且 ``_bump_for_secret_change`` deepcopy 的是这份
        没同步进去的 payload——密钥变更造出的新已发布修订里标记同样是错的
        （而其他副本与 worker 读的恰恰是那一版）。

        所以这里用 ``copy.deepcopy`` 拿到全新的内层 dict，并给 ``row.payload``
        赋一个**全新的顶层对象**。
        """
        rows = list(
            (await self._session.execute(select(ConfigRevision))).scalars()
        )
        for row in rows:
            payload = copy.deepcopy(dict(row.payload or {}))
            providers = payload.get("providers") or []
            changed = False
            for entry in providers:
                if entry.get("provider_id") == provider_id:
                    entry["has_secret"] = has_secret
                    entry["secret_last4"] = last4
                    changed = True
            if changed:
                payload["providers"] = providers
                row.payload = payload
        await self._session.flush()


__all__ = [
    "ConfigService",
    "DraftResult",
    "KNOWN_PROFILES",
    "PROFILE_DEGRADATIONS",
    "RESERVED_PROFILES",
    "ValidationIssue",
]


# ------------------------------------------------------------------ 记忆策略

# 读的时候一律夹到合法区间：库里出现脏值（或旧修订没有这个键）时，
# 记忆检索不能因此崩掉或退化成"什么都召回"。
_MEMORY_BOUNDS: dict[str, tuple[float, float]] = {
    "relative_score_floor": (0.0, 1.0),
    "rerank_top_n": (1, 100),
    "recency_half_life_days": (1.0, 3650.0),
    "max_retention_days": (1, 3650),
}


# 对话节奏的取值范围。同样单独一张表。
_DIALOGUE_BOUNDS: dict[str, tuple[float, float]] = {
    "group_turn_budget": (1, 20),
    "proactive_daily_limit": (0, 20),
    "quiet_start_minute": (0, 1439),
    "quiet_end_minute": (0, 1439),
    # 自主度开关是布尔，但现有钳制机制是数值区间。用 0/1 表示，
    # 避免为两个布尔再写一套读写分支。
    "private_may_stay_silent": (0, 1),
    "group_mention_requires_reply": (0, 1),
    "rest_may_skip": (0, 1),
    "group_attention_deferred": (0, 1),
    "proactive_delivery_enabled": (0, 1),
    # 群聊「决定回了」是否真的发出去。同样是 0/1 的布尔。
    "group_reply_delivery_enabled": (0, 1),
}

# 生活节奏的取值范围。与记忆策略分开一张表，避免两类的键名撞车。
_LIFE_BOUNDS: dict[str, tuple[float, float]] = {
    "scene_ttl_minutes": (1, 1440),
    "energy_decay_per_hour": (0.0, 100.0),
    "social_load_per_turn": (0.0, 100.0),
    "social_load_decay_per_hour": (0.0, 100.0),
    "max_live_plans": (1, 20),
}


def _clean_models(raw: object) -> list[dict[str, object]]:
    """把模型清单归一化成可以写进修订 payload 的形状。

    规则（都是为了让"同一份清单"在前后端是同一个东西）：

    - 兼容旧客户端直接传 ``["m1", "m2"]``；
    - ``model_id`` / ``note`` 去首尾空白，note 最多 200 字；
    - 同一个 id 只留最后一条——重名不会变成两行幽灵条目；
    - ``enabled`` 只认显式的 ``false``，其余都当启用；
    - 空 id **保留**，交给 ``validate_payload`` 报错，不静默吞掉。
    """
    if not isinstance(raw, list):
        return []
    cleaned: list[dict[str, object]] = []
    index: dict[str, int] = {}
    for item in raw:
        if isinstance(item, str):
            item = {"model_id": item}
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("model_id", "") or "").strip()
        entry: dict[str, object] = {
            "model_id": model_id,
            "note": str(item.get("note", "") or "").strip()[:200],
            "enabled": item.get("enabled", True) is not False,
        }
        if model_id and model_id in index:
            cleaned[index[model_id]] = entry
        elif model_id:
            index[model_id] = len(cleaned)
            cleaned.append(entry)
        else:
            cleaned.append(entry)
    return cleaned


def _provider_models(raw: object) -> tuple[ProviderModel, ...]:
    """修订 payload → 生效快照里的模型清单（空 id 不进入运行期快照）。"""
    return tuple(
        ProviderModel(
            model_id=str(item["model_id"]),
            note=str(item.get("note", "")),
            enabled=bool(item.get("enabled", True)),
        )
        for item in _clean_models(raw)
        if item["model_id"]
    )


def _clamp_in(
    bounds: dict[str, tuple[float, float]], name: str, value: object, default: float
) -> float:
    low, high = bounds[name]
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return min(max(num, low), high)


def _clamp(name: str, value: object, default: float) -> float:
    return _clamp_in(_MEMORY_BOUNDS, name, value, default)


def _memory_policy(raw: object) -> MemoryPolicy:
    """从修订 payload 读出记忆策略。缺键 → 默认值（行为与抽取前一致）。"""
    data = raw if isinstance(raw, dict) else {}
    defaults = MemoryPolicy()
    return MemoryPolicy(
        relative_score_floor=_clamp(
            "relative_score_floor",
            data.get("relative_score_floor"),
            defaults.relative_score_floor,
        ),
        rerank_top_n=int(
            _clamp("rerank_top_n", data.get("rerank_top_n"), defaults.rerank_top_n)
        ),
        recency_half_life_days=_clamp(
            "recency_half_life_days",
            data.get("recency_half_life_days"),
            defaults.recency_half_life_days,
        ),
        max_retention_days=int(
            _clamp(
                "max_retention_days",
                data.get("max_retention_days"),
                defaults.max_retention_days,
            )
        ),
    )


def _policy_to_dict(policy: MemoryPolicy) -> dict[str, float | int]:
    return {
        "relative_score_floor": policy.relative_score_floor,
        "rerank_top_n": policy.rerank_top_n,
        "recency_half_life_days": policy.recency_half_life_days,
        "max_retention_days": policy.max_retention_days,
    }


def _merge_memory(current: object, patch: dict) -> dict:
    return _merge_fields(current, patch, _MEMORY_BOUNDS)


async def published_memory_policy(session: AsyncSession) -> MemoryPolicy:
    """已发布修订里的 ``memory`` 段（没有已发布修订 → 代码默认值）。

    给**没有 ``EffectiveConfig`` 可用**的进程内路径用：worker 的
    ``memory.extract`` / ``memory.archive`` 每个 outbox 事件都是一次独立事务，
    没有一个"轮次级快照"可以继承，所以它们在处理事件时**现读一次**最新
    已发布修订——发布后下一个事件就生效，不需要重启、也没有进程内缓存
    需要失效（每个进程各自查库，见 ``docs/SETTINGS-TAXONOMY.md`` ④）。

    会话内核（``ContextCompiler``）**不用**这个函数：它从本轮
    ``EffectiveConfig.memory`` 拿快照，保证一轮之内策略不漂移。
    """
    revision = (
        await session.execute(
            select(ConfigRevision)
            .where(ConfigRevision.state == "published")
            .order_by(ConfigRevision.revision.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if revision is None:
        return MemoryPolicy()
    return _memory_policy(dict(revision.payload or {}).get("memory"))


# ------------------------------------------------------------------ 生活策略


def _life_policy(raw: object) -> LifePolicy:
    """从修订 payload 读出生活节奏。缺键 → 默认值（行为与抽取前一致）。"""
    data = raw if isinstance(raw, dict) else {}
    d = LifePolicy()
    return LifePolicy(
        scene_ttl_minutes=int(
            _clamp_in(_LIFE_BOUNDS, "scene_ttl_minutes", data.get("scene_ttl_minutes"), d.scene_ttl_minutes)
        ),
        energy_decay_per_hour=_clamp_in(
            _LIFE_BOUNDS, "energy_decay_per_hour", data.get("energy_decay_per_hour"), d.energy_decay_per_hour
        ),
        social_load_per_turn=_clamp_in(
            _LIFE_BOUNDS, "social_load_per_turn", data.get("social_load_per_turn"), d.social_load_per_turn
        ),
        social_load_decay_per_hour=_clamp_in(
            _LIFE_BOUNDS,
            "social_load_decay_per_hour",
            data.get("social_load_decay_per_hour"),
            d.social_load_decay_per_hour,
        ),
        max_live_plans=int(
            _clamp_in(_LIFE_BOUNDS, "max_live_plans", data.get("max_live_plans"), d.max_live_plans)
        ),
    )


def _life_to_dict(policy: LifePolicy) -> dict[str, float | int]:
    return {
        "scene_ttl_minutes": policy.scene_ttl_minutes,
        "energy_decay_per_hour": policy.energy_decay_per_hour,
        "social_load_per_turn": policy.social_load_per_turn,
        "social_load_decay_per_hour": policy.social_load_decay_per_hour,
        "max_live_plans": policy.max_live_plans,
    }


def _merge_fields(current: object, patch: dict, bounds: dict[str, tuple[float, float]]) -> dict:
    """只覆盖显式给出的字段。不传的字段保持原值——不是重置成默认。"""
    base = current if isinstance(current, dict) else {}
    merged = dict(base)
    for key in bounds:
        if key in patch and patch[key] is not None:
            merged[key] = patch[key]
    return merged


# ------------------------------------------------------------------ 对话策略


def _dialogue_policy(raw: object) -> DialoguePolicy:
    """从修订 payload 读出对话节奏与主动门槛。缺键 → 默认值。"""
    data = raw if isinstance(raw, dict) else {}
    d = DialoguePolicy()
    return DialoguePolicy(
        group_turn_budget=int(
            _clamp_in(
                _DIALOGUE_BOUNDS,
                "group_turn_budget",
                data.get("group_turn_budget"),
                d.group_turn_budget,
            )
        ),
        proactive_daily_limit=int(
            _clamp_in(
                _DIALOGUE_BOUNDS,
                "proactive_daily_limit",
                data.get("proactive_daily_limit"),
                d.proactive_daily_limit,
            )
        ),
        quiet_start_minute=int(
            _clamp_in(
                _DIALOGUE_BOUNDS,
                "quiet_start_minute",
                data.get("quiet_start_minute"),
                d.quiet_start_minute,
            )
        ),
        quiet_end_minute=int(
            _clamp_in(
                _DIALOGUE_BOUNDS,
                "quiet_end_minute",
                data.get("quiet_end_minute"),
                d.quiet_end_minute,
            )
        ),
        private_may_stay_silent=bool(
            int(
                _clamp_in(
                    _DIALOGUE_BOUNDS,
                    "private_may_stay_silent",
                    data.get("private_may_stay_silent"),
                    int(d.private_may_stay_silent),
                )
            )
        ),
        group_mention_requires_reply=bool(
            int(
                _clamp_in(
                    _DIALOGUE_BOUNDS,
                    "group_mention_requires_reply",
                    data.get("group_mention_requires_reply"),
                    int(d.group_mention_requires_reply),
                )
            )
        ),
        rest_may_skip=bool(
            int(
                _clamp_in(
                    _DIALOGUE_BOUNDS,
                    "rest_may_skip",
                    data.get("rest_may_skip"),
                    int(d.rest_may_skip),
                )
            )
        ),
        group_attention_deferred=bool(
            int(
                _clamp_in(
                    _DIALOGUE_BOUNDS,
                    "group_attention_deferred",
                    data.get("group_attention_deferred"),
                    int(d.group_attention_deferred),
                )
            )
        ),
        proactive_delivery_enabled=bool(
            int(
                _clamp_in(
                    _DIALOGUE_BOUNDS,
                    "proactive_delivery_enabled",
                    data.get("proactive_delivery_enabled"),
                    int(d.proactive_delivery_enabled),
                )
            )
        ),
        group_reply_delivery_enabled=bool(
            int(
                _clamp_in(
                    _DIALOGUE_BOUNDS,
                    "group_reply_delivery_enabled",
                    data.get("group_reply_delivery_enabled"),
                    int(d.group_reply_delivery_enabled),
                )
            )
        ),
    )


def _dialogue_to_dict(policy: DialoguePolicy) -> dict[str, int | bool]:
    return {
        "group_turn_budget": policy.group_turn_budget,
        "proactive_daily_limit": policy.proactive_daily_limit,
        "quiet_start_minute": policy.quiet_start_minute,
        "quiet_end_minute": policy.quiet_end_minute,
        "private_may_stay_silent": policy.private_may_stay_silent,
        "group_mention_requires_reply": policy.group_mention_requires_reply,
        "rest_may_skip": policy.rest_may_skip,
        "group_attention_deferred": policy.group_attention_deferred,
        "proactive_delivery_enabled": policy.proactive_delivery_enabled,
        "group_reply_delivery_enabled": policy.group_reply_delivery_enabled,
    }
