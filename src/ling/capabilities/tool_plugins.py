"""Operator-installed HTTP tools. No dynamic code or model-supplied destination."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ling.capabilities.tools import ToolRegistry, ToolRisk, ToolSpec

MANIFEST_NAME = "tool-plugins.json"
_MAX_MANIFEST = 24 * 1024
_MAX_RESPONSE = 16 * 1024
_MAX_INPUT = 400
_ID_PATTERN = r"[a-z][a-z0-9_]{0,39}"


class PluginManifestError(ValueError):
    """Invalid manifest; callers must not disclose paths or parsed values."""


class ToolPlugin(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: str = Field(pattern=rf"^{_ID_PATTERN}$")
    name: str = Field(min_length=1, max_length=64)
    endpoint: str = Field(min_length=1, max_length=512)
    enabled: bool = False

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        if any(char.isspace() for char in value) or "\\" in value:
            raise ValueError("invalid endpoint")
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.fragment
            or parts.query
        ):
            raise ValueError("use a fixed HTTP(S) endpoint without credentials or query")
        # Invalid port access raises rather than deferring failure until the request.
        if parts.port == 0:
            raise ValueError("invalid port")
        return value

    @property
    def tool_id(self) -> str:
        return f"plugin_{self.id}"


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: str = Field(pattern=r"^v1$")
    plugins: list[ToolPlugin] = Field(max_length=16)

    @model_validator(mode="after")
    def unique_ids(self) -> PluginManifest:
        if len({plugin.id for plugin in self.plugins}) != len(self.plugins):
            raise ValueError("duplicate plugin ID")
        return self


def load_tool_plugins(data_dir: Path) -> tuple[ToolPlugin, ...]:
    try:
        with (data_dir / MANIFEST_NAME).open("rb") as stream:
            raw = stream.read(_MAX_MANIFEST + 1)
        if len(raw) > _MAX_MANIFEST:
            raise ValueError("manifest too large")
        return tuple(PluginManifest.model_validate_json(raw).plugins)
    except FileNotFoundError:
        return ()
    except (OSError, ValueError) as exc:
        raise PluginManifestError("插件清单无法读取或格式错误，请检查部署配置。") from exc


def parse_plugin_command(text: str) -> tuple[str, str] | None:
    parts = text.strip().split(maxsplit=2)
    if len(parts) != 3 or parts[0] != "/plugin":
        return None
    plugin_id, value = parts[1:]
    if re.fullmatch(_ID_PATTERN, plugin_id) is None or not 0 < len(value) <= _MAX_INPUT:
        return None
    return plugin_id, value


def register_tool_plugin(registry: ToolRegistry, plugin: ToolPlugin, *, text: str) -> None:
    async def execute(args: dict[str, object]) -> str:
        if args:
            raise ValueError("插件只允许使用当前指令显式提供的输入")
        return await _invoke(plugin, text)

    registry.register(
        ToolSpec(
            tool_id=plugin.tool_id,
            version="1.0.0",
            description="执行用户在本条消息中明确指定的只读插件，无参数。以实际结果为准。",
            risk=ToolRisk.read_only,
            requires_confirmation=False,
            reversible=True,
            timeout_seconds=8,
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_untrusted=True,
            model_callable=True,
            model_result_chars=3200,
        ),
        execute,
    )


async def _invoke(plugin: ToolPlugin, text: str) -> str:
    try:
        async with (
            httpx.AsyncClient(timeout=6, follow_redirects=False, trust_env=False) as client,
            client.stream(
                "POST", plugin.endpoint,
                json={"schema_version": "v1", "text": text},
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            ) as response,
        ):
            if response.status_code != 200:
                raise ValueError("插件没有返回成功结果")
            if response.headers.get("content-encoding", "identity").lower().strip() != "identity":
                raise ValueError("插件返回了不支持的压缩响应")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                if len(raw) + len(chunk) > _MAX_RESPONSE:
                    raise ValueError("插件响应超出大小限制")
                raw.extend(chunk)
    except (httpx.HTTPError, httpx.InvalidURL):
        raise ValueError("无法连接插件服务") from None
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError("插件响应不是有效 JSON") from None
    if not isinstance(payload, dict) or payload.get("schema_version") != "v1":
        raise ValueError("插件响应缺少 v1 版本标识")
    result = payload.get("result")
    if not isinstance(result, str) or not result.strip():
        raise ValueError("插件没有返回可读结果")
    return result
