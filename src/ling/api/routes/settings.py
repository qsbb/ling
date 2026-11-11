"""设置路由：统一模型设置入口（供应商、密钥、职责绑定、发布、回滚、探测）。"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ling.api.deps import ConfigDep, CurrentAuth, DbSession, RouterDep, SettingsDep
from ling.api.errors import LingError
from ling.audit import audit_event
from ling.contracts.v1 import (
    ConnectorAccessGroupOut,
    ConnectorActivationOut,
    ConnectorFieldOut,
    ConnectorsOut,
    ConnectorView,
    ErrorCode,
)
from ling.infra.models import DEFAULT_INSTANCE_ID, utcnow
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
    # 看视频：职责名与「视频」槽位先落地，用户要求能在设置页挑"看视频的模型"。
    # **注意：媒体描述链路目前没有给它自动路由**（用户要求"她想主动想看才读"，
    # 逐条自动描述白烧 token）。绑定值会在接线后由调用方显式传
    # `VIDEO_PROFILE` 时被读取；在那之前绑它不改变任何行为——这一条必须留在
    # 这里，别让它变成一个没人知道的假入口。
    "perception.video",
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


class PreferenceBody(BaseModel):
    """用户偏好（时区 + 视频读取模式）。**缺省 = 不改；显式空串 = 清掉。**

    ## 为什么必须是"缺省不改"

    两个偏好共用一条 PUT，前端也是各发各的（``setTimezone`` 只带 ``timezone``、
    ``setVideoMode`` 只带 ``video_mode``）。如果缺省值等于"清掉"，那么切一次
    "怎么看视频"就会静默把时区一起清掉——用户看到的是「已保存」，刷新后才发现
    回到默认时区（验收 B1 实测，这是数据丢失）。

    所以：``None`` = 这次不碰这个字段；``""`` = 显式清掉（时区回退环境变量/默认，
    视频模式回退默认 ``frames``）。想改哪个就只发哪个。
    """

    timezone: str | None = Field(default=None, max_length=64)
    # 视频读取模式：``frames``（抽帧，默认）/ ``native``（原生视频输入）。
    video_mode: str | None = Field(default=None, max_length=16)


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


async def _preference_view(
    session: AsyncSession, *, subject_id: str, env: str | None
) -> dict[str, object]:
    """偏好的响应形状。GET 与 PUT 共用一份——两个入口拼两份，迟早不一样。"""
    from ling.settings.user_preferences import (
        COMMON_TIMEZONES,
        DEFAULT_USER_TIMEZONE,
        DEFAULT_VIDEO_MODE,
        VIDEO_MODES,
        load_user_timezone,
        load_video_mode,
        stored_timezone,
    )

    name, source = await load_user_timezone(session, subject_id=subject_id, env=env)
    return {
        # 视频怎么看（抽帧/原生）：用户偏好，**不在模型页**——它管的是
        # "怎么把视频交给模型"，不是"用哪个模型"。
        "video_mode": await load_video_mode(session, subject_id=subject_id),
        "video_modes": list(VIDEO_MODES),
        "video_mode_default": DEFAULT_VIDEO_MODE,
        "schema_version": "v1",
        "timezone": name,
        "source": source.value,
        # 库里存的原值：界面用它显示"你自己填过"，与生效值区分开。
        "stored": await stored_timezone(session, subject_id),
        "env": env,
        "default": DEFAULT_USER_TIMEZONE,
        "common": list(COMMON_TIMEZONES),
    }


@router.get("/settings/preferences")
async def get_preferences(
    auth: CurrentAuth, session: DbSession, settings: SettingsDep
) -> dict[str, object]:
    """用户自己的偏好（当前只有时区）。

    **为什么不并进 ``/settings/config``**：那一份是配置修订（草稿→发布→
    回滚），时区是用户的事实，不进修订、也不该跟着回滚（见
    ``infra/models.py::UserPreferenceRow``）。混进同一份响应里，前端迟早会
    拿 ``saveDraft`` 去存它。

    ``source`` 必须回给前端：界面要能说出"现在是默认值，不是你设的"。
    没有它的话，用户看到 Asia/Shanghai 会以为自己设过。
    """
    return await _preference_view(
        session, subject_id=auth.user.subject_id, env=settings.user_timezone
    )


@router.put("/settings/preferences")
async def put_preferences(
    body: PreferenceBody,
    auth: CurrentAuth,
    session: DbSession,
    settings: SettingsDep,
) -> dict[str, object]:
    """写用户偏好。**只改请求里带了的字段**：``None`` 跳过，``""`` 清掉。

    两个偏好共用一条 PUT，所以"没带这个字段"绝不能等于"清掉它"——
    否则切一次视频模式就会把时区静默清回默认（验收 B1 的数据丢失）。

    校验在这一层做，并且**给出可执行的提示**：非法名字只回一句"invalid"，
    用户会去猜是不是少了时区前缀、是不是该写 UTC+8。实际正确的是 IANA 名。
    """
    from ling.settings.user_preferences import (
        clear_user_timezone,
        clear_video_mode,
        normalize_timezone,
        normalize_video_mode,
        save_user_timezone,
        save_video_mode,
    )

    # 时区：None = 这次不改；空串 = 清掉（回退 env → 默认）。
    if body.timezone is not None:
        raw = body.timezone
        if not raw.strip():
            await clear_user_timezone(session, subject_id=auth.user.subject_id)
        else:
            if normalize_timezone(raw) is None:
                raise LingError(
                    ErrorCode.invalid_request,
                    "不是合法的时区名，请填 IANA 名称（例：Asia/Shanghai）",
                    detail=(
                        "IANA 名形如「区域/城市」，例如 Asia/Shanghai、Europe/London、"
                        "America/New_York；不要写 UTC+8 或「东八区」。"
                    ),
                )
            await save_user_timezone(
                session, subject_id=auth.user.subject_id, raw=raw
            )

    # 视频模式：None = 这次不改；空串 = 清掉（回退默认 frames）。
    if body.video_mode is not None:
        mode = body.video_mode.strip()
        if not mode:
            await clear_video_mode(session, subject_id=auth.user.subject_id)
        else:
            if normalize_video_mode(mode) is None:
                raise LingError(
                    ErrorCode.invalid_request,
                    "不是合法的视频读取模式",
                    detail=(
                        "可选：frames（抽帧，任何供应商都能用）/ "
                        "native（原生视频输入）。"
                    ),
                )
            await save_video_mode(
                session, subject_id=auth.user.subject_id, raw=mode
            )

    return await _preference_view(
        session, subject_id=auth.user.subject_id, env=settings.user_timezone
    )


async def _connector_field_views(
    session: AsyncSession,
    *,
    settings: object,
    app_state: object,
    include_sensitive: bool,
    instances: dict[str, str] | None = None,
) -> dict[str, list[dict[str, object]]]:
    """三个 scope 的字段视图（``source`` / ``configured`` / ``preview`` / ``value``）。

    **GET 全程不解密**：这里只读 ``preview`` 与指纹，密钥原文一次都没进内存。
    设计 §5 的硬要求是"普通 GET、审计、状态机与日志不解密"，只有构造连接器
    的那条受控路径才解密。

    密钥**永远**只有 ``preview`` 与 ``configured``，``value`` 恒 ``None``；
    非 owner 连 ``preview`` 都不给（设计 §6：敏感预览只给 owner）。

    ``instances`` 是 ``{scope: instance_id}``：**逐个 scope** 决定读哪个实例的
    覆盖与 HEAD 指针。缺省（``None``）时三个 scope 都读 ``'default'``——不带
    实例的调用方（老前端、reset、任何内部读取）行为与升级前完全一致。
    """
    from ling.settings import connector_fields as cf
    from ling.settings import connector_overrides as ov

    # env 基线（不含任何 DB 覆盖）：构造 dataclass 只为让解析器拿到
    # OneBotConfig / QQBotConfig 那一层的 env 值。
    env_only_onebot = await ov.resolve_field_values(
        session,
        scope=cf.SCOPE_ONEBOT,
        settings=settings,
        include_secrets=False,
        use_overrides=False,
    )
    env_only_qqbot = await ov.resolve_field_values(
        session,
        scope=cf.SCOPE_QQBOT,
        settings=settings,
        include_secrets=False,
        use_overrides=False,
    )
    onebot_cfg = ov.build_onebot_config_from(env_only_onebot.fields_by_key())
    qqbot_cfg = ov.build_qqbot_config_from(env_only_qqbot.fields_by_key())

    out: dict[str, list[dict[str, object]]] = {}
    for scope in cf.SCOPES:
        resolved = await ov.resolve_field_values(
            session,
            scope=scope,
            instance_id=(instances or {}).get(scope, DEFAULT_INSTANCE_ID),
            settings=settings,
            onebot=onebot_cfg,
            qqbot=qqbot_cfg,
            include_secrets=False,
        )
        out[scope] = [
            state.public(include_sensitive=include_sensitive)
            for state in resolved.fields.values()
        ]
    return out


async def _activation_views(
    session: AsyncSession, *, instances: dict[str, str] | None = None
) -> dict[str, dict[str, object]]:
    """三个 scope 的 ``activation`` 段，**按各自实例**取。

    HEAD 指针与"最近一条修订"必须落在同一个实例上：只按 scope 查修订会让
    实例 B 的 ``last_revision_id`` / ``last_state`` 显示成实例 A 的，而那段
    形状完全正常——读串了看不出来。

    ``instances`` 缺省时三个 scope 都读 ``'default'``（升级前行为）。
    """
    from ling.infra.models import ConnectorConfigHead
    from ling.settings import connector_fields as cf
    from ling.settings.connector_service import activation_payload, latest_revision

    out: dict[str, dict[str, object]] = {}
    for scope in cf.SCOPES:
        instance_id = (instances or {}).get(scope, DEFAULT_INSTANCE_ID)
        head = await session.get(ConnectorConfigHead, (scope, instance_id))
        out[scope] = activation_payload(
            scope=scope,
            generation=int(getattr(head, "generation", 0) or 0),
            active_revision_id=getattr(head, "active_revision_id", None),
            candidate_revision_id=getattr(head, "candidate_revision_id", None),
            last=await latest_revision(session, scope, instance_id=instance_id),
        )
    return out


async def _live_instances(
    session: AsyncSession, *, instance_id: str | None
) -> dict[str, str]:
    """逐个 scope 解析这次要读的实例，返回 ``{scope: instance_id}``。

    实例是**按 scope 独立**的（主键 ``(scope, instance_id)``），而这条端点一次
    返回三个 scope，所以"读哪个实例"必须逐 scope 回答，不能拿一个 id 套三处：

    - ``instance_id is None``（老前端、任何不带参数的调用方）→ 三个 scope 都读
      ``'default'``。**不查目录、不补建行**：GET 是读路径，不该有副作用；而且
      升级前的存量配置全在默认实例上，目录表里可能根本没有那一行（迁移只给
      "有过数据"的 scope 回填，全新部署表是空的）。硬性要求它会 404 掉老部署。
    - 显式给了 ``instance_id`` → 该 scope 上必须**存在且未软删**，否则
      404 ``instance_not_found`` / 409 ``instance_deleted``。**不**静默回退默认
      实例：那会让"我在看实例 B"变成"我在看实例 A，而界面照常显示"——正是这个
      缺口本来的形状，只是把串读挪到了前端。

    "逐 scope"是刻意的：用户通常只在**一个** scope 上建实例（例如只配了
    onebot 的第二台号），另外两个 scope 上根本没有同名实例。要求三个 scope
    都有会让"切到实例 B"直接 404，把本该能看的部分也挡掉。
    """
    from ling.settings import connector_fields as cf

    if instance_id is None:
        return dict.fromkeys(cf.SCOPES, DEFAULT_INSTANCE_ID)

    from ling.settings.connector_instances import (
        ConnectorInstanceError,
        ConnectorInstanceService,
    )

    service = ConnectorInstanceService(session)
    out: dict[str, str] = {}
    for scope in cf.SCOPES:
        try:
            await service.require_live(scope=scope, instance_id=instance_id)
            out[scope] = instance_id
        except ConnectorInstanceError as exc:
            if exc.status_code != 404:
                raise LingError(
                    ErrorCode.conflict,
                    exc.message,
                    status_code=exc.status_code,
                    detail=exc.code,
                    field=exc.name,
                ) from exc
            # 这个 scope 上没有该实例：它是一个单 scope 的实例，不是"坏请求"。
            # 该 scope 退回默认实例——**但必须让调用方看得见**（见下方
            # `instance_scope_fallbacks` 回显），不能安静地混在一起。
            out[scope] = DEFAULT_INSTANCE_ID
    return out


@router.get("/settings/connectors", response_model=ConnectorsOut)
async def get_connectors(
    request: Request,
    auth: CurrentAuth,
    settings: SettingsDep,
    session: DbSession,
    instance_id: str | None = None,
) -> ConnectorsOut:
    """接入器的诊断 **+ 可配置字段**（v2）：凭据齐不齐、连没连上、有效值来自哪。

    **必须鉴权**：这条端点把部署的接入方式摊开（有哪些入站端口、开了哪个
    渠道、凭据设了没有）。它不含任何密钥明文（见
    ``ling.settings.connectors``：``kind="secret"`` 的字段连值都不进视图对象），
    但"哪个变量设了、监听在哪个端口"本身就是部署指纹，不该匿名可读。

    **多实例**：``?instance_id=`` 决定读**哪个实例**的字段与 ``activation``。
    实例**只在 scope 内存在**，所以它是**逐 scope** 解析的：

    - 不传：三个 scope 都读默认实例。**行为与升级前一致**（存量配置全在默认
      实例上），且**不**在这里补建实例行——GET 是读路径，不该有副作用。
    - 显式传：该 scope 上有这个实例就读它；没有就退回默认实例。已软删 →
      ``409 instance_deleted``（不是"没有"，两者的提示不该混成一句）。
    - 为什么"逐 scope 回退"而不是"整条 404"：用户通常只在**一个** scope 上建
      实例（只给 onebot 配了第二台号），另外两个 scope 上并没有同名实例。
      整条 404 会把本该看得见的部分一起挡掉。
    - 为什么回退必须**可见**：响应同时回显 ``instance_id``（全局）与
      ``instances``（逐 scope 实际读的那个）。退回了默认实例的 scope 在
      ``instances`` 里就是 ``'default'``——**安静回退才是这个缺口本来的形状**，
      回显让"我看的是哪一份"变成可断言的事实，而不是约定。

    ``configured`` 不在这里另判一遍：QQ 官方机器人复用
    ``QQBotConfig.configured``，与总览的 ``connectivity`` 段同源；否则会出现
    "总览说配好了、设置页说没配"这种自相矛盾，而用户无从判断谁对。

    状态只读进程里**已经存在**的对象，不发网络探测：设置页会被反复打开，
    把它做成探活会让每次打开都对外打一次 QQ 开放平台。
    """
    from ling.settings import connector_fields as cf
    from ling.settings.connector_service import operation_view  # noqa: F401 - 供文档引用
    from ling.settings.connectors import connector_views

    is_owner = str(getattr(auth.user, "role", "")) == "owner"

    # 逐 scope 解析"这次读哪个实例"（实例按 scope 独立，见 _live_instances）。
    live = await _live_instances(session, instance_id=instance_id)

    views = connector_views(settings, request.app.state)
    field_views = await _connector_field_views(
        session,
        settings=settings,
        app_state=request.app.state,
        include_sensitive=is_owner,
        instances=live,
    )
    activations = await _activation_views(session, instances=live)

    # 渠道视图：字段与生效状态按 id 挂上去；白名单（access）没有"渠道"概念，
    # 单独一段（设计 §6）。
    merged: list[dict[str, object]] = [
        {**view, "editable": is_owner, "storage": "db"} for view in views
    ]

    access = activations[cf.SCOPE_ACCESS]
    # 用契约模型而不是裸 dict 返回：字段名/类型由同一个地方定义，
    # 前端按 schema 读、后端按 schema 序列化，中间少一层手抄。
    from ling.contracts.v1 import (
        ConnectorActivationOut,
        ConnectorFieldOut,
        ConnectorsOut,
    )

    def _activation(payload: dict[str, object]) -> ConnectorActivationOut:
        return ConnectorActivationOut.model_validate(payload)

    def _fields(items: list[dict[str, object]]) -> list[ConnectorFieldOut]:
        return [ConnectorFieldOut.model_validate(item) for item in items]

    connector_views_out: list[ConnectorView] = []
    for view in merged:
        channel_id = str(view["id"])
        connector_views_out.append(
            ConnectorView(
                id=channel_id,
                label=str(view["label"]),
                configured=bool(view["configured"]),
                enabled=bool(view["enabled"]),
                status=str(view["status"]),
                status_reason=str(view["status_reason"]),
                status_message=str(view["status_message"]),
                guidance=str(view["guidance"]),
                editable=is_owner,
                storage="db",
                generation=int(activations[channel_id]["generation"]),  # type: ignore[call-overload]
                activation=_activation(activations[channel_id]),
                fields=_fields(field_views.get(channel_id, [])),
                runtime=_runtime_of(view),
            )
        )

    return ConnectorsOut(
        as_of=utcnow(),
        can_edit=is_owner,
        # 回显**逐 scope 实际读取**的实例（不是入参）：`live` 是解析后的映射，
        # 某个 scope 上没有请求的那个实例时会退回默认实例，这里如实呈现。
        # 前端据此断言"我看到的这份数据属于我在看的那台"。
        instance_id=live[cf.SCOPE_ONEBOT],
        instances=dict(live),
        connectors=connector_views_out,
        access=ConnectorAccessGroupOut(
            generation=int(access["generation"]),  # type: ignore[call-overload]
            activation=_activation(access),
            editable=is_owner,
            fields=_fields(field_views.get(cf.SCOPE_ACCESS, [])),
        ),
    )


def _cleared_keys(results: list[dict[str, object]]) -> list[str]:
    """reset 结果里所有被清掉的键（审计用）。"""
    out: list[str] = []
    for result in results:
        raw = result.get("cleared")
        if isinstance(raw, list):
            out.extend(str(item) for item in raw)
    return out


def _runtime_of(item: dict[str, object]) -> dict[str, int | None]:
    """把渠道视图的 ``runtime`` 规整成契约要求的 ``dict[str, int | None]``。"""
    raw = item.get("runtime")
    if not isinstance(raw, dict):
        return {}
    return {str(key): (value if isinstance(value, int) else None) for key, value in raw.items()}


class ConnectorChangeBody(BaseModel):
    """``PUT /v1/settings/connectors/{scope}`` 的请求体（设计 §6）。

    ``set`` 里没出现的键 = 不改；密钥给空串 = **不改**（密码框总是空的，
    不能因此变成一次删除）；``clear`` = 删掉该键的覆盖、回退到 env。
    同一个键同时出现在两边 → 422。
    """

    set: dict[str, object] = Field(default_factory=dict)
    clear: list[str] = Field(default_factory=list)


def _require_connector_write_guards(request: Request, auth: object) -> None:
    """owner + 同源（复用更新通道那套门禁，不自创弱校验）。

    为什么不能只判 ``role == owner``：cookie 会话被 CSRF 借用时，"当前用户是
    owner"这条依然成立，所以还要校验 **Origin 同源**。
    """
    from ling.api.routes.version import (  # noqa: PLC2701 - 同一进程内的门禁复用
        _origin_problem,
        _require_json_content_type,
    )

    if str(getattr(getattr(auth, "user", None), "role", "")) != "owner":
        raise LingError(
            ErrorCode.forbidden,
            "只有所有者可以修改接入器配置",
            status_code=403,
            detail="not_owner",
        )
    _require_json_content_type(request)
    problem = _origin_problem(request, request.app.state.settings)
    if problem is not None:
        # 跨站 / 缺失 Origin 一律拒绝（设计 §6：``bad_origin``）。
        raise LingError(
            ErrorCode.forbidden,
            "这个请求的来源不可信",
            status_code=403,
            detail="bad_origin",
        )


def _connector_service(request: Request, auth: object, session: AsyncSession):  # noqa: ANN202
    from ling.settings.connector_service import ConnectorConfigService

    app_state = request.app.state
    return ConnectorConfigService(
        settings=app_state.settings,
        box=getattr(app_state, "secret_box", None),
        runtime=getattr(app_state, "connector_runtime", None),
    )


# ------------------------------------------------------------------ 实例目录
#
# 路径挂在 `/settings/connectors/{scope}/instances` 下，而不是另起一个
# `/settings/connector-instances`：实例**只在 scope 内存在**（主键是
# (scope, instance_id)），把 scope 放进前缀后，路径本身就是那个命名空间，
# 不会出现"列表里某行的 scope 和路径里的 scope 不一致"这种形状。
#
# 用 POST/非 PUT 的语义：创建是原语（不幂等），改名走 PUT 在子资源上。


class ConnectorInstanceCreateBody(BaseModel):
    display_name: str = Field(min_length=1, max_length=64)
    #: 不给就按显示名派生（中文名派生不出时用随机后缀）。**建后不可变**。
    instance_id: str | None = Field(default=None, min_length=1, max_length=16)
    enabled: bool = True


class ConnectorInstanceRenameBody(BaseModel):
    display_name: str = Field(min_length=1, max_length=64)


class ConnectorInstanceEnabledBody(BaseModel):
    enabled: bool


def _instance_error(exc: object) -> LingError:
    """实例目录错误 → ``LingError``。

    ``ErrorCode`` 是受控枚举，不加新值：404 映射 ``not_found``，其余冲突
    （重名 / 已删 / 默认实例受保护）映射 ``conflict``，具体原因放 ``detail``。
    """
    status_code = int(getattr(exc, "status_code", 422))
    code = {
        404: ErrorCode.not_found,
        409: ErrorCode.conflict,
    }.get(status_code, ErrorCode.invalid_request)
    return LingError(
        code,
        str(getattr(exc, "message", str(exc))),
        status_code=status_code,
        detail=str(getattr(exc, "code", "")),
        field=getattr(exc, "name", None),
    )


def _require_instance_scope(scope: str) -> None:
    from ling.settings import connector_fields as cf

    if not cf.is_known_scope(scope):
        raise LingError(ErrorCode.not_found, "没有这个接入器", status_code=404)


@router.get("/settings/connectors/{scope}/instances")
async def list_connector_instances(
    scope: str,
    auth: CurrentAuth,
    session: DbSession,
    include_deleted: bool = False,
) -> dict[str, object]:
    """列出某个接入器下的实例。

    默认**不含已软删的**：实例选择器不该出现已经删掉的名字。
    ``?include_deleted=true`` 用于"历史配置还能查"的那条路径。

    必须鉴权：实例名本身是部署指纹（"她挂了哪几个号"）。
    """
    from ling.settings.connector_instances import ConnectorInstanceService, instance_view

    _require_instance_scope(scope)
    rows = await ConnectorInstanceService(session).list_instances(
        scope=scope, include_deleted=include_deleted
    )
    return {
        "schema_version": "v1",
        "scope": scope,
        "items": [instance_view(row) for row in rows],
        "include_deleted": include_deleted,
    }


@router.post("/settings/connectors/{scope}/instances", status_code=201)
async def create_connector_instance(
    scope: str,
    body: ConnectorInstanceCreateBody,
    request: Request,
    auth: CurrentAuth,
    session: DbSession,
    response: Response,
) -> dict[str, object]:
    """建一个实例（owner only）。

    **真的建行**：``connector_instances`` 里会出现 ``(scope, instance_id)``。
    建完之后就可以拿这个 ``instance_id`` 去 ``PUT /settings/connectors/{scope}
    ?instance_id=...`` 保存配置——配置保存路径认的就是它。

    同 scope 下显示名重复 → 409 ``duplicate_display_name``（**不是 500**）。
    """
    from ling.settings.connector_instances import (
        ConnectorInstanceError,
        ConnectorInstanceService,
        instance_view,
    )

    _require_connector_write_guards(request, auth)
    _require_instance_scope(scope)
    service = ConnectorInstanceService(session)
    try:
        row = await service.create(
            scope=scope,
            display_name=body.display_name,
            instance_id=body.instance_id,
            enabled=body.enabled,
            created_by=str(getattr(getattr(auth, "user", None), "subject_id", "") or None),
        )
    except ConnectorInstanceError as exc:
        raise _instance_error(exc) from exc
    await session.commit()
    audit_event(
        "connector_instance_created",
        actor=str(getattr(getattr(auth, "user", None), "subject_id", "") or "owner"),
        target_type="connector_instance",
        target_id=f"{scope}/{row.instance_id}",
        attributes={"scope": scope, "instance_id": row.instance_id},
    )
    response.headers["Cache-Control"] = "no-store"
    return {"schema_version": "v1", "item": instance_view(row)}


@router.put("/settings/connectors/{scope}/instances/{instance_id}")
async def rename_connector_instance(
    scope: str,
    instance_id: str,
    body: ConnectorInstanceRenameBody,
    request: Request,
    auth: CurrentAuth,
    session: DbSession,
    response: Response,
) -> dict[str, object]:
    """改显示名。**实例身份不变**——改的是 ``display_name``，不是 ``instance_id``。"""
    from ling.settings.connector_instances import (
        ConnectorInstanceError,
        ConnectorInstanceService,
        instance_view,
    )

    _require_connector_write_guards(request, auth)
    _require_instance_scope(scope)
    service = ConnectorInstanceService(session)
    try:
        row = await service.rename(
            scope=scope, instance_id=instance_id, display_name=body.display_name
        )
    except ConnectorInstanceError as exc:
        raise _instance_error(exc) from exc
    await session.commit()
    audit_event(
        "connector_instance_renamed",
        actor=str(getattr(getattr(auth, "user", None), "subject_id", "") or "owner"),
        target_type="connector_instance",
        target_id=f"{scope}/{instance_id}",
        attributes={"scope": scope, "instance_id": instance_id},
    )
    response.headers["Cache-Control"] = "no-store"
    return {"schema_version": "v1", "item": instance_view(row)}


@router.put("/settings/connectors/{scope}/instances/{instance_id}/enabled")
async def set_connector_instance_enabled(
    scope: str,
    instance_id: str,
    body: ConnectorInstanceEnabledBody,
    request: Request,
    auth: CurrentAuth,
    session: DbSession,
    response: Response,
) -> dict[str, object]:
    """启用/停用。软删过的实例**不能**再启用（409 ``instance_deleted``）。

    停用**不动**已保存的配置与修订链：它表达的是"这个实例现在不参与运行"，
    而不是"忘掉它的配置"。
    """
    from ling.settings.connector_instances import (
        ConnectorInstanceError,
        ConnectorInstanceService,
        instance_view,
    )

    _require_connector_write_guards(request, auth)
    _require_instance_scope(scope)
    service = ConnectorInstanceService(session)
    try:
        row = await service.set_enabled(
            scope=scope, instance_id=instance_id, enabled=body.enabled
        )
    except ConnectorInstanceError as exc:
        raise _instance_error(exc) from exc
    await session.commit()
    audit_event(
        "connector_instance_toggled",
        actor=str(getattr(getattr(auth, "user", None), "subject_id", "") or "owner"),
        target_type="connector_instance",
        target_id=f"{scope}/{instance_id}",
        attributes={"scope": scope, "instance_id": instance_id, "enabled": bool(body.enabled)},
    )
    response.headers["Cache-Control"] = "no-store"
    return {"schema_version": "v1", "item": instance_view(row)}


@router.delete("/settings/connectors/{scope}/instances/{instance_id}")
async def delete_connector_instance(
    scope: str,
    instance_id: str,
    request: Request,
    auth: CurrentAuth,
    session: DbSession,
    response: Response,
) -> dict[str, object]:
    """**软删**（置 ``deleted_at``）。修订链与审计留着可查。

    默认实例 ``'default'`` **不能删**：存量数据全部折叠在它上面，删了之后
    读取路径（``connector_service.py`` 里按 ``(scope, 'default')`` 取 HEAD）
    会在"实例已删除"的状态下继续工作，没有任何一处会报错。
    """
    from ling.settings.connector_instances import (
        ConnectorInstanceError,
        ConnectorInstanceService,
        instance_view,
    )

    _require_connector_write_guards(request, auth)
    _require_instance_scope(scope)
    service = ConnectorInstanceService(session)
    try:
        row = await service.soft_delete(scope=scope, instance_id=instance_id)
    except ConnectorInstanceError as exc:
        raise _instance_error(exc) from exc
    await session.commit()
    audit_event(
        "connector_instance_deleted",
        actor=str(getattr(getattr(auth, "user", None), "subject_id", "") or "owner"),
        target_type="connector_instance",
        target_id=f"{scope}/{instance_id}",
        attributes={"scope": scope, "instance_id": instance_id},
    )
    response.headers["Cache-Control"] = "no-store"
    return {"schema_version": "v1", "item": instance_view(row)}


@router.put("/settings/connectors/{scope}")
async def put_connector_config(
    scope: str,
    body: ConnectorChangeBody,
    request: Request,
    auth: CurrentAuth,
    session: DbSession,
    response: Response,
    instance_id: str | None = None,
) -> dict[str, object]:
    """保存某个接入器**某个实例**的配置并**立刻尝试生效**（owner only）。

    语义：受理 ≠ 生效。``202`` 表示切换已完成（单副本部署里它同时也是终态）；
    ``409`` 是代际冲突/另一处正在生效/实例已删；``422`` 是字段校验失败——**此时
    一个连接都没停过**；``503`` 是切换器不可用。真正的失败（新配置起不来）会以
    ``rolled_back`` 返回，并保证旧配置与旧连接已经恢复。

    ``instance_id`` 默认 ``'default'``：老前端不带这个参数时行为与升级前完全
    一致（存量配置全部折叠在默认实例上）。实例必须**存在且未软删**——否则
    "保存到一个已删除的实例"会静默写进一张没人看得到的修订链。
    """
    from ling.settings import connector_fields as cf
    from ling.settings.connector_instances import (
        ConnectorInstanceError,
        ConnectorInstanceService,
    )
    from ling.settings.connector_service import (
        OP_ACTIVE,
        OP_DEGRADED,
        OP_ROLLED_BACK,
        OP_UNCHANGED,
        ChangeRequest,
        ConnectorServiceError,
    )

    _require_connector_write_guards(request, auth)
    if not cf.is_known_scope(scope):
        raise LingError(ErrorCode.not_found, "没有这个接入器", status_code=404)

    # 实例必须活着。
    #
    # 分两种情况，**故意不对称**：
    #
    # - 调用方**没带** ``instance_id``：落到默认实例，且默认实例不存在时补建
    #   一行。理由是升级前保存配置从不要求实例行（``_write_candidate`` 自己
    #   建 HEAD），迁移也只给"有过数据"的 scope 回填——全新部署表是空的，
    #   硬性 404 会把"第一次保存"弄坏。
    # - 调用方**显式指定**了 ``instance_id``：必须真的存在且未软删，查不到就
    #   404。**不**静默退回默认实例——那会让"保存到实例 B"变成"覆盖了默认
    #   实例"，而写入方拿到的是一份正常的 202。
    instances = ConnectorInstanceService(session)
    target_instance = instance_id or DEFAULT_INSTANCE_ID
    try:
        if instance_id is None:
            await instances.ensure_default(scope=scope)
        await instances.require_live(scope=scope, instance_id=target_instance)
    except ConnectorInstanceError as exc:
        raise LingError(
            ErrorCode.not_found if exc.status_code == 404 else ErrorCode.conflict,
            exc.message,
            status_code=exc.status_code,
            detail=exc.code,
            field=exc.name,
        ) from exc

    # If-Match：设计 §6 要求并发保存只有一个赢家。
    raw_generation = request.headers.get("if-match") or request.headers.get("If-Match")
    expected: int | None = None
    if raw_generation is not None:
        try:
            expected = int(str(raw_generation).strip().strip('"'))
        except ValueError as exc:
            raise LingError(
                ErrorCode.invalid_request,
                "If-Match 必须是代际号（整数）",
                status_code=422,
                detail="invalid_field",
            ) from exc

    try:
        change = ChangeRequest.build(scope, body.model_dump())
    except ConnectorServiceError as exc:
        raise LingError(
            ErrorCode.invalid_request,
            exc.message,
            status_code=exc.status_code,
            detail=exc.code,
            field=exc.field_name,
        ) from exc

    service = _connector_service(request, auth, session)
    try:
        result = await service.apply(
            session,
            scope=scope,
            instance_id=target_instance,
            change=change,
            expected_generation=expected,
            actor_id=str(getattr(getattr(auth, "user", None), "subject_id", "") or ""),
        )
    except ConnectorServiceError as exc:
        # `ErrorCode` 是**受控枚举**：409 的两种具体原因（代际冲突 / 另一处
        # 正在生效）不能当成新的错误码塞进去——那会让契约多出两个没人映射的
        # 取值。所以 code 用 `conflict`，具体原因放 `detail`（稳定字符串）。
        raise LingError(
            ErrorCode.conflict if exc.status_code == 409 else ErrorCode.invalid_request,
            exc.message,
            status_code=exc.status_code,
            detail=exc.code,
            field=exc.field_name,
        ) from exc

    # 审计：只记字段名 + 指纹 + 结果，**绝不写值或预览**（设计 §6）。
    if result.changed or result.state not in {OP_UNCHANGED}:

        audit_event(
            "connector_config_changed",
            outcome="ok" if result.state == OP_ACTIVE else "degraded",
            actor=str(getattr(getattr(auth, "user", None), "subject_id", "") or "owner"),
            target_type="connector",
            target_id=scope,
            attributes={
                "scope": scope,
                # 多实例下**必须记实例**：否则审计只能答"onebot 被改过"，
                # 答不了"是哪个机器人被改过"——而这正是出事后要查的那一句。
                # 记解析后的实例（缺省时是补建出来的 default），不是未解析的入参。
                "instance_id": target_instance,
                "fields": sorted(set(body.set) | set(body.clear)),
                "action": {
                    key: ("set" if key in body.set else "clear")
                    for key in sorted(set(body.set) | set(body.clear))
                },
                "revision_id": result.revision_id,
                "generation": result.generation,
                "state": result.state,
                "error_code": result.error_code,
                # 指纹由 service 在**写入前**算好（旧值>新值）；
                # 这里再读库只会读到已经覆盖后的值，两边一样。
                "fingerprints": result.fingerprints,
            },
        )
    if result.state == OP_ACTIVE:
        audit_event(
            "connector_activated",
            actor=str(getattr(getattr(auth, "user", None), "subject_id", "") or "owner"),
            target_type="connector",
            target_id=scope,
            attributes={"generation": result.generation, "revision_id": result.revision_id},
        )
    elif result.state in {OP_ROLLED_BACK, OP_DEGRADED}:
        audit_event(
            "connector_rolled_back",
            outcome="degraded",
            target_type="connector",
            target_id=scope,
            reason_code=str(result.error_code or ""),
            attributes={"generation": result.generation, "revision_id": result.revision_id},
        )

    # 202 = 受理并**尝试过**（成功或已回滚都算做过事）；200 = 什么都没变。
    # 注意 202 不等于"已生效"：终态看 `state`（active/rolled_back/degraded）。
    response.status_code = 200 if result.state == OP_UNCHANGED else 202
    response.headers["Cache-Control"] = "no-store"
    return {
        "schema_version": "v2",
        "operation_id": result.operation_id,
        "scope": scope,
        "instance_id": target_instance,
        "generation": result.generation,
        "status": result.state,
        "state": result.state,
        "changed": result.changed,
        "revision_id": result.revision_id,
        "active_revision_id": result.active_revision_id,
        "ack_count": result.ack_count,
        "expected_count": result.expected_count,
        "started_at": result.started_at.isoformat(),
        "finished_at": result.finished_at.isoformat(),
        "error_code": result.error_code,
        "error_safe": result.error_safe,
        # 量化中断窗口：不承诺零丢消息，只如实给出几个时刻（设计 §4）
        "timings": result.timings,
    }


@router.get("/settings/connectors/operations/{operation_id}")
async def get_connector_operation(
    operation_id: str, auth: CurrentAuth, session: DbSession
) -> dict[str, object]:
    """按 operation_id 查一次生效操作的状态（owner only，设计 §6）。

    202 不等于已生效：客户端拿这里返回的 ``active / rolled_back / degraded``
    才是终态。
    """
    from ling.settings.connector_service import operation_view

    if str(getattr(auth.user, "role", "")) != "owner":
        raise LingError(
            ErrorCode.forbidden, "只有所有者可以查看", status_code=403, detail="not_owner"
        )
    view = await operation_view(session, operation_id)
    if view is None:
        raise LingError(ErrorCode.not_found, "没有这个操作", status_code=404)
    return view


@router.post("/settings/connectors/reset")
async def reset_connector_config(
    request: Request,
    auth: CurrentAuth,
    session: DbSession,
    response: Response,
) -> dict[str, object]:
    """把三个 scope 的覆盖全部清空（回退到 env / 默认）。

    一个操作跟踪三个 scope：**全部成功**才算 reset 成功；任一 scope 重建失败
    就保留原 active，绝不让"部分恢复默认"被报成成功（设计 §6）。
    """
    from ling.settings import connector_fields as cf
    from ling.settings.connector_service import (
        OP_DEGRADED,
        OP_ROLLED_BACK,
        OP_UNCHANGED,
        ChangeRequest,
        ConnectorServiceError,
    )

    _require_connector_write_guards(request, auth)
    service = _connector_service(request, auth, session)
    results: list[dict[str, object]] = []
    failed = False
    for scope in cf.SCOPES:
        specs = cf.fields_for(scope)
        current = await service.resolved(session, scope=scope, include_secrets=False)
        to_clear = [
            spec.key
            for spec in specs
            if current.fields.get(spec.key) is not None
            and current.fields[spec.key].source == cf.SOURCE_DB
        ]
        if not to_clear:
            results.append({"scope": scope, "state": OP_UNCHANGED, "cleared": []})
            continue
        change = ChangeRequest(set_values={}, clear_keys=tuple(to_clear))
        try:
            result = await service.apply(
                session,
                scope=scope,
                change=change,
                expected_generation=None,
                actor_id=str(getattr(auth.user, "subject_id", "") or ""),
            )
        except ConnectorServiceError as exc:
            failed = True
            results.append(
                {
                    "scope": scope,
                    "state": "failed",
                    "cleared": [],
                    "error_code": exc.code,
                    "error_safe": exc.message,
                }
            )
            continue
        if result.state in {OP_ROLLED_BACK, OP_DEGRADED}:
            failed = True
        results.append(
            {
                "scope": scope,
                "state": result.state,
                "cleared": to_clear,
                "operation_id": result.operation_id,
                "generation": result.generation,
            }
        )
    audit_event(
        "connector_config_changed",
        outcome="degraded" if failed else "ok",
        actor=str(getattr(auth.user, "subject_id", "") or "owner"),
        target_type="connector",
        target_id="all",
        attributes={
            "scope": "all",
            "action": "reset",
            "fields": _cleared_keys(results),
            "state": "failed" if failed else "reset",
        },
    )
    response.status_code = 200
    return {"schema_version": "v2", "ok": not failed, "results": results}


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
        # 光比"生效模型"不够：**绑定方式本身也会被快速配置改掉**。
        # 用户单独指定了 X，而槽位恰好也是 X 时，模型没变、但他的选择被换成了
        # "跟随默认"——以后改槽位它就会跟着漂。只比模型会把它报成"与现在相同"，
        # 点了才知道发生了改动，这和不给 diff 没区别。
        old_binding = before.profiles.get(profile)
        new_binding = after.profiles.get(profile)
        mode_changed = (
            old_binding is not None
            and new_binding is not None
            and old_binding.mode is not new_binding.mode
        )
        # 旧版兼容是**另一条轴**：那个模型既不是用户选的、也不是槽位来的。
        # 文案上必须分开，否则会写成"覆盖你选的 X"，而用户从没选过 X。
        if new.source is ResolutionSource.legacy_fallback:
            kind = "legacy"
            # 文案要短：这一列桌面约 390px、375 的堆叠布局里只有 ~250px，
            # 长句会折出 2–3 字孤行（验收 G2 实测末行「配）」）。原因说一半
            # 就够——"槽位没配"已经解释了为什么给不了它模型。
            change = (
                f"仍然借主对话 {new_model['model_id'] if new_model else ''}"
                f"（{SLOT_LABELS[key]}槽位没配）"
            )
        elif new_model is None:
            change = f"仍然不可用：{new.reason}"
            kind = "unavailable"
        elif old_model is None:
            change = "修复不可用"
            kind = "repair"
        elif old_model == new_model and not mode_changed:
            change = "与现在相同"
            kind = "same"
        elif old_model == new_model and mode_changed:
            # 模型一样但绑定方式变了：说清"你的单独指定会被改成跟随默认"，
            # 而不是含糊地说"会变"。
            change = (
                f"模型仍是 {new_model['model_id']}，但你的单独指定会被改成"
                f"「跟随默认 · {SLOT_LABELS[key]}」"
            )
            kind = "override"
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
