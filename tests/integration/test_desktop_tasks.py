"""Owner and companion behavior through the API and real PostgreSQL."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from connectors.desktop.companion import DesktopCompanion, DesktopExecutionError

from ling.api.routes import desktop
from ling.domains.identity.service import IdentityService
from ling.infra.db import session_scope
from tests.integration.conftest import TestEnv

PATH = "/v1/desktop"
POLL = f"{PATH}/companion/poll"
NOTE = "测试记事本\n带空格的第二行 🌿 "

pytestmark = pytest.mark.w0_web


def _client(env: TestEnv, token: str = "") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"} if token else {},
    )


async def _pair(owner: httpx.AsyncClient) -> str:
    response = await owner.put(f"{PATH}/pair", json={"name": "Windows 验收机"})
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    return response.json()["token"]


@pytest_asyncio.fixture
async def linked(logged_in: TestEnv):
    owner = logged_in.client
    token = await _pair(owner)
    async with _client(logged_in, token) as companion:
        assert (await companion.post(POLL, json={"ready": True})).json() == {"task": None}
        assert (await owner.put(f"{PATH}/control", json={"paused": False})).json()["paused"] is False
        yield owner, companion


async def _queue(owner: httpx.AsyncClient) -> dict:
    body = {"request_id": str(uuid4()), "text": NOTE}
    response = await owner.post(f"{PATH}/tasks", json=body)
    assert response.status_code == 201, response.text
    assert response.json()["state"] == "queued"
    return response.json()


async def _claim(companion: httpx.AsyncClient) -> dict:
    response = await companion.post(POLL, json={"ready": True})
    assert response.status_code == 200, response.text
    return response.json()["task"]


async def _report(companion: httpx.AsyncClient, task: dict, step: str, **fields) -> bool:
    response = await companion.post(f"{PATH}/companion/tasks/{task['id']}/report", json={
        "step": step, "lease_token": task["lease_token"], **fields,
    })
    assert response.status_code == 200, response.text
    return response.json()["accepted"]


class TestEditor:
    __test__ = False

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.text = ""

    def ready(self) -> bool:
        return True

    async def open(self, directory: Path, deadline: float) -> None:
        assert directory.is_dir()
        self.steps.append("opened")

    def write(self, text: str, deadline: float) -> None:
        self.steps.append("written")
        self.text = text.replace("\n", "\r\n")

    def read(self, deadline: float) -> str:
        self.steps.append("read")
        return self.text


async def test_companion_receipt_records_a_verified_unicode_note(linked):
    owner, companion = linked
    queued = await _queue(owner)
    editor = TestEditor()
    runner = DesktopCompanion(companion, editor)
    await runner.tick()
    await runner.tick()
    task, = (await owner.get(PATH)).json()["tasks"]
    assert task["id"] == queued["id"]
    assert (task["kind"], task["state"], task["step"], task["reason"]) == (
        "notepad_write", "succeeded", "verified", None,
    )
    assert task["text"] == NOTE and task["started_at"] <= task["finished_at"]
    assert editor.steps == ["opened", "written", "read"]


async def test_pause_while_opening_stops_write_even_if_already_resumed(linked):
    owner, companion = linked

    class PausedEditor(TestEditor):
        async def open(self, directory: Path, deadline: float) -> None:
            await super().open(directory, deadline)
            assert (await owner.put(f"{PATH}/control", json={"paused": True})).json()["paused"]
            assert not (await owner.put(f"{PATH}/control", json={"paused": False})).json()["paused"]

    await _queue(owner)
    editor = PausedEditor()
    await DesktopCompanion(companion, editor).tick()
    task, = (await owner.get(PATH)).json()["tasks"]
    assert (task["state"], task["step"], task["reason"]) == ("interrupted", None, "owner_paused")
    assert editor.steps == ["opened"]
    await _queue(owner)
    await DesktopCompanion(companion, TestEditor()).tick()
    assert (await owner.get(PATH)).json()["tasks"][0]["state"] == "succeeded"


@pytest.mark.parametrize("reason", ["desktop_unavailable", "window_changed"])
async def test_interactive_target_loss_pauses_future_work(linked, reason):
    owner, companion = linked

    class ChangedEditor(TestEditor):
        def write(self, text: str, deadline: float) -> None:
            raise DesktopExecutionError(reason)

    await _queue(owner)
    editor = ChangedEditor()
    await DesktopCompanion(companion, editor).tick()
    status = (await owner.get(PATH)).json()
    assert status["paused"] is True
    assert status["device"]["state"] == ("unavailable" if reason == "desktop_unavailable" else "ready")
    task, = status["tasks"]
    assert (task["state"], task["step"], task["reason"]) == ("interrupted", "opened", reason)
    assert editor.steps == ["opened"]
    assert await _claim(companion) is None
    assert (await owner.post(f"{PATH}/tasks", json={
        "request_id": str(uuid4()), "text": NOTE,
    })).json()["error"]["detail"] == "paused"


async def test_companion_never_writes_after_losing_the_step_response(linked, monkeypatch):
    owner, companion = linked
    post = companion.post

    async def lose_opened(url, **kwargs):
        result = await post(url, **kwargs)
        if kwargs["json"].get("step") == "opened":
            raise httpx.ReadTimeout("lost the step acknowledgement")
        return result

    monkeypatch.setattr(companion, "post", lose_opened)
    await _queue(owner)
    editor = TestEditor()
    with pytest.raises(httpx.ReadTimeout):
        await DesktopCompanion(companion, editor).tick()
    status = (await owner.get(PATH)).json()
    assert status["paused"]
    task, = status["tasks"]
    assert (task["state"], task["step"], task["reason"]) == ("interrupted", "opened", "connection_lost")
    assert editor.steps == ["opened"]


async def test_readback_requires_exact_text_order_and_holder(linked):
    owner, companion = linked
    await _queue(owner)
    task = await _claim(companion)
    assert not await _report(companion, task, "verified", readback=NOTE)
    assert not await _report(companion, {**task, "lease_token": "a" * 43}, "opened")
    assert await _report(companion, task, "opened")
    assert not await _report(companion, task, "opened")
    assert await _report(companion, task, "written")
    assert await _report(companion, task, "verified", readback=NOTE.strip())
    assert not await _report(companion, task, "verified", readback=NOTE)
    outcome, = (await owner.get(PATH)).json()["tasks"]
    assert (outcome["state"], outcome["step"], outcome["reason"]) == (
        "failed", "written", "readback_mismatch",
    )


async def test_duplication_busy_and_cancel_use_the_same_task(linked):
    owner, companion = linked
    body = {"request_id": str(uuid4()), "text": NOTE}
    responses = await asyncio.gather(*(owner.post(f"{PATH}/tasks", json=body) for _ in range(2)))
    assert all(response.status_code == 201 for response in responses)
    assert responses[0].json() == responses[1].json()
    conflict = await owner.post(f"{PATH}/tasks", json={**body, "text": "different"})
    assert conflict.status_code == 409 and conflict.json()["error"]["detail"] == "request_id_conflict"
    busy = await owner.post(f"{PATH}/tasks", json={**body, "request_id": str(uuid4())})
    assert busy.status_code == 409 and busy.json()["error"]["detail"] == "task_in_progress"
    assignments = [value for value in await asyncio.gather(
        *(_claim(companion) for _ in range(3))
    ) if value]
    task, = assignments
    assert task["id"] == body["request_id"]
    cancelled = await owner.post(f"{PATH}/tasks/{task['id']}/cancel")
    assert cancelled.json()["reason"] == "owner_cancelled"
    assert not await _report(companion, task, "opened")
    retry = await owner.post(f"{PATH}/tasks", json=body)
    assert retry.json()["state"] == "cancelled"
    assert await _claim(companion) is None


async def test_expired_queue_and_lease_stay_terminal_across_polls(linked, monkeypatch):
    owner, companion = linked
    now = datetime.now(UTC)
    monkeypatch.setattr(desktop, "utcnow", lambda: now)
    first = await _queue(owner)
    now += desktop.QUEUE_TTL + timedelta(seconds=1)
    assert await _claim(companion) is None
    assert (await owner.get(PATH)).json()["tasks"][0]["reason"] == "queue_expired"
    second = await _queue(owner)
    task = await _claim(companion)
    assert task["id"] == second["id"]
    assert await _report(companion, task, "opened")
    now += timedelta(seconds=desktop.LEASE_SECONDS + 1)
    assert not await _report(companion, task, "written")
    status = (await owner.get(PATH)).json()
    assert status["device"]["state"] == "offline" and status["paused"]
    expired, earlier = status["tasks"]
    assert earlier["id"] == first["id"]
    assert (expired["state"], expired["reason"]) == ("interrupted", "lease_expired")
    assert await _claim(companion) is None


async def test_pairing_requires_owner_and_both_credentials_have_one_purpose(logged_in: TestEnv):
    owner = logged_in.client
    assert (await owner.get(PATH)).json() == {"device": None, "paused": True, "tasks": []}
    assert (await owner.put(
        f"{PATH}/pair", json={"name": "peer"}, headers={"Origin": "https://example.invalid"},
    )).status_code == 403
    token = await _pair(owner)
    assert (await owner.post(POLL, json={"ready": True})).status_code == 401
    assert token not in (await owner.get(PATH)).text
    not_ready = await owner.put(f"{PATH}/control", json={"paused": False})
    assert not_ready.status_code == 409 and not_ready.json()["error"]["detail"] == "desktop_not_ready"
    async with _client(logged_in, token) as companion:
        assert (await companion.get(PATH)).status_code == 401
        assert await _claim(companion) is None
        assert (await owner.get(PATH)).json()["device"]["state"] == "ready"
        assert await _pair(owner) != token
        assert (await companion.post(POLL, json={"ready": True})).status_code == 401

    async with session_scope() as session:
        await IdentityService(session).create_user(
            username="desk-member", password="test-member-password", role="member",
        )
    async with _client(logged_in) as member:
        login = await member.post("/v1/auth/login", json={
            "username": "desk-member", "password": "test-member-password",
        })
        assert login.status_code == 200
        member.headers["Origin"] = "http://testserver"
        assert (await member.get(PATH)).status_code == 403
        assert (await member.put(f"{PATH}/pair", json={"name": "peer"})).status_code == 403
        assert (await member.post(f"{PATH}/tasks", json={
            "request_id": str(uuid4()), "text": "member note",
        })).status_code == 403


async def test_unpair_revokes_running_task_and_preserves_evidence(linked):
    owner, companion = linked
    await _queue(owner)
    task = await _claim(companion)
    assert await _report(companion, task, "opened")
    assert (await owner.delete(f"{PATH}/pair")).status_code == 204
    status = (await owner.get(PATH)).json()
    assert status["device"] is None and status["paused"]
    history, = status["tasks"]
    assert (history["state"], history["step"], history["reason"]) == (
        "interrupted", "opened", "pairing_changed",
    )
    assert (await companion.post(POLL, json={"ready": True})).status_code == 401
