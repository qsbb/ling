"""④ 记忆与学习 的设置面：策略从设置写进召回与保留。

这一类的参数原来是模块级常量（``RELATIVE_SCORE_FLOOR`` 等）。抽成策略
是因为它们是**用户会想调的东西**——觉得角色记性太好/太差、翻旧账太频繁，
都该能在设置里改，而不是改代码重部署。

本文件钉住五件事：

1. 不配置时行为不变（默认值 = 原常量）
2. 草稿 → 发布 → 生效，整条链路通
3. 只改一个字段时其他字段保持，**不是重置成默认**
4. 非法值在入口被拒，库里出现脏值时读取夹回合法区间
5. **发布后的参数真的改变运行时的召回/归档行为**——而且是从
   **生产入口**（真实一轮对话 / worker handler）观察到的，不是手工构造
   `MemoryService(policy=...)` 的自证。

第 5 条是本文件里最容易被写假的一条：旧用例只测了
``MemoryService.default_retention()``，而那个方法**没有任何生产调用者**，
于是"策略生效"是绿的、运行时却从不读策略。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from ling.domains.memory.service import MemoryService
from ling.infra.db import session_scope
from ling.infra.models import ConfigRevision, MemoryRow
from ling.ports.config import MemoryPolicy
from ling.settings.service import _memory_policy, published_memory_policy
from tests.integration.conftest import TestEnv, read_sse

pytestmark = pytest.mark.w0_web

DEFAULTS = {
    "relative_score_floor": 0.35,
    "rerank_top_n": 20,
    "recency_half_life_days": 120.0,
    "max_retention_days": 3650,
}

# 这三条记忆都命中同一个查询；只有 `old` 够老（61 天）。
QUERY = "用户喜欢喝什么茶"
TEXTS = {
    "close": "用户喜欢喝乌龙茶，每天下午都要一杯",
    "old": "用户以前住在杭州西湖边，房东姓周，喜欢喝龙井",
    "noise": "用户养了一只叫年糕的猫，喜欢在窗台晒太阳",
}


async def _publish(env: TestEnv, payload: dict) -> dict:
    draft = await env.client.put("/v1/settings/draft", json=payload)
    assert draft.status_code == 200, draft.text
    revision = draft.json()["revision"]
    published = await env.client.post("/v1/settings/publish", json={"revision": revision})
    assert published.status_code == 200, published.text
    return (await env.client.get("/v1/settings/config")).json()


async def _seed(
    env: TestEnv, *, label: str, days_old: int, retention_days: int = 3650
) -> str:
    memory_id = f"mem_{uuid.uuid4().hex[:12]}"
    async with session_scope() as session:
        session.add(
            MemoryRow(
                id=memory_id,
                subject_id=env.subject_id,
                kind="assertion",
                text=TEXTS[label],
                origin="user_statement",
                visibility="subject_private",
                confidence=0.8,
                source_event_ids=["evt"],
                retracted=False,
                retention_days=retention_days,
                created_at=datetime.now(UTC) - timedelta(days=days_old),
            )
        )
    return memory_id


async def _new_session(env: TestEnv) -> str:
    r = await env.client.post("/v1/sessions", json={"title": f"policy-{uuid.uuid4().hex[:6]}"})
    assert r.status_code == 201, r.text
    return r.json()["session_id"]


async def _send_turn(env: TestEnv, session_id: str) -> list[tuple[str, dict]]:
    """真实一轮对话（生产入口：路由 → TurnCoordinator → ContextCompiler）。"""
    body = {"client_turn_id": str(uuid.uuid4()), "text": QUERY}
    response = await env.client.post(f"/v1/sessions/{session_id}/turns", json=body)
    assert response.status_code == 200, response.text
    events = await read_sse(response)
    assert any(name == "turn.completed" for name, _ in events), events[-3:]
    return events


async def _recalled_ids(subject_id: str) -> set[str]:
    """这一轮真正被"想起"的记忆（ContextCompiler 编译后写的 last_recalled_at）。"""
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(MemoryRow.id).where(
                        MemoryRow.subject_id == subject_id,
                        MemoryRow.last_recalled_at.is_not(None),
                    )
                )
            ).scalars()
        )
    return set(rows)


async def _reset_recall_marks(subject_id: str) -> None:
    async with session_scope() as session:
        await session.execute(
            text(
                "UPDATE memories SET last_recalled_at=NULL, last_injected_at=NULL, "
                "access_count=0 WHERE subject_id=:s"
            ),
            {"s": subject_id},
        )


# ---------------------------------------------------------------- 默认与往返


async def test_memory_defaults_when_unset(logged_in: TestEnv) -> None:
    """没配置过时，接口回显的就是原常量值——行为不变。"""
    body = (await logged_in.client.get("/v1/settings/config")).json()
    assert body["memory"] == DEFAULTS


async def test_memory_roundtrip(logged_in: TestEnv) -> None:
    """草稿 → 发布 → 生效。"""
    body = await _publish(
        logged_in,
        {
            "memory": {
                "relative_score_floor": 0.6,
                "rerank_top_n": 8,
                "recency_half_life_days": 30.0,
                "max_retention_days": 180,
            }
        },
    )
    assert body["memory"] == {
        "relative_score_floor": 0.6,
        "rerank_top_n": 8,
        "recency_half_life_days": 30.0,
        "max_retention_days": 180,
    }


async def test_memory_partial_update_keeps_other_fields(logged_in: TestEnv) -> None:
    """只改一个字段时，其他字段保持原值——不是被重置成默认。

    这条最容易写错：合并时把没传的字段当成 None 写回去，
    用户改个重排条数，结果召回下限和保留期一起被重置。
    """
    await _publish(logged_in, {"memory": {"rerank_top_n": 8, "max_retention_days": 180}})
    body = await _publish(logged_in, {"memory": {"rerank_top_n": 12}})

    assert body["memory"]["rerank_top_n"] == 12
    assert body["memory"]["max_retention_days"] == 180, "没传的字段被重置了"
    assert body["memory"]["relative_score_floor"] == DEFAULTS["relative_score_floor"]


# ---------------------------------------------------------------- 校验


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("relative_score_floor", 1.5),
        ("relative_score_floor", -0.1),
        ("rerank_top_n", 0),
        ("rerank_top_n", 1000),
        ("recency_half_life_days", 0),
        ("max_retention_days", 0),
    ],
)
async def test_out_of_range_is_rejected(logged_in: TestEnv, field: str, value: float) -> None:
    """非法值在入口就拒，不进草稿。"""
    response = await logged_in.client.put(
        "/v1/settings/draft", json={"memory": {field: value}}
    )
    assert response.status_code == 422, response.text


def test_dirty_value_is_clamped_on_read() -> None:
    """库里出现脏值（或旧修订手改过）时，读取夹到合法区间。

    不能因为一个越界数字就让记忆检索崩掉，也不能让下限变成负数导致
    "什么都召回"。夹回来是唯一安全的处理。
    """
    policy = _memory_policy(
        {
            "relative_score_floor": 99,
            "rerank_top_n": -5,
            "recency_half_life_days": "oops",
            "max_retention_days": 10**9,
        }
    )
    assert policy.relative_score_floor == 1.0
    assert policy.rerank_top_n == 1
    assert policy.recency_half_life_days == DEFAULTS["recency_half_life_days"]
    assert policy.max_retention_days == 3650


def test_missing_key_falls_back_to_default() -> None:
    assert _memory_policy(None) == MemoryPolicy()
    assert _memory_policy({}) == MemoryPolicy()


# ---------------------------------------------------------------- 真的用上了
#
# 下面这些用例都从**生产入口**观察行为：
# - 召回：真实一轮对话（HTTP → TurnCoordinator → ContextCompiler），
#   观察点是被召回行的 `last_recalled_at`；
# - 归档/写入：worker 的 handler 本体（`handle_memory_archive` /
#   `handle_memory_extract`）。
#
# 为什么不能用 `ContextCompiler(session).compile(...)`：那是**测试旁路**——
# 生产里由 turn.py 构造它，旁路构造永远不会暴露"turn.py 没把 policy 传进去"
# 这个缺陷。把 ContextCompiler 的 policy 注入删掉时，只有走真实轮次的用例会红。


async def test_default_policy_keeps_recall_behavior(logged_in: TestEnv) -> None:
    """没有已发布策略时，行为等于原来的代码默认值（61 天前的记忆照样召回）。"""
    session_id = await _new_session(logged_in)
    close = await _seed(logged_in, label="close", days_old=2)
    old = await _seed(logged_in, label="old", days_old=61)
    noise = await _seed(logged_in, label="noise", days_old=1)

    await _send_turn(logged_in, session_id)
    recalled = await _recalled_ids(logged_in.subject_id)

    assert {close, old, noise} <= recalled, (
        "默认策略下三条都该被召回；少了一条说明默认值被改窄了"
    )
    # 默认值本身没被改（GET /v1/settings/config 与本文件常量一致）
    body = (await logged_in.client.get("/v1/settings/config")).json()
    assert body["memory"] == DEFAULTS


async def test_published_policy_takes_effect_on_next_turn(logged_in: TestEnv) -> None:
    """发布 → **下一轮**生效；而且必须是通过 ContextCompiler 的生产接线生效。

    发布 `max_retention_days=30` + `relative_score_floor=1.0`：
    - 61 天前的记忆被保留期硬门挡掉（默认 3650 时它会被召回）；
    - 相关性下限 1.0 只留下与第一名同分的那条。

    这两条**任意一条**不生效，都说明策略没到运行时。
    """
    session_id = await _new_session(logged_in)
    close = await _seed(logged_in, label="close", days_old=2)
    old = await _seed(logged_in, label="old", days_old=61)
    await _seed(logged_in, label="noise", days_old=1)

    # 发布前：三条都在
    await _send_turn(logged_in, session_id)
    before = await _recalled_ids(logged_in.subject_id)
    assert old in before, "默认策略下 61 天前的记忆不该被挡"
    assert len(before) >= 2

    await _publish(
        logged_in,
        {
            "memory": {
                "max_retention_days": 30,
                "relative_score_floor": 1.0,
                "recency_half_life_days": 1.0,
                "rerank_top_n": 1,
            }
        },
    )
    await _reset_recall_marks(logged_in.subject_id)

    # 发布后：下一轮就换口径
    await _send_turn(logged_in, session_id)
    after = await _recalled_ids(logged_in.subject_id)

    assert old not in after, (
        "已发布 max_retention_days=30 没有到达召回硬门：61 天前的记忆仍进了提示词"
    )
    assert after == {close}, (
        f"relative_score_floor=1.0 应当只留下最相关的一条，实际 {sorted(after)}"
    )


async def test_retention_ruling_row_value_is_a_shorter_request(logged_in: TestEnv) -> None:
    """`max_retention_days` 与行级 `retention_days` 的裁决（文档化行为）。

    - 策略是**硬上限**：行内 3650 + 策略 30 → 按 30 天到期；
    - 行内值可以**更短**：行内 10 + 策略 3650 → 按 10 天到期；
    - 写入时按当时的策略盖章（写侧不再落回列默认 365）。
    """
    old_wide = await _seed(logged_in, label="old", days_old=100, retention_days=3650)

    async with session_scope() as session:
        service = MemoryService(session, policy=MemoryPolicy(max_retention_days=30))
        row = await session.get(MemoryRow, old_wide)
        assert row is not None
        assert service.effective_retention_days(row) == 30
        archived = await service.archive_expired()
        assert archived >= 1
        await session.refresh(row)
        assert row.archived_at is not None
        assert "策略上限 30" in (row.archive_reason or "")

    # 行内更短：策略很宽也要按行内的来
    short = await _seed(logged_in, label="noise", days_old=20, retention_days=10)
    async with session_scope() as session:
        service = MemoryService(session, policy=MemoryPolicy(max_retention_days=3650))
        short_row = await session.get(MemoryRow, short)
        assert short_row is not None
        assert service.effective_retention_days(short_row) == 10
        await service.archive_expired()
        await session.refresh(short_row)
        assert short_row.archived_at is not None, "行内 10 天的请求没被尊重"


async def test_archive_handler_uses_published_policy(logged_in: TestEnv) -> None:
    """worker 的 `memory.archive` 要按已发布策略裁决，而不是代码默认值。

    行内 3650（= 策略默认值）、100 天前；发布 `max_retention_days=30` 后
    这一轮归档必须把它收掉。旧实现里归档只读行内 3650，永远不归档。
    """
    memory_id = await _seed(logged_in, label="old", days_old=100, retention_days=3650)
    await _publish(logged_in, {"memory": {"max_retention_days": 30}})

    from ling.worker import handle_memory_archive

    await handle_memory_archive({})

    async with session_scope() as session:
        row = await session.get(MemoryRow, memory_id)
    assert row is not None, "归档不该物理删除"
    assert row.archived_at is not None, "已发布 max_retention_days=30 没到 worker 的归档判定"


async def test_extract_handler_stamps_published_retention(logged_in: TestEnv) -> None:
    """worker 的 `memory.extract` 写入的新行，保留期要按已发布策略盖章。

    不盖的话会落回列默认 365，与设置页的 `max_retention_days` 无关——
    这正是"设置里的 3650 永远不生效"那一半的原因。
    """
    await _publish(logged_in, {"memory": {"max_retention_days": 120}})
    window_id = await _new_session(logged_in)
    turn_id = f"turn_{uuid.uuid4().hex[:8]}"

    from ling.worker import handle_memory_extract

    async with session_scope() as session:
        await handle_memory_extract(
            {
                "subject_id": logged_in.subject_id,
                "window_id": window_id,
                "turn_id": turn_id,
                "user_text": "我喜欢喝龙井茶",
                "actor_text": "记住了",
                "source_visibility": "subject_private",
            }
        )

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(MemoryRow).where(MemoryRow.turn_id == turn_id)
                )
            ).scalars()
        )
    assert rows, "提取槽位没落库，用例失去意义"
    assert {int(r.retention_days) for r in rows} == {120}, (
        f"写入时没用已发布策略盖章：{[r.retention_days for r in rows]}"
    )


async def test_bad_published_payload_falls_back_to_defaults(logged_in: TestEnv) -> None:
    """库里被手改成脏值时，运行时回落到合法值，不崩、不乱召回。

    发布入口有校验（422），但库是外部可写的：旧修订、手工改、迁移残留都可能
    留下 `relative_score_floor=-1` 这种值。读取侧必须夹住。
    """
    # 先按正常路径发布一份（这样 payload 里有可用的 provider/profile，
    # 脏值测试才不会变成"没模型所以对话跑不起来"）
    await _publish(logged_in, {"memory": {}})

    async with session_scope() as session:
        current = (
            await session.execute(
                select(ConfigRevision)
                .where(ConfigRevision.state == "published")
                .order_by(ConfigRevision.revision.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        payload = dict(current.payload or {}) if current is not None else {}
        # 保留 providers/profiles（否则这一轮对话会因为没模型而失败，
        # 测的就不是"脏策略值"了）
        payload["memory"] = {
            "relative_score_floor": -1,
            "rerank_top_n": 0,
            "recency_half_life_days": "oops",
            "max_retention_days": 10**9,
        }
        session.add(
            ConfigRevision(
                id=f"cfg_dirty_{uuid.uuid4().hex[:8]}",
                revision=int(current.revision) + 1 if current is not None else 1,
                state="published",
                payload=payload,
            )
        )

    async with session_scope() as session:
        policy = await published_memory_policy(session)
    assert policy.relative_score_floor == 0.0
    assert policy.rerank_top_n == 1
    assert policy.recency_half_life_days == DEFAULTS["recency_half_life_days"]
    assert policy.max_retention_days == 3650

    # 脏配置下真实一轮对话仍然能跑完（夹住的值不会让检索崩）
    session_id = await _new_session(logged_in)
    close = await _seed(logged_in, label="close", days_old=2)
    await _send_turn(logged_in, session_id)
    assert close in await _recalled_ids(logged_in.subject_id)


async def test_new_process_reads_latest_revision_without_restart(logged_in: TestEnv) -> None:
    """多进程可见性：换一个**新 session**（等价于另一个 api/worker 进程）
    现读一次就能拿到刚发布的修订——设置没有进程内缓存，不需要重启/失效。

    api×2 + worker 共三个进程，各自在下一轮/下一个事件时查一次库；
    已开始的轮次用自己那份快照，所以"发布只对下一轮生效"。
    """
    async with session_scope() as session:
        before = (await published_memory_policy(session)).max_retention_days
    assert before == DEFAULTS["max_retention_days"]

    await _publish(logged_in, {"memory": {"max_retention_days": 45}})

    # 模拟"另一个进程"：全新 session（新连接、新事务）
    from ling.infra.security import SecretBox
    from ling.settings.service import ConfigService

    async with session_scope() as session:
        effective = await ConfigService(
            session,
            SecretBox(logged_in.settings),
            test_mode_default=logged_in.settings.test_mode,
            persona_id=logged_in.settings.persona_id,
        ).effective()
        from_worker_helper = await published_memory_policy(session)
    assert effective.memory.max_retention_days == 45
    assert from_worker_helper.max_retention_days == 45

