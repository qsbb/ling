"""设置路由：统一模型设置入口（供应商、密钥、职责绑定、发布、回滚、探测）。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ling.api.deps import ConfigDep, CurrentAuth, RouterDep
from ling.api.errors import LingError
from ling.contracts.v1 import ErrorCode
from ling.settings.service import PROFILE_DEGRADATIONS, RESERVED_PROFILES

router = APIRouter(tags=["settings"])

# 设置页展示的角色，按人读的顺序（不是字母序）。
# 只列**绑定后真的会生效**的：保留角色见 RESERVED_PROFILES，
# 它们绑定了没有任何效果，展示出来等于承诺一个不存在的能力。
_OFFERABLE_PROFILES: tuple[str, ...] = (
    "dialogue.fast",
    "dialogue.deep",
    "dialogue.group",
    "perception.vision",
    "speech.asr",
    "speech.tts",
    "realtime.voice",
    "memory.extract",
)


class ProviderIn(BaseModel):
    provider_id: str = Field(min_length=1, max_length=64)
    kind: str = "fake"
    base_url: str | None = None
    enabled: bool = True
    extra: dict = Field(default_factory=dict)


class ProfileIn(BaseModel):
    profile: str
    provider_id: str
    model_id: str
    fallback_provider_id: str | None = None
    fallback_model_id: str | None = None
    enabled: bool = True
    temperature: float = 0.8
    max_tokens: int | None = None
    timeout_seconds: float = 60.0
    daily_cost_limit: float | None = None


class MemoryPolicyIn(BaseModel):
    """④ 记忆与学习 的策略。范围校验两处：这里拒掉明显非法的输入，
    读取时再夹一次（库里可能有旧修订或手工改过的值）。"""

    relative_score_floor: float | None = Field(default=None, ge=0.0, le=1.0)
    rerank_top_n: int | None = Field(default=None, ge=1, le=100)
    recency_half_life_days: float | None = Field(default=None, gt=0, le=3650)
    max_retention_days: int | None = Field(default=None, ge=1, le=3650)


class LifePolicyIn(BaseModel):
    """⑧ 生活与活动 的策略。不含作息——那在 actor_routines 表里按 actor 存。"""

    scene_ttl_minutes: int | None = Field(default=None, ge=1, le=1440)
    energy_decay_per_hour: float | None = Field(default=None, ge=0, le=100)
    social_load_per_turn: float | None = Field(default=None, ge=0, le=100)
    social_load_decay_per_hour: float | None = Field(default=None, ge=0, le=100)
    max_live_plans: int | None = Field(default=None, ge=1, le=20)


class DialoguePolicyIn(BaseModel):
    """⑤ 对话与主动性 的策略。免打扰用"当天分钟数"，因为跨午夜是常态。"""

    group_turn_budget: int | None = Field(default=None, ge=1, le=20)
    proactive_daily_limit: int | None = Field(default=None, ge=0, le=20)
    quiet_start_minute: int | None = Field(default=None, ge=0, le=1439)
    quiet_end_minute: int | None = Field(default=None, ge=0, le=1439)
    # 自主度开关：她能否自己决定不回
    private_may_stay_silent: bool | None = None
    group_mention_requires_reply: bool | None = None
    rest_may_skip: bool | None = None
    group_attention_deferred: bool | None = None
    proactive_delivery_enabled: bool | None = None


class DraftBody(BaseModel):
    providers: list[ProviderIn] | None = None
    profiles: list[ProfileIn] | None = None
    test_mode: bool | None = None
    memory: MemoryPolicyIn | None = None
    life: LifePolicyIn | None = None
    dialogue: DialoguePolicyIn | None = None
    note: str | None = None


class PublishBody(BaseModel):
    revision: int


class RollbackBody(BaseModel):
    to_revision: int


class SecretBody(BaseModel):
    secret: str = Field(min_length=1, max_length=4096)


@router.get("/settings/taxonomy")
async def get_taxonomy(auth: CurrentAuth) -> dict:
    """设置分类法。前端按这里渲染导航，不要在客户端再抄一份类名。

    ``available=false`` 表示这一类还没有设置面——前端**不应**为它渲染入口，
    否则用户点进去只会看到空页面。等实现后把 ``editable_route`` 填上即可。
    """
    from ling.settings.taxonomy import CATEGORIES, summary

    return {
        "categories": [
            {
                "index": c.index,
                "key": c.key,
                "label": c.label,
                "verb": c.verb,
                "boundary": c.boundary,
                "available": c.available,
                "editable_route": c.editable_route,
                "status_route": c.status_route,
                "groups": [
                    {
                        "key": g.key,
                        "label": g.label,
                        "defaults_to": g.defaults_to.value,
                    }
                    for g in c.groups
                ],
            }
            for c in CATEGORIES
        ],
        "summary": summary(),
    }


@router.get("/settings/config")
async def get_config(auth: CurrentAuth, config: ConfigDep) -> dict:
    effective = await config.effective()
    revisions = await config.list_revisions(limit=30)
    providers = []
    for p in effective.providers.values():
        status = await config.secret_status(p.provider_id)
        providers.append(
            {
                "provider_id": p.provider_id,
                "kind": p.kind,
                "base_url": p.base_url,
                "enabled": p.enabled,
                "has_secret": bool(status["has_secret"]),
                "secret_last4": status["last4"],
                "secret_fingerprint": status["fingerprint"],
                "extra": p.extra,
            }
        )
    return {
        "schema_version": "v1",
        "revision": effective.revision,
        "test_mode": effective.test_mode,
        "providers": providers,
        "memory": {
            "relative_score_floor": effective.memory.relative_score_floor,
            "rerank_top_n": effective.memory.rerank_top_n,
            "recency_half_life_days": effective.memory.recency_half_life_days,
            "max_retention_days": effective.memory.max_retention_days,
        },
        "life": {
            "scene_ttl_minutes": effective.life.scene_ttl_minutes,
            "energy_decay_per_hour": effective.life.energy_decay_per_hour,
            "social_load_per_turn": effective.life.social_load_per_turn,
            "social_load_decay_per_hour": effective.life.social_load_decay_per_hour,
            "max_live_plans": effective.life.max_live_plans,
        },
        "dialogue": {
            "group_turn_budget": effective.dialogue.group_turn_budget,
            "proactive_daily_limit": effective.dialogue.proactive_daily_limit,
            "quiet_start_minute": effective.dialogue.quiet_start_minute,
            "quiet_end_minute": effective.dialogue.quiet_end_minute,
            "private_may_stay_silent": effective.dialogue.private_may_stay_silent,
            "group_mention_requires_reply": effective.dialogue.group_mention_requires_reply,
            "rest_may_skip": effective.dialogue.rest_may_skip,
            "group_attention_deferred": effective.dialogue.group_attention_deferred,
            "proactive_delivery_enabled": effective.dialogue.proactive_delivery_enabled,
        },
        "profiles": [
            {
                "profile": b.profile,
                "provider_id": b.provider_id,
                "model_id": b.model_id,
                "fallback_provider_id": b.fallback_provider_id,
                "fallback_model_id": b.fallback_model_id,
                "enabled": b.enabled,
                "temperature": b.temperature,
                "max_tokens": b.max_tokens,
                "timeout_seconds": b.timeout_seconds,
                "daily_cost_limit": b.daily_cost_limit,
            }
            for b in effective.profiles.values()
        ],
        "revisions": [
            {
                "revision": r.revision,
                "state": r.state,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "published_at": r.published_at.isoformat() if r.published_at else None,
                "note": r.note,
                "validation_errors": r.validation_errors or [],
            }
            for r in revisions
        ],
        "known_kinds": ["fake", "openai_compatible", "local"],
        # 只列出**绑定后真的会生效**的角色。
        # 保留角色见 settings/service.py::RESERVED_PROFILES，
        # 它们绑定了没有任何效果，展示出来等于承诺不存在的能力。
        "known_profiles": [p for p in _OFFERABLE_PROFILES],
        "reserved_profiles": dict(RESERVED_PROFILES),
        # **未绑定职责会失去什么**。系统不会因此报错，它会走降级路径——
        # 看起来一切正常，只是能力悄悄变窄。用户只会觉得"她好像记不住事"，
        # 无从判断是没配模型。
        "unbound_degradations": {
            profile: PROFILE_DEGRADATIONS[profile]
            for profile in _OFFERABLE_PROFILES
            if profile in PROFILE_DEGRADATIONS
            and not (effective.profiles.get(profile) and effective.profiles[profile].enabled)
        },
    }


@router.put("/settings/draft")
async def save_draft(
    body: DraftBody, auth: CurrentAuth, config: ConfigDep
) -> dict[str, object]:
    result = await config.save_draft(
        providers=[p.model_dump() for p in body.providers] if body.providers else None,
        profiles=[p.model_dump() for p in body.profiles] if body.profiles else None,
        test_mode=body.test_mode,
        memory=body.memory.model_dump(exclude_none=True) if body.memory else None,
        life=body.life.model_dump(exclude_none=True) if body.life else None,
        dialogue=(
            body.dialogue.model_dump(exclude_none=True) if body.dialogue else None
        ),
        note=body.note,
        created_by=auth.user.id,
    )
    return {
        "revision": result.revision,
        "state": result.state,
        "validation_errors": [i.to_dict() for i in result.validation_errors],
    }


@router.post("/settings/publish")
async def publish(
    body: PublishBody, request: Request, auth: CurrentAuth, config: ConfigDep
) -> dict[str, object]:
    revision = await config.publish(body.revision, published_by=auth.user.id)
    request.app.state.registry.invalidate()
    request.app.state.secret_cache.clear()
    for pid in (await config.effective()).providers:
        secret = await config.read_secret(pid)
        if secret:
            request.app.state.secret_cache[pid] = secret
    return {"published_revision": revision}


@router.post("/settings/rollback")
async def rollback(
    body: RollbackBody, request: Request, auth: CurrentAuth, config: ConfigDep
) -> dict[str, object]:
    revision = await config.rollback(body.to_revision)
    request.app.state.registry.invalidate()
    request.app.state.secret_cache.clear()
    for pid in (await config.effective()).providers:
        secret = await config.read_secret(pid)
        if secret:
            request.app.state.secret_cache[pid] = secret
    return {"published_revision": revision}


@router.put("/settings/providers/{provider_id}/secret")
async def set_secret(
    provider_id: str,
    body: SecretBody,
    request: Request,
    auth: CurrentAuth,
    config: ConfigDep,
) -> dict[str, object]:
    status = await config.set_secret(provider_id, body.secret)
    request.app.state.secret_cache[provider_id] = body.secret.strip()
    request.app.state.registry.invalidate(provider_id)
    return {"provider_id": provider_id, **status}


@router.delete("/settings/providers/{provider_id}/secret")
async def delete_secret(
    provider_id: str, request: Request, auth: CurrentAuth, config: ConfigDep
) -> dict[str, object]:
    await config.delete_secret(provider_id)
    request.app.state.secret_cache.pop(provider_id, None)
    request.app.state.registry.invalidate(provider_id)
    return {"provider_id": provider_id, "has_secret": False}


@router.post("/settings/providers/{provider_id}/probe")
async def probe_provider(
    provider_id: str, auth: CurrentAuth, config: ConfigDep, router_: RouterDep
) -> dict[str, object]:
    effective = await config.effective()
    result = await router_.probe(effective, provider_id)
    return {
        "provider_id": provider_id,
        "ok": result.ok,
        "detail": result.detail,
        "latency_ms": result.latency_ms,
        "models": result.models,
    }


@router.get("/settings/health")
async def model_health(router_: RouterDep, config: ConfigDep) -> dict[str, object]:
    effective = await config.effective()
    return {
        "revision": effective.revision,
        "test_mode": effective.test_mode,
        "health": router_.health_snapshot(),
        "profiles": {
            name: {"provider_id": b.provider_id, "model_id": b.model_id, "enabled": b.enabled}
            for name, b in effective.profiles.items()
        },
    }


@router.get("/settings/providers/{provider_id}/secret")
async def secret_status(
    provider_id: str, auth: CurrentAuth, config: ConfigDep
) -> dict[str, object]:
    status = await config.secret_status(provider_id)
    return {"provider_id": provider_id, **status}


__all__ = ["router"]


def _unused() -> None:  # pragma: no cover
    raise LingError(ErrorCode.internal, "unused")
