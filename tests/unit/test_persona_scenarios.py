"""场景提示词的纯逻辑：默认、覆盖、清洗。

这些是"人设页写进去的东西到底会不会生效"的第一道闸。放在 unit 层是因为
它们不碰库：默认值改一次影响所有人设，这类语义值得有独立回归。
"""

from __future__ import annotations

import pytest

from ling.domains.persona.scenarios import (
    MAX_SCENARIO_CHARS,
    SCENARIO_BY_KEY,
    SCENARIOS,
    clean_overrides,
    effective_text,
    is_custom,
    known_keys,
    render,
)


def test_catalog_keys_are_stable() -> None:
    """键是代码里的判定，改名等于让已有覆盖静默失效。"""
    assert known_keys() == {
        "private",
        "group",
        "group_mentioned",
        "group_unaddressed",
    }


def test_every_scenario_has_default_and_explanation() -> None:
    for scenario in SCENARIOS:
        assert scenario.default.strip(), f"{scenario.key} 没有默认文本"
        assert scenario.when.strip(), f"{scenario.key} 没说清什么时候生效"
        assert len(scenario.default) <= MAX_SCENARIO_CHARS


def test_effective_text_falls_back_to_default() -> None:
    default = SCENARIO_BY_KEY["group"].default
    assert effective_text({}, "group") == default
    assert effective_text(None, "group") == default


def test_override_wins_and_is_trimmed() -> None:
    assert effective_text({"group": "  群里我基本不接话  "}, "group") == "群里我基本不接话"
    assert is_custom({"group": "群里我基本不接话"}, "group") is True
    assert is_custom({}, "group") is False


def test_blank_override_means_default_not_silence() -> None:
    """空串 = 恢复默认。这是"清空"唯一的表示，不能读成"这个场景不要提示"。"""
    default = SCENARIO_BY_KEY["private"].default
    assert effective_text({"private": "   "}, "private") == default
    assert is_custom({"private": "   "}, "private") is False


def test_unknown_key_is_ignored_by_readers() -> None:
    """读取端不猜：未知键既不渲染，也不冒充某个已知场景。"""
    assert render({"nope": "随便写"}, "nope") == ""
    assert effective_text({"nope": "随便写"}, "nope") == ""


def test_render_prefixes_label() -> None:
    rendered = render({}, "group_mentioned")
    assert rendered.startswith("【群里被点名】")
    assert SCENARIO_BY_KEY["group_mentioned"].default in rendered


def test_clean_overrides_drops_unknown_blank_and_non_string() -> None:
    cleaned = clean_overrides(
        {
            "group": "  群里少说点  ",
            "private": "",
            "made_up": "不该留下",
            "group_mentioned": 3,  # type: ignore[dict-item]
        }
    )
    assert cleaned == {"group": "群里少说点"}


def test_clean_overrides_truncates_long_text() -> None:
    cleaned = clean_overrides({"group": "长" * (MAX_SCENARIO_CHARS + 50)})
    assert len(cleaned["group"]) == MAX_SCENARIO_CHARS


@pytest.mark.parametrize("key", sorted(known_keys()))
def test_each_key_resolves_to_its_own_default(key: str) -> None:
    """四个键不能共用一条默认——那等于没有分场景。"""
    others = [s.default for s in SCENARIOS if s.key != key]
    assert SCENARIO_BY_KEY[key].default not in others
