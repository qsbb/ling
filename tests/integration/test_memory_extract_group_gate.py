"""群聊场景下的敏感槽位闸门：`is_group` 必须真的生效。

回归的缺陷：`memory.extract` 的生产者（`kernel/turn.py` 入队处）只写
`source_visibility`，从来不写 `is_group`；handler 却读
`bool(payload.get("is_group"))`，于是恒为 False。后果是
`domains/memory/extract.py` 的群聊闸门永不触发——
群里说出的 boundary/health/family/birthday 会被当成用户档案沉淀下来，
`build_prompt` 里的群聊附加约束也永不追加。

这里不构造 payload 字面量，而是按生产者的做法**从真实窗口行取
visibility**，再交给 handler：这样测的是整条派生链，而不是某次手写的字段。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select, update

from ling.infra.db import session_scope
from ling.infra.models import MemoryEpisode, MemoryRow, Window
from ling.ports.model import ModelChunk
from ling.worker import handle_memory_extract
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

# 群聊敏感槽位之一。值必须能在原话里找到依据（闸门的 grounded 检查），
# 否则会被"疑似臆造"拒掉，测试就测不到 is_group 这一条了。
BOUNDARY_TEXT = "别在我面前提我前任"
BOUNDARY_VALUE = "提我前任"


class _StubRouter:
    """固定返回一条 boundary 候选的假模型。"""

    def __init__(self, text: str) -> None:
        self._text = text
        self.seen: list[dict] = []

    async def stream(self, effective, *, profile, messages, **kwargs) -> AsyncIterator:  # noqa: ANN001, ANN003
        from ling.models.router import StreamOutcome

        self.seen.append({"profile": profile, "messages": messages})
        yield ModelChunk(delta=self._text), StreamOutcome()
        yield ModelChunk(done=True), StreamOutcome(text=self._text)


@pytest.fixture()
def router(monkeypatch: pytest.MonkeyPatch, web_env: TestEnv):  # noqa: ANN201
    """替换 worker 的模块级路由，并让 worker 用测试环境配置（别读开发机环境）。"""
    import ling.worker as worker

    # `_extract_candidates` 里 `get_settings()` 是模块级引用：不替换的话
    # SecretBox 会按开发机的 data_dir 去写根密钥文件。
    monkeypatch.setattr(worker, "get_settings", lambda: web_env.settings)

    def install() -> _StubRouter:
        stub = _StubRouter(
            json.dumps(
                [{"slot": "boundary", "value": BOUNDARY_VALUE, "confidence": 0.9}],
                ensure_ascii=False,
            )
        )
        monkeypatch.setattr(worker, "_MEMORY_ROUTER", stub)
        return stub

    return install


async def _window(env: TestEnv, *, visibility: str) -> tuple[str, str]:
    """建窗口并设置可见范围；返回 (window_id, 该窗口的 visibility)。"""
    session_id = (
        await env.client.post("/v1/sessions", json={"title": f"gate-{visibility}"})
    ).json()["session_id"]
    async with session_scope() as session:
        await session.execute(
            update(Window).where(Window.id == session_id).values(visibility=visibility)
        )
    async with session_scope() as session:
        row = await session.get(Window, session_id)
        return session_id, row.visibility


def _payload(env: TestEnv, window_id: str, source_visibility: str) -> dict:
    """与 `kernel/turn.py` 入队处同形——**没有 is_group**，只有 source_visibility。"""
    return {
        "subject_id": env.subject_id,
        "window_id": window_id,
        "turn_id": f"turn_{uuid.uuid4().hex[:10]}",
        "user_text": BOUNDARY_TEXT,
        "actor_text": "好，我记住了",
        "source_visibility": source_visibility,
        "attempt": 1,
        "source_account": None,
    }


async def _assertions(turn_id: str) -> list[MemoryRow]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(MemoryRow).where(
                        MemoryRow.turn_id == turn_id, MemoryRow.kind == "assertion"
                    )
                )
            ).scalars()
        )


async def _episodes(turn_id: str) -> list[MemoryEpisode]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(MemoryEpisode).where(MemoryEpisode.turn_id == turn_id)
                )
            ).scalars()
        )


async def test_group_window_rejects_boundary_slot(
    logged_in: TestEnv, router  # noqa: ANN001
) -> None:
    """群聊窗口里提炼出 boundary → 必须被闸门拒绝，不能沉淀成长期记忆。"""
    window_id, visibility = await _window(logged_in, visibility="group_scope")
    assert visibility == "group_scope"
    stub = router()
    payload = _payload(logged_in, window_id, visibility)

    await handle_memory_extract(payload)

    # 提炼链路确实跑了（否则"没有断言"可能只是模型没被调用）
    assert stub.seen, "模型提炼没有被调用，这条测试失去意义"
    assert await _episodes(payload["turn_id"]), "情节记忆应写入（闸门只拦断言）"

    rows = await _assertions(payload["turn_id"])
    assert not rows, (
        "群聊里的 boundary 被沉淀成了长期记忆："
        f"{[(r.slot, r.text) for r in rows]}"
    )

    # 群聊语义也要传进提示词：附加约束此前同样永不追加
    prompt = stub.seen[-1]["messages"][-1].content
    assert "群聊" in prompt, "群聊上下文没有传进提炼提示词"


async def test_private_window_allows_boundary_slot(
    logged_in: TestEnv, router  # noqa: ANN001
) -> None:
    """私聊窗口里同一条 boundary → 允许写入（闸门不能把私聊也一起拦掉）。"""
    window_id, visibility = await _window(logged_in, visibility="subject_private")
    stub = router()
    payload = _payload(logged_in, window_id, visibility)

    await handle_memory_extract(payload)

    assert stub.seen, "模型提炼没有被调用，这条测试失去意义"
    rows = await _assertions(payload["turn_id"])
    assert any(r.slot == "boundary" for r in rows), (
        f"私聊里的 boundary 没有被记住：{[(r.slot, r.text) for r in rows]}"
    )
    assert all(r.visibility == "subject_private" for r in rows)

    prompt = stub.seen[-1]["messages"][-1].content
    assert "群聊" not in prompt, "私聊被误标成群聊"
