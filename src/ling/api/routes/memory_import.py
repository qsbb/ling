"""Preview and confirm a personal AstrBot memory import."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from tempfile import TemporaryFile
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response
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
    "unsupported_archive": (422, ErrorCode.invalid_request, "请选择 Ling 转换脚本生成的迁移文件"),
    "astrbot_native_archive": (
        422, ErrorCode.invalid_request, "这是 AstrBot 原生导出，请用插件的 SQLite 备份生成 Ling 迁移文件",
    ),
    "invalid_source_pair": (422, ErrorCode.invalid_request, "旧 Bot QQ 和你的 QQ 必须不同"),
    "memory_purge_in_progress": (409, ErrorCode.conflict, "记忆正在清理，请稍后重新预览"),
    "preview_invalid": (409, ErrorCode.conflict, "预览已失效，请重新预览"),
}


class PreviewOut(importer.ImportCounts):
    destination: importer.Destination
    accepted: int
    sample: list[importer.ImportSample]
    confirmation_token: str | None
    expires_at: datetime | None


class CommitOut(importer.ImportCounts):
    destination: importer.Destination
    imported: int


def _public_error(exc: importer.ArchiveError) -> LingError:
    status, code, message = _ERRORS[exc.detail]
    return LingError(code, message, status_code=status, detail=exc.detail)


@asynccontextmanager
async def _uploaded_archive(request: Request, settings: Settings) -> AsyncIterator[importer.Archive]:
    # Use the data volume: /tmp may be a memory-backed filesystem in Compose.
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with TemporaryFile(dir=settings.data_dir) as file:
        async for chunk in request.stream():
            if chunk:
                await run_in_threadpool(file.write, chunk)
                digest.update(chunk)
        await run_in_threadpool(file.seek, 0)
        yield await run_in_threadpool(importer.parse_archive, file, digest=digest.hexdigest())


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
    session: DbSession, settings: Settings, auth: AuthContext, header: importer.ArchiveHeader,
    *, lock: bool = False,
) -> importer.ImportTarget:
    subject, binding = await IdentityService(session).resolve_proven_login_binding(
        subject_id=auth.user.subject_id,
        connector_instances=await _onebot_namespaces(session, settings),
        external_account_id=header.owner_qq,
        lock=lock,
    )
    if await has_open_purge(session, subject_id=subject.id):
        raise importer.ArchiveError("memory_purge_in_progress")
    return importer.ImportTarget(
        destination=importer.Destination(
            subject_id=subject.id, display_name=subject.display_name,
            qq=header.owner_qq, bot_qq=header.bot_qq,
        ),
        binding_id=binding.id,
        binding_revision=binding.updated_at.isoformat(),
        forget_generation=subject.forget_generation,
    )


@router.post("/preview", response_model=PreviewOut, openapi_extra=_FILE_BODY)
async def preview(
    request: Request, response: Response, auth: OwnerImport, session: DbSession, config: ConfigDep,
) -> PreviewOut:
    settings: Settings = request.app.state.settings
    try:
        async with _uploaded_archive(request, settings) as archive:
            target = await _resolve_target(session, settings, auth, archive.header)
            reviewed_at = utcnow().replace(microsecond=0)
            retention = MemoryService(session, policy=(await config.effective()).memory).write_retention_days()
            result = await importer.process_archive(
                session, archive=archive, target=target, now=reviewed_at, retention_days=retention,
            )
    except importer.ArchiveError as exc:
        raise _public_error(exc) from None

    expires = utcnow().replace(microsecond=0) + importer.TOKEN_TTL
    response.headers["Cache-Control"] = "no-store"
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
        async with _uploaded_archive(request, settings) as archive:
            target = await _resolve_target(session, settings, auth, archive.header, lock=True)
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
