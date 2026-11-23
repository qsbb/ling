#!/usr/bin/env python3
"""关系页验收用的合成数据：1000+ 主体，含同名不同账号与跨平台关联。

## 为什么走 `IdentityService.resolve_or_provision`

那是**真实产品路径**：群里见到一个没见过的账号，bridge 就调它建一个 known 级
subject、挂 `pending` 账号。关系页面对的就是这样一张表。直接 `INSERT` 能造出
更好看的数字，但那些数字没有经过真实的证明等级、`trust_level` 与账号绑定约束。

## 数据形状（刻意贴近真实场景）

- 1200+ 自然人；其中 40 组**同名**（每组 2 人），验证"同名按账号区分"；
- 每人 1~3 个号，跨平台：`napcat-2` / `napcat-3` / `onebot-default` / `qqbot`
  （同一平台多实例，验证平台标识归并）；
- 一部分账号 `pending`（未验证），验证"未验证"这一真实状态；
- 一部分 subject **没有姓名**（群里只拿到账号，没拿到昵称）；
- 一部分人真正**没有账号**（历史遗留/手工建的），验证空账号表达；
- 跨平台合并用真实 `merge_subjects` 产生，不伪造 `merged_into`。

## 用法

后端不必启动——它直接调领域服务，落在 `LING_DATABASE_URL` 指向的库与 schema。

    LING_DATABASE_URL=... LING_DB_SEARCH_PATH=acc_relations \
      python3 scripts/seed_relationship_acceptance.py --count 1200
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ling.domains.identity.service import IdentityService  # noqa: E402
from ling.infra.models import ActorAccount, Subject  # noqa: E402

# 40 个会被重复使用的姓名：每组两人同名，靠账号区分。
DUPLICATE_NAMES = [f"小明{n:02d}" for n in range(1, 41)]

# 平台实例名：同一平台可以有多个实例，列表上应归到同一平台标识。
CONNECTOR_INSTANCES = ["napcat-2", "napcat-3", "onebot-default", "qqbot"]


def _engine_for(database_url: str, schema: str | None):
    """建引擎，并把 `search_path` 显式设成目标 schema。

    服务端是这么做的（`ling.infra.db` 把 `LING_DB_SEARCH_PATH` 塞进
    `server_settings`）。种子脚本**必须用同一套设置**：否则它写进 `public`，
    而服务端读的是目标 schema——两边都"成功"，但看到的不是同一张表。
    这个坑的代价是一次完整的灌数白跑（实测）。
    """
    connect_args: dict = {}
    if schema:
        connect_args["server_settings"] = {"search_path": schema}
    return create_async_engine(database_url, connect_args=connect_args)


async def seed(
    database_url: str, count: int, groups: int, seed_value: int, schema: str | None = None
) -> dict:
    rng = random.Random(seed_value)
    engine = _engine_for(database_url, schema)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    names: list[str] = []
    cross_platform_merges = 0
    nameless = 0
    accountless = 0
    total_accounts = 0

    async with factory() as session:
        identity = IdentityService(session)

        async def provision(
            name: str | None,
            accounts: list[tuple[str, str, str]],
        ) -> str | None:
            """用真实建档路径造一个自然人，再按真实绑定路径挂齐它的号。

            `resolve_or_provision` 只建**第一个**号（这是产品里"见到新账号"的
            真实行为）；后续的号用 `bind_account` 显式绑到同一个人身上，
            这与人工绑定的语义一致，也保证 `proof` 是我们想要的等级。
            """
            if not accounts:
                return None
            first_instance, first_external, first_proof = accounts[0]
            subject_id = await identity.resolve_or_provision(
                first_instance, first_external, display_name=name
            )
            if subject_id is None:
                return None
            # resolve_or_provision 一律建 pending；按场景升到 proven
            await identity.bind_account(
                subject_id=subject_id,
                connector_instance=first_instance,
                external_account_id=first_external,
                proof=first_proof,
            )
            for instance, external, proof in accounts[1:]:
                await identity.bind_account(
                    subject_id=subject_id,
                    connector_instance=instance,
                    external_account_id=external,
                    proof=proof,
                )
            if name:
                await identity.rename_subject(subject_id, name)
            return subject_id

        # 1. 同名组：每组两人，平台不同、账号不同、一个已验证一个未验证
        for group in DUPLICATE_NAMES[:groups]:
            for variant in range(2):
                index = len(names)
                instance = CONNECTOR_INSTANCES[variant % len(CONNECTOR_INSTANCES)]
                await provision(
                    group,
                    [(instance, f"9{index:08d}", "proven" if variant == 0 else "pending")],
                )
                names.append(group)
                total_accounts += 1

        # 2. 普通人：1~3 个号，跨平台
        while len(names) < count:
            index = len(names)
            name = f"群友{index:05d}"
            picked: list[tuple[str, str, str]] = []
            used: set[str] = set()
            for _ in range(rng.choice([1, 1, 2, 2, 3])):
                instance = rng.choice(CONNECTOR_INSTANCES)
                if instance in used:
                    continue
                used.add(instance)
                proof = "proven" if rng.random() < 0.65 else "pending"
                picked.append((instance, f"3{index:07d}{len(picked)}", proof))
            await provision(name, picked)
            names.append(name)
            total_accounts += len(picked)

        # 3. 没有姓名：群里只拿到账号、没拿到昵称
        for index in range(15):
            subject_id = await provision(
                None, [(CONNECTOR_INSTANCES[index % 4], f"1{index:09d}", "pending")]
            )
            if subject_id:
                nameless += 1
            total_accounts += 1

        # 4. 真正没有账号的人（历史遗留 / 手工建的），列表要能表达"没有绑定账号"
        for _ in range(8):
            session.add(Subject(
                id=f"subj_manual_{uuid.uuid4().hex[:16]}",
                display_name="手工建的人",
                trust_level="known",
                merged_into=None,
            ))
            accountless += 1
        await session.commit()

        # 5. 跨平台合并：把不同平台的两两合并（真实 merge，可拆）
        rows = (
            await session.execute(
                select(Subject).where(Subject.merged_into.is_(None)).order_by(Subject.created_at.desc())
            )
        ).scalars().all()
        pool = [row.id for row in rows]
        for index in range(min(24, len(pool) // 3)):
            source = pool[index * 3]
            target = pool[index * 3 + 1]
            try:
                await identity.merge_subjects(source, target)
                cross_platform_merges += 1
            except ValueError:
                continue
        await session.commit()

        subjects_total = int((await session.execute(select(func.count()).select_from(Subject))).scalar() or 0)
        accounts_total = int(
            (await session.execute(select(func.count()).select_from(ActorAccount))).scalar() or 0
        )

    await engine.dispose()
    return {
        "named_subjects": len(names),
        "nameless_subjects": nameless,
        "accountless_subjects": accountless,
        "cross_platform_merges": cross_platform_merges,
        "subjects_total": subjects_total,
        "accounts_total": accounts_total,
        "planned_accounts": total_accounts,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=1200)
    parser.add_argument("--groups", type=int, default=40)
    parser.add_argument("--seed", type=int, default=20261123)
    parser.add_argument("--schema", default=os.environ.get("LING_DB_SEARCH_PATH"))
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "LING_DATABASE_URL", "postgresql+asyncpg://ling@127.0.0.1:55432/ling_test"
        ),
    )
    args = parser.parse_args(argv)

    result = asyncio.run(
        seed(args.database_url, args.count, args.groups, args.seed, args.schema)
    )
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
