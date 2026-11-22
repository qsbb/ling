"""隔离回放：在 ling_test 库的独立 schema 里建当前模型对应的表，跑 ContextCompiler.compile()，
导出 compiled.annotated 快照。不改 src/，不碰测试库现有 schema 的数据。

用法（仓库根）：
    source /tmp/ling_test_lock.sh && acquire_test_lock
    export LING_TEST_DATABASE_URL=... LING_DATABASE_URL=...
    .venv/bin/python docs/evidence/ling-context-research/replay/01-replay.py > replay/out.json 2> replay/err.log
    release_test_lock

输出 JSON 到 stdout（每行一条 JSON 记录）；真实模型输出一律不产出——本脚本只取编译输入。
脚本模拟的回复不在此脚本内生成，见 02-variants.py / 报告。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ling.contracts.v1 import TurnState
from ling.infra.models import Base, Message, Persona, Subject, Window
from ling.kernel.context import CompiledContext, ContextCompiler

BASE_URL = os.environ.get(
    "LING_TEST_DATABASE_URL",
    "postgresql+asyncpg://ling@127.0.0.1:55432/ling_test",
)
SCHEMA = "so_replay"


def engine_for(path: str):
    return create_async_engine(
        BASE_URL, connect_args={"server_settings": {"search_path": path}}
    )


def compact(msg: Any) -> dict[str, str]:
    text = msg.content
    return {
        "role": msg.role,
        "lane": getattr(msg, "lane", ""),
        "key": getattr(msg, "key", ""),
        "chars": len(text),
        "text": text[:240] + ("…" if len(text) > 240 else ""),
    }


def lane_counts(compiled: CompiledContext) -> dict[str, int]:
    out: dict[str, int] = {}
    for m in compiled.annotated:
        lane = getattr(m, "lane", "") or "?"
        out[lane] = out.get(lane, 0) + 1
    return out


async def seed(
    s: AsyncSession, *, case: str, subject_id: str, persona_id: str, window_id: str,
    history: list[tuple[int, str, str, str | None]],  # (seq, role, text, sender_name)
) -> None:
    s.add(Subject(id=subject_id, display_name="样本用户", trust_level="owner"))
    s.add(Persona(id=persona_id, display_name="样本人格", name="sample",
                  system_prompt="只使用合成数据的测试角色。", boundaries=[]))
    s.add(Window(
        id=window_id, actor_id=f"a_{case}", persona_id=persona_id,
        subject_id=subject_id, title="回放窗口", visibility="subject_private",
        channel="web", origin_status="not_applicable",
        connector_window_ref=f"replay:private:{window_id}",
    ))
    await s.flush()  # 确保 Window 先入库，messages 的 FK 才满足
    for seq, role, text, _sender in history:
        s.add(Message(
            id=f"{window_id}_m{seq}", window_id=window_id,
            role=role, text=text, state=TurnState.completed.value, sequence=seq,
            sender_subject_id=subject_id if role == "user" else None,
        ))
    await s.flush()
    await s.commit()


async def run_case(
    *, case: str, user_text: str, history: list[tuple[int, str, str, str | None]],
    is_group: bool = False,
) -> dict[str, Any]:
    eng = engine_for(SCHEMA)
    wid = f"w_{case}"
    sid, pid = f"s_{case}", f"p_{case}"
    try:
        async with AsyncSession(eng) as s:
            await seed(s, case=case, subject_id=sid, persona_id=pid, window_id=wid, history=history)
            compiled: CompiledContext = await ContextCompiler(
                s, actor_id=f"a_{case}", persona_id=pid
            ).compile(
                subject_id=sid, window_id=wid, user_text=user_text,
                is_group=is_group, user_sender_name=("样本群友" if is_group else None),
            )
            rec: dict[str, Any] = {
                "case": case,
                "user_text": user_text,
                "is_group": is_group,
                "meta": {k: str(v) for k, v in compiled.meta.items() if k in (
                    "memory_recall_skipped", "evidence_selected", "evidence_dropped",
                    "history_cap_hit", "rolled_up", "audience",
                )},
                "lanes": lane_counts(compiled),
                "annotated_chars": sum(len(m.content) for m in compiled.annotated),
                "n_messages": len(compiled.annotated),
                "n_calls": 1,
                "n_calls_kind": "ContextCompiler.compile",
                "n_llm_calls": 0,
                "m12_status": "UNKNOWN_NO_REAL_MODEL_OUTPUT",
                "annotated": [compact(m) for m in compiled.annotated],
                "source": "COMPILED_INPUT_ONLY__NO_MODEL_OUTPUT",
            }
            return rec
    finally:
        await eng.dispose()


async def setup_schema() -> None:
    """建独立 schema（先 drop 重建），供本轮回放独占。"""
    admin = create_async_engine(BASE_URL)
    async with admin.begin() as c:
        await c.execute(__import__("sqlalchemy").text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        await c.execute(__import__("sqlalchemy").text(f"CREATE SCHEMA {SCHEMA}"))
    await admin.dispose()
    eng0 = engine_for(SCHEMA)
    async with eng0.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    await eng0.dispose()


async def teardown_schema() -> None:
    admin2 = create_async_engine(BASE_URL)
    async with admin2.begin() as c:
        await c.execute(__import__("sqlalchemy").text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
    await admin2.dispose()


async def main() -> None:
    await setup_schema()

    HIST_OLD = [(1, "user", "上次那个产品方案你再发我一份", None),
                (2, "actor", "好，明天整理给你", None)]
    HIST_MULTI = [(1, "user", "A 方案的成本表发你了", None),
                  (2, "actor", "收到", None),
                  (3, "user", "B 方案的成本表也发你了", None),
                  (4, "actor", "好", None)]
    cases = [
        ("S01_explicit_resume", "上次说的那个产品方案，整理好了吗？", HIST_OLD, False),
        ("S05_greeting", "早上好", HIST_OLD, False),
        ("S06_q_immediate", "？", [(1, "user", "我十分钟到", None)], False),
        ("S07_q_stale", "？", [(1, "user", "明天见", None)], False),
        ("S08_multi_similar", "B 方案那个成本表，第三行怎么算的？", HIST_MULTI, False),
        ("S13_shift_then_q", "？", [(1, "user", "那个方案定了吗", None),
                                    (2, "user", "先别说那个", None)], False),
    ]
    try:
        for case, text, hist, group in cases:
            rec = await run_case(case=case, user_text=text, history=hist, is_group=group)
            print(json.dumps(rec, ensure_ascii=False), flush=True)
    finally:
        await teardown_schema()


if __name__ == "__main__":
    asyncio.run(main())
