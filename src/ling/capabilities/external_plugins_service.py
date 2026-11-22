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
    HostCall,
    HostResult,
    PluginManifest,
    RegisterBody,
    version_in_range,
)
from ling.capabilities.external_plugins_client import (
    PluginClient,
    PluginRejected,
    PluginUnavailable,
)
from ling.capabilities.tool_plugins import (
    MANIFEST_NAME as LEGACY_MANIFEST_NAME,
)
from ling.capabilities.tool_plugins import (
    load_tool_plugins,
)
from ling.contracts.v1 import new_id
from ling.infra.db import short_session_scope
from ling.infra.models import ExternalPluginRow, ExternalPluginStateRow

MAX_STATE_VALUE_CHARS = 4096
DEFAULT_STATE_TTL_SECONDS = 7 * 24 * 3600
MAX_STATE_TTL_SECONDS = 30 * 24 * 3600
COMMAND_PREFIX = "/plugincmd"
LEGACY_COMMAND_PREFIX = "/plugin"

_LEGACY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class PluginServiceError(RuntimeError):
    """Safe-to-display service error with no endpoint or secret details."""


class PluginIdentityMismatch(PluginServiceError):
    """The endpoint presented a different manifest identity."""


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


def allows(plugin: ExternalPluginRow, capability: str) -> bool:
    return (
        plugin.enabled
        and plugin.status in {"ready", "registered"}
        and capability in _as_list(plugin.capabilities)
        and capability in _as_list(plugin.granted_capabilities)
    )


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
        await self._verify_identity(row, manifest)
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
            assert health.manifest is not None
            await self._verify_identity(row, health.manifest)
            if not version_in_range(
                HOST_VERSION,
                minimum=health.manifest.min_host_version,
                maximum=health.manifest.max_host_version,
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

    async def _verify_identity(self, row: ExternalPluginRow, manifest: PluginManifest) -> None:
        if manifest.id == row.id:
            return
        error = PluginIdentityMismatch("插件 ID 与端点声明不一致，已停用")
        row.enabled = False
        row.status = "identity_mismatch"
        row.last_error = str(error)
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        raise error

    async def unregister(self, plugin_id: str) -> None:
        row = await self._session.get(ExternalPluginRow, plugin_id, with_for_update=True)
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
        result["reachable"] = health.ok and health.manifest is not None and health.manifest.id == plugin_id
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

    def host_call_executor(
        self, plugin: ExternalPluginRow, *, invocation_capability: str = "host.state"
    ) -> Any:
        plugin_id, endpoint, created_at = plugin.id, plugin.endpoint, plugin.created_at

        async def execute(calls: list[HostCall], request_id: str) -> list[HostResult]:
            # Check the registration under the same short transaction as the state
            # operation. Release the lock before sending results over HTTP.
            async with short_session_scope() as session:
                current = await session.get(ExternalPluginRow, plugin_id, with_for_update=True)
                if (
                    current is None
                    or current.endpoint != endpoint
                    or current.created_at != created_at
                    or not allows(current, invocation_capability)
                    or not allows(current, "host.state")
                ):
                    return [
                        HostResult(call_id=call.call_id, ok=False, error="capability_not_granted")
                        for call in calls
                    ]
                results: list[HostResult] = []
                for call in calls:
                    if call.value is not None and len(call.value) > MAX_STATE_VALUE_CHARS:
                        results.append(
                            HostResult(call_id=call.call_id, ok=False, error="state_value_too_large")
                        )
                        continue
                    value = await self._state(session, plugin_id, call)
                    results.append(HostResult(call_id=call.call_id, ok=True, value=value))
                return results

        return execute

    @staticmethod
    async def _state(session: AsyncSession, plugin_id: str, call: HostCall) -> str | None:
        now = datetime.now(UTC)
        row = await session.get(ExternalPluginStateRow, (plugin_id, call.key))
        if call.op == "get":
            if row is None:
                return None
            if row.expires_at is not None and row.expires_at <= now:
                await session.delete(row)
                await session.flush()
                return None
            return row.value
        if row is None:
            row = ExternalPluginStateRow(plugin_id=plugin_id, key=call.key)
        row.value = call.value or ""
        row.expires_at = now + timedelta(seconds=DEFAULT_STATE_TTL_SECONDS)
        row.updated_at = now
        session.add(row)
        await session.flush()
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
        if not allows(plugin, cap):
            raise PluginServiceError("插件未获得该能力授权")
        rid = request_id or new_id("plgreq")
        try:
            return await self._client.invoke(
                endpoint=plugin.endpoint,
                plugin_id=plugin.id,
                method=method,
                params=params,
                request_id=rid,
                host_executor=self.host_call_executor(plugin, invocation_capability=cap),
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
    "PluginIdentityMismatch",
    "PluginServiceError",
    "to_public",
]
