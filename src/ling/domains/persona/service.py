"""Persona 域：角色稳定设定、叙事边界与人格版本。

唯一写入方是本模块。切换人格不会自动继承另一人格的私聊内容
（继承逻辑属于 Conversation/Consent，不在这里）。
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.persona.scenarios import clean_overrides
from ling.infra.models import Persona, Window, utcnow

logger = logging.getLogger("ling.domains.persona")

# ## 没有"默认人设"
#
# 用户 2026-11-11 拍板：「可以把默认人设去掉，不填就是直连模型+其他功能提示词，
# 不注入人设」，并在"空人设怎么处理"的选项里选了**「什么都不注入（最彻底）」**。
# 所以这里**一个默认人设常量都没有**：
#
# - 库里三列为空 → `kernel/context.py` 的 persona / constraints / scenario
#   三段**一条都不发**，模型直接收到"功能提示词"（工具说明、记忆、知识、媒体、
#   self_state、关系），而不是任何替使用者写好的角色；
# - 没有"读取时回落"——回落会让前端的空输入框显示成默认值，用户一点保存，
#   默认值就又被写进库、从此冻结。这一版把 `effective_prompt` 那一族回落
#   函数连同迁移 `0064_persona_defaults_fallback` 一起删掉了。
#
# **为什么不是"把 DEFAULT_* 改成空值"**：这个名字本身就在说"这里有一份默认
# 人设"。留三个空壳常量，下一个人会往里填内容，而且每个引用点都得写
# `if DEFAULT_SYSTEM_PROMPT:` 这种恒假判断——那是"语义为空、形状还在"的坑。
# 删掉常量之后，"没有人设"是代码里唯一能表达的状态，想加回来必须先改名字，
# 改名字就必须先解释为什么。
#
# ## 身份表述归人设：这一版是"没有立场可删"
#
# 2026-11-10 的要求「不是必须承认是 ai，而是根据人设决定，不额外注入，
# 有的人想要拟人，有的人想要 ai」（`docs/AUTONOMY-DESIGN.md` §5.5）现在由
# "没有默认"直接满足：代码不再替任何使用者选边。
#
# 代价要说清：**事实纪律（不编造履历、没回执不声称已做到、不泄露私聊）
# 以前靠默认人设捎带，现在不再有任何默认载体**。空人设 = 这些也不进提示词。
# 想要就写进人设——这是"行为归人设"那一侧该在的位置；代码侧仍然保证的
# 不是"说什么"，而是"不把模拟当真实回执"（见 `kernel/self_view.py`、
# `domains/presence/service.py` 的结构化来源标记）。


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


# --------------------------------------------------------------------------- #
# 「人设是否为空」——**唯一的判据**，注入侧与 API 都问这一个函数
# --------------------------------------------------------------------------- #
#
# 放在这里而不是各处自己写 `if persona.system_prompt or persona.traits or ...`：
# 同一句话在 `kernel/context.py`（决定发不发）、前端（决定显示"暂无人设"还是表单）
# 各算一遍，迟早会分歧成"页面说有、提示词里没有"——这类分歧极难排查。
#
# **display_name 不算人设。** 依据（读代码得出的，不是猜的）：
#
# - `display_name` 的消费方都不是"人格设定"：`capabilities/naming.py` 拿它剥
#   "她自己加的名字前缀"，`domains/agenda/timeline.py`、`domains/dream/service.py`
#   拿它当叙事里的称谓，`worker.py` 排日程时写一句"你是{display_name}"。这些
#   都带 `or persona.name or "她"` 的兜底，名字缺失也不影响功能。
# - 它不可清空：`PUT /v1/persona` 传空串会写成 `""`，但上面那些消费方立刻
#   回落到 `name`（默认 `"ling"`），而人设页的输入框也会是空的。也就是说
#   "名字为空"本身不是一个稳定状态，"名字非空"也就不可能代表"有人设"。
# - 新装用户的行是 `display_name="ling"`：如果把它算作人设，**每一个新装用户**
#   都会被判成"有人设"，那么"暂无人设 + 新建人设"的空状态永远不会出现。
#
# 所以：`system_prompt` / `traits` / `boundaries` 三个字段才是人设。


def persona_is_empty(
    *,
    system_prompt: object = None,
    traits: object = None,
    boundaries: object = None,
) -> bool:
    """三个字段**全空**才算"没有人设"。

    判据宽严的取舍（两个方向都会出错，这里选的是"宽"）：

    - `system_prompt` 只有空白（``"   \n"``）算空——空白不是设定，注入它只会
      让模型收到一段空指令。比 `kernel/context.py` 最终那道 ``text.strip()``
      过滤更早、更明确地把它归到"空"。
    - `traits` 里**有键但值全空**（``{"a": ""}``）算空：`format_traits` 渲染出来
      就是空串，注入侧本来也会丢掉它。两边判断必须一致，否则页面会显示"已写
      人设"而提示词里什么都没有。
    - 非 dict / 非 list 的历史脏数据一律按空处理，不抛异常。
    """
    prompt_empty = not (isinstance(system_prompt, str) and system_prompt.strip())
    traits_empty = not (
        isinstance(traits, dict) and format_traits(traits).strip()
    )
    boundaries_empty = not (
        isinstance(boundaries, list)
        and any(isinstance(item, str) and item.strip() for item in boundaries)
    )
    return prompt_empty and traits_empty and boundaries_empty


def empty_persona_view(persona_id: str = "") -> PersonaView:
    """"这一轮没有人格"的空视图（**不是**一个可以直接落库的人格）。

    谁需要它：注入侧（`kernel/context.py`）与读名字的地方（`capabilities/naming.py`、
    `domains/dream`、`domains/agenda`）原本假设 `personas` 里**至少有一行**。
    `ensure_default()` 不再建行之后，"一行都没有"是正常状态，那些地方如果直接
    `await service.get(...)` 会拿到 404/异常。这里给它们一个**明确的空**：

    - 三列全空 → `view_is_empty()` 为真 → 注入侧三段一起跳过（"暂无人设"）；
    - ``display_name`` 为空 → 调用方原有的 `or "她"` 兜底照常生效；
    - ``persona_id`` 原样带回（可能是那个"指向不存在行"的配置 id），
      日志与 meta 要能指出"这一轮用的是哪个 id、但它没有人格"。

    它**不写库**、也不被任何写路径接受：`PersonaService.update` 只认 exist 的行。
    """
    return PersonaView(
        persona_id=persona_id,
        revision=0,
        name="",
        display_name="",
        system_prompt="",
        traits={},
        boundaries=[],
        scenario_prompts={},
    )


def view_is_empty(view: PersonaView) -> bool:
    """`PersonaView` 版本：注入侧与 API 都用它，不各自拆字段。"""
    return persona_is_empty(
        system_prompt=view.system_prompt,
        traits=view.traits,
        boundaries=view.boundaries,
    )


def new_persona_id(display_name: str = "", *, taken: set[str] | None = None) -> str:
    """生成一个新人格的 id：``persona_<slug>-<6 位随机后缀>``。

    ## 为什么不是纯 uuid

    id 会出现在 URL（``/v1/personas/{id}``）、日志、``actor_events.persona_id``
    与会话窗口的 ``persona_id`` 里。纯 uuid 在这些地方只有"能区分"这一个好处，
    而人看日志时完全不知道它对应哪个人格。slug 前缀让排查时一眼能认出来。
    也不会出现 ``personas`` 里只有 ``persona_ling`` 一条、别处却出现
    ``persona_<uuid>`` 这种"看不出是同一个特性"的割裂感。

    ## 为什么不是纯 slug（``persona_温柔``）

    纯 slug 有两个硬伤：中文名转出来是空串（要么退化成序号，要么退化成随机），
    而且"同名就加序号"会让删除重建后 id 复用——``actor_events`` 里那条旧人格的
    历史会被新人格"继承"，而人格切换的本意恰恰是**整套换掉、不继承**。

    ## 后缀为什么是随机的而不是序号

    序号要求"先查再写"，两个并发创建会撞主键；而且 ``persona_x-2`` 读起来像是
    "x 的第二个版本"，实际是另一个人格。随机后缀让 id 天然唯一，也直观地
    表示"这是一个独立的人格"，而不是某个 id 的修订号（修订号是 ``revision``）。

    ``taken`` 只用来给 slug 去重（同名人格的 slug 相同，后缀不同就够区分了），
    所以前缀冲突时换一个后缀即可，不需要递增。
    """
    taken = taken or set()
    slug = re.sub(r"[^a-z0-9]+", "-", (display_name or "").strip().lower()).strip("-")[:32]
    while True:
        candidate = f"persona_{slug}-{secrets.token_hex(3)}" if slug else f"persona_{secrets.token_hex(4)}"
        # 兜底：极端情况下撞了已有 id，再生成一次。不能静默返回重复 id
        # （那会让 `session.add` 抛主键冲突，错在前一个调用栈里，很难读）。
        if candidate not in taken:
            return candidate


# --------------------------------------------------------------------------- #
# 服务层错误：**给 API 层翻译成状态码用**，不直接抛 HTTP 异常
# --------------------------------------------------------------------------- #
#
# 放在这一层（而不是路由里）是因为同一个判据还会被 worker / 脚本用到：
# "删掉当前激活的人格"在任何一个入口都必须被拒绝，判据只能有一份。


class PersonaNotFoundError(LookupError):
    """人格不存在。**以前这里会静默建行**，所以这个类型本身就是一处行为变更。"""

    def __init__(self, persona_id: str) -> None:
        super().__init__(f"人格不存在：{persona_id}")
        self.persona_id = persona_id


class PersonaConflictError(ValueError):
    """id 冲突之类的"格式对但状态不允许"。"""


class PersonaActiveError(PersonaConflictError):
    """要删的是**当前激活**的人格。必须先切到另一套再删。"""

    def __init__(self, persona_id: str) -> None:
        super().__init__(f"{persona_id} 是当前激活的人格，先切换到别的人格再删除")
        self.persona_id = persona_id


class PersonaInUseError(PersonaConflictError):
    """还有非空会话挂在这个人格名下。"""

    def __init__(self, persona_id: str, in_use_windows: int) -> None:
        super().__init__(
            f"{persona_id} 名下还有 {in_use_windows} 个有内容的会话，先归档或清空再删除"
        )
        self.persona_id = persona_id
        self.in_use_windows = in_use_windows


# --------------------------------------------------------------------------- #
# 「当前激活的人格」的**跨域公开入口**
# --------------------------------------------------------------------------- #
#
# 实现在 `domains/persona/current.py`。这里再包一层是为了遵守 ADR-0001 的
# 跨域闸门（`tests/unit/test_domain_boundaries.py`）：别的域只允许 import
# 邻居域的**公开 `service` 模块**。`agenda` / `conversation` / `dream` 需要
# "这一轮用哪套人格"，走这三个函数，而不是 import `persona.current`
# （那是实现模块，扩白名单也不行——白名单只接受 `*.service`）。
#
# 这三个函数**不做任何解析逻辑**：全部转调 `current.py`，避免出现第二份判据。

async def active_persona_id(session: object | None = None) -> str:
    """当前激活的人格 id（唯一真相来源：已发布配置的 `persona_id`）。

    ``session`` 传了就复用它的会话级缓存（同一轮只查一次）**并且用它读库**
    （同一事务里刚写的修订也能看到）；不传则自己开一个短会话读。
    """
    from ling.domains.persona.current import effective_persona_id

    return await effective_persona_id(session)  # type: ignore[arg-type]


async def active_persona_view(session: object | None = None) -> PersonaView | None:
    """当前激活的人格；**库里没有这一行时返回 ``None``**（新装 = 暂无人设）。

    只读。与 `PersonaService.get_or_none()` 的区别：那个用服务实例上记录的
    id，这个必定现读配置——跨域调用方（日程/梦境/会话）手上没有这个实例。
    """
    from ling.domains.persona.current import effective_persona_id

    persona_id = await effective_persona_id(session)  # type: ignore[arg-type]
    if not persona_id:
        return None
    from ling.infra.db import session_scope

    if session is not None:
        row = await session.get(Persona, persona_id)  # type: ignore[attr-defined]
        return None if row is None else PersonaService._to_view(row)
    async with session_scope() as own:
        row = await own.get(Persona, persona_id)
        return None if row is None else PersonaService._to_view(row)


class PersonaService:
    """人格（persona）的**唯一写入方**，多套并存、同一时间只有一套生效。

    ## 「当前激活的人格」从哪来

    ``persona_id`` 传了就用它（显式指定，例如人设页要看某一张卡）；
    不传才去解析**当前激活**的那一套（见
    :func:`ling.domains.persona.current.resolve_active_persona_id`）。

    这**不是**"隐式默认"：以前传一个不存在的 id 会静默建行，于是"我切到了
    B，读到的却是 A"永远查不出来。现在不存在的 id 一律抛
    :class:`PersonaNotFoundError`（`get` / `update` / `delete` 都不会建行）。
    """

    def __init__(self, session: AsyncSession, persona_id: str | None = None, /, **kwargs: object) -> None:
        """``persona_id`` 可以是关键字（新写法）也可以是第二个位置参数（旧写法）。

        旧写法 ``PersonaService(session, "persona_x")`` 仍在仓库与测试里广泛使用，
        改成只能关键字会牵动十几个与本特性无关的调用点，而两种写法的语义完全
        一样。关键字名也从 ``default_persona_id`` 改成 ``persona_id``：它已经
        不是"默认值"，而是"这一份服务对象是谁"。
        """
        explicit = persona_id if persona_id is not None else kwargs.pop("persona_id", None)
        default_param = kwargs.pop("default_persona_id", None)
        if kwargs:
            raise TypeError(f"PersonaService 不认识的参数：{'、'.join(sorted(kwargs))}")
        self._session = session
        # 显式指定优先；``default_persona_id`` 只为兼容旧关键字名。
        self._persona_id: str | None = explicit or default_param  # type: ignore[assignment]

    async def _resolved_id(self) -> str:
        persona_id = self._persona_id
        if persona_id:
            return str(persona_id)
        from ling.domains.persona.current import resolve_active_persona_id

        return await resolve_active_persona_id(self._session)

    async def ensure_default(self) -> Persona | None:
        """**新装即零人格**：没有就返回 ``None``，绝不自己造一个。

        改造前它会建一行 ``persona_ling``；用户 2026-11-11 的要求是人格页
        一开始显示"暂无人设"，于是那一行成了"新装用户永远有一条看不见的人格"。
        现在它只做一件事：**读取**当前激活的人格（没有就是 ``None``）。
        保留这个函数名是为了 ``app.py`` 的启动引导与测试不用改调用形状。
        """
        return await self._row_or_none(await self._resolved_id())

    async def _row_or_none(self, persona_id: str) -> Persona | None:
        if not persona_id:
            return None
        return await self._session.get(Persona, persona_id)

    async def _row(self, persona_id: str) -> Persona:
        persona = await self._row_or_none(persona_id)
        if persona is None:
            # 不建行。建行就是"隐式默认"——那会让"切到了 B 却读到 A"这种错
            # 伪装成"人格重置了"，因为新行是空的。
            raise PersonaNotFoundError(persona_id)
        return persona

    async def get(self, persona_id: str | None = None) -> PersonaView:
        """读一个人格。``persona_id`` 为空时读**当前激活**的那一套。

        **不存在的 id 报 :class:`PersonaNotFoundError`**，不再静默建行：
        后者会把"切错 id"伪装成"人格被重置了"（新行是空的），而这两件事的
        修法完全不同。
        """
        pid = persona_id or await self._resolved_id()
        return self._to_view(await self._row(pid))

    async def get_or_none(self, persona_id: str | None = None) -> PersonaView | None:
        """同 :meth:`get`，但"没有"是一个正常结果（``None``）。

        给"根本没配人格"的新装用：调用方要把它渲染成"暂无人设"，而不是 404。
        只有**读**允许走这条路——写永远要求行存在。
        """
        pid = persona_id or await self._resolved_id()
        row = await self._row_or_none(pid)
        return None if row is None else self._to_view(row)

    async def create(
        self,
        *,
        display_name: str = "",
        name: str | None = None,
        system_prompt: str = "",
        traits: dict | None = None,
        boundaries: list[str] | None = None,
        scenario_prompts: dict | None = None,
        persona_id: str | None = None,
        replace: bool = False,
    ) -> PersonaView:
        """新建一套人格。**id 自动生成**（见 :func:`new_persona_id`）。

        ## 不自动"设为激活"

        新建完只是多了**一张卡**，不会自己生效。理由：人设页「创建人格」
        之后用户的第一件事通常是**写内容**；如果创建即生效，那些内容会在
        他点保存之前的每一轮对话里都影响模型（这时候它还是空的，等于把正在
        用的人格换成了"没有人设"）。想生效要显式 ``activate``——
        与 AstrBot 的"设为默认"同一个动作。

        ``display_name`` 允许为空：空名字的人格在列表里显示成"未命名人格"，
        它**不算**"有人设"（判据见 :func:`persona_is_empty`）。
        """
        # 留空时不要写 "ling"：那会让"未命名"和"叫 ling"在库里长成一样
        # （旧版建行就是这么做的），而列表页要靠它区分"没起名"。
        label = (display_name or "").strip()
        if persona_id and await self._session.get(Persona, persona_id) is not None:
            # 显式给了 id 又已存在：两种都会错。默认报冲突（列表页「复制/新建」
            # 撞名时该让用户看到原因，而不是静默覆盖）；覆盖需要调用方**明确**
            # 用 `replace=True`，只在"我知道这一行该被这次内容顶掉"时用
            # （例如旧单例时代的建行夹具）。自动生成 id 那条路不需要它——
            # 随机后缀保证不撞，撞了是随机数出了问题，那时重试一次即可。
            if not replace:
                raise PersonaConflictError(f"人格 id 已存在：{persona_id}")
            row = await self._session.get(Persona, persona_id)
            assert row is not None
            row.display_name = label
            row.system_prompt = system_prompt or ""
            row.traits = dict(traits or {})
            row.boundaries = list(boundaries or [])
            row.scenario_prompts = clean_overrides(scenario_prompts or {})
            row.revision = int(row.revision or 0) + 1
            row.updated_at = utcnow()
            await self._session.flush()
            return self._to_view(row)
        pid = persona_id or new_persona_id(label, taken=await self._existing_ids())
        row = Persona(
            id=pid,
            revision=1,
            name=(name or "").strip() or pid,
            display_name=label,
            # 三列为空 = 刚开始写的人设。**不要**在这里填任何默认文案：
            # 一旦写进来，它就和用户自己写的内容长得一模一样，事后分不开
            # （这是旧版 `DEFAULT_*` 复制进库踩过的坑，见模块头）。
            system_prompt=system_prompt or "",
            traits=dict(traits or {}),
            boundaries=list(boundaries or []),
            scenario_prompts=clean_overrides(scenario_prompts or {}),
            published=True,
        )
        self._session.add(row)
        await self._session.flush()
        logger.info("新建人格 %s（%s）", pid, label or "未命名")
        return self._to_view(row)

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
        """改一套已存在的人格。``persona_id`` 为空时改**当前激活**的那一套。

        **行不存在就报错，不建行。** 旧实现在这里 ``ensure_default()``，于是
        "在设置页改人设"和"系统偷偷新建了一个人格"是同一个分支——改了不生效
        时，看日志只能看到一行空人格。
        """
        pid = persona_id or await self._resolved_id()
        persona = await self._row(pid)
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
        """全部人格，按创建时间正序（列表页的顺序就是"建出来的顺序"）。"""
        rows = (
            await self._session.execute(select(Persona).order_by(Persona.created_at, Persona.id))
        ).scalars().all()
        return [self._to_view(r) for r in rows]

    # ---------------------------------------------------------- 删除
    #
    # ``windows`` / ``actor_events`` / ``actor_states`` 的 ``persona_id`` 是
    # **纯字符串列**（`infra/models.py`：没有 ForeignKey、没有级联）。删掉
    # personas 里的一行**不会**被数据库拦住，也不会连带删掉窗口与事件——
    # 这正是本仓库"权威事件不改写"的既有约定，所以不设计"删除时清引用"。
    #
    # 代价是：删完之后那些行会指向一个不存在的人格。对"删除一张从来不用的
    # 卡"这是无害的（它本来也没产生过轮次），但对"删除正在用的人格"就是
    # 数据悬空。所以判据不是"有没有引用"，而是**这条人格的引用是不是活的**：
    #
    # 1. 它是当前激活的人格 → 拒绝（用户必须先切走，见 `delete`）；
    # 2. 有非空内容的会话窗口挂在它名下 → 拒绝，并给出数量。空窗口
    #    （`message_count == 0` / 没有消息）不算"用过"——人设页点一次"新建"
    #    或前端预建窗口都会留下这种行，拿它拦删除只会逼用户去手工删库；
    # 3. 剩下的（从未实际使用、或只有历史审计事件）→ 允许删除。
    #    历史事件里的 `persona_id` 保留原值：事件是"当时发生过什么"的记录，
    #    回填成新人格 id 等于篡改历史，比悬空引用更糟。

    async def delete(self, persona_id: str) -> PersonaView:
        """删一套人格。不删它名下的窗口与事件（见上面那段说明）。

        三种拒绝，都给**可操作**的原因，而不是笼统的 409：

        - :class:`PersonaActiveError`：这是当前激活的人格 → 先 ``activate`` 别的；
        - :class:`PersonaInUseError`：还有非空会话挂在它名下 → 先归档/清空那些会话；
        - :class:`PersonaNotFoundError`：id 不存在（删除不是幂等的"删了也算过"：
          静默成功会让"我删的是另一张卡吗"永远没法回答）。
        """
        persona = await self._row(persona_id)
        from ling.domains.persona.current import effective_persona_id

        active = await effective_persona_id(self._session)
        if persona_id == active:
            raise PersonaActiveError(persona_id)
        in_use = await self._non_empty_window_count(persona_id)
        if in_use:
            raise PersonaInUseError(persona_id, in_use)
        view = self._to_view(persona)
        await self._session.delete(persona)
        await self._session.flush()
        # 名字缓存按 persona_id 存"她叫什么"。删掉后必须清掉，否则同一进程里
        # 一个已被删掉的人格名还会被用来剥前缀（`display_name_for` 命中缓存时
        # 根本不会再查库，它也就永远发现不了这一行没了）。
        from ling.capabilities.naming import invalidate_names

        invalidate_names()
        logger.info("删除人格 %s（%s）", persona_id, view.display_name or "未命名")
        return view

    async def _existing_ids(self) -> set[str]:
        return set((await self._session.execute(select(Persona.id))).scalars().all())

    async def _non_empty_window_count(self, persona_id: str) -> int:
        from sqlalchemy import func

        from ling.infra.models import Message

        count = (
            await self._session.execute(
                select(func.count())
                .select_from(Window)
                .where(
                    Window.persona_id == persona_id,
                    Window.message_count > 0,
                )
            )
        ).scalar_one()
        if count:
            return int(count)
        # `message_count` 是派生列：历史数据里可能没维护过。再按消息表实查
        # 一次——宁可多一次查询，也不能因为一个陈旧的计数就把"正在用"判成
        # "没用过"，然后删掉一个有人记得的人格。
        return int(
            (
                await self._session.execute(
                    select(func.count(func.distinct(Message.window_id)))
                    .select_from(Message)
                    .join(Window, Window.id == Message.window_id)
                    .where(Window.persona_id == persona_id)
                )
            ).scalar_one()
        )

    @staticmethod
    def _to_view(p: Persona) -> PersonaView:
        """**原样**视图：库里是什么就返回什么，空就是空。

        ## 这里**不能**回落成默认值（两个方向都是坑）

        1. 回落 → 前端拿到非空串 → 显示在输入框里 → 用户点保存 → 默认值被写
           进库、从此冻结。这一条是上一版 `effective_prompt` 真正踩到的坑：
           "空 = 跟随默认"这个设计根本存不下来。
        2. 把空串改写成某个占位符（例如"（未设置）"）→ 保存时占位符也会被当成
           用户正文提交上去。

        所以唯一的正确做法是原样返回。"人设为空时该不该注入"是**注入侧**的
        问题，由 `kernel/context.py` 用 `persona_is_empty()` 判断，不在读数时
        提前替用户做决定。想确认"生效值"，去看那一轮实际发出去的提示词
       （`persona_empty` / 快照），而不是看这里。
        """
        return PersonaView(
            persona_id=p.id,
            revision=p.revision,
            name=p.name,
            # 历史数据/手工改库可能写出 NULL：只对"形状"兜底（None → 空），
            # 不改变"空"这个事实本身。
            display_name=p.display_name or "",
            system_prompt=p.system_prompt or "",
            traits=dict(p.traits or {}),
            boundaries=[item for item in (p.boundaries or []) if isinstance(item, str)],
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
    "PersonaActiveError",
    "active_persona_id",
    "active_persona_view",
    "PersonaConflictError",
    "PersonaInUseError",
    "PersonaNotFoundError",
    "PersonaService",
    "PersonaView",
    "empty_persona_view",
    "format_traits",
    "new_persona_id",
    "persona_is_empty",
    "traits_report",
    "view_is_empty",
]
