"""Capture payload shapes after real HTTP routing and serialization."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient, Response

from ling.api.observability import route_template
from tests.integration.conftest import TestEnv
from tests.regression.snapshots import shape, snapshot

CLIENTS = json.loads(Path(__file__).with_name("client_routes.json").read_text(encoding="utf-8"))
WEB: dict[str, str] = CLIENTS["web"]
STREAMED = {"sendTurn", "retryTurn", "stopTurn"}


class Capture:
    def __init__(self, env: TestEnv) -> None:
        self.env = env
        self.route: str | None = None
        self.samples: dict[str, dict] = {}

    async def app(self, scope, receive, send) -> None:
        await self.env.app(scope, receive, send)
        route = scope.get("route")
        if isinstance(route, APIRoute) and scope["method"] in route.methods:
            self.route = route_template(Request(scope))

    @asynccontextmanager
    async def open(self) -> AsyncIterator[Capture]:
        async with AsyncClient(
            transport=ASGITransport(app=self.app),
            base_url="http://testserver",
            cookies=dict(self.env.client.cookies),
            headers={"Origin": "http://testserver"},
        ) as client:
            self.client = client
            yield self

    async def raw(
        self,
        name: str,
        *,
        status: int = 200,
        variant: str = "",
        path_args: dict[str, str] | None = None,
        url: str | None = None,
        media_type: str | None = "application/json",
        maps: frozenset[str] = frozenset(),
        opaque_maps: frozenset[str] = frozenset(),
        **kwargs: Any,
    ) -> Response:
        operation = WEB[name]
        method, template = operation.split(" ", 1)
        target = url or template.format(**(path_args or {}))
        self.route = None
        response = await self.client.request(method, target, **kwargs)
        assert response.status_code == status, (
            f"{operation} {variant}: {response.status_code} {response.text}"
        )
        assert self.route == template, f"{operation} matched {self.route!r}"
        content_type = response.headers.get("content-type", "").split(";", 1)[0] or None
        assert content_type == media_type, f"{operation}: received {content_type}"
        case = f"{name}/{variant}" if variant else name
        assert case not in self.samples, f"duplicate fixture case: {case}"
        sampled = {"operation": operation, "status": status, "media_type": content_type}
        if status == 204 or content_type is None:
            assert response.content == b""
        elif content_type == "application/json":
            sampled["body"] = shape(response.json(), maps=maps, opaque_maps=opaque_maps)
        else:
            assert response.content
        self.samples[case] = sampled
        return response

    async def json(self, name: str, **kwargs: Any) -> Any:
        return (await self.raw(name, **kwargs)).json()

    def check(self, area: str) -> None:
        snapshot(f"api-{area}", self.samples)
