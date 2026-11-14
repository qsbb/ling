"""会话设置的真实响应、OpenAPI 和发布契约保持一致。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from ling.contracts.v1 import WindowDialoguePolicyPreview, WindowDialoguePolicyView
from tests.integration.conftest import TestEnv
from tests.integration.test_window_dialogue_policy import (
    change,
    create_window,
    preview,
    publish,
    save,
)

pytestmark = pytest.mark.w0_web
ROOT = Path(__file__).resolve().parents[2]
FIELD = "private_may_stay_silent"
POLICY_FIELDS = (
    "persona_id",
    "private_may_stay_silent",
    "group_mention_requires_reply",
    "group_attention_deferred",
    "group_turn_budget",
    "rest_may_skip",
)
VIEW_KEYS = {
    "field", "global_value", "override", "effective", "source",
    "fields", "origin_status", "instance_scope", "instance_id",
    "global_revision", "instance_revision", "session_revision",
}


def _valid_view() -> dict:
    fields = {
        "persona_id": {
            "global_value": "persona-parent",
            "instance_override": None,
            "has_instance_override": False,
            "session_override": None,
            "has_session_override": False,
            "effective": "persona-parent",
            "source": "global",
        },
        FIELD: {
            "global_value": True,
            "instance_override": None,
            "has_instance_override": False,
            "session_override": False,
            "has_session_override": True,
            "effective": False,
            "source": "session",
        },
        "group_mention_requires_reply": {
            "global_value": True,
            "instance_override": None,
            "has_instance_override": False,
            "session_override": None,
            "has_session_override": False,
            "effective": True,
            "source": "global",
        },
        "group_attention_deferred": {
            "global_value": False,
            "instance_override": None,
            "has_instance_override": False,
            "session_override": None,
            "has_session_override": False,
            "effective": False,
            "source": "global",
        },
        "group_turn_budget": {
            "global_value": 7,
            "instance_override": None,
            "has_instance_override": False,
            "session_override": None,
            "has_session_override": False,
            "effective": 7,
            "source": "global",
        },
        "rest_may_skip": {
            "global_value": True,
            "instance_override": None,
            "has_instance_override": False,
            "session_override": None,
            "has_session_override": False,
            "effective": True,
            "source": "global",
        },
    }
    assert tuple(fields) == POLICY_FIELDS
    return {
        "field": FIELD,
        "global_value": True,
        "override": False,
        "effective": False,
        "source": "session",
        "global_revision": 0,
        "instance_revision": 0,
        "session_revision": 1,
        "origin_status": "not_applicable",
        "instance_scope": None,
        "instance_id": None,
        "fields": fields,
    }


def _valid_proposed_fields(view: dict) -> dict:
    fields = copy.deepcopy(view["fields"])
    fields[FIELD] = {
        "global_value": True,
        "instance_override": None,
        "has_instance_override": False,
        "session_override": None,
        "has_session_override": False,
        "effective": True,
        "source": "global",
    }
    return fields


def _exported(name: str) -> dict:
    directory = ROOT / "contracts" / "v1"
    index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    assert index["contracts"][name] == f"{name}.schema.json"
    return json.loads((directory / index["contracts"][name]).read_text(encoding="utf-8"))


def _validate_representations(model, payload: dict, openapi: dict, path: str, method: str) -> None:  # noqa: ANN001
    model.model_validate(payload)
    Draft202012Validator(_exported(model.__name__)).validate(payload)
    response = openapi["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]
    assert response == {"$ref": f"#/components/schemas/{model.__name__}"}
    Draft202012Validator({**response, "components": openapi["components"]}).validate(payload)


async def test_real_policy_responses_match_published_contracts(logged_in: TestEnv) -> None:
    window = await create_window(logged_in)
    path = "/v1/sessions/{session_id}/dialogue-policy"
    preview_path = f"{path}/preview"
    openapi = logged_in.app.openapi()

    async def read() -> dict:
        response = await logged_in.client.get(f"/v1/sessions/{window}/dialogue-policy")
        assert response.status_code == 200
        result = response.json()
        assert set(result) == VIEW_KEYS
        _validate_representations(WindowDialoguePolicyView, result, openapi, path, "get")
        return result

    async def modify(value: bool | None, global_value: bool) -> dict:
        before = await read()
        proposed = await preview(logged_in, window, value)
        assert set(proposed) == {"current", "proposed", "preview_token", "expires_at"}
        assert proposed["current"] == before
        expected_fields = copy.deepcopy(before["fields"])
        for key, field_view in before["fields"].items():
            if key == FIELD:
                continue
            assert field_view["has_instance_override"] is False
            assert field_view["instance_override"] is None
            assert field_view["has_session_override"] is False
            assert field_view["session_override"] is None
            assert field_view["effective"] == field_view["global_value"]
            assert field_view["source"] == "global"
        expected_fields[FIELD] = {
            "global_value": global_value,
            "instance_override": None,
            "has_instance_override": False,
            "session_override": value,
            "has_session_override": value is not None,
            "effective": global_value if value is None else value,
            "source": "global" if value is None else "session",
        }
        assert proposed["proposed"] == {
            "override": value,
            "effective": global_value if value is None else value,
            "source": "global" if value is None else "session",
            "fields": expected_fields,
        }
        _validate_representations(
            WindowDialoguePolicyPreview, proposed, openapi, preview_path, "post"
        )
        assert await read() == before
        response = await save(logged_in, window, value, proposed["preview_token"])
        assert response.status_code == 200, response.text
        result = response.json()
        assert set(result) == VIEW_KEYS
        assert result["global_value"] is global_value
        assert result["override"] is value
        assert result["effective"] is (global_value if value is None else value)
        assert result["source"] == ("global" if value is None else "session")
        assert result["fields"] == expected_fields
        _validate_representations(WindowDialoguePolicyView, result, openapi, path, "put")
        assert await read() == result
        return result

    initial = await read()
    assert initial["global_value"] is True and initial["override"] is None
    await modify(False, True)
    await modify(None, True)
    await publish(logged_in, False)
    await modify(True, False)
    await modify(None, False)


@pytest.mark.parametrize("model", [WindowDialoguePolicyView, WindowDialoguePolicyPreview])
def test_contract_rejects_missing_invalid_and_unknown_fields(model) -> None:  # noqa: ANN001
    view = _valid_view()
    proposal = {
        "override": None,
        "effective": True,
        "source": "global",
        "fields": _valid_proposed_fields(view),
    }
    valid = view if model is WindowDialoguePolicyView else {
        "current": view, "proposed": proposal, "preview_token": "token", "expires_at": 1,
    }
    validator = Draft202012Validator(_exported(model.__name__))
    model.model_validate(valid)
    validator.validate(valid)

    cases = []
    bad = copy.deepcopy(valid)
    target = bad if model is WindowDialoguePolicyView else bad["current"]
    target["override"] = "false"
    cases.append(bad)
    bad = copy.deepcopy(valid)
    target = bad if model is WindowDialoguePolicyView else bad["current"]
    target["global_value"] = None
    cases.append(bad)
    bad = copy.deepcopy(valid)
    target = bad if model is WindowDialoguePolicyView else bad["current"]
    del target["effective"]
    cases.append(bad)
    bad = copy.deepcopy(valid)
    bad["unexpected"] = 1
    cases.append(bad)
    if model is WindowDialoguePolicyPreview:
        bad = copy.deepcopy(valid)
        bad["proposed"]["unexpected"] = 1
        cases.append(bad)
    bad = copy.deepcopy(valid)
    target = bad if model is WindowDialoguePolicyView else bad["current"]
    target["fields"][FIELD]["session_override"] = "false"
    cases.append(bad)
    bad = copy.deepcopy(valid)
    target = bad if model is WindowDialoguePolicyView else bad["current"]
    target["fields"]["unexpected"] = target["fields"][FIELD]
    cases.append(bad)
    for bad in cases:
        with pytest.raises(ValidationError):
            model.model_validate(bad)
        assert not validator.is_valid(bad)


@pytest.mark.parametrize(
    "field", ["global_revision", "instance_revision", "session_revision", "expires_at"]
)
@pytest.mark.parametrize(
    ("value", "accepted"),
    [(1, True), (1.0, True), (1.1, False), (-1.0, False), (True, False), ("1", False)],
)
def test_json_integer_validation_matches_exported_schema(field, value, accepted) -> None:  # noqa: ANN001
    view = _valid_view()
    if field == "expires_at":
        model = WindowDialoguePolicyPreview
        payload = {
            "current": view,
            "proposed": {
                "override": None,
                "effective": True,
                "source": "global",
                "fields": _valid_proposed_fields(view),
            },
            "preview_token": "token",
            field: value,
        }
    else:
        model = WindowDialoguePolicyView
        payload = {**view, field: value}
    schema_accepts = Draft202012Validator(_exported(model.__name__)).is_valid(payload)
    try:
        model.model_validate_json(json.dumps(payload))
        model_accepts = True
    except ValidationError:
        model_accepts = False
    assert model_accepts is schema_accepts is accepted


async def test_missing_or_cross_origin_still_rejected(logged_in: TestEnv) -> None:
    window = await create_window(logged_in)
    endpoint = f"/v1/sessions/{window}/dialogue-policy/preview"
    assert (await logged_in.client.post(endpoint, json=change(False))).status_code == 403
    response = await logged_in.client.post(
        endpoint, json=change(False), headers={"Origin": "https://example.invalid"}
    )
    assert response.status_code == 403
