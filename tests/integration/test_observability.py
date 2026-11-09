"""请求级可观测性：trace_id 出现在响应头、错误体与日志里。"""

from __future__ import annotations

import logging
import re

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
    # 属性里放的是**路由模板**，不是带 id 的真实路径：日志页长期保留，
    # 按模板才能聚合，也不会把某个人的会话 id 混进去。
    assert attributes["http.route"] == "/v1/sessions"


# ------------------------------------------------------- message 要自解释


def _access_events(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if getattr(record, "event_name", "") == "http_request_completed"
    ]


async def test_message_names_the_hot_route_in_chinese(logged_in, caplog) -> None:
    """高频路由走中文动作名：不用点开 attributes 也知道刚发生了什么。"""
    with caplog.at_level(logging.INFO, logger="ling.api.access"):
        await logged_in.client.get("/v1/sessions")

    message = _access_events(caplog)[-1].getMessage()
    assert re.fullmatch(r"读取会话列表 → 200（[\d.]+ms）", message), message


async def test_message_uses_the_route_template(logged_in, caplog) -> None:
    """路径参数用模板表示：真实 id 不进 message（也不算 query）。"""
    with caplog.at_level(logging.INFO, logger="ling.api.access"):
        response = await logged_in.client.get(
            "/v1/messages/msg_not_a_real_id/prompt?debug=1"
        )
    assert response.status_code == 404

    message = _access_events(caplog)[-1].getMessage()
    assert re.fullmatch(
        r"GET /v1/messages/\{message_id\}/prompt → 404（[\d.]+ms，未找到）", message
    ), message
    assert "msg_not_a_real_id" not in message
    assert "debug=1" not in message


async def test_polling_route_stays_quiet_when_it_succeeds(logged_in, caplog) -> None:
    """轮询成功照旧不记：否则日志页每 2 秒的一条会把自己的缓冲刷满。"""
    with caplog.at_level(logging.INFO, logger="ling.api.access"):
        response = await logged_in.client.get("/v1/logs")
    assert response.status_code == 200
    assert _access_events(caplog) == [], [
        record.getMessage() for record in caplog.records
    ]


async def test_access_log_entry_carries_the_trace_id(logged_in, caplog) -> None:
    """访问日志那条自己也要带 trace_id——日志页里它才是"对得上用户那次请求"的锚点。

    `reset_context` 原来在记访问日志**之前**执行，于是这条日志是唯一一条
    没有 trace_id 的：用户在错误页上拿到 id，回来却查不到对应的那次请求。

    ## 为什么这里断言"日志页真的看得到"，而不是只翻 `caplog`

    这条用例在完整套件里反复偶发失败，根因不是产品行为，而是**取证据的方式**：

    - 早先它读进程级环形缓冲（`recent_logs`），前面上千条用例会把缓冲刷满；
    - 改用 `caplog` 之后仍偶发——`caplog` 的记录是 root 上某个 handler 写进去
      的，而 `ContextFilter` 挂在哪一层、哪个 handler 先装，取决于**前面用例
      执行顺序**。换句话说，断言拿到的是"当时那套 handler 配置"的产物，
      而不是用户实际会看到的东西。

    用户实际依赖的路径是唯一的：`GET /v1/logs` 返回的条目里有没有
    `trace_id`（前端的日志页就是照这个字段筛选的）。所以这里直接断言那条
    路径——它不依赖 handler 装配顺序，也不会被别的用例影响。

    顺带把「`LogEntry.to_dict` 会输出 trace_id」这件事钉住：一旦有人删掉
    那个字段，日志页就再也对不上请求，而单纯翻日志对象的用例发现不了。
    """
    # 环形缓冲是**进程级单例**，前面的用例会把它刷满、也有用例会主动清空它
    # （`test_message_recall` / `test_recall_tool` 都会）。这里先清空，
    # 让"本次请求那条日志"一定在窗口里——清空**不改变**被测行为，只是把
    # "最后 N 条里有没有我这条"变成确定的。
    from ling.logging_setup import clear_log_buffer

    clear_log_buffer()
    response = await logged_in.client.get("/v1/sessions")
    trace = response.headers["X-Trace-Id"]
    assert trace

    # 走用户真实路径：日志页读的就是这个接口。
    logs = await logged_in.client.get("/v1/logs", params={"limit": 200})
    assert logs.status_code == 200, logs.text
    items = logs.json()["items"]

    matched = [
        item
        for item in items
        if item.get("event_name") == "http_request_completed"
        and item.get("trace_id") == trace
    ]
    assert matched, (
        "日志页里查不到这次请求的 trace_id；"
        f"本次 trace={trace}，接口返回里出现过的 trace_id："
        f"{sorted({i.get('trace_id') for i in items if i.get('trace_id')})[:5]}"
    )
    # 那一行还要能自解释（用户原话：一长串「请求完成」看不出完成了什么）。
    assert "读取会话列表" in str(matched[-1].get("message") or "")
