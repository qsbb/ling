"""关系详情的身份、来源、清理和游标边界。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ling.domains.identity.service import IdentityService
from ling.infra.db import session_scope
from ling.infra.models import (
    ActorAccount,
    MemoryPurgeTombstone,
    MemoryRow,
    Message,
    RelationshipEvent,
    Subject,
    Turn,
    Window,
)
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web
STAMP = datetime(2026, 1, 1, tzinfo=UTC)


def _memory(
    memory_id: str,
    subject_id: str,
    *,
    visibility: str = "subject_private",
    source: str = "subject_private",
    retracted: bool = False,
    archived: bool = False,
) -> MemoryRow:
    return MemoryRow(
        id=memory_id,
        subject_id=subject_id,
        kind="assertion",
        text=f"记忆 {memory_id}",
        origin="user_statement",
        visibility=visibility,
        source_visibility=source,
        retracted=retracted,
        archived_at=STAMP if archived else None,
        created_at=STAMP,
    )


async def _other_user() -> str:
    async with session_scope() as session:
        return (
            await IdentityService(session).create_user(
                username="other", password="other-password", role="member"
            )
        ).subject_id


async def _detail(env: TestEnv, subject: str, section: str, **params: object) -> dict:
    response = await env.client.get(
        f"/v1/accounts/subjects/{subject}/details", params={"section": section, **params}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_details_apply_subject_and_source_visibility_after_merge(logged_in: TestEnv) -> None:
    other = await _other_user()
    async with session_scope() as session:
        member = await session.get(Subject, other)
        assert member is not None
        member.merged_into = logged_in.subject_id
        session.add_all(
            [
                ActorAccount(
                    id="acct_owner",
                    subject_id=logged_in.subject_id,
                    connector_instance="demo",
                    external_account_id="owner",
                ),
                ActorAccount(
                    id="acct_member",
                    subject_id=other,
                    connector_instance="demo",
                    external_account_id="member",
                ),
                _memory("own_private", logged_in.subject_id),
                _memory(
                    "own_group",
                    logged_in.subject_id,
                    visibility="group_scope",
                    source="group_scope",
                ),
                _memory(
                    "own_window",
                    logged_in.subject_id,
                    visibility="window_private",
                    source="window_private",
                ),
                _memory("own_admin", logged_in.subject_id, visibility="admin_only"),
                _memory("own_removed", logged_in.subject_id, retracted=True),
                _memory("own_archived", logged_in.subject_id, archived=True),
                _memory("other_private", other),
                _memory("other_public", other, visibility="public", source="public"),
                _memory("other_group_source", other, visibility="public", source="group_scope"),
                _memory(
                    "other_private_source", other, visibility="public", source="subject_private"
                ),
            ]
        )

    accounts = await _detail(logged_in, other, "accounts")
    assert {item["account_id"] for item in accounts["items"]} == {"acct_owner", "acct_member"}
    own = await _detail(logged_in, logged_in.subject_id, "memories")
    assert {item["memory_id"] for item in own["items"]} == {"own_private"}
    public = await _detail(logged_in, other, "memories")
    assert public["access"] == "public"
    assert {item["memory_id"] for item in public["items"]} == {"other_public"}
    assert (await _detail(logged_in, other, "interactions"))["access"] == "restricted"

    # 交换登录者后验证同一组的反向权限。合并本身不会让成员读取主人的私有记忆。
    login = await logged_in.client.post(
        "/v1/auth/login", json={"username": "other", "password": "other-password"}
    )
    assert login.status_code == 200, login.text
    member_own = await _detail(logged_in, other, "memories")
    assert {item["memory_id"] for item in member_own["items"]} == {
        "other_private",
        "other_public",
        "other_private_source",
    }
    owner_from_member = await _detail(logged_in, logged_in.subject_id, "memories")
    assert owner_from_member["access"] == "public"
    assert owner_from_member["items"] == []


async def test_interactions_require_unretracted_personal_window_evidence(
    logged_in: TestEnv,
) -> None:
    other = await _other_user()
    async with session_scope() as session:
        session.add_all(
            [
                Window(
                    id="window_private",
                    actor_id="actor_ling",
                    persona_id="p",
                    subject_id=logged_in.subject_id,
                    visibility="subject_private",
                ),
                Window(
                    id="window_group",
                    actor_id="actor_ling",
                    persona_id="p",
                    subject_id=logged_in.subject_id,
                    visibility="group_scope",
                ),
                Window(
                    id="window_other",
                    actor_id="actor_ling",
                    persona_id="p",
                    subject_id=other,
                    visibility="subject_private",
                ),
            ]
        )
        await session.flush()
        for suffix, window_id, retracted, sender in [
            ("allowed", "window_private", False, logged_in.subject_id),
            ("withdrawn", "window_private", True, logged_in.subject_id),
            ("group", "window_group", False, logged_in.subject_id),
            ("other", "window_other", False, logged_in.subject_id),
            ("other_sender", "window_private", False, other),
        ]:
            session.add(Turn(id=f"turn_{suffix}", window_id=window_id, client_turn_id=suffix))
            await session.flush()
            session.add(
                Message(
                    id=f"message_{suffix}",
                    window_id=window_id,
                    turn_id=f"turn_{suffix}",
                    role="user",
                    text=f"正文 {suffix}",
                    sender_subject_id=sender,
                    retracted_at=STAMP if retracted else None,
                )
            )
            session.add(
                RelationshipEvent(
                    id=f"event_{suffix}",
                    actor_id="actor_ling",
                    subject_id=logged_in.subject_id,
                    turn_id=f"turn_{suffix}",
                    kind="warmth",
                    origin="user_statement",
                    note=f"隐私原文 {suffix}",
                    occurred_at=STAMP,
                )
            )
        session.add(
            RelationshipEvent(
                id="event_no_evidence",
                actor_id="actor_ling",
                subject_id=logged_in.subject_id,
                kind="warmth",
                origin="user_statement",
                occurred_at=STAMP,
            )
        )
        session.add(
            Message(
                id="voice_message",
                window_id="window_private",
                turn_id="turn_allowed",
                role="actor",
                output_modality="voice",
                text="语音正文应被隐藏",
            )
        )

    result = await _detail(logged_in, logged_in.subject_id, "interactions")
    assert [item["event_id"] for item in result["items"]] == ["event_allowed"]
    assert "原文" not in str(result)
    assert "正文" not in str(result)
    assert (await _detail(logged_in, other, "interactions"))["items"] == []


async def test_memory_pages_are_stable_when_new_rows_arrive(logged_in: TestEnv) -> None:
    subject = logged_in.subject_id
    async with session_scope() as session:
        session.add_all([_memory(f"memory_{i:02d}", subject) for i in range(25)])

    first = await _detail(logged_in, subject, "memories", limit=6)
    assert len(first["items"]) == 6 and first["next_cursor"]
    seen = [item["memory_id"] for item in first["items"]]
    async with session_scope() as session:
        newer = _memory("memory_new", subject)
        newer.created_at = STAMP + timedelta(days=1)
        session.add(newer)
    cursor = first["next_cursor"]
    while cursor:
        page = await _detail(logged_in, subject, "memories", limit=6, cursor=cursor)
        seen.extend(item["memory_id"] for item in page["items"])
        cursor = page["next_cursor"]
    assert len(seen) == len(set(seen)) == 25
    assert set(seen) == {f"memory_{i:02d}" for i in range(25)}


async def test_account_and_interaction_pages_keep_stable_order(logged_in: TestEnv) -> None:
    subject = logged_in.subject_id
    async with session_scope() as session:
        session.add(
            Window(
                id="page_window",
                actor_id="actor_ling",
                persona_id="p",
                subject_id=subject,
                visibility="subject_private",
            )
        )
        session.add_all(
            [
                ActorAccount(
                    id=f"account_{i:02d}",
                    subject_id=subject,
                    connector_instance="demo",
                    external_account_id=f"account-{i:02d}",
                )
                for i in range(15)
            ]
        )
        await session.flush()
        session.add_all(
            [
                Turn(id=f"page_turn_{i:02d}", window_id="page_window", client_turn_id=f"t{i}")
                for i in range(15)
            ]
        )
        await session.flush()
        for i in range(15):
            session.add(
                Message(
                    id=f"page_message_{i:02d}",
                    window_id="page_window",
                    turn_id=f"page_turn_{i:02d}",
                    role="user",
                    text="测试正文",
                )
            )
            session.add(
                RelationshipEvent(
                    id=f"page_event_{i:02d}",
                    actor_id="actor_ling",
                    subject_id=subject,
                    turn_id=f"page_turn_{i:02d}",
                    kind="warmth",
                    origin="user_statement",
                    occurred_at=STAMP,
                )
            )

    for section, id_key, prefix in [
        ("accounts", "account_id", "account"),
        ("interactions", "event_id", "page_event"),
    ]:
        seen: list[str] = []
        cursor = None
        while True:
            params = {"limit": 4}
            if cursor:
                params["cursor"] = cursor
            page = await _detail(logged_in, subject, section, **params)
            seen.extend(item[id_key] for item in page["items"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert len(seen) == len(set(seen)) == 15
        assert set(seen) == {f"{prefix}_{i:02d}" for i in range(15)}

    accounts = await _detail(logged_in, subject, "accounts", limit=1)
    invalid = await logged_in.client.get(
        f"/v1/accounts/subjects/{subject}/details",
        params={"section": "memories", "cursor": accounts["next_cursor"]},
    )
    assert invalid.status_code == 422


async def test_pending_purge_hides_own_and_public_memories(logged_in: TestEnv) -> None:
    other = await _other_user()
    async with session_scope() as session:
        member = await session.get(Subject, other)
        assert member is not None
        member.merged_into = logged_in.subject_id
        session.add_all(
            [
                _memory("purged_own", logged_in.subject_id),
                _memory("purged_public", other, visibility="public", source="public"),
                MemoryPurgeTombstone(
                    id="purge_own", subject_id=logged_in.subject_id, generation=1, state="running"
                ),
            ]
        )
    own = await _detail(logged_in, logged_in.subject_id, "memories")
    assert own["access"] == "purging" and own["items"] == []
    other_page = await _detail(logged_in, other, "memories")
    assert other_page["access"] == "public" and other_page["items"] == []


async def test_detail_rejects_bad_cursor_and_missing_subject(logged_in: TestEnv) -> None:
    path = f"/v1/accounts/subjects/{logged_in.subject_id}/details"
    bad = await logged_in.client.get(path, params={"section": "memories", "cursor": "invalid"})
    assert bad.status_code == 422
    invalid_limit = await logged_in.client.get(path, params={"section": "accounts", "limit": 101})
    assert invalid_limit.status_code == 422
    missing = await logged_in.client.get(
        "/v1/accounts/subjects/missing/details", params={"section": "memories"}
    )
    assert missing.status_code == 404
