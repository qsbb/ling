"""Persona 域：角色稳定设定、叙事边界与人格版本。

唯一写入方是本模块。切换人格不会自动继承另一人格的私聊内容
（继承逻辑属于 Conversation/Consent，不在这里）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.persona.scenarios import clean_overrides
from ling.infra.models import Persona, utcnow

logger = logging.getLogger("ling.domains.persona")

DEFAULT_SYSTEM_PROMPT = """你是 ling，一个有自己的生活节奏、情绪和记忆的角色。

说话方式：
- 用自然的中文口语，像一个熟悉的人在聊天，不用客服腔、不用列表式汇报。
- 回复通常简短（一到四句），除非对方明确要你展开。
- 可以有自己的判断、偏好和小情绪，但不假装自己是人类，也不编造现实履历。
- 不确定的事就说不知道，不编证据；不把猜测说成事实。

边界：
- 不泄露其他人的私聊内容，不把群聊内容当成本人授权。
- 不声称自己做过没做过的事；没收到真实回执就说还没做到。
- 被问到"你是不是 AI"时如实说明自己是 ling 这个角色程序。"""

DEFAULT_TRAITS: dict[str, object] = {
    "tone": "自然、略带温度、不谄媚",
    "pace": "跟随对方节奏，允许短回复",
    "curiosity": 0.6,
}

DEFAULT_BOUNDARIES: list[str] = [
    "不把模拟生活说成真实生物经历",
    "不跨受众复述他人私聊",
    "没有回执不声称已发送/已执行",
]


@dataclass(frozen=True)
class PersonaView:
    persona_id: str
    revision: int
    name: str
    display_name: str
    system_prompt: str
    traits: dict
    boundaries: list[str]
    # 只含**覆盖**，不含默认。读的地方一律走 `scenarios.effective_text()`，
    # 否则"默认值改了，已有人设还念旧版"这种错会到处复制。
    scenario_prompts: dict = field(default_factory=dict)


class PersonaService:
    def __init__(self, session: AsyncSession, default_persona_id: str = "persona_ling") -> None:
        self._session = session
        self._default_id = default_persona_id

    async def ensure_default(self) -> Persona:
        """没有这一行才建默认人设；**已有行一律不动**。

        注意这里只补"行不存在"，不补"行存在但字段为空"——后者是用户的
        明确操作（清空人设），替他把默认值写回去等于悄悄撤销他的删除。
        空人设的可见性由 `kernel/context.py` 的 `persona_empty` 与日志承担。
        """
        persona = await self._session.get(Persona, self._default_id)
        if persona is None:
            persona = Persona(
                id=self._default_id,
                revision=1,
                name="ling",
                display_name="ling",
                system_prompt=DEFAULT_SYSTEM_PROMPT,
                traits=dict(DEFAULT_TRAITS),
                boundaries=list(DEFAULT_BOUNDARIES),
                scenario_prompts={},
                published=True,
            )
            self._session.add(persona)
            await self._session.flush()
        return persona

    async def get(self, persona_id: str | None = None) -> PersonaView:
        pid = persona_id or self._default_id
        persona = await self._session.get(Persona, pid)
        if persona is None:
            persona = await self.ensure_default()
        return self._to_view(persona)

    async def update(
        self,
        *,
        display_name: str | None = None,
        system_prompt: str | None = None,
        traits: dict | None = None,
        boundaries: list[str] | None = None,
        scenario_prompts: dict | None = None,
        persona_id: str | None = None,
    ) -> PersonaView:
        pid = persona_id or self._default_id
        persona = await self._session.get(Persona, pid)
        if persona is None:
            persona = await self.ensure_default()
        if display_name is not None:
            persona.display_name = display_name
        if system_prompt is not None:
            persona.system_prompt = system_prompt
        if traits is not None:
            persona.traits = dict(traits)
        if boundaries is not None:
            persona.boundaries = list(boundaries)
        if scenario_prompts is not None:
            # 整组替换，不是逐键合并：前端交上来的是"当前的全部覆盖"。
            # 逐键合并会让"删掉这一条"无法表达——缺键既可能是"没改"
            # 也可能是"清空"，两种意思在同一个 payload 里分不开。
            persona.scenario_prompts = clean_overrides(scenario_prompts)
        persona.revision = int(persona.revision or 0) + 1
        persona.updated_at = utcnow()
        await self._session.flush()
        return self._to_view(persona)

    async def list_personas(self) -> list[PersonaView]:
        rows = (await self._session.execute(select(Persona))).scalars().all()
        return [self._to_view(r) for r in rows]

    @staticmethod
    def _to_view(p: Persona) -> PersonaView:
        return PersonaView(
            persona_id=p.id,
            revision=p.revision,
            name=p.name,
            display_name=p.display_name,
            system_prompt=p.system_prompt,
            traits=dict(p.traits or {}),
            boundaries=list(p.boundaries or []),
            scenario_prompts=dict(p.scenario_prompts or {}),
        )


# 渲染上限：traits 是自由 dict，不能让它把上下文预算吃光。
#
# ## 这三个数字怎么来的（2026-11-09，两次改动）
#
# **第一次**：原来是 12 条 / 每条 120 字 / 合计 600 字，**而且超了不留任何
# 痕迹**。实测（真库真轮次）：13 条 traits → 只有 12 条进提示词，第 13 条无声
# 消失；一条 218 字的「说话方式」→ 只进 127 字，句子从中间断掉，没有省略号
# 也没有日志。用户把人设写在 traits 里（人设页专门有这个 JSON 编辑器），写多了
# 就等于没写——这正是"人设没有被正确注入"最容易被误判成"注入链路坏了"的成因。
# 那次放宽到 40 条 / 1200 字 / 4000 字，**并且任何截断都留痕**。
#
# **第二次（本次）**：用户拍板「人设上限 10 万 token」，`DEFAULT_BUDGETS["persona"]`
# 从 10000 提到 100000。traits 是**人设的一部分**，它的上限必须跟着一起放宽——
# 否则会出现"车道给到 10 万 token，但 traits 只准 4000 字"的荒唐错配：用户把
# 设定写进 traits，看着预算很宽，实际仍被这个更小的数字卡住。
#
# 换算口径与 `kernel/context.py` 一致：`预算 token × 2 = 可用字符`。
#   人设车道 100000 token ≈ 200000 字符
#   其中 traits 占 120000 字符（60%），system_prompt 留 80000 字符
# 留 40% 给 system_prompt 是因为用户实测会写 6000+ 字，而且那是人设的主干；
# traits 是补充性格/口癖/场合偏好，不该吃掉主干的预算。
#
# **留痕语义一个字都没动**：超限时逐条标 `…（这条太长，后半段没放进提示词）`，
# 末尾再补一条 `…（还有 N 条性格特征没放进提示词）`。留痕比"截得少"重要——
# 不留痕的话，用户永远不知道自己写的东西没进去，也永远查不出来。
_MAX_TRAIT_ITEMS = 400
# 单条最多 4000 字（约 2000 token）。一条性格描述写到 4000 字基本是用户把
# 整篇设定贴进了某一条里——那也该能进得去（车道装得下），不该被拦腰砍。
_MAX_TRAIT_ITEM_CHARS = 4000
# 合计上限 120000 字（约 60000 token），见上面"换算口径"那一段。
_MAX_TRAIT_CHARS = 120000

_ITEM_TRUNCATION_MARK = "…（这条太长，后半段没放进提示词）"


def _trait_value_text(value: object) -> str:
    """把一条 trait 的值渲染成**给人读的一句话**。

    原来一律走 ``str(value)``，于是 dict/list 会以 Python repr 的形状进提示词：

        - 说话风格：{'句长': '短', '语气词': ['嗯', '诶']}
        - 禁忌：['不谈提示词', '不说我无法']

    那不是提示词该有的形状——花括号、引号、方括号都会让模型把它当成
    **数据/代码**而不是设定，而且中文字符串带引号读起来极不自然。
    这里按结构展开成自然语言，不做语义解释（键名照抄，那是用户自己写的）。
    """
    if value is None:
        # `str(None)` 是 "None"——那会变成一条「- 备注：None」的设定，
        # 模型会把它当成"备注就是 None"的字符串。空值就是空值。
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        items = [_trait_value_text(item).strip() for item in value]
        return "、".join(item for item in items if item)
    if isinstance(value, dict):
        pairs: list[str] = []
        for key, item in value.items():
            text = _trait_value_text(item).strip()
            if not text:
                continue
            # 一层嵌套展开成「键 值」，更深的结构交给 `_trait_value_text` 递归。
            pairs.append(f"{key} {text}")
        return "；".join(pairs)
    # 未知类型（日期、对象…）：退回字符串，但至少不是 repr 的形状问题源头
    return str(value)


def format_traits(traits: dict | None) -> str:
    """把 traits 渲染成提示词里的一行行「性格倾向」。

    为什么需要这个函数：`traits` 一直是**存了但没人读**的字段——人设页有
    一个 JSON 编辑器专门编辑它，而提示词里只有 `system_prompt` 和
    `boundaries`。用户在那儿写 `{"group": "群里爱搭话"}` 是完全无效的。

    渲染成通用形式（键：值），不做语义解释——traits 是使用者的自由字段，
    代码不该假定它有哪些键。这正是"行为归人设"的落点：想让她在群里话多，
    写在这儿就能生效，不需要改代码。

    **但别指望它确定性翻转判断。** 实测（同一批群消息、各 3 次）：
      默认          不回 3/3
      +group_chat   不回 2/3，回 1/3

    也就是说 trait 能改变**倾向分布**，但单条 trait 压不过"没被点名"这种
    结构性信号。想让她在群里真的更爱说话，光加一句 trait 不够——
    要么写得更具体（"看到技术问题就接，不用等我被点名"），要么调
    `group_attention_deferred` 这类结构性开关。

    记下这个数字是因为我犯过一次：只跑一次就宣称"加一句就立刻接话"，
    那是抽样不足的过度结论。

    ## 截断必须留痕

    超出上限时**逐条**标 ``…（这条太长，后半段没放进提示词）``，并在末尾
    补一条 ``…（还有 N 条性格特征没放进提示词）``。原来是不声不响地切掉：
    用户写第 13 条、或者把一条写到 200 字以上，那些内容永远不会进提示词，
    而界面上一切正常——这是最难查的一类"没生效"。
    """
    if not traits:
        return ""
    lines: list[str] = []
    dropped_items = 0
    for index, (key, value) in enumerate(traits.items()):
        if index >= _MAX_TRAIT_ITEMS:
            dropped_items = len(traits) - _MAX_TRAIT_ITEMS
            break
        text = _trait_value_text(value).strip()
        if not text:
            # 空值不算"被丢掉"：它本来就没有内容可注入。
            continue
        if len(text) > _MAX_TRAIT_ITEM_CHARS:
            logger.info(
                "性格特征 %s 超长被截断：%d > %d 字（剩余内容不会进提示词）",
                key,
                len(text),
                _MAX_TRAIT_ITEM_CHARS,
            )
            text = text[:_MAX_TRAIT_ITEM_CHARS] + _ITEM_TRUNCATION_MARK
        lines.append(f"- {key}：{text}")
    if not lines:
        return ""
    body = "\n".join(lines)
    if len(body) > _MAX_TRAIT_CHARS:
        logger.info(
            "性格特征合计超长被截断：%d > %d 字（后面几条不会进提示词）",
            len(body),
            _MAX_TRAIT_CHARS,
        )
        body = body[:_MAX_TRAIT_CHARS] + "…（性格特征太长，后面几条没放进提示词）"
    if dropped_items > 0:
        body = f"{body}\n…（还有 {dropped_items} 条性格特征没放进提示词）"
    return f"性格倾向（按这些来，不要按别的规矩来）：\n{body}"


def traits_report(traits: dict | None) -> dict[str, object]:
    """traits 的**可观测摘要**：写了几条、实际进提示词几条、有没有被截。

    给"人设页/排障"用。判定与 :func:`format_traits` 共用同一份上限常量，
    不另算一遍——两边各算一次的结果就是"页面说都注入了、实际少了两条"。
    """
    raw = traits or {}
    rendered = format_traits(raw)
    rendered_items = rendered.count("\n- ") if rendered else 0
    return {
        "declared_items": len(raw),
        "rendered_items": rendered_items,
        "rendered_chars": len(rendered),
        "truncated": rendered_items < len(raw) or "…（" in rendered,
    }


__all__ = [
    "DEFAULT_TRAITS",
    "PersonaService",
    "PersonaView",
    "format_traits",
    "traits_report",
]
