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
from ling.infra.models import MemoryRow, Window

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
_GROUP_SESSIONS = {"groupmessage", "group", "groupchat"}
_ASTRBOT_NON_MEMORY_TYPES = ("identity", "relationship", "timeline", "acl_rule", "acl_policy")
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


class SourcePair(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bot_qq: QQ
    owner_qq: QQ


class ArchiveHeader(SourcePair):
    format: Literal["ling-astrbot-memory-jsonl"]
    version: Literal[1]


class AstrBotNativeHeader(BaseModel):
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

    def _session(self) -> tuple[str, str, str]:
        instance, _, session = self.session_id.partition(":")
        session_kind, _, peer = session.partition(":")
        return instance, session_kind.lower(), peer

    @property
    def is_group(self) -> bool:
        return self.scope == "group"

    def belongs_to_private(self, source: SourcePair) -> bool:
        instance, session_kind, qq = self._session()
        return (
            self.platform in _QQ_PLATFORMS
            and bool(instance)
            and session_kind in _PRIVATE_SESSIONS
            and qq == source.owner_qq
            and self.scope == "private"
            and not self.group_id
            and self.visibility == "private_pair"
            and self.subject_kind == "user"
            and self.subject_id == source.owner_qq
            and (self.object_kind, self.object_id) in {
                ("bot", source.bot_qq), ("user", source.owner_qq),
            }
            and self.owner_bot_id == source.bot_qq
        )

    def belongs_to_group(self, source: SourcePair) -> bool:
        """群聊历史：Bot 在群里看到/总结的共同经历。

        只接受 Bot 自己作为 subject、群作为 object 的群摘要；群成员个人资料
        不以群聊身份混入。来源窗口稍后由导入器绑定到内部群窗口。
        """
        instance, session_kind, peer = self._session()
        return (
            self.platform in _QQ_PLATFORMS
            and bool(instance)
            and session_kind in _GROUP_SESSIONS
            and peer == self.group_id
            and bool(self.group_id)
            and self.scope == "group"
            and self.visibility in {"group_public", "group_scope"}
            and self.subject_kind == "bot"
            and self.subject_id == source.bot_qq
            and self.object_kind == "group"
            and self.object_id == self.group_id
            and self.owner_bot_id == source.bot_qq
        )

    def belongs_to(self, source: SourcePair) -> bool:
        return self.belongs_to_private(source) or self.belongs_to_group(source)

    def skip_reason(
        self, source: SourcePair, *, reviewed_at: datetime, now: datetime, retention_cutoff: datetime,
    ) -> SkipReason | None:
        if not self.belongs_to(source):
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
    source: SourcePair
    records: Iterator[ArchiveRecord | None]
    digest: str


def _decode(line: bytes) -> str:
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArchiveError("invalid_encoding") from exc


def _reference_ids(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(
        item for item in value if isinstance(item, str) and 0 < len(item) <= 200
    ))


def _native_memory(data: dict) -> ArchiveRecord | None:
    subject, target = data.get("subject"), data.get("object")
    if not isinstance(subject, dict) or not isinstance(target, dict):
        return None
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    record = {key: data[key] for key in ArchiveRecord.model_fields if key in data}
    message_ids = _reference_ids([data.get("message_id")]) + _reference_ids(
        metadata.get("source_message_ids")
    )
    record.update(
        subject_kind=subject.get("kind"),
        subject_id=subject.get("id"),
        object_kind=target.get("kind"),
        object_id=target.get("id"),
        owner_bot_id=data.get("owner_bot_id") or metadata.get("owner_bot_id")
        or (target.get("id") if target.get("kind") == "bot" else ""),
        # Match the plugin's defaults when portable exports omit these fields.
        validity_status=data.get("validity_status") or metadata.get("validity_status")
        or ("archived" if data.get("lifecycle") == "archived" else "active"),
        sensitivity=data.get("sensitivity") or metadata.get("sensitivity") or "private",
        valid_from=data.get("valid_from") or metadata.get("valid_from") or None,
        valid_to=data.get("valid_to") or metadata.get("valid_to") or None,
        occurred_at=data.get("occurred_at") or None,
        source_event_ids=_reference_ids(metadata.get("source_event_ids")),
        source_message_ids=list(dict.fromkeys(message_ids)),
    )
    return ArchiveRecord.model_validate(record)


def _records(file: BinaryIO, *, native: bool) -> Iterator[ArchiveRecord | None]:
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
            if not native:
                yield ArchiveRecord.model_validate_json(record, strict=True)
                continue
            entry = json.loads(record)
            record_type = entry.get("record_type") if isinstance(entry, dict) else None
            if record_type in _ASTRBOT_NON_MEMORY_TYPES:
                continue
            data = entry.get("data") if record_type == "memory" else None
            yield _native_memory(data) if isinstance(data, dict) else None
        except (ValueError, RecursionError):
            yield None


def read_archive_header(file: BinaryIO) -> ArchiveHeader | AstrBotNativeHeader:
    line = file.readline(MAX_RECORD_BYTES + 1)
    if len(line) > MAX_RECORD_BYTES or line.startswith(b"SQLite format 3\x00"):
        raise ArchiveError("unsupported_archive")
    header_line = _decode(line.removeprefix(b"\xef\xbb\xbf"))
    try:
        return ArchiveHeader.model_validate_json(header_line, strict=True)
    except ValidationError:
        try:
            return AstrBotNativeHeader.model_validate_json(header_line, strict=True)
        except ValidationError as exc:
            raise ArchiveError("unsupported_archive") from exc


def find_native_sources(file: BinaryIO, owner_qqs: set[str]) -> list[SourcePair]:
    """Find QQ pairs belonging to the signed-in owner and rewind to the first record."""
    start = file.tell()
    pairs: dict[tuple[str, str], SourcePair] = {}
    try:
        for record in _records(file, native=True):
            if record is None or record.subject_id not in owner_qqs:
                continue
            key = (record.subject_id, record.owner_bot_id)
            if key in pairs or record.subject_id == record.owner_bot_id:
                continue
            try:
                source = SourcePair(owner_qq=record.subject_id, bot_qq=record.owner_bot_id)
            except ValidationError:
                continue
            if record.belongs_to(source):
                pairs[key] = source
    finally:
        file.seek(start)
    return [pairs[key] for key in sorted(pairs)]


def open_archive(
    file: BinaryIO, *, digest: str, header: ArchiveHeader | AstrBotNativeHeader, source: SourcePair | None,
) -> Archive:
    native = isinstance(header, AstrBotNativeHeader)
    if isinstance(header, ArchiveHeader):
        source = header
    if source is None:
        raise ArchiveError("preview_invalid")
    if source.bot_qq == source.owner_qq:
        raise ArchiveError("invalid_source_pair")
    return Archive(source, _records(file, native=native), digest)


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
    actor_id: str = "actor_ling"
    persona_id: str = "persona_ling"
    # 历史群没有现成的平台实例，按导入端默认 OneBot 实例建历史群窗口。
    instance_id: str = "onebot-default"


def sign_confirmation(
    root_key: bytes, *, user_id: str, target: ImportTarget, digest: str,
    reviewed_at: int, expires: int, retention_days: int,
) -> str:
    payload = json.dumps([
        "ling:astrbot-memory-import:v3", user_id, target.destination.subject_id,
        target.destination.qq, target.destination.bot_qq,
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
    scope_counts: dict[str, int] = Field(default_factory=dict)


class ImportSample(BaseModel):
    source_id: str
    kind: Literal["episode", "assertion"]
    text: str
    occurred_at: datetime | None
    created_at: datetime
    valid_until: datetime | None
    scope: Literal["private", "group"] = "private"


@dataclass(frozen=True)
class ImportResult:
    counts: ImportCounts
    accepted: int
    sample: list[ImportSample]


def _memory_id(subject_id: str, source: SourcePair, source_id: str) -> str:
    key = json.dumps([subject_id, source.bot_qq, source.owner_qq, source_id], separators=(",", ":"))
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
    accepted_scopes: Counter[str] = Counter()
    if not write:
        await session.execute(CreateTable(_PREVIEW_IDS))
    while batch := await run_in_threadpool(list, islice(archive.records, RECORD_BATCH_SIZE)):
        total += len(batch)
        candidates: dict[str, ArchiveRecord] = {}
        for record in batch:
            reason = "invalid_record" if record is None else record.skip_reason(
                archive.source, now=now, reviewed_at=review_time, retention_cutoff=cutoff,
            )
            if reason is not None:
                skip_reasons[reason] += 1
                continue
            assert record is not None
            memory_id = _memory_id(target.destination.subject_id, archive.source, record.id)
            if memory_id in candidates:
                duplicates += 1
                continue
            candidates[memory_id] = record

        if not candidates:
            continue
        if write:
            inserted, inserted_scopes = await _insert_memories(
                session, source=archive.source, target=target, candidates=candidates,
                retention_days=retention_days,
            )
            accepted += inserted
            accepted_scopes.update(inserted_scopes)
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
        for memory_id in fresh - existing:
            accepted_scopes[candidates[memory_id].scope] += 1
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
            scope_counts=dict(accepted_scopes),
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
        scope="group" if record.is_group else "private",
    )


async def _ensure_group_window(
    session: AsyncSession,
    *,
    target: ImportTarget,
    record: ArchiveRecord,
) -> str:
    """历史群窗口：以导入端默认实例 + 群号建，来源未知，不冒充已核验。

    这个窗口用于给群记忆一个可追溯的 `source_window_id`。它不会被当成
    已证明的群受众；群回复是否可用仍由现有 audience/window 检查决定。
    """
    ref = f"{target.instance_id}:group:{record.group_id}"
    existing = await session.scalar(
        select(Window).where(
            Window.actor_id == target.actor_id,
            Window.connector_window_ref == ref,
        ).limit(1)
    )
    if existing is not None:
        return existing.id
    window_id = "win_as_" + hashlib.sha256(
        f"{target.actor_id}:{ref}".encode()
    ).hexdigest()[:24]
    cached = await session.get(Window, window_id)
    if cached is not None:
        return cached.id
    window = Window(
        id=window_id,
        actor_id=target.actor_id,
        persona_id=target.persona_id,
        subject_id=target.destination.subject_id,
        title=ref[:128],
        channel="onebot",
        visibility="group_scope",
        connector_window_ref=ref,
        origin_status="unknown",
        message_count=0,
    )
    session.add(window)
    await session.flush()
    return window.id


async def _insert_memories(
    session: AsyncSession,
    *,
    source: SourcePair,
    target: ImportTarget,
    candidates: dict[str, ArchiveRecord],
    retention_days: int,
) -> tuple[int, Counter[str]]:
    rows = []
    ref = f"astrbot:{source.bot_qq}:{source.owner_qq}"
    group_windows: dict[str, str] = {}
    for record in candidates.values():
        if record.is_group and record.group_id not in group_windows:
            group_windows[record.group_id] = await _ensure_group_window(
                session, target=target, record=record,
            )
    for memory_id, record in candidates.items():
        is_group = record.is_group
        visibility = Visibility.group_scope.value if is_group else Visibility.subject_private.value
        rows.append({
            "id": memory_id,
            "subject_id": target.destination.subject_id,
            "kind": "episode" if record.is_summary else "assertion",
            "text": record.content,
            "origin": Origin.model_inference.value if record.is_summary else Origin.user_statement.value,
            "visibility": visibility,
            "source_visibility": visibility,
            "source_window_id": group_windows.get(record.group_id) if is_group else None,
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
                "scope": record.scope,
                "group_id": record.group_id,
                "platform": record.platform,
                "session_id": record.session_id,
                "bot_qq": source.bot_qq,
                "owner_qq": source.owner_qq,
                "occurred_at": record.occurred_at.isoformat() if record.occurred_at else None,
                "created_at": record.created_at.isoformat(),
                "updated_at": record.updated_at.isoformat(),
                "valid_from": record.valid_from.isoformat() if record.valid_from else None,
                "valid_to": record.valid_to.isoformat() if record.valid_to else None,
            },
        })
    stmt = insert(MemoryRow).on_conflict_do_nothing(index_elements=[MemoryRow.id]).returning(MemoryRow.id)
    inserted_ids = list(await session.scalars(stmt, rows))
    scopes: Counter[str] = Counter()
    for memory_id in inserted_ids:
        scopes[candidates[memory_id].scope] += 1
    return len(inserted_ids), scopes
