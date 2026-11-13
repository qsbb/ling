"""模型角色闸：每个 `ModelRole` 要么真的被请求，要么标明是保留角色。

## 为什么需要这个文件

设置页原来把所有角色列出来让人绑定，部分角色绑了没有任何效果：

| 角色 | 情况 |
| --- | --- |
| `reflection` / `planning` / `moderation.advisory` | 至今**没有任何代码请求**它们 |
| `dialogue.deep` | 同样没有任何代码按它请求（`turn.py` 的默认值恒为 fast） |
| `realtime.voice` | 尚无实时语音会话实现 |

用户看到的是一份"可选职责"清单，其中一半绑上去什么都不会发生。
这不是崩溃，是**承诺了不存在的能力**——和契约里的幻影字段同一类问题。

（`memory.embed` / `memory.rerank` 曾经也在这张表里，理由是"配置走环境
变量"。现在它们真的读绑定了：`domains/memory/providers.py` 先读绑定、
没绑定才退回环境变量，所以回到了可绑清单。）

文件识别与语音合成现在分别通过 `speech.asr` / `speech.tts` 绑定装配。
本文件检查设置入口和供应商能力，实际语音效果由正常入口集成测试覆盖。

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

import ast
import pathlib
import re

import pytest

from ling.contracts.v1 import ModelRole
from ling.settings.service import RESERVED_PROFILES

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "ling"

# 只定义/回显角色、不真正请求它们的地方
DEFINITION_ONLY = {
    "contracts/v1.py",
    "settings/service.py",
    "api/routes/settings.py",
    # `ports/speech.py` 的能力声明不构造供应商，属于定义层。
    "ports/speech.py",
    # `ports/model.py` 的 `PROFILE_CAPABILITIES` 是**能力声明表**（"这个角色
    # 要哪种能力"），不是"现在去请求这个角色"。和上面 speech 的
    # `SPEECH_PROFILE_REQUIREMENTS` 同一类。把它算进"已被请求"，会让
    # `dialogue.deep` 永远不能进保留名单——而它恰恰没有任何调用方。
    "ports/model.py",
}


def _profile_literals(source: str) -> str:
    tree = ast.parse(source)
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    return "\n".join(
        repr(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    )


def _requested_profiles() -> str:
    return "\n".join(
        _profile_literals(p.read_text(encoding="utf-8"))
        for p in SRC.rglob("*.py")
        if p.relative_to(SRC).as_posix() not in DEFINITION_ONLY
    )


def _used_as_profile(name: str, blob: str) -> bool:
    """这个角色名是不是被当作模型职责用过。

    **这是宽松检查，会过近似**：只要字符串字面量在代码里出现就算用过。
    已知盲区——outbox topic 名和模型职责名可能撞同一个字符串
    （`@handler("memory.extract")` 就撞过），于是"没人按职责请求它"
    被判成"用过"。所以关键路径另有**行为测试**兜底
    （见 tests/integration/test_memory_extract_wiring.py）。

    不把这里做成精确静态分析：这些角色通过字面量、模块常量、元组迭代、
    关键字参数等多种形态被引用，逐个覆盖下去只会得到一个脆到没人敢改的
    正则。宁可承认它宽松，用行为测试守真正要紧的那几条。
    """
    return bool(re.search(rf'["\']{re.escape(name)}["\']', blob))


@pytest.mark.parametrize("source", ['# profile = "speech.tts"', '"""speech.tts"""'])
def test_profile_scan_ignores_comments_and_docstrings(source: str) -> None:
    assert not _used_as_profile("speech.tts", _profile_literals(source))


def test_profile_scan_finds_executable_profile_literal() -> None:
    assert _used_as_profile("speech.tts", _profile_literals('profile = "speech.tts"'))


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
    """已请求的角色应当进入设置页的可绑定清单。"""
    blob = _requested_profiles()
    stale = [
        name
        for name in RESERVED_PROFILES
        if _used_as_profile(name, blob)
    ]
    assert not stale, f"这些角色已经在代码里被请求，却仍标为保留（用户看不到）：{stale}"


@pytest.mark.parametrize(
    ("profile", "capability", "expected_kinds"),
    [
        ("speech.tts", "tts", {"minimax_tts", "fishaudio_tts", "mimo_tts"}),
        ("speech.asr", "asr", {"siliconflow_asr", "dashscope_asr"}),
    ],
)
def test_speech_roles_are_offerable_with_dedicated_provider_candidates(
    profile: str, capability: str, expected_kinds: set[str]
) -> None:
    """语音职责可选，候选供应商只提供对应的语音能力。"""
    from ling.api.routes.settings import _OFFERABLE_PROFILES
    from ling.ports.model import (
        ProviderKind,
        kind_supports_profile,
        provider_capabilities,
        required_capability,
    )

    assert profile in _OFFERABLE_PROFILES
    assert profile not in RESERVED_PROFILES
    requirement = required_capability(profile)
    assert requirement is not None and requirement.value == capability
    candidates = {kind.value for kind in ProviderKind if kind_supports_profile(kind, profile)}
    assert candidates == expected_kinds
    for kind in candidates:
        assert {item.value for item in provider_capabilities(kind)} == {capability}


def test_realtime_voice_remains_reserved() -> None:
    from ling.api.routes.settings import _OFFERABLE_PROFILES

    assert "realtime.voice" in RESERVED_PROFILES
    assert "realtime.voice" not in _OFFERABLE_PROFILES


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
    实际零效果。

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
