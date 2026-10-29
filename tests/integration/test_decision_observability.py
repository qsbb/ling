"""验收：**只看日志**能不能回答"她为什么没回这条消息"。

这是日志体系的判定标准，不是"有没有打日志"。所以用例不看返回值，
只抓日志记录，看能不能凭 event_name + skip_code 还原结论。
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import update

from ling.infra.models import Window
from ling.kernel.decision import SKIP_CODES

pytestmark = pytest.mark.w0_web


async def _group_window(env) -> str:  # noqa: ANN001
    created = await env.client.post("/v1/sessions", json={"title": "决策可观测性"})
    session_id = created.json()["session_id"]
    from ling.infra.db import session_scope

    async with session_scope() as session:
        await session.execute(
            update(Window).where(Window.id == session_id).values(visibility="group_scope")
        )
        await session.commit()
    return session_id


async def _send(client, session_id: str, text: str) -> None:  # noqa: ANN001
    """走真实的 turn 端点，并把 SSE 读完（不读完请求不算完成）。"""
    import uuid

    response = await client.post(
        f"/v1/sessions/{session_id}/turns",
        json={"client_turn_id": str(uuid.uuid4()), "text": text},
    )
    assert response.status_code == 200, response.text


def _decision_events(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records
        if getattr(r, "event_name", "") == "reply_decision_made"
    ]


async def test_silence_is_explained_by_logs_alone(logged_in, caplog) -> None:
    """群聊普通聊天 → 日志里能直接读出"因为没点名她所以不回"。"""
    session_id = await _group_window(logged_in)

    with caplog.at_level(logging.INFO, logger="ling.kernel.bridge"):
        # 用一句确定的陈述（无问号/疑问词），落到"未点名"这一档
        await _send(logged_in.client, session_id, "今天中午吃了面")

    events = _decision_events(caplog)
    assert events, [r.getMessage() for r in caplog.records]

    last = events[-1].attributes
    assert last["decision"] == "silence"
    # 关键：机器可读的原因落在受控枚举里，而不是一句自由文本
    assert last["skip_code"] in SKIP_CODES
    assert last["skip_code"] in {
        "group_not_addressed",
        "group_question_not_addressed",
        "group_no_meaningful_text",
        "model_chose_silence",
    }
    assert last["is_group"] is True
    # 同时保留给人看的措辞
    assert last["reason"]


async def test_mention_is_logged_with_its_own_code(logged_in, caplog) -> None:
    """被点名 → 另一个 skip_code，和"没点名"能区分开。"""
    session_id = await _group_window(logged_in)

    with caplog.at_level(logging.INFO, logger="ling.kernel.bridge"):
        await _send(logged_in.client, session_id, "ling 你在吗")

    events = _decision_events(caplog)
    assert events
    last = events[-1].attributes
    assert last["decision"] != "silence"
    assert last["skip_code"] in SKIP_CODES
    assert last["skip_code"] != "group_not_addressed"


async def test_every_skip_decision_uses_a_controlled_code(logged_in, caplog) -> None:
    """所有沉默理由都必须落在枚举里——自由文本没法聚合。

    这条是防止将来有人加分支时随手写一句 reason 就返回。
    """
    session_id = await _group_window(logged_in)
    cases = ["今天中午吃了面", "😀😀😀", "ling 你在吗"]

    with caplog.at_level(logging.INFO, logger="ling.kernel.bridge"):
        for text in cases:
            await _send(logged_in.client, session_id, text)

    events = _decision_events(caplog)
    assert len(events) >= len(cases)
    for record in events:
        code = record.attributes.get("skip_code")
        assert code in SKIP_CODES, f"未受控的 skip_code: {code!r}"
