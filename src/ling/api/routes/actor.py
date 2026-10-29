"""角色路由：Persona 设定与权威事件时间线。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ling.api.deps import CurrentAuth, DbSession, PersonaDep, SettingsDep
from ling.contracts.v1 import Origin
from ling.kernel.turn import ActorSequencer

router = APIRouter(tags=["actor"])


class PersonaOut(BaseModel):
    persona_id: str
    revision: int
    name: str
    display_name: str
    system_prompt: str
    traits: dict
    boundaries: list[str]


class PersonaUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=64)
    system_prompt: str | None = Field(default=None, max_length=8000)
    traits: dict | None = None
    boundaries: list[str] | None = None


@router.get("/persona", response_model=PersonaOut)
async def get_persona(auth: CurrentAuth, persona: PersonaDep) -> PersonaOut:
    view = await persona.get()
    return PersonaOut(**view.__dict__)


@router.put("/persona", response_model=PersonaOut)
async def update_persona(
    body: PersonaUpdate,
    auth: CurrentAuth,
    persona: PersonaDep,
    session: DbSession,
    settings: SettingsDep,
) -> PersonaOut:
    view = await persona.update(
        display_name=body.display_name,
        system_prompt=body.system_prompt,
        traits=body.traits,
        boundaries=body.boundaries,
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
    return PersonaOut(**view.__dict__)


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
