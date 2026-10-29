"""回不回的自主选择：Web 与 QQ 共用的唯一入口。

## 这一层要解决的问题

在这之前，"回不回"不是一个决定，而是一条**规则的结果**：
私聊有文本必回、群聊被点名必回、普通群聊必沉默。模型从未参与过——
Web 路径想调模型但它读 `app.state.config_service`（**该属性不存在**），
永远静默退回规则；QQ 路径连模型都不问。

本模块把「回不回」变成一个**可审计的选择**：

- 确定性规则仍然先跑，给出**基线结论**
- 只有"允许模型参与"的情形才去问模型
- 模型说什么都**必须过校验**；模型不可用时退回基线，并**如实记录**状态

## 哪些情形允许模型参与

由配置里的自主度开关决定，**默认值等于现在的行为**，所以接入本模块
不会改变任何既有表现：

| 情形 | 默认 | 打开后 |
| --- | --- | --- |
| 私聊 | 必回（基线 `reply`） | `private_may_stay_silent=True` 时可选择不回 |
| 群聊被 @ | 必回（基线 `reply`） | `group_mention_requires_reply=False` 时可选 |
| 群聊其他 | 弹性区间（早已设计但从未生效） | 同左 |

**绝对不交给模型的**：防刷屏上限。那是 L0 硬约束——再像人也不能刷屏。

## 为什么要有 `model_status`

没有它，"模型参与"和"模型没参与"在外部看起来一模一样。
上一轮那个 `config_service` 死代码就是靠这个盲区活了很久：
测试全绿、界面正常，只是模型从来没被调用过。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from enum import StrEnum

from ling.kernel.decision import (
    DecisionContext,
    DecisionEngine,
    DecisionResult,
    TurnDecision,
)
from ling.ports.model import ChatMessageIn, ModelError

logger = logging.getLogger("ling.kernel.autonomy")

# 选择用的职责：群聊有专门的 dialogue.group，私聊用最快的那个。
GROUP_PROFILE = "dialogue.group"
PRIVATE_PROFILE = "dialogue.fast"

# 允许的长度档位。**不给自由文本**：否则模型会返回"中等偏长"这种没法用的值。
LENGTHS = ("brief", "normal", "extended")

CHOICE_PROMPT = """你在决定要不要回应一条消息。

只输出一个 JSON 对象，不要有任何其他文字、不要 Markdown 代码块。字段：
- "respond": true 或 false —— 要不要回应
- "reason": 一句话说明为什么（中文，不超过 40 字）
- "length": "brief" | "normal" | "extended" —— 如果要回应，期望多长
- "followup"（可选）：如果你这轮不回、但过一会儿想再提这件事，写
  {"after_minutes": 分钟数, "goal": "想说什么"}；不想提就省略这个字段

下面会给你我此刻的处境（精力、这个对话的倦怠程度）。你可以按自己的性格
决定要不要理会这些——累了可以少说、可以不说，也可以照样想聊。

判断依据是"像不像你自己此刻的反应"，不是"有没有礼貌"，也不是"合不合规范"。
"""

# 注意上面**没有**任何行为规定。这里曾经写着
# 「如果消息是别人之间的对话、广告、或已经有其他人回答了，通常不该插话」——
# 那是把一种性格（谨慎、不抢话）写死进代码。爱说话的人在群里恰恰会插嘴，
# 而那应该由人设决定，不由提示词决定。
#
# 用户明确要求：「这类都要根据人设来，我想给 llm 一个高自由度」。


class ModelStatus(StrEnum):
    """这次选择里模型到底做了什么。**必须能被外部看到**。"""

    ok = "ok"
    """模型参与并给出可用结论。"""

    not_asked = "not_asked"
    """按当前自主度设置，这一情形不交给模型（沿用基线）。"""

    unbound = "unbound"
    """职责没有绑定模型。"""

    error = "error"
    """调用失败或输出不可解析。"""

    overridden = "overridden"
    """模型想回，但被 L0 硬约束挡下（如防刷屏上限）。"""


@dataclass(frozen=True)
class Situation:
    """她此刻的处境。只作为**输入**给模型，不作为语气指令。

    用户明确要求行为风格由人设决定，所以这里给的只是状态事实
    （"此刻在歇着""这个对话已经有点倦了"），怎么表达由模型/人设决定。
    """

    availability: str = "available"  # available / low / resting
    load_band: str = "low"  # low / medium / high
    load_text: str = ""
    # 当前情绪（粗粒度）。**只作为状态事实给模型**，不规定它该怎么用——
    # 要不要因为心情不好少回两句，由人设和模型决定。
    affect_text: str = ""
    # 我对**这个人**的熟悉程度（粗粒度，不含数值）。
    #
    # **只在私聊填**。群聊不填：群窗口的 subject_id 指向的是"角色的主人"，
    # 而同一批消息可能来自不同的人，把"对主人的熟悉度"当成"对说话人的
    # 熟悉度"是错误归属。宁可空着，不要给她一个错的关系判断。
    relationship_hint: str = ""

    @property
    def resting(self) -> bool:
        return self.availability == "resting"


@dataclass(frozen=True)
class Followup:
    """这一轮不回应/回应之后，她还想稍后再提的事。

    为什么要模型显式给出而不是我们猜：靠"理由里有没有'稍后'字样"去推断
    会变成一个猜谜游戏。让她说清楚"我想几分钟后再提这件事"更可靠，
    也让意图队列的每一条都有明确来源。
    """

    after_minutes: int
    goal: str


@dataclass(frozen=True)
class TurnChoice:
    """一轮的处理选择。``respond=False`` 表示这一轮不产生任何外发。"""

    respond: bool
    reason: str
    length: str = "normal"
    model_status: ModelStatus = ModelStatus.not_asked
    # 基线规则怎么说（用于对比"模型改变了什么"）
    baseline: str = ""
    # 基线结论是不是"硬规则"（模型没参与时它才算数）
    baseline_hard: bool = True
    # 建议的无声回应（如表情）。**这只是建议**——目前没有任何执行路径，
    # 所以它不代表"已经发了表情"。真要接入需要渠道支持 reaction。
    silent_ack: str | None = None
    # 稍后想再提的事（可能为空）。兑现由意图队列负责，不由本模块发消息。
    followup: Followup | None = None
    # 决策时她的处境（便于事后复盘"当时为什么这么说"）
    availability: str = ""
    load_band: str = ""
    # 「为什么」的受控枚举（见 decision.SKIP_CODES）。空字符串表示没有结论。
    skip_code: str = ""

    @property
    def decision(self) -> TurnDecision:
        """把 respond 折回三态里的两态，便于日志与调用方统一读。"""
        return TurnDecision.reply if self.respond else TurnDecision.silence

    @property
    def hard(self) -> bool:
        """这个结论是不是不可推翻的硬规则结论。

        模型参与过 → 不是硬结论（即使结论与基线相同，它也是"选的"而非"规定的"）。
        """
        return self.baseline_hard and self.model_status is ModelStatus.not_asked

    @property
    def model_participated(self) -> bool:
        return self.model_status in {ModelStatus.ok, ModelStatus.overridden}


@dataclass
class _PolicyView:
    private_may_stay_silent: bool = False
    group_mention_requires_reply: bool = True
    # 在歇着时可以不进入决策（"没空回"）。用户要求"忙/休息时没空回消息"。
    # 做成开关是因为个人口味不同：有人希望她休息也回。
    rest_may_skip: bool = True


class AutonomyTurnService:
    """Web 与 QQ 共用的「回不回」选择器。"""

    def __init__(
        self,
        *,
        router: object | None = None,
        config_reader: object | None = None,
        situation_reader: object | None = None,
        persona_reader: object | None = None,
    ) -> None:
        self._router = router
        self._config_reader = config_reader
        # 异步无参可调用：返回 Situation。没有就当作"状态未知"，
        # **不能**因此假设她一定闲着或一定在忙。
        self._situation_reader = situation_reader
        # 异步无参可调用，返回人设文本（str）。
        # **决策时也要知道"她是谁"**——否则"要不要开口"是在没有性格的情况下
        # 做出来的，四个场景全判"不插话"就是那么来的。
        self._persona_reader = persona_reader
        self._last_status: ModelStatus = ModelStatus.not_asked

    async def choose(self, ctx: DecisionContext, *, text: str = "") -> TurnChoice:
        baseline = DecisionEngine().decide(ctx)
        policy = await self._policy()
        situation = await self._situation()

        # ---- L0：这些结论永远不由模型改 ----
        if self._is_safety_silence(baseline):
            return TurnChoice(
                respond=False,
                reason=baseline.reason,
                model_status=ModelStatus.not_asked,
                baseline=baseline.decision.value,
                baseline_hard=baseline.hard,
                silent_ack=baseline.silent_ack,
                availability=situation.availability,
                load_band=situation.load_band,
                skip_code=baseline.skip_code,
            )

        if not self._model_may_decide(ctx, policy, baseline):
            return TurnChoice(
                respond=baseline.decision is not TurnDecision.silence,
                reason=baseline.reason,
                model_status=ModelStatus.not_asked,
                baseline=baseline.decision.value,
                baseline_hard=baseline.hard,
                silent_ack=baseline.silent_ack,
                skip_code=baseline.skip_code,
            )

        # ---- 处境：在歇着时先不进入决策 ----
        # 这是**时机**问题不是语气问题：用户要求"忙/休息时没空回消息"。
        # 紧急内容永远不受影响——把求助默默吞掉是功能失败，不是风格问题。
        if situation.resting and policy.rest_may_skip and not _looks_urgent(text):
            return TurnChoice(
                respond=False,
                reason="此刻在休息，这条先放着",
                model_status=ModelStatus.not_asked,
                baseline=baseline.decision.value,
                baseline_hard=False,
                availability=situation.availability,
                load_band=situation.load_band,
                skip_code="resting",
            )

        # ---- 模型参与 ----
        verdict = await self._ask(ctx, text, situation=situation)
        if verdict is None:
            # 退回基线。**不猜**——拿不到就按规则走，并如实标记状态。
            return TurnChoice(
                respond=baseline.decision is not TurnDecision.silence,
                reason=baseline.reason,
                model_status=self._last_status,
                baseline=baseline.decision.value,
                baseline_hard=baseline.hard,
                silent_ack=baseline.silent_ack,
                skip_code=baseline.skip_code,
            )

        respond, reason, length, followup = verdict
        return TurnChoice(
            respond=respond,
            reason=reason,
            length=length,
            followup=followup,
            model_status=ModelStatus.ok,
            baseline=baseline.decision.value,
            baseline_hard=baseline.hard,
            # 模型说要回，就不存在"无声回应"这回事
            silent_ack=None if respond else baseline.silent_ack,
            availability=situation.availability if situation else "",
            load_band=situation.load_band if situation else "",
            skip_code="model_replied" if respond else "model_chose_silence",
        )

    # ---------------------------------------------------------------- 内部

    @staticmethod
    def _is_safety_silence(result: DecisionResult) -> bool:
        """防刷屏上限这类结论是 L0，模型无权推翻。

        "再像人也不能刷屏"——这一条不能因为要显得自主就放开。
        """
        return (
            result.decision is TurnDecision.silence
            and "上限" in result.reason
        )

    @staticmethod
    def _model_may_decide(
        ctx: DecisionContext, policy: _PolicyView, baseline: DecisionResult
    ) -> bool:
        if not ctx.is_group:
            return policy.private_may_stay_silent
        if ctx.mentioned or ctx.explicitly_asked:
            return not policy.group_mention_requires_reply
        # 群聊没点名：本来就在弹性区间，交给模型
        return True

    async def _policy(self) -> _PolicyView:
        if self._config_reader is None:
            return _PolicyView()
        try:
            effective = await self._config_reader.effective()  # type: ignore[attr-defined]
            d = effective.dialogue
            return _PolicyView(
                private_may_stay_silent=bool(d.private_may_stay_silent),
                group_mention_requires_reply=bool(d.group_mention_requires_reply),
                rest_may_skip=bool(getattr(d, "rest_may_skip", True)),
            )
        except Exception as exc:  # noqa: BLE001 - 读配置失败退回默认（=既有行为）
            logger.info("自主度配置读取失败，按默认处理: %s", type(exc).__name__)
            return _PolicyView()

    async def _persona_text(self) -> str:
        """读人设。读不到就返回空串——**不编一个默认性格**。

        编默认性格比"没有人设"更糟：没人会想到去改它，而它会一直生效。
        """
        reader = self._persona_reader
        if reader is None:
            return ""
        try:
            value = await reader()  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001 - 人设读不到不该让决策崩
            logger.info("人设读取失败，按无人设处理: %s", type(exc).__name__)
            return ""
        return str(value or "").strip()

    async def _situation(self) -> Situation:
        """读她此刻的处境。读不到就当"未知"，**不假设忙也不假设闲**。"""
        reader = self._situation_reader
        if reader is None:
            return Situation()
        try:
            value = await reader()  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001 - 状态读不到不影响决策
            logger.info("处境读取失败，按未知处理: %s", type(exc).__name__)
            return Situation()
        if isinstance(value, Situation):
            return value
        return Situation()

    async def _ask(
        self, ctx: DecisionContext, text: str, *, situation: Situation | None = None
    ) -> tuple[bool, str, str, Followup | None] | None:
        if self._router is None or self._config_reader is None:
            self._last_status = ModelStatus.unbound
            return None
        try:
            effective = await self._config_reader.effective()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            self._last_status = ModelStatus.error
            return None

        profile = GROUP_PROFILE if ctx.is_group else PRIVATE_PROFILE
        persona = await self._persona_text()
        text_block = _describe(ctx, text, situation)
        chunks: list[str] = []
        try:
            async for chunk, outcome in self._router.stream(  # type: ignore[attr-defined]
                effective,
                profile=profile,
                messages=[
                    ChatMessageIn(role="system", content=_system_prompt(persona)),
                    ChatMessageIn(role="user", content=text_block),
                ],
                allow_fallback=False,
            ):
                if chunk.delta:
                    chunks.append(chunk.delta)
                if outcome.error is not None:
                    raise ModelError(outcome.error.kind, outcome.error.message)
        except ModelError as exc:
            # 没绑定职责走这里。**不要**因为拿不到就假装模型参与过。
            self._last_status = (
                ModelStatus.unbound
                if exc.kind.value == "unsupported_capability"
                else ModelStatus.error
            )
            logger.info("回不回：模型不可用（%s）", exc.message[:80])
            return None
        except Exception as exc:  # noqa: BLE001
            self._last_status = ModelStatus.error
            logger.info("回不回：模型调用异常 %s", type(exc).__name__)
            return None

        parsed = _parse("".join(chunks))
        if parsed is None:
            self._last_status = ModelStatus.error
            return None
        self._last_status = ModelStatus.ok
        return parsed


def _system_prompt(persona: str) -> str:
    """把基础指令和人设拼成 system prompt。

    人设放在**同一段**而不是第二条 message：模型对单条 system 的遵循度
    明显高于"system + 再来一段"，而这里恰恰最需要它真的按人设来。
    """
    if not persona:
        return CHOICE_PROMPT
    return f"{CHOICE_PROMPT}\n\n你是谁（按这个来，不要按别的规矩来）：\n{persona}"


def _describe(ctx: DecisionContext, text: str, situation: Situation | None = None) -> str:
    """把处境写成模型能用的判断依据。

    刻意**不给**原始聊天记录——那会让模型倾向于"接上话题"，
    而这里要它判断的是"该不该开口"。
    """
    where = "群聊" if ctx.is_group else "私聊"
    parts = [f"场景：{where}"]
    if ctx.is_group:
        parts.append(f"是否被点名：{'是' if ctx.mentioned else '否'}")
        parts.append(f"是否接着角色的话：{'是' if ctx.addressed_to_actor else '否'}")
        parts.append(f"近期角色已发言 {ctx.recent_actor_turns} 次")
    parts.append(f"是否问句：{'是' if ctx.is_question else '否'}")
    if situation is not None:
        # 只陈述状态，不规定语气——怎么表达由人设和模型决定
        state = {"resting": "在休息", "low": "有点累", "available": "状态还行"}.get(
            situation.availability, "状态未知"
        )
        parts.append(f"我此刻：{state}")
        if situation.load_text:
            parts.append(f"对这个对话的感觉：{situation.load_text}")
        if situation.affect_text:
            # 只陈述状态，不规定它该怎么用
            parts.append(f"我此刻的情绪：{situation.affect_text}")
        if situation.relationship_hint:
            parts.append(f"我和这个人的关系：{situation.relationship_hint}")
    parts.append(f"消息内容：{text[:500]}")
    return "\n".join(parts)


def _parse(raw: str) -> tuple[bool, str, str, Followup | None] | None:
    """解析模型输出。任何不合规都返回 None（退回基线，不猜）。"""
    text = raw.strip()
    if not text:
        return None
    # 模型常把 JSON 包在 ```json 里。剥掉，但不要"猜"别的格式。
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("respond"), bool):
        return None
    length = str(data.get("length") or "normal")
    if length not in LENGTHS:
        length = "normal"
    reason = str(data.get("reason") or "").strip()[:120] or "模型未给出理由"
    followup = _parse_followup(data.get("followup"))
    return bool(data["respond"]), reason, length, followup


def _parse_followup(raw: object) -> Followup | None:
    """解析可选的稍后再提。任何不合规都当"没有"——不要为它退回整次选择。"""
    if not isinstance(raw, dict):
        return None
    goal = str(raw.get("goal") or "").strip()
    try:
        minutes = int(raw.get("after_minutes") or 0)
    except (TypeError, ValueError):
        return None
    if not goal or minutes <= 0:
        return None
    # 上限一天：想说的话过一天还没说出口，通常就不必再说了。
    return Followup(after_minutes=min(minutes, 24 * 60), goal=goal[:500])


__all__ = [
    "GROUP_PROFILE",
    "Followup",
    "PRIVATE_PROFILE",
    "AutonomyTurnService",
    "ModelStatus",
    "TurnChoice",
    "make_persona_reader",
]


def make_persona_reader(session: object, *, persona_id: str):
    """构造人设读取器。把人设带进"要不要开口"的决策里。

    之前决策只知道场景和处境，**不知道她是谁**——于是四个群聊场景
    全判"不插话"，因为提示词里恰好写着"别人之间的对话通常不该插话"。
    """

    async def _read() -> str:
        from ling.domains.persona.service import PersonaService

        view = await PersonaService(session, persona_id).get(persona_id)  # type: ignore[arg-type]
        from ling.domains.persona.service import format_traits

        parts: list[str] = []
        if view.system_prompt:
            parts.append(str(view.system_prompt))
        # traits 也要进"要不要开口"的判断——不然"她在群里话多"这种设定
        # 影响不到她插不插话。
        rendered = format_traits(getattr(view, "traits", None))
        if rendered:
            parts.append(rendered)
        # 边界是操作者自己定的，属于"她是谁"的一部分，要一起给
        for item in view.boundaries or []:
            parts.append(f"- {item}")
        return "\n".join(parts).strip()

    return _read


def make_situation_reader(
    session: object,
    *,
    actor_id: str,
    window_id: str,
    subject_id: str | None = None,
):
    """构造处境读取器：精力/倦怠 + 这个窗口的交互负担。

    **按窗口取负担**是刻意的：群里的烦躁不能影响对私聊对象的态度，
    那会变成跨受众泄露。

    任何一步读失败都退回中性，不猜。
    """

    async def _read() -> Situation:
        from ling.domains.presence.load import InteractionLoadService
        from ling.domains.presence.service import PresenceService

        availability = "available"
        load_text = ""
        band = "low"
        try:
            view = await PresenceService(session, actor_id=actor_id).view()  # type: ignore[arg-type]
            availability = view.availability
        except Exception as exc:  # noqa: BLE001 - 状态读不到按未知处理
            logger.info("presence 读取失败: %s", type(exc).__name__)
        try:
            load = await InteractionLoadService(session, actor_id=actor_id).level(  # type: ignore[arg-type]
                scope_key=window_id
            )
            band = load.band
            load_text = load.describe()
        except Exception as exc:  # noqa: BLE001
            logger.info("交互负担读取失败: %s", type(exc).__name__)
        # 情绪也要进"要不要开口"的判断。原来它只进措辞提示，于是她的判断
        # 对情绪完全免疫——而用户明确要求情绪可以影响回复意愿。
        affect_text = ""
        try:
            from ling.domains.affect.service import AffectService

            affect = AffectService(session, actor_id=actor_id)  # type: ignore[arg-type]
            tones: list[str] = []
            global_view = await affect.current()
            if global_view.tone_hint():
                tones.append(f"整体{global_view.tone_hint()}")
            window_view = await affect.for_window(window_id)
            if (
                window_view is not None
                and window_view.intensity >= 0.15
                and window_view.tone_hint()
            ):
                tones.append(window_view.tone_hint())
            affect_text = "；".join(tones)
        except Exception as exc:  # noqa: BLE001 - 情绪读不到不影响判断
            logger.info("情绪读取失败: %s", type(exc).__name__)
        relationship_hint = ""
        if subject_id:
            try:
                from ling.domains.relationship.service import RelationshipService

                rel = RelationshipService(session, actor_id=actor_id)  # type: ignore[arg-type]
                relationship_hint = rel.external_hint(
                    rel.view(await rel.state(subject_id))
                )
            except Exception as exc:  # noqa: BLE001 - 关系读不到不影响判断
                logger.info("关系读取失败: %s", type(exc).__name__)
        return Situation(
            availability=availability,
            load_band=band,
            load_text=load_text,
            affect_text=affect_text,
            relationship_hint=relationship_hint,
        )

    return _read


# 紧急信号的保守匹配。**宁可误判为紧急，也不要漏掉求救**——
# 误判的代价只是"休息时也回了一句"，漏判的代价是一条求助被默默吞掉。
_URGENT = re.compile(
    r"救命|求助|帮帮我|帮我一下|急用|紧急|出事了|怎么办|快|在吗.*急|"
    r"help|urgent|emergency|asap",
    re.IGNORECASE,
)


def _looks_urgent(text: str) -> bool:
    return bool(_URGENT.search(text or ""))
