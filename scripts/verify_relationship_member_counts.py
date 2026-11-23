#!/usr/bin/env python3
"""验证「并入 N 个身份 / N 个账号」两个计数在真实数据下的正确性。

针对验收反馈：一个被并入的身份挂了两个账号时，列表徽标显示「已并入 1 个号」，
而详情里并入了两个账号。这里把三种形状都造出来，直接比对接口返回值：

1. **一个身份挂两个号**（验收复现的那个）：身份数 1、账号数 2；
2. **两个身份各挂一个号**：身份数 2、账号数 2 —— 只数身份时它和情形 1 相同，
   只数账号时也相同，所以**必须两个数都对**才能区分它们；
3. **一个身份没挂号**：身份数 1、账号数 0 —— 验证 LEFT JOIN 没有把它数丢。

情形 1 与 2 在"只用单一数字"的实现下会给出相同输出，这正是这条脚本的区分力：
任何把两个数混为一谈的实现，都会在其中一种上给出错误答案。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from ling.domains.identity.service import IdentityService  # noqa: E402
from ling.infra.models import Subject  # noqa: E402


def engine_for(database_url: str):
    return create_async_engine(database_url)


async def provision(session, identity, name: str, accounts: list[tuple[str, str]]) -> str:
    first_instance, first_external = accounts[0]
    subject_id = await identity.resolve_or_provision(first_instance, first_external, display_name=name)
    for instance, external in accounts[1:]:
        await identity.bind_account(
            subject_id=subject_id,
            connector_instance=instance,
            external_account_id=external,
            proof="proven",
        )
    await identity.rename_subject(subject_id, name)
    return subject_id


async def run(database_url: str) -> int:
    engine = engine_for(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        identity = IdentityService(session)

        # 情形 1：一个被并入的身份，挂两个号
        keeper1 = await provision(session, identity, "保留者一", [("napcat-9", "kc1-0001")])
        member1 = await provision(
            session, identity, "并入者一", [("napcat-9", "mm1-0001"), ("onebot-9", "mm1-0002")]
        )
        await identity.merge_subjects(member1, keeper1)

        # 情形 2：两个被并入的身份，各挂一个号
        keeper2 = await provision(session, identity, "保留者二", [("napcat-9", "kc2-0001")])
        for index in (1, 2):
            member = await provision(
                session, identity, f"并入者二{index}", [("napcat-9", f"mm2-000{index}")]
            )
            await identity.merge_subjects(member, keeper2)

        # 情形 3：被并入的身份一个号都没有
        keeper3 = await provision(session, identity, "保留者三", [("napcat-9", "kc3-0001")])
        empty_member = f"subj_empty_member_{os.getpid()}"
        session.add(
            Subject(id=empty_member, display_name="没有号的并入者", trust_level="known", merged_into=None)
        )
        await session.flush()
        await identity.merge_subjects(empty_member, keeper3)

        await session.commit()

        # 直接从接口的同一份查询逻辑取数
        from ling.api.routes.manage import _merged_member_counts

        counts = await _merged_member_counts(session, [keeper1, keeper2, keeper3])

        cases = [
            ("一个身份挂两个号", keeper1, (1, 2)),
            ("两个身份各挂一个号", keeper2, (2, 2)),
            ("一个身份没挂号", keeper3, (1, 0)),
        ]
        failures = 0
        for label, subject_id, expected in cases:
            actual = counts.get(subject_id, (0, 0))
            ok = actual == expected
            failures += 0 if ok else 1
            print(
                f"{'OK  ' if ok else 'FAIL'} {label}: 身份数 {actual[0]}（期望 {expected[0]}）, "
                f"账号数 {actual[1]}（期望 {expected[1]}）"
            )

        # 情形 1 与情形 2 的区分力：只输出单一数字时它们必然相同
        one_identity_only = counts.get(keeper1, (0, 0))[0]
        two_identities_only = counts.get(keeper2, (0, 0))[0]
        print(
            f"\n区分力检查：只数身份时 情形1={one_identity_only} 情形2={two_identities_only}"
            "（不同 → 单数字实现无法同时满足两者）"
        )

    await engine.dispose()
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "LING_DATABASE_URL", "postgresql+asyncpg://ling@127.0.0.1:55432/ling_test"
        ),
    )
    args = parser.parse_args(argv)
    return asyncio.run(run(args.database_url))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
