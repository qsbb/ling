"""访问日志的 message：一眼看出「谁请求了什么、结果如何」。

用户原话是「一长串的请求完成，到底请求完成了什么，能不能详细点」。日志页只
显示 message，细节全在 attributes 里要展开才看得到，所以 message 必须自己
说清楚：

- 命中动作名表 → `读取会话列表 → 200（12ms）`
- 未命中        → `GET /v1/messages/{message_id}/prompt → 404（3ms，未找到）`

两条硬约束在这里钉住：**只用路由模板**（不出现真实 id、不出现 query）、
**2xx 不加判断**而 4xx/5xx 各用不同的措辞。

这里用最小 Starlette 应用直接测中间件，不碰数据库：消息怎么拼是纯粹的
格式问题，真实接线由 `tests/integration/test_observability.py` 覆盖。
"""

from __future__ import annotations

import logging
import re

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from ling.api.observability import (
    _access_message,
    _safe_path,
    observability_middleware,
)

ACCESS_LOGGER = "ling.api.access"


def _make_app() -> Starlette:
    async def ok(request):  # noqa: ANN001, ANN202
        return JSONResponse({"ok": True})

    async def not_found(request):  # noqa: ANN001, ANN202
        return PlainTextResponse("没有", status_code=404)

    async def boom(request):  # noqa: ANN001, ANN202
        return PlainTextResponse("炸了", status_code=500)

    app = Starlette(
        routes=[
            Route("/v1/sessions", ok),
            Route("/v1/messages/{message_id}/prompt", not_found),
            Route("/v1/selftest/boom", boom),
            Route("/v1/logs", ok),
        ]
    )
    app.add_middleware(BaseHTTPMiddleware, dispatch=observability_middleware)
    return app


def _completed(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if getattr(record, "event_name", "") == "http_request_completed"
    ]


async def _get(path: str, caplog: pytest.LogCaptureFixture) -> str:
    async with AsyncClient(
        transport=ASGITransport(app=_make_app()), base_url="http://test"
    ) as client:
        with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER):
            await client.get(path)
    events = _completed(caplog)
    assert events, [record.getMessage() for record in caplog.records]
    return events[-1].getMessage()


# ------------------------------------------------------------ 拼装规则（纯函数）


def test_2xx_has_no_verdict_besides_the_status() -> None:
    """2xx 只在括号里给状态码与耗时——不写"成功"之类的判断。"""
    message = _access_message(
        method="GET", template="/v1/world/places", status=200, elapsed_ms=4.0
    )
    assert message == "GET /v1/world/places → 200（4ms）"


def test_hot_route_action_name_replaces_the_method_and_path() -> None:
    assert (
        _access_message(
            method="post", template="/v1/onebot/events", status=200, elapsed_ms=9.0
        )
        == "接收 OneBot 事件 → 200（9ms）"
    )
    assert (
        _access_message(
            method="GET", template="/v1/logs", status=200, elapsed_ms=2.5
        )
        == "读取日志 → 200（2.5ms）"
    )


def test_4xx_and_5xx_are_worded_differently() -> None:
    """同一路由，404 与 500 的措辞必须能分开「没找到」和「服务端出错」。"""
    missing = _access_message(
        method="GET", template="/v1/sessions/{session_id}", status=404, elapsed_ms=3.0
    )
    broken = _access_message(
        method="GET", template="/v1/sessions/{session_id}", status=500, elapsed_ms=8.0
    )
    assert missing == "读取会话详情 → 404（3ms，未找到）"
    assert broken == "读取会话详情 → 500（8ms，服务端错误）"


def test_unknown_status_falls_back_to_its_class() -> None:
    assert "，请求被拒绝" in _access_message(
        method="GET", template="/v1/x", status=418, elapsed_ms=1.0
    )
    assert "，服务端错误" in _access_message(
        method="GET", template="/v1/x", status=507, elapsed_ms=1.0
    )


# ------------------------------------------------- 没有路由模板时的脱敏兜底


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/sessions", "/sessions"),
        ("/", "/"),
        # 会话 id / 长 token 一律折成 {id}，而且后面的段不再展开
        ("/v1/sessions/sess_ab12cd34ef56/extra", "/v1/sessions/{id}"),
        ("/log/1728394", "/log/{id}"),
        ("/" + "a" * 40, "/{id}"),
    ],
)
def test_safe_path_redacts_ids(path: str, expected: str) -> None:
    assert _safe_path(path) == expected


# ------------------------------------------------------------ 走真实中间件


async def test_message_uses_route_template_not_the_real_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """真实 id 不出现在 message 里：日志页是长期保留的，且模板才能聚合。"""
    message = await _get("/v1/messages/msg_abc123456/prompt", caplog)
    assert re.fullmatch(
        r"GET /v1/messages/\{message_id\}/prompt → 404（[\d.]+ms，未找到）", message
    ), message
    assert "msg_abc123456" not in message


async def test_query_string_never_reaches_the_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    message = await _get("/v1/sessions?trace=abc123&q=秘密", caplog)
    assert message.startswith("读取会话列表 → 200（")
    assert "abc123" not in message and "秘密" not in message


async def test_hot_route_reaches_the_message_with_action_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    message = await _get("/v1/sessions", caplog)
    assert re.fullmatch(r"读取会话列表 → 200（[\d.]+ms）", message), message


async def test_500_message_says_what_went_wrong(
    caplog: pytest.LogCaptureFixture,
) -> None:
    message = await _get("/v1/selftest/boom", caplog)
    assert re.fullmatch(
        r"GET /v1/selftest/boom → 500（[\d.]+ms，服务端错误）", message
    ), message


async def test_polling_success_is_still_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """轮询路径成功时依旧不记（否则日志页自己把自己的缓冲刷满）。"""
    async with AsyncClient(
        transport=ASGITransport(app=_make_app()), base_url="http://test"
    ) as client:
        with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER):
            response = await client.get("/v1/logs")
    assert response.status_code == 200
    assert _completed(caplog) == [], [r.getMessage() for r in caplog.records]


async def test_include_router_prefix_is_restored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`include_router(prefix="/v1")` 那一层要补回模板上。

    FastAPI 的 `_IncludedRouter` 让 `scope["route"].path` 只剩子路由自己的
    路径（`/sessions`，没有 `/v1`）。不补的话日志会写成 `GET /sessions`，
    和真实入口对不上；这里同时证明动作名表是按**带前缀**的模板命中的。
    """
    from fastapi import APIRouter, FastAPI

    router = APIRouter()

    @router.get("/sessions")
    async def list_sessions():  # noqa: ANN202
        return {"sessions": []}

    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.add_middleware(BaseHTTPMiddleware, dispatch=observability_middleware)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER):
            await client.get("/v1/sessions")

    message = _completed(caplog)[-1].getMessage()
    assert re.fullmatch(r"读取会话列表 → 200（[\d.]+ms）", message), message
