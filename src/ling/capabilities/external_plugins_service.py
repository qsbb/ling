"""Domain service for registered external plugins.

Owns persistence, capability grants, event IDs, and the bounded host-state API.
No plugin code is imported or executed inside Ling.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ling import __version__ as HOST_VERSION
from ling.capabilities.external_plugin_contract import (
    SUPPORTED_CAPABILITIES,
    ExternalPluginError,
    HostCall,
    HostResult,
    InvokeFailure,
    PluginManifest,
    RegisterBody,
    version_in_range,
)
from ling.capabilities.external_plugins_client import (
    PluginClient,
    PluginHealth,
    PluginRejected,
    PluginUnavailable,
)
from ling.capabilities.tool_plugins import (
    MANIFEST_NAME as LEGACY_MANIFEST_NAME,
    load_tool_plugins,
)
from ling.contracts.v1 import new_id
from ling.infra.models import ExternalPluginRow, ExternalPluginStateRow

MAX_STATE_VALUE_CHARS = 4096
DEFAULT_STATE_TTL_SECONDS = 7 * 24 * 3600
MAX_STATE_TTL_SECONDS = 30 * 24 * 3600
COMMAND_PREFIX = "/plugincmd"
LEGACY_COMMAND_PREFIX = "/plugin"

_LEGACY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class PluginServiceError(RuntimeError):
    """Safe-to-display service error with no endpoint or secret details."""


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _safe_error(exc: BaseException) -> str:
    """Return a bounded internal status string; never echoed to chat users."""
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def to_public(row: ExternalPluginRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "version": row.version,
        "api_version": row.api_version,
        "min_host_version": row.min_host_version,
        "max_host_version": row.max_host_version,
        "capabilities": _as_list(row.capabilities),
        "events": _as_list(row.events),
        "tools": [item.get("name") for item in _as_list(row.tools) if isinstance(item, dict)],
        "commands": [item.get("name") for item in _as_list(row.commands) if isinstance(item, dict)],
        "granted_capabilities": _as_list(row.granted_capabilities),
        "enabled": bool(row.enabled),
        "status": row.status,
        "last_error": row.last_error,
        "last_checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None,
    }


class ExternalPluginService:
    def __init__(self, session: AsyncSession, *, client: PluginClient | None = None) -> None:
        self._session = session
        self._client = client or PluginClient()

    async def list(self) -> list[ExternalPluginRow]:
        result = await self._session.execute(
            select(ExternalPluginRow).order_by(ExternalPluginRow.id)
        )
        return list(result.scalars().all())

    async def get(self, plugin_id: str) -> ExternalPluginRow | None:
        return await self._session.get(ExternalPluginRow, plugin_id)

    async def enabled_plugins(self) -> list[ExternalPluginRow]:
        return [row for row in await self.list() if row.enabled and row.status in {"ready", "registered"}]

    async def register(
        self, body: RegisterBody, *, force: bool = False
    ) -> ExternalPluginRow:
        manifest = await self._client.fetch_manifest(body.endpoint)
        if body.expected_id is not None and manifest.id != body.expected_id:
            raise PluginServiceError("插件 ID 与端点声明不一致")
        granted = [cap for cap in body.granted_capabilities if cap in manifest.capabilities]
        existing = await self.get(manifest.id)
        if existing is not None and not force:
            raise PluginServiceError("插件已登记，请先取消登记或使用同步接口")
        row = existing or ExternalPluginRow(id=manifest.id)
        row.name = manifest.name
        row.endpoint = body.endpoint
        row.version = manifest.version
        row.api_version = manifest.api_version
        row.min_host_version = manifest.min_host_version
        row.max_host_version = manifest.max_host_version
        row.capabilities = list(manifest.capabilities)
        row.events = list(manifest.events)
        row.tools = [tool.model_dump(mode="json") for tool in manifest.tools]
        row.commands = [command.model_dump(mode="json") for command in manifest.commands]
        row.granted_capabilities = granted
        row.enabled = body.enabled
        row.status = "ready"
        row.last_error = None
        row.last_checked_at = datetime.now(UTC)
        row.updated_at = datetime.now(UTC)
        self._session.add(row)
        await self._session.flush()
        return row

    async def sync(self, plugin_id: str) -> ExternalPluginRow:
        row = await self.get(plugin_id)
        if row is None:
            raise PluginServiceError("插件不存在")
        manifest = await self._client.fetch_manifest(row.endpoint)
        if manifest.id != plugin_id:
            raise PluginServiceError("插件 ID 与端点声明不一致")
        row.name = manifest.name
        row.version = manifest.version
        row.api_version = manifest.api_version
        row.min_host_version = manifest.min_host_version
        row.max_host_version = manifest.max_host_version
        row.capabilities = list(manifest.capabilities)
        row.events = list(manifest.events)
        row.tools = [tool.model_dump(mode="json") for tool in manifest.tools]
        row.commands = [command.model_dump(mode="json") for command in manifest.commands]
        row.granted_capabilities = [
            cap for cap in _as_list(row.granted_capabilities) if cap in manifest.capabilities
        ]
        row.status = "ready"
        row.last_error = None
        row.last_checked_at = datetime.now(UTC)
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        return row

    async def set_enabled(self, plugin_id: str, enabled: bool) -> ExternalPluginRow:
        row = await self.get(plugin_id)
        if row is None:
            raise PluginServiceError("插件不存在")
        if enabled:
            health = await self._client.health(row.endpoint)
            if not health.ok:
                row.status = "unavailable"
                row.last_error = _safe_error(PluginUnavailable(health.error or "插件不可用"))
                row.enabled = False
                row.updated_at = datetime.now(UTC)
                await self._session.flush()
                raise PluginServiceError("插件不可用，未启用")
            if not version_in_range(
                HOST_VERSION,
                minimum=health.manifest.min_host_version,  # type: ignore[union-attr]
                maximum=health.manifest.max_host_version,  # type: ignore[union-attr]
            ):
                row.status = "incompatible"
                row.last_error = "插件与当前 Ling 版本不兼容"
                row.enabled = False
                row.updated_at = datetime.now(UTC)
                await self._session.flush()
                raise PluginServiceError("插件与当前 Ling 版本不兼容")
            row.status = "ready"
            row.last_error = None
        row.enabled = enabled
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        return row

    async def unregister(self, plugin_id: str) -> None:
        row = await self.get(plugin_id)
        if row is None:
            raise PluginServiceError("插件不存在")
        await self._session.execute(
            update(ExternalPluginStateRow).where(ExternalPluginStateRow.plugin_id == plugin_id).values(
                value="", expires_at=datetime.now(UTC)
            )
        )
        await self._session.delete(row)
        await self._session.flush()

    async def status(self, plugin_id: str) -> dict[str, Any]:
        row = await self.get(plugin_id)
        if row is None:
            raise PluginServiceError("插件不存在")
        health = await self._client.health(row.endpoint)
        result = to_public(row)
        result["reachable"] = health.ok
        result["health_checked_at"] = health.checked_at.isoformat() if health.checked_at else None
        return result

    async def list_legacy(self, data_dir: Any) -> list[dict[str, Any]]:
        """Old ``tool-plugins.json`` view; kept for migration and old /plugin path."""
        from pathlib import Path

        try:
            plugins = load_tool_plugins(Path(data_dir))
        except Exception:  # noqa: BLE001 - never disclose manifest internals
            return []
        return [
            {
                "id": plugin.id,
                "name": plugin.name,
                "enabled": plugin.enabled,
                "endpoint": None,
                "source": LEGACY_MANIFEST_NAME,
            }
            for plugin in plugins
        ]

    def host_call_executor(self, plugin: ExternalPluginRow) -> Any:
        """Only host.state calls that were declared AND granted are executed."""
        granted = set(_as_list(plugin.granted_capabilities))
        allowed = "host.state" in granted and "host.state" in _as_list(plugin.capabilities)

        async def execute(calls: list[HostCall], request_id: str) -> list[HostResult]:
            results: list[HostResult] = []
            for call in calls:
                if not allowed or call.capability != "host.state":
                    results.append(
                        HostResult(call_id=call.call_id, ok=False, error="capability_not_granted")
                    )
                    continue
                if len(call.key) > 128 or (call.value is not None and len(call.value) > MAX_STATE_VALUE_CHARS):
                    results.append(
                        HostResult(call_id=call.call_id, ok=False, error="state_value_too_large")
                    )
                    continue
                try:
                    value = await self._state(plugin.id, call)
                    results.append(HostResult(call_id=call.call_id, ok=True, value=value))
                except PluginServiceError as exc:
                    results.append(HostResult(call_id=call.call_id, ok=False, error=str(exc)))
            return results

        return execute

    async def _state(self, plugin_id: str, call: HostCall) -> str | None:
        now = datetime.now(UTC)
        if call.op == "get":
            row = await self._session.get(ExternalPluginStateRow, (plugin_id, call.key))
            if row is None:
                return None
            if row.expires_at is not None and row.expires_at <= now:
                await self._session.delete(row)
                await self._session.flush()
                return None
            return row.value
        row = await self._session.get(ExternalPluginStateRow, (plugin_id, call.key))
        if row is None:
            row = ExternalPluginStateRow(plugin_id=plugin_id, key=call.key)
        row.value = call.value or ""
        row.expires_at = now + timedelta(seconds=DEFAULT_STATE_TTL_SECONDS)
        row.updated_at = now
        self._session.add(row)
        await self._session.flush()
        return None

    async def invoke(
        self,
        *,
        plugin: ExternalPluginRow,
        method: str,
        params: dict[str, Any],
        request_id: str | None = None,
    ) -> str:
        if not plugin.enabled or plugin.status not in {"ready", "registered"}:
            raise PluginServiceError("插件未启用")
        cap = "tools" if method == "tools.call" else "commands"
        granted = set(_as_list(plugin.granted_capabilities))
        if cap not in granted or cap not in _as_list(plugin.capabilities):
            raise PluginServiceError("插件未获得该能力授权")
        rid = request_id or new_id("plgreq")
        try:
            return await self._client.invoke(
                endpoint=plugin.endpoint,
                plugin_id=plugin.id,
                method=method,
                params=params,
                request_id=rid,
                host_executor=self.host_call_executor(plugin),
            )
        except (PluginUnavailable, PluginRejected) as exc:
            await self._mark_failure(plugin, exc)
            raise PluginServiceError("插件调用失败") from exc

    async def _mark_failure(self, plugin: ExternalPluginRow, exc: BaseException) -> None:
        plugin.status = "unavailable"
        plugin.last_error = _safe_error(exc)
        plugin.updated_at = datetime.now(UTC)
        await self._session.flush()

    async def emit_completed(
        self,
        *,
        plugin: ExternalPluginRow,
        turn_id: str,
        window_id: str,
        subject_id: str,
        attempt: int = 1,
        event_id: str | None = None,
    ) -> bool:
        """Stable per-attempt event ID; repeated calls may redeliver the same event."""
        if not plugin.enabled or "events" not in _as_list(plugin.granted_capabilities):
            return False
        if "turn.completed" not in _as_list(plugin.events):
            return False
        from ling.capabilities.external_plugin_contract import API_VERSION, PluginEvent

        event = PluginEvent(
            api_version=API_VERSION,
            event_id=event_id or f"evt_turn_completed_{turn_id}_{attempt}_{plugin.id}",
            type="turn.completed",
            occurred_at=datetime.now(UTC).isoformat(),
            data={
                "turn_id": turn_id,
                "window_id": window_id,
                "subject_id": subject_id,
                "attempt": attempt,
            },
        )
        try:
            return await self._client.emit_event(endpoint=plugin.endpoint, event=event)
        except Exception as exc:  # noqa: BLE001 - event delivery is never on the chat critical path
            import logging
            logging.getLogger("ling.plugins").warning("event emit failed: %r", exc)
            return False


__all__ = [
    "COMMAND_PREFIX",
    "LEGACY_COMMAND_PREFIX",
    "ExternalPluginService",
    "PluginServiceError",
    "to_public",
]
