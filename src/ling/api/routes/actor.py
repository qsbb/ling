"""角色路由：Persona 设定与权威事件时间线。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ling.api.deps import CurrentAuth, DbSession, PersonaDep, SettingsDep
from ling.api.errors import LingError
from ling.contracts.v1 import ErrorCode, Origin
from ling.domains.persona.scenarios import (
    SCENARIOS,
    effective_text,
    is_custom,
    known_keys,
)
from ling.domains.persona.service import PersonaView
from ling.kernel.turn import ActorSequencer

router = APIRouter(tags=["actor"])


class ScenarioOut(BaseModel):
    """一个场景在当前人设下的样子。

    同时给 ``text``（生效）和 ``default_text``（内置默认）是刻意的：界面要能
    显示"现在用的是默认，还是你改过"，而"改过"必须能一键退回默认。
    只给生效值的话，前端只能自己复制一份默认，迟早和服务端走散。
    """

    key: str
    label: str
    when: str
    text: str
    default_text: str
    custom: bool


class PersonaOut(BaseModel):
    persona_id: str
    revision: int
    name: str
    display_name: str
    system_prompt: str
    traits: dict
    boundaries: list[str]
    scenario_prompts: dict
    scenarios: list[ScenarioOut]


class PersonaUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=64)
    system_prompt: str | None = Field(default=None, max_length=8000)
    traits: dict | None = None
    boundaries: list[str] | None = None
    # 只放覆盖；某个键给空串 = 恢复默认。未知键直接报错，不静默丢弃——
    # 拼错一个键名而"保存成功、什么都不生效"是最难查的一类错。
    scenario_prompts: dict[str, str] | None = None


def _persona_out(view: PersonaView) -> PersonaOut:
    overrides = dict(view.scenario_prompts or {})
    return PersonaOut(
        persona_id=view.persona_id,
        revision=view.revision,
        name=view.name,
        display_name=view.display_name,
        system_prompt=view.system_prompt,
        traits=dict(view.traits or {}),
        boundaries=list(view.boundaries or []),
        scenario_prompts=overrides,
        scenarios=[
            ScenarioOut(
                key=s.key,
                label=s.label,
                when=s.when,
                text=effective_text(overrides, s.key),
                default_text=s.default,
                custom=is_custom(overrides, s.key),
            )
            for s in SCENARIOS
        ],
    )


@router.get("/persona", response_model=PersonaOut)
async def get_persona(auth: CurrentAuth, persona: PersonaDep) -> PersonaOut:
    view = await persona.get()
    return _persona_out(view)


@router.put("/persona", response_model=PersonaOut)
async def update_persona(
    body: PersonaUpdate,
    auth: CurrentAuth,
    persona: PersonaDep,
    session: DbSession,
    settings: SettingsDep,
) -> PersonaOut:
    if body.scenario_prompts is not None:
        unknown = sorted(set(body.scenario_prompts) - known_keys())
        if unknown:
            # 报错而不是丢弃：这些键在提示词里没有任何注入点，接受下来
            # 等于承诺了一件不会发生的事。
            raise LingError(
                ErrorCode.invalid_request,
                f"未知的场景键：{'、'.join(unknown)}",
                status_code=400,
                detail=f"可用键：{'、'.join(sorted(known_keys()))}",
            )
    view = await persona.update(
        display_name=body.display_name,
        system_prompt=body.system_prompt,
        traits=body.traits,
        boundaries=body.boundaries,
        scenario_prompts=body.scenario_prompts,
    )
    # 改名后要立刻失效名字缓存：否则"剥离自己名字前缀"还在按旧名判断，
    # 新前缀就漏过去了。
    from ling.capabilities.naming import invalidate_names

    invalidate_names()
    # 角色设定变更属于权威时间线的一部分：管理员要能追溯"为什么语气变了"
    changed = [
        name
        for name, value in (
            ("display_name", body.display_name),
            ("system_prompt", body.system_prompt),
            ("traits", body.traits),
            ("boundaries", body.boundaries),
            ("scenario_prompts", body.scenario_prompts),
        )
        if value is not None
    ]
    sequencer = ActorSequencer(
        session, actor_id=settings.actor_id, persona_id=settings.persona_id
    )
    await sequencer.commit_event(
        type="persona.updated",
        payload={"persona_id": view.persona_id, "revision": view.revision, "fields": changed},
        origin=Origin.user_statement,
        subject_id=auth.user.subject_id,
    )
    return _persona_out(view)


@router.get("/actor/timeline")
async def timeline(
    auth: CurrentAuth, session: DbSession, settings: SettingsDep, limit: int = 50
) -> dict[str, object]:
    seq = ActorSequencer(session, actor_id=settings.actor_id, persona_id=settings.persona_id)
    events = await seq.timeline(limit=limit)
    return {
        "events": [
            {
                "id": e.id,
                "type": e.type,
                "origin": e.origin,
                "visibility": e.visibility,
                "revision": e.revision,
                "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
                "window_id": e.window_id,
                "summary": _summarize(e.type, e.payload or {}),
            }
            for e in events
        ]
    }


def _summarize(event_type: str, payload: dict) -> str:
    if event_type == "turn.completed":
        chars = payload.get("chars", 0)
        model = payload.get("model") or "未知模型"
        return f"完成一轮回复（{chars} 字，{model}）"
    if event_type == "persona.updated":
        return "角色设定更新"
    return event_type


__all__ = ["router"]
