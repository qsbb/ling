"""窗口摘要的后台重写：**只换措辞，不动覆盖范围**。

主链上的 `_rollup_summary` 做确定性压缩（逐条截取），这个处理器在后台
用 `memory.extract` 职责把它换成更连贯的版本。分工是刻意的：
每轮调模型做摘要会让首字延迟和费用都翻倍，而摘要只需要"接上话题"。

三条不变式，本文件逐条钉住：

1. **不推进 `through_sequence`**——推进了会跳过没进摘要的消息
2. **拿不到就保留原摘要**——丢旧摘要换空话比不改更糟
3. **重写后长度不能明显缩水**——模型很容易压成一句"聊了聊近况"，
   那等于悄悄丢信息
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select

from ling.infra.db import session_scope
from ling.infra.models import WindowContext
from ling.ports.model import ModelChunk, ModelError, ModelErrorKind
from ling.worker import enqueue_window_summaries, handle_window_summary
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ORIGINAL = "\n".join(
    [
        "用户：我叫阿岚，在成都做设计。",
        "ling：记住了。",
        "用户：下周三要去体检，早上空腹。",
        "ling：要不要我提前提醒你？",
        "用户：好，前一天晚上提醒。",
    ]
)


class _StubRouter:
    def __init__(
        self, *, text: str = "", error: ModelError | None = None, seen: list | None = None
    ) -> None:
        self._text = text
        self._error = error
        self.seen = seen if seen is not None else []

    async def stream(self, effective, *, profile, messages, **kwargs) -> AsyncIterator:  # noqa: ANN001, ANN003
        from ling.models.router import StreamOutcome

        self.seen.append({"profile": profile, "messages": messages})
        if self._error is not None:
            raise self._error
        if self._text:
            yield ModelChunk(delta=self._text), StreamOutcome()
        yield ModelChunk(done=True), StreamOutcome(text=self._text)


async def _seed(window_id: str, *, summary: str = ORIGINAL) -> None:
    async with session_scope() as session:
        row = WindowContext(
            id=f"wctx_{window_id}",
            window_id=window_id,
            summary=summary,
            summary_through_sequence=42,
            summary_message_count=17,
            open_questions=[],
        )
        session.add(row)


async def _read(window_id: str) -> WindowContext:
    async with session_scope() as session:
        return (
            await session.execute(
                select(WindowContext).where(WindowContext.window_id == window_id)
            )
        ).scalar_one()


@pytest.fixture()
def router(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """替换模块级路由。处理器直接读全局，没有别的注入点。"""
    import ling.worker as worker

    def install(stub: _StubRouter) -> _StubRouter:
        monkeypatch.setattr(worker, "_MEMORY_ROUTER", stub)
        return stub

    return install


# ---------------------------------------------------------------- 正常路径


async def test_rewrite_keeps_through_and_count(logged_in: TestEnv, router) -> None:  # noqa: ANN001, ARG001
    """重写成功时**只**换 summary，覆盖范围与条数原样保留。

    这条是核心：`through_sequence` 决定"摘要覆盖到第几条"。
    推进了就等于宣称"这些消息已经被压缩进摘要"，而实际上模型没见过它们。
    """
    await _seed("win_sum_a")
    rewrite = "用户阿岚住在成都，做后端工作；他说下周三要去体检，早上要空腹；他还让我在前一天晚上提醒他这件事。"
    router(_StubRouter(text=rewrite))

    await handle_window_summary({"window_id": "win_sum_a"})

    row = await _read("win_sum_a")
    assert row.summary == rewrite
    assert row.summary_through_sequence == 42, "覆盖范围被改动了"
    assert row.summary_message_count == 17, "消息计数被改动了"


async def test_uses_memory_extract_profile(logged_in: TestEnv, router) -> None:  # noqa: ANN001, ARG001
    """必须走 memory.extract 职责，不跨职责回退。

    用 dialogue 模型顺手重写摘要，会让对话预算被摘要任务吃掉，
    而且摘要质量与对话质量是两回事。
    """
    await _seed("win_sum_b")
    stub = router(_StubRouter(text="阿岚在成都做设计，下周三要体检，希望前一晚被提醒这件事。"))
    await handle_window_summary({"window_id": "win_sum_b"})
    assert stub.seen, "没有调用模型"
    assert stub.seen[0]["profile"] == "memory.extract"


# ---------------------------------------------------------------- 保留原摘要


@pytest.mark.parametrize(
    ("case", "reply"),
    [
        ("空回复", ""),
        ("压成一句话", "聊了聊近况。"),
    ],
)
async def test_degenerate_rewrite_keeps_original(
    logged_in: TestEnv, router, case: str, reply: str  # noqa: ANN001, ARG002
) -> None:
    """退化输出（空、或压成一句"聊了聊近况"）一律不采纳。

    这两种其实是**同一道门**：长度守卫。空串是"缩水到 0%"的极端情况。
    早先我把它拆成两条测试，删掉 `if not rewritten` 分支时"空回复"那条
    仍然绿——因为它本来就由长度守卫挡住，那条测试验证的不是它声称的规则，
    是假的。合并成一条参数化用例，测的才是真实的那道门。
    """
    await _seed("win_sum_d")
    router(_StubRouter(text=reply))
    await handle_window_summary({"window_id": "win_sum_d"})
    row = await _read("win_sum_d")
    assert row.summary == ORIGINAL, f"{case}把原摘要冲掉了"
    assert row.summary_through_sequence == 42


async def test_model_error_keeps_original(logged_in: TestEnv, router) -> None:  # noqa: ANN001, ARG001
    await _seed("win_sum_e")
    router(
        _StubRouter(
            error=ModelError(ModelErrorKind.unsupported, "职责 memory.extract 尚未绑定模型")
        )
    )
    await handle_window_summary({"window_id": "win_sum_e"})
    row = await _read("win_sum_e")
    assert row.summary == ORIGINAL


async def test_no_router_keeps_original(logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG001
    import ling.worker as worker

    await _seed("win_sum_f")
    monkeypatch.setattr(worker, "_MEMORY_ROUTER", None)
    await handle_window_summary({"window_id": "win_sum_f"})
    row = await _read("win_sum_f")
    assert row.summary == ORIGINAL


# ---------------------------------------------------------------- 边界


async def test_missing_summary_is_noop(logged_in: TestEnv, router) -> None:  # noqa: ANN001, ARG001
    await _seed("win_sum_g", summary="   ")
    stub = router(_StubRouter(text="随便改写点什么内容，长度要够长才可能被采纳。"))
    await handle_window_summary({"window_id": "win_sum_g"})
    assert stub.seen == [], "没有原文时不该调模型"
    row = await _read("win_sum_g")
    assert row.summary.strip() == ""


async def test_unknown_window_is_noop(logged_in: TestEnv, router) -> None:  # noqa: ANN001, ARG001
    stub = router(_StubRouter(text="x"))
    await handle_window_summary({"window_id": "win_does_not_exist"})
    assert stub.seen == []


async def test_missing_window_id_is_noop(logged_in: TestEnv, router) -> None:  # noqa: ANN001, ARG001
    stub = router(_StubRouter(text="x"))
    await handle_window_summary({})
    assert stub.seen == []


# ---------------------------------------------------------------- 调度来源


async def _outbox_topics() -> list[str]:
    from ling.infra.models import OutboxEvent

    async with session_scope() as session:
        rows = (
            (await session.execute(select(OutboxEvent))).scalars().all()
        )
        return [r.topic for r in rows if r.topic == "window.summary"]


async def test_scheduler_enqueues_recent_windows(logged_in: TestEnv) -> None:  # noqa: ARG001
    """handler 写好了还必须有**调度来源**，否则等于不存在。

    `memory.archive` 已经犯过一次这个错（注释里记着）。这里钉住。
    """
    await _seed("win_sum_sched_a")
    created = await enqueue_window_summaries()
    assert created >= 1
    assert len(await _outbox_topics()) >= 1


async def test_scheduler_is_idempotent_per_day(logged_in: TestEnv) -> None:  # noqa: ARG001
    """同一天同一个窗口只投一次——重复投递只是白花钱。"""
    await _seed("win_sum_sched_b")
    await enqueue_window_summaries()
    before = len(await _outbox_topics())
    await enqueue_window_summaries()
    assert len(await _outbox_topics()) == before, "重复投递了同一批任务"


async def test_scheduler_skips_stale_and_empty(logged_in: TestEnv) -> None:  # noqa: ARG001
    """只挑 24 小时内更新过、且确实有摘要的窗口。

    历史摘要不会再变，反复重写只是白花钱；没有摘要的更不该进队列。
    """
    from datetime import UTC, datetime, timedelta

    from ling.infra.models import WindowContext

    await _seed("win_sum_stale", summary="很久以前的摘要内容，长度够长够长够长。")
    await _seed("win_sum_empty", summary="   ")
    async with session_scope() as session:
        row = (
            await session.execute(
                select(WindowContext).where(WindowContext.window_id == "win_sum_stale")
            )
        ).scalar_one()
        row.updated_at = datetime.now(UTC) - timedelta(days=3)

    await enqueue_window_summaries()

    from ling.infra.models import OutboxEvent

    async with session_scope() as session:
        ids = [
            r.id
            for r in (await session.execute(select(OutboxEvent))).scalars().all()
            if r.topic == "window.summary"
        ]
    assert not any("win_sum_stale" in i for i in ids), "过期窗口被投递了"
    assert not any("win_sum_empty" in i for i in ids), "空摘要窗口被投递了"


def test_handler_and_scheduler_agree_on_topic() -> None:
    """调度投的 topic 必须有处理器接。

    这两个字符串在两处各写一遍，是最典型的"改名改一半"现场：
    改了 handler 的 topic 而没改调度，任务投出去没人接，静默堆积。
    """
    from ling.worker import HANDLERS

    assert "window.summary" in HANDLERS
