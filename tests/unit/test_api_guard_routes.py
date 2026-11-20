"""Check dependencies on the actual mounted methods, not on source text."""

from fastapi import routing

from ling.api.guards import OwnerGuard, OwnerWriteGuard, require_owner
from ling.app import create_app

_JSON_WRITES = {
    ("PUT", "/v1/settings/draft"),
    ("POST", "/v1/settings/publish"),
    ("POST", "/v1/settings/rollback"),
    ("PUT", "/v1/settings/providers/{provider_id}/secret"),
    ("PUT", "/v1/settings/connectors/{scope}"),
    ("POST", "/v1/settings/connectors/{scope}/instances"),
    ("PUT", "/v1/settings/connectors/{scope}/instances/{instance_id}"),
    ("PUT", "/v1/settings/connectors/{scope}/instances/{instance_id}/enabled"),
    ("POST", "/v1/settings/connectors/{scope}/instances/{instance_id}/dialogue-policy/preview"),
    ("PUT", "/v1/settings/connectors/{scope}/instances/{instance_id}/dialogue-policy"),
    ("POST", "/v1/sessions/{session_id}/origin-binding/preview"),
    ("PUT", "/v1/sessions/{session_id}/origin-binding"),
    ("POST", "/v1/sessions/{session_id}/dialogue-policy/preview"),
    ("PUT", "/v1/sessions/{session_id}/dialogue-policy"),
    ("PUT", "/v1/persona"),
    ("POST", "/v1/personas"),
    ("PUT", "/v1/personas/{persona_id}"),
    ("PATCH", "/v1/accounts/subjects/{subject_id}"),
    ("POST", "/v1/accounts/subjects/merge"),
    ("POST", "/v1/accounts/bind"),
    ("POST", "/v1/tools/{tool_id}/call"),
}

_BODYLESS_WRITES = {
    ("DELETE", "/v1/settings/providers/{provider_id}/secret"),
    ("POST", "/v1/settings/providers/{provider_id}/probe"),
    ("DELETE", "/v1/settings/connectors/{scope}/instances/{instance_id}"),
    ("POST", "/v1/settings/connectors/reset"),
    ("DELETE", "/v1/personas/{persona_id}"),
    ("POST", "/v1/personas/{persona_id}/activate"),
    ("POST", "/v1/accounts/subjects/{source_id}/unmerge"),
    ("POST", "/v1/accounts/{account_id}/prove"),
    ("DELETE", "/v1/accounts/{account_id}"),
    ("POST", "/v1/health/ops/notify"),
}

_UPDATE_WRITES = {
    ("POST", "/v1/version/update-requests"),
    ("POST", "/v1/version/image-update-requests"),
    ("POST", "/v1/version/image-update-checks"),
}

_OWNER_READS = {
    "/v1/health/model",
    "/v1/health/ops",
    "/v1/health/stats",
    "/v1/sessions/{session_id}/origin-binding",
    "/v1/settings/connectors/operations/{operation_id}",
    "/v1/settings/connectors/{scope}/instances/{instance_id}/dialogue-policy",
}


def _dependencies(dependant):
    for child in dependant.dependencies:
        yield child.call
        yield from _dependencies(child)


def test_mounted_control_plane_dependencies_match_policy() -> None:
    app = create_app()
    # FastAPI versions that lazily include routers expose the effective routes
    # through this iterator; older versions flatten to APIRoute on inclusion.
    iter_contexts = getattr(routing, "iter_route_contexts", iter)
    uploads = {("POST", "/v1/settings/speech/voice-reference")}
    control_plane = _JSON_WRITES | _UPDATE_WRITES | _BODYLESS_WRITES | uploads
    routes = {}
    for route in iter_contexts(app.routes):
        if not hasattr(route, "dependant"):
            continue
        for method in route.methods or ():
            key = (method, route.path)
            if key in control_plane:
                assert key not in routes, f"duplicate management route: {key}"
            routes[key] = tuple(_dependencies(route.dependant))

    for group, content_type in (
        (_JSON_WRITES | _UPDATE_WRITES, "application/json"),
        (_BODYLESS_WRITES, None),
        (uploads, "multipart/form-data"),
    ):
        for key in sorted(group):
            calls = routes[key]
            guard = OwnerWriteGuard(content_type, update_origin_details=key in _UPDATE_WRITES)
            assert guard in calls and require_owner in calls, key

    for path in sorted(_OWNER_READS):
        assert require_owner in routes["GET", path], path
    assert OwnerGuard(missing_message="消息不存在") in routes["GET", "/v1/messages/{message_id}/prompt"]

    for key in (
        ("GET", "/v1/logs"),
        ("POST", "/v1/sessions"),
        ("POST", "/v1/sessions/{session_id}/turns"),
        ("POST", "/v1/turns/{turn_id}/retry"),
        ("POST", "/v1/turns/{turn_id}/stop"),
        ("PUT", "/v1/settings/preferences"),
        ("POST", "/v1/memory"),
        ("POST", "/v1/onebot/events"),
        ("POST", "/v1/qqbot/events"),
    ):
        assert not any(isinstance(call, (OwnerGuard, OwnerWriteGuard)) for call in routes[key]), key
