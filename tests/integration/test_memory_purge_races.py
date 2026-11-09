"""记忆真删：并发与失败路径的竞态回归（真实 PostgreSQL，两个独立 session）。

为什么必须真库、真并发：这两个缺陷都是**事务语义**问题，sqlite 上没有行锁，
"读出来再赋值"与"原子递增"看起来一样正确；失败状态能否落库也取决于调用方
是否回滚。用假库测出来的"能过"不代表线上能过。

覆盖：

1. **代次竞态**：两个并发 `request_purge`。原实现是 `session.get()` 之后
   `forget_generation += 1`，两边都读到 0、都写 1，结果代次只 +1。而被吃掉的
   那一次请求对应的写入**不会被作废**（代次对得上，栅栏放行）。
2. **失败状态落库**：清理 SQL 失败时，墓碑在库里必须是 `failed`（用新 session
   重读），且 `error_code` 非空。
3. **历史事件**：`subject_id` 列为 NULL 的 pending / done 事件也必须被取消与
   脱敏（真实 PostgreSQL 的 JSON 取文本表达式与 sqlite 不同，这里顺带钉住
   `payload["subject_id"].as_string()` 在 PG 上确实编译成 `->>` 并命中）。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from ling.domains.memory.purge import MemoryPurgeError, purge_subject, request_purge
from ling.infra.db import session_scope
from ling.infra.models import (
    MemoryPurgeTombstone,
    MemoryRow,
    OutboxEvent,
    Subject,
)
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

CANARY = "我对花生过敏，千万别给我吃"


async def _seed(env: TestEnv) -> dict[str, str]:
    subject_id = env.subject_id
    window_id = f"win_{uuid.uuid4().hex[:10]}"
    async with session_scope() as session:
        session.add(
            MemoryRow(
                id=f"mem_{uuid.uuid4().hex[:10]}",
                subject_id=subject_id,
                kind="assertion",
                text=CANARY,
                origin="user_statement",
                visibility="subject_private",
                confidence=0.9,
                slot="health",
                value="花生过敏",
            )
        )
    return {"subject_id": subject_id, "window_id": window_id}


async def _generation(subject_id: str) -> int:
    async with session_scope() as session:
        value = await session.scalar(
            select(Subject.forget_generation).where(Subject.id == subject_id)
        )
    return int(value or 0)


# ------------------------------------------------------ 缺陷 3：并发删除的竞态


async def test_two_concurrent_purge_requests_bump_the_generation_twice(
    logged_in: TestEnv,
) -> None:
    """两个并发 `request_purge`：代次必须是 **2**，不是 1。

    这是"丢失更新"的经典形态：两边都读到 generation=0，各自算出 1 写回。
    丢掉的那一次请求不是"少一个数字"——它意味着**第二次请求之前入队的写入
    不会被作废**（它们带代次 1，而库里也是 1，栅栏放行），用户以为删了，
    内容却还能被写回来。

    这里用两个独立 session 真并发（`asyncio.gather`），不是顺序调用。
    """
    ids = await _seed(logged_in)
    subject_id = ids["subject_id"]
    assert await _generation(subject_id) == 0

    async def one_request() -> int:
        async with session_scope() as session:
            request = await request_purge(session, subject_id=subject_id)
            return request.generation

    generations = sorted(await asyncio.gather(one_request(), one_request()))

    assert generations == [1, 2], (
        f"两次并发请求拿到的代次是 {generations}，说明有一边读了旧值——"
        "丢失的那次请求对应的写入不会被作废"
    )
    assert await _generation(subject_id) == 2, (
        "库里的代次只 +1：两个并发请求发生了丢失更新"
    )


async def test_many_concurrent_purge_requests_never_lose_a_bump(
    logged_in: TestEnv,
) -> None:
    """N 个并发请求：代次必须正好 +N（每一个都不能丢）。

    比两条更狠一点：任何"读-算-写"的残留都会在这里暴露成计数不足。
    """
    ids = await _seed(logged_in)
    subject_id = ids["subject_id"]
    rounds = 5

    async def one_request() -> None:
        async with session_scope() as session:
            await request_purge(session, subject_id=subject_id)

    await asyncio.gather(*(one_request() for _ in range(rounds)))

    assert await _generation(subject_id) == rounds, (
        f"{rounds} 次并发删除只把代次加到 {await _generation(subject_id)}"
    )


async def test_concurrent_requests_write_one_tombstone_each(logged_in: TestEnv) -> None:
    """每次请求都要有自己的墓碑：否则前端查不到其中一次的进度。"""
    ids = await _seed(logged_in)
    subject_id = ids["subject_id"]

    async def one_request() -> str:
        async with session_scope() as session:
            request = await request_purge(session, subject_id=subject_id)
            return request.tombstone_id

    ids_written = await asyncio.gather(one_request(), one_request())
    assert len(set(ids_written)) == 2

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(MemoryPurgeTombstone).where(
                        MemoryPurgeTombstone.subject_id == subject_id
                    )
                )
            ).scalars()
        )
    assert len(rows) == 2
    assert {row.generation for row in rows} == {1, 2}


# ------------------------------------------------- 缺陷 2：失败状态必须落库


async def test_failed_state_is_committed_and_visible_from_a_new_session(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清理 SQL 失败后，**新 session** 读到的墓碑必须是 `failed` 且带原因码。

    原实现在 except 里写 `failed` 然后抛异常，调用方 `session_scope` 回滚，
    写入被丢掉：墓碑停在 `running`，`memory.purge.sweep` 扫不到 `failed`，
    用户看到的是"一直清理中"。

    用新 session 重读是关键：只有它读到 `failed`，才证明状态真的提交了。
    """
    ids = await _seed(logged_in)
    async with session_scope() as session:
        request = await request_purge(session, subject_id=ids["subject_id"])

    def _boom(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("模拟删除 SQL 失败")

    monkeypatch.setattr("ling.domains.memory.purge.delete", _boom, raising=True)

    with pytest.raises(MemoryPurgeError):
        async with session_scope() as session:
            await purge_subject(session, tombstone_id=request.tombstone_id)

    monkeypatch.undo()

    async with session_scope() as session:
        tombstone = await session.get(MemoryPurgeTombstone, request.tombstone_id)
        assert tombstone is not None
        assert tombstone.state == "failed", (
            "失败状态被事务回滚吃掉了：墓碑停在 "
            f"{tombstone.state!r}，用户会一直看到「清理中」"
        )
        assert tombstone.error_code, "失败原因码没落库，排查时看不出为什么失败"


async def test_failure_then_retry_converges_to_done(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"失败可重试"必须真的成立：修好之后同一个墓碑能跑到 `done`。

    只证明"能记下 failed"不够——如果失败那次的回滚把待删的行也一起丢了，
    重试会删到 0 行，看起来成功而数据其实早就不在了。这里断言**重试真的
    删掉了那一行**。
    """
    ids = await _seed(logged_in)
    subject_id = ids["subject_id"]
    async with session_scope() as session:
        request = await request_purge(session, subject_id=subject_id)

    def _boom(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("第一次失败")

    monkeypatch.setattr("ling.domains.memory.purge.delete", _boom, raising=True)
    with pytest.raises(MemoryPurgeError):
        async with session_scope() as session:
            await purge_subject(session, tombstone_id=request.tombstone_id)
    monkeypatch.undo()

    async with session_scope() as session:
        result = await purge_subject(session, tombstone_id=request.tombstone_id)

    assert result.state == "done"
    assert result.memories_deleted == 1, "重试没有真的删掉那一行"

    async with session_scope() as session:
        remaining = await session.scalar(
            select(MemoryRow).where(MemoryRow.subject_id == subject_id).limit(1)
        )
        assert remaining is None
        tombstone = await session.get(MemoryPurgeTombstone, request.tombstone_id)
        assert tombstone is not None
        assert tombstone.state == "done"
        assert tombstone.error_code is None, "成功后失败原因码没有清掉"


# ------------------------------------------- 缺陷 1 在真实 PostgreSQL 上的验证


async def test_legacy_event_without_subject_column_is_cancelled_on_pg(
    logged_in: TestEnv,
) -> None:
    """`subject_id` 为 NULL 的历史 pending 事件在真库上也要被取消。

    单独在 PG 上跑一遍的理由：`payload["subject_id"].as_string()` 在
    PostgreSQL 上编译成 `payload ->> 'subject_id'`（`payload` 是 `json` 类型），
    在 sqlite 上是 `JSON_EXTRACT`。两条路径都命中才算真的修好。
    """
    ids = await _seed(logged_in)
    event_id = f"obx_{uuid.uuid4().hex[:10]}"
    async with session_scope() as session:
        session.add(
            OutboxEvent(
                id=event_id,
                topic="memory.extract",
                state="pending",
                subject_id=None,
                forget_generation=0,
                payload={
                    "subject_id": ids["subject_id"],
                    "user_text": CANARY,
                    "actor_text": f"她回的：{CANARY}",
                },
            )
        )

    async with session_scope() as session:
        request = await request_purge(session, subject_id=ids["subject_id"])
    assert request.events_cancelled >= 1

    async with session_scope() as session:
        row = await session.get(OutboxEvent, event_id)
        assert row is not None
        assert row.state == "cancelled"
        assert CANARY not in str(row.payload)


async def test_legacy_done_event_is_scrubbed_on_pg(logged_in: TestEnv) -> None:
    """`done` 状态的历史事件在真库上也要被脱敏（JSON 更新路径同样要验证）。"""
    ids = await _seed(logged_in)
    event_id = f"obx_{uuid.uuid4().hex[:10]}"
    async with session_scope() as session:
        session.add(
            OutboxEvent(
                id=event_id,
                topic="memory.extract",
                state="done",
                subject_id=None,
                forget_generation=0,
                payload={"subject_id": ids["subject_id"], "user_text": CANARY},
            )
        )

    async with session_scope() as session:
        request = await request_purge(session, subject_id=ids["subject_id"])
    async with session_scope() as session:
        await purge_subject(session, tombstone_id=request.tombstone_id)

    async with session_scope() as session:
        row = await session.get(OutboxEvent, event_id)
        assert row is not None
        assert CANARY not in str(row.payload), "done 事件 payload 里的原文还在库里"
