"""人格路由：多套人格并存、同一时间只有一套生效。

## 为什么单独一个模块（`routes/persona.py`）而不是塞进 `actor.py`

`actor.py` 是"角色设定 + 权威事件时间线"，旧版的 `GET/PUT /v1/persona` 是
**单例**语义（总是那一个人格）。多实例之后，"列表/新建/删除/切换"是另一组
资源（`/v1/personas` 复数），与单例路由放在一起会让"哪个是当前、哪个是某一张卡"
在同一个文件里来回跳。这里只放复数资源；单例两个路由留在 `actor.py`
（它们现在是"当前激活人格"的**快捷方式**，见那边的注释）。

## 权限：全部 owner

与"改人设就是改她是谁"的判断一致。旧版 `PUT /v1/persona` **没有做角色判断**
（任何已登录用户都能改人设）——这条特性新增了"删除人格"这种**不可撤销**的
动作（删除时还会连带让该人格名下的窗口失去归属），必须收紧。只读接口
（列表/详情）仍对已登录用户开放：非 owner 看到的是"有哪些人格",
但**不能**创建、修改、删除、切换。

## 错误码

| 情况 | 状态码 | `code` | `detail` |
| --- | --- | --- | --- |
| id 不存在 | 404 | `not_found` | `persona_not_found` |
| 删的是当前激活的人格 | 409 | `conflict` | `persona_active` |
| 还有非空会话挂在它名下 | 409 | `conflict` | `persona_in_use` |
| 切换目标不存在 | 404 | `not_found` | `persona_not_found` |
| 非 owner 写 | 403 | `forbidden` | `not_owner` |
| 场景键写错 / 名字超长 | 400 / 422 | `invalid_request` | 见响应正文 |
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ling.api.deps import CurrentAuth, DbSession, PersonaDep
from ling.api.errors import LingError

# 与旧接口共用同一份字段定义与序列化，避免"哪些字段可改 / 场景键怎么报错"
# 在两个接口里各写一份、然后走散。`actor.py` 不在导入期反向依赖本模块，
# 所以这条导入没有环。
from ling.api.routes.actor import (
    PERSONA_PROMPT_MAX_CHARS,
    PersonaOut,
    PersonaUpdate,
    _persona_out,
    validate_scenario_keys,
)
from ling.audit import audit_event
from ling.contracts.v1 import ErrorCode, Origin
from ling.domains.persona import current as persona_current
from ling.domains.persona.service import (
    PersonaActiveError,
    PersonaInUseError,
    PersonaNotFoundError,
    PersonaView,
)
from ling.kernel.turn import ActorSequencer

router = APIRouter(tags=["persona"])

# 名字长度上限：与 `infra/models.py::Persona.display_name` 的列宽一致（64）。
# 在接口层也卡一次，是为了给出"名字太长"这种**能看懂**的 422，而不是等到
# flush 时由数据库抛 `StringDataRightTruncation`（那时整笔事务的信息都没了）。
PERSONA_DISPLAY_NAME_MAX_CHARS = 64


class PersonaListItem(BaseModel):
    """列表页一张卡。

    刻意**不带** `system_prompt` / `traits` / `boundaries` / `scenarios`：
    卡片上要显示的只有名字、"是不是当前"、"有没有写内容"。正文可能很长
    （上限 20 万字符），列表接口带上它会把一次列表变成几百 KB 的响应，
    而前端真正需要正文时点开详情即可。

    ``is_active`` 由服务端算：前端自己拿 ``GET /persona`` 的 id 去比也可以，
    但"当前是谁"的判据只能有一份（见 `domains/persona/current.py`）。
    ``has_content`` 用的就是 `persona_is_empty()` 同一个判据——它决定卡片上
    显示"暂未填写"还是"已写 N 字"，与注入侧是否真的会注入**是同一个答案**。
    """

    persona_id: str
    revision: int
    name: str
    display_name: str
    is_active: bool
    has_content: bool
    system_prompt_chars: int
    trait_count: int
    boundary_count: int
    scenario_count: int
    published: bool


class PersonaListOut(BaseModel):
    items: list[PersonaListItem]
    # 当前激活的人格 id。新装时它可能是"指向一个不存在行"的 id
    # （见 `domains/persona/current.py`），所以**不要**拿它去 items 里找，
    # 找不到就是"暂无人设"。
    active_persona_id: str
    # 一条都没有时前端显示"暂无人设 + 创建人格"，与 `active_persona_id`
    # 指不指向任何卡片是两回事：删光之后 active 仍然有值（它就是一个 id）。
    total: int


class PersonaCreate(BaseModel):
    display_name: str = Field(default="", max_length=PERSONA_DISPLAY_NAME_MAX_CHARS)
    system_prompt: str = Field(default="", max_length=PERSONA_PROMPT_MAX_CHARS)
    traits: dict | None = None
    boundaries: list[str] | None = None
    scenario_prompts: dict[str, str] | None = None


class ActivateOut(BaseModel):
    """切换结果。

    ``revision`` 是新发布的配置修订号：切换人格 = 发布一条新修订（这样它
    才能跨进程生效、能回滚）。前端不需要它，但排障时需要——"我切了，
    但 worker 还在旧人格"这种问题的第一个问题是"修订号变了没有"。
    """

    persona_id: str
    config_revision: int


def _require_owner(auth: CurrentAuth) -> None:
    if str(getattr(auth.user, "role", "")) != "owner":
        raise LingError(
            ErrorCode.forbidden,
            "只有所有者可以修改人格",
            status_code=403,
            detail="not_owner",
        )


def _not_found(persona_id: str) -> LingError:
    return LingError(
        ErrorCode.not_found,
        f"人格不存在：{persona_id}",
        status_code=404,
        detail="persona_not_found",
    )


def _validate_body(body: PersonaCreate) -> None:
    validate_scenario_keys(body.scenario_prompts)


def _item(view: PersonaView, *, active_id: str) -> PersonaListItem:
    from ling.domains.persona.service import persona_is_empty

    return PersonaListItem(
        persona_id=view.persona_id,
        revision=view.revision,
        name=view.name,
        display_name=view.display_name,
        is_active=view.persona_id == active_id,
        has_content=not persona_is_empty(
            system_prompt=view.system_prompt,
            traits=view.traits,
            boundaries=view.boundaries,
        ),
        system_prompt_chars=len(view.system_prompt or ""),
        trait_count=len(view.traits or {}),
        boundary_count=len(view.boundaries or []),
        scenario_count=len(view.scenario_prompts or {}),
        published=True,
    )


@router.get("/personas", response_model=PersonaListOut)
async def list_personas(auth: CurrentAuth, persona: PersonaDep) -> PersonaListOut:
    """全部人格（按创建顺序）+ 当前激活的那个。

    **无人格时返回空列表 200**，不是 404：新装就是"暂无人设"，那是一个
    正常状态，前端据此渲染"创建人格"入口。
    """
    active_id = await persona_current.effective_persona_id()
    items = [_item(view, active_id=active_id) for view in await persona.list_personas()]
    return PersonaListOut(items=items, active_persona_id=active_id, total=len(items))


@router.post("/personas", response_model=PersonaOut, status_code=201)
async def create_persona(
    body: PersonaCreate, auth: CurrentAuth, persona: PersonaDep, session: DbSession
) -> PersonaOut:
    """新建一张人格卡。**id 由服务端生成**（``persona_<slug>-<6 位随机>``）。

    **不会自动设为当前激活**：见 `PersonaService.create` 的说明——创建之后
    用户的第一件事通常是写内容，创建即生效等于把正在用的人格先换成一个
    空的。"设为当前"要显式调 ``POST /v1/personas/{id}/activate``。
    """
    _require_owner(auth)
    _validate_body(body)
    settings = _settings()
    view = await persona.create(
        display_name=body.display_name,
        system_prompt=body.system_prompt,
        traits=body.traits,
        boundaries=body.boundaries,
        scenario_prompts=body.scenario_prompts,
    )
    await _record(
        session,
        subject_id=auth.user.subject_id,
        actor_id=settings.actor_id,
        persona_id=view.persona_id,
        event_type="persona.created",
        payload={
            "persona_id": view.persona_id,
            "display_name": view.display_name,
            "revision": view.revision,
        },
    )
    audit_event(
        "persona_created",
        actor=str(auth.user.id),
        target_type="persona",
        target_id=view.persona_id,
        attributes={"display_name": view.display_name},
    )
    return _persona_out(view)


@router.get("/personas/{persona_id}", response_model=PersonaOut)
async def get_persona_detail(
    persona_id: str, auth: CurrentAuth, persona: PersonaDep
) -> PersonaOut:
    """某一张卡的完整设定（含场景生效/默认对照）。"""
    try:
        view = await persona.get(persona_id)
    except PersonaNotFoundError as exc:
        raise _not_found(persona_id) from exc
    return _persona_out(view)


@router.put("/personas/{persona_id}", response_model=PersonaOut)
async def update_persona_detail(
    persona_id: str,
    body: PersonaUpdate,
    auth: CurrentAuth,
    persona: PersonaDep,
    session: DbSession,
) -> PersonaOut:
    """改某一张卡。**行不存在就 404，不会顺手建一行。**

    与旧 `PUT /v1/persona` 的区别只有"改哪一个"：那个改的是**当前激活**
    的人格，这个改的是指定的那一个（卡片点「编辑」时用）。
    """
    _require_owner(auth)
    validate_scenario_keys(body.scenario_prompts)
    try:
        view = await persona.update(
            persona_id=persona_id,
            display_name=body.display_name,
            system_prompt=body.system_prompt,
            traits=body.traits,
            boundaries=body.boundaries,
            scenario_prompts=body.scenario_prompts,
        )
    except PersonaNotFoundError as exc:
        raise _not_found(persona_id) from exc
    # 改名后立刻失效名字缓存：否则"剥离自己名字前缀"还按旧名判断。
    from ling.capabilities.naming import invalidate_names

    invalidate_names()
    changed = [
        field
        for field, value in (
            ("display_name", body.display_name),
            ("system_prompt", body.system_prompt),
            ("traits", body.traits),
            ("boundaries", body.boundaries),
            ("scenario_prompts", body.scenario_prompts),
        )
        if value is not None
    ]
    await _record(
        session,
        subject_id=auth.user.subject_id,
        actor_id=_settings().actor_id,
        persona_id=view.persona_id,
        event_type="persona.updated",
        payload={
            "persona_id": view.persona_id,
            "revision": view.revision,
            "fields": changed,
        },
    )
    return _persona_out(view)


@router.delete("/personas/{persona_id}", status_code=204)
async def delete_persona(
    persona_id: str, auth: CurrentAuth, persona: PersonaDep, session: DbSession
) -> None:
    """删一张卡。**当前激活的不能删**（先切走）；有非空会话的也不能删。

    **不删**它名下的窗口与 `actor_events`：那些行的 `persona_id` 是字符串列，
    不是外键（见 `PersonaService.delete` 的说明）。事件保留原值 = "当时发生
    过什么"的记录不被改写。
    """
    _require_owner(auth)
    try:
        view = await persona.delete(persona_id)
    except PersonaNotFoundError as exc:
        raise _not_found(persona_id) from exc
    except PersonaActiveError as exc:
        raise LingError(
            ErrorCode.conflict,
            f"「{persona_id}」是当前激活的人格，先切换到别的人格再删除",
            status_code=409,
            detail="persona_active",
        ) from exc
    except PersonaInUseError as exc:
        raise LingError(
            ErrorCode.conflict,
            f"「{persona_id}」名下还有 {exc.in_use_windows} 个有内容的会话，"
            "先归档或清空这些会话再删除",
            status_code=409,
            detail=f"persona_in_use:{exc.in_use_windows}",
        ) from exc
    await _record(
        session,
        subject_id=auth.user.subject_id,
        actor_id=_settings().actor_id,
        persona_id=persona_id,
        event_type="persona.deleted",
        payload={"persona_id": persona_id, "display_name": view.display_name},
    )
    audit_event(
        "persona_deleted",
        actor=str(auth.user.id),
        target_type="persona",
        target_id=persona_id,
        attributes={"display_name": view.display_name},
    )
    return None


@router.post("/personas/{persona_id}/activate", response_model=ActivateOut)
async def activate_persona(
    persona_id: str, auth: CurrentAuth, persona: PersonaDep, session: DbSession
) -> ActivateOut:
    """切换当前激活的人格：改配置里的 ``persona_id`` 并**发布一条新修订**。

    ## 为什么是"发布修订"而不是改某个开关

    真相来源是 `EffectiveConfig.persona_id`（见 `domains/persona/current.py`）。
    切换 = 把新值写进修订并发布，于是：

    - 跨进程生效（其它 API 副本与 worker 靠修订号变化感知，不需要新机制）；
    - 可回滚（``POST /settings/rollback`` 回到上一版修订时人格也一起回去）；
    - 原子（两条 SQL 同一事务，不会停在"草稿已存、还没发布"）。

    ## 拒绝条件

    目标行必须存在：切到一个不存在的 id 会让"当前激活"指向一个空人格，
    界面上看起来像"人格被清空了"，而实际是切错了——所以 404，不落地。

    ## 切换的语义是"整套换"

    - 新轮次读新人格的 `system_prompt` / `traits` / `boundaries` / 场景；
    - **已存在的窗口**仍挂在原人格 id 上（它们记录的是"当时是哪一套人设在说"）。
      不把历史对话改归属：那等于把旧人格说过的话重新算到新人格头上；
    - 名字缓存立刻失效（切人格往往也换了名字）。
    """
    _require_owner(auth)
    try:
        view = await persona.get(persona_id)
    except PersonaNotFoundError as exc:
        raise _not_found(persona_id) from exc

    from ling.settings.service import ConfigService

    config = ConfigService(
        session,
        _secret_box(),
        test_mode_default=_settings().test_mode,
        persona_id=_settings().persona_id,
        log_denials_default=_settings().log_access_denials,
    )
    revision = await config.set_active_persona(persona_id, changed_by=str(auth.user.id))
    # 同一会话里立刻回读要新值（缓存见 `current.invalidate`）。
    persona_current.invalidate(session)
    from ling.capabilities.naming import invalidate_names

    invalidate_names()
    await _record(
        session,
        subject_id=auth.user.subject_id,
        actor_id=_settings().actor_id,
        # 事件挂在**切换后**的人格上：这条事件属于"从此以后生效的那一套"。
        persona_id=persona_id,
        event_type="persona.activated",
        payload={
            "persona_id": persona_id,
            "display_name": view.display_name,
            "config_revision": revision,
        },
    )
    audit_event(
        "persona_activated",
        actor=str(auth.user.id),
        target_type="persona",
        target_id=persona_id,
        attributes={"config_revision": revision},
    )
    return ActivateOut(persona_id=persona_id, config_revision=revision)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def _settings():  # noqa: ANN202
    from ling.config import get_settings

    return get_settings()


def _secret_box():  # noqa: ANN202
    from ling.infra.security import SecretBox

    return SecretBox(_settings())


async def _record(
    session,  # noqa: ANN001
    *,
    subject_id: str,
    actor_id: str,
    persona_id: str,
    event_type: str,
    payload: dict,
) -> None:
    """往权威时间线写一条人格事件。

    ``ActorSequencer`` 要 `actor_id` / `persona_id` 两个作用域；这里都从
    **已认证的身份与配置**取，绝不从请求体取（请求体里的 actor/persona
    一律忽略是本仓库的既有约定）。
    """
    settings = _settings()
    sequencer = ActorSequencer(
        session,
        actor_id=actor_id or settings.actor_id,
        persona_id=persona_id,
    )
    await sequencer.commit_event(
        type=event_type,
        payload=payload,
        origin=Origin.user_statement,
        subject_id=subject_id,
    )


__all__ = ["PERSONA_DISPLAY_NAME_MAX_CHARS", "router"]

