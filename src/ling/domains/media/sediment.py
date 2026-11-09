"""把"看过的媒体"沉降成记忆：短期证据过期后，长期还记得。

## 用户原话

「然后看过的视频完整的解析留存一小段时间，过了一段时间之后，他就总结出简单
的内容，存放到记忆或者知识库里（经过筛选，走记忆入库流程）」

## 现状的缺口

媒体描述会进上下文，但只取**最近 3 条**（`kernel/context.py:749`）。也就是说
她看过一个视频之后大约三轮，那段描述就彻底从提示里消失，而且 `expires_at`
到了连行都读不到。用户问"上次那个视频怎么样"，她只能说不知道。

这不是"再描述一遍"能解决的——描述本来就在库里，缺的是**过期之后还能记得**。

## 筛选条件（这是需求里"经过筛选"的落点）

只有同时满足下面全部条件的证据才沉降：

1. **她真的看过**：`description IS NOT NULL` 且 `description_origin` 不是
   `model_inference`。模型推断出来的"这是一只猫"不能变成她记得的事——
   与 `fact_eligible` 是同一条铁律。
2. **有来源**：`source_event_id` 或 `dedupe_key` 非空，能挂到 `source_event_ids`
   上。没有来源的记忆不该存在（`MemoryService.propose` 本身就会拒绝）。
3. **有归属主体**：`subject_id` 非空。她自产素材（`subject_id IS NULL`）不沉降
   ——那是她自己的东西，不是"关于某个人的记忆"。
4. **已经过期或将要过期**：只处理"再过一会儿就读不到了"的那些。还新鲜的
   证据继续走实时通道，不必提前变成记忆（那会让她提前说"我以前看过"）。
5. **还没沉降过**：按 `media:{evidence_id}` 做幂等键，重复跑不会长出第二条。

## 为什么写成记忆而不是知识库

知识库（`knowledge_items`）存的是**外部可核验事实**（有 `source_url`）。
"她看过一个视频、里面有一只猫"不是外部事实，是她自己的经历。写进知识库会
让她把这件事当成"查到的资料"讲出来，那是另一种失真。

## 为什么不高抬置信度

沉降出来的记忆沿用证据原本的可信度（人说的 > 她自己推断的），并且
`origin` 用 `external_observation`——它是"她观察到的东西"，不是"用户亲口
陈述的事实"。降级比抬高安全：她可以提"我之前看过一个视频"，但不能说
"你告诉过我"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import MediaEvidenceRow

logger = logging.getLogger("ling.domains.media.sediment")

#: 沉降用的槽位名。固定值让它可被检索（"看过什么"是一类，不是每视频一个槽）。
SEDIMENT_SLOT = "media_watched"

#: 只沉降这么久之内看过的：太老的视频再翻出来说"我看过那个"会显得莫名其妙，
#: 而且与记忆保留期是两套语义（这条是她自己的经历，不是用户的事实）。
DEFAULT_LOOKBACK_DAYS = 30

#: 提前多久开始沉降。"将要过期"也算——等到真的读不到再沉降，中间会有一段
#: 完全遗忘的空窗。
DEFAULT_EXPIRE_MARGIN_HOURS = 24


@dataclass
class SedimentOutcome:
    """一次沉降的结果。"""

    scanned: int = 0
    written: int = 0
    skipped: int = 0
    already: int = 0

    @property
    def did_something(self) -> bool:
        return self.written > 0


def _source_ids(evidence: MediaEvidenceRow) -> list[str]:
    """这条证据的**真实来源** id 列表。

    只认 `source_event_id`（消息事件 id）。**不**把 `dedupe_key` 当来源：
    它是个去重令牌，不是事件。往 `source_event_ids` 里塞 `media:<key>` 这种
    合成串，会让"这条记忆有来源"变成一句空话——真要追溯时找不到任何东西。

    没有真实来源就不沉降（见调用处的 `skipped` 计数）。
    """
    return [str(evidence.source_event_id)] if evidence.source_event_id else []


def _memory_text(evidence: MediaEvidenceRow, *, when: datetime | None) -> str:
    """把一条媒体证据写成**一句人话记忆**。

    刻意保留 kind（视频/图片/语音）："我看了个视频，里面…"和"我看了张图，
    里面…"对后续对话是不同的线索。

    时间只在有 `created_at` 时写，且写**日期**不写具体时刻——精确到分钟
    在这里没有意义，反而会让记忆读起来像日志。
    """
    label = {"video": "视频", "image": "图片", "audio": "语音", "emoji": "表情"}.get(
        str(evidence.kind), "东西"
    )
    body = (evidence.description or "").strip()
    if when is not None:
        return f"之前看过一个{label}（{when.date().isoformat()}）：{body}"
    return f"之前看过一个{label}：{body}"


async def sediment_expired_evidence(
    session: AsyncSession,
    *,
    actor_id: str,
    now: datetime | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    expire_margin_hours: int = DEFAULT_EXPIRE_MARGIN_HOURS,
) -> SedimentOutcome:
    """把**将要过期/已过期**的"她真的看过"的媒体描述沉降成记忆。

    调用方负责提交事务。幂等：同一证据重复跑不会长出第二条记忆。
    """
    from ling.contracts.v1 import MemoryProposal, Origin, Visibility
    from ling.domains.memory.service import MemoryService
    from ling.settings.service import published_memory_policy

    moment = now or datetime.now(UTC)
    horizon = moment - timedelta(days=max(1, lookback_days))
    expiry_cut = moment + timedelta(hours=max(0, expire_margin_hours))

    rows = list(
        (
            await session.execute(
                select(MediaEvidenceRow).where(
                    MediaEvidenceRow.actor_id == actor_id,
                    MediaEvidenceRow.description.is_not(None),
                    # **她真的看过**：模型推断出来的不能变成她记得的事。
                    # 这一条与 `fact_eligible=False` 是同一条铁律。
                    or_(
                        MediaEvidenceRow.description_origin.is_(None),
                        MediaEvidenceRow.description_origin != "model_inference",
                    ),
                    # 有归属主体（她自产素材不沉降）
                    MediaEvidenceRow.subject_id.is_not(None),
                    # 看过的时间在回看窗口内
                    MediaEvidenceRow.created_at >= horizon,
                    # 将过期或已过期：还新鲜的继续走实时通道
                    or_(
                        MediaEvidenceRow.expires_at.is_(None),
                        MediaEvidenceRow.expires_at <= expiry_cut,
                    ),
                )
            )
        ).scalars()
    )
    outcome = SedimentOutcome(scanned=len(rows))
    if not rows:
        return outcome

    service = MemoryService(session, policy=await published_memory_policy(session))
    for evidence in rows:
        source_ids = _source_ids(evidence)
        if not source_ids:
            # 没有来源 → 不写。`propose` 本身也会拒，但在这里就跳过可以
            # 少一次无谓的往返，并让计数如实反映"为什么没写"。
            outcome.skipped += 1
            continue
        proposal = MemoryProposal(
            proposal_id=f"media_sediment:{evidence.id}",
            kind="assertion",
            text=_memory_text(evidence, when=evidence.created_at),
            # **外部观察**，不是"用户亲口陈述"：她看过一个视频 ≠ 用户告诉过她。
            # 用 user_statement 会让这条记忆在证据分组里被当成"可以直接说"的事实。
            origin=Origin.external_observation,
            source_event_ids=source_ids,
            visibility=Visibility.window_private,
            # 不高抬：沿用证据本身的证据等级（人说的 > 她推断的）。
            confidence=0.6,
        )
        # 幂等键：`propose` 按 (turn_id, slot) 判重，用证据 id 保证
        # "同一条证据只沉降一次"。
        #
        # **必须自己先查**：`propose` 命中幂等时**返回已有行的 id**（不是 None，
        # 见 `memory/service.py:449-458`）。只看返回值会把"已存在"数成"刚写入"，
        # 于是同一个证据每跑一次日志就多一条"写入 1 条"——而库里并没有多。
        dedupe_key = f"media:{evidence.id}"
        # **不自己查 `memories` 表**：那是记忆域的表（ADR-0001：跨域读投影可以，
        # 改别人的表不行）。改成让记忆域自己回答"这条沉降过没有"。
        if await service.already_proposed(turn_id=dedupe_key, slot=SEDIMENT_SLOT):
            outcome.already += 1
            continue
        await service.propose(
            proposal,
            subject_id=str(evidence.subject_id),
            source_window_id=evidence.window_id,
            source_visibility=str(evidence.visibility or "window_private"),
            turn_id=dedupe_key,
            slot=SEDIMENT_SLOT,
            value=str(evidence.id),
        )
        outcome.written += 1
    if outcome.written:
        logger.info(
            "媒体沉降：扫描 %d 条，写入 %d 条记忆，跳过 %d 条（无来源），已有 %d 条",
            outcome.scanned,
            outcome.written,
            outcome.skipped,
            outcome.already,
        )
    return outcome


__all__ = [
    "DEFAULT_EXPIRE_MARGIN_HOURS",
    "DEFAULT_LOOKBACK_DAYS",
    "SEDIMENT_SLOT",
    "SedimentOutcome",
    "sediment_expired_evidence",
]
