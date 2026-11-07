"""群名采集：能采就采，采不到就安静地返回 None。

会话列表要显示「群名为标题」，而群名只有平台知道（OneBot ``get_group_info``）。
这条外呼有三个必须守住的边界，缺一个都会让\"看一眼会话列表\"变慢或变脆：

1. **失败不能抛**：没实现这个 API 的实现（404）、断连、超时都是常态，
   群名只是标题，不该让主功能跟着挂。
2. **失败也要缓存**：否则每次开页面都重新打一遍不可达的 NapCat。
3. **成功缓存**：群名很少改，6 小时够用，不值得每次开页面都问一次。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from connectors.onebot.connector import OneBotV11Connector
from connectors.onebot.settings import OneBotConfig


def _connector(handler, **overrides) -> OneBotV11Connector:  # noqa: ANN001
    config = OneBotConfig(
        api_base="http://onebot.test:5700",
        access_token="",
        self_id="10001",
        instance_id="onebot-unit",
        **overrides,
    )
    return OneBotV11Connector(
        config, http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


@pytest.mark.asyncio
async def test_group_name_is_fetched_with_get_group_info() -> None:
    seen: list[tuple[str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url).rsplit("/", 1)[-1], json.loads(request.content or b"{}")))
        return httpx.Response(
            200,
            json={"status": "ok", "retcode": 0, "data": {"group_name": " 测试群 "}},
        )

    connector = _connector(handler)
    assert await connector.fetch_group_name("100200400") == "测试群"
    assert seen == [("get_group_info", {"group_id": "100200400"})]


@pytest.mark.asyncio
async def test_group_name_is_cached_after_first_hit() -> None:
    """开两次页面只问一次平台——群名不是实时数据。"""
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"group_name": "甲群"}}
        )

    connector = _connector(handler)
    assert await connector.fetch_group_name("1") == "甲群"
    assert await connector.fetch_group_name("1") == "甲群"
    assert calls == 1


@pytest.mark.asyncio
async def test_missing_api_falls_back_to_none() -> None:
    """很多 OneBot 实现没有 ``get_group_info``：404 → None，不抛。"""
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    connector = _connector(handler)
    assert await connector.fetch_group_name("2") is None
    # 失败也缓存：否则每次读列表都要等一次 404
    assert await connector.fetch_group_name("2") is None
    assert calls == 1


@pytest.mark.asyncio
async def test_unreachable_platform_falls_back_to_none() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        raise httpx.ConnectError("boom", request=request)

    connector = _connector(handler)
    assert await connector.fetch_group_name("3") is None


@pytest.mark.asyncio
async def test_timeout_falls_back_to_none_instead_of_hanging() -> None:
    """采集挂在读接口上，必须有自己的短超时（比 ``timeout_seconds`` 短得多）。"""

    async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        await asyncio.sleep(0.2)
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"group_name": "慢群"}}
        )

    connector = _connector(handler)
    connector.group_name_timeout = 0.01
    assert await connector.fetch_group_name("4") is None


@pytest.mark.asyncio
async def test_empty_group_name_is_treated_as_unknown() -> None:
    """``data`` 是空/字段缺失时返回 None，不要回一个空字符串当名字。"""

    async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": None})

    connector = _connector(handler)
    assert await connector.fetch_group_name("5") is None


@pytest.mark.asyncio
async def test_blank_group_id_does_not_call_the_platform() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        raise AssertionError("不该外呼")

    connector = _connector(handler)
    assert await connector.fetch_group_name("  ") is None


@pytest.mark.asyncio
async def test_cache_is_bounded() -> None:
    """长跑进程里不能每个群都留一条——这里只证明淘汰不会把缓存搞坏。"""

    async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"group_name": "群"}}
        )

    connector = _connector(handler)
    connector.group_name_cache_max = 2
    for group_id in ("1", "2", "3"):
        assert await connector.fetch_group_name(group_id) == "群"
    assert len(connector._group_name_cache) == 2
