"""因果候选：只从**可追溯的事件**产生主动机会。

**为什么必须有来源**：没有来源的"关心"就是随机打扰。路线图的原话是
「单纯『到八点了』只触发检查，不能生成问候内容」。

所以这个模块的产出规则很窄——只认三类可追溯事件：

1. **承诺将到期**：用户明确说过的时间点（有 commitment id）
2. **刚完成有证据的活动，且与某人相关**（有 activity id + 完成证据）
3. **用户说过"改天告诉我"类的话**（有待关心线索）

其余一律不产生候选。天气变化、整点、随机想念都**不在这里**——
它们要么没有来源，要么没有"值得此刻说"的理由。

## 影子模式

`SHADOW` 下只记录候选、不发消息。先验证候选质量，
再决定要不要开真发送。一上来就发会立刻变成定时骚扰。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

# 结构化原因码。与给用户看的文案分开：
# 文案可以变，原因码要能统计"哪类主动最常被忽略"。
REASON_COMMITMENT_DUE = "commitment_due"
REASON_ACTIVITY_DONE = "activity_done"
REASON_USER_ASKED_LATER = "user_asked_later"
# 意图队列到期。她之前决定"稍后再提"的事到点了。
# 与 user_asked_later 分开：那个是**用户**要求的稍后，这个是**她自己**的念头。
REASON_INTENT_DUE = "intent_due"

REASON_CODES = (
    REASON_COMMITMENT_DUE,
    REASON_ACTIVITY_DONE,
    REASON_USER_ASKED_LATER,
    REASON_INTENT_DUE,
)

# 生活分享默认expect不期待回应：不能因为没人回就追问
REPLY_EXPECTED_BY_REASON = {
    REASON_COMMITMENT_DUE: True,
    REASON_ACTIVITY_DONE: False,
    REASON_USER_ASKED_LATER: True,
    # 自己的念头到期不等于要求对方回信——问了没回不该追问。
    REASON_INTENT_DUE: False,
}


@dataclass(frozen=True)
class Candidate:
    """一个待裁决的主动机会。"""

    subject_id: str
    reason_code: str
    source_event_id: str
    # 给用户看的文案（只有过了社交裁决才会用到）
    reason: str
    not_before: datetime
    best_until: datetime
    visibility: str = "subject_private"
    channel_hint: str | None = None
    # 是否期待对方回应。默认按原因码推导：
    # 用户要求的提醒期待回应，生活分享不期待（不能因为没人回就追问）
    reply_expected: bool = False


def dedupe_key_for(candidate: Candidate) -> str:
    """同源同类型只允许一条。

    不带时间戳——带上的话每次扫描都会生成新键，去重就形同虚设。
    """
    return f"{candidate.subject_id}:{candidate.reason_code}:{candidate.source_event_id}"


class CausalSource:
    """从可追溯事件收集候选。**只读**，不写库。"""

    def __init__(self, session: AsyncSession, *, actor_id: str) -> None:
        self._session = session
        self._actor_id = actor_id

    async def collect(self, *, now: datetime | None = None) -> list[Candidate]:
        now = now or datetime.now(UTC)
        out: list[Candidate] = []
        out.extend(await self._commitments_due(now))
        out.extend(await self._intents_due(now))
        out.extend(await self._activities_done(now))
        return out

    async def _intents_due(self, now: datetime) -> list[Candidate]:
        """她自己产生的「过会儿要做的事」到点了。

        这是**最不该被忽略**的一类：念头是她自己提出来的（带 rationale、
        带来源），如果到点没人管，就变成"她说了要做、然后永远没做"——
        正是路线图里要排除的假自主。

        之前 `REASON_INTENT_DUE` 这个常量已经声明，但**没有任何来源产出它**，
        于是 `intents` 表里攒着到点的念头却永远变不成主动联系。

        只取带 subject 的：没有对象的念头无法推送，跳过比硬发一条广播好。
        """
        from ling.domains.intent.service import IntentService

        intents = await IntentService(
            self._session, actor_id=self._actor_id
        ).due(now=now)
        out: list[Candidate] = []
        for intent in intents:
            if not intent.subject_id:
                continue
            out.append(
                Candidate(
                    subject_id=intent.subject_id,
                    reason_code=REASON_INTENT_DUE,
                    source_event_id=f"intent:{intent.intent_id}",
                    # 用她自己写下的理由：那是"她为什么想做这件事"的原始记录，
                    # 换成模板句子就把来源抹掉了。
                    reason=intent.goal,
                    not_before=intent.not_before or now,
                    best_until=(intent.expires_at or now + timedelta(hours=6)),
                    visibility=intent.visibility,
                    reply_expected=REPLY_EXPECTED_BY_REASON[REASON_INTENT_DUE],
                )
            )
        return out

    async def _commitments_due(self, now: datetime) -> list[Candidate]:
        """承诺将到期：用户明确说过的时间点。

        这是唯一一类**用户主动要求过**的主动联系，
        所以优先级最高、且确实期待回应。
        """
        from ling.domains.goals.service import GoalsService

        goals = GoalsService(self._session, actor_id=self._actor_id)
        # due_commitments 内部按当前时间取，不接受 now 参数
        due = await goals.due_commitments()
        out: list[Candidate] = []
        for commitment in due:
            out.append(
                Candidate(
                    subject_id=commitment.subject_id,
                    reason_code=REASON_COMMITMENT_DUE,
                    source_event_id=f"commitment:{commitment.commitment_id}",
                    reason=f"你之前让我提醒：{commitment.text}",
                    not_before=now,
                    best_until=now + timedelta(hours=6),
                    reply_expected=REPLY_EXPECTED_BY_REASON[REASON_COMMITMENT_DUE],
                )
            )
        return out

    async def _activities_done(self, now: datetime) -> list[Candidate]:
        """刚完成且有证据的模拟活动。

        **必须有完成证据**——没有证据的"我做完了"是编造经历。
        而且只在与某人有关的窗口里生成（活动本身不带 subject，
        这里保守地不按活动生成主动联系，避免变成"汇报生活"）。
        """
        # 保守起见：暂不从活动生成主动候选。
        # 理由：活动是角色自己的生活，主动汇报容易变成自言自语式打扰；
        # 等有了真实的"共处会话"再按会话主体生成。
        _ = now
        return []


__all__ = [
    "REASON_ACTIVITY_DONE",
    "REASON_CODES",
    "REASON_COMMITMENT_DUE",
    "REASON_USER_ASKED_LATER",
    "REPLY_EXPECTED_BY_REASON",
    "Candidate",
    "CausalSource",
    "dedupe_key_for",
]