"""Read AstrBot migration files and add eligible records to the personal library."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import islice
from typing import Annotated, BinaryIO, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import Column, MetaData, String, Table, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateTable
from starlette.concurrency import run_in_threadpool

from ling.contracts.v1 import Origin, Visibility
from ling.infra.models import MemoryRow

PREVIEW_LIMIT = 12
RECORD_BATCH_SIZE = 128
# Preserve every record accepted by the former 8 MiB archive limit.
MAX_RECORD_BYTES = 8 * 1024 * 1024
TOKEN_TTL = timedelta(minutes=15)
SOURCE = "astrbot_memory_companion"
_PREVIEW_IDS = Table(
    "ling_memory_import_preview_ids",
    MetaData(),
    Column("id", String(64), primary_key=True),
    schema="pg_temp",
    prefixes=["TEMPORARY"],
    postgresql_on_commit="DROP",
)
_SUMMARY_TYPES = {"conversation_summary", "daily_digest", "memory_decay_summary"}
_STATEMENT_TYPES = {"manual_memory", "explicit_memory", "user_profile", "user_preference"}
_QQ_PLATFORMS = {"aiocqhttp", "onebot", "qq"}
_PRIVATE_SESSIONS = {"friendmessage", "privatemessage", "friend", "private"}
QQ = Annotated[str, Field(pattern=r"^[1-9][0-9]{4,19}$")]
Reference = Annotated[str, Field(min_length=1, max_length=200)]
Label = Annotated[str, Field(max_length=80)]
SkipReason = Literal[
    "invalid_record", "outside_private_pair", "unsupported_memory", "inactive",
    "not_reviewed", "restricted", "not_yet_valid", "expired", "retention_expired",
]


class ArchiveError(ValueError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class ArchiveHeader(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["ling-astrbot-memory-jsonl"]
    version: Literal[1]
    bot_qq: QQ
    owner_qq: QQ


class _AstrBotNativeHeader(BaseModel):
    record_type: Literal["header"]
    format: Literal["astrbot-memory-jsonl"]
    version: Literal[1]


class ArchiveRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Reference
    memory_type: Label
    subject_kind: Label
    subject_id: Annotated[str, Field(max_length=160)]
    object_kind: Label
    object_id: Annotated[str, Field(max_length=160)]
    scope: Label
    session_id: Reference
    platform: Label
    group_id: Annotated[str, Field(max_length=160)]
    owner_bot_id: Annotated[str, Field(max_length=160)]
    visibility: Label
    sayability: Label
    reality_level: Label
    lifecycle: Label
    sensitivity: Label
    review_status: Label
    validity_status: Label
    content: Annotated[str, Field(min_length=1, max_length=4_000)]
    confidence: Annotated[float, Field(ge=0, le=1)]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    occurred_at: AwareDatetime | None
    valid_from: AwareDatetime | None
    valid_to: AwareDatetime | None
    source_event_ids: Annotated[list[Reference], Field(max_length=1_000)]
    source_message_ids: Annotated[list[Reference], Field(max_length=1_000)]

    @field_validator("content")
    @classmethod
    def _valid_content(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("empty content or NUL")
        return value

    @property
    def is_summary(self) -> bool:
        return self.memory_type in _SUMMARY_TYPES

    def skip_reason(
        self, header: ArchiveHeader, *, reviewed_at: datetime, now: datetime, retention_cutoff: datetime,
    ) -> SkipReason | None:
        instance, _, session = self.session_id.partition(":")
        session_kind, _, qq = session.partition(":")
        if not (
            self.platform in _QQ_PLATFORMS
            and instance
            and session_kind.lower() in _PRIVATE_SESSIONS
            and qq == header.owner_qq
            and self.scope == "private"
            and not self.group_id
            and self.visibility == "private_pair"
            and self.subject_kind == "user"
            and self.subject_id == header.owner_qq
            and (self.object_kind, self.object_id) in {
                ("bot", header.bot_qq), ("user", header.owner_qq),
            }
            and self.owner_bot_id == header.bot_qq
        ):
            return "outside_private_pair"
        if self.validity_status != "active":
            return "inactive"
        if self.review_status not in {"auto", "approved"}:
            return "not_reviewed"
        if (
            self.sensitivity not in {"private", "internal", "public"}
            or self.sayability not in {"direct", "indirect"}
        ):
            return "restricted"
        if self.lifecycle != "stable_memory" or not (
            (self.is_summary and self.reality_level in {"llm_summary", "imported_summary"})
            or (self.memory_type in _STATEMENT_TYPES and self.reality_level == "real_user_fact")
        ):
            return "unsupported_memory"
        if self.created_at > reviewed_at or (
            self.valid_from and self.valid_to and self.valid_from >= self.valid_to
        ):
            return "invalid_record"
        if self.valid_from and self.valid_from > reviewed_at:
            return "not_yet_valid"
        if self.valid_to and self.valid_to <= now:
            return "expired"
        if self.created_at <= retention_cutoff:
            return "retention_expired"
        return None


@dataclass(frozen=True)
class Archive:
    header: ArchiveHeader
    records: Iterator[ArchiveRecord | None]
    digest: str


def _decode(line: bytes) -> str:
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArchiveError("invalid_encoding") from exc


def _records(file: BinaryIO) -> Iterator[ArchiveRecord | None]:
    while line := file.readline(MAX_RECORD_BYTES + 1):
        if len(line) > MAX_RECORD_BYTES:
            while line and not line.endswith(b"\n"):
                line = file.readline(65_536)
            yield None
            continue
        if not line.strip():
            continue
        record = _decode(line)
        try:
            yield ArchiveRecord.model_validate_json(record, strict=True)
        except ValidationError:
            yield None


def parse_archive(file: BinaryIO, *, digest: str) -> Archive:
    line = file.readline(MAX_RECORD_BYTES + 1)
    if len(line) > MAX_RECORD_BYTES or line.startswith(b"SQLite format 3\x00"):
        raise ArchiveError("unsupported_archive")
    header_line = _decode(line.removeprefix(b"\xef\xbb\xbf"))
    try:
        header = ArchiveHeader.model_validate_json(header_line, strict=True)
    except ValidationError as exc:
        try:
            _AstrBotNativeHeader.model_validate_json(header_line, strict=True)
        except ValidationError:
            raise ArchiveError("unsupported_archive") from exc
        raise ArchiveError("astrbot_native_archive") from exc
    if header.bot_qq == header.owner_qq:
        raise ArchiveError("invalid_source_pair")
    return Archive(header, _records(file), digest)


class Destination(BaseModel):
    subject_id: str
    display_name: str
    qq: str
    bot_qq: str


@dataclass(frozen=True)
class ImportTarget:
    destination: Destination
    binding_id: str
    binding_revision: str
    forget_generation: int


def sign_confirmation(
    root_key: bytes, *, user_id: str, target: ImportTarget, digest: str,
    reviewed_at: int, expires: int, retention_days: int,
) -> str:
    payload = json.dumps([
        "ling:astrbot-memory-import:v2", user_id, target.destination.subject_id,
        target.binding_id, target.binding_revision, target.forget_generation,
        digest, reviewed_at, expires, retention_days,
    ], separators=(",", ":")).encode()
    signature = hmac.digest(root_key, payload, "sha256").hex()
    return f"{reviewed_at}.{expires}.{signature}"


def check_confirmation(
    root_key: bytes,
    *,
    token: str,
    user_id: str,
    target: ImportTarget,
    digest: str,
    now: datetime,
    retention_days: int,
) -> datetime:
    reviewed_at, _, remainder = token.partition(".")
    expires, _, _ = remainder.partition(".")
    if (
        len(token) > 90 or not token.isascii()
        or not reviewed_at.isdecimal() or len(reviewed_at) > 12
        or not expires.isdecimal() or len(expires) > 12
        or int(expires) <= now.timestamp()
    ):
        raise ArchiveError("preview_invalid")
    expected = sign_confirmation(
        root_key, user_id=user_id, target=target, digest=digest,
        reviewed_at=int(reviewed_at), expires=int(expires), retention_days=retention_days,
    )
    if not hmac.compare_digest(token, expected):
        raise ArchiveError("preview_invalid")
    return datetime.fromtimestamp(int(reviewed_at), UTC)


class ImportCounts(BaseModel):
    total: int
    duplicates: int
    skipped: int
    skip_reasons: dict[SkipReason, int]


class ImportSample(BaseModel):
    source_id: str
    kind: Literal["episode", "assertion"]
    text: str
    occurred_at: datetime | None
    created_at: datetime
    valid_until: datetime | None


@dataclass(frozen=True)
class ImportResult:
    counts: ImportCounts
    accepted: int
    sample: list[ImportSample]


def _memory_id(subject_id: str, header: ArchiveHeader, source_id: str) -> str:
    key = json.dumps([subject_id, header.bot_qq, header.owner_qq, source_id], separators=(",", ":"))
    return "mem_as_" + hashlib.sha256(key.encode()).hexdigest()[:56]


async def process_archive(
    session: AsyncSession,
    *,
    archive: Archive,
    target: ImportTarget,
    now: datetime,
    retention_days: int,
    reviewed_at: datetime | None = None,
    write: bool = False,
) -> ImportResult:
    cutoff = now - timedelta(days=retention_days)
    review_time = reviewed_at or now
    skip_reasons: Counter[SkipReason] = Counter()
    sample: list[ImportSample] = []
    total = duplicates = accepted = 0
    if not write:
        await session.execute(CreateTable(_PREVIEW_IDS))
    while batch := await run_in_threadpool(list, islice(archive.records, RECORD_BATCH_SIZE)):
        total += len(batch)
        candidates: dict[str, ArchiveRecord] = {}
        for record in batch:
            reason = "invalid_record" if record is None else record.skip_reason(
                archive.header, now=now, reviewed_at=review_time, retention_cutoff=cutoff,
            )
            if reason is not None:
                skip_reasons[reason] += 1
                continue
            assert record is not None
            memory_id = _memory_id(target.destination.subject_id, archive.header, record.id)
            if memory_id in candidates:
                duplicates += 1
                continue
            candidates[memory_id] = record

        if not candidates:
            continue
        if write:
            inserted = await _insert_memories(
                session, header=archive.header, target=target, candidates=candidates,
                retention_days=retention_days,
            )
            accepted += inserted
            duplicates += len(candidates) - inserted
            continue

        # The transaction's temporary index deduplicates across batches without retaining all IDs in the API process.
        fresh = set(await session.scalars(
            insert(_PREVIEW_IDS).on_conflict_do_nothing().returning(_PREVIEW_IDS.c.id),
            [{"id": memory_id} for memory_id in candidates],
        ))
        duplicates += len(candidates) - len(fresh)
        if not fresh:
            continue
        existing = set(await session.scalars(
            select(MemoryRow.id).where(MemoryRow.id.in_(fresh))
        ))
        duplicates += len(existing)
        accepted += len(fresh) - len(existing)
        if len(sample) < PREVIEW_LIMIT:
            for memory_id, record in candidates.items():
                if memory_id in fresh and memory_id not in existing:
                    sample.append(_sample(record))
                    if len(sample) == PREVIEW_LIMIT:
                        break
    return ImportResult(
        counts=ImportCounts(
            total=total,
            duplicates=duplicates,
            skipped=sum(skip_reasons.values()),
            skip_reasons=dict(skip_reasons),
        ),
        accepted=accepted,
        sample=sample,
    )


def _sample(record: ArchiveRecord) -> ImportSample:
    return ImportSample(
        source_id=record.id,
        kind="episode" if record.is_summary else "assertion",
        text=record.content[:400],
        occurred_at=record.occurred_at,
        created_at=record.created_at,
        valid_until=record.valid_to,
    )


async def _insert_memories(
    session: AsyncSession,
    *,
    header: ArchiveHeader,
    target: ImportTarget,
    candidates: dict[str, ArchiveRecord],
    retention_days: int,
) -> int:
    rows = []
    ref = f"astrbot:{header.bot_qq}:{header.owner_qq}"
    for memory_id, record in candidates.items():
        rows.append({
            "id": memory_id,
            "subject_id": target.destination.subject_id,
            "kind": "episode" if record.is_summary else "assertion",
            "text": record.content,
            "origin": Origin.model_inference.value if record.is_summary else Origin.user_statement.value,
            "visibility": Visibility.subject_private.value,
            "source_visibility": Visibility.subject_private.value,
            "confidence": record.confidence,
            "source_event_ids": [
                f"{ref}:memory:{record.id}",
                *(f"{ref}:event:{item}" for item in record.source_event_ids),
                *(f"{ref}:message:{item}" for item in record.source_message_ids),
            ],
            "valid_from": record.valid_from,
            "valid_until": record.valid_to,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "retention_days": retention_days,
            "import_source": {
                "source": SOURCE,
                "record_id": record.id,
                "memory_type": record.memory_type,
                "platform": record.platform,
                "session_id": record.session_id,
                "bot_qq": header.bot_qq,
                "owner_qq": header.owner_qq,
                "occurred_at": record.occurred_at.isoformat() if record.occurred_at else None,
                "created_at": record.created_at.isoformat(),
                "updated_at": record.updated_at.isoformat(),
                "valid_from": record.valid_from.isoformat() if record.valid_from else None,
                "valid_to": record.valid_to.isoformat() if record.valid_to else None,
            },
        })
    stmt = insert(MemoryRow).on_conflict_do_nothing(index_elements=[MemoryRow.id]).returning(MemoryRow.id)
    inserted = await session.scalars(stmt, rows)
    return len(inserted.all())
