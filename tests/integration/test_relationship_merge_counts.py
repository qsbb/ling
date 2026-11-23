"""合并汇总（列表徽标）与详情（整组账号）必须是**同一个口径**。

## 为什么这个文件存在

验收连续合并时发现回归：A(2 个号) 并入 B(1 个号)，随后 B 并入 C(1 个号)，
两次都 200。只搜索 C 时列表返回 `merged_member_count=1`，而 C 的账号详情
包含 A、B、C 的 4 个账号——同一份数据两个地方各说各的。

根因是**口径不同**，不是数字算错：

- 列表只数**直接**指向本行的记录（`merged_into = 本行`）→ 只有 B；
- 详情走 `IdentityService.subject_group()`，沿链**传递**展开 → A、B、C。

修法是把列表改成传递闭包（一条递归 CTE），与详情同口径。这个文件把四种形状
都钉住，其中"连续合并"是唯一能区分两种实现的形状——单次合并下它们给出相同
结果，所以只测单次合并的用例证明不了这条不变量。
"""

from __future__ import annotations

import pytest
from sqlalchemy import insert

from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

PATH = "/v1/accounts/subjects"


async def _seed_exact(specs: dict[str, int], *, prefix: str) -> dict[str, str]:
    """按 ``{名字: 账号数}`` 精确造 subject，返回 ``{名字: subject_id}``。

    不借用 `_seed` 的"每 5 个挂 2 个号"规律：这里的断言要精确到账号数，
    种子的形状必须由用例自己控制，而不是从别人的取模规则里推出来。
    """
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
            subject_id = ids[name]
            for index in range(count):
                accounts.append(
                    {
                        "id": f"acc_{subject_id}_{index}",
                        "connector_instance": "napcat-chain",
                        "external_account_id": f"napcat-chain:{prefix}-{name}-{index}",
                        "subject_id": subject_id,
                        "proof": "proven",
                        "consent_revision": 0,
                        "revoked_at": None,
                    }
                )
        if accounts:
            await session.execute(insert(ActorAccount), accounts)
    return ids


async def _merge(client, source: str, target: str) -> None:
    response = await client.post(f"{PATH}/merge", json={"source_id": source, "target_id": target})
    assert response.status_code == 200, response.text


async def _list_row(client, subject_id: str) -> dict:
    """只召回这一个目标的列表行（`q=id` + `limit=1`），复现验收的取数方式。"""
    response = await client.get(PATH, params={"q": subject_id, "limit": 1})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1, body
    row, = body["subjects"]
    assert row["subject_id"] == subject_id, row
    return row


async def _detail_accounts(client, subject_id: str) -> list[dict]:
    response = await client.get(f"{PATH}/{subject_id}/details", params={"section": "accounts"})
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def _assert_consistent(client, subject_id: str) -> dict:
    """列表汇总必须与详情里的整组账号一致；返回这一对比对结果便于断言。"""
    row = await _list_row(client, subject_id)
    accounts = await _detail_accounts(client, subject_id)
    merged_subjects = {a["subject_id"] for a in accounts} - {subject_id}
    merged_accounts = sum(1 for a in accounts if a["subject_id"] != subject_id)
    listed = (row["merged_member_count"], row["merged_member_account_count"])
    assert listed == (len(merged_subjects), merged_accounts), (
        f"列表汇总 {listed} 与详情不一致"
        f"（详情：{len(merged_subjects)} 个身份 / {merged_accounts} 个账号）"
    )
    return {
        "listed": listed,
        "detail_subjects": len(merged_subjects),
        "detail_accounts": merged_accounts,
        "total_accounts": len(accounts),
    }


async def test_single_merge_matches_detail(logged_in: TestEnv) -> None:
    """单次合并：源挂 2 个号并入目标，汇总 = (1 个身份, 2 个账号)。"""
    ids = await _seed_exact({"source": 2, "target": 1}, prefix="m1")
    client = logged_in.client

    await _merge(client, ids["source"], ids["target"])
    result = await _assert_consistent(client, ids["target"])
    assert result["listed"] == (1, 2)
    assert result["total_accounts"] == 3


async def test_successive_merges_use_the_same_scope_as_detail(logged_in: TestEnv) -> None:
    """连续合并：A(2) → B(1) → C(1)，C 这一组应报 (2 个身份, 3 个账号)。

    这是唯一能区分"只数一层"与"传递展开"的形状：只数一层时 C 只看到 B，
    给出 (1, 1)，而详情给出 4 个账号。
    """
    ids = await _seed_exact({"a": 2, "b": 1, "c": 1}, prefix="chain")
    client = logged_in.client

    await _merge(client, ids["a"], ids["b"])
    await _merge(client, ids["b"], ids["c"])

    result = await _assert_consistent(client, ids["c"])
    assert result["listed"] == (2, 3), result
    assert result["total_accounts"] == 4

    # 链条中间那一环：B 自身也被合并走了，它这一行不该再声称拥有成员。
    middle = await _list_row(client, ids["b"])
    assert middle["merged_into"] == ids["c"]
    assert (middle["merged_member_count"], middle["merged_member_account_count"]) == (0, 0)


async def test_chain_search_returns_only_the_target_row(logged_in: TestEnv) -> None:
    """`q=目标` + `limit=1` 时只召回目标行，且它已经带着整组的汇总。

    紧耦合的一点：若汇总要靠"成员也出现在结果里"才能算，这条会直接失败——
    这正是早先"把成员折进目标行"的方案在搜索下凭空变空的原因。
    """
    ids = await _seed_exact({"a": 2, "b": 1, "c": 1}, prefix="chainq")
    client = logged_in.client
    await _merge(client, ids["a"], ids["b"])
    await _merge(client, ids["b"], ids["c"])

    response = await client.get(PATH, params={"q": ids["c"], "limit": 1})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["subjects"]) == 1
    assert body["subjects"][0]["subject_id"] == ids["c"]
    assert body["subjects"][0]["merged_member_count"] == 2
    assert body["subjects"][0]["merged_member_account_count"] == 3


async def test_unmerge_restores_counts_after_a_chain(logged_in: TestEnv) -> None:
    """拆开链里的一环后，两边的计数都回到各自真实的样子。

    A(2) → B(1) → C(1) 之后拆开 B：C 不再有成员；B 重新变成"拥有 A"的那个人
    （A 仍指向 B），所以 B 的汇总应为 (1 个身份, 2 个账号)。
    """
    ids = await _seed_exact({"a": 2, "b": 1, "c": 1}, prefix="unchain")
    client = logged_in.client
    await _merge(client, ids["a"], ids["b"])
    await _merge(client, ids["b"], ids["c"])

    unmerged = await client.post(f"{PATH}/{ids['b']}/unmerge")
    assert unmerged.status_code == 200, unmerged.text

    # C：没有任何成员了
    c_row = await _list_row(client, ids["c"])
    assert (c_row["merged_member_count"], c_row["merged_member_account_count"]) == (0, 0)
    c_accounts = await _detail_accounts(client, ids["c"])
    assert {a["subject_id"] for a in c_accounts} == {ids["c"]}

    # B：A 仍然指向它，所以 B 这一组是 A + B
    b_result = await _assert_consistent(client, ids["b"])
    assert b_result["listed"] == (1, 2), b_result
    assert b_result["total_accounts"] == 3

    # A 仍是被并入者，自己不声称拥有成员
    a_row = await _list_row(client, ids["a"])
    assert a_row["merged_into"] == ids["b"]
    assert (a_row["merged_member_count"], a_row["merged_member_account_count"]) == (0, 0)


async def test_counts_are_not_inflated_by_multiple_accounts(logged_in: TestEnv) -> None:
    """一个身份挂很多号时，身份数不会被账号数放大（DISTINCT 的必要性）。

    用 3 个身份各挂 2 个号：正确结果是 (3, 6)。若 JOIN 后不做 DISTINCT，
    身份数会变成 6——这条能区分两种实现。
    """
    ids = await _seed_exact({"x": 2, "y": 2, "z": 2, "keeper": 1}, prefix="multi")
    client = logged_in.client
    await _merge(client, ids["x"], ids["keeper"])
    await _merge(client, ids["y"], ids["keeper"])
    await _merge(client, ids["z"], ids["keeper"])

    result = await _assert_consistent(client, ids["keeper"])
    assert result["listed"] == (3, 6), result
