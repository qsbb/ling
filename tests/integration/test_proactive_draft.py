"""主动草稿：她会说什么。

主动联系的链路是「收集 → 门控 → 生成内容 → 投递」，其中投递整段原本缺失。
直接从"收集"跳到"投递"太冒险——用户没法判断"她今天为什么突然找我"。

所以先做可观察的中间态：生成出来、存下来、**不发送**。

三条硬规则：

1. **`reason` 不能直接当消息发**。那是内部推理（"问对方手机上聊天指什么项目"），
   发出去像自言自语，不像对你说的话。
2. **生成失败不写占位符**。`draft_text` 保持 NULL 表示"还没生成"；写一句
   "（生成失败）"会把"没内容"伪装成"有内容但很怪"，而且重试永远不会发生。
3. **幂等**。已经有草稿的跳过——重复生成会让用户看到内容跳变。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from ling.domains.proactive.draft import draft_pending
from ling.infra.db import session_scope
from ling.infra.models import ProactiveOpportunity
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"
SUBJ = "subj_draft_test"


class _Router:
    """记录调用；可配置为返回固定文本或抛错。"""

    def __init__(self, *, text: str = "刚想到个事，跟你唠两句。", boom: bool = False) -> None:
        self.calls: list[list[object]] = []
        self._text = text
        self._boom = boom

    async def stream(self, config, *, profile, messages, **kw):  # noqa: ANN001, ANN003
        from ling.models.router import StreamOutcome
        from ling.ports.model import ModelChunk

        self.calls.append(list(messages))
        if self._boom:
            raise RuntimeError("模型挂了")
        yield ModelChunk(delta=self._text), StreamOutcome()
        yield ModelChunk(done=True), StreamOutcome(text=self._text)


class _Config:
    async def effective(self):  # noqa: ANN202
        return SimpleNamespace()


async def _clean() -> None:
    from sqlalchemy import delete

    async with session_scope() as session:
        await session.execute(
            delete(ProactiveOpportunity).where(
                ProactiveOpportunity.subject_id == SUBJ
            )
        )


@pytest.fixture(autouse=True)
async def _reset(logged_in: TestEnv) -> None:  # noqa: ARG001
    await _clean()
    yield
    await _clean()


async def _seed(n: int = 1, *, expired: bool = False) -> list[str]:
    ids: list[str] = []
    now = datetime.now(UTC)
    async with session_scope() as session:
        for i in range(n):
            oid = f"opp_draft_{i}"
            session.add(
                ProactiveOpportunity(
                    id=oid,
                    actor_id=ACTOR,
                    subject_id=SUBJ,
                    kind="reach_out",
                    reason=f"想问对方第 {i} 件事",
                    state="candidate",
                    expires_at=now + (timedelta(hours=-1) if expired else timedelta(hours=2)),
                    created_at=now,
                )
            )
            ids.append(oid)
    return ids


async def test_draft_generates_text_for_candidates(logged_in: TestEnv) -> None:  # noqa: ARG001
    ids = await _seed(2)
    router = _Router()

    async with session_scope() as session:
        stats = await draft_pending(
            session, actor_id=ACTOR, router=router, config_reader=_Config(), limit=5
        )
    assert stats["drafted"] == 2, stats
    assert len(router.calls) == 2

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(ProactiveOpportunity).where(ProactiveOpportunity.id.in_(ids))
            )
        ).scalars().all()
    assert all(r.draft_text == "刚想到个事，跟你唠两句。" for r in rows)


async def test_draft_is_idempotent(logged_in: TestEnv) -> None:  # noqa: ARG001
    """已有草稿的跳过——重复生成会让用户看到内容跳变。"""
    await _seed(1)
    router = _Router()

    async with session_scope() as session:
        first = await draft_pending(
            session, actor_id=ACTOR, router=router, config_reader=_Config()
        )
    async with session_scope() as session:
        second = await draft_pending(
            session, actor_id=ACTOR, router=router, config_reader=_Config()
        )

    assert first["drafted"] == 1
    assert second["drafted"] == 0, "重复生成了"
    assert second["considered"] == 0, "又把它捞出来了"
    assert len(router.calls) == 1


async def test_expired_candidates_are_skipped(logged_in: TestEnv) -> None:  # noqa: ARG001
    """过期的不生成——本来就不会发，白花一次调用。"""
    await _seed(1, expired=True)
    router = _Router()

    async with session_scope() as session:
        stats = await draft_pending(
            session, actor_id=ACTOR, router=router, config_reader=_Config()
        )
    assert stats["considered"] == 0
    assert router.calls == []


async def test_failure_does_not_write_a_placeholder(logged_in: TestEnv) -> None:  # noqa: ARG001
    """生成失败时 draft_text 保持 NULL，不写占位符。

    写"(生成失败)"会把"没内容"伪装成"有内容但很怪"，而且重试永远不会发生。
    """
    ids = await _seed(1)
    router = _Router(boom=True)

    async with session_scope() as session:
        stats = await draft_pending(
            session, actor_id=ACTOR, router=router, config_reader=_Config()
        )
    assert stats["drafted"] == 0
    assert stats["failed"] == 1

    async with session_scope() as session:
        row = await session.get(ProactiveOpportunity, ids[0])
    assert row is not None
    assert row.draft_text is None, f"失败却写了内容：{row.draft_text!r}"


async def test_empty_generation_is_not_stored(logged_in: TestEnv) -> None:  # noqa: ARG001
    """模型返回空白也不写——空字符串和"还没生成"必须分开。"""
    ids = await _seed(1)
    router = _Router(text="   ")

    async with session_scope() as session:
        stats = await draft_pending(
            session, actor_id=ACTOR, router=router, config_reader=_Config()
        )
    assert stats["drafted"] == 0
    async with session_scope() as session:
        row = await session.get(ProactiveOpportunity, ids[0])
    assert row.draft_text is None


async def test_reason_is_in_the_prompt_but_not_the_output(logged_in: TestEnv) -> None:  # noqa: ARG001
    """`reason` 要进 prompt（她得知道自己为什么找对方），但不能直接当消息。

    这条钉住的是"必须过一次生成"这个决定。
    """
    await _seed(1)
    router = _Router(text="随便说句话。")

    async with session_scope() as session:
        await draft_pending(
            session, actor_id=ACTOR, router=router, config_reader=_Config()
        )
    prompt = "\n".join(m.content for m in router.calls[0])
    assert "想问对方第 0 件事" in prompt, "reason 没进 prompt"
    assert "只输出这句话本身" in prompt, "没约束输出形态"


async def test_draft_is_exposed_by_api(logged_in: TestEnv) -> None:
    """接口要把草稿带出去，用户才能看到"她会说什么"。

    用**登录用户的 subject** 造候选，否则 pending 会按 subject 过滤掉它，
    测试就变成了"查库确认"而不是"确认接口返回"——那样等于没测到接口。
    """
    me = (await logged_in.client.get("/v1/auth/me")).json()
    subject_id = me["user"]["subject_id"]
    now = datetime.now(UTC)
    async with session_scope() as session:
        session.add(
            ProactiveOpportunity(
                id="opp_draft_api",
                actor_id=ACTOR,
                subject_id=subject_id,
                kind="reach_out",
                reason="想问对方一件事",
                state="candidate",
                expires_at=now + timedelta(hours=2),
                created_at=now,
                draft_text="在吗，问个事。",
            )
        )

    body = (await logged_in.client.get("/v1/proactive")).json()
    rows = [p for p in body["pending"] if p["opportunity_id"] == "opp_draft_api"]
    assert rows, f"候选没出现在待发列表里：{[p['opportunity_id'] for p in body['pending']]}"
    assert rows[0]["draft_text"] == "在吗，问个事。", "接口没带出草稿"
    assert body["draft_implemented"] is True
    # reason 是内部推理，也要带出来但语义不同
    assert rows[0]["reason"] == "想问对方一件事"
