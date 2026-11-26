"""Preview and confirm a personal AstrBot memory import."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from tempfile import TemporaryFile
from typing import Annotated, BinaryIO

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import DBAPIError
from starlette.concurrency import run_in_threadpool

from ling.api.deps import AuthContext, ConfigDep, DbSession
from ling.api.errors import LingError
from ling.api.guards import OwnerWriteGuard
from ling.audit import audit_event
from ling.capabilities.channels import onebot_config
from ling.config import Settings
from ling.contracts.v1 import ErrorCode
from ling.domains.identity.service import IdentityService
from ling.domains.memory import astrbot_import as importer
from ling.domains.memory.purge import has_open_purge
from ling.domains.memory.service import MemoryService
from ling.infra.models import utcnow
from ling.infra.security import load_signing_root_key
from ling.settings.connector_fields import SCOPE_ONEBOT
from ling.settings.connector_instances import DEFAULT_INSTANCE_ID, ConnectorInstanceService
from ling.settings.connector_overrides import resolve_field_values

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/memory/import", tags=["memory"])
UPLOAD_PATHS = ("/v1/memory/import/preview", "/v1/memory/import/commit")
CONTENT_TYPE = "application/x-ndjson"
OwnerImport = Annotated[AuthContext, Depends(OwnerWriteGuard(content_type=CONTENT_TYPE))]

_FILE_BODY = {
    "requestBody": {"required": True, "content": {CONTENT_TYPE: {
        "schema": {"type": "string", "format": "binary"},
    }}},
}
_ERRORS = {
    "invalid_encoding": (422, ErrorCode.invalid_request, "导入文件需要使用 UTF-8 编码"),
    "unsupported_archive": (422, ErrorCode.invalid_request, "请选择 AstrBot 导出或 Ling 迁移的 JSONL 文件"),
    "qq_binding_required": (
        409, ErrorCode.conflict, "请先确认此 QQ 已绑定到你在 Ling 的自然人",
    ),
    "astrbot_source_unmatched": (
        422, ErrorCode.invalid_request, "没有找到与你已绑定 QQ 对应的私聊记忆来源",
    ),
    "invalid_source_pair": (
        422, ErrorCode.invalid_request, "请填写两个不同的 QQ 号，分别对应你本人和旧 Bot",
    ),
    "memory_purge_in_progress": (409, ErrorCode.conflict, "记忆正在清理，请稍后重新预览"),
    "preview_invalid": (409, ErrorCode.conflict, "预览已失效，请重新预览"),
}


class PreviewOut(importer.ImportCounts):
    destination: importer.Destination
    accepted: int
    sample: list[importer.ImportSample]
    confirmation_token: str | None
    expires_at: datetime | None


class SourceChoicesOut(BaseModel):
    sources: list[importer.SourcePair]


class CommitOut(importer.ImportCounts):
    destination: importer.Destination
    imported: int


def _public_error(exc: importer.ArchiveError) -> LingError:
    status, code, message = _ERRORS[exc.detail]
    return LingError(code, message, status_code=status, detail=exc.detail)


@asynccontextmanager
async def _uploaded_archive(
    request: Request, settings: Settings,
) -> AsyncIterator[tuple[BinaryIO, str, importer.ArchiveHeader | importer.AstrBotNativeHeader]]:
    # Use the data volume: /tmp may be a memory-backed filesystem in Compose.
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with TemporaryFile(dir=settings.data_dir) as file:
        async for chunk in request.stream():
            if chunk:
                await run_in_threadpool(file.write, chunk)
                digest.update(chunk)
        await run_in_threadpool(file.seek, 0)
        header = await run_in_threadpool(importer.read_archive_header, file)
        yield file, digest.hexdigest(), header


def _requested_source(request: Request) -> importer.SourcePair | None:
    owner_qq = request.headers.get("X-Ling-Import-Owner-QQ")
    bot_qq = request.headers.get("X-Ling-Import-Bot-QQ")
    if owner_qq is None and bot_qq is None:
        return None
    try:
        return importer.SourcePair.model_validate({"owner_qq": owner_qq, "bot_qq": bot_qq})
    except ValidationError as exc:
        raise importer.ArchiveError("invalid_source_pair") from exc


async def _onebot_namespaces(session: DbSession, settings: Settings) -> set[str]:
    instances = await ConnectorInstanceService(session).list_instances(scope=SCOPE_ONEBOT)
    ids = {DEFAULT_INSTANCE_ID, *(instance.instance_id for instance in instances)}
    baseline = onebot_config()
    values = set()
    for instance_id in ids:
        resolved = await resolve_field_values(
            session, scope=SCOPE_ONEBOT, instance_id=instance_id, settings=settings, onebot=baseline,
        )
        values.add(str(resolved.get("LING_ONEBOT_INSTANCE_ID")))
    return values


async def _resolve_target(
    session: DbSession, auth: AuthContext, source: importer.SourcePair, onebot_namespaces: set[str],
    settings: Settings, *, lock: bool = False,
) -> importer.ImportTarget:
    subject, binding = await IdentityService(session).resolve_proven_login_binding(
        subject_id=auth.user.subject_id,
        connector_instances=onebot_namespaces,
        external_account_id=source.owner_qq,
        lock=lock,
    )
    if await has_open_purge(session, subject_id=subject.id):
        raise importer.ArchiveError("memory_purge_in_progress")
    return importer.ImportTarget(
        destination=importer.Destination(
            subject_id=subject.id, display_name=subject.display_name,
            qq=source.owner_qq, bot_qq=source.bot_qq,
        ),
        binding_id=binding.id,
        binding_revision=binding.updated_at.isoformat(),
        forget_generation=subject.forget_generation,
        actor_id=settings.actor_id,
        persona_id=settings.persona_id,
        instance_id=sorted(onebot_namespaces)[0] if onebot_namespaces else DEFAULT_INSTANCE_ID,
    )


@router.post("/preview", response_model=PreviewOut | SourceChoicesOut, openapi_extra=_FILE_BODY)
async def preview(
    request: Request, response: Response, auth: OwnerImport, session: DbSession, config: ConfigDep,
) -> PreviewOut | SourceChoicesOut:
    settings: Settings = request.app.state.settings
    response.headers["Cache-Control"] = "no-store"
    try:
        async with _uploaded_archive(request, settings) as (file, digest, header):
            namespaces = await _onebot_namespaces(session, settings)
            source = None
            if isinstance(header, importer.AstrBotNativeHeader):
                source = _requested_source(request)
                if source is None:
                    owners = await IdentityService(session).proven_login_account_ids(
                        auth.user.subject_id, namespaces,
                    )
                    if not owners:
                        raise importer.ArchiveError("qq_binding_required")
                    choices = await run_in_threadpool(importer.find_native_sources, file, owners)
                    if not choices:
                        raise importer.ArchiveError("astrbot_source_unmatched")
                    if len(choices) > 1:
                        return SourceChoicesOut(sources=choices)
                    source = choices[0]
            archive = importer.open_archive(file, digest=digest, header=header, source=source)
            target = await _resolve_target(session, auth, archive.source, namespaces, settings)
            reviewed_at = utcnow().replace(microsecond=0)
            retention = MemoryService(session, policy=(await config.effective()).memory).write_retention_days()
            result = await importer.process_archive(
                session, archive=archive, target=target, now=reviewed_at, retention_days=retention,
            )
    except importer.ArchiveError as exc:
        raise _public_error(exc) from None

    expires = utcnow().replace(microsecond=0) + importer.TOKEN_TTL
    return PreviewOut(
        **result.counts.model_dump(),
        destination=target.destination,
        accepted=result.accepted,
        sample=result.sample,
        confirmation_token=importer.sign_confirmation(
            load_signing_root_key(settings),
            user_id=auth.user.id, target=target, digest=archive.digest,
            reviewed_at=int(reviewed_at.timestamp()), expires=int(expires.timestamp()), retention_days=retention,
        ) if result.accepted else None,
        expires_at=expires if result.accepted else None,
    )


@router.post("/commit", response_model=CommitOut, openapi_extra=_FILE_BODY)
async def commit(
    request: Request, response: Response, auth: OwnerImport, session: DbSession, config: ConfigDep,
    token: Annotated[str, Header(alias="X-Ling-Import-Token")] = "",
) -> CommitOut:
    settings: Settings = request.app.state.settings
    submitted_at = utcnow()
    try:
        if not token:
            raise importer.ArchiveError("preview_invalid")
        async with _uploaded_archive(request, settings) as (file, digest, header):
            source = _requested_source(request) if isinstance(header, importer.AstrBotNativeHeader) else None
            archive = importer.open_archive(file, digest=digest, header=header, source=source)
            target = await _resolve_target(
                session, auth, archive.source, await _onebot_namespaces(session, settings),
                settings, lock=True,
            )
            retention = MemoryService(session, policy=(await config.effective()).memory).write_retention_days()
            reviewed_at = importer.check_confirmation(
                load_signing_root_key(settings),
                token=token, user_id=auth.user.id, target=target, digest=archive.digest,
                now=submitted_at, retention_days=retention,
            )
            result = await importer.process_archive(
                session, archive=archive, target=target, now=utcnow(), retention_days=retention,
                reviewed_at=reviewed_at, write=True,
            )
        await session.commit()
    except importer.ArchiveError as exc:
        raise _public_error(exc) from None
    except DBAPIError as exc:
        logger.error("写入迁移记忆失败（%s）", type(exc).__name__)
        raise LingError(
            ErrorCode.internal, "导入未完成，可以重试", status_code=500, retriable=True,
        ) from None

    if result.accepted:
        audit_event(
            "memory_imported", actor=auth.user.id, target_type="subject",
            target_id=target.destination.subject_id,
            attributes={
                "imported": result.accepted,
                "duplicates": result.counts.duplicates,
                "skipped": result.counts.skipped,
            },
        )
    response.headers["Cache-Control"] = "no-store"
    return CommitOut(
        **result.counts.model_dump(),
        destination=target.destination, imported=result.accepted,
    )
