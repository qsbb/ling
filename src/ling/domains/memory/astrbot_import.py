"""Read AstrBot migration files and add eligible records to the personal library."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ling.contracts.v1 import Origin, Visibility
from ling.infra.models import MemoryRow

MAX_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 5_000
PREVIEW_LIMIT = 12
TOKEN_TTL = timedelta(minutes=15)
SOURCE = "astrbot_memory_companion"
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
    records: list[ArchiveRecord | None]
    digest: str


def parse_archive(data: bytes) -> Archive:
    if len(data) > MAX_BYTES:
        raise ArchiveError("file_too_large")
    if data.startswith(b"SQLite format 3\x00"):
        raise ArchiveError("unsupported_archive")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArchiveError("invalid_encoding") from exc

    lines = BytesIO(data)
    try:
        header = ArchiveHeader.model_validate_json(lines.readline().removeprefix(b"\xef\xbb\xbf"), strict=True)
    except ValidationError as exc:
        raise ArchiveError("unsupported_archive") from exc
    if header.bot_qq == header.owner_qq:
        raise ArchiveError("invalid_source_pair")
    records: list[ArchiveRecord | None] = []
    for line in lines:
        if not line.strip():
            continue
        if len(records) >= MAX_RECORDS:
            raise ArchiveError("too_many_records")
        try:
            records.append(ArchiveRecord.model_validate_json(line, strict=True))
        except ValidationError:
            records.append(None)
    return Archive(header, records, hashlib.sha256(data).hexdigest())


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
    root_key: bytes, *, user_id: str, target: ImportTarget, digest: str, expires: int, retention_days: int,
) -> str:
    payload = json.dumps([
        "ling:astrbot-memory-import:v1", user_id, target.destination.subject_id,
        target.binding_id, target.binding_revision, target.forget_generation, digest, expires, retention_days,
    ], separators=(",", ":")).encode()
    signature = hmac.digest(root_key, payload, "sha256").hex()
    return f"{expires}.{signature}"


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
    expires, _, _ = token.partition(".")
    if (
        len(token) > 80 or not token.isascii()
        or not expires.isdecimal() or len(expires) > 12
        or int(expires) <= now.timestamp()
    ):
        raise ArchiveError("preview_invalid")
    expected = sign_confirmation(
        root_key, user_id=user_id, target=target, digest=digest, expires=int(expires), retention_days=retention_days,
    )
    if not hmac.compare_digest(token, expected):
        raise ArchiveError("preview_invalid")
    return datetime.fromtimestamp(int(expires), UTC) - TOKEN_TTL


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


@dataclass
class ImportPlan:
    counts: ImportCounts
    candidates: list[tuple[str, ArchiveRecord]]


def _memory_id(subject_id: str, header: ArchiveHeader, source_id: str) -> str:
    key = json.dumps([subject_id, header.bot_qq, header.owner_qq, source_id], separators=(",", ":"))
    return "mem_as_" + hashlib.sha256(key.encode()).hexdigest()[:56]


async def plan_import(
    session: AsyncSession,
    *,
    archive: Archive,
    target: ImportTarget,
    now: datetime,
    retention_days: int,
    reviewed_at: datetime | None = None,
) -> ImportPlan:
    cutoff = now - timedelta(days=retention_days)
    skipped: Counter[SkipReason] = Counter()
    candidates: dict[str, ArchiveRecord] = {}
    duplicates = 0
    for record in archive.records:
        if record is None:
            skipped["invalid_record"] += 1
            continue
        reason = record.skip_reason(
            archive.header, now=now, reviewed_at=reviewed_at or now, retention_cutoff=cutoff,
        )
        if reason:
            skipped[reason] += 1
            continue
        memory_id = _memory_id(target.destination.subject_id, archive.header, record.id)
        if memory_id in candidates:
            duplicates += 1
            continue
        candidates[memory_id] = record

    existing = set(await session.scalars(
        select(MemoryRow.id).where(MemoryRow.id.in_(list(candidates)))
    )) if candidates else set()
    return ImportPlan(
        counts=ImportCounts(
            total=len(archive.records),
            duplicates=duplicates + len(existing),
            skipped=sum(skipped.values()),
            skip_reasons=dict(skipped),
        ),
        candidates=[(memory_id, record) for memory_id, record in candidates.items() if memory_id not in existing],
    )


def sample_records(plan: ImportPlan) -> list[ImportSample]:
    return [
        ImportSample(
            source_id=record.id,
            kind="episode" if record.is_summary else "assertion",
            text=record.content[:400],
            occurred_at=record.occurred_at,
            created_at=record.created_at,
            valid_until=record.valid_to,
        )
        for _, record in plan.candidates[:PREVIEW_LIMIT]
    ]


async def insert_memories(
    session: AsyncSession,
    *,
    archive: Archive,
    target: ImportTarget,
    plan: ImportPlan,
    retention_days: int,
) -> int:
    rows = []
    header = archive.header
    for memory_id, record in plan.candidates:
        ref = f"astrbot:{header.bot_qq}:{header.owner_qq}"
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
    if not rows:
        return 0
    stmt = insert(MemoryRow).on_conflict_do_nothing(index_elements=[MemoryRow.id]).returning(MemoryRow.id)
    inserted = await session.scalars(stmt, rows)
    return len(inserted.all())
