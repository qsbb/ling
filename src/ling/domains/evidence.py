"""完成证据的**机器可判种类**（P0.5「事实入口封堵」）。

## 为什么放在 ``ling.domains`` 根、而不是某个域里

判定要被四处共用：``memory``（自我时间线）、``agenda``（大纲的已证实经历摘要）、
``dream``（候选素材）、**以及将来 P7 的梦境候选查询**。放进任一个域都会让另外两个域
跨域 import，而 ``tests/unit/test_domain_boundaries.py`` 的闸门只放行
``ling.domains.<域>.service``（``.evidence`` 这类模块连白名单都进不去）。所以它是一个
**跨域共享的纯规则模块**：只依赖 ``ling.infra.models`` 的行类型（读属性，不写表），
没有自己的表、没有状态、没有 IO。

## 为什么需要这一层

项目硬规则是「计划不是事实，完成必须有证据」。但"有证据"以前只检查
``completion_evidence`` 非空——于是 ``life.tick`` 里"计划时段到了"这种**纯时钟
推断**也能被写成"她做过"，再被日程大纲、控制台事实层、梦境候选、记忆检索当成
真实经历读出来。

本模块把"证据是哪一种"做成**机器可判**的，并给所有读侧一把共同的闸：
:func:`is_fact_eligible`。

## 三种证据

- ``clock_only``：证据只来自时钟/计划边界（"时段结束了"）。**不是事实**——
  它证明的是"时间到了"，不是"她做了什么"。
- ``observed``：有真实观测/工具回执支撑（``activities.origin=tool_receipt``）。
- ``user_confirmed``：用户明确确认过（``activities.origin=user_stated``）。

只有后两种能进"事实"读侧（:data:`FACT_KINDS`）。

## 存储约定（不加列、不加迁移）

``activities`` 没有放结构化证据的列，``migrations/**`` 也不归本任务，所以种类
先编码在**现有** ``completion_evidence`` 的前缀里::

    kind=observed;笔记已写入 notes.md
    ^^^^^^^^^^^^^ 机器可判段（ASCII），分号之后是人读文本

- 只解析**字符串最开头**的 ``kind=<token>;``，**绝不**按中文子串猜语义
  （"非真实经历"这种措辞改一个字就会失效）。
- 写侧（``ActivityService.transition``）自己决定种类并**总是重写**前缀：
  调用方传进来的文本即使带 ``kind=`` 也不会被采信（HTTP 请求体不能自封事实）。

## 历史值兼容（只读、保守）

改造前写入的 ``completion_evidence`` 没有前缀，**无法从证据文本本身判定**。
保守规则：

- ``origin`` 这个结构化列明确是 ``tool_receipt`` / ``user_stated`` 时，
  分别按 ``observed`` / ``user_confirmed``；
- 其余（``simulated``、空、未知）一律 ``clock_only``。

于是 ``life.tick`` 收尾的模拟活动（``origin=simulated``）和所有旧记录都被读侧
排除。**这是读侧策略，不是数据清洗**：历史行保留、不回填、不删除；补证据或
清理数据是另一件事（迁移任务）。

## ``self_experiences`` 怎么判

``self_experiences`` 也没有证据列。派生经历时把种类写进现有 JSON 列
``source_event_ids`` 的**结构化标签**里（与 ``schedule_segments`` 用
``reason:<code>`` 标签是同一种约定）::

    ["activity:act_x", "evidence:observed"]

没有 ``evidence:`` 标签的历史行 → ``clock_only`` → 读侧排除。
:func:`fact_eligible_experiences` 还会**回查来源活动**复核证据性质：只读派生行、
不复核来源，正是 P0.5 要堵的洞。

## P7 梦境候选怎么用

``DreamService`` 已经留了同步的 ``fact_filter`` 注入点（一个
``Callable[[SelfExperienceRow], bool]``）。**单行判定用** :func:`is_fact_eligible`
就能接上（它只看这一行自己的 ``evidence:`` 标签，足够挡住历史 clock_only 行）::

    from ling.domains.evidence import is_fact_eligible

    dream = DreamService(session, actor_id=actor_id, fact_filter=is_fact_eligible)

要**回查来源活动**（更严，异步）就用 :func:`fact_eligible_experiences`：它接受
一批已取出的行、批量复核来源活动、返回合格行。推荐先多取再过滤：

```python
from sqlalchemy import select

from ling.domains.evidence import fact_eligible_experiences
from ling.infra.models import SelfExperienceRow

rows = list(
    (
        await session.execute(
            select(SelfExperienceRow)
            .where(
                SelfExperienceRow.actor_id == actor_id,
                SelfExperienceRow.archived_at.is_(None),
                SelfExperienceRow.occurred_at >= since,
            )
            .order_by(SelfExperienceRow.occurred_at.desc())
            .limit(50)  # 先多取：被排除的行不能占满配额
        )
    ).scalars()
)
candidates = await fact_eligible_experiences(session, rows, limit=10)
```

**不要**换回 ``select(SelfExperienceRow.summary)`` 直接读——那正是本模块要关掉的
旧路径。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import ActivityRow, SelfExperienceRow

__all__ = [
    "COMPLETION_EVIDENCE_PREFIX",
    "EVIDENCE_TAG_PREFIX",
    "FACT_KINDS",
    "EvidenceKind",
    "activity_evidence_kind",
    "activity_ids_of_experience",
    "encode_completion_evidence",
    "evidence_kind_from_origin",
    "experience_evidence_kind",
    "experience_source_event_ids",
    "fact_eligible_experiences",
    "is_fact_eligible",
    "parse_completion_evidence",
]


class EvidenceKind(StrEnum):
    """完成证据的种类。值写进 ``completion_evidence`` 前缀，是机器契约。"""

    clock_only = "clock_only"
    observed = "observed"
    user_confirmed = "user_confirmed"


#: 能进"事实"读侧的种类。``clock_only`` 永远不在里面。
FACT_KINDS: frozenset[EvidenceKind] = frozenset(
    {EvidenceKind.observed, EvidenceKind.user_confirmed}
)

#: ``completion_evidence`` 的结构化前缀：``kind=<token>;``。
COMPLETION_EVIDENCE_PREFIX = "kind="

#: ``self_experiences.source_event_ids`` 里的证据种类标签：``evidence:<token>``。
EVIDENCE_TAG_PREFIX = "evidence:"

_ACTIVITY_TAG_PREFIX = "activity:"

# 锚定字符串最开头：只有 `kind=<小写字母/下划线>;` 才算结构化段。
_KIND_HEAD = re.compile(r"^kind=([a-z_]+);")

# 结构化来源 → 证据种类。
# 注意这是**来源**（活动怎么来的），不是"证据文本写了什么"；两者混用会让
# "模型自称做完了"重新变成事实。写侧只有显式给了 evidence_kind 才可能偏离它。
_KIND_BY_ORIGIN: dict[str, EvidenceKind] = {
    "tool_receipt": EvidenceKind.observed,
    "user_stated": EvidenceKind.user_confirmed,
}


def evidence_kind_from_origin(origin: object) -> EvidenceKind:
    """按 ``origin`` 结构化列判定种类；判不出来 → 最保守的 ``clock_only``。"""
    return _KIND_BY_ORIGIN.get(str(origin or "").strip(), EvidenceKind.clock_only)


def encode_completion_evidence(kind: EvidenceKind, text: str) -> str:
    """把种类写进 ``completion_evidence`` 前缀。

    ``text`` 自带的旧前缀会被剥掉再重写——服务层决定种类，调用方文本不参与。
    """
    _ignored, body = parse_completion_evidence(text)
    return f"{COMPLETION_EVIDENCE_PREFIX}{kind.value};{body}"


def parse_completion_evidence(raw: str | None) -> tuple[EvidenceKind, str]:
    """拆出 ``(种类, 人读文本)``。

    - 有合法前缀 → 前缀就是权威（文本部分原样返回）；
    - 没有前缀（历史值）/ 未知种类 → ``clock_only``（保守一侧），
      文本原样返回，用于展示，不丢信息。
    """
    value = (raw or "").strip()
    match = _KIND_HEAD.match(value)
    if match is None:
        return EvidenceKind.clock_only, value
    try:
        kind = EvidenceKind(match.group(1))
    except ValueError:
        # 前缀是我们不认识的种类：不猜，按最保守处理，文本保持原样。
        return EvidenceKind.clock_only, value
    return kind, value[match.end() :].strip()


def activity_evidence_kind(row: object) -> EvidenceKind:
    """活动行/视图的证据种类。

    ``ActivityView`` 已经把种类算成独立字段（对外的 ``completion_evidence`` 是
    剥掉前缀的人读文本），所以先看这个字段；之后再按存储层的 ``completion_evidence``
    前缀判定。没有前缀的历史值再退回 ``origin``（结构化列）——仍然判不出来就是
    ``clock_only``。没有任何证据时同样 ``clock_only``。
    """
    declared = getattr(row, "evidence_kind", None)
    if declared is not None:
        try:
            return EvidenceKind(str(declared))
        except ValueError:
            return EvidenceKind.clock_only
    raw = getattr(row, "completion_evidence", None)
    if not str(raw or "").strip():
        return EvidenceKind.clock_only
    if _KIND_HEAD.match(str(raw).strip()) is not None:
        return parse_completion_evidence(raw)[0]
    return evidence_kind_from_origin(getattr(row, "origin", None))


def experience_evidence_kind(row: object) -> EvidenceKind:
    """自我经历行的证据种类（来自 ``source_event_ids`` 的 ``evidence:`` 标签）。"""
    for item in getattr(row, "source_event_ids", None) or []:
        tag = str(item).strip()
        if not tag.startswith(EVIDENCE_TAG_PREFIX):
            continue
        try:
            return EvidenceKind(tag[len(EVIDENCE_TAG_PREFIX) :].strip())
        except ValueError:
            return EvidenceKind.clock_only
    # 没有标签 = 改造前派生的历史行：无法判定 → 最保守的一侧。
    return EvidenceKind.clock_only


def is_fact_eligible(row: object) -> bool:
    """这一行能不能进"事实"读侧。

    两种行都能传：

    - ``ActivityRow`` / ``ActivityView``：按 ``completion_evidence`` 前缀
      （历史值退回 ``origin``）判定，且必须 ``state == "completed"``；
    - ``SelfExperienceRow``：按 ``source_event_ids`` 的 ``evidence:`` 标签判定。
      这只是"行自己声称的"，跨表复核见 :func:`fact_eligible_experiences`。

    判不出来一律 ``False``——宁可少认一条事实，也不能把时钟当经历。
    """
    if hasattr(row, "completion_evidence"):
        if str(getattr(row, "state", "completed")) != "completed":
            return False
        return activity_evidence_kind(row) in FACT_KINDS
    return experience_evidence_kind(row) in FACT_KINDS


def experience_source_event_ids(activity_id: str, kind: EvidenceKind) -> list[str]:
    """派生经历时写进 ``source_event_ids`` 的标签：来源 + 证据种类。"""
    return [f"{_ACTIVITY_TAG_PREFIX}{activity_id}", f"{EVIDENCE_TAG_PREFIX}{kind.value}"]


def activity_ids_of_experience(row: object) -> list[str]:
    """经历行指向的来源活动 id（``activity:<id>`` 标签）。"""
    ids: list[str] = []
    for item in getattr(row, "source_event_ids", None) or []:
        tag = str(item).strip()
        if tag.startswith(_ACTIVITY_TAG_PREFIX):
            activity_id = tag[len(_ACTIVITY_TAG_PREFIX) :].strip()
            if activity_id:
                ids.append(activity_id)
    return ids


async def fact_eligible_experiences(
    session: AsyncSession,
    rows: Iterable[SelfExperienceRow],
    *,
    limit: int | None = None,
) -> list[SelfExperienceRow]:
    """读侧统一过滤：只留下"事实可用"的自我经历，并**回查来源活动**复核。

    两步：

    1. 行自己带 ``evidence:observed|user_confirmed`` 标签；
    2. 来源活动（``activity:<id>``）必须存在且自己也合格——
       只读派生行、不复核来源，正是 P0.5 要堵的洞。

    没有 ``activity:`` 来源标签的行（未来 scene/milestone 之类）保守排除；
    要放进事实层，就得像活动派生一样把证据种类写进来并有意修改本规则。
    """
    candidates = [row for row in rows if experience_evidence_kind(row) in FACT_KINDS]
    if not candidates:
        return []
    wanted = sorted({aid for row in candidates for aid in activity_ids_of_experience(row)})
    sources: dict[str, ActivityRow] = {}
    if wanted:
        result = await session.execute(select(ActivityRow).where(ActivityRow.id.in_(wanted)))
        sources = {str(row.id): row for row in result.scalars()}
    eligible: list[SelfExperienceRow] = []
    for row in candidates:
        source_ids = activity_ids_of_experience(row)
        if not source_ids:
            continue
        if any(
            (source := sources.get(activity_id)) is None or not is_fact_eligible(source)
            for activity_id in source_ids
        ):
            continue
        eligible.append(row)
        if limit is not None and len(eligible) >= limit:
            break
    return eligible
