"""默认槽位与「生效模型」的判定：只回退一层，不允许套娃。

## 这个文件守的是什么

设置页上每一行最右边有一列「实际生效」。它必须**永远等于**运行时会调的
模型——否则用户按着界面排查，方向从一开始就是错的。

所以回退规则只能有一份实现（`EffectiveConfig.resolve_profile`），并且必须
满足四条：

1. 单独指定了、且那个 `(provider, model)` 可用 → 用它。
2. 否则用它**所属槽位**的模型。
3. 槽位没配 / 不可用 → 不可用，并说清断在哪。
4. **到此为止**：不试别的槽位，也不借别的角色的模型。

第 4 条最容易在"顺手补个兜底"的时候被破坏，所以这里专门钉住它：
`memory.extract` 的槽位（创作）没配时，即使「快速」槽位配得好好的，
它也必须是不可用——**不能**再去借主对话的模型。
"""

from __future__ import annotations

import pytest

from ling.contracts.v1 import ModelRole
from ling.ports.config import (
    BindingMode,
    EffectiveConfig,
    ModelSlot,
    ProfileBinding,
    ProviderConfig,
    ProviderModel,
    ResolutionSource,
)
from ling.ports.model import (
    PROFILE_CAPABILITIES,
    PROFILE_SLOTS,
    SLOT_HINTS,
    SLOT_LABELS,
    SLOT_ORDER,
    ModelCapability,
    ProviderSlot,
)

FAST = ProviderConfig(
    provider_id="fast_provider",
    kind="openai_compatible",
    models=(ProviderModel("qwen-fast"),),
)
VISION = ProviderConfig(
    provider_id="vision_provider",
    kind="openai_compatible",
    models=(ProviderModel("sensenova-vision"),),
)


def _config(
    *,
    providers: tuple[ProviderConfig, ...] = (FAST, VISION),
    profiles: tuple[ProfileBinding, ...] = (),
    slots: dict[str, ModelSlot] | None = None,
) -> EffectiveConfig:
    return EffectiveConfig(
        revision=1,
        providers={p.provider_id: p for p in providers},
        profiles={b.profile: b for b in profiles},
        slots=slots
        if slots is not None
        else {"fast": ModelSlot("fast", "fast_provider", "qwen-fast")},
    )


# ---------------------------------------------------------------- 映射表本身


def test_every_slot_has_label_hint_and_order() -> None:
    """四个槽位一个都不能少：少一个就意味着某些职责永远没有默认值。"""
    assert {slot.value for slot in SLOT_ORDER} == {member.value for member in ProviderSlot}
    for slot in SLOT_ORDER:
        assert SLOT_LABELS[slot].strip(), f"{slot} 没有名字"
        # 名字旁边的半句话要能分清「快速」和「日常」，太短等于没说
        assert len(SLOT_HINTS[slot]) >= 6, f"{slot} 的说明太短：{SLOT_HINTS[slot]}"


def test_profile_slots_keys_are_real_roles() -> None:
    """映射表里不能有拼错的角色名——拼错的那条永远不会被匹配，静默失效。"""
    known = {role.value for role in ModelRole}
    for profile in PROFILE_SLOTS:
        assert profile in known, f"PROFILE_SLOTS 里有不存在的角色：{profile}"


def test_slot_roles_are_chat_roles() -> None:
    """跟随槽位的角色必须都要 `chat` 能力。

    槽位里放的是对话模型（`_slot_issues` 按 `chat` 能力校验）。要是有个
    嵌入角色挂进槽位表，它会拿到一个 chat 模型，然后静默失效。
    """
    for profile in PROFILE_SLOTS:
        assert PROFILE_CAPABILITIES.get(profile) is ModelCapability.chat, (
            f"{profile} 跟着槽位走，但它要的不是 chat 能力"
        )


def test_embed_and_rerank_have_no_slot() -> None:
    """嵌入/重排**不能**进槽位：它们要的是另一套协议。

    把它们指到 chat 槽位上，界面上看起来配好了，实际调用必失败。
    """
    assert "memory.embed" not in PROFILE_SLOTS
    assert "memory.rerank" not in PROFILE_SLOTS


def test_video_role_follows_its_own_slot() -> None:
    """看视频跟随「视频」槽位，**不**跟识图共用。

    合成一个槽位，用户就只能在"图片配便宜的视觉模型"和"视频配能读多帧的
    模型"之间二选一。能力仍按 `chat` 判（见
    `test_provider_capabilities.py::test_perception_video_uses_chat_capability`）。
    """
    assert PROFILE_SLOTS["perception.video"] is ProviderSlot.video
    assert PROFILE_CAPABILITIES["perception.video"] is ModelCapability.chat
    assert SLOT_LABELS[ProviderSlot.video] == "视频"
    # 说明里必须写清"能看懂视频"：这是用户挑模型的唯一依据
    assert "视频" in SLOT_HINTS[ProviderSlot.video]
    assert "看懂" in SLOT_HINTS[ProviderSlot.video]


# ---------------------------------------------------------------- 四条规则


def test_explicit_and_usable_wins() -> None:
    """规则 1：单独指定且可用 → 用它，槽位不参与。"""
    config = _config(
        profiles=(
            ProfileBinding(
                profile="dialogue.fast", provider_id="vision_provider",
                model_id="sensenova-vision",
            ),
        ),
    )
    resolved = config.resolve_profile("dialogue.fast")
    assert resolved.source is ResolutionSource.explicit
    assert (resolved.provider_id, resolved.model_id) == (
        "vision_provider",
        "sensenova-vision",
    )
    assert resolved.reason == ""


@pytest.mark.parametrize(
    "provider",
    [
        # 供应商被停用
        ProviderConfig(
            provider_id="fast_provider",
            kind="openai_compatible",
            enabled=False,
            models=(ProviderModel("qwen-fast"),),
        ),
        # 模型被停用
        ProviderConfig(
            provider_id="fast_provider",
            kind="openai_compatible",
            models=(ProviderModel("qwen-fast", enabled=False),),
        ),
    ],
    ids=["provider_disabled", "model_disabled"],
)
def test_broken_explicit_falls_back_to_its_own_slot(provider: ProviderConfig) -> None:
    """规则 2：单独指定的坏了 → 用**自己那个**槽位，并记下被丢掉的选择。

    `requested_*` 是「已回退」这个红标的唯一依据：没有它，界面分不出
    「从来没单独选过」（继承）和「选了但坏了」（已回退）。
    """
    config = _config(
        providers=(provider, VISION),
        profiles=(
            ProfileBinding(
                profile="dialogue.fast", provider_id="fast_provider", model_id="qwen-fast"
            ),
        ),
        slots={
            "fast": ModelSlot("fast", "vision_provider", "sensenova-vision"),
        },
    )
    resolved = config.resolve_profile("dialogue.fast")
    assert resolved.source is ResolutionSource.slot
    assert (resolved.provider_id, resolved.model_id) == (
        "vision_provider",
        "sensenova-vision",
    )
    assert resolved.requested_provider_id == "fast_provider"
    assert resolved.requested_model_id == "qwen-fast"
    assert resolved.fell_back is True


def test_unconfigured_slot_is_unavailable_with_reason() -> None:
    """规则 3：槽位没配 → 不可用，原因要指出**哪个槽位**。"""
    config = _config(profiles=(), slots={})
    resolved = config.resolve_profile("perception.vision")
    assert resolved.source is ResolutionSource.unavailable
    assert resolved.provider_id is None
    assert "识图" in resolved.reason, resolved.reason
    assert resolved.usable is False


def test_slot_pointing_at_disabled_provider_is_unavailable() -> None:
    """规则 3：槽位配了但不可用 → 同样不可用，并说清是哪个模型不可用。"""
    disabled = ProviderConfig(
        provider_id="vision_provider",
        kind="openai_compatible",
        enabled=False,
        models=(ProviderModel("sensenova-vision"),),
    )
    config = _config(
        providers=(FAST, disabled),
        slots={"vision": ModelSlot("vision", "vision_provider", "sensenova-vision")},
    )
    resolved = config.resolve_profile("perception.vision")
    assert resolved.source is ResolutionSource.unavailable
    assert "sensenova-vision" in resolved.reason, resolved.reason


def test_no_second_hop_across_slots() -> None:
    """规则 4（**最重要的一条**）：槽位没配时**不许跳到别的槽位**。

    这里「快速」槽位配得妥妥的，而 `memory.extract` 跟着的「创作」没配。
    它**不能**因为「快速」有模型就拿去用——那会让设置页的"跟着创作"变成一句
    谎话，而且用户永远猜不到实际跑的是谁。
    """
    config = _config(
        slots={"fast": ModelSlot("fast", "fast_provider", "qwen-fast")},
        profiles=(),
    )
    assert config.resolve_profile("dialogue.fast").usable is True

    resolved = config.resolve_profile("memory.extract")
    # 走的是**旧版兼容**那一档，不是槽位：source 明确标出来，界面才能说清
    # "这个模型不是你要的，是旧版行为在兜"。
    assert resolved.source is ResolutionSource.legacy_fallback
    assert resolved.slot == "creation"
    assert resolved.model_id == "qwen-fast"
    assert "旧版行为" in resolved.reason, resolved.reason


def test_legacy_fallback_never_chains() -> None:
    """旧版兼容**只借一层、且只借 dialogue.fast**，主对话自己都没模型就报不可用。

    防的是"借不到再往下借"：那条链一旦成立，界面上的"实际生效"就没人能解释了。
    """
    # 什么都没配：连 dialogue.fast 自己都不可用
    config = _config(slots={}, profiles=())
    resolved = config.resolve_profile("memory.extract")
    assert resolved.source is ResolutionSource.unavailable
    assert resolved.provider_id is None


def test_legacy_fallback_respects_explicit_disable() -> None:
    """用户**主动停用**这个职责时，兼容逻辑不许把它"修好"。"""
    config = _config(
        slots={"fast": ModelSlot("fast", "fast_provider", "qwen-fast")},
        profiles=(
            ProfileBinding(
                profile="memory.extract",
                provider_id="",
                model_id="",
                enabled=False,
            ),
        ),
    )
    resolved = config.resolve_profile("memory.extract")
    assert resolved.source is ResolutionSource.unavailable
    assert resolved.enabled is False


def test_role_without_slot_cannot_be_satisfied_by_any_slot() -> None:
    """没有默认槽位的角色（嵌入/重排）不会因为"槽位配好了"而变得可用。"""
    config = _config(
        slots={
            "fast": ModelSlot("fast", "fast_provider", "qwen-fast"),
            "creation": ModelSlot("creation", "fast_provider", "qwen-fast"),
            "vision": ModelSlot("vision", "vision_provider", "sensenova-vision"),
            "daily": ModelSlot("daily", "fast_provider", "qwen-fast"),
        },
    )
    for profile in ("memory.embed", "memory.rerank"):
        resolved = config.resolve_profile(profile)
        assert resolved.source is ResolutionSource.unavailable
        assert "只能单独指定" in resolved.reason, resolved.reason


# ---------------------------------------------------------------- 边界情况


def test_slot_mode_ignores_leftover_provider_and_model() -> None:
    """`mode=slot` 时，绑定里的残留值不算数。

    保存时 `_apply_patch` 会把它们清空；万一没清干净（旧数据、手工改库），
    解析也不能拿它当显式选择——否则"跟随默认"会跟着一个看不见的旧值。
    """
    config = _config(
        profiles=(
            ProfileBinding(
                profile="dialogue.group",
                provider_id="vision_provider",
                model_id="sensenova-vision",
                mode=BindingMode.slot,
            ),
        ),
        slots={"daily": ModelSlot("daily", "fast_provider", "qwen-fast")},
    )
    resolved = config.resolve_profile("dialogue.group")
    assert resolved.source is ResolutionSource.slot
    assert resolved.model_id == "qwen-fast"
    assert resolved.requested_provider_id is None


def test_disabled_profile_is_not_rescued_by_its_slot() -> None:
    """职责被停用是**用户的决定**，不该被槽位"修好"。"""
    config = _config(
        profiles=(
            ProfileBinding(profile="dialogue.group", provider_id="", model_id="", enabled=False),
        ),
        slots={"daily": ModelSlot("daily", "fast_provider", "qwen-fast")},
    )
    resolved = config.resolve_profile("dialogue.group")
    assert resolved.source is ResolutionSource.unavailable
    assert resolved.enabled is False
    assert "停用" in resolved.reason


def test_model_missing_from_catalog_is_still_usable() -> None:
    """模型不在清单里 ≠ 不可用。

    清单是给人看的目录，不是路由白名单（见 `ProviderModel`）。把 id 写进
    绑定却忘了加进目录是常见状态，路由一直照调不误；判成不可用会让设置页
    凭空红一片，而实际调用完全正常。
    """
    config = _config(
        profiles=(
            ProfileBinding(
                profile="dialogue.fast", provider_id="fast_provider", model_id="未录入清单"
            ),
        )
    )
    resolved = config.resolve_profile("dialogue.fast")
    assert resolved.source is ResolutionSource.explicit
    assert resolved.model_id == "未录入清单"


def test_unknown_provider_is_unusable() -> None:
    """供应商根本没登记 → 不可用（不是"乐观放行"）。"""
    config = _config(
        profiles=(
            ProfileBinding(profile="dialogue.fast", provider_id="ghost", model_id="m"),
        ),
        slots={},
    )
    resolved = config.resolve_profile("dialogue.fast")
    assert resolved.source is ResolutionSource.unavailable


def test_usable_requires_both_provider_and_model_enabled() -> None:
    """「可用」的定义写在一处：供应商 enabled **且** 模型 enabled。"""
    config = _config()
    assert config.is_usable("fast_provider", "qwen-fast") is True
    assert config.is_usable("fast_provider", "") is False
    assert config.is_usable("", "qwen-fast") is False
    assert config.is_usable("ghost", "qwen-fast") is False
