"""Independent acceptance of conversation inheritance against the design contract."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from itertools import product

import pytest

from ling.ports.config import DialoguePolicy, EffectiveConfig
from ling.settings.conversation_inheritance import resolve_conversation_settings

BOOL_FIELDS = (
    "private_may_stay_silent",
    "group_mention_requires_reply",
    "group_attention_deferred",
    "rest_may_skip",
)
DIALOGUE_FIELDS = (*BOOL_FIELDS, "group_turn_budget")


def _base(**dialogue_values: object) -> EffectiveConfig:
    return EffectiveConfig(
        revision=37,
        persona_id="global-persona",
        dialogue=replace(DialoguePolicy(), **dialogue_values),
    )


@pytest.mark.parametrize("field", BOOL_FIELDS)
@pytest.mark.parametrize("global_value", [False, True])
@pytest.mark.parametrize("instance_mode,session_mode", product(("missing", "false", "true"), repeat=2))
def test_boolean_full_layer_matrix(field: str, global_value: bool, instance_mode: str, session_mode: str) -> None:
    instance = {} if instance_mode == "missing" else {field: instance_mode == "true"}
    session = {} if session_mode == "missing" else {field: session_mode == "true"}
    before = deepcopy((instance, session))
    result = resolve_conversation_settings(_base(**{field: global_value}), instance_overrides=instance, session_overrides=session)
    if session_mode != "missing":
        expected, source = session_mode == "true", "session"
    elif instance_mode != "missing":
        expected, source = instance_mode == "true", "instance"
    else:
        expected, source = global_value, "global"
    observed = getattr(result, field)
    assert observed.value is expected
    assert observed.source == source
    assert (instance, session) == before


@pytest.mark.parametrize("instance", [{}, {"persona_id": "instance-persona"}, {"persona_id": None}])
@pytest.mark.parametrize("session", [{}, {"persona_id": "session-persona"}, {"persona_id": None}])
def test_persona_missing_and_explicit_null_are_distinct(instance: dict, session: dict) -> None:
    before = deepcopy((instance, session))
    result = resolve_conversation_settings(_base(), instance_overrides=instance, session_overrides=session)
    if "persona_id" in session:
        expected, source = session["persona_id"], "session"
    elif "persona_id" in instance:
        expected, source = instance["persona_id"], "instance"
    else:
        expected, source = "global-persona", "global"
    assert (result.persona_id.value, result.persona_id.source) == (expected, source)
    assert (instance, session) == before


@pytest.mark.parametrize("budget", range(1, 21))
def test_each_valid_budget_at_both_override_levels(budget: int) -> None:
    config = _base(group_turn_budget=11)
    inherited = resolve_conversation_settings(config)
    assert (inherited.group_turn_budget.value, inherited.group_turn_budget.source) == (11, "global")
    instance = resolve_conversation_settings(config, instance_overrides={"group_turn_budget": budget})
    assert (instance.group_turn_budget.value, instance.group_turn_budget.source) == (budget, "instance")
    session = resolve_conversation_settings(
        config,
        instance_overrides={"group_turn_budget": 7 if budget != 7 else 8},
        session_overrides={"group_turn_budget": budget},
    )
    assert (session.group_turn_budget.value, session.group_turn_budget.source) == (budget, "session")


@pytest.mark.parametrize("level", ["instance_overrides", "session_overrides"])
@pytest.mark.parametrize("invalid", [-1, 0, 21, 100, True, False, 1.0, "1", None, [], {}])
def test_invalid_explicit_budget_is_rejected(level: str, invalid: object) -> None:
    with pytest.raises(ValueError, match="group_turn_budget"):
        resolve_conversation_settings(_base(group_turn_budget=8), **{level: {"group_turn_budget": invalid}})


@pytest.mark.parametrize("level", ["instance_overrides", "session_overrides"])
@pytest.mark.parametrize("field", BOOL_FIELDS)
@pytest.mark.parametrize("invalid", [None, 0, 1, "false", [], {}])
def test_non_boolean_values_are_rejected(level: str, field: str, invalid: object) -> None:
    with pytest.raises(ValueError, match=field):
        resolve_conversation_settings(_base(), **{level: {field: invalid}})


@pytest.mark.parametrize("level", ["instance_overrides", "session_overrides"])
@pytest.mark.parametrize("invalid", ["", "  ", False, 0, [], {}])
def test_invalid_persona_values_are_rejected(level: str, invalid: object) -> None:
    with pytest.raises(ValueError, match="persona_id"):
        resolve_conversation_settings(_base(), **{level: {"persona_id": invalid}})


def test_interleaved_layers_apply_without_touching_other_configuration() -> None:
    config = _base(
        private_may_stay_silent=True,
        group_mention_requires_reply=True,
        group_attention_deferred=False,
        group_turn_budget=4,
        rest_may_skip=True,
        tools_enabled=True,
        proactive_delivery_enabled=True,
        group_reply_delivery_enabled=False,
        proactive_daily_limit=9,
        quiet_start_minute=100,
        quiet_end_minute=200,
    )
    instance = {"persona_id": "instance-persona", "group_mention_requires_reply": False, "group_turn_budget": 6}
    session = {"private_may_stay_silent": False, "group_attention_deferred": True, "rest_may_skip": False}
    before = deepcopy((config, instance, session))
    result = resolve_conversation_settings(config, instance_overrides=instance, session_overrides=session)
    assert (result.persona_id.value, result.persona_id.source) == ("instance-persona", "instance")
    assert (result.private_may_stay_silent.value, result.private_may_stay_silent.source) == (False, "session")
    assert (result.group_mention_requires_reply.value, result.group_mention_requires_reply.source) == (False, "instance")
    assert (result.group_attention_deferred.value, result.group_attention_deferred.source) == (True, "session")
    assert (result.group_turn_budget.value, result.group_turn_budget.source) == (6, "instance")
    assert (result.rest_may_skip.value, result.rest_may_skip.source) == (False, "session")
    applied = result.apply_dialogue(config)
    for field in DIALOGUE_FIELDS:
        assert getattr(applied.dialogue, field) == getattr(result, field).value
    for field in ("tools_enabled", "proactive_delivery_enabled", "group_reply_delivery_enabled", "proactive_daily_limit", "quiet_start_minute", "quiet_end_minute"):
        assert getattr(applied.dialogue, field) == getattr(config.dialogue, field)
    assert applied.persona_id == config.persona_id
    assert (config, instance, session) == before
    assert result == resolve_conversation_settings(config, instance_overrides=instance, session_overrides=session)
    other = resolve_conversation_settings(config, instance_overrides={"persona_id": None, "group_turn_budget": 20})
    assert (other.persona_id.value, other.group_turn_budget.value) == (None, 20)
    assert result.persona_id.value == "instance-persona" and result.group_turn_budget.value == 6


@pytest.mark.parametrize("level", ["instance_overrides", "session_overrides"])
def test_unknown_field_is_rejected_even_when_other_layer_is_valid(level: str) -> None:
    with pytest.raises(ValueError, match="not_a_conversation_field"):
        resolve_conversation_settings(_base(), **{level: {"not_a_conversation_field": False}})


@pytest.mark.parametrize(
    "field,global_value,instance_value,session_value",
    [
        ("persona_id", "global-persona", "instance-persona", "session-persona"),
        ("private_may_stay_silent", True, False, True),
        ("group_mention_requires_reply", True, False, True),
        ("group_attention_deferred", True, False, True),
        ("group_turn_budget", 4, 7, 12),
        ("rest_may_skip", True, False, True),
    ],
)
def test_each_field_distinguishes_instance_and_session(
    field: str, global_value: object, instance_value: object, session_value: object
) -> None:
    config = _base(**({field: global_value} if field != "persona_id" else {}))
    assert (getattr(resolve_conversation_settings(config), field).value,
            getattr(resolve_conversation_settings(config), field).source) == (global_value, "global")
    instance = {field: instance_value}
    selected = getattr(resolve_conversation_settings(config, instance_overrides=instance), field)
    assert (selected.value, selected.source) == (instance_value, "instance")
    selected = getattr(resolve_conversation_settings(config, instance_overrides=instance,
                                                    session_overrides={field: session_value}), field)
    assert (selected.value, selected.source) == (session_value, "session")


@pytest.mark.parametrize(
    "field,invalid,valid",
    [("private_may_stay_silent", 1, False),
     ("group_mention_requires_reply", None, True),
     ("group_attention_deferred", "false", False),
     ("rest_may_skip", [], True),
     ("group_turn_budget", 0, 5),
     ("persona_id", "", None)],
)
def test_shadowed_invalid_instance_is_rejected(field: str, invalid: object, valid: object) -> None:
    with pytest.raises(ValueError, match=field):
        resolve_conversation_settings(_base(), instance_overrides={field: invalid},
                                      session_overrides={field: valid})
