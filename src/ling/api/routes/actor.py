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


# 人设正文的**接口侧**上限（字符）。原来 8000。
#
# 为什么必须跟着预算一起改：`DEFAULT_BUDGETS["persona"]` 已提到 10 万 token
# （≈20 万字符，见 `kernel/context.py`）。如果这里还卡 8000 字符，用户看到的是
# "预算有 10 万 token"，实际却连 8000 字都存不进去——上限成了摆设，而且
# 报错是 422 `string_too_long`，跟"预算"这个词毫无关系，没人会想到是这里。
#
# 取 200000 = 人设车道的字符上限（token × 2），让**接口与预算同一个数**。
# 再大就没意义了：超出的部分反正进不了提示词（车道会截断并留痕）。
PERSONA_PROMPT_MAX_CHARS = 200_000


class PersonaUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=64)
    system_prompt: str | None = Field(default=None, max_length=PERSONA_PROMPT_MAX_CHARS)
    traits: dict | None = None
    boundaries: list[str] | None = None
    # 只放覆盖；某个键给空串 = 恢复默认。未知键直接报错，不静默丢弃——
    # 拼错一个键名而"保存成功、什么都不生效"是最难查的一类错。
    scenario_prompts: dict[str, str] | None = None


def validate_scenario_keys(scenario_prompts: dict[str, str] | None) -> None:
    """场景覆盖里出现未知键 → 400。

    报错而不是丢弃：这些键在提示词里没有任何注入点，接受下来等于承诺了一件
    不会发生的事。写成公开函数是因为 `routes/persona.py` 的创建/更新也要
    同一份判据——两个接口各写一遍"哪些键合法"，迟早有一个漏掉新场景。
    """
    if scenario_prompts is None:
        return
    unknown = sorted(set(scenario_prompts) - known_keys())
    if unknown:
        raise LingError(
            ErrorCode.invalid_request,
            f"未知的场景键：{'、'.join(unknown)}",
            status_code=400,
            detail=f"可用键：{'、'.join(sorted(known_keys()))}",
        )


# 旧名保留：已有调用点与测试用的是 `_validate_scenarios`。
_validate_scenarios = validate_scenario_keys


class PersonaActiveOut(PersonaOut):
    """当前激活人格的返回体：旧字段一个不少，另加两个"生效来源"字段。

    - ``active``：这一份是不是**当前激活**的那一套。旧 ``GET /v1/persona``
      永远是 ``true``；但新装的"暂无人设"也是它给的，此时 ``true`` 表示
      "当前激活的就是这条（空）人格"。
    - ``exists``：库里**是否真有这一行**。新装是 ``false``（配置里的
      ``persona_id`` 指向一个还不存在的 id）。前端据此显示"暂无人设 /
      创建人格"，而不是把一个全空的表单当成"你已经建好了人格"。
    """

    active: bool = True
    exists: bool = True


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


@router.get("/persona", response_model=PersonaActiveOut)
async def get_persona(auth: CurrentAuth, persona: PersonaDep) -> PersonaActiveOut:
    """**当前激活**的人格（单例快捷方式，多套人格见 `/v1/personas`）。

    ## 为什么保留

    前端与外部脚本已经在调它（`docs/API-WEB-V1.md` §5），改成 404 或换形状
    会一次打断所有调用方，而这个接口的语义在多实例时代依然成立：
    "现在是哪一套人设"。所以它是 `/v1/personas` 之上的一层**复合读**：

    1. 从已发布配置取 ``persona_id``（唯一真相来源）；
    2. 读那一行；**没有这一行就是新装** —— 不再像旧版那样顺手建一行
       ``persona_ling``。这里返回一版全空的视图 + ``exists=false``，
       与老库"有 ``persona_ling`` 那一行"的差异**只在 ``exists`` 这个字段**上，
       注入行为完全一致（三列全空 = 什么都不注入）。
    """
    view = await persona.get_or_none()
    if view is None:
        from ling.domains.persona import current as persona_current
        from ling.domains.persona.service import empty_persona_view

        # `get_or_none()` 已经把"该用哪一套"解析过了；这里再取一次 id 只是为了
        # 告诉前端"配置指向的是哪个（还不存在的）人格"，不改变任何行为。
        active_id = await persona_current.effective_persona_id()
        return PersonaActiveOut(**_persona_out(empty_persona_view(active_id)).model_dump(), exists=False)
    return PersonaActiveOut(**_persona_out(view).model_dump())


@router.put("/persona", response_model=PersonaActiveOut)
async def update_persona(
    body: PersonaUpdate,
    auth: CurrentAuth,
    persona: PersonaDep,
    session: DbSession,
    settings: SettingsDep,
) -> PersonaActiveOut:
    """改**当前激活**的人格（快捷方式）。

    ## 新装时的行为变了（这是刻意的）

    旧版：行不存在 → `ensure_default()` **静默建一行** → 写进去。
    这一版：行不存在 → 先建一行（用**当前激活的 id**，`create(..., persona_id=...)`）
    → 再写入，并把 ``exists`` 报成 ``true``。为什么这里是"建"而不是 404：

    - 这个接口的语义是"设置现在这套人设"。用户在人设页填内容点保存时，
      库里可能一行都没有（新装）；报 404 等于要他先点一次"创建人格"再重填一遍，
      而这个页面在旧版一直是"填了就存"的。
    - 与 `PUT /v1/personas/{id}` **不同**：那一个明确要求行存在（编辑某张卡），
      这一条是"当前激活人格的编辑器"，创建目标是**配置里指定的那个 id**，
      不存在歧义（不会像旧的 `get()` 那样"传错 id 就造一个"）。

    创建即"设为当前"是**必然**的：它本来就是当前激活id的那个位置。
    """
    validate_scenario_keys(body.scenario_prompts)
    created = False
    if await persona.get_or_none() is None:
        from ling.domains.persona import current as persona_current

        active_id = await persona_current.effective_persona_id()
        if not active_id:
            # 配置里**没有任何**人格 id（修订里显式写了空串）：这时没有
            # "当前那个位置"可以建，只能让用户去新建一张卡。
            raise LingError(
                ErrorCode.invalid_request,
                "当前没有激活的人格：先创建一个人格并设为当前",
                status_code=409,
                detail="no_active_persona",
            )
        await persona.create(
            persona_id=active_id,
            display_name=body.display_name or "",
            system_prompt=body.system_prompt or "",
            traits=body.traits,
            boundaries=body.boundaries,
            scenario_prompts=body.scenario_prompts,
        )
        created = True
        # 刚建的行已经带上这次提交的内容；下面 `update` 只该"再改一次"时跑，
        # 否则 revision 会白加一（前端按 revision 判断"有没有人改过"）。
        view = await persona.get()
    else:
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
    # 事件记在**实际被改的那一套**人格上，而不是环境变量里的那个 id：
    # 多套并存之后，"当前人格"是 `view.persona_id`，两者可以不同
    # （例如切到 B 之后配置里是 B，而 `settings.persona_id` 仍是 A）。
    sequencer = ActorSequencer(
        session, actor_id=settings.actor_id, persona_id=view.persona_id
    )
    await sequencer.commit_event(
        type="persona.updated",
        payload={"persona_id": view.persona_id, "revision": view.revision, "fields": changed},
        origin=Origin.user_statement,
        subject_id=auth.user.subject_id,
    )
    if created:
        return PersonaActiveOut(**_persona_out(view).model_dump(), exists=True)
    return PersonaActiveOut(**_persona_out(view).model_dump())


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
    # 多套人格之后新增的三种事件。摘要要给**管理员看得懂的中文**：
    # 时间线是"为什么语气变了"的追溯入口，一行 `persona.activated` 等于没说。
    name = str(payload.get("display_name") or payload.get("persona_id") or "")
    if event_type == "persona.created":
        return f"新建人格「{name}」" if name else "新建人格"
    if event_type == "persona.deleted":
        return f"删除人格「{name}」" if name else "删除人格"
    if event_type == "persona.activated":
        revision = payload.get("config_revision")
        suffix = f"（配置修订 {revision}）" if revision is not None else ""
        return f"切换当前人格为「{name}」{suffix}" if name else f"切换当前人格{suffix}"
    return event_type


__all__ = ["router"]
