"""模型角色闸：每个 `ModelRole` 要么真的被请求，要么标明是保留角色。

## 为什么需要这个文件

设置页原来把 **13 个角色全部**列出来让人绑定。实际查下来 5 个绑了没有任何效果：

| 角色 | 情况 |
| --- | --- |
| `reflection` / `planning` / `moderation.advisory` | 至今**没有任何代码请求**它们 |
| `dialogue.deep` | 同样没有任何代码按它请求（`turn.py` 的默认值恒为 fast） |
| `speech.asr` / `speech.tts` / `realtime.voice` | 没有按模型职责构造的 provider |

用户看到的是一份"可选职责"清单，其中一半绑上去什么都不会发生。
这不是崩溃，是**承诺了不存在的能力**——和契约里的幻影字段同一类问题。

（`memory.embed` / `memory.rerank` 曾经也在这张表里，理由是"配置走环境
变量"。现在它们真的读绑定了：`domains/memory/providers.py` 先读绑定、
没绑定才退回环境变量，所以回到了可绑清单。）

## 规则

每个 `ModelRole` 必须满足两者之一：

1. 它的 profile 字符串在 `src/ling` 里**真的被请求**（不在定义/回显层）
2. 它在 `RESERVED_PROFILES` 里，并写清原因

## 两类原因要分开写

- 「尚无实现」：能力压根没有
- 「由环境变量配置」：能力有，但配置入口不在这儿

混成一句"暂未支持"会让后来人分不清是该实现它，还是该把配置统一。
"""

from __future__ import annotations

import pathlib
import re

import pytest

from ling.contracts.v1 import ModelRole
from ling.settings.service import RESERVED_PROFILES

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "ling"

# 「代码**读得到** provider，但用户**看不到**效果」的角色，以及为什么。
#
# 这张表是 `test_reserved_profiles_are_not_still_requested` 的唯一豁免来源。
# 它存在的理由：那条规则把"字符串被按职责请求"当成"功能已交付"，而这两件事
# 可以分开——`speech.tts` 就是（`domains/speech/providers.py` 真的会按绑定造出
# 三家适配器，但没有任何代码在回复时调用 `SpeechOrchestrator.synthesize()`）。
#
# **只允许往里加"读得到、听不到"的条目**。真正的交付（用户能看到效果）应该
# 把角色从 `RESERVED_PROFILES` 挪进 `_OFFERABLE_PROFILES`，而不是往这里加一行。
PROVIDER_READ_BUT_NOT_DELIVERED: dict[str, str] = {
    "speech.tts": (
        "domains/speech/providers.py 已能按绑定构造 MiniMax/FishAudio/MiMo 适配器"
        "（GET /v1/speech 读它），但 [speak] 的解析结果没有任何生产代码交给"
        " SpeechOrchestrator.synthesize()，所以她仍然不会说话"
    ),
}

# 只定义/回显角色、不真正请求它们的地方
DEFINITION_ONLY = {
    "contracts/v1.py",
    "settings/service.py",
    "api/routes/settings.py",
    # `ports/speech.py` 的 `SPEECH_PROFILE_REQUIREMENTS` 是**能力声明表**：
    # 写的是"若将来有 speech.tts provider，它必须支持 streaming+cancellable"，
    # 不是"现在去请求 speech.tts"。全仓没有任何地方按这些角色构造 provider
    # （`validate_speech_profile` 零调用方），所以它属于定义层。
    # 把它算作"已被请求"会让保留名单永远不能收进这三个角色——而这正是
    # 它们该待的地方：绑了零效果，却显示成"已绑定"。
    "ports/speech.py",
    # `ports/model.py` 的 `PROFILE_CAPABILITIES` 是**能力声明表**（"这个角色
    # 要哪种能力"），不是"现在去请求这个角色"。和上面 speech 的
    # `SPEECH_PROFILE_REQUIREMENTS` 同一类。把它算进"已被请求"，会让
    # `dialogue.deep` 永远不能进保留名单——而它恰恰没有任何调用方。
    "ports/model.py",
}


def _requested_profiles() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in SRC.rglob("*.py")
        if p.relative_to(SRC).as_posix() not in DEFINITION_ONLY
    )


def _used_as_profile(name: str, blob: str) -> bool:
    """这个角色名是不是被当作模型职责用过。

    **这是宽松检查，会过近似**：只要字符串在代码里出现就算用过。
    已知盲区——outbox topic 名和模型职责名可能撞同一个字符串
    （`@handler("memory.extract")` 就撞过），于是"没人按职责请求它"
    被判成"用过"。所以关键路径另有**行为测试**兜底
    （见 tests/integration/test_memory_extract_wiring.py）。

    不把这里做成精确静态分析：这些角色通过字面量、模块常量、元组迭代、
    关键字参数等多种形态被引用，逐个覆盖下去只会得到一个脆到没人敢改的
    正则。宁可承认它宽松，用行为测试守真正要紧的那几条。
    """
    return bool(re.search(rf'["\']{re.escape(name)}["\']', blob))


@pytest.mark.parametrize("role", list(ModelRole), ids=lambda r: r.value)
def test_role_is_requested_or_declared_reserved(role: ModelRole) -> None:
    """绑定了却不生效的角色，不该出现在可选清单里。

    判据是"被**当作职责**请求"，不是"字符串出现过"——见 _used_as_profile。
    """
    blob = _requested_profiles()
    used = _used_as_profile(role.value, blob)
    reserved = role.value in RESERVED_PROFILES
    assert used or reserved, (
        f"角色 {role.value} 既没有被任何代码按职责请求，也没有在"
        "RESERVED_PROFILES 里写清原因。它会出现在设置页，而绑定后没有任何效果。"
    )


def test_reserved_profiles_have_a_reason() -> None:
    """保留角色必须写原因，且分清是"没实现"还是"配置在别处"。

    这两类的后续动作完全不同：一类要写实现，一类要统一配置入口。
    混成"暂未支持"会让后来人无从判断。
    """
    for name, reason in RESERVED_PROFILES.items():
        assert reason.strip(), f"{name} 没有写原因"
        assert any(
            kw in reason for kw in ("尚无实现", "环境变量")
        ), f"{name} 的原因没说清属于哪一类：{reason}"


def test_reserved_profiles_are_real_roles() -> None:
    """`RESERVED_PROFILES` 里不能有拼错的角色名。

    拼错的名字永远不会被匹配，等于这条保留声明静默失效。
    """
    known = {r.value for r in ModelRole}
    for name in RESERVED_PROFILES:
        assert name in known, f"RESERVED_PROFILES 里有不存在的角色：{name}"


def test_reserved_profiles_are_not_still_requested() -> None:
    """已经接上实现的角色不该还挂在保留名单里。

    否则它会被永久排除在设置页之外——功能做了，但用户看不到。

    ## `speech.tts` 为什么可以例外（2026-11-12）

    这条规则的前提是「按职责拿到模型 == 用户能看见效果」。`speech.tts` 打破了
    这个前提，于是**两种做法会有一边是假的**：

    - 按规则把它移出保留名单 → 设置页出现它 → 承诺"绑上就会说话"，而
      出站触发链路仍然不存在（`SpeechOrchestrator.synthesize()` 在 `src/`
      里零调用点，只有集成测试手工构造）。这正是本仓库反复消灭的假承诺。
    - 把它留在保留名单 → 这条字符串守卫会红，因为它只看"字符串出现过"。

    所以这里改成**显式豁免 + 行为判据**：豁免只对写进
    `PROVIDER_READ_BUT_NOT_DELIVERED` 的角色开，且另有一条测试
    （`test_read_but_not_delivered_exemption_is_justified`）要求
    「读得到 provider」是真的、「用户看不到效果」也是真的。
    """
    blob = _requested_profiles()
    stale = [
        name
        for name in RESERVED_PROFILES
        if name not in PROVIDER_READ_BUT_NOT_DELIVERED
        and re.search(rf'["\']{re.escape(name)}["\']', blob)
    ]
    assert not stale, f"这些角色已经在代码里被请求，却仍标为保留（用户看不到）：{stale}"


def test_read_but_not_delivered_exemption_is_justified() -> None:
    """豁免的每一条都必须真的"读得到 provider、但用户看不到效果"。

    这条不是文字游戏：它把 `RESERVED_PROFILES` 里那句说明和代码绑在一起。
    删掉 `domains/speech/providers.py` 的装配实现之后，那段说明（"已能按绑定
    构造"）就成了假的，这条会红。
    """
    from ling.api.routes.settings import _OFFERABLE_PROFILES
    from ling.domains.speech.providers import TTS_KINDS, TTS_PROFILE
    from ling.ports.model import kind_supports_profile

    assert set(PROVIDER_READ_BUT_NOT_DELIVERED) <= set(RESERVED_PROFILES), (
        "豁免表里出现了不在保留名单里的角色"
    )
    for name, why in PROVIDER_READ_BUT_NOT_DELIVERED.items():
        assert why.strip(), f"{name} 的豁免没写理由"

    # `speech.tts` 的豁免靠这三条事实撑住，缺一条它就是假的。
    assert TTS_PROFILE == "speech.tts"
    unsupported = [k for k in TTS_KINDS if not kind_supports_profile(k, TTS_PROFILE)]
    assert not unsupported, f"TTS_KINDS 里有 kind 其实不能服务 speech.tts：{unsupported}"
    assert len(TTS_KINDS) == 3, (
        "三家适配器（MiniMax / FishAudio / MiMo）应当都能按绑定构造；"
        f"实际登记的是 {sorted(TTS_KINDS)}"
    )

    # 「用户看不到效果」这一半：它不在可绑清单里。哪天进了可绑清单，就不再是
    # "读得到但听不到"，豁免必须先删掉——这条会先炸。
    assert "speech.tts" not in _OFFERABLE_PROFILES, (
        "speech.tts 已经进了可绑清单，`PROVIDER_READ_BUT_NOT_DELIVERED` 里的豁免"
        "必须删掉；在此之前先确认出站触发链路真的接上了"
    )


def test_offerable_list_excludes_reserved() -> None:
    """设置页展示的角色不能与保留名单重叠。"""
    from ling.api.routes.settings import _OFFERABLE_PROFILES

    overlap = set(_OFFERABLE_PROFILES) & set(RESERVED_PROFILES)
    assert not overlap, f"这些角色既在展示清单又在保留名单：{overlap}"
    for name in _OFFERABLE_PROFILES:
        assert name in {r.value for r in ModelRole}, f"展示清单里有不存在的角色：{name}"


def test_turn_decision_has_no_dead_member() -> None:
    """`TurnDecision.defer` 曾经是个死枚举：没有代码产出或消费它。

    而"延后"已经有活的实现（`deferred_by_floor` / `deferred_attention`）。
    同一个概念三个名字，迟早有人用错那个不生效的。
    """
    from ling.kernel.decision import TurnDecision

    members = {m.value for m in TurnDecision}
    assert "defer" not in members, (
        "TurnDecision.defer 又回来了；它没有产出方和消费方"
    )
    assert members == {"reply", "silence", "clarify"}, (
        f"TurnDecision 的成员变了，确认新增的都被真正使用：{members}"
    )


def test_dialogue_deep_is_reserved_not_offerable() -> None:
    """`dialogue.deep` 的最终裁决：**不再可绑**，进保留名单并写明原因。

    它曾经在可绑清单里，而全仓没有任何代码按这个角色发起请求
    （见 docs/MODEL-ROLES-AUDIT.md）。列着它等于承诺"绑上就会更聪明"，
    实际零效果——和语音三项是同一个错。

    这条同时守住"别把它悄悄加回来"：真要加回可绑清单，得先有代码按
    `dialogue.deep` 请求模型（那时 `test_role_is_requested_or_declared_reserved`
    与 `test_reserved_profiles_are_not_still_requested` 会一起要求把它
    从保留名单里删掉），而不是只改一行清单。
    """
    from ling.api.routes.settings import _OFFERABLE_PROFILES

    assert "dialogue.deep" not in _OFFERABLE_PROFILES, (
        "dialogue.deep 又出现在可绑清单里了：除非真有代码按它请求模型，"
        "否则这又是一个绑了不生效的入口"
    )
    assert "dialogue.deep" in RESERVED_PROFILES
    reason = RESERVED_PROFILES["dialogue.deep"]
    assert "尚无实现" in reason, f"原因没说清属于哪一类：{reason}"


def test_degradation_map_only_covers_offerable_roles() -> None:
    """降级说明只能挂在**可绑定**的角色上。

    挂在保留角色上等于告诉用户"去绑它"，而那是一条死路。
    """
    from ling.api.routes.settings import _OFFERABLE_PROFILES
    from ling.settings.service import PROFILE_DEGRADATIONS

    stray = set(PROFILE_DEGRADATIONS) - set(_OFFERABLE_PROFILES)
    assert not stray, f"这些降级说明挂在不可绑定的角色上：{stray}"
