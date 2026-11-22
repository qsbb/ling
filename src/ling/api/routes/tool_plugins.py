"""Read-only operator view of the installed tool plugins and conversation readiness."""

from fastapi import APIRouter, Request

from ling.api.deps import ConfigDep, CurrentAuth, DbSession, RouterDep, SettingsDep
from ling.api.errors import LingError
from ling.capabilities.external_plugin_contract import SUPPORTED_CAPABILITIES
from ling.capabilities.external_plugins_service import ExternalPluginService, to_public
from ling.capabilities.tool_plugins import PluginManifestError, load_tool_plugins
from ling.contracts.v1 import ErrorCode
from ling.ports.model import NATIVE_TOOLS_PROFILES, kind_may_have_native_tools

router = APIRouter(tags=["tools"])


@router.get("/settings/tool-status")
async def tool_status(
    auth: CurrentAuth, config: ConfigDep, model_router: RouterDep, settings: SettingsDep,
    request: Request, session: DbSession,
) -> dict:
    if auth.user.role not in {"owner", "admin"}:
        raise LingError(ErrorCode.forbidden, "工具设置仅管理员可查看", status_code=403)

    effective = await config.effective()
    profiles: dict[str, str] = {}
    for profile in sorted(NATIVE_TOOLS_PROFILES):
        resolved = effective.resolve_profile(profile)
        provider = effective.providers.get(resolved.provider_id or "")
        if not resolved.usable or provider is None:
            status = "unbound"
        elif not kind_may_have_native_tools(provider.kind):
            status = "unsupported_protocol"
        elif not model_router.native_tools_support(effective, profile):
            status = "instance_not_enabled"
        else:
            status = "ready"
        profiles[profile] = status

    try:
        installed = load_tool_plugins(settings.data_dir)
        manifest_valid = True
    except PluginManifestError:
        installed = ()
        manifest_valid = False
    external = await ExternalPluginService(session).list_registered()
    return {
        "tools_enabled": effective.dialogue.tools_enabled,
        "search_configured": bool(settings.brave_search_api_key),
        "profiles": profiles,
        "manifest_valid": manifest_valid,
        "plugins": [
            {"id": plugin.id, "name": plugin.name, "enabled": plugin.enabled}
            for plugin in installed
        ],
        "external_plugins": [to_public(row) for row in external],
        "external_capabilities": sorted(SUPPORTED_CAPABILITIES),
    }
