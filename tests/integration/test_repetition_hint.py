"""重复提醒：把"你最近老用同一句"的**事实**告诉她，换不换由她定。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.w0_web

TAIL = "街角新摊子香味飘过来啦，去尝尝不？"


async def test_hint_appears_when_she_repeats_herself(logged_in) -> None:
    """连续几次用同一句收尾 → 上下文里出现这条事实。

    实测背景：无论问什么，回复都以「朋友的朋友就是我的朋友」
    「街角新摊子…」收尾，因为人格文档里的**举例**被模型当成了模板。
    """
    from ling.infra.db import session_scope
    from ling.infra.models import Message
    from ling.infra.security import new_id

    session_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "重复"})
    ).json()["session_id"]

    async with session_scope() as session:
        for seq in range(1, 5):
            session.add(
                Message(
                    id=new_id("msg"),
                    window_id=session_id,
                    role="actor",
                    text=f"第{seq}句不同的开场。\n{TAIL}",
                    state="completed",
                    sequence=seq,
                    attention_state="seen",
                )
            )
        await session.commit()

    from ling.capabilities.repetition import repetition_hint

    async with session_scope() as session:
        from sqlalchemy import select

        rows = (
            await session.execute(
                select(Message.text)
                .where(Message.window_id == session_id, Message.role == "actor")
                .order_by(Message.sequence.desc())
                .limit(5)
            )
        ).all()
        texts = [r[0] for r in reversed(rows)]

    hint = repetition_hint(texts)
    assert hint is not None
    assert TAIL in hint
    assert "换不换" in hint, "提示里必须把选择权留给她"


async def test_no_hint_for_varied_replies() -> None:
    from ling.capabilities.repetition import repetition_hint

    assert repetition_hint(["读故事呢。", "在散步呀。", "刚睡醒哦。"]) is None


async def test_short_tails_are_not_flagged() -> None:
    """"好呀。"这种短句重复是正常修辞，不该提示。"""
    from ling.capabilities.repetition import repetition_hint

    assert repetition_hint(["好呀。"] * 5) is None


async def test_hint_actually_reaches_the_prompt(logged_in) -> None:
    """接线：提示必须真的进到**发给模型的 messages** 里。

    只测 helper 不算测接线——早期版本读了一个还没赋值的属性，helper
    测试全绿而提示永远是 None。这条从编译器输出上验证。
    """
    from sqlalchemy import select

    from ling.infra.db import session_scope
    from ling.infra.models import Message, Window
    from ling.infra.security import new_id
    from ling.kernel.context import ContextCompiler

    session_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "接线"})
    ).json()["session_id"]
    async with session_scope() as session:
        await session.execute(
            select(Window).where(Window.id == session_id)
        )
        for seq in range(1, 5):
            session.add(
                Message(
                    id=new_id("msg"),
                    window_id=session_id,
                    role="actor",
                    text=f"第{seq}种开场。\n{TAIL}",
                    state="completed",
                    sequence=seq,
                    attention_state="seen",
                )
            )
        await session.commit()

    async with session_scope() as session:
        compiled = await ContextCompiler(session).compile(
            subject_id=logged_in.subject_id,
            window_id=session_id,
            user_text="在吗",
        )
    joined = "\n".join(m.content for m in compiled.messages)
    assert "收尾" in joined, "重复提醒没有进到提示里"
    assert TAIL in joined


async def test_paraphrased_catchphrase_is_still_caught() -> None:
    """换了措辞的同一条口头禅也要抓到。

    实测她不会原样重复，而是变着说法用同一条口头禅：
    「朋友的朋友就是我的朋友。」/「朋友的朋友就是朋友，别客气呀。」
    整句比对永远不触发——所以需要短语级检测 + 扩到最长公共子串。
    """
    from ling.capabilities.repetition import repetition_hint

    paraphrased = [
        "这天气适合溜达。\n朋友的朋友就是我的朋友，有空约饭呗？",
        "正读冒险故事集呢。\n朋友的朋友就是我的朋友。",
        "晚饭还没吃呢。\n朋友的朋友就是朋友，别客气呀。",
    ]
    hint = repetition_hint(paraphrased)
    assert hint is not None, "换了措辞就漏了"
    assert "朋友的朋友就是" in hint
