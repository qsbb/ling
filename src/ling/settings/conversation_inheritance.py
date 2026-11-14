"""会话对话设置的逐字段继承与来源解析。

此模块只负责合并标量值；调用方仍须校验人格是否存在以及存取权限。
缺键表示继承，显式的 ``False`` 和 ``persona_id=None`` 都是覆盖值。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, TypeVar

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ling.ports.config import EffectiveConfig

Source = Literal["global", "instance", "session"]
T = TypeVar("T")


@dataclass(frozen=True)
class ResolvedField[T]:
    value: T
    source: Source


@dataclass(frozen=True)
class ResolvedConversationSettings:
    persona_id: ResolvedField[str | None]
    private_may_stay_silent: ResolvedField[bool]
    group_mention_requires_reply: ResolvedField[bool]
    group_attention_deferred: ResolvedField[bool]
    group_turn_budget: ResolvedField[int]
    rest_may_skip: ResolvedField[bool]

    def apply_dialogue(self, config: EffectiveConfig) -> EffectiveConfig:
        """复制全局配置并叠加对话策略，保留其余设置。

        人格需要单独固定首次读取的内容，不能仅用 ``None`` 改写全局 ID。
        """
        return replace(
            config,
            dialogue=replace(
                config.dialogue,
                private_may_stay_silent=self.private_may_stay_silent.value,
                group_mention_requires_reply=self.group_mention_requires_reply.value,
                group_attention_deferred=self.group_attention_deferred.value,
                group_turn_budget=self.group_turn_budget.value,
                rest_may_skip=self.rest_may_skip.value,
            ),
        )


_BOOLEAN_FIELDS = frozenset(
    {
        "private_may_stay_silent",
        "group_mention_requires_reply",
        "group_attention_deferred",
        "rest_may_skip",
    }
)
_FIELDS = _BOOLEAN_FIELDS | {"persona_id", "group_turn_budget"}


def _validate_overrides(overrides: Mapping[str, object]) -> None:
    """拒绝错误值，避免非法覆盖被静默当作继承。"""
    for key, value in overrides.items():
        if key not in _FIELDS:
            raise ValueError(f"不支持的对话设置字段: {key}")
        if key in _BOOLEAN_FIELDS:
            if type(value) is not bool:
                raise ValueError(f"{key} 必须是布尔值")
        elif key == "group_turn_budget":
            if type(value) is not int or not 1 <= value <= 20:
                raise ValueError("group_turn_budget 必须是 1 到 20 的整数")
        elif value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("persona_id 必须是非空人格 ID 或 null")


def resolve_conversation_settings(
    config: EffectiveConfig,
    *,
    instance_overrides: Mapping[str, object] | None = None,
    session_overrides: Mapping[str, object] | None = None,
) -> ResolvedConversationSettings:
    """按会话、实例、全局顺序逐字段解析，且不修改输入。"""
    instance = {} if instance_overrides is None else instance_overrides
    session = {} if session_overrides is None else session_overrides
    _validate_overrides(instance)
    _validate_overrides(session)

    def choose(key: str, global_value: T) -> ResolvedField[T]:
        if key in session:
            return ResolvedField(session[key], "session")  # type: ignore[arg-type]
        if key in instance:
            return ResolvedField(instance[key], "instance")  # type: ignore[arg-type]
        return ResolvedField(global_value, "global")

    dialogue = config.dialogue
    return ResolvedConversationSettings(
        persona_id=choose("persona_id", config.persona_id or None),
        private_may_stay_silent=choose(
            "private_may_stay_silent", dialogue.private_may_stay_silent
        ),
        group_mention_requires_reply=choose(
            "group_mention_requires_reply", dialogue.group_mention_requires_reply
        ),
        group_attention_deferred=choose(
            "group_attention_deferred", dialogue.group_attention_deferred
        ),
        group_turn_budget=choose("group_turn_budget", dialogue.group_turn_budget),
        rest_may_skip=choose("rest_may_skip", dialogue.rest_may_skip),
    )
