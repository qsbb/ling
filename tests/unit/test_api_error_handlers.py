"""Cover v1 framework failures through the production app and registered handlers."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, field_validator

from ling.app import create_app
from ling.config import Settings
from ling.contracts.v1 import ErrorResponse


@pytest.mark.parametrize("built", [False, True])
def test_v1_routing_errors_preserve_status_and_allow(tmp_path: Path, built: bool) -> None:
    dist = tmp_path / "web"
    if built:
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("Ling", encoding="utf-8")
    with_no_lifespan = TestClient(create_app(Settings(web_dist=dist)))
    not_found = with_no_lifespan.get("/v1/not-an-endpoint")
    assert not_found.status_code == 404
    assert ErrorResponse.model_validate(not_found.json()).error.code == "not_found"
    for method in ("GET", "PUT"):
        wrong_method = with_no_lifespan.request(method, "/v1/auth/login")
        assert wrong_method.status_code == 405
        assert wrong_method.headers["Allow"] == "POST"
        assert ErrorResponse.model_validate(wrong_method.json()).error.code == "invalid_request"
    legacy = with_no_lifespan.get("/api/not-an-endpoint")
    assert legacy.status_code == 404
    assert set(legacy.json()) == {"detail"}
    if built:
        assert with_no_lifespan.get("/v10/client-path").text == "Ling"


class _ValidationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str

    @field_validator("label")
    @classmethod
    def _validate_label(cls, value: str) -> str:
        if value != "ok":
            raise ValueError(value)
        return value


def test_v1_validation_reveals_field_but_not_input_or_parser_details() -> None:
    app = create_app()

    @app.post("/v1", include_in_schema=False)
    def validate(payload: _ValidationInput) -> dict:
        return payload.model_dump()

    @app.post("/v10/synthetic", include_in_schema=False)
    def legacy(payload: _ValidationInput) -> dict:
        return payload.model_dump()

    client = TestClient(app)
    assert client.post("/v1", json={"label": "ok"}).status_code == 200
    for payload, field in (
        ({"label": "synthetic-invalid-value"}, "label"),
        ({"label": "ok", "synthetic-unknown-key": "synthetic-invalid-value"}, "body"),
    ):
        response = client.post("/v1", json=payload)
        assert response.status_code == 422
        error = ErrorResponse.model_validate(response.json()).error
        assert error.code == "invalid_request"
        assert error.field == error.detail == field
        assert error.trace_id == response.headers["X-Trace-Id"]
        assert "synthetic-invalid-value" not in response.text
        assert "synthetic-unknown-key" not in response.text

    malformed = client.post(
        "/v1", content='{"label": "synthetic-invalid-json', headers={"Content-Type": "application/json"}
    )
    assert malformed.status_code == 422
    assert ErrorResponse.model_validate(malformed.json()).error.field == "body"
    assert "synthetic-invalid-json" not in malformed.text
    outside = client.post("/v10/synthetic", json={})
    assert outside.status_code == 422 and isinstance(outside.json()["detail"], list)


def test_unhandled_api_error_has_trace_without_exception_detail() -> None:
    app = create_app()

    @app.post("/v1/synthetic-failure")
    def fail() -> None:
        raise RuntimeError("synthetic internal data")

    response = TestClient(app, raise_server_exceptions=False).post("/v1/synthetic-failure")
    assert response.status_code == 500
    error = ErrorResponse.model_validate(response.json()).error
    assert error.code == "internal" and error.retriable is True
    assert error.trace_id == response.headers["X-Trace-Id"]
    assert "synthetic internal data" not in response.text
