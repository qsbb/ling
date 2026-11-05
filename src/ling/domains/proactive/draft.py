"""主动机会的草稿生成：她会说什么。

## 为什么需要这一层

主动联系的完整链路是「收集候选 → 门控 → 生成内容 → 投递」。
其中**投递整段原先完全没有**，而直接从"收集"跳到"投递"太冒险：
用户没法判断"她今天为什么突然找我"，只会觉得被打扰。

所以先做可观察的中间态：把要说的话生成出来、存下来，**不发送**。
先让人看到"她会说什么"，确认了再开真发送。

## 为什么不能直接发 `reason`

`reason` 是内部推理，例如：

> 问对方手机上聊天指什么项目

这句话直接发出去是很怪的——它像一个人的自言自语，不像对你说的话。
必须过一次生成，变成自然的开场白。

## 试过但否掉的方案：自动判定"复述理由"

想过加一道确定性闸：草稿与 reason 的字符重合度过高就判为"照抄内部理由"、丢弃重试。

**实测否掉了**：`reason="问一句他昨天说的那份材料交了没有"` 生成的
`"昨天你说的那份材料交了没呀？"` 是**好草稿**，但重合度很高被判成复述——
因为草稿本来就要复用理由里的**内容**（说的就是同一件事）。

改成拦"内部指令腔"（问一句/提醒我/顺便问）也不行：词表会误杀
`"提醒你一下，明天要交材料"` 这种正常说法。

结论：这个判断**没法用确定性规则做**，只能靠提示词约束 + 人看草稿
（投递默认关闭，本来就要求先看草稿）。所以不加这道闸。

## 生成失败怎么办

**不写占位符**。`draft_text` 保持 NULL 表示"还没生成"，
下次再试。写一句"（生成失败）"会把"没内容"伪装成"有内容但很怪"，
而且重试永远不会发生。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import ProactiveOpportunity, utcnow

logger = logging.getLogger("ling.domains.proactive.draft")

PROFILE = "dialogue.fast"
MAX_DRAFT_CHARS = 200

DRAFT_PROMPT = """你要主动找对方说一句话。

给你的是你**想找对方的原因**（这是你自己的想法，不是要发出去的句子）。
请把它变成一句自然、口语的开场白。

要求：
- 只输出这句话本身，不要引号、不要解释、不要加"我想说"之类的说明
- 一到两句，像随口开的口
- 不要复述"原因"这句话，那是你的念头不是台词
- 不要提"我注意到""系统提示"这类暴露机制的词
"""


# 句末标点：截断时要落到这里，不要切在半句话上
_SENTENCE_END = "。！？!?…\n"


def trim_to_sentence(text: str, limit: int = MAX_DRAFT_CHARS) -> str:
    """超长时在**句子边界**截断。

    原来直接 `text[:200]`，会把一句"我路过你家楼下那家店，想起你说想吃"截成
    "…想起你说想"，读起来像话没说完就被掐了。宁可少说一句，也不要说半句。
    实在找不到句末（整段没有标点）才退回硬截断。
    """
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    head = cleaned[:limit]
    cut = max(head.rfind(ch) for ch in _SENTENCE_END)
    if cut > limit // 3:  # 至少要留下有意义的长度，不要只剩一两个字
        return head[: cut + 1]
    return head


async def draft_for(
    session: AsyncSession,
    *,
    opportunity: ProactiveOpportunity,
    router: object,
    config_reader: object,
    persona: str = "",
    now: object | None = None,
) -> str | None:
    """为一条机会生成开场白。失败返回 None，不写占位符。"""
    from ling.models.router import StreamOutcome  # noqa: F401
    from ling.ports.model import ChatMessageIn

    moment = now or utcnow()
    expires = opportunity.expires_at
    if expires is not None and expires.tzinfo is None:
        from datetime import UTC

        expires = expires.replace(tzinfo=UTC)
    if expires is not None and expires <= moment:  # type: ignore[operator]
        return None

    try:
        effective = await config_reader.effective()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - 读不到配置就不生成
        logger.info("草稿生成跳过：配置不可用（%s）", type(exc).__name__)
        return None

    system = DRAFT_PROMPT if not persona else f"{DRAFT_PROMPT}\n\n你是谁：\n{persona}"
    chunks: list[str] = []
    try:
        async for chunk, outcome in router.stream(  # type: ignore[attr-defined]
            effective,
            profile=PROFILE,
            messages=[
                ChatMessageIn(role="system", content=system),
                ChatMessageIn(
                    role="user", content=f"我想找对方的原因：{opportunity.reason}"
                ),
            ],
            allow_fallback=False,
        ):
            if chunk.delta:
                chunks.append(chunk.delta)
            if outcome.error is not None:
                logger.info("草稿生成失败：%s", outcome.error)
                return None
    except Exception as exc:  # noqa: BLE001 - 生成失败不该让调度崩
        logger.info("草稿生成异常：%s", type(exc).__name__)
        return None

    text = "".join(chunks).strip().strip('"“”')
    if not text:
        return None
    return trim_to_sentence(text)


async def draft_pending(
    session: AsyncSession,
    *,
    actor_id: str,
    router: object,
    config_reader: object,
    persona: str = "",
    subject_id: str | None = None,
    limit: int = 5,
    now: object | None = None,
) -> dict[str, int]:
    """给还没有草稿的候选生成开场白。**不发送。**

    幂等：已经有 `draft_text` 的跳过——重复生成会让用户看到内容跳变。
    """
    from ling.domains.proactive.service import ProactivityService

    moment = now or utcnow()
    stmt = select(ProactiveOpportunity).where(
        ProactiveOpportunity.actor_id == actor_id,
        ProactiveOpportunity.state == "candidate",
        ProactiveOpportunity.draft_text.is_(None),
        ProactiveOpportunity.expires_at > moment,  # type: ignore[arg-type]
    )
    if subject_id:
        stmt = stmt.where(ProactiveOpportunity.subject_id == subject_id)
    rows = list((await session.execute(stmt.limit(limit))).scalars())

    drafted = failed = 0
    service = ProactivityService(session, actor_id=actor_id)
    for row in rows:
        text = await draft_for(
            session,
            opportunity=row,
            router=router,
            config_reader=config_reader,
            persona=persona,
            now=moment,
        )
        if text is None:
            failed += 1
            continue
        await service.set_draft(row.id, text=text)
        drafted += 1
    return {"drafted": drafted, "failed": failed, "considered": len(rows)}


__all__ = ["DRAFT_PROMPT", "PROFILE", "draft_for", "draft_pending"]
