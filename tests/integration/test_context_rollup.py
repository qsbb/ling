"""长会话滚动摘要：早期消息不能无声消失。

真实缺陷（实测复现）：`messages_for_prompt(limit=16)` 只取最近 16 条，
**更早的从来没被读出来**，所以既进不了上下文、也永远压不进摘要。
短消息不撞 token 预算时尤其明显——用户连着聊二十来句，
再追问早期的细节，"刚才聊的事它都忘了"。

而不是"摘要没做好"，是**那段内容根本没进过任何一环**。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from ling.infra.db import session_scope
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


def _overflow_messages(prefix: str, *, each: int = 40) -> list[str]:
    """生成**足够撞穿当前预算**的消息。

    不能写死条数：预算从 1600 调到 20000 之后，"12 条长消息"根本撞不到上限，
    于是这几个测试会静默变成空跑（摘要永远不生成，断言就看运气）。
    按 `HISTORY_TOKEN_BUDGET` 算需要多少条，预算怎么调测试都成立。
    """
    from ling.kernel.context import HISTORY_TOKEN_BUDGET, estimate_tokens

    body = f"{prefix}内容要够长才能撞到预算上限。" * each
    per = max(1, estimate_tokens(body))
    count = (HISTORY_TOKEN_BUDGET // per) + 3  # +3 确保溢出
    return [f"第{i}条{body}" for i in range(count)]


async def _seed_messages(
    env: TestEnv, window_id: str, texts: list[str], *, reply: str = ""
) -> None:
    """直接写库造消息。

    滚动摘要的关键是"消息超出原文窗口后去哪了"，不需要每轮都走完整模型链路——
    每轮 HTTP 大约 1~5s，用 HTTP 造 40 条会把整套测试从 145s 拖到 415s。
    这里只跳过模型调用，被测的编译/摘要代码路径完全一致。
    """
    import uuid

    from ling.infra.models import Message

    async with session_scope() as session:
        base = 0
        for i, text in enumerate(texts):
            session.add(
                Message(
                    id=f"msg_roll_{uuid.uuid4().hex[:12]}",
                    window_id=window_id,
                    turn_id=f"turn_roll_{uuid.uuid4().hex[:8]}",
                    role="user",
                    text=text,
                    state="completed",
                    sequence=base + i * 2 + 1,
                    origin="user_statement",
                )
            )
            session.add(
                Message(
                    id=f"msg_roll_{uuid.uuid4().hex[:12]}",
                    window_id=window_id,
                    turn_id=f"turn_roll_{uuid.uuid4().hex[:8]}",
                    role="actor",
                    text=reply or f"回应第{i}条",
                    state="completed",
                    sequence=base + i * 2 + 2,
                    origin="model_inference",
                )
            )


async def _summary(env: TestEnv, window_id: str) -> tuple[str, int, int]:
    from ling.infra.models import WindowContext

    async with session_scope() as session:
        # 主键是 id，不是 window_id（按主键取会永远拿到 None）
        row = (
            await session.execute(
                select(WindowContext).where(WindowContext.window_id == window_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return "", 0, 0
        return row.summary or "", int(row.summary_through_sequence or 0), int(
            row.summary_message_count or 0
        )


async def _compile(env: TestEnv, window_id: str, text: str) -> tuple[int, dict]:
    from ling.kernel.context import ContextCompiler

    async with session_scope() as session:
        compiled = await ContextCompiler(session).compile(
            subject_id=env.subject_id,
            window_id=window_id,
            user_text=text,
            current_turn_id="turn_rollup_probe",
        )
    return len(compiled.messages), compiled.meta


async def test_short_history_is_kept_verbatim(logged_in: TestEnv) -> None:
    """没超 token 预算时，早期消息应**全部保留原文**。

    这是零信息损失的正确行为——不该为了"有个摘要"而把还能装下的原文压掉。
    回归的是"条数上限截断"：之前 limit=16/20，第 17/21 条之后的直接消失。
    """
    window_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "rollup"})
    ).json()["session_id"]

    await _seed_messages(logged_in, window_id, [f"第{i}条短消息" for i in range(24)])

    from ling.kernel.context import ContextCompiler

    async def _blob(turn: str, text: str) -> str:
        async with session_scope() as session:
            compiled = await ContextCompiler(session).compile(
                subject_id=logged_in.subject_id,
                window_id=window_id,
                user_text=text,
                current_turn_id=turn,
            )
        return "\n".join(m.content for m in compiled.messages)

    await _blob("turn_rollup_probe3a", "第0条是什么")
    # 摘要在本轮末尾才写入，下一轮才可见
    blob = await _blob("turn_rollup_probe3b", "第0条是什么")
    # 关键回归：第 0 条要么在原文、要么在摘要里，不能两头都没有
    assert "第0条短消息" in blob, (
        "最早的一条既不在原文也不在摘要里——被条数上限直接截掉了"
    )
    assert "第23条短消息" in blob


async def test_overflow_goes_into_summary(logged_in: TestEnv) -> None:
    """超出 token 预算时，被挤出原文的消息必须进摘要，且仍可被读到。"""
    window_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "rollup-overflow"})
    ).json()["session_id"]

    await _seed_messages(
        logged_in,
        window_id,
        _overflow_messages("比较长的消息，用来把上下文预算撑满。"),
    )

    await _compile(logged_in, window_id, "刚才都聊了什么")
    summary, through, count = await _summary(logged_in, window_id)
    assert summary, "超预算后没有生成摘要"
    assert through > 0, "summary_through_sequence 没有推进"
    assert count > 0, "summary_message_count 没有记录"

    # 再编译一次，被挤出的内容必须能通过摘要读到
    from ling.kernel.context import ContextCompiler

    async with session_scope() as session:
        compiled = await ContextCompiler(session).compile(
            subject_id=logged_in.subject_id,
            window_id=window_id,
            user_text="最早聊的是什么",
            current_turn_id="turn_rollup_probe4",
        )
    blob = "\n".join(m.content for m in compiled.messages)
    assert "较早的对话摘要" in blob, "摘要没有进入上下文"
    assert "第0条比较长的消息" in blob, "最早的内容既不在原文也不在摘要里"


async def test_summary_advances_monotonically(logged_in: TestEnv) -> None:
    """多次编译不能重复把同一批消息压进摘要（否则摘要会被重复内容撑爆）。"""
    window_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "rollup2"})
    ).json()["session_id"]
    await _seed_messages(
        logged_in,
        window_id,
        _overflow_messages("消息，"),
    )

    await _compile(logged_in, window_id, "继续")
    _, through_first, count_first = await _summary(logged_in, window_id)
    await _compile(logged_in, window_id, "再继续")
    _, through_second, count_second = await _summary(logged_in, window_id)

    assert through_second >= through_first, "摘要覆盖位置倒退了"
    assert count_second >= count_first
    # 重复压缩同一批会让计数暴涨（两倍以上说明重复计入）
    if through_first > 0:
        assert count_second < count_first * 2 + 10, (
            f"疑似重复压缩：count {count_first} → {count_second}"
        )


async def test_summary_visible_in_next_context(logged_in: TestEnv) -> None:
    """摘要必须真的进入下一轮上下文，否则等于没做。"""
    window_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "rollup3"})
    ).json()["session_id"]
    await _seed_messages(
        logged_in,
        window_id,
        _overflow_messages("较早的内容，需要足够长才会触发滚动摘要。"),
    )

    await _compile(logged_in, window_id, "继续")
    summary, _, _ = await _summary(logged_in, window_id)
    assert summary

    from ling.kernel.context import ContextCompiler

    async with session_scope() as session:
        compiled = await ContextCompiler(session).compile(
            subject_id=logged_in.subject_id,
            window_id=window_id,
            user_text="刚才说到哪了",
            current_turn_id="turn_rollup_probe2",
        )
    blob = "\n".join(m.content for m in compiled.messages)
    assert "较早的对话摘要" in blob, "摘要没有进入下一轮上下文"
    assert "较早的内容" in blob, "摘要正文没有进入上下文"
