"""请求级可观测性：trace_id 出现在响应头、错误体与日志里。"""

from __future__ import annotations

import logging

import pytest

pytestmark = pytest.mark.w0_web


async def test_response_carries_trace_header(logged_in) -> None:
    response = await logged_in.client.get("/v1/sessions")
    assert response.status_code == 200
    trace = response.headers.get("X-Trace-Id")
    assert trace and len(trace) == 32


async def test_client_supplied_trace_is_echoed(logged_in) -> None:
    """前端可以在 header 里带 trace 进来，方便把界面上的 ID 拿回来查。"""
    mine = "abc123def456abc1"
    response = await logged_in.client.get(
        "/v1/sessions", headers={"X-Trace-Id": mine}
    )
    assert response.headers.get("X-Trace-Id") == mine


@pytest.mark.parametrize("bad", ["short", "not-hex-!!!!", "z" * 70])
async def test_invalid_trace_header_is_replaced(logged_in, bad: str) -> None:
    """非法 trace 一律丢弃并重新生成——不能让任意字符串塞进日志字段。"""
    response = await logged_in.client.get("/v1/sessions", headers={"X-Trace-Id": bad})
    trace = response.headers.get("X-Trace-Id")
    assert trace and trace != bad and len(trace) == 32


async def test_error_body_carries_the_same_trace(logged_in) -> None:
    """错误响应体里也要带 trace_id，用户才能把 ID 报给我们。"""
    response = await logged_in.client.get("/v1/sessions/does-not-exist")
    assert response.status_code >= 400
    body = response.json()
    assert body["error"]["trace_id"] == response.headers.get("X-Trace-Id")


async def test_access_log_records_the_request(web_env, caplog) -> None:
    """访问日志要能被筛出来，并且带状态码与耗时。"""
    login = await web_env.client.post(
        "/v1/auth/login", json={"username": "tester", "password": "tester-password"}
    )
    assert login.status_code == 200

    with caplog.at_level(logging.INFO, logger="ling.api.access"):
        await web_env.client.get("/v1/sessions")

    events = [
        r for r in caplog.records
        if getattr(r, "event_name", "") == "http_request_completed"
    ]
    assert events, [r.getMessage() for r in caplog.records]
    attributes = events[-1].attributes
    assert attributes["http.response.status_code"] == 200
    assert "duration_ms" in attributes
