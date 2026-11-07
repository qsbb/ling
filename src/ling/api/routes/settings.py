"""设置路由：统一模型设置入口（供应商、密钥、职责绑定、发布、回滚、探测）。"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ling.api.deps import ConfigDep, CurrentAuth, RouterDep, SettingsDep
from ling.api.errors import LingError
from ling.audit import audit_event
from ling.contracts.v1 import ErrorCode
from ling.infra.models import utcnow
from ling.ports.config import (
    BindingMode,
    EffectiveConfig,
    ResolutionSource,
    ResolvedProfile,
)
from ling.ports.model import (
    SLOT_HINTS,
    SLOT_LABELS,
    SLOT_ORDER,
    provider_capabilities,
    required_capability,
    slot_for_profile,
)
from ling.settings.service import (
    KNOWN_PROVIDER_KINDS,
    PROFILE_DEGRADATIONS,
    RESERVED_PROFILES,
)

router = APIRouter(tags=["settings"])

# 设置页展示的角色，按人读的顺序（不是字母序）。
# 只列**绑定后真的会生效**的：保留角色见 RESERVED_PROFILES，
# 它们绑定了没有任何效果，展示出来等于承诺一个不存在的能力。
#
# `dialogue.deep` 已移进 RESERVED_PROFILES：审计确认全仓没有任何代码按它
# 发起请求（见 docs/MODEL-ROLES-AUDIT.md），列着它等于承诺"绑上会更聪明"。
#
# 语音三项（speech.asr / speech.tts / realtime.voice）也在 RESERVED_PROFILES：
# 入站分段是真的，但**没有按模型职责构造的 provider**，绑上去不会改变任何
# 行为。用户看到"已绑定"却听不见任何声音，比看不到这个入口更糟。
#
# `memory.embed` / `memory.rerank` 从保留名单挪到这里：它们现在**真的**读
# 绑定（`domains/memory/providers.py`：先读绑定，没绑定才退回环境变量）。
_OFFERABLE_PROFILES: tuple[str, ...] = (
    "dialogue.fast",
    "dialogue.group",
    "perception.vision",
    "memory.extract",
    "memory.embed",
    "memory.rerank",
)


class ModelIn(BaseModel):
    """供应商目录里的一项。服务端只存，不用它做路由白名单。"""

    model_id: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=200)
    enabled: bool = True


class ProviderIn(BaseModel):
    provider_id: str = Field(min_length=1, max_length=64)
    kind: str = "fake"
    base_url: str | None = None
    enabled: bool = True
    extra: dict = Field(default_factory=dict)
    # 不传 = 不改动已有清单（见 `save_draft` 路由的 exclude_unset）。
    models: list[ModelIn] = Field(default_factory=list)
    # 传输与额度属于**供应商**：同一家下面所有职责共用一份超时和额度。
    # -1 = 不限制（前端留空时也提交 -1，见 ProvidersPanel）。
    timeout_seconds: float = 60.0
    max_output_tokens: int | None = None
    daily_token_limit: int | None = None


class ProfileIn(BaseModel):
    """一个职责绑定：只回答"交给哪家供应商的哪个模型"。

    超时 / 输出上限 / 日额度**不在这里**：它们是供应商的属性
    （见 ``ProviderIn``）。旧客户端可能仍会带这几个键，pydantic 默认忽略
    未知字段，所以不会因此 422，也不会悄悄覆盖供应商的设置。
    """

    profile: str
    provider_id: str = ""
    model_id: str = ""
    fallback_provider_id: str | None = None
    fallback_model_id: str | None = None
    enabled: bool = True
    # ``explicit``（默认）/ ``slot``。**默认必须是 explicit**：旧客户端不带
    # 这个键，而它们的每一条都是显式选择，按 slot 读会把现有部署全作废。
    mode: str = BindingMode.explicit.value


class SlotIn(BaseModel):
    """一个默认槽位。四项都空 = 把这个槽位清回"没配"。"""

    slot: str
    provider_id: str = ""
    model_id: str = ""


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
    # 群聊里「决定回了」之后是否真的生成并发送（默认关）
    group_reply_delivery_enabled: bool | None = None


class DraftBody(BaseModel):
    providers: list[ProviderIn] | None = None
    profiles: list[ProfileIn] | None = None
    slots: list[SlotIn] | None = None
    test_mode: bool | None = None
    # 入站白名单拒绝是否逐条写日志。只影响日志量，不影响拒绝本身。
    log_access_denials: bool | None = None
    memory: MemoryPolicyIn | None = None
    life: LifePolicyIn | None = None
    dialogue: DialoguePolicyIn | None = None
    note: str | None = None


class ResolveBody(BaseModel):
    """算「那它到底会跑哪个模型」时带上的**草稿**。

    三项都可不传：不传的部分按**当前已生效**配置算。界面会在两种时候调它——
    改了下拉还没保存时（要立刻看到「实际生效」跟着变），以及快速配置的
    二次确认（要列出"点下去会变成什么"）。

    **只算不写**：这个 body 不会产生修订，也不会碰数据库。
    """

    providers: list[ProviderIn] | None = None
    profiles: list[ProfileIn] | None = None
    slots: list[SlotIn] | None = None
    test_mode: bool | None = None


class PublishBody(BaseModel):
    revision: int


class RollbackBody(BaseModel):
    to_revision: int


class SecretBody(BaseModel):
    secret: str = Field(min_length=1, max_length=4096)


# ---------------------------------------------------------------- 视图拼装
#
# 这几个函数是「槽位是什么、职责最后会跑什么」的**唯一出口**。它们都只读
# `EffectiveConfig.resolve_profile()` 的结论，绝不自己再判一次——前端更不
# 许算：两边各算一次，迟早会出现"界面写 A、实际跑 B"，而那种 bug 没有
# 任何报错可查。


def _effective_view(resolved: ResolvedProfile) -> dict[str, str] | None:
    if not resolved.usable or not resolved.provider_id or not resolved.model_id:
        return None
    return {"provider_id": resolved.provider_id, "model_id": resolved.model_id}


def _resolution_view(resolved: ResolvedProfile) -> dict[str, object]:
    requested = None
    if resolved.requested_provider_id:
        requested = {
            "provider_id": resolved.requested_provider_id,
            "model_id": resolved.requested_model_id,
        }
    return {
        "profile": resolved.profile,
        "slot": resolved.slot,
        "source": resolved.source.value,
        "effective": _effective_view(resolved),
        "requested": requested,
        "fell_back": resolved.fell_back,
        "enabled": resolved.enabled,
        "reason": resolved.reason,
    }


def _slot_views(config: EffectiveConfig) -> list[dict[str, object]]:
    """四个默认槽位 + 各自「谁在跟着我」。

    ``roles`` 让界面能直接写出"3 个职责跟着它"，不必再去翻映射表——
    映射表在服务端（`ports/model.py::PROFILE_SLOTS`），前端拿一份手抄的
    迟早不同步。
    """
    roles_by_slot: dict[str, list[str]] = {}
    for profile in _OFFERABLE_PROFILES:
        key = slot_for_profile(profile)
        if key is not None:
            roles_by_slot.setdefault(key.value, []).append(profile)
    views: list[dict[str, object]] = []
    for key in SLOT_ORDER:
        slot = config.slots.get(key.value)
        configured = bool(slot and slot.configured)
        views.append(
            {
                "slot": key.value,
                "label": SLOT_LABELS[key],
                "hint": SLOT_HINTS[key],
                "provider_id": slot.provider_id if slot else "",
                "model_id": slot.model_id if slot else "",
                "configured": configured,
                # 「可用」= 供应商 enabled 且模型 enabled（见
                # `EffectiveConfig.is_usable`）。槽位没配 ≠ 槽位坏了，
                # 所以两个字段分开给。
                "usable": config.is_usable(
                    slot.provider_id if slot else "", slot.model_id if slot else ""
                ),
                "roles": roles_by_slot.get(key.value, []),
            }
        )
    return views


def _resolution_views(config: EffectiveConfig) -> list[dict[str, object]]:
    return [_resolution_view(config.resolve_profile(profile)) for profile in _OFFERABLE_PROFILES]


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
    # 嵌入/重排的能力现在有两种来源：网页绑定或环境变量。**前端必须能区分**——
    # 只看"有没有绑定"会把 env 部署报成"语义检索不可用"，用户会去乱配一通。
    from ling.domains.memory.providers import embedding_source, rerank_source

    memory_sources = {
        "memory.embed": embedding_source(effective),
        "memory.rerank": rerank_source(effective),
    }
    providers = []
    for p in effective.providers.values():
        status = await config.secret_status(p.provider_id)
        providers.append(
            {
                "provider_id": p.provider_id,
                "kind": p.kind,
                # 这家**能服务哪些能力**。前端的绑定下拉据此过滤：把
                # memory.embed 选到一家对话供应商上，服务端会拒绝发布
                # （见 validate_payload），但不该让用户先选错再被报错。
                "capabilities": sorted(
                    capability.value for capability in provider_capabilities(p.kind)
                ),
                "base_url": p.base_url,
                "enabled": p.enabled,
                "has_secret": bool(status["has_secret"]),
                "secret_last4": status["last4"],
                "secret_fingerprint": status["fingerprint"],
                "extra": p.extra,
                "models": [m.to_dict() for m in p.models],
                "timeout_seconds": p.timeout_seconds,
                "max_output_tokens": p.max_output_tokens,
                "daily_token_limit": p.daily_token_limit,
            }
        )
    return {
        "schema_version": "v1",
        "revision": effective.revision,
        "test_mode": effective.test_mode,
        # 与 `test_mode` 同级：实例级开关，不属于任何领域策略。
        "log_access_denials": effective.log_access_denials,
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
            "group_reply_delivery_enabled": (
                effective.dialogue.group_reply_delivery_enabled
            ),
        },
        "profiles": [
            {
                "profile": b.profile,
                "provider_id": b.provider_id,
                "model_id": b.model_id,
                "fallback_provider_id": b.fallback_provider_id,
                "fallback_model_id": b.fallback_model_id,
                "enabled": b.enabled,
                # 这条绑定是"自己指定"还是"跟随默认槽位"。少了它，前端拿到的
                # 是一串空 provider_id，只能靠猜来判断是不是"跟随"。
                "mode": b.mode.value,
            }
            for b in effective.profiles.values()
        ],
        # 四个默认槽位（快速 / 创作 / 识图 / 日常）。
        "slots": _slot_views(effective),
        # **每个职责最后会跑哪个模型**——服务端算，前端只显示。
        # 这一份同时是 `POST /settings/resolve` 的返回形状，两边共用一个拼装函数。
        "profile_resolutions": _resolution_views(effective),
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
        "known_kinds": sorted(KNOWN_PROVIDER_KINDS),
        # 只列出**绑定后真的会生效**的角色。
        # 保留角色见 settings/service.py::RESERVED_PROFILES，
        # 它们绑定了没有任何效果，展示出来等于承诺不存在的能力。
        "known_profiles": [p for p in _OFFERABLE_PROFILES],
        # 每个角色**需要哪种能力**。前端拿它去过滤供应商下拉：
        # `profile_capabilities[role] in provider["capabilities"]`。
        #
        # 为什么保留 `known_profiles` 的字符串数组、另加一份映射，而不是把
        # 两份合并成对象数组：`known_profiles` 是已发布的响应字段，改形状会
        # 让旧客户端（含本文档发布时的控制台）拿到自己解析不了的东西。新增
        # 字段是兼容的，删改字段不是。
        "profile_capabilities": {
            profile: (cap.value if (cap := required_capability(profile)) else None)
            for profile in _OFFERABLE_PROFILES
        },
        "reserved_profiles": dict(RESERVED_PROFILES),
        # 嵌入/重排当前的能力来源：``binding``（网页绑定）/ ``env``（环境变量）
        # / null（都没有 = 该能力实际关闭）。只读展示；下面的
        # `unbound_degradations` 也读它，用来避免把 env 部署报成"没配"。
        "memory_sources": memory_sources,
        # **未绑定职责会失去什么**。系统不会因此报错，它会走降级路径——
        # 看起来一切正常，只是能力悄悄变窄。用户只会觉得"她好像记不住事"，
        # 无从判断是没配模型。
        #
        # 判据是**解析结果**，不是"有没有一条绑定记录"：上了默认槽位之后，
        # `dialogue.group` 完全可以没有显式绑定却照常工作（跟随「日常」），
        # 按"有没有绑定"判会把它误报成降级，用户去补一条本来不需要的绑定。
        #
        # 嵌入/重排要额外看**环境变量**：只在"绑定与环境变量都没有"时才算降级，
        # 否则会把用 env 配好的部署报成语义检索失效。
        "unbound_degradations": {
            profile: PROFILE_DEGRADATIONS[profile]
            for profile in _OFFERABLE_PROFILES
            if profile in PROFILE_DEGRADATIONS
            and not effective.resolve_profile(profile).usable
            # 来源**不是** None 说明这份能力已经能用（绑定或环境变量），
            # 不该再报降级；不在 memory_sources 里的角色没有"环境变量也算
            # 配置"这回事，一律按未绑定处理。
            and not (
                profile in memory_sources and memory_sources[profile] is not None
            )
        },
    }


@router.get("/settings/connectors")
async def get_connectors(
    request: Request, auth: CurrentAuth, settings: SettingsDep
) -> dict[str, object]:
    """接入器（渠道连接器）的**只读**诊断：凭据齐不齐、连没连上、改哪里。

    **必须鉴权**：这条端点把部署的接入方式摊开（有哪些入站端口、开了哪个
    渠道、凭据设了没有）。它不含任何密钥明文（见
    ``ling.settings.connectors``：``kind="secret"`` 的字段连值都不进视图对象），
    但"哪个变量设了、监听在哪个端口"本身就是部署指纹，不该匿名可读。

    ``configured`` 不在这里另判一遍：QQ 官方机器人复用
    ``QQBotConfig.configured``，与总览的 ``connectivity`` 段同源；否则会出现
    "总览说配好了、设置页说没配"这种自相矛盾，而用户无从判断谁对。

    状态只读进程里**已经存在**的对象，不发网络探测：设置页会被反复打开，
    把它做成探活会让每次打开都对外打一次 QQ 开放平台。
    """
    from ling.settings.connectors import connector_views

    return {
        "schema_version": "v1",
        "as_of": utcnow().isoformat(),
        "connectors": connector_views(settings, request.app.state),
    }


@router.put("/settings/draft")
async def save_draft(
    body: DraftBody, auth: CurrentAuth, config: ConfigDep
) -> dict[str, object]:
    result = await config.save_draft(
        # exclude_unset：老客户端不带 models 时表示"别动清单"，
        # 而不是用默认值 [] 把已保存的模型目录清空。
        providers=(
            [p.model_dump(exclude_unset=True) for p in body.providers]
            if body.providers
            else None
        ),
        profiles=[p.model_dump() for p in body.profiles] if body.profiles else None,
        slots=[s.model_dump() for s in body.slots] if body.slots else None,
        test_mode=body.test_mode,
        log_access_denials=body.log_access_denials,
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


def _quick_config_rows(
    before: EffectiveConfig, after: EffectiveConfig
) -> list[dict[str, object]]:
    """快速配置点下去之后，每条职责会变成什么。

    这里**不做任何解析**：`before` / `after` 都是服务端算好的快照，
    这个函数只负责把两次结果摆到同一行里，并给出一句"变化"。
    """
    rows: list[dict[str, object]] = []
    for profile in _OFFERABLE_PROFILES:
        key = slot_for_profile(profile)
        if key is None:
            continue
        old = before.resolve_profile(profile)
        new = after.resolve_profile(profile)
        old_model = _effective_view(old)
        new_model = _effective_view(new)
        # 旧版兼容是**另一条轴**：那个模型既不是用户选的、也不是槽位来的。
        # 文案上必须分开，否则会写成"覆盖你选的 X"，而用户从没选过 X。
        if new.source is ResolutionSource.legacy_fallback:
            kind = "legacy"
            change = (
                f"仍然借主对话模型 {new_model['model_id'] if new_model else ''}"
                f"（「{SLOT_LABELS[key]}」槽位没配，快速配置给不了它模型）"
            )
        elif new_model is None:
            change = f"仍然不可用：{new.reason}"
            kind = "unavailable"
        elif old_model is None:
            change = "修复不可用"
            kind = "repair"
        elif old_model == new_model:
            change = "与现在相同"
            kind = "same"
        elif old.source is ResolutionSource.legacy_fallback:
            change = f"原来借的是 {old_model['model_id']}，之后改用 {new_model['model_id']}"
            kind = "override"
        else:
            # 说清"被覆盖的是什么"，而不是只说"会变"——用户按下去之前
            # 得能判断这次覆盖值不值。
            change = f"覆盖你选的 {old_model['model_id']}"
            kind = "override"
        rows.append(
            {
                "profile": profile,
                "slot": key.value,
                "slot_label": SLOT_LABELS[key],
                "model_id": new_model["model_id"] if new_model else None,
                "provider_id": new_model["provider_id"] if new_model else None,
                "change": change,
                "change_kind": kind,
            }
        )
    return rows


@router.post("/settings/resolve")
async def resolve_draft(
    body: ResolveBody, auth: CurrentAuth, config: ConfigDep
) -> dict[str, object]:
    """按**草稿**算每个职责的生效模型。**只算不写。**

    界面上的「实际生效」列就靠它：用户改了下拉还没保存，那一列必须立刻跟着
    变；但**不能在浏览器里算**——回退规则实现两遍，两边迟早不一致。所以
    前端把草稿发过来，服务端用与保存完全相同的合并 + 解析跑一遍。
    """
    view = await config.resolve_preview(
        providers=[p.model_dump(exclude_unset=True) for p in body.providers]
        if body.providers
        else None,
        profiles=[p.model_dump() for p in body.profiles] if body.profiles else None,
        slots=[s.model_dump() for s in body.slots] if body.slots else None,
        test_mode=body.test_mode,
    )
    return {
        "schema_version": "v1",
        "slots": _slot_views(view),
        "profile_resolutions": _resolution_views(view),
    }


@router.post("/settings/quick-config/preview")
async def quick_config_preview(
    body: ResolveBody, auth: CurrentAuth, config: ConfigDep
) -> dict[str, object]:
    """「快速配置」的二次确认：把每个职责指回它自己的槽位，会发生什么。

    **语义是全量覆盖**：所有有槽位的职责都会被改成「跟随默认」，用户单独
    指定过的模型（含"我特意换了个贵的"）一并清掉。做成"只填空、不动显式"
    的话，点完看起来什么都没发生，用户会以为按钮坏了——所以弹窗必须把
    "将要覆盖哪几条"摆在明面上，这也是这里返回逐行 ``change`` 的原因。

    ``memory.embed`` / ``memory.rerank`` 不参与：它们没有槽位（嵌入与重排
    是另一套协议），指过去只会把它们指成不可用。

    返回的 ``profiles`` 是**点确认后应当写入的那份草稿**，前端照发即可，
    不要在浏览器里再拼一次。
    """
    before = await config.resolve_preview(
        providers=[p.model_dump(exclude_unset=True) for p in body.providers]
        if body.providers
        else None,
        profiles=[p.model_dump() for p in body.profiles] if body.profiles else None,
        slots=[s.model_dump() for s in body.slots] if body.slots else None,
        test_mode=body.test_mode,
    )
    current = {b.profile: b for b in before.profiles.values()}
    next_profiles: list[dict[str, object]] = []
    for profile in _OFFERABLE_PROFILES:
        if slot_for_profile(profile) is None:
            continue
        existing = current.get(profile)
        next_profiles.append(
            {
                "profile": profile,
                "provider_id": "",
                "model_id": "",
                "fallback_provider_id": None,
                "fallback_model_id": None,
                # 只管"用谁的模型"，不管"这个职责开不开"：用户主动关掉的
                # 职责不该被一个配置按钮悄悄打开。
                "enabled": existing.enabled if existing else True,
                "mode": BindingMode.slot.value,
            }
        )
    after = await config.resolve_preview(
        providers=[p.model_dump(exclude_unset=True) for p in body.providers]
        if body.providers
        else None,
        profiles=next_profiles,
        slots=[s.model_dump() for s in body.slots] if body.slots else None,
        test_mode=body.test_mode,
    )
    rows = _quick_config_rows(before, after)
    return {
        "schema_version": "v1",
        "rows": rows,
        "profiles": next_profiles,
        "summary": {
            "total": len(rows),
            "overrides": sum(1 for row in rows if row["change_kind"] == "override"),
            "unchanged": sum(1 for row in rows if row["change_kind"] == "same"),
            "repairs": sum(1 for row in rows if row["change_kind"] == "repair"),
            "still_unavailable": sum(
                1 for row in rows if row["change_kind"] == "unavailable"
            ),
        },
    }


@router.post("/settings/publish")
async def publish(
    body: PublishBody, request: Request, auth: CurrentAuth, config: ConfigDep
) -> dict[str, object]:
    revision = await config.publish(body.revision, published_by=auth.user.id)
    request.app.state.registry.invalidate()
    request.app.state.secret_cache.clear()
    # 「入站拒绝日志」开关也在配置修订里，而受理路径把它缓存在 app.state
    # （TTL 见 inbound_intake）。发布/回滚发生在控制面，本进程的缓存立刻
    # 失效，别让用户等一个 TTL 才看到开关生效。
    request.app.state._log_denials_cache = None
    for pid in (await config.effective()).providers:
        secret = await config.read_secret(pid)
        if secret:
            request.app.state.secret_cache[pid] = secret
    # 配置发布是"改了系统行为"的动作，必须留痕：谁、从哪个修订、改成哪个
    audit_event(
        "config_published",
        actor=auth.user.id,
        target_type="config_revision",
        target_id=str(revision),
        attributes={"from_revision": body.revision},
    )
    return {"published_revision": revision}


@router.post("/settings/rollback")
async def rollback(
    body: RollbackBody, request: Request, auth: CurrentAuth, config: ConfigDep
) -> dict[str, object]:
    revision = await config.rollback(body.to_revision)
    request.app.state.registry.invalidate()
    request.app.state.secret_cache.clear()
    # 「入站拒绝日志」开关也在配置修订里，而受理路径把它缓存在 app.state
    # （TTL 见 inbound_intake）。发布/回滚发生在控制面，本进程的缓存立刻
    # 失效，别让用户等一个 TTL 才看到开关生效。
    request.app.state._log_denials_cache = None
    for pid in (await config.effective()).providers:
        secret = await config.read_secret(pid)
        if secret:
            request.app.state.secret_cache[pid] = secret
    audit_event(
        "config_rolled_back",
        actor=auth.user.id,
        target_type="config_revision",
        target_id=str(revision),
        attributes={"to_revision": body.to_revision},
    )
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
    # 只记"哪个 provider 的密钥被换了"，**绝不记密钥本身**
    audit_event(
        "provider_secret_set",
        actor=auth.user.id,
        target_type="provider",
        target_id=provider_id,
    )
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
    started = time.monotonic()
    result = await router_.probe(effective, provider_id)
    # 有的 provider 自己量了延迟（fake 是 0），有的没有（OpenAI 兼容接口的
    # probe 只拉 /models）。没有就由路由补上整次探测的墙钟时间——界面要显示
    # 「多少毫秒」，null 会变成「null 毫秒」。
    latency_ms = (
        result.latency_ms
        if result.latency_ms is not None
        else int((time.monotonic() - started) * 1000)
    )
    return {
        "provider_id": provider_id,
        "ok": result.ok,
        "detail": result.detail,
        "latency_ms": latency_ms,
        "models": result.models,
        # 探测是**只读**的：不写修订、也不会把结果变成新的配置。
        # 时间戳只用于界面标注"这次检查发生在什么时候"。
        "checked_at": utcnow().isoformat(),
    }


@router.get("/settings/health")
async def model_health(
    router_: RouterDep, config: ConfigDep, auth: CurrentAuth
) -> dict[str, object]:
    """供应商/职责的健康摘要。

    **必须鉴权**：``health`` 里的 ``last_error`` 是供应商返回的原文（可能带
    上游地址、请求 id，甚至把密钥片段回显出来），``profiles`` 也等于把部署
    配置摊开。这条端点曾经没有 ``CurrentAuth``，于是未登录就能读到这些，
    而且首次安装未改密时（``must_change_password`` 闸在 ``current_auth`` 里，
    不经过它的路由不受保护）照样暴露。
    """
    effective = await config.effective()
    # ``last_error`` 是供应商返回的原文，可能带上游地址、请求 id，甚至把
    # 密钥片段回显出来。诊断细节走服务端日志与 ``/settings/providers/{id}/probe``，
    # **不从这条汇总端点外泄**——汇总端点会被前端反复轮询。
    health = {
        key: {k: v for k, v in record.items() if k != "last_error"}
        for key, record in router_.health_snapshot().items()
    }
    return {
        "revision": effective.revision,
        "test_mode": effective.test_mode,
        "health": health,
        "profiles": {
            name: {"provider_id": b.provider_id, "model_id": b.model_id, "enabled": b.enabled}
            for name, b in effective.profiles.items()
        },
    }


@router.get("/settings/usage")
async def provider_usage(
    request: Request, auth: CurrentAuth, config: ConfigDep
) -> dict[str, object]:
    """今天各供应商已用了多少 token。

    单独一条端点、单独一次查询，而不是塞进 ``/settings/config``：那条会被
    前端在每次保存后重读，用量却每轮对话都在变，混在一起会让"配置变了"
    和"数字变了"分不清。
    """
    store = getattr(request.app.state, "quota", None)
    if store is None:
        return {"day": None, "usage": {}}
    effective = await config.effective()
    snapshot = await store.snapshot(list(effective.providers))
    return {
        "day": store.today().isoformat(),
        "usage": snapshot,
        "limits": {
            pid: cfg.daily_limit_effective() for pid, cfg in effective.providers.items()
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
