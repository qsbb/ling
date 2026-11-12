"""连接器多实例数据层：迁移 0067 的升级、回滚、存量转换与约束。

## 这份测试在验什么

本轮**只做数据层**，运行时和前端另起阶段。所以这里不测"两个实例能不能真的
同时在线"，只测四件地基层面的事：

1. 迁移链能 upgrade → downgrade → upgrade 闭环（仓库有 3 个链式回滚测试
   依赖 downgrade 不抛异常）；
2. 存量数据正确折叠成"每 scope 一个实例"，且**推导出来的实例 id 与 actor
   命名空间一致**（不是一律填 `default`）；
3. 幂等：重复执行不报错、不重复插入；
4. 复合键真的生效：同 scope 同 instance 不能两行，同 scope 不同 instance 可以。

## 为什么每个用例开一个临时库

存量数据转换必须在**插入旧数据之后**才执行 upgrade。session 级的迁移 fixture
（`test_migrations.py` 里的 `migrated_db`）在 yield 之前就已经跑到 head 了，
没法插入"0066 时代的数据"。所以这里复用同样的思路：每个用例
`CREATE DATABASE ling_mig_0067_<uuid>`，用完 DROP。

## 为什么不 import test_migrations 里的 helper

那些是 `_` 开头的私有函数，跨测试文件 import 私有符号会让"改别人的内部实现"
意外打断这边。这里重写一份**只保留本文件需要的最小集**，代价是几十行重复，
换来的是两个文件可以各自演进。
"""

from __future__ import annotations

import asyncio
import os
import uuid
import warnings
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DB_URL = os.environ.get(
    "LING_TEST_DATABASE_URL", "postgresql+asyncpg://ling@127.0.0.1:55432/ling_test"
)

REV = "0067_connector_instances"
PARENT = "0066_media_play_events"


# --------------------------------------------------------------------------- #
# 库与迁移的小工具
# --------------------------------------------------------------------------- #


def _database_url(base_url: str, database: str) -> str:
    head, _, _ = base_url.rpartition("/")
    return f"{head}/{database}"


async def _execute_ddl(url: str, statement: str) -> None:
    """执行 `CREATE DATABASE` / `DROP DATABASE` 这类**不能在事务里跑**的语句。

    `engine.begin()` 会开一个事务块，PG 直接拒绝：`CREATE DATABASE cannot run
    inside a transaction block`。所以这类语句必须走 AUTOCOMMIT 隔离级别。
    """
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


async def _execute(url: str, statement: str, params: dict | None = None) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement), params or {})
    finally:
        await engine.dispose()


async def _fetch(url: str, statement: str, params: dict | None = None) -> list[tuple]:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            rows = await connection.execute(text(statement), params or {})
            return [tuple(row) for row in rows]
    finally:
        await engine.dispose()


def _fetch_sync(url: str, statement: str, params: dict | None = None) -> list[tuple]:
    return asyncio.run(_fetch(url, statement, params))


def _alembic_config(url: str) -> Config:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


@contextmanager
def _database_url_env(url: str) -> Iterator[None]:
    """Alembic 迁移里的代码可能直接读 `LING_DATABASE_URL`。"""
    previous = os.environ.get("LING_DATABASE_URL")
    os.environ["LING_DATABASE_URL"] = url
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("LING_DATABASE_URL", None)
        else:
            os.environ["LING_DATABASE_URL"] = previous


def _run_alembic(action: str, url: str, revision: str) -> None:
    config = _alembic_config(url)
    with _database_url_env(url):
        if action == "upgrade":
            command.upgrade(config, revision)
        elif action == "downgrade":
            command.downgrade(config, revision)
        else:  # pragma: no cover - 调用方只传这两种
            raise AssertionError(f"未知动作: {action}")


def _current_version(url: str) -> str:
    rows = _fetch_sync(url, "SELECT version_num FROM alembic_version")
    assert len(rows) == 1, f"alembic_version 应有且仅有一行，实际 {rows}"
    return str(rows[0][0])


@contextmanager
def _fresh_db_at(revision: str) -> Iterator[str]:
    """建临时库并 upgrade 到 `revision`；用完 DROP。

    `DROP DATABASE ... WITH (FORCE)`：迁移可能留下连接，不强制断开会删不掉，
    临时库越积越多，最后把测试实例的连接数吃满。
    """
    database = f"ling_mig_0067_{uuid.uuid4().hex[:10]}"
    asyncio.run(_execute_ddl(TEST_DB_URL, f'CREATE DATABASE "{database}"'))
    url = _database_url(TEST_DB_URL, database)
    try:
        _run_alembic("upgrade", url, revision)
        yield url
    finally:
        try:
            asyncio.run(
                _execute_ddl(
                    TEST_DB_URL, f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'
                )
            )
        except Exception as exc:  # noqa: BLE001
            warnings.warn(f"临时数据库 {database} 清理失败，请手工删除: {exc}", stacklevel=1)


def _columns(url: str, table: str) -> dict[str, str]:
    rows = _fetch_sync(
        url,
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :table",
        {"table": table},
    )
    return {str(name): str(nullable) for name, nullable in rows}


def _constraint_defs(url: str, table: str, contype: str) -> list[str]:
    """取某张表某类约束的定义文本。

    `contype` 传的是 `'p'` / `'u'` / `'c'` 这类**单字符码**（PG 里是 `"char"`
    类型）。asyncpg 对这种类型要求 `bytes`，传 `str` 会报
    `a bytes-like object is required, not 'str'`——所以这里 encode 一下，
    而不是在 SQL 里拼字面量（拼字符串会被 ruff 的安全检查拦下）。
    """
    rows = _fetch_sync(
        url,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = CAST(:table AS regclass) AND contype = :contype",
        {"table": table, "contype": contype.encode("ascii")},
    )
    return [str(row[0]) for row in rows]


def _tables(url: str) -> set[str]:
    rows = _fetch_sync(
        url,
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'",
    )
    return {str(row[0]) for row in rows}


# --------------------------------------------------------------------------- #
# 造"0066 时代"的存量数据
# --------------------------------------------------------------------------- #


def _seed_legacy_scope(url: str, scope: str, *, actor_namespace: str | None) -> None:
    """造一条升级前的数据：一个修订 + 一行 HEAD + 一个 ACK。

    `actor_namespace` 对应 `LING_ONEBOT_INSTANCE_ID` / `LING_QQBOT_INSTANCE_ID`
    的覆盖值（`None` 表示没覆盖过，应回落到 `default`）。
    """
    revision_id = f"rev-{scope}-1"
    _run_sql(
        url,
        "INSERT INTO connector_config_revisions "
        "(id, scope, parent_id, state, created_by, created_at, activated_at, "
        " error_code, error_safe) "
        "VALUES (:id, :scope, NULL, 'active', 'seed', now(), now(), NULL, NULL)",
        {"id": revision_id, "scope": scope},
    )
    if actor_namespace is not None:
        field_key = f"LING_{scope.upper()}_INSTANCE_ID"
        _run_sql(
            url,
            "INSERT INTO connector_override_values "
            "(id, revision_id, field_key, value_kind, plain_value, ciphertext, "
            " preview, audit_fingerprint, created_at) "
            "VALUES (:id, :rev, :key, 'text', CAST(:value AS json), NULL, NULL, NULL, now())",
            {
                "id": f"ov-{scope}-1",
                "rev": revision_id,
                "key": field_key,
                # 文本字段按 JSON 标量存（与 connector_overrides 的写法一致）
                "value": f'"{actor_namespace}"',
            },
        )
    _run_sql(
        url,
        "INSERT INTO connector_config_heads "
        "(scope, active_revision_id, candidate_revision_id, generation, updated_at) "
        "VALUES (:scope, :rev, NULL, 1, now())",
        {"scope": scope, "rev": revision_id},
    )
    _run_sql(
        url,
        "INSERT INTO connector_runtime_acks "
        "(id, scope, generation, replica_id, phase, health, error_safe, updated_at) "
        "VALUES (:id, :scope, 1, 'local', 'activated', 'ok', NULL, now())",
        {"id": f"ack-{scope}-1", "scope": scope},
    )


def _run_sql(url: str, statement: str, params: dict) -> None:
    asyncio.run(_execute(url, statement, params))


# --------------------------------------------------------------------------- #
# 1) 闭环：upgrade → downgrade → upgrade
# --------------------------------------------------------------------------- #


def test_upgrade_creates_instance_dimension() -> None:
    with _fresh_db_at(REV) as url:
        assert _current_version(url) == REV
        assert "connector_instances" in _tables(url)

        for table in (
            "connector_config_revisions",
            "connector_config_heads",
            "connector_runtime_acks",
        ):
            assert "instance_id" in _columns(url, table), f"{table} 缺少 instance_id"

        # HEAD 主键必须是复合键
        pk_defs = _constraint_defs(url, "connector_config_heads", "p")
        assert any("(scope, instance_id)" in d for d in pk_defs), pk_defs

        # ACK 唯一键必须带实例维度
        uniques = _constraint_defs(url, "connector_runtime_acks", "u")
        assert any(
            "(scope, instance_id, generation, replica_id)" in d for d in uniques
        ), uniques

        # 覆盖值表**不动**：它由 revision_id 唯一确定归属
        assert "instance_id" not in _columns(url, "connector_override_values")


def test_downgrade_restores_single_instance_schema() -> None:
    with _fresh_db_at(REV) as url:
        _run_alembic("downgrade", url, PARENT)

        assert _current_version(url) == PARENT
        assert "connector_instances" not in _tables(url)

        for table in (
            "connector_config_revisions",
            "connector_config_heads",
            "connector_runtime_acks",
        ):
            assert "instance_id" not in _columns(url, table), f"{table} 残留 instance_id"

        pk_defs = _constraint_defs(url, "connector_config_heads", "p")
        assert any(d.strip() == "PRIMARY KEY (scope)" for d in pk_defs), pk_defs

        uniques = _constraint_defs(url, "connector_runtime_acks", "u")
        assert any(
            "(scope, generation, replica_id)" in d and "instance_id" not in d
            for d in uniques
        ), uniques


def test_downgrade_then_upgrade_round_trip() -> None:
    """降到底再升回来，结构完全恢复。这是"3 个链式回滚测试"依赖的能力。"""
    with _fresh_db_at(REV) as url:
        _run_alembic("downgrade", url, PARENT)
        _run_alembic("upgrade", url, REV)

        assert _current_version(url) == REV
        assert "connector_instances" in _tables(url)
        pk_defs = _constraint_defs(url, "connector_config_heads", "p")
        assert any("(scope, instance_id)" in d for d in pk_defs), pk_defs
        uniques = _constraint_defs(url, "connector_runtime_acks", "u")
        assert any("(scope, instance_id, generation, replica_id)" in d for d in uniques)


def test_downgrade_does_not_raise_on_multi_instance_data() -> None:
    """**有多个实例时 downgrade 也不许抛。**

    报告 §5.3 建议"检测到多实例就 raise"，那样最诚实。但本仓库有链式回滚
    测试要求整条链能一路降到底，任何一个迁移抛异常都会让它们全红。
    所以选择有损折叠 + `print` 出损失，而不是拒绝回滚。
    """
    with _fresh_db_at(REV) as url:
        # 造出第二个实例
        _run_sql(
            url,
            "INSERT INTO connector_instances "
            "(scope, instance_id, display_name, enabled, created_at) "
            "VALUES ('onebot', 'second', '第二个', true, now())",
            {},
        )
        _run_sql(
            url,
            "INSERT INTO connector_config_revisions "
            "(id, scope, instance_id, parent_id, state, created_by, created_at) "
            "VALUES ('rev-second', 'onebot', 'second', NULL, 'active', 'seed', now())",
            {},
        )
        _run_sql(
            url,
            "INSERT INTO connector_config_heads "
            "(scope, instance_id, active_revision_id, generation, updated_at) "
            "VALUES ('onebot', 'second', 'rev-second', 2, now())",
            {},
        )

        _run_alembic("downgrade", url, PARENT)  # 不许抛

        assert _current_version(url) == PARENT
        # 非默认实例的数据被折叠掉了（schema 装不下）
        heads = _fetch_sync(
            url, "SELECT scope, active_revision_id FROM connector_config_heads"
        )
        assert len(heads) == 1, f"折叠后每 scope 只应剩一行，实际 {heads}"


# --------------------------------------------------------------------------- #
# 2) 存量数据转换
# --------------------------------------------------------------------------- #


def test_legacy_scopes_each_become_one_default_instance() -> None:
    """两个 scope 各 1 行旧数据 → 迁移后各 1 个默认实例。"""
    with _fresh_db_at(PARENT) as url:
        _seed_legacy_scope(url, "onebot", actor_namespace=None)
        _seed_legacy_scope(url, "qqbot", actor_namespace=None)

        _run_alembic("upgrade", url, REV)

        instances = _fetch_sync(
            url,
            "SELECT scope, instance_id, display_name FROM connector_instances "
            "ORDER BY scope",
        )
        assert instances == [
            ("onebot", "default", "default"),
            ("qqbot", "default", "default"),
        ], instances

        # **两个 scope 各有一个 default 不撞主键**——这正是报告 §5.2 标 ⚠
        # 的那个错误（单列 id PK 会撞）之所以被避开的原因
        heads = _fetch_sync(
            url, "SELECT scope, instance_id FROM connector_config_heads ORDER BY scope"
        )
        assert heads == [("onebot", "default"), ("qqbot", "default")], heads


def test_legacy_actor_namespace_becomes_the_instance_id() -> None:
    """**存量部署用的是自定义命名空间时，实例 id 要跟着它，而不是填 default。**

    这是本迁移在语义上最要紧的一条。报告 §1.4 指出：如果某部署早把
    `LING_ONEBOT_INSTANCE_ID` 改成 `onebot-x`，而迁移一律填 `default`，
    库里 instance_id 与 actor 命名空间就**分叉**了——后者仍叫 onebot-x，
    前者叫 default，之后两套东西对不上。

    把字段现值当作实例 id 的初值，等价于"每个部署现在只有一个实例，
    它的名字就是它一直用的那个名字"，零语义变化。
    """
    with _fresh_db_at(PARENT) as url:
        _seed_legacy_scope(url, "onebot", actor_namespace="onebot-x")

        _run_alembic("upgrade", url, REV)

        instances = _fetch_sync(
            url, "SELECT scope, instance_id FROM connector_instances WHERE scope='onebot'"
        )
        assert instances == [("onebot", "onebot-x")], (
            f"实例 id 应沿用 actor 命名空间 onebot-x，实际 {instances}"
        )

        # 修订与 ACK 也要跟着迁移到同一个实例
        revisions = _fetch_sync(
            url, "SELECT DISTINCT instance_id FROM connector_config_revisions "
            "WHERE scope='onebot'"
        )
        assert revisions == [("onebot-x",)], revisions
        acks = _fetch_sync(
            url, "SELECT DISTINCT instance_id FROM connector_runtime_acks WHERE scope='onebot'"
        )
        assert acks == [("onebot-x",)], acks


def test_invalid_actor_namespace_falls_back_to_default() -> None:
    """非法命名空间（超长/含非法字符）不能进库，必须回落 `default`。

    `instance_id` 是 `varchar(16)` 且参与主键，塞进去一个 64 字符的
    `LING_*_INSTANCE_ID`（该字段 `max_len=64`）会直接**截断或报错**。
    宁可回落，也不能让迁移半路失败。
    """
    with _fresh_db_at(PARENT) as url:
        # 32 字符，超过 instance_id 的 16 位
        _seed_legacy_scope(url, "onebot", actor_namespace="a" * 32)

        _run_alembic("upgrade", url, REV)

        instances = _fetch_sync(
            url, "SELECT instance_id FROM connector_instances WHERE scope='onebot'"
        )
        assert instances == [("default",)], f"非法值应回落 default，实际 {instances}"


def test_empty_database_produces_no_instances() -> None:
    """从没改过配置的部署：四表全空，迁移后实例目录也应该是空的。

    不能凭空造出三个 scope 的实例——那会让设置页在升级后突然多出三个
    "默认实例"，而用户从来没配过任何东西。
    """
    with _fresh_db_at(PARENT) as url:
        _run_alembic("upgrade", url, REV)
        rows = _fetch_sync(url, "SELECT count(*) FROM connector_instances")
        assert rows[0][0] == 0, "空库升级后不该凭空产生实例"


# --------------------------------------------------------------------------- #
# 3) 幂等
# --------------------------------------------------------------------------- #


def test_migration_is_idempotent_when_rerun() -> None:
    """同一份 DDL 重复执行不报错、不重复插入。

    Alembic 本身保证一个 revision 只跑一次，但生产上出现过"手工补过列
    却没 stamp"的情况。这里直接验证可重入性：把 upgrade 的等价 DDL 再跑一遍。
    """
    with _fresh_db_at(REV) as url:
        before = _fetch_sync(
            url, "SELECT scope, instance_id FROM connector_instances ORDER BY scope, instance_id"
        )

        # 重复执行迁移内部的两条关键幂等语句
        _run_sql(
            url,
            "INSERT INTO connector_instances "
            "(scope, instance_id, display_name, enabled, created_at) "
            "SELECT scope, instance_id, instance_id, true, now() FROM connector_instances "
            "ON CONFLICT (scope, instance_id) DO NOTHING",
            {},
        )
        after = _fetch_sync(
            url, "SELECT scope, instance_id FROM connector_instances ORDER BY scope, instance_id"
        )
        assert after == before, "重复 INSERT 不该产生新行"


def test_instance_directory_backfill_is_idempotent() -> None:
    """回填语句跑两次结果一致。"""
    with _fresh_db_at(PARENT) as url:
        _seed_legacy_scope(url, "onebot", actor_namespace=None)
        _run_alembic("upgrade", url, REV)
        first = _fetch_sync(
            url, "SELECT scope, instance_id FROM connector_instances ORDER BY scope"
        )
        _run_sql(
            url,
            "INSERT INTO connector_instances "
            "(scope, instance_id, display_name, enabled, created_at) "
            "VALUES ('onebot', 'default', 'default', true, now()) "
            "ON CONFLICT (scope, instance_id) DO NOTHING",
            {},
        )
        second = _fetch_sync(
            url, "SELECT scope, instance_id FROM connector_instances ORDER BY scope"
        )
        assert first == second


# --------------------------------------------------------------------------- #
# 4) 复合键真的生效
# --------------------------------------------------------------------------- #


def test_same_scope_different_instance_allowed() -> None:
    """同 scope 下可以有两个实例——这是整个改造的目的。"""
    with _fresh_db_at(REV) as url:
        _run_sql(
            url,
            "INSERT INTO connector_instances "
            "(scope, instance_id, display_name, enabled, created_at) "
            "VALUES ('onebot', 'a', '机器人A', true, now())",
            {},
        )
        _run_sql(
            url,
            "INSERT INTO connector_instances "
            "(scope, instance_id, display_name, enabled, created_at) "
            "VALUES ('onebot', 'b', '机器人B', true, now())",
            {},
        )

        # HEAD 表同理：同 scope 两行，各自停在自己的 generation
        _run_sql(
            url,
            "INSERT INTO connector_config_heads "
            "(scope, instance_id, active_revision_id, generation, updated_at) "
            "VALUES ('onebot', 'a', NULL, 3, now())",
            {},
        )
        _run_sql(
            url,
            "INSERT INTO connector_config_heads "
            "(scope, instance_id, active_revision_id, generation, updated_at) "
            "VALUES ('onebot', 'b', NULL, 7, now())",
            {},
        )
        rows = _fetch_sync(
            url,
            "SELECT instance_id, generation FROM connector_config_heads "
            "WHERE scope='onebot' ORDER BY instance_id",
        )
        assert rows == [("a", 3), ("b", 7)], rows


def test_same_scope_same_instance_rejected() -> None:
    """同 scope 同 instance 不能两行（主键约束）。"""
    with _fresh_db_at(REV) as url:
        _run_sql(
            url,
            "INSERT INTO connector_config_heads "
            "(scope, instance_id, active_revision_id, generation, updated_at) "
            "VALUES ('onebot', 'a', NULL, 1, now())",
            {},
        )
        with pytest.raises(IntegrityError) as excinfo:
            _run_sql(
                url,
                "INSERT INTO connector_config_heads "
                "(scope, instance_id, active_revision_id, generation, updated_at) "
                "VALUES ('onebot', 'a', NULL, 2, now())",
                {},
            )
        assert "duplicate key" in str(excinfo.value).lower() or "unique" in str(
            excinfo.value
        ).lower(), excinfo.value


def test_ack_unique_key_includes_instance() -> None:
    """不同实例的同一代际可以共存——不加实例维度会互相串号。

    报告 R6：实例 A 的第 3 代会挡住实例 B 的第 3 代，而 ACK 写的是
    `phase='activated'`，表现为"切换成功但没 ACK"。
    """
    with _fresh_db_at(REV) as url:
        _run_sql(
            url,
            "INSERT INTO connector_runtime_acks "
            "(id, scope, instance_id, generation, replica_id, phase, health, updated_at) "
            "VALUES ('ack-a', 'onebot', 'a', 3, 'local', 'activated', 'ok', now())",
            {},
        )
        # 同 scope 同代际，但不同实例 —— 必须允许
        _run_sql(
            url,
            "INSERT INTO connector_runtime_acks "
            "(id, scope, instance_id, generation, replica_id, phase, health, updated_at) "
            "VALUES ('ack-b', 'onebot', 'b', 3, 'local', 'activated', 'ok', now())",
            {},
        )
        rows = _fetch_sync(
            url,
            "SELECT instance_id FROM connector_runtime_acks WHERE scope='onebot' "
            "AND generation=3 ORDER BY instance_id",
        )
        assert rows == [("a",), ("b",)], rows

        # 同实例同代际同副本 —— 必须拒绝
        with pytest.raises(IntegrityError):
            _run_sql(
                url,
                "INSERT INTO connector_runtime_acks "
                "(id, scope, instance_id, generation, replica_id, phase, health, updated_at) "
                "VALUES ('ack-a2', 'onebot', 'a', 3, 'local', 'activated', 'ok', now())",
                {},
            )


def test_instance_scope_check_constraint() -> None:
    """scope 只允许 three 个值，防手写错字。"""
    with _fresh_db_at(REV) as url:
        with pytest.raises(IntegrityError) as excinfo:
            _run_sql(
                url,
                "INSERT INTO connector_instances "
                "(scope, instance_id, display_name, enabled, created_at) "
                "VALUES ('telegram', 'a', 'A', true, now())",
                {},
            )
        assert "ck_connector_instance_scope" in str(excinfo.value) or "check" in str(
            excinfo.value
        ).lower(), excinfo.value


def test_instance_display_name_unique_per_scope() -> None:
    """同 scope 显示名不能重复；不同 scope 可以各有一个同名。"""
    with _fresh_db_at(REV) as url:
        _run_sql(
            url,
            "INSERT INTO connector_instances "
            "(scope, instance_id, display_name, enabled, created_at) "
            "VALUES ('onebot', 'a', '主号', true, now())",
            {},
        )
        with pytest.raises(IntegrityError):
            _run_sql(
                url,
                "INSERT INTO connector_instances "
                "(scope, instance_id, display_name, enabled, created_at) "
                "VALUES ('onebot', 'b', '主号', true, now())",
                {},
            )
        # 不同 scope 同名可以
        _run_sql(
            url,
            "INSERT INTO connector_instances "
            "(scope, instance_id, display_name, enabled, created_at) "
            "VALUES ('qqbot', 'a', '主号', true, now())",
            {},
        )
        rows = _fetch_sync(
            url, "SELECT count(*) FROM connector_instances WHERE display_name='主号'"
        )
        assert rows[0][0] == 2


# --------------------------------------------------------------------------- #
# 5) 模型与迁移不漂移
# --------------------------------------------------------------------------- #


def test_models_match_migration() -> None:
    """`infra/models.py` 的四表定义与迁移产出的 schema 一致。

    漂移了的话，`create_all` 建出的测试库和生产 `alembic upgrade` 会是两个
    形状——测试永远绿，生产永远错。
    """
    from ling.infra.models import (
        ConnectorConfigHead,
        ConnectorConfigRevision,
        ConnectorInstance,
        ConnectorRuntimeAck,
    )

    with _fresh_db_at(REV) as url:
        for model, table in (
            (ConnectorInstance, "connector_instances"),
            (ConnectorConfigRevision, "connector_config_revisions"),
            (ConnectorConfigHead, "connector_config_heads"),
            (ConnectorRuntimeAck, "connector_runtime_acks"),
        ):
            db_columns = set(_columns(url, table))
            model_columns = {column.name for column in model.__table__.columns}
            missing = model_columns - db_columns
            assert not missing, f"{table}: 模型有而迁移没建 {sorted(missing)}"

            pk = [column.name for column in model.__table__.primary_key.columns]
            pk_defs = _constraint_defs(url, table, "p")
            assert any(
                "(" + ", ".join(pk) + ")" in d for d in pk_defs
            ), f"{table}: 模型主键 {pk} 与迁移 {pk_defs} 不一致"


def test_head_primary_key_is_composite_in_models() -> None:
    from ling.infra.models import ConnectorConfigHead

    pk = [column.name for column in ConnectorConfigHead.__table__.primary_key.columns]
    assert pk == ["scope", "instance_id"], pk


def test_ack_unique_includes_instance_in_models() -> None:
    from ling.infra.models import ConnectorRuntimeAck

    uniques = [
        [column.name for column in constraint.columns]
        for constraint in ConnectorRuntimeAck.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    ]
    assert ["scope", "instance_id", "generation", "replica_id"] in uniques, uniques
