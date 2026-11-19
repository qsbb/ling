"""Explicit, read-only public search. Only the current user's query leaves ling."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import httpx

from ling.capabilities.tools import ToolRegistry, ToolRisk, ToolSpec

WEB_SEARCH_TOOL_ID = "web.search"
_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_MAX_QUERY = 240
_MAX_RESPONSE = 256 * 1024
_RESULTS = 3
_REQUEST = re.compile(
    r"^(?:/search(?:\s+|[:：])|(?:请|能不能)?(?:帮我|给我)?(?:"
    r"(?:上网|联网|在网上)(?:搜索|搜一下|搜|查一下|查)|搜索一下|搜索|搜一下|搜个|搜一搜|搜)"
    r"\s*[:：]?\s*)(?P<query>.+)$",
    re.IGNORECASE,
)


def explicit_search_query(user_text: str) -> str | None:
    """No historical messages, model-composed query, or implicit lookup."""
    text = user_text.strip()
    if len(text.splitlines()) != 1:
        return None
    match = _REQUEST.fullmatch(text)
    if not match:
        return None
    query = match["query"].strip()
    return query if 0 < len(query) <= _MAX_QUERY else None


def register_web_search(registry: ToolRegistry, *, key: str, query: str) -> None:
    result: str | None = None

    async def search(args: dict[str, object]) -> str:
        nonlocal result
        if args:
            raise ValueError("网页搜索只使用用户明确输入的查询词，不接受额外参数")
        if result is None:
            result = await _search(key, query)
        return result

    registry.register(
        ToolSpec(
            tool_id=WEB_SEARCH_TOOL_ID,
            version="1.0.0",
            description=(
                "使用用户这条消息中的查询词搜索公开网页，无需参数。"
                "回答时注明来源链接；失败或没有结果时明确说明，不得编造。"
            ),
            risk=ToolRisk.read_only,
            requires_confirmation=False,
            reversible=True,
            timeout_seconds=8,
            model_callable=True,
            model_result_chars=3200,
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        search,
    )


async def _search(key: str, query: str) -> str:
    try:
        async with (
            httpx.AsyncClient(timeout=6, follow_redirects=False, trust_env=False) as client,
            client.stream(
                "GET", _ENDPOINT,
                params={"q": query, "count": _RESULTS},
                headers={"X-Subscription-Token": key, "Accept": "application/json"},
            ) as response,
        ):
            if response.status_code in {401, 403}:
                raise ValueError("网页搜索服务未授权，请检查部署的 Brave Search API 密钥")
            if response.status_code == 429:
                raise ValueError("网页搜索额度或频率受限，请稍后重试")
            if response.status_code != 200:
                raise ValueError("网页搜索服务暂不可用")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > _MAX_RESPONSE:
                    raise ValueError("网页搜索响应超过大小限制")
                body.extend(chunk)
    except httpx.HTTPError:
        raise ValueError("未能连接网页搜索服务，请检查网络后重试") from None

    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        raise ValueError("网页搜索服务返回了无法读取的响应") from None
    if not isinstance(data, dict):
        raise ValueError("网页搜索服务返回了无法读取的响应")
    web = data.get("web", {})
    rows = web.get("results", []) if isinstance(web, dict) else []
    results = []
    for row in rows[:_RESULTS] if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        url = row.get("url")
        if not isinstance(url, str) or len(url) > 400 or any(c.isspace() for c in url):
            continue
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username:
                continue
        except ValueError:
            continue
        results.append({
            "url": url,
            "title": str(row.get("title") or "")[:100],
            "snippet": str(row.get("description") or "")[:240],
        })
    return json.dumps({"results": results}, ensure_ascii=False)
