"""清掉"从未编辑过"的旧默认人设：``personas``

Revision ID: 0064_persona_clear_defaults
Revises: 0063_deferred_replies
Create Date: 2026-11-11

## 为什么需要这个迁移

用户 2026-11-11 拍板：「没有默认人设」——旧版 `ensure_default()` 建行时把
`DEFAULT_SYSTEM_PROMPT` / `DEFAULT_TRAITS` / `DEFAULT_BOUNDARIES` 三份常量
**复制**进了 `personas` 表。新版已经把那三个常量**连名字带内容一起删掉**，
代码不再读它们，但**已经写进库里的副本还在**。

升级后这些副本会被当作「用户自己写的人设」原样注入。而旧默认里正含这两句：

- 「不假装自己是人类」
- 「被问到"你是不是 AI"时如实说明自己是 ling 这个角色程序」

用户明确要求的是「**不是必须承认是 AI，而是根据人设决定，不额外注入**」。
所以从没编辑过人设的老用户，会**静默冻结在一段他们没写过的身份立场上**——
而且不知道自己被注入了。

## 为什么不能直接 DELETE

两种行在库里长得一模一样，没有任何标记能区分：

| 情况 | 正确做法 |
|---|---|
| A. 从没编辑过 → 内容是旧默认的拷贝 | 清掉 |
| B. 用户改过 → 这是用户自己写的 | **绝对不动** |

粗暴按 `id = 'persona_ling'` 删除会连用户真正写的内容一起删掉。

## 判据：三个正文字段**全部**逐字/结构相等，**且** `revision = 1`

只有 `system_prompt` **且** `traits` **且** `boundaries` 三者同时等于旧默认，
**且** `revision = 1`，才判定为"从未编辑"。**任一项不同 → 整行不动**
（不是只保留不同的那项）。

### 为什么还要 `revision = 1`

`PersonaService.update()` 结尾**无条件**执行
`persona.revision = int(persona.revision or 0) + 1`（`5818d2f:service.py:123`），
而 `display_name` / `scenario_prompts` 都是**可选参数**（`:94-106`）。所以：

| 用户操作 | revision | 三字段 | 本次处理 |
|---|---|---|---|
| 从没动过 | 1 | 旧默认 | **清空** |
| 只改显示名 | 2 | 旧默认 | **不动** |
| 只加场景覆盖 | 2 | 旧默认 | **不动** |
| 改了一个字 | 2 | 有差异 | 不动（三字段判等挡住） |

第 2、3 类用户**主动动过这一行**——他知道这行存在，是他自己保存的。清掉等于
擅自替他做决定。"从未编辑"的准确含义是「这一行从建出来到现在没被任何人保存
过」，而不只是「正文没变」。

`revision > 1` 且三字段有差异的行，三字段判等本身已经挡住；加 `revision = 1`
只影响"碰过但正文未改"这一类——正是要保护的。

旧值在下面 `_OLD_*` 里**硬编码**，这是刻意的：

- 不能 import `domains/persona/service.py`——那些常量已经删了，迁移必须永远
  能跑，不能依赖一个会继续演进的应用模块；
- 不能 import 当前默认——当前根本没有默认值了。

硬编码值与 `git show 5818d2f:src/ling/domains/persona/service.py` 的常量逐字
比对过（`system_prompt` 264 字符，U+0022 ASCII 双引号，无结尾换行）。

## 为什么是清空字段，不是删行

行里还承载 `display_name`、`scenario_prompts`、`published` 等。删行有连带影响
（外键、前端引用、将来按 id 查不到）。清空三列后这行就是"空人设"——按新版
语义它**什么都不注入**，正是用户要的状态。

## JSON 列的相等语义

`traits` / `boundaries` 在表里是 `JSON`（不是 `JSONB`）。PostgreSQL 的 `json`
类型**没有**相等操作符（`json = json` 会报错），所以比较前两边都转 `jsonb`。

这意味着判等是**JSON 结构相等**，不是字节相等：

- 对象键序不同、Unicode 转义写法不同 → **相等**（正确：语义没变）；
- `0.6` 与 `0.60` → **相等**（JSON 数字按数值比）；
- 数组顺序不同 → **不相等**（数组顺序有意义，改了就是改了）。

`system_prompt` 是 `Text`，走标准字符串判等，是真正的逐字比较。

## downgrade：**不还原数据**（no-op），不是不可用

**清空之后无法区分两种空值**：0064 清掉的，还是新版本来就没写过的。把空值
一律写回旧默认会造成两种误伤：

1. 新版新建的行（`ensure_default()` 现在写 `''`/`{}`/`[]`）会被**填成旧默认**
   ——凭空给用户恢复出一份他没写过的身份立场，正好是这次要删的东西；
2. 用户升级后主动清空的人设，会被**悄悄撤销**。

这两条都违反本项目「不替用户伪造内容」的底线。所以 `downgrade()` **不写任何
数据**，只记一条 warning。它**不 raise**：仓库里没有任何其他迁移在 downgrade
里 raise，且 `alembic downgrade` 必须能沿链回退（既有测试
`test_recent_two_migrations_downgrade_then_upgrade`、`test_0052_*`、
`test_0053_*` 都依赖这一点）。在 head 上 raise 会让整条回退路径失效。

要真正恢复旧文案，请用**升级前的数据库备份**，不要把 downgrade 当无损回滚。

## 幂等与 no-op

- 老部署里没有 persona 行 → 命中 0 行，零影响；
- 新装（空人设）→ `''`/`{}`/`[]` 不等于旧默认，命中 0 行，零影响；
- 重复执行 → 已清空的行不再等于旧默认，第二次命中 0 行，天然幂等。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064_persona_clear_defaults"
down_revision: str | None = "0063_deferred_replies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

#: 旧 `DEFAULT_SYSTEM_PROMPT` 的逐字副本（commit 5818d2f `service.py:20-32`）。
#: 264 个字符；`"你是不是 AI"` 用的是 ASCII 双引号；结尾**没有**换行。
_OLD_SYSTEM_PROMPT = """你是 ling，一个有自己的生活节奏、情绪和记忆的角色。

说话方式：
- 用自然的中文口语，像一个熟悉的人在聊天，不用客服腔、不用列表式汇报。
- 回复通常简短（一到四句），除非对方明确要你展开。
- 可以有自己的判断、偏好和小情绪，但不假装自己是人类，也不编造现实履历。
- 不确定的事就说不知道，不编证据；不把猜测说成事实。

边界：
- 不泄露其他人的私聊内容，不把群聊内容当成本人授权。
- 不声称自己做过没做过的事；没收到真实回执就说还没做到。
- 被问到"你是不是 AI"时如实说明自己是 ling 这个角色程序。"""

#: 旧 `DEFAULT_TRAITS`（commit 5818d2f `service.py:34-38`）。
_OLD_TRAITS = {
    "tone": "自然、略带温度、不谄媚",
    "pace": "跟随对方节奏，允许短回复",
    "curiosity": 0.6,
}

#: 旧 `DEFAULT_BOUNDARIES`（commit 5818d2f `service.py:40-44`）。
_OLD_BOUNDARIES = [
    "不把模拟生活说成真实生物经历",
    "不跨受众复述他人私聊",
    "没有回执不声称已发送/已执行",
]


def _json_literal(value: object) -> str:
    """把 Python 值序列化成 SQL 字符串字面量里的 JSON 文本。

    `ensure_ascii=False` 保留中文原样（`jsonb` 解析时与转义写法等价，但日志里
    更好读）；转义交给 SQLAlchemy 的绑定参数做，不手工拼引号。
    """
    return json.dumps(value, ensure_ascii=False)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 正常迁移链的 baseline 已建 `personas`；到 0064 仍然缺表说明这不是一条
    # 完整的链（损坏或跳过版本）。**不静默早退**：静默返回会让 Alembic 把本
    # 版本标记成"已执行"，将来补上表也不会自动重跑，旧文案就永远留着。
    if not inspector.has_table("personas"):
        raise RuntimeError(
            "0064: 表 personas 不存在。这条迁移假定 0001_baseline 已建表；"
            "缺表说明迁移链不完整（跳过或损坏），请检查 alembic 版本历史，"
            "不要跳过本迁移。"
        )

    columns = {column["name"] for column in inspector.get_columns("personas")}
    missing = {"id", "system_prompt", "traits", "boundaries"} - columns
    if missing:
        raise RuntimeError(
            f"0064: 表 personas 缺少列 {sorted(missing)}，"
            "迁移链与模型定义不一致，请先修复结构再加数据。"
        )

    # 先数一遍总行数，用于报告"清了几行 / 各为什么跳过"。
    total = bind.execute(sa.text("SELECT count(*) FROM personas")).scalar_one()

    # 三个字段 + `revision` 必须**同时**成立，才判定为"从未编辑"。
    #
    # 为什么还要 `revision = 1`（不只是三字段判等）：
    # `PersonaService.update()` 结尾无条件执行
    # `persona.revision = int(persona.revision or 0) + 1`，而 `display_name` /
    # `scenario_prompts` 都是**可选参数**。所以「只改过显示名」或「只加过场景
    # 覆盖」的用户，revision 已经是 2，但三个正文字段仍是旧默认副本。
    #
    # 这类用户**主动动过这一行**（他知道这行存在，是他自己保存的），清掉等于
    # 擅自替他做决定。判定"从未编辑"应该是"这一行从建出来到现在没被任何人
    # 保存过"，而不只是"正文没变"。
    #
    # 反过来，`revision > 1` 且三字段有差异的行，三字段判等本身就已经挡住了；
    # 加 `revision = 1` 只影响"碰过但正文未改"这一类——正是要保护的。
    #
    # `json -> jsonb` 转换是必需的：`json` 类型没有相等操作符，直接比较会抛
    # `operator does not exist: json = json`。
    _body_matches = """
        system_prompt = :prompt
        AND traits::jsonb = CAST(:traits AS jsonb)
        AND boundaries::jsonb = CAST(:boundaries AS jsonb)
    """
    params = {
        "prompt": _OLD_SYSTEM_PROMPT,
        "traits": _json_literal(_OLD_TRAITS),
        "boundaries": _json_literal(_OLD_BOUNDARIES),
    }

    # 先分别统计三类，供日志区分"有差异"与"碰过但正文未改"：
    # `body_match`  = 三字段等于旧默认（无论 revision）
    # `revision_1`  = 三字段等于旧默认 **且** revision = 1（= 本次要清的）
    body_match = bind.execute(
        sa.text(f"SELECT count(*) FROM personas WHERE {_body_matches}"), params
    ).scalar_one()
    revision_1 = bind.execute(
        sa.text(
            f"SELECT count(*) FROM personas WHERE {_body_matches} AND revision = 1"
        ),
        params,
    ).scalar_one()

    # "本来就空"必须在 UPDATE **之前**数：UPDATE 会把命中的行也变成空值，
    # 事后统计会把"刚被清掉的"错算成"本来就空"。
    _emptied = """
        system_prompt = ''
        AND traits::jsonb = CAST('{}' AS jsonb)
        AND boundaries::jsonb = CAST('[]' AS jsonb)
    """
    already_empty = bind.execute(
        sa.text(f"SELECT count(*) FROM personas WHERE {_emptied}")
    ).scalar_one()

    stmt = sa.text(
        f"""
        UPDATE personas
        SET system_prompt = '',
            traits = CAST('{{}}' AS json),
            boundaries = CAST('[]' AS json)
        WHERE {_body_matches}
          AND revision = 1
        """
    )
    result = bind.execute(stmt, params)
    cleared = result.rowcount or 0

    # 三个互斥类别，全部在 UPDATE 前或由互斥集合推出：
    # - 正文有差异：三字段不全等于旧默认，且也不是空值
    # - 碰过但正文未改：三字段等于旧默认但 revision > 1
    # - 本来就空：升级前就已是 ''/{}'/'[]'　（三者也互斥，相加等于 total）
    skipped_edited = int(total) - int(body_match) - int(already_empty)
    skipped_touched = int(body_match) - int(revision_1)
    skipped = int(total) - int(cleared)

    # 只记数量和版本，**不记提示词内容**：日志可能进发布记录或工单。
    logger.info(
        "0064 清理旧默认人设：清空 %d 行；跳过 %d 行"
        "（其中：正文有差异 %d 行，碰过但正文未改 %d 行，本来就空 %d 行）",
        cleared,
        skipped,
        skipped_edited,
        skipped_touched,
        already_empty,
    )
    if cleared == 0 and total > 0:
        logger.info(
            "0064 没有任何『从未编辑过』的行：要么旧文案早被改过，要么这一行被"
            "保存过（revision > 1），本次零影响"
        )


def downgrade() -> None:
    """**不做任何数据还原**：这是刻意的 no-op，不是遗漏。

    ## 为什么不把旧默认写回去

    清空之后，库里的空值有三种来源，**从数据上无法区分**：

    1. 本迁移清掉的（旧默认副本）；
    2. 新版 `ensure_default()` 建行时写的（`''` / `{}` / `[]`）；
    3. 用户升级后主动清空的。

    把空值一律写回旧默认会误伤 2 和 3：

    - 新版新建的空人设会被**填回用户从没写过的身份立场**——正是这次要删的东西；
    - 用户主动清空的操作会被**悄悄撤销**。

    两者都违反本项目「不替用户伪造内容」的底线，所以宁可"回退不还原数据"，
    也不猜。要真正恢复旧文案，请用**升级前的数据库备份**，不要把 downgrade
    当无损回滚。

    ## 为什么是 no-op 而不是 raise

    仓库里没有任何其他迁移在 `downgrade()` 里 raise；`alembic downgrade` 需要
    能沿链回退（`tests/integration/test_migrations.py` 的
    `test_recent_two_migrations_downgrade_then_upgrade` / `test_0052_*` /
    `test_0053_*` 都依赖这一点）。在 head 上 raise 会让整条链的回退路径失效，
    影响范围远超本迁移该负的责任。no-op 同样不写入任何数据，风险为零。
    """
    logger.warning(
        "0064 downgrade：不回滚数据。被清空的旧默认人设无法自动恢复"
        "（空值无法区分「本迁移清的」与「本来就是空」）；"
        "如需恢复旧文案请使用升级前的数据库备份。"
    )
