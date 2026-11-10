"""「当前激活的人格是哪一套」——**全系统唯一的判据**。

把它放在 `domains/persona/` 而不是 `settings/`：**读到哪一套人格**是人格域的
问题，配置只是答案的存放处。`settings/` 里再实现一遍"人格是哪个"，迟早和
这里分叉成"设置页说 A、实际用 B"——本仓库已经有几处这种错（见各模块注释）。
`settings/refresh.py` / `worker.py` / `api/deps.py` 等取值点一律调本模块。

## 真相来源只有一处：``EffectiveConfig.persona_id``

人格可以有多套（`personas` 表多行），但同一时间只有一套生效。这个"哪一套"
不是一个进程内变量、也不是某个服务实例的属性，而是**配置修订里那个字段**：

- 它已经存在（`ports/config.py::EffectiveConfig.persona_id`），并且
  `settings/service.py::payload_from_effective` / `effective_from_payload`
  已经把它读写进修订 payload——也就是说**发布、回滚对它是生效的**；
- 它是**跨进程**的：API 副本与 worker 读同一份已发布修订，不需要额外同步；
- 回滚理应恢复旧人格：把"当前人格"放在修订里，回滚到上一版修订时
  人格也一起回去。放在别处（单行表、app.state）就做不到这一点。

切换人格 = 改这个字段并发布一条新修订。见 `settings/service.py::ConfigService.set_active_persona`。

## 没有已发布修订时（新装）

`ConfigService.effective()` 会返回 **bootstrap 配置**（`revision=0`），其中
`persona_id` 来自 `LING_PERSONA_ID`，默认 ``"persona_ling"``（`config.py:91`）。

于是新装的行为是：**没有人格行** → 解析出来一个"指向不存在行"的 id →
读人格的一方拿到 ``None`` → 什么都不注入（"暂无人设"）。这与老库里有
``persona_ling`` 这一行的情况**完全一致**：有行就是有人格，没有就是没有，
不存在"新装特殊处理"。所以这里**不**做任何兜底建行或兜底选行。

反过来，如果老库本来就没人格行、也没配 `LING_PERSONA_ID`，解析结果同样是
那个不存在的 id——同样什么都不注入。两种库的行为一致。

## 缓存

每轮都查 `config_revisions` 是没必要的一条 SQL：一轮里可能有十几次
"今天用哪套人格"。这里按**数据库会话**做缓存（`session.info`），一轮
（同一个会话）内只查一次；切换人格的那条路径会主动失效（`invalidate`），
其它进程靠修订号变化自然失效（缓存里存着解析用的修订号）。

为什么不缓存到进程全局：切换人格之后，同一个进程里**下一个请求**就必须
生效（人设页"设为当前"之后立刻能看到效果）。进程级缓存要么加 TTL（用户
等一个 TTL 才生效），要么全量失效钩子（会漏掉 worker 这条路径）。
按会话缓存两头都占：快（一轮一次查询）、准（下一个请求必然重新解析）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注
    from sqlalchemy.ext.asyncio import AsyncSession

    from ling.ports.config import EffectiveConfig
    from ling.settings.service import ConfigService

logger = logging.getLogger("ling.domains.persona.current")

#: 会话级缓存的键。值是 ``(revision, persona_id)``。
_CACHE_KEY = "ling_active_persona_id"


def _cached(session: AsyncSession) -> tuple[int, str] | None:
    info = getattr(session, "info", None)
    if info is None:
        return None
    return info.get(_CACHE_KEY)  # type: ignore[no-any-return]


def invalidate(session: AsyncSession) -> None:
    """本会话的人格解析缓存失效。切换/删除人格后必须调一次。

    ``session.info`` 是 SQLAlchemy 会话自带的一个 dict，不需要额外清理；
    会话关闭时随之丢弃。这里只提供"同一个会话里刚改完立刻再读"的路径。
    """
    info = getattr(session, "info", None)
    if info is not None:
        info.pop(_CACHE_KEY, None)


async def _config_reader(session: AsyncSession) -> ConfigService:
    from ling.config import get_settings
    from ling.infra.security import SecretBox
    from ling.settings.service import ConfigService

    settings = get_settings()
    return ConfigService(
        session,
        SecretBox(settings),
        test_mode_default=settings.test_mode,
        persona_id=settings.persona_id,
        log_denials_default=settings.log_access_denials,
    )


async def effective_config(session: AsyncSession | None = None) -> EffectiveConfig:
    """读当前生效配置（``persona_id`` 的**唯一**来源）。

    ## 为什么传了会话就**必须**用它的会话读

    这里曾经写的是"只有 ``in_session=True`` 才复用调用方会话，否则自开一个
    短会话"。那是错的：``session_scope()`` 是**另一个连接**，看不到调用方
    事务里**还没提交**的东西。调用方在同一个事务里先改配置、再问"现在用哪套
    人格"时，自开的会话会读到旧值——而且不报错，只表现为"刚切的没生效"。

    反过来在**会话里**读的代价也清楚：它挂在调用方事务上，可能多一层
    savepoint。所以只有调用方明确说"我没有会话"（``None``）时才自开。

    **测试替身注意**：这里是唯一构造 ``ConfigService`` 读 ``persona_id`` 的
    地方。旧代码里各模块自己 ``get_settings().persona_id``，于是"测试里换个人格"
    得改环境变量——环境变量是全局的，并行跑测试会互相污染。
    """
    from ling.infra.db import session_scope

    if session is not None:
        return await (await _config_reader(session)).effective()
    async with session_scope() as own_session:
        return await (await _config_reader(own_session)).effective()


async def effective_persona_id(session: AsyncSession | None = None) -> str:
    """当前激活的人格 id（**不保证这一行存在**）。

    返回空串表示"确实没有配置任何人格"（例如修订里显式写了空串——
    等价于"没人格"）。调用方不能假设它对应库里一行：新装就是
    "指向 ``persona_ling``、但没有这一行"。判"这一行在不在"用
    ``PersonaService.get_or_none()``，不要在这里替调用方决定。
    """
    if session is not None:
        cached = _cached(session)
        if cached is not None:
            return cached[1]
    config = await effective_config(session=session)
    persona_id = str(config.persona_id or "")
    if session is not None:
        try:
            session.info[_CACHE_KEY] = (int(config.revision), persona_id)
        except Exception:  # noqa: BLE001 - 缓存写不进去不影响结果
            logger.debug("人格缓存写入失败（忽略）", exc_info=True)
    return persona_id


async def resolve_active_persona_id(session: AsyncSession | None = None) -> str:
    """"这一轮该用哪套人格"的**唯一解析入口**。

    与 :func:`effective_persona_id` 是同一个答案，只是名字更贴近调用点。
    刻意**不**做以下三件事，每一件都是以前踩过的坑：

    1. **不**在没人格行时回落到"随便挑一行"——那等于替用户切换了人格，
       而他看到的界面还是"暂无人设"；
    2. **不**在解析失败时回落到某个写死的 id——写死的 id 会让
       "配置读不出来"和"用默认人格"看起来一模一样；
    3. **不**缓存到进程级（原因见模块头）。
    """
    return await effective_persona_id(session)


__all__ = [
    "effective_config",
    "effective_persona_id",
    "invalidate",
    "resolve_active_persona_id",
]
