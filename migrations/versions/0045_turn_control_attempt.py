"""停止信号按 attempt 隔离：turn_controls 主键改为 (turn_id, attempt)

Revision ID: 0045_turn_control_attempt
Revises: 0044_media_describe_failed
Create Date: 2026-11-03

## 为什么需要这一列

`turn_controls` 原来是 `turn_id` 单列主键，于是**停止信号与 turn_id 永久
绑定**。而 retry 复用同一个 `turn_id`（`bump_attempt` 只把 `attempt` 加一，
state 改回 pending），所以「停止 → 重试」时：

1. 上一轮留下的 `stop_requested_at` 仍在库里（`clear()` 当时没有任何
   生产调用点，也没有过期逻辑）；
2. 新一轮启动后 `TurnCoordinator.run_turn` 新建 `StopToken` 并
   `create_task(control.watch(turn_id, token))`，而 watch 是
   `while True: await asyncio.sleep(0.5)` —— **先睡再查**；
3. 首次检查（约 0.5s 后）命中旧标记 → `token.stopped = True` →
   `turn.py` 在编译阶段就收敛成 stopped。

结果：重试出来的每一轮都被旧标记立刻停掉，重试多少次都一样。实测
（真实 store + SQLite）：t≈0.1s False / t≈0.3s False / t≈0.6s True /
t≈1.0s True；retry#1/2/3 全部 `token.stopped=True`，而新 turn_id 的对照
为 False。唯一逃生口是新一轮 <0.5s 内跑完——真实模型场景不可能。

## 为什么加列而不是只靠清理

- 只靠 `_finalize` 落终态后清理：对「stop → **立即** retry（旧轮次还没
  终结）」无效。旧轮次没到 finalize ⇒ 标记还在 ⇒ 新一轮（同 turn_id）
  照样命中。是否被停掉取决于"旧轮次先 finalize 还是新一轮先轮询"，
  非确定。
- retry 路由在 `bump_attempt` 前清理：对同一时序更危险。清掉标记后旧轮次
  再也停不下来（跨实例时本地 `CancelRegistry` 也管不到它）。

加 attempt 维度后，信号的作用域是"这一轮（这个 attempt）"：

- 「stop → 等旧轮次终结 → retry」：旧标记是 (tid, 1)，新一轮查 (tid, 2)，
  不命中；`_finalize` 顺手清掉 (tid, 1)。
- 「stop → 立即 retry（旧轮次未终结）」：旧轮次继续轮询 (tid, 1)，
  **照样停得下来**；新一轮查 (tid, 2) 立刻是 False。互不干扰。

## 存量数据

`attempt` 用 `server_default '1'` 回填，与 `Turn.attempt` 的初值一致。
存量"幽灵标记"（上一轮停过、之后 retry 过）因为新一轮 attempt≥2 而自然
失效 —— 也就是说**这个迁移顺带修好了线上已经卡死的那批轮次**。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045_turn_control_attempt"
down_revision: str | None = "0044_media_describe_failed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PK_NAME = "turn_controls_pkey"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {col["name"] for col in inspector.get_columns("turn_controls")}
    if "attempt" not in columns:
        # 先加列并回填（NOT NULL + server_default 让存量行直接变成 attempt=1）
        op.add_column(
            "turn_controls",
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        )
    # 主键必须变成复合键，否则 attempt 只是装饰列：同一 turn 的多个 attempt
    # 仍然只能存一行，upsert 会互相覆盖 —— 等于没修。
    current_pk = tuple(inspector.get_pk_constraint("turn_controls")["constrained_columns"] or ())
    if current_pk != ("turn_id", "attempt"):
        op.drop_constraint(PK_NAME, "turn_controls", type_="primary")
        op.create_primary_key(PK_NAME, "turn_controls", ["turn_id", "attempt"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    current_pk = tuple(inspector.get_pk_constraint("turn_controls")["constrained_columns"] or ())
    if current_pk != ("turn_id",):
        # 回退前必须把多 attempt 行压成一行，否则复合键收缩会失败
        op.execute(
            sa.text(
                """
                DELETE FROM turn_controls a
                USING turn_controls b
                WHERE a.turn_id = b.turn_id
                  AND a.attempt < b.attempt
                """
            )
        )
        op.drop_constraint(PK_NAME, "turn_controls", type_="primary")
        op.create_primary_key(PK_NAME, "turn_controls", ["turn_id"])
    columns = {col["name"] for col in inspector.get_columns("turn_controls")}
    if "attempt" in columns:
        op.drop_column("turn_controls", "attempt")
