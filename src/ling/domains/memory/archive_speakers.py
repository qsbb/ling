"""Index only speakers cited by eligible AstrBot archive summaries."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import BinaryIO

from sqlalchemy import Boolean, Column, MetaData, String, Table, and_, not_, or_
from sqlalchemy.dialects.postgresql import insert

SPEAKER_SOURCES = Table(
    "ling_memory_import_speakers", MetaData(),
    Column("id", String(200), primary_key=True),
    Column("scope", String(16)),
    Column("session_id", String(200)),
    Column("platform", String(80)),
    Column("account", String(20)),
    Column("label", String(64)),
    Column("ambiguous", Boolean, nullable=False, server_default="false"),
    schema="pg_temp", prefixes=["TEMPORARY"], postgresql_on_commit="DROP",
)
_QQ = re.compile(r"[1-9][0-9]{4,19}")


def timeline_sources(
    file: BinaryIO, *, bot_qq: str, owner_qq: str, include_group: bool, max_record_bytes: int,
) -> Iterator[dict[str, str | None]]:
    """Stream source IDs and minimal attribution. The original body is never indexed."""
    start = file.tell()
    try:
        while line := file.readline(max_record_bytes + 1):
            if len(line) > max_record_bytes:
                while line and not line.endswith(b"\n"):
                    line = file.readline(65_536)
                continue
            try:
                entry = json.loads(line.decode("utf-8"))
            except (ValueError, RecursionError):
                continue
            if not isinstance(entry, dict) or entry.get("record_type") != "timeline":
                continue
            data = entry.get("data")
            if not isinstance(data, dict):
                continue
            ref = data.get("id")
            if not isinstance(ref, str) or not 0 < len(ref) <= 200 or not ref.isprintable():
                continue
            scope, source_session = data.get("scope"), data.get("session_id")
            meta = data.get("metadata")
            account = data.get("subject_id")
            valid = (
                data.get("event_type") == "user_message"
                and isinstance(meta, dict)
                and isinstance(account, str) and bool(_QQ.fullmatch(account))
                and isinstance(source_session, str) and 0 < len(source_session) <= 200
                and source_session.isprintable()
                and meta.get("owner_bot_id") == bot_qq
                and meta.get("participant_user_id", account) == account
                and meta.get("source_state") in (None, "current")
                and data.get("source_state") in (None, "current")
                and not any(meta.get(key) or data.get(key) for key in ("retracted_at", "forgotten_at", "deleted_at"))
            )
            if not valid:
                yield dict(id=ref, scope=None, session_id=None, platform=None, account=None, label=None)
                continue
            peer = source_session.rsplit(":", 1)[-1]
            relevant = (
                (scope == "private" and peer == owner_qq == account and not meta.get("source_group_id"))
                or (scope == "group" and include_group and _QQ.fullmatch(peer) is not None
                    and meta.get("source_group_id", peer) == peer)
            )
            label, platform = meta.get("sender_name"), meta.get("platform")
            if (
                relevant and data.get("object_id") == peer
                and isinstance(label, str) and 2 <= len(label.strip()) <= 64 and label.strip().isprintable()
                and isinstance(platform, str) and platform in {"aiocqhttp", "onebot", "qq"}
            ):
                yield dict(id=ref, scope=scope, session_id=source_session, platform=platform,
                           account=account, label=label.strip())
            else:
                yield dict(id=ref, scope=None, session_id=None, platform=None, account=None, label=None)
    finally:
        file.seek(start)


def store_timeline_sources():
    """Reject conflicting IDs while permitting exact repeats from converted exports."""
    table = SPEAKER_SOURCES
    statement = insert(table)
    fields = ("scope", "session_id", "platform", "account", "label")
    same = and_(*(table.c[field].is_not_distinct_from(statement.excluded[field]) for field in fields))
    return statement.on_conflict_do_update(
        index_elements=[table.c.id], set_={"ambiguous": or_(table.c.ambiguous, not_(same))},
    )
