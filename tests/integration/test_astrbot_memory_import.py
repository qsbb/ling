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
from ling.domains.memory.service import MemoryService, render_evidence
from ling.infra.db import session_scope
from ling.infra.models import ActorAccount, MemoryRow, OutboxEvent, Subject
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web
OWNER_QQ = "100200300"
BOT_QQ = "200300400"
PREFIX = "/v1/memory/import"
HEADERS = {"Content-Type": "application/x-ndjson"}


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


def _archive(*records: dict) -> bytes:
    header = {
        "format": "ling-astrbot-memory-jsonl", "version": 1, "bot_qq": BOT_QQ, "owner_qq": OWNER_QQ,
    }
    return ("\n".join(json.dumps(item, ensure_ascii=False) for item in (header, *records)) + "\n").encode()


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
    header = {
        "record_type": "header", "format": "astrbot-memory-jsonl", "version": 1,
        "plugin": "astrbot_plugin_memory_companion", "encoding": "utf-8",
    }
    record = {"record_type": "memory", "data": {"id": "example", "content": "测试记忆"}}
    before = await _row_counts()
    for format_, expected in (
        ("astrbot-memory-jsonl", "astrbot_native_archive"),
        ("other-memory-jsonl", "unsupported_archive"),
    ):
        data = ("\n".join(json.dumps(row) for row in ({**header, "format": format_}, record)) + "\n").encode()
        response = await logged_in.client.post(PREFIX + "/preview", content=data, headers=HEADERS)
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["detail"] == expected
        if expected == "astrbot_native_archive":
            assert "SQLite" in error["message"]
    assert await _row_counts() == before


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
    archive = importer.parse_archive(BytesIO(content), digest=hashlib.sha256(content).hexdigest())
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
