"""Ling 外部插件 v1 的可公开契约。

只描述跨 HTTP 边界的 JSON 形状、版本兼容与能力白名单；不 import 任何领域实现。
破坏性变化必须升 ``API_VERSION`` 的 major，未知必需字段失败关闭（extra=forbid）。

设计取舍见 ``docs/design/external-plugins-v1-design.md``；该设计目录按仓库约定不推送。
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

API_VERSION: Literal["ling-plugin/1"] = "ling-plugin/1"
MANIFEST_VERSION: Literal["v1"] = "v1"

_ID_PATTERN = r"[a-z][a-z0-9_]{0,39}(\.[a-z][a-z0-9_]{0,39})*"
_NAME_PATTERN = r"[a-z][a-z0-9_]{0,63}"
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_VERSION_RANGE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

PluginId = Annotated[str, Field(pattern=rf"^{_ID_PATTERN}$")]
CapabilityName = Annotated[str, Field(pattern=r"^(tools|commands|events|host\.state)$")]

#: 本次公开的能力全集。逐项声明、逐项授权；未知能力直接拒绝整份 manifest。
SUPPORTED_CAPABILITIES: frozenset[str] = frozenset(
    {"tools", "commands", "events", "host.state"}
)

#: 本期开放的事件类型。稳定 ID 与重试语义见 :class:`PluginEvent`。
SUPPORTED_EVENTS: frozenset[str] = frozenset({"turn.completed"})


class ExternalPluginError(ValueError):
    """契约层错误。调用方不得把内部路径或未脱敏值带进用户可见文案。"""


def parse_semver(value: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(value)
    if match is None:
        raise ExternalPluginError("version must be X.Y.Z")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def version_in_range(version: str, *, minimum: str, maximum: str) -> bool:
    """闭区间兼容判定；边界相等视为兼容。"""
    try:
        current = parse_semver(version)
        low = parse_semver(minimum)
        high = parse_semver(maximum)
    except ExternalPluginError:
        return False
    if low > high:
        return False
    return low <= current <= high


def validate_endpoint(value: str) -> str:
    """固定 http(s) 端点：无 query/fragment/userinfo/空白，避免 SSRF 面扩大。"""
    from urllib.parse import urlsplit

    if not value or any(ch.isspace() for ch in value) or "\\" in value:
        raise ValueError("invalid endpoint")
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ValueError("endpoint must be a fixed HTTP(S) URL without credentials or query")
    if parts.port == 0:
        raise ValueError("invalid port")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ToolDeclaration(_Strict):
    name: Annotated[str, Field(pattern=rf"^{_NAME_PATTERN}$")]
    description: Annotated[str, Field(min_length=1, max_length=500)]
    input_schema: dict[str, Any]

    @field_validator("input_schema")
    @classmethod
    def _check_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("type") != "object":
            raise ValueError("tool input_schema must be an object schema")
        if not isinstance(value.get("properties", {}), dict):
            raise ValueError("tool input_schema.properties must be an object")
        return value


class CommandDeclaration(_Strict):
    name: Annotated[str, Field(pattern=rf"^{_NAME_PATTERN}$")]
    pattern: Annotated[str, Field(min_length=1, max_length=512)]

    @field_validator("pattern")
    @classmethod
    def _check_pattern(cls, value: str) -> str:
        try:
            import re as _re

            _re.compile(value)
        except _re.error as exc:  # pragma: no cover - 依赖库行为
            raise ValueError("invalid command pattern") from exc
        return value


class PluginManifest(_Strict):
    """插件自述。管理员登记时由宿主 GET ``{endpoint}/v1/manifest`` 取得。"""

    manifest_version: Literal["v1"]
    id: PluginId
    version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    name: Annotated[str, Field(min_length=1, max_length=64)]
    description: Annotated[str, Field(min_length=0, max_length=500)] = ""
    api_version: Literal["ling-plugin/1"]
    min_host_version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    max_host_version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    capabilities: list[CapabilityName] = Field(max_length=len(SUPPORTED_CAPABILITIES))
    events: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.]{0,63}$")]] = Field(
        default_factory=list, max_length=16
    )
    tools: list[ToolDeclaration] = Field(default_factory=list, max_length=16)
    commands: list[CommandDeclaration] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def _consistent(self) -> PluginManifest:
        caps = set(self.capabilities)
        if len(caps) != len(self.capabilities):
            raise ValueError("duplicate capabilities")
        if "tools" in caps and not self.tools:
            raise ValueError("tools capability requires at least one tool")
        if "commands" in caps and not self.commands:
            raise ValueError("commands capability requires at least one command")
        if "events" in caps and not self.events:
            raise ValueError("events capability requires at least one event")
        if "tools" not in caps and self.tools:
            raise ValueError("tools declared without capability")
        if "commands" not in caps and self.commands:
            raise ValueError("commands declared without capability")
        if "events" not in caps and self.events:
            raise ValueError("events declared without capability")
        unknown = set(self.events) - SUPPORTED_EVENTS
        if unknown:
            raise ValueError("unsupported event type")
        tool_names = [tool.name for tool in self.tools]
        command_names = [command.name for command in self.commands]
        if len(set(tool_names)) != len(tool_names) or len(set(command_names)) != len(command_names):
            raise ValueError("duplicate tool or command name")
        return self

    def tool_names(self) -> frozenset[str]:
        return frozenset(tool.name for tool in self.tools)

    def command_names(self) -> frozenset[str]:
        return frozenset(command.name for command in self.commands)


class HostCall(_Strict):
    """插件在 ``invoke`` 响应里请求的受控宿主能力。"""

    call_id: Annotated[str, Field(min_length=1, max_length=64)]
    capability: Literal["host.state"]
    op: Literal["get", "set"]
    key: Annotated[str, Field(min_length=1, max_length=128)]
    value: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> HostCall:
        if self.op == "set" and self.value is None:
            raise ValueError("state.set requires value")
        if self.op == "get" and self.value is not None:
            raise ValueError("state.get must not carry value")
        return self


class HostResult(_Strict):
    call_id: Annotated[str, Field(min_length=1, max_length=64)]
    ok: bool
    value: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> HostResult:
        if self.ok and self.error is not None:
            raise ValueError("successful host result must not carry error")
        if not self.ok and self.error is None:
            raise ValueError("failed host result requires error")
        return self


class InvokeRequest(_Strict):
    api_version: Literal["ling-plugin/1"]
    request_id: Annotated[str, Field(min_length=1, max_length=128)]
    plugin_id: PluginId
    method: Literal["tools.call", "commands.call"]
    params: dict[str, Any]
    host_results: list[HostResult] = Field(default_factory=list, max_length=16)


class InvokeSuccess(_Strict):
    api_version: Literal["ling-plugin/1"]
    request_id: Annotated[str, Field(min_length=1, max_length=128)]
    result: Annotated[dict[str, Any], Field(default_factory=dict)]
    host_calls: list[HostCall] = Field(default_factory=list, max_length=16)


class InvokeFailure(_Strict):
    api_version: Literal["ling-plugin/1"]
    request_id: Annotated[str, Field(min_length=1, max_length=128)]
    error: Annotated[dict[str, Any], Field(default_factory=dict)]

    @field_validator("error")
    @classmethod
    def _check_error(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value.get("code"), str) or not isinstance(value.get("message"), str):
            raise ValueError("error must contain code and message")
        return value


class PluginEvent(_Strict):
    """一次宿主事件。``event_id`` 是稳定键，插件必须据此去重。

    Worker 有限次数重试，插件返回 2xx 即视为收到，重复投递时插件必须幂等。
    事件 payload 只带 ID/时间/次数，不含正文、密钥或凭据。
    """

    api_version: Literal["ling-plugin/1"]
    event_id: Annotated[str, Field(min_length=1, max_length=128)]
    type: Literal["turn.completed"]
    occurred_at: Annotated[str, Field(min_length=1, max_length=64)]
    data: dict[str, Any]


class RegisterBody(_Strict):
    """管理端登记请求。凭据可留空；不接收 secret 明文到响应。"""

    endpoint: Annotated[str, Field(min_length=1, max_length=512)]

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, value: str) -> str:
        return validate_endpoint(value)

    expected_id: PluginId | None = None
    granted_capabilities: list[CapabilityName] = Field(default_factory=list, max_length=len(SUPPORTED_CAPABILITIES))
    enabled: bool = False

    @field_validator("granted_capabilities")
    @classmethod
    def _unique_caps(cls, value: list[CapabilityName]) -> list[CapabilityName]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate granted capabilities")
        return value
