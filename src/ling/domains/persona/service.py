"""Persona 域：角色稳定设定、叙事边界与人格版本。

唯一写入方是本模块。切换人格不会自动继承另一人格的私聊内容
（继承逻辑属于 Conversation/Consent，不在这里）。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import Persona, utcnow

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


class PersonaService:
    def __init__(self, session: AsyncSession, default_persona_id: str = "persona_ling") -> None:
        self._session = session
        self._default_id = default_persona_id

    async def ensure_default(self) -> Persona:
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
        )


# 渲染上限：traits 是自由 dict，不能让它把上下文预算吃光
_MAX_TRAIT_ITEMS = 12
_MAX_TRAIT_CHARS = 600


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
    """
    if not traits:
        return ""
    lines: list[str] = []
    for key, value in list(traits.items())[:_MAX_TRAIT_ITEMS]:
        if value is None or value == "":
            continue
        text = value if isinstance(value, str) else str(value)
        text = text.strip()
        if not text:
            continue
        lines.append(f"- {key}：{text[:120]}")
    if not lines:
        return ""
    body = "\n".join(lines)
    return f"性格倾向（按这些来，不要按别的规矩来）：\n{body[:_MAX_TRAIT_CHARS]}"


__all__ = ["DEFAULT_TRAITS", "PersonaService", "PersonaView", "format_traits"]
