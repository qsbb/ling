"""Smoke test：确认能在隔离测试库上调用 ContextCompiler.compile() 拿到 annotated。

用法（在仓库根）：
    source /tmp/ling_test_lock.sh && acquire_test_lock
    export LING_TEST_DATABASE_URL=... LING_DATABASE_URL=...
    .venv/bin/python docs/evidence/ling-context-research/replay/00-smoke-compile.py
    release_test_lock

不改 src/；只在测试库里插入带固定前缀的临时行并在 finally 清理。
"""

from __future__ import annotations

import asyncio
import os
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ling.contracts.v1 import TurnState
from ling.infra.models import Message, Persona, Subject, Window, WindowContext
from ling.kernel.context import CompiledContext, ContextCompiler

URL = os.environ.get(
    "LING_TEST_DATABASE_URL",
    "postgresql+asyncpg://ling@127.0.0.1:55432/ling_test",
)
PREFIX = "so_smoke_"


async def main() -> None:
    engine = create_async_engine(URL)
    uid = PREFIX + uuid.uuid4().hex[:8]
    try:
        async with AsyncSession(engine) as s:
            subject = (await s.execute(select(Subject).limit(1))).scalar_one_or_none()
            persona = (await s.execute(select(Persona).limit(1))).scalar_one_or_none()
            if not subject or not persona:
                print("NO_FIXTURE subject/persona; skip")
                return
            w = Window(
                id=uid + "_w",
                subject_id=subject.id,
                title="smoke",
                visibility="subject_private",
                window_ref=f"smoke:private:{uid}",
            )
            s.add(w)
            await s.flush()
            msg = Message(
                id=uid + "_m1",
                window_id=w.id,
                subject_id=subject.id,
                role="user",
                text="上次那个产品方案，整理好了吗",
                state=TurnState.completed.value,
                sequence=1,
            )
            s.add(msg)
            await s.commit()

            compiled: CompiledContext = await ContextCompiler(
                s, actor_id="actor_smoke", persona_id=persona.id
            ).compile(
                subject_id=subject.id,
                window_id=w.id,
                user_text="？",
                is_group=False,
                user_sender_name=None,
            )
            print("SMOKE_OK lanes=", [(m.lane, m.role, m.key) for m in compiled.annotated])
            print("META=", compiled.meta)
    finally:
        async with AsyncSession(engine) as s:
            for model, col in (
                (Message, Message.id),
                (WindowContext, WindowContext.window_id),
                (Window, Window.id),
            ):
                rows = (
                    await s.execute(select(model).where(col.like(PREFIX + "%")))
                ).scalars().all()
                for r in rows:
                    await s.delete(r)
            await s.commit()
        await engine.dispose()


asyncio.run(main())
