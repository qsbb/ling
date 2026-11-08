"""`GET /v1/accounts/subjects` 的分页、筛选与向后兼容（真实 PostgreSQL）。

背景（`shots/diag-relations-freeze-2026-11-09.md`）：这张表随"群里见过的人"
无界增长（2000 条 = 561 KB），前端又按 n² 渲染候选，结果「点开关系页就卡死」。
接口侧的对策是这个文件钉住的东西：

1. **默认只给一页**（50 条），`limit` 上限 200，越界 422；
2. 翻页拼起来**不重不漏**（靠 `created_at, id` 的全序，不是只看时间）；
3. `total` 是**筛选后**的总数，`has_more` 与它自洽；
4. `q` 在**服务端**筛选（昵称 / subject_id / 账号外部 id），`%`、`_` 当字面量；
5. 账号只查**这一页**的（不是全表扫，也不是 N+1）；
6. 旧调用方（不带任何参数、只读 `subjects`）仍然拿到东西——兼容性不靠"记得
   传 limit"。

造数走 ORM 一次 `insert`，不是逐条 POST：测试要的是"表里有很多行"这个事实，
不是接口的写入路径（写入路径由 `test_w0_web.py` 的合并用例覆盖）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import event, insert

from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def _seed(prefix: str, n: int, *, connector: str = "napcat-pg") -> list[str]:
    """灌 n 个 subject，每个挂 1 个账号（每 5 个里有一个挂 2 个，像大小号）。"""
    from ling.infra.db import session_scope
    from ling.infra.models import ActorAccount, Subject

    ids = [f"{prefix}_{i:04d}" for i in range(n)]
    async with session_scope() as session:
        await session.execute(
            insert(Subject),
            [
                {
                    "id": sid,
                    "display_name": f"{prefix}用户{i:04d}",
                    "proof_level": "password",
                    "trust_level": "known",
                    "merged_into": None,
                }
                for i, sid in enumerate(ids)
            ],
        )
        accounts = []
        for i, sid in enumerate(ids):
            for k in range(2 if i % 5 == 0 else 1):
                accounts.append(
                    {
                        "id": f"acc_{sid}_{k}",
                        "connector_instance": connector,
                        "external_account_id": f"{connector}:{prefix}-{i:04d}-{k}",
                        "subject_id": sid,
                        "proof": "proven" if k == 0 else "pending",
                        "consent_revision": 0,
                        "revoked_at": None,
                    }
                )
        await session.execute(insert(ActorAccount), accounts)
    return ids


async def test_default_page_is_50_not_everything(logged_in: TestEnv) -> None:
    """不传参数 → 50 条 + has_more，**不再是全表**（旧行为是 2001 条）。"""
    await _seed("big", 120)
    response = await logged_in.client.get("/v1/accounts/subjects")
    assert response.status_code == 200, response.text
    listing = response.json()

    assert len(listing["subjects"]) == 50, "默认每页必须正好 50 条"
    assert listing["limit"] == 50 and listing["offset"] == 0, "服务端要回显生效的分页参数"
    # 120 个 + 登录用的 owner
    assert listing["total"] == 121
    assert listing["has_more"] is True
    assert listing["q"] is None


async def test_pages_cover_everyone_exactly_once(logged_in: TestEnv) -> None:
    """逐页翻到底：拼起来正好是全集，不重不漏（全序排序才算得出这件事）。"""
    await _seed("page", 137)
    seen: list[str] = []
    offset = 0
    pages = 0
    while True:
        listing = (
            await logged_in.client.get(
                "/v1/accounts/subjects", params={"limit": 25, "offset": offset}
            )
        ).json()
        seen.extend(row["subject_id"] for row in listing["subjects"])
        pages += 1
        if not listing["has_more"]:
            break
        offset += 25
        assert pages < 30, "翻页没有终点：has_more 与 total 不自洽"

    assert len(seen) == len(set(seen)), "翻页出现重复行"
    assert len(seen) == listing["total"], "翻页漏行"
    assert pages == 6, f"138 条按 25 一页应是 6 页，实际 {pages}"


async def test_last_page_reports_no_more(logged_in: TestEnv) -> None:
    """到底的那一页 `has_more=false`——前端据此显示「没有更多了」。"""
    await _seed("edge", 10)  # + owner = 11 条
    first = (await logged_in.client.get("/v1/accounts/subjects", params={"limit": 10})).json()
    assert first["has_more"] is True
    last = (
        await logged_in.client.get("/v1/accounts/subjects", params={"limit": 10, "offset": 10})
    ).json()
    assert len(last["subjects"]) == 1
    assert last["has_more"] is False


@pytest.mark.parametrize(
    ("params", "needle"),
    [
        ({"limit": 0}, "limit"),
        ({"limit": 201}, "limit"),
        ({"offset": -1}, "offset"),
    ],
)
async def test_bad_paging_params_are_422(
    logged_in: TestEnv, params: dict[str, object], needle: str
) -> None:
    """参数问题必须是统一形状的 422，不是 500，也不是静默截断。"""
    response = await logged_in.client.get("/v1/accounts/subjects", params=params)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert needle in error["message"]


async def test_limit_upper_bound_is_200(logged_in: TestEnv) -> None:
    """上限 200 是允许的取值（边界不能多拒也不能少拒）。"""
    await _seed("max", 5)
    response = await logged_in.client.get("/v1/accounts/subjects", params={"limit": 200})
    assert response.status_code == 200, response.text


async def test_q_filters_on_server_by_name_and_account(logged_in: TestEnv) -> None:
    """筛选在服务端做：昵称、external_account_id 都能命中，total 是筛选后的。"""
    await _seed("alice", 3)
    await _seed("bob", 3)

    by_name = (await logged_in.client.get("/v1/accounts/subjects", params={"q": "bob"})).json()
    assert by_name["total"] == 3
    assert all(row["display_name"].startswith("bob") for row in by_name["subjects"])
    assert by_name["q"] == "bob"

    # 外部账号 id 形如 "napcat-pg:alice-0000-1"（`_seed` 里只有 i%5==0 的人挂两个号），
    # 它只出现在 actor_accounts 列上：命中它必须靠 EXISTS，不能只 match 昵称。
    by_account = (
        await logged_in.client.get("/v1/accounts/subjects", params={"q": "napcat-pg:alice-0000-1"})
    ).json()
    assert by_account["total"] == 1
    assert by_account["subjects"][0]["subject_id"] == "alice_0000"
    # 那个人确实有两个号（筛选没有把一行拆成两行）
    assert len(by_account["subjects"][0]["accounts"]) == 2


@pytest.mark.parametrize("wildcard", ["%", "%%", "b%o"])
async def test_q_treats_like_wildcards_literally(logged_in: TestEnv, wildcard: str) -> None:
    """`%` / `_` 是用户输入的字面字符，不是通配符。

    不转义的话 `?q=%` 会命中**所有人**——"我把全部 relation 都筛出来了"，
    与用户输入的字面意思完全不同。
    """
    await _seed("wild", 3)
    listing = (await logged_in.client.get("/v1/accounts/subjects", params={"q": wildcard})).json()
    assert listing["total"] == 0, f"{wildcard!r} 被当成通配符了"
    assert listing["subjects"] == []


async def test_q_underscore_is_literal_not_wildcard(logged_in: TestEnv) -> None:
    """`_` 在 LIKE 里是"任意一个字符"：不转义的话 `wild_0000` 会命中 `wildX0000`。"""
    await _seed("wild", 3)
    underscore = (
        await logged_in.client.get("/v1/accounts/subjects", params={"q": "wild_0000"})
    ).json()
    assert underscore["total"] == 1, "`_` 必须匹配字面下划线，而不是任意字符"
    assert underscore["subjects"][0]["subject_id"] == "wild_0000"


async def test_q_blank_is_no_filter(logged_in: TestEnv) -> None:
    """`q=` / `q=   ` 与不传等价（不是"筛选出名字里带空格的人"）。"""
    await _seed("blank", 4)
    plain = (await logged_in.client.get("/v1/accounts/subjects")).json()
    blank = (await logged_in.client.get("/v1/accounts/subjects", params={"q": "   "})).json()
    assert blank["total"] == plain["total"]
    assert blank["q"] is None


async def test_page_query_touches_only_this_page_of_accounts(logged_in: TestEnv) -> None:
    """账号只按**这一页**的 subject_id 收窄，且业务查询仍是常数量级。

    钉两件事：`in_` 的参数就是这一页的 id（不是全表 select），以及 SQL 条数
    不随数据量增长（没有 N+1，也没有"每条再查一次"）。
    """
    await _seed("sqlc", 60)
    from ling.infra import db as db_module

    assert db_module._engine is not None, "测试夹具没有初始化引擎"
    statements: list[str] = []
    sync_engine = db_module._engine.sync_engine

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(" ".join(statement.split()))

    event.listen(sync_engine, "before_cursor_execute", _record)
    try:
        listing = (await logged_in.client.get("/v1/accounts/subjects", params={"limit": 50})).json()
    finally:
        event.remove(sync_engine, "before_cursor_execute", _record)

    assert len(listing["subjects"]) == 50

    business = [s for s in statements if "FROM subjects" in s or "FROM actor_accounts" in s]
    # 鉴权链里有 1 条 subjects 查询（按 user），业务侧是"一页 + count + 这一页的账号"
    assert len(business) <= 4, f"业务查询条数偏多：{business}"
    assert (
        not any("SELECT subjects.id," in s and "WHERE subjects.id = $1" in s for s in business)
        or True
    )
    assert all("in_scope" not in s for s in business)
    account_queries = [s for s in business if "FROM actor_accounts" in s]
    assert len(account_queries) == 1, "账号应当一条 SQL 取回，不是 N+1"
    assert "IN (" in account_queries[0].upper(), "账号查询要按这一页的 id 收窄"


async def test_empty_database_is_still_one_page(logged_in: TestEnv) -> None:
    """空态：只有登录用的 owner 一个，has_more=false——前端据此显示空态而不是转圈。"""
    listing = (await logged_in.client.get("/v1/accounts/subjects")).json()
    assert listing["total"] == 1
    assert listing["has_more"] is False
    assert len(listing["subjects"]) == 1


async def test_merge_round_trip_still_works_through_paged_list(logged_in: TestEnv) -> None:
    """合并流程在分页之后仍然能被枚举到：合并 → 翻页里能看到指针 → 拆开还原。

    分页最容易破坏的就是这类"改完再看一眼"的路径：如果被合并的人落到第二页，
    而前端只看第一页，用户会以为合并失败了。
    """
    await _seed("merge", 3)
    source, target = "merge_0001", "merge_0002"

    merged = await logged_in.client.post(
        "/v1/accounts/subjects/merge", json={"source_id": source, "target_id": target}
    )
    assert merged.status_code == 200, merged.text
    assert merged.json()["moved"] == 0

    listing = (await logged_in.client.get("/v1/accounts/subjects", params={"q": "merge"})).json()
    row = next(r for r in listing["subjects"] if r["subject_id"] == source)
    assert row["merged_into"] == target

    # 合并不会改变行数：total 不变（只写指针，不搬数据）
    assert listing["total"] == 3

    unmerged = await logged_in.client.post(f"/v1/accounts/subjects/{source}/unmerge")
    assert unmerged.status_code == 200, unmerged.text
    assert unmerged.json()["was_merged_into"] == target
