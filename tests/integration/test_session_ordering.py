"""会话列表按置顶、手动顺序和最近收到的消息排序。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ling.domains.conversation.service import ConversationService
from ling.infra.db import session_scope
from ling.infra.models import Message, RelationshipState, User, Window
from tests.integration.conftest import TestEnv, logged_in  # noqa: F401 - fixture


async def _make_window(
    env: TestEnv,
    *,
    title: str,
    received_at: datetime | None = None,
    pinned_at: datetime | None = None,
) -> str:
    async with session_scope() as session:
        service = ConversationService(session)
        window = await service.create_window(subject_id=env.subject_id, title=title)
        window.pinned_at = pinned_at
        if received_at is not None:
            message = await service.add_message(window.id, role="user", text="历史消息")
            row = await session.get(Message, message.message_id)
            assert row is not None
            row.created_at = received_at
            window.updated_at = received_at
        return window.id


async def test_manual_order_survives_activity_and_respects_pin_groups(logged_in: TestEnv) -> None:  # noqa: F811
    now = datetime.now(UTC)
    oldest = await _make_window(logged_in, title="oldest", received_at=now - timedelta(days=3))
    middle = await _make_window(logged_in, title="middle", received_at=now - timedelta(days=2))
    newest = await _make_window(logged_in, title="newest", received_at=now - timedelta(days=1))
    pinned = await _make_window(logged_in, title="pinned", received_at=now, pinned_at=now)
    path = "/v1/sessions"

    async def listed() -> list[str]:
        response = await logged_in.client.get(path)
        assert response.status_code == 200, response.text
        return [row["session_id"] for row in response.json()["sessions"]]

    selected = {oldest, middle, newest, pinned}
    assert [row for row in await listed() if row in selected] == [pinned, newest, middle, oldest]
    move = await logged_in.client.post(
        f"{path}/{oldest}/move", json={"anchor_id": newest, "before": True}
    )
    assert move.status_code == 204, move.text
    async with session_scope() as session:
        old_window = await session.get(Window, oldest)
        assert old_window is not None and old_window.updated_at == now - timedelta(days=3)
        await ConversationService(session).add_message(middle, role="user", text="新消息")
    assert [row for row in await listed() if row in selected] == [pinned, oldest, newest, middle]

    cross_group = await logged_in.client.post(
        f"{path}/{oldest}/move", json={"anchor_id": pinned, "before": True}
    )
    assert cross_group.status_code == 422, cross_group.text
    pin = await logged_in.client.post(f"{path}/{middle}/pin", json={"pinned": True})
    assert pin.status_code == 200, pin.text
    assert [row for row in await listed() if row in selected] == [middle, pinned, oldest, newest]
    move = await logged_in.client.post(
        f"{path}/{middle}/move", json={"anchor_id": pinned, "before": False}
    )
    assert move.status_code == 204, move.text
    assert [row for row in await listed() if row in selected] == [pinned, middle, oldest, newest]
    repin = await logged_in.client.post(f"{path}/{middle}/pin", json={"pinned": True})
    assert repin.status_code == 200, repin.text
    assert [row for row in await listed() if row in selected] == [middle, pinned, oldest, newest]


async def test_presentation_edits_keep_existing_permissions(logged_in: TestEnv) -> None:  # noqa: F811
    async with session_scope() as session:
        service = ConversationService(session)
        channel = await service.create_window(
            subject_id=logged_in.subject_id, title="onebot-default:group:100200400", channel="onebot"
        )
        web = await service.create_window(subject_id=logged_in.subject_id, title="原名")
        foreign = await service.create_window(subject_id="subj_unrelated", title="其他会话")
        channel_id, web_id, foreign_id = channel.id, web.id, foreign.id

    path = "/v1/sessions"
    unavailable = await logged_in.client.post(
        f"{path}/{web_id}/move", json={"anchor_id": foreign_id, "before": True}
    )
    assert unavailable.status_code == 404, unavailable.text

    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.subject_id == logged_in.subject_id))
        assert user is not None
        user.role = "member"

    renamed_person = await logged_in.client.patch(
        f"/v1/accounts/subjects/{logged_in.subject_id}", json={"display_name": "未授权"}
    )
    rename_channel = await logged_in.client.patch(f"{path}/{channel_id}", json={"title": "未授权"})
    pin_channel = await logged_in.client.post(f"{path}/{channel_id}/pin", json={"pinned": True})
    reorder = await logged_in.client.post(
        f"{path}/{web_id}/move", json={"anchor_id": channel_id, "before": True}
    )
    assert all(response.status_code == 403 for response in (
        renamed_person, rename_channel, pin_channel, reorder
    ))
    rename_web = await logged_in.client.patch(f"{path}/{web_id}", json={"title": "网页新名"})
    assert rename_web.status_code == 200 and rename_web.json()["title"] == "网页新名"


async def test_new_input_orders_sessions_without_bumping_renames_or_replies(
    logged_in: TestEnv,  # noqa: F811
) -> None:
    now = datetime.now(UTC)
    recent = await _make_window(logged_in, title="近期会话", received_at=now)
    older = await _make_window(
        logged_in, title="较早会话", received_at=now - timedelta(days=30)
    )
    empty = await _make_window(logged_in, title="空会话")
    async with session_scope() as session:
        session.add(RelationshipState(
            id="rel_session_ordering",
            actor_id=logged_in.settings.actor_id,
            subject_id=logged_in.subject_id,
            familiarity=0.45,
        ))

    async def listed() -> list[str]:
        response = await logged_in.client.get("/v1/sessions")
        assert response.status_code == 200, response.text
        rows = {row["session_id"]: row for row in response.json()["sessions"]}
        assert rows[recent]["familiarity"] == 0.45
        return [session_id for session_id in rows if session_id in {recent, older, empty}]

    assert await listed() == [recent, older, empty]
    rename = await logged_in.client.patch(f"/v1/sessions/{older}", json={"title": "新会话名"})
    assert rename.status_code == 200, rename.text
    assert await listed() == [recent, older, empty]
    async with session_scope() as session:
        await ConversationService(session).add_message(older, role="actor", text="她发的消息")
    assert await listed() == [recent, older, empty]
    async with session_scope() as session:
        await ConversationService(session).add_message(older, role="user", text="刚收到的消息")
    assert await listed() == [older, recent, empty]


async def test_pinned_windows_come_first(logged_in: TestEnv) -> None:  # noqa: F811
    now = datetime.now(UTC)
    await _make_window(
        logged_in,
        title="old-pinned",
        received_at=now - timedelta(days=60),
        pinned_at=now - timedelta(days=60),
    )
    await _make_window(logged_in, title="new-unpinned", received_at=now)
    response = await logged_in.client.get("/v1/sessions")
    assert response.status_code == 200, response.text
    assert response.json()["sessions"][0]["title"] == "old-pinned"


async def test_pin_endpoint_round_trips_and_exposes_the_field(logged_in: TestEnv) -> None:  # noqa: F811
    import uuid

    window_id = await _make_window(
        logged_in, title=f"pin-{uuid.uuid4().hex[:8]}", received_at=datetime.now(UTC)
    )

    # 1) 置顶：返回整条记录，`pinned` 为真
    response = await logged_in.client.post(
        f"/v1/sessions/{window_id}/pin", json={"pinned": True}
    )
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["pinned"] is True, record
    assert record["session_id"] == window_id

    # 2) 列表里也带这个字段，且它排在前面
    listed = (await logged_in.client.get("/v1/sessions")).json()["sessions"]
    mine = [item for item in listed if item["session_id"] == window_id]
    assert len(mine) == 1, listed
    assert mine[0]["pinned"] is True
    assert listed[0]["session_id"] == window_id, "置顶后没排到最前"

    # 3) 幂等：重复置顶不报错
    again = await logged_in.client.post(
        f"/v1/sessions/{window_id}/pin", json={"pinned": True}
    )
    assert again.status_code == 200, again.text

    # 4) 取消置顶：`pinned` 变回假
    unpinned = await logged_in.client.post(
        f"/v1/sessions/{window_id}/pin", json={"pinned": False}
    )
    assert unpinned.status_code == 200, unpinned.text
    assert unpinned.json()["pinned"] is False

    # 5) 幂等：取消一个没置顶的也不报错
    noop = await logged_in.client.post(
        f"/v1/sessions/{window_id}/pin", json={"pinned": False}
    )
    assert noop.status_code == 200, noop.text
