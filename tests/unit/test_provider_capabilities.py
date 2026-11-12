"""能力映射的完整性：每个 kind 说得清自己能干什么，每个角色说得清自己要什么。

## 为什么需要这张表

设置页原来的下拉是"所有供应商 × 所有职责"的笛卡尔积。混进嵌入/重排之后，
这个下拉会让人选出**发布成功、调用必失败**的组合（协议完全不同）。所以先有
能力维度，再让绑定按能力过滤。

## 这里守什么

1. `ProviderKind` 的每个成员都必须声明能力——漏一个的后果是"那个 kind
   不提供任何能力"，所有绑定都会被校验拦下（失败关闭，但没人知道为什么）。
2. 可绑角色必须有明确的能力要求，且**至少有一个 kind 能提供它**。
   列一个谁都提供不了的角色，等于给用户一个永远选不中的下拉。
3. 能力名只能来自 `ModelCapability`——散落成裸字符串 "embed" 之后，
   拼错（"embeddings"）不会有任何报错，只会静默匹配不上。
"""

from __future__ import annotations

import pytest

from ling.contracts.v1 import ModelRole
from ling.ports.model import (
    PROFILE_CAPABILITIES,
    PROVIDER_CAPABILITIES,
    ModelCapability,
    ProviderKind,
    kind_supports_profile,
    provider_capabilities,
    required_capability,
)

# 对话能力的**原始**成员：这五个从一开始就能当 chat 用。
LEGACY_CHAT_KINDS = {"fake", "openai_compatible", "anthropic", "chat_responses", "local"}

# 对话能力的全部成员 = 老五个 + 小米 MiMo 的对话面。
#
# MiMo 走 `chat/completions`，所以它**确实能**当对话供应商用。但它与语音面
# 被拆成两个 kind（`mimo_chat` / `mimo_tts`），理由见 `ports/model.py` 里
# `ProviderKind.mimo_chat` 的长注释：一个 kind 同时持有 {chat, tts} 会让
# "把 TTS 供应商绑到聊天槽位"这种错配通过能力闸。
CHAT_KINDS = LEGACY_CHAT_KINDS | {"mimo_chat"}

# 本期新增的三家 TTS 供应商（MiMo 拆两面，语音面单独算一个）。
TTS_KINDS = {"minimax_tts", "fishaudio_tts", "mimo_tts"}


def test_every_provider_kind_declares_capabilities() -> None:
    """穷举：新增 kind 却忘了声明能力，会让它任何绑定都过不了校验。"""
    declared = set(PROVIDER_CAPABILITIES)
    assert declared == set(ProviderKind), (
        f"这些 kind 没有声明能力: {sorted(k.value for k in set(ProviderKind) - declared)}；"
        f"这些声明了不存在的 kind: {sorted(k.value for k in declared - set(ProviderKind))}"
    )


def test_capability_sets_are_not_empty() -> None:
    for kind, capabilities in PROVIDER_CAPABILITIES.items():
        assert capabilities, f"{kind.value} 声明了空能力集：它对外等于一个不能用的供应商"


def test_chat_capable_kinds_are_exactly_the_legacy_five() -> None:
    """chat 能力的成员不能悄悄变化。

    多一个：某个只做嵌入的 kind 被误标成能对话；少一个：原有对话供应商
    突然选不中。两种都只在用户那里暴露。
    """
    chat = {k.value for k, caps in PROVIDER_CAPABILITIES.items() if ModelCapability.chat in caps}
    assert chat == CHAT_KINDS


def test_embedding_and_rerank_kinds_only_serve_their_own_capability() -> None:
    assert PROVIDER_CAPABILITIES[ProviderKind.cloudflare_embedding] == {
        ModelCapability.embed
    }
    assert PROVIDER_CAPABILITIES[ProviderKind.siliconflow_rerank] == {
        ModelCapability.rerank
    }


def test_unknown_kind_offers_nothing() -> None:
    """未知 kind 失败关闭：空能力集，任何角色绑上去都会被拦下。"""
    assert provider_capabilities("no_such_kind") == frozenset()
    assert provider_capabilities("") == frozenset()


def test_profile_capabilities_reference_real_roles() -> None:
    """角色名必须真实存在（拼错的名字永远匹配不上，等于这条约束静默失效）。"""
    known = {role.value for role in ModelRole}
    unknown = set(PROFILE_CAPABILITIES) - known
    assert not unknown, f"PROFILE_CAPABILITIES 里有不存在的角色: {sorted(unknown)}"


def test_every_offerable_profile_has_a_capability() -> None:
    """可绑清单里的角色必须说清要什么能力，否则前端没法过滤下拉。"""
    from ling.api.routes.settings import _OFFERABLE_PROFILES

    missing = [p for p in _OFFERABLE_PROFILES if p not in PROFILE_CAPABILITIES]
    assert not missing, f"这些可绑角色没有能力声明: {missing}"


def test_every_offerable_profile_has_at_least_one_provider_kind() -> None:
    """每个可绑角色都至少有一种 kind 能服务它。

    反例是语音：`speech.tts` 要 tts 能力，而没有任何 kind 提供——所以它
    不在可绑清单里。以后若把它加进去而没实现 provider，这条会先炸。
    """
    from ling.api.routes.settings import _OFFERABLE_PROFILES

    for profile in _OFFERABLE_PROFILES:
        need = PROFILE_CAPABILITIES[profile]
        assert any(need in caps for caps in PROVIDER_CAPABILITIES.values()), (
            f"{profile} 需要「{need.value}」能力，但没有任何 kind 提供它"
        )


@pytest.mark.parametrize("profile", ["memory.embed", "memory.rerank"])
def test_memory_profiles_are_only_served_by_their_own_kind(profile: str) -> None:
    """嵌入/重排只能落在对应 kind 上，chat 供应商一律不匹配。"""
    for kind in CHAT_KINDS:
        assert not kind_supports_profile(kind, profile), (
            f"{profile} 不该落到 {kind} 上：协议完全不同，绑上去也调不通"
        )


def test_chat_profiles_are_served_by_chat_kinds_and_not_by_non_chat_kinds() -> None:
    assert kind_supports_profile("openai_compatible", "dialogue.fast")
    assert kind_supports_profile("fake", "memory.extract")
    assert not kind_supports_profile("cloudflare_embedding", "dialogue.fast")
    assert not kind_supports_profile("siliconflow_rerank", "memory.extract")


def test_perception_video_uses_chat_capability() -> None:
    """看视频要的是 `chat`，**不是**新造的 `video` 能力。

    这是一条刻意的取舍：能力表回答的是"这个**供应商类型**能不能服务这个
    角色"。现有对话 kind 的能力集里没有 `video`，新增一个没人能提供的能力
    会让 `perception.video` 判成"所有供应商都不匹配"——设置页那一行永远红、
    绑谁都说"这家不支持"。能不能看懂视频取决于**具体模型**（同一个 kind 下
    既有纯文本也有多模态），所以那句话写在槽位提示里，由用户挑。
    """
    assert required_capability("perception.video") is ModelCapability.chat
    assert "video" not in {c.value for c in ModelCapability}
    # 至少有一个 kind 能服务它，否则它不该出现在可绑清单里
    assert kind_supports_profile("openai_compatible", "perception.video")
    assert kind_supports_profile("fake", "perception.video")
    assert not kind_supports_profile("cloudflare_embedding", "perception.video")


def test_roles_without_declared_capability_are_unconstrained() -> None:
    """没有能力声明的角色不做约束（它们既不可绑，也没有任何代码请求）。"""
    assert required_capability("reflection") is None
    assert kind_supports_profile("fake", "reflection")


def test_capability_values_are_plain_words() -> None:
    """能力值是前端直接比较的字符串，必须与枚举名一致且稳定。"""
    assert {c.value for c in ModelCapability} == {"chat", "embed", "rerank", "tts", "asr"}


# --------------------------------------------------------------------------- #
# 三家 TTS 供应商与 MiMo 的「一厂两面」
# --------------------------------------------------------------------------- #


def test_tts_kinds_are_exactly_the_three_vendors() -> None:
    """提供 tts 能力的 kind 就是这三家，不多不少。

    多一个：某家只做对话的供应商被误标成能合成，用户绑上去每次调用都失败。
    少一个：用户配好了却在下拉里选不中。
    """
    tts = {k.value for k, caps in PROVIDER_CAPABILITIES.items() if ModelCapability.tts in caps}
    assert tts == TTS_KINDS


def test_no_kind_declares_both_chat_and_tts() -> None:
    """**核心约束**：没有任何 kind 同时提供 chat 和 tts。

    MiMo 走 `chat/completions`，天然能同时承担两种角色——但它必须在**配置**
    里表达成两条 provider，而不是一个 kind 带两个能力。

    为什么这条断言比"功能测试"更重要：`PROVIDER_CAPABILITIES` 是绑定校验的
    唯一能力闸（`kind_supports_profile` 只看它）。一旦某个 kind 同时持有
    {chat, tts}，`speech.tts` 绑到聊天供应商就会**通过**校验，fail-closed
    直接作废。这是契约层面的不变量，所以单独守一条。
    """
    both = {
        k.value
        for k, caps in PROVIDER_CAPABILITIES.items()
        if ModelCapability.chat in caps and ModelCapability.tts in caps
    }
    assert not both, f"这些 kind 同时声明了 chat 与 tts，会废掉绑定闸: {sorted(both)}"


def test_mimo_split_into_two_single_capability_kinds() -> None:
    """MiMo 的对话面与语音面是两个 kind，能力各自单一。"""
    assert ProviderKind.mimo_chat.value == "mimo_chat"
    assert ProviderKind.mimo_tts.value == "mimo_tts"
    assert PROVIDER_CAPABILITIES[ProviderKind.mimo_chat] == {ModelCapability.chat}
    assert PROVIDER_CAPABILITIES[ProviderKind.mimo_tts] == {ModelCapability.tts}


def test_mimo_faces_cannot_cross_bind() -> None:
    """错绑必须被拒：TTS 绑不到对话面，对话绑不到语音面。"""
    # 语音角色 → mimo_chat：拒绝（对话面没有 tts 能力）
    assert not kind_supports_profile("mimo_chat", "speech.tts")
    # 对话角色 → mimo_tts：拒绝（语音面没有 chat 能力）
    assert not kind_supports_profile("mimo_tts", "dialogue.fast")
    assert not kind_supports_profile("mimo_tts", "dialogue.deep")
    # 正确方向必须通过
    assert kind_supports_profile("mimo_tts", "speech.tts")
    assert kind_supports_profile("mimo_chat", "dialogue.fast")


@pytest.mark.parametrize("kind", sorted(TTS_KINDS))
def test_tts_kinds_serve_speech_tts_only(kind: str) -> None:
    """三家 TTS 能服务 speech.tts，但不能服务任何对话/记忆角色。"""
    assert kind_supports_profile(kind, "speech.tts")
    assert not kind_supports_profile(kind, "dialogue.fast")
    assert not kind_supports_profile(kind, "memory.extract")
    assert not kind_supports_profile(kind, "memory.embed")
    assert not kind_supports_profile(kind, "memory.rerank")
    assert not kind_supports_profile(kind, "perception.vision")


@pytest.mark.parametrize("kind", sorted(CHAT_KINDS))
def test_chat_kinds_cannot_serve_tts(kind: str) -> None:
    """任何对话 kind 都不能承担语音合成。"""
    assert not kind_supports_profile(kind, "speech.tts")


def test_asr_is_served_by_file_recognition_adapters() -> None:
    """文件识别只能绑定对应协议适配器，合成供应商仍被能力闸拦住。"""
    asr = {k.value for k, caps in PROVIDER_CAPABILITIES.items() if ModelCapability.asr in caps}
    assert asr == {"siliconflow_asr", "dashscope_asr"}
    assert kind_supports_profile("siliconflow_asr", "speech.asr")
    assert kind_supports_profile("dashscope_asr", "speech.asr")
    assert not kind_supports_profile("minimax_tts", "speech.asr")
    assert not kind_supports_profile("mimo_tts", "speech.asr")
