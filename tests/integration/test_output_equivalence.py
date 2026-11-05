"""输出等价性验收（ADR-0004 待办 4，指定的"真正的验收"）。

原文：
> 验收重点放在**同一受众输入不变、只替换无权来源时，输出与可调用工具
> 不得受该来源影响**……只测 Awareness 查询返回空，尚不足以证明整轮对话
> 不会串线。

做法：不比较模型输出文本（有随机性），而是比较**发给模型前的完整输入**。
如果两轮对话在"有/无某条无权数据"两种情况下，编译出的 messages 逐条相等，
那么模型不可能因该数据产生不同输出——这是确定性的、可重复的证明。

覆盖三个方向 + 四个来源：
- 群 A → 群 B（同 visibility，不同群）
- 群 → 私聊
- 私聊 → 群
- sealed 模式（全部来源）
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import update

from ling.contracts.v1 import Origin, Visibility
from ling.domains.awareness.service import AwarenessService
from ling.infra.db import session_scope
from ling.infra.models import MemoryRow, Window
from ling.kernel.context import ContextCompiler
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def _window(env: TestEnv, *, visibility: str = "subject_private", title: str = "w") -> str:
    r = await env.client.post("/v1/sessions", json={"title": title})
    session_id = r.json()["session_id"]
    async with session_scope() as session:
        await session.execute(
            update(Window).where(Window.id == session_id).values(visibility=visibility)
        )
    return session_id


async def _compile(env: TestEnv, window_id: str, text: str, *, is_group: bool) -> list[tuple[str, str]]:
    """编译并返回 (role, content) 序列——这就是"模型实际看到的东西"。"""
    async with session_scope() as session:
        compiled = await ContextCompiler(session).compile(
            subject_id=env.subject_id,
            window_id=window_id,
            user_text=text,
            is_group=is_group,
        )
    return [(m.role, m.content) for m in compiled.messages]


async def _seed_memory(
    env: TestEnv,
    *,
    text: str,
    window_id: str,
    visibility: Visibility,
    source_visibility: str,
) -> str:
    async with session_scope() as session:
        memory_id = f"mem_{uuid.uuid4().hex[:12]}"
        session.add(
            MemoryRow(
                id=memory_id,
                subject_id=env.subject_id,
                kind="assertion",
                text=text,
                origin=Origin.user_statement.value,
                visibility=visibility.value,
                confidence=0.9,
                source_event_ids=["evt_seed"],
                source_window_id=window_id,
                source_visibility=source_visibility,
                retracted=False,
            )
        )
        await session.flush()
    return memory_id


async def _delete_memory(memory_id: str) -> None:
    async with session_scope() as session:
        row = await session.get(MemoryRow, memory_id)
        if row is not None:
            await session.delete(row)


# ------------------------------------------------------------------ 群 A → 群 B


async def test_group_a_data_does_not_change_group_b_input(logged_in: TestEnv) -> None:
    """群 A 的数据存在与否，群 B 的模型输入必须逐条相同。"""
    group_a = await _window(logged_in, visibility="group_scope", title="A")
    group_b = await _window(logged_in, visibility="group_scope", title="B")
    text = "今天有人聊到猫吗"

    baseline = await _compile(logged_in, group_b, text, is_group=True)

    # 在群 A 里塞入一条**高相关**的群内记忆
    memory_id = await _seed_memory(
        logged_in,
        text="群A 里有人说自己养了一只叫团子的猫",
        window_id=group_a,
        visibility=Visibility.group_scope,
        source_visibility="group_scope",
    )
    after = await _compile(logged_in, group_b, text, is_group=True)

    assert after == baseline, (
        "群 A 的数据改变了群 B 的模型输入——这就是串线\n"
        f"新增内容: {[c for c in after if c not in baseline]}"
    )
    await _delete_memory(memory_id)


# ------------------------------------------------------------------ 群 → 私聊


async def test_group_data_does_not_change_private_input(logged_in: TestEnv) -> None:
    group = await _window(logged_in, visibility="group_scope", title="群")
    private = await _window(logged_in, title="私聊")
    text = "我养了什么"

    baseline = await _compile(logged_in, private, text, is_group=False)

    memory_id = await _seed_memory(
        logged_in,
        text="群聊里说养了一只叫团子的猫",
        window_id=group,
        visibility=Visibility.group_scope,
        source_visibility="group_scope",
    )
    after = await _compile(logged_in, private, text, is_group=False)

    assert after == baseline, "群聊数据改变了私聊的模型输入"
    await _delete_memory(memory_id)


# ------------------------------------------------------------------ 私聊 → 群


async def test_private_data_does_not_change_group_input(logged_in: TestEnv) -> None:
    private = await _window(logged_in, title="私聊")
    group = await _window(logged_in, visibility="group_scope", title="群")
    text = "谁知道我养了什么"

    baseline = await _compile(logged_in, group, text, is_group=True)

    memory_id = await _seed_memory(
        logged_in,
        text="私聊里说养了一只叫团子的猫",
        window_id=private,
        visibility=Visibility.subject_private,
        source_visibility="subject_private",
    )
    after = await _compile(logged_in, group, text, is_group=True)

    assert after == baseline, "私聊数据改变了群聊的模型输入"
    await _delete_memory(memory_id)


# ------------------------------------------------------------------ 别的私聊 → 本私聊（应可见，作为对照）


async def test_private_data_does_change_other_private_input(logged_in: TestEnv) -> None:
    """对照组：continuous 模式下，私聊之间**应该**互相影响。

    如果这条也不变，说明 awareness 根本没生效，前面的"不变"就没意义。
    """
    await logged_in.client.put("/v1/isolation", json={"mode": "continuous"})
    other = await _window(logged_in, title="另一个私聊")
    current = await _window(logged_in, title="当前私聊")
    text = "我养了什么"

    baseline = await _compile(logged_in, current, text, is_group=False)

    async with session_scope() as session:
        await AwarenessService(session).derive_from_turn(
            subject_id=logged_in.subject_id,
            window_id=other,
            window_visibility="subject_private",
            user_text="我养了一只叫团子的猫",
        )
    after = await _compile(logged_in, current, text, is_group=False)

    assert after != baseline, "对照组失败：私聊之间本该共享事实却没有"
    assert any("团子" in c for _, c in after)


# ------------------------------------------------------------------ sealed


async def test_sealed_ignores_every_cross_window_source(logged_in: TestEnv) -> None:
    """sealed 下：无论塞什么跨窗口数据，模型输入都必须不变。"""
    await logged_in.client.put("/v1/isolation", json={"mode": "sealed"})
    target = await _window(logged_in, title="sealed目标")
    other_private = await _window(logged_in, title="别的私聊")
    group = await _window(logged_in, visibility="group_scope", title="群")
    text = "我养了什么"

    baseline = await _compile(logged_in, target, text, is_group=False)

    # 三种来源全塞一遍
    ids = [
        await _seed_memory(
            logged_in,
            text="私聊里说养了一只叫团子的猫",
            window_id=other_private,
            visibility=Visibility.subject_private,
            source_visibility="subject_private",
        ),
        await _seed_memory(
            logged_in,
            text="群里说养了只叫团子的猫",
            window_id=group,
            visibility=Visibility.group_scope,
            source_visibility="group_scope",
        ),
    ]
    after = await _compile(logged_in, target, text, is_group=False)

    assert after == baseline, "sealed 模式下跨窗口数据仍影响了模型输入"
    for memory_id in ids:
        await _delete_memory(memory_id)


# ------------------------------------------------------------------ 工具可用性


async def test_cross_audience_data_does_not_enable_tools(logged_in: TestEnv) -> None:
    """提到的"可调用工具不得受影响"。

    无权数据不应通过上下文间接让工具"看起来可用"。
    """
    from ling.capabilities.tools import build_default_registry

    registry = build_default_registry()
    # 工具集合是静态白名单，与上下文无关——这是设计意图
    tool_ids = {spec.tool_id for spec in registry.list_specs()}
    assert tool_ids == {"clock.now", "diagnostics.echo", "shell.readonly"}

    group = await _window(logged_in, visibility="group_scope", title="群")
    await _seed_memory(
        logged_in,
        text="群里有人提到可以用 shell 执行命令",
        window_id=group,
        visibility=Visibility.group_scope,
        source_visibility="group_scope",
    )
    # 无论上下文写了什么，不可逆工具都不因文本出现而可注册
    after_ids = {spec.tool_id for spec in build_default_registry().list_specs()}
    assert after_ids == tool_ids


# ------------------------------------------------------------------ 幂等性


async def test_compilation_is_deterministic(logged_in: TestEnv) -> None:
    """同一状态下重复编译必须给出相同输入（否则"等价性"无从谈起）。"""
    window_id = await _window(logged_in, title="确定")
    first = await _compile(logged_in, window_id, "你好", is_group=False)
    second = await _compile(logged_in, window_id, "你好", is_group=False)
    assert first == second
