"""媒体注意力唯一槽位：``media_slots``

Revision ID: 0061_media_slot
Revises: 0060_memory_purge
Create Date: 2026-11-09

## 为什么需要这张表

用户原话：「看视频的线程只允许存在一个全局，当这个会话在看视频的时候，
另一个会话可以说在忙」「与刷 B 站共用」。

设计见 `docs/MEDIA-EXCLUSIVE-SLOT.md`。核心结论是**不能用锁**：

- `asyncio.Lock` 只约束本进程，而 Web 请求、普通 worker、专用媒体 worker
  可以跑在不同进程；
- `pg_advisory_xact_lock` 一提交就释放，撑不住几十秒到几分钟的观看；
- 会话级 `pg_try_advisory_lock` 绑物理连接，连接池复用会让下一个请求继承
  一把它没申请的锁。

所以用**单行表 + 短事务条件抢占 + 租约**：抢到就提交、立刻归还连接，
崩溃后靠租约过期被接手，不需要任何进程间协调。

## 为什么预置一行

"只有一个槽"这件事必须由**数据**保证，不能靠"记得只插一行"的约定。
预置 `resource_key='media.attention'` 这一行并把它设为主键，第二行就插不进去。
抢占者永远只 `UPDATE` 这一行——不需要 `INSERT ... ON CONFLICT`，
也就没有"两个进程同时插出两行"的窗口。

## 为什么有 `generation`

租约只解决"别永久占住"，不解决"旧持有者晚到写库"。没有代次时：A 卡住 4 分钟
→ 租约过期 → B 接手 → A 醒过来把结果写进库，覆盖 B 的。代次让 A 的写入
`WHERE generation=...` 落空，整笔作废。

## 字段语义

- `holder_id IS NULL` = 空槽。用空值而不是单独的 `occupied` 布尔：两者并存
  会出现"occupied=true 但 holder 为空"的坏状态。
- `phase`：`held` = 已拿到执行权（还没真的开始看）；`active` = 耗时动作
  真的开始了。**只有 `active` 才能对外说"她正在看东西"**——把 `held` 也当
  成"在忙"就是编造她的状态。
- 时间字段存数据库时间（写入时用 `clock_timestamp()`），不用应用时钟：
  多机时钟偏差会让"谁还没过期"判断错乱。

## 为什么 `lease_until` 建索引

清理任务要按"已过期的槽"扫描回收孤儿。表只有一行，这个索引在单行表上几乎
没有收益——保留它是因为：设计允许将来按 `kind` 分槽（如直播单独限额），
那时行数不再是一。现在不建 `kind` 索引，避免为不存在的查询付维护成本。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0061_media_slot"
down_revision: str | None = "0060_memory_purge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESOURCE_KEY = "media.attention"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("media_slots"):
        op.create_table(
            "media_slots",
            sa.Column("resource_key", sa.String(length=64), primary_key=True),
            sa.Column("holder_id", sa.String(length=64), nullable=True),
            sa.Column("phase", sa.String(length=16), nullable=False, server_default="held"),
            sa.Column("kind", sa.String(length=24), nullable=False, server_default=""),
            sa.Column("actor_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("window_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("subject_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("call_id", sa.String(length=64), nullable=True),
            sa.Column("activity_id", sa.String(length=64), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "generation", sa.BigInteger(), nullable=False, server_default="0"
            ),
            sa.CheckConstraint(
                "phase IN ('held', 'active')",
                name="ck_media_slots_phase",
            ),
        )
        op.create_index(
            "ix_media_slots_lease_until", "media_slots", ["lease_until"]
        )

    # 预置唯一那一行。`ON CONFLICT DO NOTHING` 让迁移可重复执行
    # （重复跑不该报错，也不该把已有持有者清掉）。
    op.execute(
        sa.text(
            """
            INSERT INTO media_slots (resource_key, holder_id, phase, kind,
                                     actor_id, window_id, subject_id, generation)
            VALUES (:key, NULL, 'held', '', '', '', '', 0)
            ON CONFLICT (resource_key) DO NOTHING
            """
        ).bindparams(key=RESOURCE_KEY)
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("media_slots"):
        indexes = {idx["name"] for idx in inspector.get_indexes("media_slots")}
        if "ix_media_slots_lease_until" in indexes:
            op.drop_index("ix_media_slots_lease_until", table_name="media_slots")
        op.drop_table("media_slots")
