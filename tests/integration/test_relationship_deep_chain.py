"""合并组展开不能有固定轮数上限，且成环时必须能停下来。

## 为什么这个文件存在

`IdentityService.subject_group()` 原来是 `for _ in range(5)`：注释写"一层足够，
多跑几轮容得下历史脏数据"。但连续合并会造出比 5 更长的链——`merge_subjects`
每次写 `source → target`，A→…→G 时从根展开要走 6 跳。定成 5 轮就漏掉最早的
一环。

验收实测（六次合并）：列表说「并入 6 个身份 / 8 个账号」（正确，列表用无上限的
递归 CTE），详情却只给 7 个账号——最早那个身份的两个号不见了。同一份数据两个
地方对不上。

## 这两条用例分别在测什么

1. **深链**：链长远超旧的固定上限，整组必须**完整**。用 7 个身份 / 12 个账号，
   逐 id 记录修复前后的账号集合，比的是**集合**不是计数——少一个 id 也能抓到。
2. **成环**：去掉轮数上限后，脏数据成环不能让展开死循环。环上的节点全部入组后
   就不再增长，自然终止。

第 1 条在"固定轮数"实现下必红、在"无上限"实现下应绿；第 2 条在没有任何终止
保证的实现下会挂住（超时），所以它防的是修第 1 条时引入的新风险。
"""

from __future__ import annotations

import pytest
from sqlalchemy import insert, update

from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

PATH = "/v1/accounts/subjects"


async def _seed_chain_subjects(specs: dict[str, int], *, prefix: str) -> dict[str, str]:
    """按 ``{名字: 账号数}`` 造 subject，返回 ``{名字: id}``（不建合并关系）。"""
    from ling.infra.db import session_scope
    from ling.infra.models import ActorAccount, Subject

    ids = {name: f"{prefix}_{name}" for name in specs}
    async with session_scope() as session:
        await session.execute(
            insert(Subject),
            [
                {
                    "id": subject_id,
                    "display_name": f"{prefix}{name}",
                    "proof_level": "password",
                    "trust_level": "known",
                    "merged_into": None,
                }
                for name, subject_id in ids.items()
            ],
        )
        accounts = []
        for name, count in specs.items():
            for index in range(count):
                accounts.append(
                    {
                        "id": f"acc_{ids[name]}_{index}",
                        "connector_instance": "napcat-deep",
                        "external_account_id": f"napcat-deep:{prefix}-{name}-{index}",
                        "subject_id": ids[name],
                        "proof": "proven",
                        "consent_revision": 0,
                        "revoked_at": None,
                    }
                )
        await session.execute(insert(ActorAccount), accounts)
    return ids


async def _account_ids(client, subject_id: str) -> set[str]:
    response = await client.get(f"{PATH}/{subject_id}/details", params={"section": "accounts"})
    assert response.status_code == 200, response.text
    return {row["account_id"] for row in response.json()["items"]}


async def _merge(client, source: str, target: str) -> None:
    response = await client.post(f"{PATH}/merge", json={"source_id": source, "target_id": target})
    assert response.status_code == 200, response.text


async def test_group_expands_beyond_any_fixed_depth(logged_in: TestEnv) -> None:
    """7 个身份串成一条链：整组必须完整，列表与详情同口径。

    链长 6 跳，超过旧的 `range(5)`。用集合比对，漏掉最早那一环的两个号也会被
    抓到——这正是验收报的形态。
    """
    # 每个身份账号数不同，避免"恰好相等"掩盖错误
    specs = {"n0": 2, "n1": 1, "n2": 3, "n3": 1, "n4": 2, "n5": 1, "n6": 1}
    ids = await _seed_chain_subjects(specs, prefix="deep")
    order = ["n0", "n1", "n2", "n3", "n4", "n5", "n6"]
    client = logged_in.client

    # 合并前先按 ID 记下每个身份的账号，复测时按集合比对
    before = {name: await _account_ids(client, ids[name]) for name in order}
    expected_all = set().union(*before.values())
    target = ids["n6"]
    expected_merged = expected_all - before["n6"]

    for source_name, target_name in zip(order[:-1], order[1:], strict=True):
        await _merge(client, ids[source_name], ids[target_name])

    response = await client.get(PATH, params={"q": target, "limit": 1})
    assert response.status_code == 200, response.text
    row, = response.json()["subjects"]
    assert row["subject_id"] == target

    listed = (row["merged_member_count"], row["merged_member_account_count"])
    assert listed == (len(order) - 1, len(expected_merged)), listed

    detail = await _account_ids(client, target)
    assert detail == expected_all, {
        "missing": sorted(expected_all - detail),
        "unexpected": sorted(detail - expected_all),
    }


async def test_cycle_does_not_hang_or_drop_members(logged_in: TestEnv) -> None:
    """成环的历史脏数据：展开必须终止，且环上节点都在组里。

    去掉轮数上限后，终止条件从"转够 5 圈"变成"集合不再增长"。这条钉住后者：
    若实现改成无终止条件的递归，这个用例会挂住（测试超时）而不是悄悄通过。
    """
    ids = await _seed_chain_subjects({"c0": 1, "c1": 1, "c2": 1}, prefix="cyc")
    from ling.infra.db import session_scope
    from ling.infra.models import Subject

    # 造环：c0 -> c1 -> c2 -> c0（绕过 merge API，那是产品不会产生的脏数据，
    # 但历史上存在过，服务必须能读得下去）
    async with session_scope() as session:
        for source, target in (("c0", "c1"), ("c1", "c2"), ("c2", "c0")):
            await session.execute(
                update(Subject).where(Subject.id == ids[source]).values(merged_into=ids[target])
            )

    from ling.domains.identity.service import IdentityService

    async with session_scope() as session:
        group = await IdentityService(session).subject_group(ids["c0"])
    # 环上的三个节点全部可达，且没有额外节点被拉进来
    assert group == {ids["c0"], ids["c1"], ids["c2"]}, group
