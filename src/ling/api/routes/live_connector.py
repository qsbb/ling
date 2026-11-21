from __future__ import annotations

import hmac

from connectors.inbound_access import parse_source_ips, source_allowed
from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from ling.api.errors import LingError, not_found
from ling.contracts.v1 import ErrorCode
from ling.infra.models import ConnectorInstance
from ling.infra.security import hash_token
from ling.kernel.live.events import (
    CUSTOM_SCOPE,
    BroadcastWindow,
    Identifier,
    account_namespace,
    plan_from_config,
)
from ling.kernel.live.room import active_session, request_stop
from ling.settings.connector_instances import (
    ConnectorInstanceError,
    ConnectorInstanceService,
    normalize_instance_id,
)


class CustomConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: str = Field(min_length=1, max_length=64)
    room_id: Identifier
    enabled: bool = True
    source_ips: str = Field(default="", max_length=4096)
    token: SecretStr | None = None
    signing_secret: SecretStr | None = None
    stage_token: SecretStr | None = None
    public_topic: str = Field(default="", max_length=400)
    max_minutes: int = Field(default=30, ge=1, le=240)
    obs_control: bool = False
    autonomous_window: BroadcastWindow | None = None

    @model_validator(mode="after")
    def require_obs_control(self) -> CustomConfig:
        if self.autonomous_window and not self.obs_control:
            raise ValueError("自主开播需要允许控制 OBS 推流")
        return self


def custom_view(row: ConnectorInstance) -> dict:
    settings = row.custom_config or {}
    return {
        "id": row.instance_id,
        "display_name": row.display_name,
        "enabled": row.enabled,
        "feature": settings.get("feature"),
        "account_prefix": account_namespace(row.instance_id),
        "event_path": f"/v1/custom/{row.instance_id}/events",
        "room_id": settings.get("room_id"),
        "source_ips": settings.get("source_ips", ""),
        "token_set": bool(settings.get("token_hash")),
        "signing_secret_set": bool(settings.get("signing_secret")),
        "stage_token_set": bool(settings.get("stage_token_hash")),
        "public_topic": settings.get("public_topic", ""),
        "max_minutes": settings.get("max_minutes", 30),
        "obs_control": settings.get("obs_control", False),
        "autonomous_window": settings.get("autonomous_window"),
        "autonomous_state": settings.get("autonomous_state"),
        "live_plan": plan_from_config(settings).model_dump(mode="json"),
        "has_plan": bool(settings.get("live_plan")),
        "plan_pending": bool(settings.get("plan_pending")),
        "stage_observation": settings.get("stage_observation"),
        "last_received_at": settings.get("last_received_at"),
    }


async def require_custom(
    session: AsyncSession, instance_id: str, *, lock: bool = False,
) -> ConnectorInstance:
    row = await session.get(
        ConnectorInstance, (CUSTOM_SCOPE, instance_id),
        with_for_update=lock, populate_existing=lock,
    )
    if row is None or row.deleted_at is not None:
        raise not_found("自定义接入器不存在")
    return row


async def configure_custom(
    session: AsyncSession,
    request: Request,
    instance_id: str,
    config: CustomConfig,
    *,
    created_by: str,
    feature: str | None = None,
) -> ConnectorInstance:
    instance_id = normalize_instance_id(instance_id)
    try:
        parse_source_ips(config.source_ips)
    except ValueError as exc:
        raise LingError(ErrorCode.invalid_request, "来源白名单需填写 IP 地址", status_code=422) from exc
    service = ConnectorInstanceService(session)
    # Serialize manual configuration and feature registration for the same key.
    from sqlalchemy import func, select

    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"custom:{instance_id}"))))
    row = await session.get(ConnectorInstance, (CUSTOM_SCOPE, instance_id), with_for_update=True)
    old = (row.custom_config or {}) if row else {}
    if row and feature and old.get("feature") != feature:
        raise LingError(ErrorCode.conflict, "接入器前缀已被其他配置占用", status_code=409)
    if row and (old.get("room_id") != config.room_id):
        raise LingError(ErrorCode.conflict, "房间已绑定，请为新房间创建接入器", status_code=409)
    active = await active_session(session, request.app.state.settings.actor_id, lock=True)
    if active and active.connector_id == instance_id:
        if config.enabled and not (old.get("obs_control") and not config.obs_control):
            raise LingError(ErrorCode.conflict, "请结束当前直播后修改配置", status_code=409)
        request_stop(active)
    try:
        if row is None:
            row = await service.create(
                scope=CUSTOM_SCOPE, instance_id=instance_id, display_name=config.display_name,
                enabled=config.enabled, created_by=created_by,
            )
        elif row.display_name != config.display_name:
            row = await service.rename(
                scope=CUSTOM_SCOPE, instance_id=instance_id, display_name=config.display_name,
            )
    except ConnectorInstanceError as exc:
        code = ErrorCode.conflict if exc.status_code == 409 else ErrorCode.invalid_request
        raise LingError(code, exc.message, status_code=exc.status_code) from exc

    settings = dict(old)
    legacy_fields = {"public_topic", "max_minutes", "autonomous_window"}
    settings.update(config.model_dump(mode="json", exclude={
        "token", "signing_secret", "stage_token", "display_name", "enabled",
        *(legacy_fields - config.model_fields_set),
    }))
    if "live_plan" not in settings and settings.get("autonomous_window") != old.get("autonomous_window"):
        settings.pop("autonomous_state", None)
    if feature:
        settings["feature"] = feature
    for key, value in (("token_hash", config.token), ("stage_token_hash", config.stage_token)):
        if value is not None:
            secret = value.get_secret_value()
            if secret and not 16 <= len(secret) <= 256:
                raise LingError(ErrorCode.invalid_request, "令牌应为 16 到 256 个字符", status_code=422)
            settings[key] = hash_token(secret) if secret else ""
    if config.signing_secret is not None:
        secret = config.signing_secret.get_secret_value()
        settings["signing_secret"] = request.app.state.secret_box.encrypt(secret) if secret else ""
    # Keep the stage credential usable for cleanup while stopping.
    if active and active.connector_id == instance_id:
        settings["stage_token_hash"] = old.get("stage_token_hash", "")
        for key in ("token_hash", "signing_secret", "source_ips"):
            settings[key] = old.get(key, "")
    if settings.get("stage_token_hash") and settings["stage_token_hash"] == settings.get("token_hash"):
        raise LingError(ErrorCode.invalid_request, "上报与舞台请使用不同令牌", status_code=422)
    if not config.enabled or settings.get("stage_token_hash") != old.get("stage_token_hash"):
        settings.pop("stage_observation", None)
    row.custom_config = settings
    row.enabled = config.enabled
    await session.flush()
    return row


def _bearer_matches(request: Request, expected: str) -> bool:
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    return bool(
        expected and scheme.lower() == "bearer"
        and hmac.compare_digest(hash_token(value.strip()), expected)
    )


def require_stage(request: Request, settings: dict) -> None:
    if not _bearer_matches(request, settings.get("stage_token_hash", "")):
        raise LingError(ErrorCode.forbidden, "舞台端令牌校验失败", status_code=403)


def require_ingress(request: Request, config: ConnectorInstance, body: bytes) -> None:
    settings = config.custom_config or {}
    token = settings.get("token_hash", "")
    signature = settings.get("signing_secret", "")
    allowed = source_allowed(
        request.client.host if request.client else None,
        request.headers, has_credential=bool(token or signature),
        allowed_ips=settings.get("source_ips", ""),
    )
    if token:
        allowed = allowed and _bearer_matches(request, token)
    if signature and allowed:
        secret = request.app.state.secret_box.decrypt(signature).encode()
        digest = "sha256=" + hmac.digest(secret, body, "sha256").hex()
        given = request.headers.get("X-Ling-Signature", "").encode()
        allowed = hmac.compare_digest(given, digest.encode())
    if not allowed:
        raise LingError(ErrorCode.forbidden, "接入器来源或凭据校验失败", status_code=403)
