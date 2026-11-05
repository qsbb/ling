"""DecisionEngine：一轮对话该"回答 / 澄清 / 沉默 / 延后"。

设计文档 §6.1 明确候选集合；§1 第 7 条要求**确定性代码最终裁决**，
模型只能提出候选。因此本模块的顺序是：

1. **硬规则**：必须答 / 必须不答——不看模型意见
2. **弹性区间**：可答可不答时，才允许模型（小模型）判断
3. 任何一步失败都退回确定性默认值，不让模型故障变成"乱说话"或"永久沉默"

关键约束：
- 私聊里用户直接对角色说话，默认**必须答**（不回是失礼）
- 群聊里没被点名、没有明确指向时，默认**沉默**（宁可少说，不刷屏）
- 沉默不是丢弃：消息仍落库，只是不产生 actor 回复
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class TurnDecision(StrEnum):
    """这一轮她怎么处置。

    曾经有个 `defer` 成员：声明了但**没有任何代码产出或消费它**。
    而"延后"这件事在系统里已经有活的实现——`deferred_by_floor`
    （发言权仲裁）和 `deferred_attention`（注意力层）。
    三个名字表示同一个概念，迟早会有人用错那个不生效的。
    所以删掉，而不是留着"以后可能用"。
    """

    """一轮对话的四种候选（设计文档 §6.1）。"""

    reply = "reply"
    clarify = "clarify"
    silence = "silence"


@dataclass(frozen=True)
class DecisionContext:
    """裁决所需的确定性事实。全部由服务端解析，模型/客户端不能伪造。"""

    is_group: bool
    # 是否被明确点名（@、回复角色消息、叫角色名）
    mentioned: bool = False
    # 这轮是直接对角色说的吗（私聊恒为 True；群聊看是否指向角色）
    addressed_to_actor: bool = False
    # 消息是否含明确指令性问句
    is_question: bool = False
    # 群内近期角色已发言次数（用于防刷屏）
    recent_actor_turns: int = 0
    # 该窗口 5 分钟内的发言次数上限
    group_turn_budget: int = 3
    # 用户明确要求回答（"ling 你说呢"）
    explicitly_asked: bool = False
    # 消息是否只是表情/图片等无文本内容
    has_meaningful_text: bool = True


# 「为什么没回」的**受控枚举**。
#
# 为什么不让它自由文本：自由文本没法聚合。想问"最近她因为什么不回消息"时，
# 得到的是几十种措辞各异的句子。事实用枚举，措辞放 reason。
SKIP_CODES: frozenset[str] = frozenset(
    {
        "private_direct",  # 私聊直接对话，应当回复（不是跳过，便于统计分母）
        "clarify_no_text",  # 私聊收到无文本内容，先澄清
        "group_mentioned",  # 群里被点名/明确要求
        "group_thread_continuation",  # 接着她的话说，属于延续
        "group_no_meaningful_text",  # 纯表情/图片
        "group_budget_exceeded",  # 群内发言配额用尽
        "group_question_not_addressed",  # 泛问句，未指向她
        "group_not_addressed",  # 普通群聊，未指向她
        "model_chose_silence",  # 弹性区间里模型决定不接话
        "model_replied",  # 弹性区间里模型决定接话
        "resting",  # 她在休息/忙，这条先放着
        "access_denied",  # 出入许可拦下
        "quiet_hours",  # 勿扰时段
        "duplicate_event",  # 重复事件
        "no_meaningful_text",  # 通用：没有可回的内容
        "connector_unavailable",  # 渠道不可用，投不出去
    }
)


@dataclass
class DecisionResult:
    decision: TurnDecision
    reason: str
    # 「为什么」的机器可读结论。必须落在 SKIP_CODES 里——
    # reason 是给人看的措辞，skip_code 是拿来检索聚合的事实。
    skip_code: str = "group_not_addressed"
    # True 表示这是硬规则结论，不需要也不允许模型推翻
    hard: bool = True
    # 需要在群里"无声存在"时的建议动作（表情回应等），平台不支持则为 None
    silent_ack: str | None = None


# 明确的点名模式：@ 已在平台层剥离，这里处理文字点名
_NAME_PATTERNS = re.compile(r"(ling|小?ling|阿岚|灵|你(?:说|觉得|来))", re.IGNORECASE)
_QUESTION_PATTERNS = re.compile(r"[?？]|(?:吗|呢|怎么|为什么|如何|多少|哪[个里]|是不是|能不能|可不可以)")
_EXPLICIT_ASK = re.compile(r"(ling|你)(?:来|说|回答|讲|看看)")
# 纯表情/无实质内容
_EMOJI_ONLY = re.compile(r"^[\s\U0001F300-\U0001FAFF\u2600-\u27BF~`!@#$%^&*()_\-+=<>?,./;:'\"\[\]{}|\\]+$")


def classify_text(text: str) -> tuple[bool, bool, bool]:
    """返回 (是否问句, 是否文字点名, 是否明确要求)。"""
    stripped = (text or "").strip()
    if not stripped:
        return False, False, False
    return (
        bool(_QUESTION_PATTERNS.search(stripped)),
        bool(_NAME_PATTERNS.search(stripped)),
        bool(_EXPLICIT_ASK.search(stripped)),
    )


class DecisionEngine:
    """确定性裁决 + 可选的模型弹性判断。

    本类不调用模型；``decide`` 只给硬结论。需要模型参与的"可答可不答"
    由调用方拿 ``DecisionResult.hard == False`` 后再问 ``dialogue.group``。
    """

    def decide(self, ctx: DecisionContext) -> DecisionResult:
        # ---------- 私有对话：直接对角色说话 ----------
        if not ctx.is_group:
            if not ctx.has_meaningful_text:
                # 空消息在私聊里也回一句澄清，而不是沉默（沉默会被当成坏了）
                return DecisionResult(
                    decision=TurnDecision.clarify,
                    reason="私聊收到无文本内容，先问清对方想说什么",
                    skip_code="clarify_no_text",
                )
            return DecisionResult(
                decision=TurnDecision.reply,
                reason="私聊里用户直接对角色说话，必须回应",
                skip_code="private_direct",
            )

        # ---------- 群聊：硬性不答 ----------
        if not ctx.has_meaningful_text:
            return DecisionResult(
                decision=TurnDecision.silence,
                reason="群聊里的纯表情/图片不构成对角色的发问，不插话",
                skip_code="group_no_meaningful_text",
                silent_ack="react",
            )

        # 被点名/被明确要求：必须答（豁免频率限制）
        if ctx.mentioned or ctx.explicitly_asked:
            return DecisionResult(
                decision=TurnDecision.reply,
                reason="群里被点名或明确要求回答，必须回应（豁免频率限制）",
                skip_code="group_mentioned",
            )

        # 群内已刷屏：硬性不答，避免变成噪音源
        if ctx.recent_actor_turns >= ctx.group_turn_budget:
            return DecisionResult(
                decision=TurnDecision.silence,
                reason=(
                    f"本群近期角色已发言 {ctx.recent_actor_turns} 次"
                    f"（上限 {ctx.group_turn_budget}），本轮不插话"
                ),
                skip_code="group_budget_exceeded",
                silent_ack="react",
            )

        # 明确指向角色但没点名（例如接着角色的话追问）：可答
        if ctx.addressed_to_actor:
            return DecisionResult(
                decision=TurnDecision.reply,
                reason="消息接着角色的话说，属于对话延续",
                skip_code="group_thread_continuation",
                hard=False,
            )

        # 没点名但含问句：可答可不答 → 交给模型判断（弹性区间）
        if ctx.is_question:
            return DecisionResult(
                decision=TurnDecision.silence,
                reason="群聊里的泛问句，未指向角色；默认不接话，交由模型判断是否值得插话",
                skip_code="group_question_not_addressed",
                hard=False,
            )

        # 其余：硬性沉默
        return DecisionResult(
            decision=TurnDecision.silence,
            reason="群聊里的普通聊天，没有指向角色，不插话",
            skip_code="group_not_addressed",
            silent_ack="react",
        )

    @staticmethod
    def has_meaningful_text(text: str) -> bool:
        stripped = (text or "").strip()
        if not stripped:
            return False
        return not bool(_EMOJI_ONLY.match(stripped))


@dataclass
class GroupTurnBudget:
    """群内发言频率（单进程内存态；多实例需换持久化）。"""

    window_id: str
    window_seconds: int = 300
    limit: int = 3
    _marks: list[float] = field(default_factory=list)

    def mark(self, now: float) -> None:
        self._marks.append(now)
        self._prune(now)

    def count(self, now: float) -> int:
        self._prune(now)
        return len(self._marks)

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        self._marks = [m for m in self._marks if m >= cutoff]


class GroupBudgetRegistry:
    """按窗口维护群聊发言预算。"""

    def __init__(self, *, window_seconds: int = 300, limit: int = 3) -> None:
        self._budgets: dict[str, GroupTurnBudget] = {}
        self._window_seconds = window_seconds
        self._limit = limit

    def budget(self, window_id: str) -> GroupTurnBudget:
        budget = self._budgets.get(window_id)
        if budget is None:
            budget = GroupTurnBudget(
                window_id=window_id,
                window_seconds=self._window_seconds,
                limit=self._limit,
            )
            self._budgets[window_id] = budget
        return budget

    def snapshot(self, window_id: str, now: float) -> dict[str, object]:
        budget = self.budget(window_id)
        return {
            "window_id": window_id,
            "count": budget.count(now),
            "limit": budget.limit,
            "window_seconds": budget.window_seconds,
        }


__all__ = [
    "DecisionContext",
    "DecisionEngine",
    "DecisionResult",
    "GroupBudgetRegistry",
    "GroupTurnBudget",
    "TurnDecision",
    "classify_text",
]
