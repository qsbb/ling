"""Wiring helpers: register external plugin tools and parse explicit plugin commands.

These helpers live in ``capabilities`` but are called only from ``kernel/turn.py``
after the audience/policy gate has decided that external tools are allowed.
They never choose endpoints from chat text or model arguments.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ling.capabilities.external_plugins_service import (
    COMMAND_PREFIX,
    ExternalPluginService,
    to_public,
)
from ling.capabilities.tools import ToolRisk, ToolRegistry, ToolSpec
from ling.infra.models import ExternalPluginRow

EXPLICIT_COMMAND_PATTERN = re.compile(
    rf"^{re.escape(COMMAND_PREFIX)}\s+([a-z][a-z0-9_]{{0,39}}(?:\.[a-z][a-z0-9_]{{0,39}})*)\s+([a-z][a-z0-9_]{{0,63}})\s+(.+)$"
)
MAX_COMMAND_INPUT = 2000


def parse_external_command(text: str) -> tuple[str, str, str] | None:
    """Parse ``/plugincmd <plugin_id> <command> <text>`` exactly.

    The plugin id and command are identifiers, never URLs; the trailing text is
    the only user-provided value forwarded to the plugin.
    """
    match = EXPLICIT_COMMAND_PATTERN.fullmatch(text.strip())
    if match is None:
        return None
    plugin_id, command, value = match.groups()
    if not 0 < len(value.strip()) <= MAX_COMMAND_INPUT:
        return None
    return plugin_id, command, value.strip()


def external_tool_id(plugin: ExternalPluginRow, tool_name: str) -> str:
    """Stable, provider-safe tool name; dots are allowed by the existing adapters."""
    return f"plugin_{plugin.id.replace('.', '_')}_{tool_name}"


def external_tool_spec(
    plugin: ExternalPluginRow, declaration: dict[str, Any]
) -> ToolSpec | None:
    """Build the spec only; handler is supplied by the caller's registry."""
    name = str(declaration.get("name") or "")
    schema = declaration.get("input_schema")
    if not name or not isinstance(schema, dict):
        return None
    return ToolSpec(
        tool_id=external_tool_id(plugin, name),
        version=plugin.version,
        description=str(declaration.get("description") or "")[:500],
        risk=ToolRisk.read_only,
        requires_confirmation=False,
        reversible=True,
        timeout_seconds=8.0,
        input_schema=schema,
        output_untrusted=True,
        model_callable=True,
        model_result_chars=3200,
    )


def external_tool_handler(
    plugin: ExternalPluginRow, name: str, *, service: ExternalPluginService
) -> Any:
    async def handler(args: dict[str, Any]) -> str:
        return await service.invoke(
            plugin=plugin,
            method="tools.call",
            params={"tool": name, "arguments": args},
        )

    return handler


def register_external_tools(
    registry: ToolRegistry, plugin: ExternalPluginRow, *, service: ExternalPluginService
) -> list[str]:
    """Register this plugin's declared tools into the given per-turn registry."""
    registered: list[str] = []
    for declaration in _declarations(plugin, "tools"):
        spec = external_tool_spec(plugin, declaration)
        if spec is None:
            continue
        name = str(declaration.get("name") or "")
        registry.register(
            spec,
            external_tool_handler(plugin, name, service=service),
            replace=True,
        )
        registered.append(spec.tool_id)
    return registered


def find_command(plugin: ExternalPluginRow, name: str) -> dict[str, Any] | None:
    for declaration in _declarations(plugin, "commands"):
        if declaration.get("name") == name:
            return declaration
    return None


def _declarations(plugin: ExternalPluginRow, key: str) -> list[dict[str, Any]]:
    from ling.capabilities.external_plugins_service import _as_list

    return [item for item in _as_list(getattr(plugin, key)) if isinstance(item, dict)]


def public_summary(plugin: ExternalPluginRow) -> str:
    """Bounded manifest summary for management views; no endpoint or secret."""
    view = to_public(plugin)
    return json.dumps(
        {
            "id": view["id"],
            "name": view["name"],
            "version": view["version"],
            "capabilities": view["granted_capabilities"],
        },
        ensure_ascii=False,
    )
