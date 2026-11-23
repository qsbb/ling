"""Search with the current user's query and read the first public result."""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlsplit

import httpx

from ling.capabilities.tools import ToolRegistry, ToolRisk, ToolSpec
from ling.capabilities.web_read import PublicWebReadError, read_public_page

WEB_SEARCH_TOOL_ID = "web.search"
_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_MAX_QUERY = 240
_MAX_RESPONSE = 256 * 1024
_RESULTS = 3
_PAGE_EXCERPT_CHARS = 1800
_COMMAND = re.compile(r"^(?:/search(?:$|[\s:：])|搜索\s*[:：])", re.IGNORECASE)


def has_explicit_search_command(user_text: str) -> bool:
    """Recognize slash/colon command syntax even when the query is invalid."""
    return _COMMAND.match(user_text.strip()) is not None


def explicit_search_query(user_text: str) -> str | None:
    """Parse the deliberate slash or colon command."""
    text = user_text.strip()
    command = _COMMAND.match(text)
    return _query_text(text[command.end():]) if command else None


def _query_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not 0 < len(text) <= _MAX_QUERY or any(ord(char) < 32 for char in text):
        return None
    return text


def register_web_search(
    registry: ToolRegistry, *, key: str, query: str | None = None, user_text: str = "",
) -> None:
    result: str | None = None
    sent_query: str | None = None

    async def search(args: dict[str, object]) -> str:
        nonlocal result, sent_query
        selected: str | None
        if query is not None:
            if args:
                raise ValueError("本次搜索已指定查询词")
            selected = query
        else:
            selected = _query_text(args.get("query"))
            if selected is None or selected not in user_text:
                raise ValueError("请从用户当前消息中原样摘取查询词")
        if sent_query is not None:
            if selected == sent_query and result is not None:
                return result
            raise ValueError("这一轮已经查询过一次")
        sent_query = selected
        result = await _search(key, selected)
        return result

    parameters: dict[str, object] = {"type": "object", "properties": {}, "additionalProperties": False}
    if query is None:
        parameters["properties"] = {
            "query": {"type": "string", "minLength": 1, "maxLength": _MAX_QUERY},
        }
        parameters["required"] = ["query"]
    query_help = "本次查询词由用户指定，无需参数。" if query else "query 要从当前用户消息中原样摘取。"
    registry.register(
        ToolSpec(
            tool_id=WEB_SEARCH_TOOL_ID,
            version="1.0.0",
            description=(
                "查询需要公网资料才能回答的问题，并尝试读取第一条结果的正文。"
                f"{query_help}"
                "有正文时引用实际读到的来源链接；只拿到摘要时说明依据是搜索摘要。"
            ),
            risk=ToolRisk.read_only,
            requires_confirmation=False,
            reversible=True,
            timeout_seconds=14,
            model_callable=True,
            model_result_chars=7000,
            input_schema=parameters,
        ),
        search,
    )


async def _search(key: str, query: str) -> str:
    try:
        async with (
            asyncio.timeout(7),
            httpx.AsyncClient(timeout=6, follow_redirects=False, trust_env=False) as client,
            client.stream(
                "GET", _ENDPOINT,
                params={"q": query, "count": _RESULTS},
                headers={
                    "X-Subscription-Token": key,
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
            ) as response,
        ):
            if response.status_code in {401, 403}:
                raise ValueError("网页搜索服务未授权，请检查部署的 Brave Search API 密钥")
            if response.status_code == 429:
                raise ValueError("网页搜索额度或频率受限，请稍后重试")
            if response.status_code != 200:
                raise ValueError("网页搜索服务暂不可用")
            if response.headers.get("content-encoding", "identity").lower().strip() != "identity":
                raise ValueError("网页搜索服务返回了不支持的压缩响应")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > _MAX_RESPONSE:
                    raise ValueError("网页搜索响应超过大小限制")
                body.extend(chunk)
    except (TimeoutError, httpx.HTTPError):
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
            if (
                parsed.scheme not in {"https", "http"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
            ):
                continue
        except ValueError:
            continue
        results.append({
            "url": url,
            "title": str(row.get("title") or "")[:100],
            "snippet": str(row.get("description") or "")[:240],
        })
    payload: dict[str, object] = {"results": results}
    if results:
        url = results[0]["url"]
        try:
            page = await read_public_page(url)
        except PublicWebReadError as exc:
            payload["page_error"] = {"url": url, "reason": exc.reason, "detail": str(exc)}
        else:
            payload["page"] = {
                "url": page.url,
                "title": page.title,
                "text": page.text[:_PAGE_EXCERPT_CHARS],
                "truncated": page.truncated or len(page.text) > _PAGE_EXCERPT_CHARS,
            }
    return json.dumps(payload, ensure_ascii=False)
