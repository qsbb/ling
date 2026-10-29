"""核心记忆：每轮常驻，不依赖检索命中。

参照 Letta 的 core memory blocks 与「我会牢牢记住你」的"核心记忆按 Label
管理、每轮常驻、不走 RAG"。

为什么必须有这一层：检索是概率性的。边界、禁忌、称呼这类内容一旦"没命中"，
角色就显得忘了你是谁或忘了你划过的线——后果比漏记一件小事严重得多。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from ling.domains.memory.core import MAX_PER_SUBJECT, CoreMemoryService
from ling.infra.db import session_scope
from ling.kernel.context import ContextCompiler
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def _window(env: TestEnv) -> str:
    return (await env.client.post("/v1/sessions", json={"title": "core"})).json()[
        "session_id"
    ]


async def _compile(env: TestEnv, window_id: str, text: str = "随便聊聊") -> str:
    async with session_scope() as session:
        compiled = await ContextCompiler(session).compile(
            subject_id=env.subject_id,
            window_id=window_id,
            user_text=text,
            current_turn_id="turn_core",
        )
    return "\n".join(m.content for m in compiled.messages)


# ------------------------------------------------------------------ 注入


async def test_core_memory_injected_every_turn(logged_in: TestEnv) -> None:
    """核心记忆必须出现在提示里，且**不依赖用户这句话是否相关**。"""
    async with session_scope() as session:
        await CoreMemoryService(session).upsert(
            subject_id=logged_in.subject_id,
            label="边界",
            text="不要主动提起他的前任",
        )
    window_id = await _window(logged_in)
    # 用户说的是完全无关的话题，核心记忆仍要在
    blob = await _compile(logged_in, window_id, "今天吃什么")
    assert "不要主动提起他的前任" in blob
    assert "边界" in blob


async def test_core_memory_marked_as_always_present(logged_in: TestEnv) -> None:
    """注入文本要说明"每轮都在"，避免模型把它当成检索到的旧信息。"""
    async with session_scope() as session:
        await CoreMemoryService(session).upsert(
            subject_id=logged_in.subject_id, label="称呼", text="叫他阿澄"
        )
    window_id = await _window(logged_in)
    blob = await _compile(logged_in, window_id)
    assert "始终记住" in blob or "每轮" in blob


async def test_private_core_memory_hidden_in_group(logged_in: TestEnv) -> None:
    """scope=private 的核心记忆不能进群聊上下文。

    群里不该出现"他怕黑""他在吃药"这类私聊才该说的内容。
    """
    async with session_scope() as session:
        await CoreMemoryService(session).upsert(
            subject_id=logged_in.subject_id,
            label="隐私",
            text="他在吃抗抑郁药",
            scope="private",
        )
        await CoreMemoryService(session).upsert(
            subject_id=logged_in.subject_id,
            label="群规",
            text="群里不谈工作",
            scope="group",
        )
    group_window = await _window(logged_in)
    async with session_scope() as session:
        from sqlalchemy import update

        from ling.infra.models import Window

        await session.execute(
            update(Window)
            .where(Window.id == group_window)
            .values(visibility="group_scope")
        )
    blob = await _compile(logged_in, group_window)
    assert "抗抑郁药" not in blob, "私聊核心记忆泄漏到群聊"
    assert "群里不谈工作" in blob


async def test_budget_truncates_by_priority(logged_in: TestEnv) -> None:
    """预算不够时按 priority 保留（小的先留）。"""
    async with session_scope() as session:
        await CoreMemoryService(session).upsert(
            subject_id=logged_in.subject_id, label="重要", text="这句话必须保留" * 4, priority=1
        )
        await CoreMemoryService(session).upsert(
            subject_id=logged_in.subject_id, label="次要", text="这句话应该被截掉" * 200, priority=999
        )
    window_id = await _window(logged_in)
    blob = await _compile(logged_in, window_id)
    assert "这句话必须保留" in blob
    assert "这句话应该被截掉" not in blob


# ------------------------------------------------------------------ 写入规则


async def test_same_label_overwrites(logged_in: TestEnv) -> None:
    """同 label 覆盖而不是新增，避免"同一边界记了十条互相矛盾"。"""
    async with session_scope() as session:
        await CoreMemoryService(session).upsert(
            subject_id=logged_in.subject_id, label="边界", text="不要提工作"
        )
    async with session_scope() as session:
        await CoreMemoryService(session).upsert(
            subject_id=logged_in.subject_id, label="边界", text="不要提工作，也不要提加班"
        )
    async with session_scope() as session:
        items = await CoreMemoryService(session).list_active(logged_in.subject_id)
    assert len(items) == 1
    assert "加班" in items[0].text


async def test_capacity_limit(logged_in: TestEnv) -> None:
    """核心记忆有上限：它进每一轮上下文，条数一多就吃掉预算。"""
    async with session_scope() as session:
        service = CoreMemoryService(session)
        for i in range(MAX_PER_SUBJECT):
            await service.upsert(
                subject_id=logged_in.subject_id, label=f"l{i}", text=f"内容{i}"
            )
    with pytest.raises(ValueError, match="上限"):
        async with session_scope() as session:
            await CoreMemoryService(session).upsert(
                subject_id=logged_in.subject_id, label="overflow", text="超了"
            )


async def test_empty_label_or_text_rejected(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        service = CoreMemoryService(session)
        with pytest.raises(ValueError):
            await service.upsert(subject_id=logged_in.subject_id, label="", text="x")
        with pytest.raises(ValueError):
            await service.upsert(subject_id=logged_in.subject_id, label="x", text="  ")


async def test_deactivate_removes_from_context(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        view = await CoreMemoryService(session).upsert(
            subject_id=logged_in.subject_id, label="临时", text="这条待会删掉"
        )
    window_id = await _window(logged_in)
    assert "这条待会删掉" in await _compile(logged_in, window_id)

    async with session_scope() as session:
        assert await CoreMemoryService(session).deactivate(
            view.core_id, logged_in.subject_id
        )
    assert "这条待会删掉" not in await _compile(logged_in, window_id)


async def test_deactivate_rejects_wrong_subject(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        view = await CoreMemoryService(session).upsert(
            subject_id=logged_in.subject_id, label="x", text="y"
        )
        assert (
            await CoreMemoryService(session).deactivate(view.core_id, "subj_other")
            is False
        )


# ------------------------------------------------------------------ HTTP


async def test_api_roundtrip(logged_in: TestEnv) -> None:
    put = await logged_in.client.put(
        "/v1/core-memory",
        json={"label": "称呼", "text": "叫他阿澄", "priority": 10},
    )
    assert put.status_code == 200, put.text
    core_id = put.json()["core_id"]

    listing = await logged_in.client.get("/v1/core-memory")
    assert any(i["label"] == "称呼" for i in listing.json()["items"])

    deleted = await logged_in.client.delete(f"/v1/core-memory/{core_id}")
    assert deleted.status_code == 200


async def test_api_rejects_bad_scope(logged_in: TestEnv) -> None:
    response = await logged_in.client.put(
        "/v1/core-memory", json={"label": "x", "text": "y", "scope": "whatever"}
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "/v1/core-memory"), ("put", "/v1/core-memory"),
     ("delete", "/v1/core-memory/core_x")],
)
async def test_core_memory_requires_auth(
    web_env: TestEnv, method: str, path: str
) -> None:
    handler = getattr(web_env.client, method)
    resp = await (handler(path, json={}) if method == "put" else handler(path))
    assert resp.status_code == 401


async def test_core_memory_not_injected_when_sealed(logged_in: TestEnv) -> None:
    """sealed（完全隔离）时核心记忆也不该注入。

    核心块是跨窗口常驻的，隔离时仍注入就等于"完全隔离"名不副实。
    """
    from sqlalchemy import update

    from ling.infra.models import SessionPolicyRow

    async with session_scope() as session:
        await CoreMemoryService(session).upsert(
            subject_id=logged_in.subject_id, label="常驻", text="这条不该出现在隔离窗口"
        )
        # 切到 sealed
        rows = list((await session.execute(select(SessionPolicyRow))).scalars())
        if rows:
            await session.execute(
                update(SessionPolicyRow).values(mode="sealed")
            )
        else:
            from ling.infra.models import SessionPolicyRow as P

            session.add(P(subject_id=logged_in.subject_id, mode="sealed"))
    window_id = await _window(logged_in)
    blob = await _compile(logged_in, window_id)
    assert "这条不该出现在隔离窗口" not in blob, "sealed 模式下核心记忆仍被注入"


# ---------------------------------------------------------------- traits 进上下文


async def test_persona_traits_reach_the_context(logged_in: TestEnv) -> None:
    """`traits` 必须进上下文，不只是"存着"。

    人设页有一个专门的 JSON 编辑器编辑 traits，而提示词里原本只有
    system_prompt 和 boundaries——用户在那儿写什么都无效。
    """
    from ling.domains.persona.service import PersonaService
    from ling.infra.db import session_scope
    from ling.infra.models import Persona

    async with session_scope() as session:
        await PersonaService(session).ensure_default()
        persona = await session.get(Persona, logged_in.settings.persona_id)
        assert persona is not None
        persona.traits = {"group_chat": "群里看到感兴趣的就接一句", "tone": "偏冷"}
        session.add(persona)

    text = await _compile(logged_in, await _window(logged_in))
    assert "group_chat" in text, f"上下文里没有 traits：{text[:300]}"
    assert "群里看到感兴趣的就接一句" in text


async def test_recall_pool_is_bounded(web_env, monkeypatch) -> None:
    """召回候选查询必须带 LIMIT。

    原来按 subject **无 LIMIT 全量取回**再在 Python 里算相关度：3 条记忆时
    没问题，几千条就是每轮扫全表——成本随记忆总量线性增长，数据少时完全
    看不出来。

    **不能断言"返回条数 ≤ N"**：召回本身还有 limit 参数，去掉 SQL LIMIT
    结果条数也不会超（第一版就这么写，负例照样绿）。所以直接抓那条 SQL，
    断言里面真有 LIMIT。
    """
    from ling.contracts.v1 import Visibility
    from ling.domains.memory import service as mem_mod
    from ling.infra.db import session_scope

    statements: list[str] = []
    async with session_scope() as session:
        svc = mem_mod.MemoryService(session)
        original = session.execute

        async def _capture(stmt, *a, **k):  # noqa: ANN001, ANN002, ANN003, ANN202
            if isinstance(stmt, object):
                text = str(stmt)
                if "memories" in text and "retracted" in text:
                    statements.append(text)
            return await original(stmt, *a, **k)

        session.execute = _capture  # type: ignore[method-assign]
        await svc.recall(
            subject_id=web_env.subject_id,
            query="任意",
            visibility_allow={Visibility.subject_private},
            limit=5,
        )

    assert statements, "没抓到召回查询"
    assert any("LIMIT" in s.upper() for s in statements), "召回查询没有上界"
