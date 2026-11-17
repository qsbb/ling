"""Pure merge behavior for conversation policy inheritance."""

from dataclasses import FrozenInstanceError, replace

import pytest

from ling.ports.config import DialoguePolicy, EffectiveConfig
from ling.settings.conversation_inheritance import resolve_conversation_settings

BOOL_FIELDS = (
    "private_may_stay_silent",
    "group_mention_requires_reply",
    "group_attention_deferred",
    "rest_may_skip",
)


@pytest.mark.parametrize("field", BOOL_FIELDS)
@pytest.mark.parametrize("global_value", [False, True])
def test_each_boolean_field_obeys_each_level(field: str, global_value: bool) -> None:
    dialogue = replace(DialoguePolicy(), **{field: global_value})
    config = EffectiveConfig(revision=4, dialogue=dialogue)
    global_result = resolve_conversation_settings(config)
    assert getattr(global_result, field).value is global_value
    assert getattr(global_result, field).source == "global"

    instance_value = not global_value
    instance_result = resolve_conversation_settings(
        config, instance_overrides={field: instance_value}
    )
    assert getattr(instance_result, field).value is instance_value
    assert getattr(instance_result, field).source == "instance"

    session_result = resolve_conversation_settings(
        config,
        instance_overrides={field: instance_value},
        session_overrides={field: global_value},
    )
    assert getattr(session_result, field).value is global_value
    assert getattr(session_result, field).source == "session"


def test_persona_null_is_a_value_and_missing_key_inherits() -> None:
    config = EffectiveConfig(revision=1, persona_id="persona-global")
    instance = {"persona_id": "persona-instance"}
    inherited = resolve_conversation_settings(config, instance_overrides=instance)
    assert (inherited.persona_id.value, inherited.persona_id.source) == (
        "persona-instance",
        "instance",
    )

    without_persona = resolve_conversation_settings(
        config, instance_overrides=instance, session_overrides={"persona_id": None}
    )
    assert (without_persona.persona_id.value, without_persona.persona_id.source) == (
        None,
        "session",
    )
    no_instance_persona = resolve_conversation_settings(
        config, instance_overrides={"persona_id": None}
    )
    assert (no_instance_persona.persona_id.value, no_instance_persona.persona_id.source) == (
        None,
        "instance",
    )
    session_persona = resolve_conversation_settings(
        config,
        instance_overrides={"persona_id": None},
        session_overrides={"persona_id": "persona-session"},
    )
    assert (session_persona.persona_id.value, session_persona.persona_id.source) == (
        "persona-session",
        "session",
    )
    assert resolve_conversation_settings(config).persona_id.value == "persona-global"
    assert resolve_conversation_settings(replace(config, persona_id="")).persona_id.value is None


@pytest.mark.parametrize("budget", range(1, 21))
def test_valid_budget_can_override(budget: int) -> None:
    config = EffectiveConfig(revision=1, dialogue=DialoguePolicy(group_turn_budget=7))
    instance = resolve_conversation_settings(config, instance_overrides={"group_turn_budget": budget})
    session = resolve_conversation_settings(
        config,
        instance_overrides={"group_turn_budget": 7},
        session_overrides={"group_turn_budget": budget},
    )
    assert (instance.group_turn_budget.value, instance.group_turn_budget.source) == (
        budget,
        "instance",
    )
    assert (session.group_turn_budget.value, session.group_turn_budget.source) == (budget, "session")
    assert resolve_conversation_settings(config).group_turn_budget.value == 7


@pytest.mark.parametrize("field", BOOL_FIELDS)
@pytest.mark.parametrize("value", [None, 0, 1, "false", []])
def test_boolean_overrides_reject_other_types(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        resolve_conversation_settings(
            EffectiveConfig(revision=1), instance_overrides={field: value}
        )


@pytest.mark.parametrize("value", [None, -1, 0, 21, 100, True, False, 1.0, "1"])
def test_invalid_budget_does_not_fall_back(value: object) -> None:
    with pytest.raises(ValueError, match="group_turn_budget"):
        resolve_conversation_settings(
            EffectiveConfig(revision=1), session_overrides={"group_turn_budget": value}
        )


@pytest.mark.parametrize("value", ["", "   ", False, 5, [], {}])
def test_invalid_persona_does_not_fall_back(value: object) -> None:
    with pytest.raises(ValueError, match="persona_id"):
        resolve_conversation_settings(
            EffectiveConfig(revision=1), session_overrides={"persona_id": value}
        )


def test_fields_are_resolved_independently_without_mutating_inputs() -> None:
    config = EffectiveConfig(
        revision=12,
        persona_id="persona-global",
        dialogue=DialoguePolicy(
            private_may_stay_silent=True,
            group_mention_requires_reply=True,
            group_attention_deferred=True,
            group_turn_budget=3,
            rest_may_skip=True,
            tools_enabled=True,
        ),
    )
    instance = {
        "persona_id": "persona-instance",
        "group_mention_requires_reply": False,
        "group_turn_budget": 5,
    }
    session = {
        "private_may_stay_silent": False,
        "group_turn_budget": 8,
        "rest_may_skip": False,
    }
    instance_before, session_before = instance.copy(), session.copy()
    result = resolve_conversation_settings(
        config, instance_overrides=instance, session_overrides=session
    )
    assert (result.persona_id.value, result.persona_id.source) == ("persona-instance", "instance")
    assert (result.private_may_stay_silent.value, result.private_may_stay_silent.source) == (
        False,
        "session",
    )
    assert (result.group_mention_requires_reply.value, result.group_mention_requires_reply.source) == (
        False,
        "instance",
    )
    assert (result.group_attention_deferred.value, result.group_attention_deferred.source) == (
        True,
        "global",
    )
    assert (result.group_turn_budget.value, result.group_turn_budget.source) == (8, "session")
    assert (result.rest_may_skip.value, result.rest_may_skip.source) == (False, "session")
    assert instance == instance_before and session == session_before
    assert result == resolve_conversation_settings(
        config, instance_overrides=instance, session_overrides=session
    )
    applied = result.apply_dialogue(config)
    assert applied.dialogue.private_may_stay_silent is False
    assert applied.dialogue.group_mention_requires_reply is False
    assert applied.dialogue.group_attention_deferred is True
    assert applied.dialogue.group_turn_budget == 8
    assert applied.dialogue.rest_may_skip is False
    assert applied.dialogue.tools_enabled is True
    assert config.dialogue.group_turn_budget == 3
    assert config.dialogue.private_may_stay_silent is True
    assert applied.persona_id == config.persona_id
    with pytest.raises(FrozenInstanceError):
        result.group_turn_budget.value = 10  # type: ignore[misc]


def test_rejects_unknown_keys_even_when_they_would_be_shadowed() -> None:
    with pytest.raises(ValueError, match="unknown"):
        resolve_conversation_settings(
            EffectiveConfig(revision=1),
            instance_overrides={"unknown": True},
            session_overrides={"private_may_stay_silent": False},
        )


def test_alternating_contexts_do_not_share_state() -> None:
    config = EffectiveConfig(revision=1)
    first = resolve_conversation_settings(
        config, instance_overrides={"group_turn_budget": 1}, session_overrides={"persona_id": None}
    )
    second = resolve_conversation_settings(
        config, instance_overrides={"group_turn_budget": 20, "persona_id": "persona-b"}
    )
    assert (first.group_turn_budget.value, first.persona_id.value) == (1, None)
    assert (second.group_turn_budget.value, second.persona_id.value) == (20, "persona-b")
    assert first == resolve_conversation_settings(
        config, instance_overrides={"group_turn_budget": 1}, session_overrides={"persona_id": None}
    )
    assert resolve_conversation_settings(config) == resolve_conversation_settings(
        config, instance_overrides={}, session_overrides={}
    )
