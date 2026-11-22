"""HTTP runtime for administrator-registered external plugins.

The host never loads plugin code and never lets chat text choose a URL.
All requests go to a fixed endpoint registered by an owner/admin, with bounded
timeouts, response limits, and no redirects.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from ling import __version__ as HOST_VERSION
from ling.capabilities.external_plugin_contract import (
    API_VERSION,
    SUPPORTED_CAPABILITIES,
    ExternalPluginError,
    HostCall,
    HostResult,
    InvokeFailure,
    InvokeRequest,
    InvokeSuccess,
    PluginEvent,
    PluginManifest,
    version_in_range,
)

MANIFEST_PATH = "/v1/manifest"
INVOKE_PATH = "/v1/invoke"
EVENTS_PATH = "/v1/events"

DEFAULT_TIMEOUT_SECONDS = 6.0
MANIFEST_TIMEOUT_SECONDS = 4.0
EVENT_TIMEOUT_SECONDS = 2.0
MAX_RESPONSE_BYTES = 16 * 1024
MAX_HOST_ROUNDS = 4


class PluginUnavailable(RuntimeError):
    """The endpoint could not be reached or returned unusable data."""


class PluginRejected(RuntimeError):
    """The endpoint returned a well-formed error or refused the capability."""


@dataclass(frozen=True)
class PluginHealth:
    ok: bool
    error: str | None = None
    checked_at: datetime | None = None
    manifest: PluginManifest | None = None


def _endpoint(base: str, path: str) -> str:
    return base.rstrip("/") + path


async def _read_bounded(response: httpx.Response, *, limit: int = MAX_RESPONSE_BYTES) -> dict[str, Any]:
    if response.status_code != 200:
        raise PluginUnavailable("插件没有返回成功结果")
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding != "identity":
        raise PluginUnavailable("插件返回了不支持的压缩响应")
    raw = bytearray()
    async for chunk in response.aiter_bytes():
        if len(raw) + len(chunk) > limit:
            raise PluginUnavailable("插件响应超出大小限制")
        raw.extend(chunk)
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise PluginUnavailable("插件响应不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise PluginUnavailable("插件响应不是 JSON 对象")
    return payload


class PluginClient:
    """Stateless HTTP client; all per-plugin state stays in PostgreSQL."""

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout

    async def fetch_manifest(self, endpoint: str) -> PluginManifest:
        try:
            async with httpx.AsyncClient(
                timeout=MANIFEST_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
            ) as client:
                async with client.stream(
                    "GET",
                    _endpoint(endpoint, MANIFEST_PATH),
                    headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                ) as response:
                    payload = await _read_bounded(response)
        except (httpx.HTTPError, httpx.InvalidURL, PluginUnavailable) as exc:
            raise PluginUnavailable("无法读取插件 manifest") from exc
        try:
            manifest = PluginManifest.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - contract boundary
            raise PluginUnavailable("插件 manifest 不符合 v1 契约") from exc
        if manifest.api_version != API_VERSION:
            raise PluginUnavailable("插件 API 版本不受支持")
        if not version_in_range(
            HOST_VERSION, minimum=manifest.min_host_version, maximum=manifest.max_host_version
        ):
            raise PluginUnavailable("插件与当前 Ling 版本不兼容")
        unknown = set(manifest.capabilities) - SUPPORTED_CAPABILITIES
        if unknown:
            raise PluginUnavailable("插件声明了不受支持的能力")
        return manifest

    async def health(self, endpoint: str) -> PluginHealth:
        try:
            manifest = await self.fetch_manifest(endpoint)
        except (PluginUnavailable, PluginRejected) as exc:
            return PluginHealth(ok=False, error=str(exc), checked_at=datetime.now(UTC))
        return PluginHealth(ok=True, checked_at=datetime.now(UTC), manifest=manifest)

    async def invoke(
        self,
        *,
        endpoint: str,
        plugin_id: str,
        method: str,
        params: dict[str, Any],
        request_id: str,
        host_executor: Any | None = None,
    ) -> str:
        """Run one tool/command call; optional ``host_executor`` handles host_calls.

        ``host_executor(calls, request_id) -> list[HostResult]`` may be async or sync.
        It is only called for capabilities the plugin declared and the admin granted;
        the caller (runtime service) enforces that before invoking this method.
        """
        host_results: list[HostResult] = []
        for _round in range(MAX_HOST_ROUNDS):
            request = InvokeRequest(
                api_version=API_VERSION,
                request_id=request_id,
                plugin_id=plugin_id,
                method=method,  # type: ignore[arg-type]
                params=params,
                host_results=host_results,
            )
            payload = await self._post(endpoint, INVOKE_PATH, request.model_dump(mode="json"))
            if "error" in payload:
                try:
                    failure = InvokeFailure.model_validate(payload)
                except Exception as exc:  # noqa: BLE001 - contract boundary
                    raise PluginUnavailable("插件错误响应不符合 v1 契约") from exc
                raise PluginRejected(str(failure.error.get("message") or "插件拒绝了本次调用"))
            try:
                success = InvokeSuccess.model_validate(payload)
            except Exception as exc:  # noqa: BLE001 - contract boundary
                raise PluginUnavailable("插件响应不符合 v1 契约") from exc
            if not success.host_calls:
                text = success.result.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise PluginUnavailable("插件没有返回可读结果")
                return text[:8000]
            if host_executor is None:
                raise PluginRejected("插件请求了未授权的宿主能力")
            result = host_executor(success.host_calls, request_id)
            if asyncio.iscoroutine(result):
                result = await result
            host_results = list(result)
        raise PluginRejected("插件宿主调用次数超过上限")

    async def emit_event(self, *, endpoint: str, event: PluginEvent) -> bool:
        """Best-effort event delivery; returns True on 2xx ack, never raises."""
        try:
            async with httpx.AsyncClient(
                timeout=EVENT_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
            ) as client:
                response = await client.post(
                    _endpoint(endpoint, EVENTS_PATH),
                    json=event.model_dump(mode="json"),
                    headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                )
        except (httpx.HTTPError, httpx.InvalidURL):
            return False
        return 200 <= response.status_code < 300

    async def _post(self, endpoint: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, follow_redirects=False, trust_env=False
            ) as client:
                async with client.stream(
                    "POST",
                    _endpoint(endpoint, path),
                    json=body,
                    headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                ) as response:
                    return await _read_bounded(response)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise PluginUnavailable("无法连接插件服务") from exc


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "EVENT_TIMEOUT_SECONDS",
    "MANIFEST_PATH",
    "INVOKE_PATH",
    "EVENTS_PATH",
    "PluginClient",
    "PluginHealth",
    "PluginRejected",
    "PluginUnavailable",
    "HostCall",
]
