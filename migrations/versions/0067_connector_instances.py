"""连接器多实例：实例目录、修订与指针加实例维度

Revision ID: 0067_connector_instances
Revises: 0066_media_play_events
Create Date: 2026-11-11

## 本轮只动数据层

运行时与前端另起阶段。schema 先定稳，两边才能并行开发而不互相阻塞。
**本迁移不改变任何现有行为**：存量数据全部折叠成各自的默认实例，读写路径
原来的行一条不少、值一个不变。

## 实例标识：用哪一个 `instance_id`

代码里**有两个同名不同物**的东西，必须先分清再动手，否则整张表建在错的语义上。

### (a) `_instances[scope][组件名]` —— **不是**实例标识

`src/ling/kernel/connector_runtime.py:70` 的 `_instances` 外层 key 是 scope，
内层 key 是**硬编码的组件名**：`"onebot_ws_server"` / `"onebot_ws"` /
`"onebot_connector"`（见 `src/ling/app.py:352-353,370-371,380,410`）。

它只是"这条 scope 当前由哪几个对象组成"的快照，`apply()` 每次用
`new_instances` **整体替换** `_instances[scope]`（`connector_runtime.py:158`）。
形状像"scope → 多实例"，语义是"单实例快照 + 内部组件表"。用户给实例起的名字
**不可能**出现在这里，也不该出现。

### (b) `LING_ONEBOT_INSTANCE_ID`（默认 `onebot-default`）—— **也不是**

`src/ling/settings/connector_fields.py:290-296` 把它定义为"实例标识（账号绑定
按它隔离）"，它进入 `ActorAccount.connector_instance`
（`src/ling/kernel/inbound_intake.py:215`）和 `window_ref`
（`connectors/onebot/message.py:228`）。

它的**真实职责是 actor 命名空间**：决定"两个机器人下的同一个 QQ 号算不算同一个人"、
"同一个群算不算同一个会话窗口"。它是**配置值**，属于某个实例的**内容**，
而不是实例的**身份**——一个实例改了它，实例还是同一个实例。

### (c) 本迁移引入的 `instance_id` —— **实例的稳定身份**

`varchar(16)`，**由数据库主键 `(scope, instance_id)` 保证唯一**，
创建后**不可变**（改名改的是 `display_name`，不是它）。

三者关系：

```
scope（onebot / qqbot / access）
  └── instance_id（实例身份，稳定、不可变、用户可命名）
        ├── HEAD 指针 + generation（每实例各一套）
        ├── 修订链（每实例独立，父链只在实例内）
        └── 配置内容，其中一项是 actor_namespace
              ← 就是原来的 LING_ONEBOT_INSTANCE_ID 字段
```

**关键决定：存量数据里 (c) 的初值从 (b) 的字段值推导，而不是一律填 `'default'`。**

理由：报告 §1.4 指出，若某部署已把 `LING_ONEBOT_INSTANCE_ID` 改成
`onebot-x`，那么"库里的 instance_id 是 default、而 actor 命名空间是 onebot-x"
会让两者分叉。直接把 (b) 的现值当作 (c) 的初值，等价于"每个部署现在只有
一个实例，它的名字就是它一直用的那个名字"——**零语义变化**。

只有推导不出（没有覆盖值、或值为空/非法）时才落回 `'default'`。

### 为什么 (c) 不做成"把 scope 列改成 `onebot:default` 复合串"

报告 §1.3C 把这条路列为"不推荐"。补充一条**代码层面**的理由：仓库里到处是
`scope == cf.SCOPE_ONEBOT` 的直接比较（`connector_overrides.py:293,297,562-563`；
`connector_service.py:250,259`；`app.py:310-316,330`），改成复合串后任何一处
漏改都会静默走错分支——不报错、不崩，只是行为错。独立列则让旧代码继续按 scope
工作，语义边界清晰。另外 `scope` 只有 `varchar(16)`，`onebot:my-long-name` 会溢出。

## 改了哪四张表

| 表 | 改动 | 理由 |
|---|---|---|
| `connector_instances` | **新建** | 没有它，实例"存在但还没保存过配置"在库里不存在；也给 display_name / 启停 / 软删提供了属主 |
| `connector_config_revisions` | 加 `instance_id` | 每个实例的配置历史必须独立 |
| `connector_config_heads` | PK `scope` → `(scope, instance_id)` | `generation` 本来就是**每实例**递增的（`connector_service.py:518`），单列 PK 装不下 |
| `connector_runtime_acks` | 唯一键加 `instance_id` | 不加则实例 A 的第 3 代挡住实例 B 的第 3 代，ACK 串号 |
| `connector_override_values` | **不动** | 它由 `revision_id` 唯一确定归属，而 revision 已隐含实例 |

**不加外键**：这四张表现在一条 FK 都没有（`0054` 全文无 `ForeignKey`），
`parent_id` / `revision_id` / `active_revision_id` 都是应用层保证的软引用。
加 FK 会让迁移在历史孤儿行上失败，而生产上无法确认不存在这类行。

**不改 `value_fingerprint` 的 HMAC 输入**：它当前是
`{"scope","field","value"}`（`connector_overrides.py:447-451`）。把 `instance_id`
放进去会让**全部历史审计指纹失效**——每条历史记录的旧指纹与新算出的值永不相等，
用户会看到"每个字段都被改过"，而审计也无法区分"真改了"和"算法变了"（报告 R5）。

## 幂等

迁移框架本身保证一个 revision 只跑一次，但这里仍然写成可重入：
建表用 `IF NOT EXISTS`、加列前查 `information_schema`、约束先查
`pg_constraint`。理由是 Alembic 的 `stamp` 与实际 DDL 可能因人工干预而
不同步（生产上出过：手工补过列但没 stamp），此时"再跑一次"应该是无害的，
而不是崩在半路留下不可描述的状态。

## downgrade 不 raise

本仓库有 3 个链式回滚测试依赖 `downgrade()` 不抛异常（回滚整条链会逐个
调用每个迁移的 downgrade）。所以这里**不**采用报告 §5.3 的"方案 A（检测到多
实例就 raise）"。

代价是回滚会**丢弃**非默认实例：

- 其它实例的 HEAD 指针、修订链、ACK 记录会被 `DELETE` 掉；
- 对应的 `connector_override_values` 也一并删（否则留下没有 revision 的孤儿行）。

这是**有损操作**，但它是**可预期的、结构性的**损失（schema 装不下了），
而不是"静默改变了运行时行为"。为了让它可见，downgrade 里对这些删除计数并
用 `print` 输出（Alembic 迁移里没有配置好的 logger，`print` 是这条链上
已确立的可见做法，见 `0064_persona_clear_defaults` 的同类输出）。

保留 `'default'` 实例：它是升级时由存量数据折叠出来的那个，回滚后仍然对应
"每 scope 一个配置"的旧语义，直接留着就等价于升级前的状态。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0067_connector_instances"
down_revision: str | None = "0066_media_play_events"
branch_labels = None
depends_on = None


#: 存量数据折叠出来的默认实例 id。与报告 §1.4、§5.5 的约定一致。
DEFAULT_INSTANCE_ID = "default"

#: 允许的 scope 取值。与 `0054` 建表时的注释一致（onebot | qqbot | access）。
SCOPES = ("onebot", "qqbot", "access")

#: actor 命名空间字段（原 `LING_*_INSTANCE_ID`）。存量迁移用它推导实例 id。
ACTOR_NAMESPACE_FIELDS = {
    "onebot": "LING_ONEBOT_INSTANCE_ID",
    "qqbot": "LING_QQBOT_INSTANCE_ID",
}


def _has_table(bind: sa.engine.Connection, name: str) -> bool:
    return name in sa.inspect(bind).get_table_names()


def _has_column(bind: sa.engine.Connection, table: str, column: str) -> bool:
    return any(col["name"] == column for col in sa.inspect(bind).get_columns(table))


def _has_constraint(bind: sa.engine.Connection, table: str, name: str) -> bool:
    """约束是否已存在。用 `pg_constraint` 而不是 `get_unique_constraints`：
    后者看不到 PRIMARY KEY，而本迁移恰好要判主键。
    """
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM pg_constraint c JOIN pg_class t ON c.conrelid = t.oid "
            "WHERE t.relname = :table AND c.conname = :name"
        ),
        {"table": table, "name": name},
    ).first()
    return row is not None


def _unique_columns(bind: sa.engine.Connection, table: str, name: str) -> list[str] | None:
    """某个具名 UNIQUE 约束包含的列（按定义顺序）。不存在时返回 `None`。

    不能用"先 `_has_constraint` 再决定 drop"这种两次查询的写法：
    `DROP CONSTRAINT` 之后同一事务内的第二次查询在某些驱动/缓存下仍可能
    读到旧结果，实测导致"drop 了但没重建"，唯一键直接消失（比不改更糟）。
    这里改成"读列定义 → 与目标比对 → 不一致才重建"，一次判断到底。
    """
    rows = bind.execute(
        sa.text(
            "SELECT a.attname FROM pg_constraint c "
            "JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
            "WHERE c.conrelid = CAST(:table AS regclass) AND c.conname = :name "
            "ORDER BY k.ord"
        ),
        {"table": table, "name": name},
    ).fetchall()
    return [row[0] for row in rows] if rows else None


def _primary_key_columns(bind: sa.engine.Connection, table: str) -> list[str]:
    """当前主键的列（按定义顺序）。用于判断是否已经是复合主键。"""
    # 走 `pg_constraint` + `unnest(conkey) WITH ORDINALITY`。
    # 踩过的坑：`attnum = ANY(i.indkey)` 和 `generate_subscripts` 两种写法
    # 都会产生**多重匹配**（实测各返回 41 个重复的 'scope'），导致
    # "是否已是复合主键"判断失败，进而重复 ADD PRIMARY KEY 报
    # `multiple primary keys are not allowed`。`WITH ORDINALITY` 配上显式
    # 列名 k(attnum, ord) 才不会把整行展开成多条。
    rows = bind.execute(
        sa.text(
            "SELECT a.attname FROM pg_constraint c "
            "JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
            "WHERE c.conrelid = CAST(:table AS regclass) AND c.contype = 'p' "
            "ORDER BY k.ord"
        ),
        {"table": table},
    ).fetchall()
    return [row[0] for row in rows]


def upgrade() -> None:
    bind = op.get_bind()

    # ---------------------------------------------------------------- 实例目录
    #
    # 为什么需要它：现状没有任何地方存"这个 scope 下有哪些实例"。
    # `LING_*_INSTANCE_ID` 只是配置里的一个字符串字段，没有生命周期、
    # 不能枚举、不能表达"已删除"；HEAD 只在首次保存时才建行
    # （`connector_service.py:657-661`），所以"建了实例但还没保存过配置"
    # 在库里根本不存在。而设置页要的实例列表需要名称和创建时间两个属主。
    if not _has_table(bind, "connector_instances"):
        op.create_table(
            "connector_instances",
            # 与 `scope` 同宽。命名空间是 (scope, instance_id)，不是裸 id——
            # 详见模块 docstring「实例标识」一节。
            sa.Column("scope", sa.String(16), nullable=False),
            sa.Column("instance_id", sa.String(16), nullable=False),
            sa.Column("display_name", sa.String(64), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_by", sa.String(64), nullable=True),
            # 软删：删掉实例后历史修订与审计仍要可查。
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("scope", "instance_id", name="connector_instances_pkey"),
            # 同一 scope 下显示名唯一，但不同 scope 可以各有一个"默认接入"。
            sa.UniqueConstraint(
                "scope", "display_name", name="uq_connector_instance_display_name"
            ),
            sa.CheckConstraint(
                "scope IN ('onebot', 'qqbot', 'access')", name="ck_connector_instance_scope"
            ),
        )
        op.create_index(
            "ix_connector_instance_scope_deleted",
            "connector_instances",
            ["scope", "deleted_at"],
        )

    # ---------------------------------------------------------------- 加实例维度
    #
    # `server_default='default'` 让存量行自动获得实例维度（PG 11+ 走
    # metadata-only，不重写表）。**先加列、再回填、最后才收紧语义**，
    # 这样任何一步失败都不会留下"有列但值是空"的中间态。
    for table in ("connector_config_revisions", "connector_config_heads", "connector_runtime_acks"):
        if not _has_column(bind, table, "instance_id"):
            op.add_column(
                table,
                sa.Column(
                    "instance_id",
                    sa.String(16),
                    nullable=False,
                    server_default=DEFAULT_INSTANCE_ID,
                ),
            )

    # ------------------------------------------------- 存量数据：推导实例 id
    #
    # 把 actor 命名空间字段的现值当作实例 id。这样"每个部署现在只有一个实例，
    # 它的名字就是它一直用的那个名字"，零语义变化。
    # 推导不出时下面的 COALESCE 会落回 'default'。
    #
    # 只读当前生效修订（HEAD.active_revision_id）上的值：候选/历史修订里的
    # 旧值不代表现在的语义。
    for scope, field_key in ACTOR_NAMESPACE_FIELDS.items():
        bind.execute(
            sa.text(
                """
                UPDATE connector_config_heads AS h
                SET instance_id = candidate.value
                FROM (
                    SELECT h2.scope, h2.instance_id AS old_id,
                           btrim(o.plain_value #>> '{}', '"') AS value
                    FROM connector_config_heads h2
                    JOIN connector_config_revisions r ON r.id = h2.active_revision_id
                    JOIN connector_override_values o ON o.revision_id = r.id
                    WHERE r.scope = :scope AND o.field_key = :field_key
                      AND o.value_kind = 'text'
                      AND o.plain_value IS NOT NULL
                ) AS candidate
                WHERE h.scope = candidate.scope AND h.instance_id = candidate.old_id
                  AND candidate.value ~ '^[a-z0-9_-]{1,16}$'
                """
            ),
            {"scope": scope, "field_key": field_key},
        )
        # 修订行跟着 HEAD 的推导结果走：同一个 scope 下本轮只有一条修订链，
        # 所以这等价于"该 scope 的全部修订都归到那个实例"。
        bind.execute(
            sa.text(
                """
                UPDATE connector_config_revisions
                SET instance_id = (
                    SELECT h.instance_id FROM connector_config_heads h
                    WHERE h.scope = connector_config_revisions.scope
                    LIMIT 1
                )
                WHERE scope = :scope
                  AND EXISTS (
                      SELECT 1 FROM connector_config_heads h WHERE h.scope = :scope
                  )
                """
            ),
            {"scope": scope},
        )
        bind.execute(
            sa.text(
                """
                UPDATE connector_runtime_acks
                SET instance_id = (
                    SELECT h.instance_id FROM connector_config_heads h
                    WHERE h.scope = connector_runtime_acks.scope
                    LIMIT 1
                )
                WHERE scope = :scope
                  AND EXISTS (
                      SELECT 1 FROM connector_config_heads h WHERE h.scope = :scope
                  )
                """
            ),
            {"scope": scope},
        )

    # ------------------------------------------------- 回填实例目录
    #
    # 每个"曾经有过 HEAD 或修订或 ACK"的 scope 都造出它的实例行。
    # **主键是 (scope, instance_id)**，所以两个 scope 各有一个 'default'
    # 不会相撞——报告 §5.2 第 2 条标 ⚠ 的那个错误（单列 id PK 撞 PK）
    # 正是靠这个复合主键避开的。
    #
    # 用 CTE 让 id 与 display_name 一致地取到"该 scope 当前生效的实例 id"。
    bind.execute(
        sa.text(
            """
            INSERT INTO connector_instances
                (scope, instance_id, display_name, enabled, created_at)
            SELECT s.scope, s.instance_id, s.instance_id, true, now()
            FROM (
                SELECT scope, MAX(instance_id) AS instance_id FROM connector_config_heads
                  GROUP BY scope
                UNION
                SELECT scope, MAX(instance_id) AS instance_id FROM connector_config_revisions
                  GROUP BY scope
                UNION
                SELECT scope, MAX(instance_id) AS instance_id FROM connector_runtime_acks
                  GROUP BY scope
            ) AS s
            ON CONFLICT (scope, instance_id) DO NOTHING
            """
        )
    )

    # ------------------------------------------------- HEAD 表改复合主键
    #
    # Alembic 没有 ALTER PRIMARY KEY，只能 DROP + ADD。`scope` 单主键
    # 装不下多实例：`generation` 本来就是每实例递增的，两行同 scope 必须共存。
    pk_cols = _primary_key_columns(bind, "connector_config_heads")
    if pk_cols == ["scope"]:
        op.drop_constraint("connector_config_heads_pkey", "connector_config_heads", type_="primary")
    if _primary_key_columns(bind, "connector_config_heads") != ["scope", "instance_id"]:
        op.create_primary_key(
            "connector_config_heads_pkey",
            "connector_config_heads",
            ["scope", "instance_id"],
        )

    # ------------------------------------------------- ACK 表改唯一键
    #
    # 不加实例维度会让实例 A 的第 3 代挡住实例 B 的第 3 代（报告 R6）：
    # 表现为"切换成功但没 ACK"，进而污染 ack_count。当前代码里 ack_count
    # 是硬编码的所以暂时看不出来，但这是一份"已经错了、只是没人读"的数据。
    target_ack_cols = ["scope", "instance_id", "generation", "replica_id"]
    if _unique_columns(bind, "connector_runtime_acks", "uq_connector_ack_generation") != target_ack_cols:
        if _unique_columns(bind, "connector_runtime_acks", "uq_connector_ack_generation") is not None:
            op.drop_constraint(
                "uq_connector_ack_generation", "connector_runtime_acks", type_="unique"
            )
        op.create_unique_constraint(
            "uq_connector_ack_generation", "connector_runtime_acks", target_ack_cols
        )

    # ------------------------------------------------- 补索引
    #
    # "某实例的历史/当前状态"是最常见的查询形状，按 (scope, instance_id, …)
    # 建索引。老的 `ix_..._scope_state` 保留：查"某 scope 全部实例"仍要用它。
    existing = {idx["name"] for idx in sa.inspect(bind).get_indexes("connector_config_revisions")}
    if "ix_connector_config_revisions_scope_instance_state" not in existing:
        op.create_index(
            "ix_connector_config_revisions_scope_instance_state",
            "connector_config_revisions",
            ["scope", "instance_id", "state"],
        )


def downgrade() -> None:
    """回滚到"每 scope 一个配置"。

    **不 raise**：本仓库有 3 个链式回滚测试要求整条迁移链能一路降到底，
    任何一个迁移抛异常都会让它们全红。所以这里选择**有损但可预期**的折叠，
    并把损失显式打印出来，而不是拒绝回滚。

    丢弃的是"schema 装不下的那部分"：非 `default` 实例的指针、修订链、ACK。
    保留 `default`，它对应升级前"每 scope 一份配置"的语义。
    """
    bind = op.get_bind()

    # ------------------------------------------------- 先删非默认实例的数据
    #
    # 顺序有讲究：先删修订的**子**（覆盖值），再删修订本身，最后删指针与 ACK。
    # 反过来会先删掉 revision 再找不到它的覆盖值，留下孤儿行。
    #
    # 注意不能删 `default` 之外的**全部**行：升级时从 actor 命名空间推导出来的
    # 实例 id 可能不是 'default'（比如存量部署用了 onebot-x）。所以判断依据是
    # "`connector_instances` 里登记了几行"——只留每 scope 的第一行（按创建时间）。
    doomed = bind.execute(
        sa.text(
            """
            SELECT scope, instance_id FROM (
                SELECT scope, instance_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY scope ORDER BY deleted_at NULLS FIRST, created_at, instance_id
                       ) AS rn
                FROM connector_instances
            ) ranked
            WHERE rn > 1
            """
        )
    ).fetchall()

    if doomed:
        pairs = [{"scope": row[0], "instance_id": row[1]} for row in doomed]
        for pair in pairs:
            bind.execute(
                sa.text(
                    "DELETE FROM connector_override_values WHERE revision_id IN ("
                    "SELECT id FROM connector_config_revisions "
                    "WHERE scope = :scope AND instance_id = :instance_id)"
                ),
                pair,
            )
            bind.execute(
                sa.text(
                    "DELETE FROM connector_config_revisions "
                    "WHERE scope = :scope AND instance_id = :instance_id"
                ),
                pair,
            )
            bind.execute(
                sa.text(
                    "DELETE FROM connector_config_heads "
                    "WHERE scope = :scope AND instance_id = :instance_id"
                ),
                pair,
            )
            bind.execute(
                sa.text(
                    "DELETE FROM connector_runtime_acks "
                    "WHERE scope = :scope AND instance_id = :instance_id"
                ),
                pair,
            )
        print(
            f"0067 回滚：丢弃 {len(doomed)} 个非默认实例的指针与修订链"
            f"（{'、'.join(f'{row[0]}/{row[1]}' for row in doomed[:8])}"
            f"{' 等' if len(doomed) > 8 else ''}）"
        )

    # ------------------------------------------------- 恢复旧唯一键
    target_ack_cols = ["scope", "generation", "replica_id"]
    if _unique_columns(bind, "connector_runtime_acks", "uq_connector_ack_generation") != target_ack_cols:
        if _unique_columns(bind, "connector_runtime_acks", "uq_connector_ack_generation") is not None:
            op.drop_constraint(
                "uq_connector_ack_generation", "connector_runtime_acks", type_="unique"
            )
        op.create_unique_constraint(
            "uq_connector_ack_generation", "connector_runtime_acks", target_ack_cols
        )

    # ------------------------------------------------- 恢复 HEAD 单主键
    #
    # 必须先删掉多余的同 scope 行，否则重建 `PRIMARY KEY (scope)` 会撞主键。
    # 上面的折叠循环只删了 `connector_instances` 里登记过的；没有登记但
    # 存在于 HEAD 的行（理论上不该有）也在这里兜掉，保证 DDL 一定成功。
    bind.execute(
        sa.text(
            """
            DELETE FROM connector_config_heads h
            USING connector_config_heads keep
            WHERE h.scope = keep.scope
              AND (keep.updated_at, keep.instance_id) > (h.updated_at, h.instance_id)
            """
        )
    )
    pk_cols = _primary_key_columns(bind, "connector_config_heads")
    if pk_cols == ["scope", "instance_id"]:
        op.drop_constraint("connector_config_heads_pkey", "connector_config_heads", type_="primary")
    if _primary_key_columns(bind, "connector_config_heads") != ["scope"]:
        op.create_primary_key(
            "connector_config_heads_pkey", "connector_config_heads", ["scope"]
        )

    # ------------------------------------------------- 删索引与列
    existing = {idx["name"] for idx in sa.inspect(bind).get_indexes("connector_config_revisions")}
    if "ix_connector_config_revisions_scope_instance_state" in existing:
        op.drop_index(
            "ix_connector_config_revisions_scope_instance_state",
            table_name="connector_config_revisions",
        )

    for table in ("connector_runtime_acks", "connector_config_heads", "connector_config_revisions"):
        if _has_column(bind, table, "instance_id"):
            op.drop_column(table, "instance_id")

    # ------------------------------------------------- 最后删实例目录
    #
    # 放最后：上面几步都要读它来判断"哪些实例是非默认的"。
    if _has_table(bind, "connector_instances"):
        op.drop_index("ix_connector_instance_scope_deleted", table_name="connector_instances")
        op.drop_table("connector_instances")
