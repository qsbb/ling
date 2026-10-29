"""模型角色闸：每个 `ModelRole` 要么真的被请求，要么标明是保留角色。

## 为什么需要这个文件

设置页原来把 **13 个角色全部**列出来让人绑定。实际查下来 5 个绑了没有任何效果：

| 角色 | 情况 |
| --- | --- |
| `reflection` / `planning` / `moderation.advisory` | 至今**没有任何代码请求**它们 |
| `memory.embed` / `memory.rerank` | 能力**存在**，但走环境变量（`LING_EMBEDDING_*` / `LING_RERANK_*`），完全不经过模型绑定 |

用户看到的是一份"可选职责"清单，其中五分之二绑上去什么都不会发生。
这不是崩溃，是**承诺了不存在的能力**——和契约里的幻影字段同一类问题。

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

# 只定义/回显角色、不真正请求它们的地方
DEFINITION_ONLY = {
    "contracts/v1.py",
    "settings/service.py",
    "api/routes/settings.py",
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
    """
    blob = _requested_profiles()
    stale = [
        name
        for name in RESERVED_PROFILES
        if re.search(rf'["\']{re.escape(name)}["\']', blob)
    ]
    assert not stale, (
        f"这些角色已经在代码里被请求，却仍标为保留（用户看不到）：{stale}"
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


def test_degradation_map_only_covers_offerable_roles() -> None:
    """降级说明只能挂在**可绑定**的角色上。

    挂在保留角色上等于告诉用户"去绑它"，而那是一条死路。
    """
    from ling.api.routes.settings import _OFFERABLE_PROFILES
    from ling.settings.service import PROFILE_DEGRADATIONS

    stray = set(PROFILE_DEGRADATIONS) - set(_OFFERABLE_PROFILES)
    assert not stray, f"这些降级说明挂在不可绑定的角色上：{stray}"
