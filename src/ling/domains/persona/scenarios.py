"""场景提示词：同一份人设，在不同情境下该怎么处理。

## 为什么不能全塞进 system_prompt

`system_prompt` 每轮都注入。把"群里话少一点"写进去，私聊也会跟着变冷；
把"被点名就正面回"写进去，没人点她的时候那条就是噪声。场景提示只在
**那个场景真的成立**时才进提示词。

## 键必须是代码里真实存在的判定

`private` / `group` 由窗口可见性决定（`ContextCompiler.compile` 的
`is_group`）；`group_mentioned` / `group_unaddressed` 由 DecisionContext
的 `mentioned` / `explicitly_asked` 决定。**加一个新键必须同时加注入点**，
否则就是页面上一个写了也不生效的输入框——`traits` 曾经整整一段时间
只有编辑器、没有读者，那个教训不值得重演。

## 空串的语义

覆盖值为空串表示"回到内置默认"，不是"这个场景不要提示"。想弱化某条
默认，就直接在文本里写出来（例如"不必刻意回应"），不要清空。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# 单条提示的长度上限。场景提示是**补丁**不是正文：超过这个长度说明
# 作者想写的东西属于 system_prompt，挤在这里会把预算吃光。
MAX_SCENARIO_CHARS = 600


@dataclass(frozen=True)
class Scenario:
    key: str
    label: str
    when: str
    default: str


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        key="private",
        label="私聊",
        when="一对一聊天时，回复按这条来",
        default=(
            "一对一说话，可以慢一点、短一点，不用急着把话接满。"
            "对方没主动提的私事就别追问；不确定的事说不知道。"
        ),
    ),
    Scenario(
        key="group",
        label="群聊",
        when="在群里说话时，回复按这条来",
        default=(
            "群里人多，说话短一些，像随口接一句。不要每条都开口，"
            "也别把私聊里的称呼和约定带进群。"
        ),
    ),
    Scenario(
        key="group_mentioned",
        label="群里被点名",
        when="有人在群里 @ 你或点了你的名字时，判断要不要回按这条来",
        default=(
            "被点名了就正面回应，别装作没看见。回应简短直接，"
            "不用解释刚才为什么没说话。"
        ),
    ),
    Scenario(
        key="group_unaddressed",
        label="群里没被点名",
        when="群里没人点你时，判断要不要插话按这条来",
        default=(
            "没人点你时可以先不说话。只有真的接得上、说了对当前这句有用"
            "才插一句；别人之间的对话不要硬接。"
        ),
    ),
)

SCENARIO_BY_KEY: dict[str, Scenario] = {s.key: s for s in SCENARIOS}


def known_keys() -> set[str]:
    return set(SCENARIO_BY_KEY)


def effective_text(prompts: Mapping[str, str] | None, key: str) -> str:
    """这条场景当前生效的文本：覆盖优先，没有覆盖就用内置默认。"""
    scenario = SCENARIO_BY_KEY.get(key)
    if scenario is None:
        return ""
    override = (prompts or {}).get(key)
    if isinstance(override, str) and override.strip():
        return override.strip()
    return scenario.default


def is_custom(prompts: Mapping[str, str] | None, key: str) -> bool:
    override = (prompts or {}).get(key)
    return isinstance(override, str) and bool(override.strip())


def render(prompts: Mapping[str, str] | None, key: str) -> str:
    """渲染成能直接进提示词的一段。未知键返回空串，不猜。"""
    scenario = SCENARIO_BY_KEY.get(key)
    if scenario is None:
        return ""
    text = effective_text(prompts, key)
    if not text:
        return ""
    return f"【{scenario.label}】{text}"


def clean_overrides(raw: Mapping[str, object] | None) -> dict[str, str]:
    """清洗写入前的覆盖值：只留已知键、去空白、超长截断、空串丢弃。

    空串在这里被丢弃而不是存下来，是为了让"恢复默认"只有一种表示——
    存一个空串和不存在这条覆盖，读出来必须是同一个结果。
    """
    cleaned: dict[str, str] = {}
    for key, value in (raw or {}).items():
        if key not in SCENARIO_BY_KEY:
            continue
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text:
            continue
        cleaned[key] = text[:MAX_SCENARIO_CHARS]
    return cleaned


__all__ = [
    "MAX_SCENARIO_CHARS",
    "SCENARIOS",
    "SCENARIO_BY_KEY",
    "Scenario",
    "clean_overrides",
    "effective_text",
    "is_custom",
    "known_keys",
    "render",
]
