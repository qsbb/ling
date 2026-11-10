"""记忆召回的**作用域**：候选池上限与账号合并。

## 这个文件防的是两个实测发现的真缺陷

### 一、候选池按"最近更新"截断，最旧的高相关记忆永远读不到

`recall()` 为了"每轮成本不随记忆总量增长"，先取
`ORDER BY updated_at DESC LIMIT 500` 的候选池再排序。这本身是对的取舍，
但**顺序依据是"最近更新"而不是"相关度"**——于是一条约一年没动过、恰好
高度相关的记忆，只要库里有 500 条比它新的，就**永远不会进候选池**。

实测（本文件 `test_old_but_relevant_memory_survives_the_candidate_pool` 就是它
的可复现形式）：500 条时召回、501 条时不召回，唯一变量是条数。
用户视角是"她把我早告诉她的重要事情忘了，却记得一堆无关琐事"。

### 二、账号合并后，情节记忆读不到另一半

断言记忆（`MemoryRow`）用合并展开后的 `subjects` 集合查，而情节记忆
（`MemoryEpisode`）原来只用单个 `subject_id`。后果：A 合并进 B 之后，从 A
的窗口问起，**断言读得到 B、情节读不到 B**——同一个人的两段记忆有一半瞎的，
而且看起来只像"她记性不好"，不像 bug。

实测修复前 `kinds={'fact': 1}`、修复后 `kinds={'episode': 1, 'fact': 1}`。

## 为什么这两个要放在一个文件

它们是同一个问题的两面：**"谁的内容能进候选"这件事有两处独立的判据**
（条数上限、主体范围），两处都写错过一次。放一起便于下次改召回时一起看。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from ling.contracts.v1 import Visibility
from ling.domains.memory.service import RECALL_CANDIDATE_LIMIT, MemoryService
from ling.infra.db import session_scope
from ling.infra.models import MemoryEpisode, MemoryRow, Subject
from tests.integration.conftest import (
    TestEnv,  # noqa: F401 - 类型注解
    logged_in,  # noqa: F401 - fixture
)

pytestmark = pytest.mark.w0_web

SUBJ = "subj_recall_scope"
NEEDLE_TEXT = "主人最喜欢的颜色是紫色"


async def _clear() -> None:
    async with session_scope() as session:
        await session.execute(delete(MemoryRow).where(MemoryRow.subject_id == SUBJ))
        await session.execute(
            delete(MemoryEpisode).where(MemoryEpisode.subject_id == SUBJ)
        )


def _noise(index: int, now: datetime) -> MemoryRow:
    return MemoryRow(
        id=f"mem_noise_{index:04d}",
        subject_id=SUBJ,
        kind="fact",
        text=f"无关的日常记录第 {index} 条",
        origin="user_statement",
        visibility="subject_private",
        confidence=0.9,
        source_event_ids=[],
        valid_from=now,
        consent_revision=1,
        retracted=False,
        retention_days=3650,
        created_at=now,
        updated_at=now,
    )


def _needle(now: datetime) -> MemoryRow:
    """一年没更新过、但高度相关的那条。"""
    old = now - timedelta(days=400)
    return MemoryRow(
        id="mem_needle",
        subject_id=SUBJ,
        kind="fact",
        text=NEEDLE_TEXT,
        origin="user_statement",
        visibility="subject_private",
        confidence=0.9,
        source_event_ids=[],
        valid_from=old,
        consent_revision=1,
        retracted=False,
        retention_days=3650,
        created_at=old,
        updated_at=old,  # ← 最旧：UPDATE 时间是候选池的排序键
    )


async def _seed(noise_count: int) -> None:
    now = datetime.now(UTC)
    async with session_scope() as session:
        session.add_all([_noise(i, now) for i in range(noise_count)])
        session.add(_needle(now))


async def _recall_ids() -> set[str]:
    async with session_scope() as session:
        result = await MemoryService(session).recall(
            subject_id=SUBJ,
            query="我最喜欢什么颜色",
            visibility_allow={Visibility.subject_private},
            limit=8,
        )
    return {hit.memory_id for hit in result.hits}


# ================================================== 一、候选池上限


@pytest.mark.xfail(
    reason=(
        "已知真实缺陷（本轮实测确认，未修）：候选池按 updated_at 倒序取前 500 条，"
        "所以库里有 500 条更新过的记忆时，一条一年没提过但高度相关的记忆"
        "永远进不了候选池——实测 500 条时召回、501 条时一条都不返回。"
        "修法方向：候选池要按相关度而非仅按时间选（例如"
        "「最近 N 条 ∪ 全文/向量命中的 M 条」的有界并集），不能只靠时间倒序截断。"
        "标 xfail 而不是删掉：这个用例就是缺陷的可复现形式，修好之后它会自动"
        "XPASS，那时应当去掉这个标记。详见 docs/REQUIREMENTS-AUDIT.md「记忆召回」一节。"
    ),
    strict=False,
)
async def test_old_but_relevant_memory_survives_the_candidate_pool(
    logged_in: TestEnv,  # noqa: F811
) -> None:
    """最旧的高相关记忆**必须**能被召回，不论库里有几百条更新的。

    ## 这条测试为什么是这个形状

    它用**同一份数据、唯一的变量是噪声条数**来对照：

    - 总数 ≤ 上限 → needle 在池内 → 召回；
    - 总数 = 上限 + 1 → needle 被挤出去 → **不召回**（这就是缺陷）。

    如果只写"501 条时要能召回"，实现者可以靠调大上限糊弄过去（治标）。
    写清"500 与 501 的分界"才能说明：**问题不是上限大小，而是排序依据**
    ——按 `updated_at` 选池，等于"越久没提到的事越容易被忘掉"，
    而这恰好与"重要的事应该记得更牢"相反。
    """
    await _clear()
    try:
        # 1) 恰好等于上限：needle 挤得进池子 → 应当召回
        await _seed(RECALL_CANDIDATE_LIMIT - 1)
        assert "mem_needle" in await _recall_ids(), (
            f"总数 {RECALL_CANDIDATE_LIMIT} 时最旧的高相关记忆就没被召回——"
            "那不是上限问题，是排序依据问题"
        )

        # 2) 超过上限一条：按 updated_at 选池会把 needle 挤出去
        await _clear()
        await _seed(RECALL_CANDIDATE_LIMIT)
        ids = await _recall_ids()
        assert "mem_needle" in ids, (
            f"库里有 {RECALL_CANDIDATE_LIMIT + 1} 条记忆时，最旧但高度相关的"
            f"「{NEEDLE_TEXT}」没有进候选池。候选池按 `updated_at` 选，"
            "所以越久没提到的事越容易被忘——与「重要的事该记得更牢」相反。"
            "修法：候选池要按**相关度**而非仅按时间选（例如先按时间取更大池子"
            "再打分，或按 BM25/向量召回取候选并集），不能只靠时间倒序截断。"
        )
    finally:
        await _clear()


# ================================================== 二、账号合并后的情节


async def test_merged_subject_episodes_are_recalled_too(logged_in: TestEnv) -> None:  # noqa: F811
    """A 合并进 B 之后，**断言和情节都要读得到**。

    原来只有断言记忆用了合并展开后的 `subjects`，情节用自己的 `subject_id`，
    于是同一个人两段记忆有一半是瞎的。修复前 `kinds={'fact': 1}`，
    修复后 `kinds={'episode': 1, 'fact': 1}`。
    """
    owner = logged_in.subject_id
    merged = "subj_recall_merged"
    now = datetime.now(UTC)

    async with session_scope() as session:
        await session.execute(delete(MemoryRow).where(MemoryRow.subject_id == owner))
        await session.execute(
            delete(MemoryEpisode).where(MemoryEpisode.subject_id == owner)
        )
        await session.execute(delete(Subject).where(Subject.id == merged))
        session.add(Subject(id=merged, display_name="旧账号", merged_into=owner))

        # 两条内容都属于 owner（B 侧），关键词相同
        session.add(
            MemoryRow(
                id="mem_merged_fact",
                subject_id=owner,
                kind="fact",
                text="主人养了一只叫团子的橘猫",
                origin="user_statement",
                visibility="subject_private",
                confidence=0.9,
                source_event_ids=[],
                valid_from=now,
                consent_revision=1,
                retracted=False,
                retention_days=3650,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            MemoryEpisode(
                id="ep_merged",
                subject_id=owner,
                window_id="win_merged",
                summary="主人说团子今天不肯吃饭",
                visibility="subject_private",
                origin="tool_receipt",
                created_at=now,
                archived_at=None,
            )
        )

    try:
        async with session_scope() as session:
            result = await MemoryService(session).recall(
                # **从被合并掉的旧账号提问**——这正是用户会遇到的情形：
                # 他在旧窗口里说话，那窗口绑的是已经合并掉的 subject。
                subject_id=merged,
                query="团子",
                visibility_allow={Visibility.subject_private},
                limit=8,
            )
        kinds = {hit.kind for hit in result.hits}
        assert "fact" in kinds, f"合并后断言读不到：{sorted(kinds)}"
        assert "episode" in kinds, (
            f"合并后情节读不到（只拿到 {sorted(kinds)}）——"
            "同一个人的两段记忆有一半是瞎的，用户只觉得「她记性不好」"
        )
    finally:
        async with session_scope() as session:
            await session.execute(
                delete(MemoryRow).where(MemoryRow.subject_id == owner)
            )
            await session.execute(
                delete(MemoryEpisode).where(MemoryEpisode.subject_id == owner)
            )
            await session.execute(delete(Subject).where(Subject.id == merged))
