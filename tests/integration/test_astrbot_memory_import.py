"""Exercise converter output and the owner import workflow on real PostgreSQL."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from scripts.export_astrbot_memories import convert
from sqlalchemy import func, select

from ling.api.routes import memory_import
from ling.contracts.v1 import Visibility
from ling.domains.identity.service import IdentityService
from ling.domains.memory import astrbot_import as importer
from ling.domains.memory.entities import EntityService
from ling.domains.memory.service import MemoryService, render_evidence
from ling.infra.db import session_scope
from ling.infra.models import ActorAccount, EntityRow, MemoryEntityRow, MemoryRow, OutboxEvent, Subject, Window
from ling.kernel.audience import AudienceSnapshot
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web
OWNER_QQ = "100200300"
BOT_QQ = "200300400"
GROUP_ID = "300400500"
PREFIX = "/v1/memory/import"
HEADERS = {"Content-Type": "application/x-ndjson"}
NATIVE_HEADERS = {
    **HEADERS,
    "X-Ling-Import-Owner-QQ": OWNER_QQ,
    "X-Ling-Import-Bot-QQ": BOT_QQ,
}


def _record(source_id: str = "memo-original", **changes: object) -> dict:
    now = datetime.now(UTC)
    return {
        "id": source_id,
        "memory_type": "conversation_summary",
        "subject_kind": "user",
        "subject_id": OWNER_QQ,
        "object_kind": "bot",
        "object_id": BOT_QQ,
        "scope": "private",
        "session_id": f"aiocqhttp:FriendMessage:{OWNER_QQ}",
        "platform": "aiocqhttp",
        "group_id": "",
        "owner_bot_id": BOT_QQ,
        "visibility": "private_pair",
        "sayability": "direct",
        "reality_level": "llm_summary",
        "lifecycle": "stable_memory",
        "sensitivity": "internal",
        "review_status": "auto",
        "validity_status": "active",
        "content": "古典吉他课在周四晚上",
        "confidence": 0.85,
        "created_at": (now - timedelta(days=10)).isoformat(),
        "updated_at": (now - timedelta(days=8)).isoformat(),
        "occurred_at": (now - timedelta(days=12)).isoformat(),
        "valid_from": (now - timedelta(days=12)).isoformat(),
        "valid_to": (now + timedelta(days=3)).isoformat(),
        "source_event_ids": ["old-event"],
        "source_message_ids": ["old-message"],
        **changes,
    }


def _group_record(source_id: str = "group-memo", **changes: object) -> dict:
    fields = {
        "subject_kind": "bot", "subject_id": BOT_QQ,
        "object_kind": "group", "object_id": GROUP_ID,
        "scope": "group", "session_id": f"onebot-default:GroupMessage:{GROUP_ID}",
        "group_id": GROUP_ID, "visibility": "group_public", "sensitivity": "internal",
        "content": "读书群约定周五借出蓝封画册",
    }
    fields.update(changes)
    return _record(source_id, **fields)


def _archive(*records: dict) -> bytes:
    header = {
        "format": "ling-astrbot-memory-jsonl", "version": 1, "bot_qq": BOT_QQ, "owner_qq": OWNER_QQ,
    }
    return ("\n".join(json.dumps(item, ensure_ascii=False) for item in (header, *records)) + "\n").encode()


def _native_archive(*entries: dict, format_: str = "astrbot-memory-jsonl") -> bytes:
    header = {
        "record_type": "header", "format": format_, "version": 1,
        "plugin": "astrbot_plugin_memory_companion", "encoding": "utf-8",
    }
    return ("\n".join(json.dumps(item, ensure_ascii=False) for item in (header, *entries)) + "\n").encode()


def _native_entry(source_id: str, **changes: object) -> dict:
    row = _record(source_id, session_id=f"default:FriendMessage:{OWNER_QQ}")
    subject = {"kind": row.pop("subject_kind"), "id": row.pop("subject_id")}
    target = {"kind": row.pop("object_kind"), "id": row.pop("object_id")}
    metadata = {key: row.pop(key) for key in ("owner_bot_id", "source_event_ids", "source_message_ids")}
    for key in ("validity_status", "sensitivity", "valid_from", "valid_to"):
        row.pop(key)
    return {"record_type": "memory", "data": {
        **row, "subject": subject, "object": target, "metadata": metadata,
        "message_id": "direct-message", **changes,
    }}


def _sqlite_snapshot(path: Path, *records: dict) -> None:
    rows = []
    for record in records:
        row = dict(record)
        row["message_id"] = "direct-message"
        row["metadata"] = json.dumps({
            field: row.pop(field) for field in ("source_event_ids", "source_message_ids")
        })
        rows.append(row)
    columns = ", ".join(
        f"{field} REAL" if field == "confidence" else f"{field} TEXT" for field in rows[0]
    )
    with closing(sqlite3.connect(path)) as db:
        db.execute(f"CREATE TABLE memories ({columns})")
        db.executemany(
            f"INSERT INTO memories VALUES ({', '.join('?' for _ in rows[0])})",
            [tuple(row.values()) for row in rows],
        )
        db.commit()


async def _bind(env: TestEnv, *, proof: str = "admin") -> str:
    return (await env.client.post("/v1/accounts/bind", json={
        "connector_instance": "onebot-default", "external_account_id": OWNER_QQ, "proof": proof,
    })).json()["account_id"]


async def _row_counts() -> list[int]:
    async with session_scope() as session:
        return [
            int(await session.scalar(select(func.count()).select_from(table)) or 0)
            for table in (MemoryRow, ActorAccount, OutboxEvent)
        ]


async def test_preview_distinguishes_native_astrbot_export(logged_in: TestEnv) -> None:
    before = await _row_counts()
    for format_, status, expected in (
        ("astrbot-memory-jsonl", 409, "qq_binding_required"),
        ("other-memory-jsonl", 422, "unsupported_archive"),
    ):
        data = _native_archive(_native_entry("example"), format_=format_)
        response = await logged_in.client.post(PREFIX + "/preview", content=data, headers=HEADERS)
        assert response.status_code == status
        assert response.json()["error"]["detail"] == expected
    assert await _row_counts() == before


async def test_native_export_previews_and_imports_only_the_selected_private_pair(
    logged_in: TestEnv,
) -> None:
    await _bind(logged_in)
    before = await _row_counts()
    valid_until = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    data = _native_archive(
        _native_entry("summary"),
        _native_entry("object-bot", metadata={}),
        _native_entry(
            "profile", object={"kind": "user", "id": OWNER_QQ},
            memory_type="user_profile", reality_level="real_user_fact",
            metadata={"owner_bot_id": BOT_QQ, "valid_to": valid_until},
        ),
        _native_entry("group", scope="group", group_id="100200301"),
        _native_entry("stranger", subject={"kind": "user", "id": "100200302"}),
        _native_entry("wrong-bot", metadata={"owner_bot_id": "200300401"}),
        _native_entry("unknown-bot", object={"kind": "user", "id": OWNER_QQ}, metadata={}),
        _native_entry("unknown-platform", platform="default"),
        _native_entry("archived", lifecycle="archived"),
        _native_entry("pending", review_status="pending"),
        _native_entry("restricted", metadata={"sensitivity": "secret"}),
        _native_entry("expired", metadata={"valid_to": "2000-01-01T00:00:00Z"}),
        {"record_type": "memory", "data": {"content": "字段不全的记忆"}},
        {"record_type": "timeline", "data": _native_entry("timeline")["data"]},
        {"record_type": "identity", "data": {"entity_kind": "user", "entity_id": OWNER_QQ}},
    )
    not_bound = await logged_in.client.post(
        PREFIX + "/preview", content=data,
        headers={**NATIVE_HEADERS, "X-Ling-Import-Owner-QQ": "100200302"},
    )
    assert not_bound.status_code == 409 and not_bound.json()["error"]["detail"] == "qq_binding_required"
    same_qq = await logged_in.client.post(
        PREFIX + "/preview", content=data,
        headers={**NATIVE_HEADERS, "X-Ling-Import-Bot-QQ": OWNER_QQ},
    )
    assert same_qq.status_code == 422 and same_qq.json()["error"]["detail"] == "invalid_source_pair"
    unmatched = await logged_in.client.post(
        PREFIX + "/preview",
        content=_native_archive(_native_entry("unverified", platform="default")),
        headers=HEADERS,
    )
    assert unmatched.status_code == 422 and unmatched.json()["error"]["detail"] == "astrbot_source_unmatched"
    preview = await logged_in.client.post(PREFIX + "/preview", content=data, headers=HEADERS)
    assert preview.status_code == 200, preview.text
    report = preview.json()
    assert (report["total"], report["accepted"], report["skipped"]) == (13, 3, 10)
    assert report["destination"]["qq"] == OWNER_QQ and report["destination"]["bot_qq"] == BOT_QQ
    assert {sample["source_id"] for sample in report["sample"]} == {"summary", "object-bot", "profile"}
    assert report["skip_reasons"] == {
        "outside_private_pair": 5, "inactive": 1, "not_reviewed": 1,
        "restricted": 1, "expired": 1, "invalid_record": 1,
    }
    assert await _row_counts() == before

    headers = {**NATIVE_HEADERS, "X-Ling-Import-Token": report["confirmation_token"]}
    changed_pair = await logged_in.client.post(
        PREFIX + "/commit", content=data, headers={**headers, "X-Ling-Import-Bot-QQ": "200300401"},
    )
    assert changed_pair.status_code == 409 and changed_pair.json()["error"]["detail"] == "preview_invalid"
    assert await _row_counts() == before
    commit = await logged_in.client.post(PREFIX + "/commit", content=data, headers=headers)
    assert commit.status_code == 200, commit.text
    assert commit.json()["imported"] == 3
    library = (await logged_in.client.get("/v1/memory?view=library&source=astrbot_memory_companion")).json()
    rows = {row["import_source"]["record_id"]: row for row in library["memories"]}
    assert set(rows) == {"summary", "object-bot", "profile"}
    assert rows["summary"]["kind"] == "episode" and rows["summary"]["valid_until"] is None
    assert rows["profile"]["kind"] == "assertion" and rows["profile"]["valid_until"] == valid_until
    assert f"astrbot:{BOT_QQ}:{OWNER_QQ}:message:direct-message" in rows["summary"]["source_event_ids"]
    repeat = await logged_in.client.post(
        PREFIX + "/preview", content=_archive(_record("summary")), headers=HEADERS,
    )
    assert repeat.status_code == 200
    assert (repeat.json()["accepted"], repeat.json()["duplicates"]) == (0, 1)


async def test_group_export_imports_with_group_scope_and_window(logged_in: TestEnv) -> None:
    await _bind(logged_in)
    data = _archive(
        _group_record("group-memo", group_name="读书群"),
        _group_record("wrong-group", group_id="300400501", object_id="300400501"),
        _group_record("wrong-bot", subject_id="200300401", owner_bot_id="200300401"),
        _record("private-memo"),
    )
    preview = await logged_in.client.post(PREFIX + "/preview", content=data, headers=HEADERS)
    assert preview.status_code == 200, preview.text
    report = preview.json()
    assert report["accepted"] == 2
    assert report["scope_counts"] == {"private": 1, "group": 1}
    assert {sample["scope"] for sample in report["sample"]} == {"private", "group"}
    assert report["skip_reasons"]["outside_private_pair"] == 2

    headers = {**NATIVE_HEADERS, "X-Ling-Import-Token": report["confirmation_token"]}
    commit = await logged_in.client.post(PREFIX + "/commit", content=data, headers=headers)
    assert commit.status_code == 200, commit.text
    assert commit.json()["imported"] == 2

    async with session_scope() as session:
        row = (await session.execute(
            select(MemoryRow).where(MemoryRow.import_source["record_id"].as_string() == "group-memo")
        )).scalar_one()
        assert row.visibility == "group_scope"
        assert row.source_visibility == "group_scope"
        assert row.source_window_id
        window = await session.get_one(Window, row.source_window_id)
        assert window.visibility == "group_scope"
        assert window.connector_window_ref == f"onebot-default:group:{GROUP_ID}"
        assert window.subject_id == logged_in.subject_id
        assert window.origin_status == "unknown"

    library = (await logged_in.client.get("/v1/memory?view=library&source=astrbot_memory_companion")).json()
    group_row = next(item for item in library["memories"] if item["import_source"]["record_id"] == "group-memo")
    assert group_row["visibility"] == "group_scope"
    assert group_row["import_source"]["scope"] == "group"
    assert group_row["import_source"]["group_id"] == GROUP_ID

    async with session_scope() as session:
        entity = (await session.execute(
            select(EntityRow).where(EntityRow.subject_id == logged_in.subject_id, EntityRow.kind == "group")
        )).scalar_one()
        assert entity.name == f"读书群 · {GROUP_ID}"
        links = list(await session.scalars(
            select(MemoryEntityRow.memory_id).where(MemoryEntityRow.entity_id == entity.id)
        ))
        assert len(links) == 1

        renamed = await EntityService(session).upsert_group(
            subject_id=logged_in.subject_id, group_id=GROUP_ID,
            display_name="新的读书群", aliases=["读书群"],
        )
        assert renamed.id == entity.id
        assert renamed.name == f"新的读书群 · {GROUP_ID}"
        assert "读书群" in renamed.aliases
        assert int(await session.scalar(
            select(func.count()).select_from(EntityRow).where(
                EntityRow.subject_id == logged_in.subject_id, EntityRow.kind == "group",
            )
        ) or 0) == 1
    objects = (await logged_in.client.get("/v1/memory/objects")).json()["objects"]
    assert objects[0]["kind"] == "self" and objects[0]["count"] == 2
    group_object = next(item for item in objects if item["kind"] == "group")
    assert group_object["count"] == 1 and group_object["name"] == f"新的读书群 · {GROUP_ID}"
    only_group = (await logged_in.client.get(
        f"/v1/memory?view=library&object_id={group_object['entity_id']}"
    )).json()
    assert [item["visibility"] for item in only_group["memories"]] == ["group_scope"]
    assert only_group["total"] == 1

    # 历史群窗口不能冒充当前群受众：另一个群窗口不能召回这条记忆。
    async with session_scope() as session:
        audience = await AudienceSnapshot.freeze(
            session, subject_id=logged_in.subject_id,
            window_id="win_live_other_group", window_visibility="group_scope",
        )
        recalled = await MemoryService(session).recall(
            subject_id=logged_in.subject_id, query="蓝封画册",
            visibility_allow={Visibility.group_scope}, audience=audience,
        )
        assert row.id not in {hit.memory_id for hit in recalled.hits}

        # 正例：同源历史群窗口可以召回，证明不是把群记忆一刀切禁掉。
        same_source = await AudienceSnapshot.freeze(
            session, subject_id=logged_in.subject_id,
            window_id=row.source_window_id, window_visibility="group_scope",
        )
        same_recalled = await MemoryService(session).recall(
            subject_id=logged_in.subject_id, query="蓝封画册",
            visibility_allow={Visibility.group_scope}, audience=same_source,
        )
        assert row.id in {hit.memory_id for hit in same_recalled.hits}

        # 私聊窗口也不能读到群记忆。
        private = await AudienceSnapshot.freeze(
            session, subject_id=logged_in.subject_id,
            window_id="win_private", window_visibility="subject_private",
        )
        private_recalled = await MemoryService(session).recall(
            subject_id=logged_in.subject_id, query="蓝封画册",
            visibility_allow={Visibility.subject_private}, audience=private,
        )
        assert row.id not in {hit.memory_id for hit in private_recalled.hits}


async def test_native_export_offers_choices_for_multiple_proven_private_sources(logged_in: TestEnv) -> None:
    await _bind(logged_in)
    other_bot = "200300401"
    data = _native_archive(
        _native_entry("first"),
        _native_entry("second", object={"kind": "bot", "id": other_bot}, metadata={"owner_bot_id": other_bot}),
        _native_entry(
            "unbound-owner", subject={"kind": "user", "id": "100200302"},
            session_id="default:FriendMessage:100200302",
        ),
    )
    response = await logged_in.client.post(PREFIX + "/preview", content=data, headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"sources": [
        {"owner_qq": OWNER_QQ, "bot_qq": BOT_QQ},
        {"owner_qq": OWNER_QQ, "bot_qq": other_bot},
    ]}
    response = await logged_in.client.post(
        PREFIX + "/preview", content=data, headers={**NATIVE_HEADERS, "X-Ling-Import-Bot-QQ": other_bot},
    )
    assert response.status_code == 200
    report = response.json()
    assert report["destination"]["bot_qq"] == other_bot
    assert [row["source_id"] for row in report["sample"]] == ["second"]


async def test_converter_group_scope_is_opt_in(logged_in: TestEnv, tmp_path: Path) -> None:
    snapshot = tmp_path / "memory_companion.db"
    _sqlite_snapshot(
        snapshot,
        _record(session_id=f"default:FriendMessage:{OWNER_QQ}"),
        _group_record("group-summary"),
    )
    private_path = tmp_path / "private.jsonl"
    assert convert(snapshot, private_path, bot_qq=BOT_QQ, owner_qq=OWNER_QQ) == (1, False)
    assert "群聊".encode() not in private_path.read_bytes() and "蓝封画册".encode() not in private_path.read_bytes()
    all_path = tmp_path / "all.jsonl"
    assert convert(
        snapshot, all_path, bot_qq=BOT_QQ, owner_qq=OWNER_QQ, include_group=True,
    ) == (2, False)
    rows = [json.loads(line) for line in all_path.read_text().splitlines()[1:]]
    assert {row["scope"] for row in rows} == {"private", "group"}
    group = next(row for row in rows if row["scope"] == "group")
    assert group["group_id"] == GROUP_ID and group["visibility"] == "group_public"


async def test_converted_snapshot_reaches_library_and_private_recall(
    logged_in: TestEnv, tmp_path: Path,
) -> None:
    snapshot = tmp_path / "memory_companion.db"
    base = _record(session_id=f"default:FriendMessage:{OWNER_QQ}")
    other_record = {**base, "id": "same-content-second-record"}
    _sqlite_snapshot(
        snapshot,
        base,
        other_record,
        _record("other-owner", subject_id="100200301", content="另一个人私聊"),
        _record("group", scope="group", group_id="100200302", content="群聊"),
        _record("wrong-bot", owner_bot_id="100200302", content="另一个 Bot"),
        _record("wrong-session", session_id="default:FriendMessage:100200301"),
        _record("non-qq", platform="other-channel", session_id=f"default:FriendMessage:{OWNER_QQ}"),
    )
    snapshot_digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    path = tmp_path / "memory.jsonl"
    assert convert(snapshot, path, bot_qq=BOT_QQ, owner_qq=OWNER_QQ, offset=0, limit=5000) == (2, False)
    with snapshot.open("rb") as handle:
        assert hashlib.file_digest(handle, "sha256").hexdigest() == snapshot_digest
    data = path.read_bytes()
    assert json.loads(data.splitlines()[1])["validity_status"] == "active"
    assert "另一个人私聊".encode() not in data and "群聊".encode() not in data

    await _bind(logged_in)
    async with session_scope() as session:
        session.add(MemoryRow(
            id="mem_native", subject_id=logged_in.subject_id, kind="assertion",
            text=base["content"], visibility="subject_private", origin="user_statement",
        ))
    before = await _row_counts()
    preview = await logged_in.client.post(PREFIX + "/preview", content=data, headers=HEADERS)
    assert preview.status_code == 200, preview.text
    payload = preview.json()
    assert payload["destination"] == {
        "subject_id": logged_in.subject_id, "display_name": "tester", "qq": OWNER_QQ, "bot_qq": BOT_QQ,
    }
    assert payload["accepted"] == 2 and payload["sample"][0]["text"] == base["content"]
    assert await _row_counts() == before

    headers = {**HEADERS, "X-Ling-Import-Token": payload["confirmation_token"]}
    replies = await asyncio.gather(*[
        logged_in.client.post(PREFIX + "/commit", content=data, headers=headers)
        for _ in range(2)
    ])
    assert all(response.status_code == 200 for response in replies), [r.text for r in replies]
    assert sorted(r.json()["imported"] for r in replies) == [0, 2]
    assert sorted(r.json()["duplicates"] for r in replies) == [0, 2]

    changed = _archive({**other_record, "content": "此修改不能覆盖原文"}, base)
    repeated = await logged_in.client.post(PREFIX + "/preview", content=changed, headers=HEADERS)
    assert (repeated.json()["accepted"], repeated.json()["duplicates"]) == (0, 2)

    library = (await logged_in.client.get("/v1/memory?view=library")).json()
    records = library["memories"]
    assert len(records) == 3
    assert next(r for r in records if r["memory_id"] == "mem_native")["text"] == base["content"]
    row = next(r for r in records if r.get("import_source", {}).get("record_id") == base["id"])
    assert row["origin"] == "model_inference" and row["kind"] == "episode"
    assert row["valid_until"] == base["valid_to"]
    assert row["created_at"] == base["created_at"]
    assert row["import_source"]["occurred_at"] == base["occurred_at"]
    assert row["import_source"]["session_id"] == base["session_id"]
    assert row["import_source"]["updated_at"] == base["updated_at"]
    assert row["import_source"]["valid_from"] == base["valid_from"]
    assert f"astrbot:{BOT_QQ}:{OWNER_QQ}:event:old-event" in row["source_event_ids"]
    assert f"astrbot:{BOT_QQ}:{OWNER_QQ}:message:direct-message" in row["source_event_ids"]

    filtered_url = "/v1/memory?view=library&source=astrbot_memory_companion&limit=1"
    first_page = (await logged_in.client.get(filtered_url)).json()
    second_page = (await logged_in.client.get(filtered_url + "&offset=1")).json()
    assert first_page["has_more"] and not second_page["has_more"]
    assert first_page["total"] == second_page["total"] == 2
    assert {
        page["memories"][0]["import_source"]["record_id"] for page in (first_page, second_page)
    } == {base["id"], other_record["id"]}

    async with session_scope() as session:
        await session.delete(await session.get_one(MemoryRow, "mem_native"))
        recall = await MemoryService(session).recall(
            subject_id=logged_in.subject_id, query="古典吉他课是哪天",
            visibility_allow={Visibility.subject_private},
        )
    assert {hit.memory_id for hit in recall.hits} & {r["memory_id"] for r in records if r.get("import_source")}
    assert "模糊的印象" in render_evidence(recall.hits)


async def test_commit_rechecks_eligibility_and_binds_file(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _bind(logged_in)
    now = datetime.now(UTC)
    upcoming = _record("about-to-expire", valid_to=(now + timedelta(seconds=2)).isoformat())
    data = _archive(
        _record(
            "preference", memory_type="user_preference", reality_level="real_user_fact",
            object_kind="user", object_id=OWNER_QQ,
        ),
        upcoming,
        _record("group", scope="group", group_id="100200302"),
        _record("stranger", subject_id="100200301"),
        _record("inactive", validity_status="archived"),
        _record("pending", review_status="pending"),
        _record("private-policy", sayability="never"),
        _record("relationship", memory_type="relationship_claim"),
        _record("future", valid_from=(now + timedelta(seconds=1)).isoformat()),
        _record("expired", valid_to=(now - timedelta(days=2)).isoformat()),
        _record("old", created_at=(now - timedelta(days=3651)).isoformat()),
        _record("bad-date", valid_from="2026-01-01"),
    )
    monkeypatch.setattr(memory_import, "utcnow", lambda: now)
    preview = (await logged_in.client.post(PREFIX + "/preview", content=data, headers=HEADERS)).json()
    assert preview["accepted"] == 2
    assert preview["skip_reasons"] == {
        "outside_private_pair": 2, "inactive": 1, "not_reviewed": 1, "restricted": 1,
        "unsupported_memory": 1, "not_yet_valid": 1, "expired": 1,
        "retention_expired": 1, "invalid_record": 1,
    }
    headers = {**HEADERS, "X-Ling-Import-Token": preview["confirmation_token"]}
    changed = data.replace("古典吉他".encode(), "古典钢琴".encode())
    assert len(changed) == len(data) and changed != data
    response = await logged_in.client.post(PREFIX + "/commit", content=changed, headers=headers)
    assert response.status_code == 409 and response.json()["error"]["detail"] == "preview_invalid"
    assert (await _row_counts())[0] == 0

    monkeypatch.setattr(memory_import, "utcnow", lambda: now + timedelta(seconds=3))
    response = await logged_in.client.post(PREFIX + "/commit", content=data, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["imported"] == 1 and response.json()["skip_reasons"]["expired"] == 2
    assert response.json()["skip_reasons"]["not_yet_valid"] == 1
    result = (await logged_in.client.get("/v1/memory")).json()["memories"]
    assert [(row["kind"], row["origin"]) for row in result] == [("assertion", "user_statement")]


async def test_large_archive_previews_and_imports_every_batch(logged_in: TestEnv, tmp_path: Path) -> None:
    await _bind(logged_in)
    record_count = 5_001
    template = _record(content="合成记忆" * 600)
    snapshot = tmp_path / "snapshot.db"
    _sqlite_snapshot(snapshot, *(
        {**template, "id": f"record-{number:05}"} for number in range(record_count)
    ))
    path = tmp_path / "archive.jsonl"
    assert convert(snapshot, path, bot_qq=BOT_QQ, owner_qq=OWNER_QQ) == (record_count, False)
    with path.open("rb") as file:
        file.readline()
        first_record = file.readline()
    with path.open("ab") as file:
        file.write(first_record)
    size = path.stat().st_size
    assert size > 32 * 1024 * 1024

    async def body() -> AsyncIterator[bytes]:
        with path.open("rb") as file:
            while chunk := file.read(65_536):
                yield chunk

    length_headers = {**HEADERS, "Content-Length": str(size)}
    preview = await logged_in.client.post(PREFIX + "/preview", content=body(), headers=length_headers)
    assert preview.status_code == 200, preview.text
    report = preview.json()
    assert report["total"] == record_count + 1
    assert report["accepted"] == record_count
    assert report["destination"]["subject_id"] == logged_in.subject_id
    assert report["duplicates"] == 1
    assert report["skipped"] == 0
    assert report["sample"][0]["source_id"] == "record-00000"
    assert (await _row_counts())[0] == 0

    headers = {**HEADERS, "X-Ling-Import-Token": report["confirmation_token"]}
    commit = await logged_in.client.post(PREFIX + "/commit", content=body(), headers=headers)
    assert commit.status_code == 200, commit.text
    assert commit.json()["total"] == record_count + 1
    assert commit.json()["imported"] == record_count
    assert commit.json()["duplicates"] == 1
    async with session_scope() as session:
        rows = await session.execute(select(MemoryRow.subject_id, MemoryRow.import_source["record_id"].as_string()))
        assert set(rows) == {(logged_in.subject_id, f"record-{number:05}") for number in range(record_count)}

    repeated = await logged_in.client.post(PREFIX + "/commit", content=body(), headers={**headers, **length_headers})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["imported"] == 0
    assert repeated.json()["duplicates"] == record_count + 1
    repeated_preview = await logged_in.client.post(PREFIX + "/preview", content=body(), headers=HEADERS)
    assert repeated_preview.status_code == 200, repeated_preview.text
    assert repeated_preview.json()["duplicates"] == record_count + 1
    assert repeated_preview.json()["confirmation_token"] is None


def test_archive_skips_an_oversized_line_and_reads_the_next_record() -> None:
    header, record = _archive(_record()).splitlines(keepends=True)
    content = header + b"x" * (importer.MAX_RECORD_BYTES + 50) + b"\n" + record
    file = BytesIO(content)
    archive = importer.open_archive(
        file, digest=hashlib.sha256(content).hexdigest(), header=importer.read_archive_header(file), source=None,
    )
    invalid, valid = archive.records
    assert invalid is None
    assert valid is not None and valid.id == "memo-original"


async def test_requires_owner_origin_and_current_proven_binding(web_env: TestEnv) -> None:
    data = _archive(_record())
    response = await web_env.client.post(PREFIX + "/preview", content=data, headers=HEADERS)
    assert response.status_code == 401
    async with session_scope() as session:
        service = IdentityService(session)
        member = await service.create_user(username="member", password="test-password")
    await web_env.client.post("/v1/auth/login", json={"username": "member", "password": "test-password"})
    response = await web_env.client.post(PREFIX + "/preview", content=data, headers=HEADERS)
    assert response.status_code == 403
    await web_env.client.post("/v1/auth/login", json={"username": "tester", "password": "tester-password"})
    me = await web_env.client.get("/v1/auth/me")
    web_env.subject_id = me.json()["user"]["subject_id"]

    response = await web_env.client.post(
        PREFIX + "/preview", content=data, headers={**HEADERS, "Origin": "https://other.example"},
    )
    assert response.status_code == 403
    response = await web_env.client.post(
        PREFIX + "/preview", content=b"SQLite format 3\x00fake", headers=HEADERS,
    )
    assert response.status_code == 422 and response.json()["error"]["detail"] == "unsupported_archive"
    binding_id = await _bind(web_env, proof="pending")
    response = await web_env.client.post(PREFIX + "/preview", content=data, headers=HEADERS)
    assert response.status_code == 409 and response.json()["error"]["detail"] == "qq_binding_required"

    async with session_scope() as session:
        binding = await session.get_one(ActorAccount, binding_id)
        binding.proof = "proven"
        binding.subject_id = member.subject_id
        await IdentityService(session).merge_subjects(member.subject_id, web_env.subject_id)
    response = await web_env.client.post(PREFIX + "/preview", content=data, headers=HEADERS)
    assert response.status_code == 409 and response.json()["error"]["detail"] == "qq_binding_required"

    async with session_scope() as session:
        session.add(Subject(id="subj_qq_import", merged_into=web_env.subject_id))
        binding = await session.get_one(ActorAccount, binding_id)
        binding.subject_id = "subj_qq_import"
    preview = await web_env.client.post(PREFIX + "/preview", content=data, headers=HEADERS)
    assert preview.status_code == 200 and preview.json()["accepted"] == 1
    token = preview.json()["confirmation_token"]
    async with session_scope() as session:
        await IdentityService(session).revoke_account(binding_id)
    response = await web_env.client.post(
        PREFIX + "/commit", content=data, headers={**HEADERS, "X-Ling-Import-Token": token},
    )
    assert response.status_code == 409 and response.json()["error"]["detail"] == "qq_binding_required"


def test_confirmation_binds_authenticated_actor_target_generation_and_expiry() -> None:
    now = datetime.now(UTC)
    dest = importer.Destination(subject_id="subject-a", display_name="展示名", qq=OWNER_QQ, bot_qq=BOT_QQ)
    target = importer.ImportTarget(dest, "binding-id", "binding-revision", 0)
    inputs = {"user_id": "user-a", "target": target, "digest": "record-digest", "retention_days": 3650}
    deadline = now + timedelta(minutes=5)
    token = importer.sign_confirmation(
        b"root", **inputs, reviewed_at=int(now.timestamp()), expires=int(deadline.timestamp()),
    )
    assert importer.check_confirmation(b"root", token=token, now=now, **inputs) == now.replace(microsecond=0)
    changes = [
        {"user_id": "user-b"},
        {"target": replace(target, destination=dest.model_copy(update={"subject_id": "subject-b"}))},
        {"target": replace(target, forget_generation=1)},
        {"retention_days": 365},
        {"now": deadline},
    ]
    for change in changes:
        with pytest.raises(importer.ArchiveError, match="preview_invalid"):
            importer.check_confirmation(b"root", token=token, **{**inputs, "now": now, **change})
