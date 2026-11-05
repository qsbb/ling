"""A6：精排整批丢弃情节记忆。

`recall` 里 ``scored`` 的构造顺序是"先全部断言（按融合分降序）→ 再按 id
追加情节"，于是情节永远排在列表尾部。``_rerank_hits`` 原来只取
``hits[:rerank_top_n]``（默认 20）送去精排、并且只返回被精排过的那批：
断言候选 ≥ 20 时，情节一次都进不了重排器，也永远不在返回值里
（实测 assertions=20 total=23 → episodes_in_out=0 episode_sent=0）。

这里用记录型假重排器（不依赖外部 API）验证：
1) 情节必须被**送进**重排器；2) 情节必须出现在**最终返回结果**里。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

QUERY = "苹果"
EPISODE_TEXT = "上次和用户一起烤了苹果派，还聊到秋天要去看柿子"

# 每条断言都含查询词"苹果"（BM25 必中），主题词两两不同——
# 避免 MMR 近重复抑制（Jaccard 0.7）把断言裁到 20 条以下，
# 那样就测不到"断言占满精排批"这个前置条件了。
_TOPICS = [
    "登山远足和看日出", "周末烘焙戚风蛋糕", "收集老邮票的乐趣", "围棋让子局复盘",
    "游泳自由泳换气", "街拍胶片影调", "爵士钢琴即兴", "养青苔微景观点",
    "长途骑行装备", "古籍修复手艺", "陶艺拉坯成型", "天文望远镜观测",
    "咖啡手冲参数", "木工榫卯结构", "徒步露营篝火", "书法小楷临帖",
    "皮具手工缝线", "潜水耳压平衡", "香草花园打理", "猫咪行为观察",
    "旧相机维修", "街头涂鸦文化", "植物标本压制", "雪场单板练习",
    "录音混音母带",
]

EPISODE_MARK = "烤了苹果派"


class RecordingReranker:
    """按内容给分，并**记录实际收到的文档**——缺陷现场正是"情节没被送进来"。"""

    model_id = "recording-rerank"

    def __init__(self) -> None:
        self.documents: list[str] = []
        self.calls = 0

    async def rerank(
        self, query: str, documents: list[str], *, top_n: int | None = None
    ) -> list[tuple[int, float]]:
        self.calls += 1
        self.documents = list(documents)
        pairs: list[tuple[int, float]] = []
        for i, doc in enumerate(documents):
            if EPISODE_MARK in doc:
                pairs.append((i, 0.5))
            elif i == 0:
                pairs.append((i, 0.6))
            else:
                pairs.append((i, 0.02))
        pairs.sort(key=lambda p: -p[1])
        return pairs


async def _seed_assertions(subject_id: str, count: int) -> None:
    from ling.contracts.v1 import MemoryProposal, Origin, Visibility
    from ling.domains.memory.service import MemoryService
    from ling.infra.db import session_scope

    assert count <= len(_TOPICS), "主题词不够，会退化成近重复被 MMR 裁掉"
    async with session_scope() as session:
        service = MemoryService(session)
        for i in range(count):
            text = f"{_TOPICS[i]}，也提到苹果"
            await service.propose(
                MemoryProposal(
                    proposal_id=f"a6p{i}",
                    kind="assertion",
                    text=text,
                    origin=Origin.user_statement,
                    source_event_ids=["e"],
                    visibility=Visibility.subject_private,
                    confidence=0.9,
                ),
                subject_id=subject_id,
                turn_id=f"a6t{i}",
                slot=f"a6slot{i}",
                value=text,
            )


async def _seed_episode(subject_id: str) -> str:
    from ling.contracts.v1 import Origin, Visibility
    from ling.infra.db import session_scope
    from ling.infra.models import MemoryEpisode

    episode_id = f"ep_{uuid.uuid4().hex[:12]}"
    async with session_scope() as session:
        session.add(
            MemoryEpisode(
                id=episode_id,
                subject_id=subject_id,
                window_id=f"win_{uuid.uuid4().hex[:8]}",
                summary=EPISODE_TEXT,
                origin=Origin.user_statement.value,
                visibility=Visibility.subject_private.value,
                source_event_ids=["evt"],
                turn_id=f"turn_{uuid.uuid4().hex[:8]}",
                occurred_at=datetime.now(UTC),
            )
        )
    return episode_id


async def _recall(subject_id: str, reranker: object, limit: int = 6):  # noqa: ANN202
    from ling.contracts.v1 import Visibility
    from ling.domains.memory.service import MemoryService
    from ling.infra.db import session_scope

    async with session_scope() as session:
        return await MemoryService(session, embedder=None, reranker=reranker).recall(
            subject_id=subject_id,
            query=QUERY,
            visibility_allow={Visibility.subject_private},
            limit=limit,
        )


async def _assertion_candidates(subject_id: str, reranker: object) -> int:
    """前置条件：断言候选数确实吃满了精排批（否则本轮测不到 A6）。"""
    result = await _recall(subject_id, reranker, limit=100)
    return sum(1 for h in result.hits if h.kind == "assertion")


async def test_episode_retained_when_20_assertions_fill_rerank_batch(
    logged_in: TestEnv,
) -> None:
    """断言 20 条（正好等于 rerank_top_n=20）+ 情节命中 → 情节仍要进最终结果。"""
    subject_id = logged_in.subject_id
    await _seed_assertions(subject_id, 20)
    await _seed_episode(subject_id)

    reranker = RecordingReranker()
    result = await _recall(subject_id, reranker)

    assert any(EPISODE_MARK in doc for doc in reranker.documents), (
        f"情节没被送进重排器（收到 {len(reranker.documents)} 条，全无情节）"
    )
    texts = [h.text for h in result.hits]
    assert EPISODE_TEXT in texts, f"情节命中却不在最终结果里: {texts}"
    # 返回结果里也只能靠精排分排序（tail 仍是 RRF 分，会被下限挡掉），
    # 这里顺带确认情节带着精排分排在首位断言之后。
    assert [h.kind for h in result.hits] == ["assertion", "episode"]


async def test_episode_retained_when_25_assertions_fill_rerank_batch(
    logged_in: TestEnv,
) -> None:
    """断言 25 条（远超 rerank_top_n）+ 情节命中 → 同上。"""
    subject_id = logged_in.subject_id
    await _seed_assertions(subject_id, 25)
    await _seed_episode(subject_id)

    assert await _assertion_candidates(subject_id, None) >= 20

    reranker = RecordingReranker()
    result = await _recall(subject_id, reranker)

    assert any(EPISODE_MARK in doc for doc in reranker.documents), (
        f"情节没被送进重排器（收到 {len(reranker.documents)} 条，全无情节）"
    )
    texts = [h.text for h in result.hits]
    assert EPISODE_TEXT in texts, f"情节命中却不在最终结果里: {texts}"


async def test_below_top_n_behaviour_unchanged(logged_in: TestEnv) -> None:
    """对照组：断言 19 条（低于 top_n=20）→ 行为不变（原本就没丢情节）。"""
    subject_id = logged_in.subject_id
    await _seed_assertions(subject_id, 19)
    await _seed_episode(subject_id)

    reranker = RecordingReranker()
    result = await _recall(subject_id, reranker)

    # 19 断言 + 1 情节 = 20 条，正好全进精排批，一条不剩。
    assert len(reranker.documents) == 20
    assert any(EPISODE_MARK in doc for doc in reranker.documents)
    assert [h.kind for h in result.hits] == ["assertion", "episode"]
