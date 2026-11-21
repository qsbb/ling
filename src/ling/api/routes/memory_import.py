"""Preview and confirm a personal AstrBot memory import."""

from __future__ import annotations

import logging
from datetime import datetime
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
CONTENT_TYPE = "application/x-ndjson"
OwnerImport = Annotated[AuthContext, Depends(OwnerWriteGuard(content_type=CONTENT_TYPE))]

_FILE_BODY = {
    "requestBody": {"required": True, "content": {CONTENT_TYPE: {
        "schema": {"type": "string", "format": "binary"},
    }}},
}
_ERRORS = {
    "file_too_large": (413, ErrorCode.invalid_request, "导入文件不能超过 8 MiB"),
    "too_many_records": (422, ErrorCode.invalid_request, "导入文件最多包含 5000 条记录"),
    "invalid_encoding": (422, ErrorCode.invalid_request, "导入文件需要使用 UTF-8 编码"),
    "unsupported_archive": (422, ErrorCode.invalid_request, "请选择 Ling 转换脚本生成的迁移文件"),
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


async def _read_archive(request: Request) -> importer.Archive:
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > importer.MAX_BYTES:
            raise importer.ArchiveError("file_too_large")
        content.extend(chunk)
    return await run_in_threadpool(importer.parse_archive, bytes(content))


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
    try:
        archive = await _read_archive(request)
        settings: Settings = request.app.state.settings
        target = await _resolve_target(session, settings, auth, archive.header)
        now = utcnow().replace(microsecond=0)
        retention = MemoryService(session, policy=(await config.effective()).memory).write_retention_days()
        plan = await importer.plan_import(
            session, archive=archive, target=target, now=now, retention_days=retention,
        )
    except importer.ArchiveError as exc:
        raise _public_error(exc) from None

    expires = now + importer.TOKEN_TTL
    response.headers["Cache-Control"] = "no-store"
    return PreviewOut(
        **plan.counts.model_dump(),
        destination=target.destination,
        accepted=len(plan.candidates),
        sample=importer.sample_records(plan),
        confirmation_token=importer.sign_confirmation(
            load_signing_root_key(settings),
            user_id=auth.user.id, target=target, digest=archive.digest, expires=int(expires.timestamp()),
            retention_days=retention,
        ) if plan.candidates else None,
        expires_at=expires if plan.candidates else None,
    )


@router.post("/commit", response_model=CommitOut, openapi_extra=_FILE_BODY)
async def commit(
    request: Request, response: Response, auth: OwnerImport, session: DbSession, config: ConfigDep,
    token: Annotated[str, Header(alias="X-Ling-Import-Token")] = "",
) -> CommitOut:
    try:
        if not token:
            raise importer.ArchiveError("preview_invalid")
        archive = await _read_archive(request)
        settings: Settings = request.app.state.settings
        target = await _resolve_target(session, settings, auth, archive.header, lock=True)
        now = utcnow()
        retention = MemoryService(session, policy=(await config.effective()).memory).write_retention_days()
        reviewed_at = importer.check_confirmation(
            load_signing_root_key(settings),
            token=token, user_id=auth.user.id, target=target, digest=archive.digest, now=now,
            retention_days=retention,
        )
        plan = await importer.plan_import(
            session, archive=archive, target=target, now=now, retention_days=retention,
            reviewed_at=reviewed_at,
        )
        inserted = await importer.insert_memories(
            session, archive=archive, target=target, plan=plan, retention_days=retention,
        )
        await session.commit()
    except importer.ArchiveError as exc:
        raise _public_error(exc) from None
    except DBAPIError as exc:
        logger.error("写入迁移记忆失败（%s）", type(exc).__name__)
        raise LingError(
            ErrorCode.internal, "导入未完成，可以重试", status_code=500, retriable=True,
        ) from None

    duplicates = plan.counts.duplicates + len(plan.candidates) - inserted
    if inserted:
        audit_event(
            "memory_imported", actor=auth.user.id, target_type="subject",
            target_id=target.destination.subject_id,
            attributes={"imported": inserted, "duplicates": duplicates, "skipped": plan.counts.skipped},
        )
    response.headers["Cache-Control"] = "no-store"
    return CommitOut(
        **{**plan.counts.model_dump(), "duplicates": duplicates},
        destination=target.destination, imported=inserted,
    )
