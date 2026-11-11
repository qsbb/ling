"""连接器**实例目录**：某个 scope 下有哪些实例、叫什么、开没开、删没删。

## 这个模块只管"身份"，不管"内容"

实例有两个概念同名不同物，混了整张表就建在错的语义上（见
``migrations/versions/0067_connector_instances.py`` 的模块 docstring）：

- ``(scope, instance_id)`` —— 实例的**稳定身份**，建后不可变。本模块管这个。
- ``LING_ONEBOT_INSTANCE_ID`` —— **actor 命名空间**，决定"两个机器人下同一个
  QQ 号算不算同一个人"。它是实例的**配置内容**，由
  ``ling.settings.connector_service`` 保存，改它**不会**变成另一个实例。

所以本模块里**没有**任何一处读或写 actor 命名空间字段。它的名字也不是从
那个字段推导的：``instance_id`` 在创建时由调用方给（或按显示名生成），
之后只改 ``display_name``。

## 软删为什么不是真删

修订链与审计要留着可查（``ConnectorInstance`` 的 docstring 已经这么写了）。
真删会让"这个实例过去存过什么配置"变成无主行——修订单上没有 scope 之外的
线索能找回它属于谁。

## 软删之后 display_name 仍占着唯一键

``uq_connector_instance_display_name`` 是 ``UNIQUE(scope, display_name)``，
**不带** ``WHERE deleted_at IS NULL`` 这种部分索引条件。于是"删掉「小号」再
建一个「小号」"会撞唯一键。这里**不靠数据库报错**来表达它，而是先查一次
并给出明确原因——但最终仍以数据库为准（见 ``_flush_unique_violation``）：
并发下两次插入都能通过预检查，只有唯一键能拦住。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import DEFAULT_INSTANCE_ID, ConnectorInstance, utcnow

#: 与迁移里 ``sa.String(16)`` 一致。实例 id 落在主键上，超长会被数据库截断/报错。
INSTANCE_ID_MAX_LENGTH = 16

#: 与迁移里 ``sa.String(64)`` 一致。
DISPLAY_NAME_MAX_LENGTH = 64

#: 实例 id 的允许形状。
#:
#: 与迁移里"从 actor 命名空间推导实例 id"用的正则 ``^[a-z0-9_-]{1,16}$``
#: **刻意一致**：推导出来的存量实例名必须也是新建实例时能接受的名字，否则
#: 会出现"库里有的名字，API 不让你再建同一个"这种自相矛盾。
_INSTANCE_ID_RE = re.compile(r"^[a-z0-9_-]{1,16}$")


class ConnectorInstanceError(Exception):
    """实例目录的对外错误：稳定 code + 中文 message + HTTP 状态码。

    形状照 ``ConnectorServiceError``：路由层把它翻译成 ``LingError``，
    ``code`` 进 ``detail``，``message`` 进用户可见文案。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 422,
        name: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        #: 与其它路由的 ``field`` 对齐，前端据此高亮输入框。
        self.name = name


# ---------------------------------------------------------------- 纯逻辑


def normalize_display_name(raw: str) -> str:
    """校验并规范化显示名。

    **去首尾空白后判空**：全空格的名字在列表里看起来像一行空白，用户没法点。
    中间的空格保留（"主号 二号"是合法的人起的名字）。
    """
    if not isinstance(raw, str):
        raise ConnectorInstanceError("invalid_field", "显示名必须是字符串", name="display_name")
    name = raw.strip()
    if not name:
        raise ConnectorInstanceError("invalid_field", "显示名不能为空", name="display_name")
    if len(name) > DISPLAY_NAME_MAX_LENGTH:
        raise ConnectorInstanceError(
            "invalid_field",
            f"显示名最长 {DISPLAY_NAME_MAX_LENGTH} 个字符",
            name="display_name",
        )
    return name


def normalize_instance_id(raw: str) -> str:
    if not isinstance(raw, str):
        raise ConnectorInstanceError("invalid_field", "实例 id 必须是字符串", name="instance_id")
    value = raw.strip().lower()
    if not _INSTANCE_ID_RE.match(value):
        raise ConnectorInstanceError(
            "invalid_field",
            f"实例 id 只能用小写字母、数字、下划线或短横线，长度 1-{INSTANCE_ID_MAX_LENGTH}",
            name="instance_id",
        )
    return value


def suggest_instance_id(display_name: str) -> str:
    """从显示名派生一个候选 id（可能不合法/为空，调用方要再校验）。

    只做"把明显不能当 id 的字符换掉"，**不保证唯一**——唯一性由主键保证，
    调用方在冲突时换一个。中文名会被清成空串，那时由调用方退回随机后缀。
    """
    slug = re.sub(r"[^a-z0-9_-]+", "-", display_name.strip().lower()).strip("-")
    return slug[:INSTANCE_ID_MAX_LENGTH]


# ---------------------------------------------------------------- 视图


def instance_view(row: ConnectorInstance) -> dict[str, object]:
    """列表/详情的同一份形状。**不暴露 actor 命名空间**（那是内容，不在这里读）。"""
    return {
        "scope": row.scope,
        "instance_id": row.instance_id,
        "display_name": row.display_name,
        "enabled": bool(row.enabled),
        # 默认实例删不得，前端据此把删除按钮置灰，而不是点了才 409。
        "deletable": row.instance_id != DEFAULT_INSTANCE_ID,
        "created_at": _iso(row.created_at),
        "created_by": row.created_by,
        "deleted_at": _iso(row.deleted_at),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


# ---------------------------------------------------------------- 服务


@dataclass
class _Candidate:
    instance_id: str
    display_name: str


class ConnectorInstanceService:
    """实例目录的读写。调用方负责 commit（与 ``ConnectorConfigService`` 一致）。"""

    def __init__(self, session: AsyncSession, *, clock: object = None) -> None:
        self._session = session
        self._clock = clock or utcnow

    # ------------------------------------------------------------ 读

    async def list_instances(
        self, *, scope: str, include_deleted: bool = False
    ) -> list[ConnectorInstance]:
        """列出某 scope 下的实例。

        默认**排除软删**：设置页的实例选择器不该出现已经删掉的名字。
        ``include_deleted=True`` 给"历史配置还能查"那条路径用——它要先看到
        被删实例的存在。
        """
        stmt = select(ConnectorInstance).where(ConnectorInstance.scope == scope)
        if not include_deleted:
            stmt = stmt.where(ConnectorInstance.deleted_at.is_(None))
        # 默认实例排最前（它是存量数据的属主），其余按创建时间。
        stmt = stmt.order_by(
            (ConnectorInstance.instance_id != DEFAULT_INSTANCE_ID),
            ConnectorInstance.created_at,
            ConnectorInstance.instance_id,
        )
        return list((await self._session.execute(stmt)).scalars())

    async def get(self, *, scope: str, instance_id: str) -> ConnectorInstance | None:
        """按身份取一行，**软删的也会返回**。

        调用方要自己决定"软删了算不算存在"：改名/启停必须拒绝，而"查历史
        配置"要能读到。把它做成两种情况各一个方法会让两处判断漂移。
        """
        return await self._session.get(ConnectorInstance, (scope, instance_id))

    async def require_live(self, *, scope: str, instance_id: str) -> ConnectorInstance:
        """取一个**未软删**的实例，否则抛错（404 / 409）。

        配置保存路径用它来保证"保存到已删实例"不会静默成功。
        """
        row = await self.get(scope=scope, instance_id=instance_id)
        if row is None:
            raise ConnectorInstanceError(
                "instance_not_found",
                f"没有实例 {instance_id}",
                status_code=404,
                name="instance_id",
            )
        if row.deleted_at is not None:
            raise ConnectorInstanceError(
                "instance_deleted",
                "这个实例已被删除，不能再用它保存配置",
                status_code=409,
                name="instance_id",
            )
        return row

    async def ensure_default(self, *, scope: str) -> ConnectorInstance:
        """保证 ``(scope, 'default')`` 这一行存在并返回它。

        ## 为什么需要这一条

        升级前"保存配置"**从不要求实例行**：``_write_candidate`` 在第一次
        保存时自己建 HEAD 指针。迁移 0067 只给"有过 HEAD/修订/ACK"的 scope
        回填了实例行，所以**全新部署**（从来没保存过配置）里
        ``connector_instances`` 是空的——这时若硬性要求实例存在，"第一次
        保存配置"会变成 404，等于把升级前能用的操作弄坏了。

        只补建**默认实例**，且 ``display_name`` 直接取 ``instance_id``：与
        迁移回填出来的形状完全一致（迁移里是
        ``SELECT s.scope, s.instance_id, s.instance_id, true, now()``）。
        于是"升级部署"与"全新部署"的默认实例行长得一模一样，不会出现同一
        语义两个显示名。

        ## 与 persona 那条教训的区别

        ``PersonaService.update`` 的注释是"行不存在就报错，不建行"，因为那里
        "改人设"和"系统偷偷新建人格"走了同一分支。这里不同：默认实例**不是
        用户创建的东西**，它是"存量配置属主"这个结构位置；补建它不会产生
        第二个实体——主键 ``(scope, 'default')`` 是确定的。
        """
        row = await self.get(scope=scope, instance_id=DEFAULT_INSTANCE_ID)
        if row is not None:
            return row
        created = ConnectorInstance(
            scope=scope,
            instance_id=DEFAULT_INSTANCE_ID,
            display_name=DEFAULT_INSTANCE_ID,
            enabled=True,
            created_at=self._clock(),  # type: ignore[operator]
            created_by=None,
        )
        self._session.add(created)
        try:
            await self._session.flush()
        except IntegrityError:
            # 并发：两个请求同时补建，只有一个能过主键。撞了就重读。
            self._session.expunge(created)
            existing = await self.get(scope=scope, instance_id=DEFAULT_INSTANCE_ID)
            if existing is not None:
                return existing
            raise
        return created

    # ------------------------------------------------------------ 写

    async def create(
        self,
        *,
        scope: str,
        display_name: str,
        instance_id: str | None = None,
        enabled: bool = True,
        created_by: str | None = None,
    ) -> ConnectorInstance:
        """建一个实例。

        ``instance_id`` 不给就按显示名派生；派生不出合法 id（中文名）或撞了
        主键时，退回 ``inst-<随机>``。**id 一旦建成就不变**，改名只改
        ``display_name``。
        """
        name = normalize_display_name(display_name)
        await self._ensure_name_free(scope=scope, display_name=name)

        candidates = self._id_candidates(instance_id, name)
        last_conflict: ConnectorInstanceError | None = None
        for candidate in candidates:
            row = await self.get(scope=scope, instance_id=candidate)
            if row is not None:
                # 身份已被占用：显式给了 id 就明确报错，别偷偷换一个。
                last_conflict = ConnectorInstanceError(
                    "instance_exists",
                    f"实例 id {candidate} 已被占用",
                    status_code=409,
                    name="instance_id",
                )
                if instance_id is not None:
                    raise last_conflict
                continue
            created = ConnectorInstance(
                scope=scope,
                instance_id=candidate,
                display_name=name,
                enabled=bool(enabled),
                created_at=self._clock(),  # type: ignore[operator]
                created_by=created_by,
            )
            self._session.add(created)
            try:
                await self._session.flush()
            except IntegrityError as exc:
                # 并发：两次创建都过了上面的预检查，只有一个能过唯一键。
                #
                # **只丢弃这一行，不回滚整个会话**：`session.rollback()` 会把
                # 调用方在同一事务里已经做的其它改动一起丢掉，而调用方拿到的
                # 是一个 409，会以为"只是名字撞了，别的都没事"。
                raise self._unique_violation(exc, scope=scope, display_name=name) from exc
            return created
        raise last_conflict or ConnectorInstanceError(
            "instance_exists", "没有可用的实例 id", status_code=409, name="instance_id"
        )

    async def rename(
        self, *, scope: str, instance_id: str, display_name: str
    ) -> ConnectorInstance:
        row = await self._require_editable(scope=scope, instance_id=instance_id)
        name = normalize_display_name(display_name)
        if name == row.display_name:
            return row
        await self._ensure_name_free(
            scope=scope, display_name=name, exclude_instance_id=row.instance_id
        )
        original = row.display_name
        row.display_name = name
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # 同上：不整体回滚；把内存里的对象改回原值，免得后续读到脏数据。
            row.display_name = original
            raise self._unique_violation(exc, scope=scope, display_name=name) from exc
        return row

    async def set_enabled(
        self, *, scope: str, instance_id: str, enabled: bool
    ) -> ConnectorInstance:
        row = await self._require_editable(scope=scope, instance_id=instance_id)
        row.enabled = bool(enabled)
        await self._session.flush()
        return row

    async def soft_delete(self, *, scope: str, instance_id: str) -> ConnectorInstance:
        """软删。**幂等**：已删的再删一次直接返回，不刷新 ``deleted_at``。"""
        if instance_id == DEFAULT_INSTANCE_ID:
            # 存量数据全部折叠在默认实例上（迁移 docstring「实例标识」一节）。
            # 删了它，那些修订链与 HEAD 指针就变成没有属主的行，而读取路径
            # （`connector_service.py` 里 6 处 `(scope, DEFAULT_INSTANCE_ID)`）
            # 会查到一个"已删除"的实例却不报错——静默读到已删实例比报错更糟。
            raise ConnectorInstanceError(
                "default_instance_protected",
                "默认实例是存量配置的属主，不能删除",
                status_code=409,
                name="instance_id",
            )
        row = await self.get(scope=scope, instance_id=instance_id)
        if row is None:
            raise ConnectorInstanceError(
                "instance_not_found", f"没有实例 {instance_id}", status_code=404
            )
        if row.deleted_at is None:
            row.deleted_at = self._clock()  # type: ignore[operator]
            # 软删即停用：一个"已删除但 enabled=true"的行会让"启用状态"这条
            # 查询出现自相矛盾的结果（列表里看不到，但计数里还在）。
            row.enabled = False
            await self._session.flush()
        return row

    # ------------------------------------------------------------ 内部

    async def _require_editable(
        self, *, scope: str, instance_id: str
    ) -> ConnectorInstance:
        row = await self.get(scope=scope, instance_id=instance_id)
        if row is None:
            raise ConnectorInstanceError(
                "instance_not_found", f"没有实例 {instance_id}", status_code=404
            )
        if row.deleted_at is not None:
            raise ConnectorInstanceError(
                "instance_deleted",
                "这个实例已被删除，不能改名或启停",
                status_code=409,
                name="instance_id",
            )
        return row

    async def _ensure_name_free(
        self,
        *,
        scope: str,
        display_name: str,
        exclude_instance_id: str | None = None,
    ) -> None:
        """同 scope 下显示名唯一（**含已软删的**，与唯一键一致）。"""
        stmt = select(ConnectorInstance.instance_id).where(
            ConnectorInstance.scope == scope,
            ConnectorInstance.display_name == display_name,
        )
        if exclude_instance_id is not None:
            stmt = stmt.where(ConnectorInstance.instance_id != exclude_instance_id)
        taken = (await self._session.execute(stmt)).first()
        if taken is not None:
            deleted = await self.get(scope=scope, instance_id=str(taken[0]))
            hint = (
                "（那个名字属于一个已删除的实例，历史配置还要留着查，所以名字没释放）"
                if deleted is not None and deleted.deleted_at is not None
                else ""
            )
            raise ConnectorInstanceError(
                "duplicate_display_name",
                f"这个接入器下已经有叫「{display_name}」的实例了{hint}",
                status_code=409,
                name="display_name",
            )

    def _unique_violation(
        self, exc: IntegrityError, *, scope: str, display_name: str
    ) -> ConnectorInstanceError:
        """把数据库唯一键冲突翻译成**明确原因**，而不是漏成 500。

        ``uq_connector_instance_display_name`` 与主键都要区分：前者是"名字被占"，
        后者是"id 被占"。约束名在 ``exc.orig`` 的文本里，不同驱动格式不同，
        所以用子串匹配而不是精确解析——匹配不到时按"名字被占"给出最可能的
        原因，**但仍标明是冲突而不是内部错误**。
        """
        text = str(getattr(exc, "orig", exc))
        if "connector_instances_pkey" in text:
            return ConnectorInstanceError(
                "instance_exists",
                "这个实例 id 已被占用",
                status_code=409,
                name="instance_id",
            )
        return ConnectorInstanceError(
            "duplicate_display_name",
            f"这个接入器下已经有叫「{display_name}」的实例了",
            status_code=409,
            name="display_name",
        )

    def _id_candidates(self, explicit: str | None, display_name: str) -> list[str]:
        if explicit is not None:
            return [normalize_instance_id(explicit)]
        slug = suggest_instance_id(display_name)
        out: list[str] = []
        if slug:
            out.append(normalize_instance_id(slug))
        # 派生不出（中文名）或全被占用时，用随机后缀兜底。
        # 不用 `infra.security.new_id`：它生成 `prefix_` + 24 位 hex，远超
        # 主键的 varchar(16)。这里只需 8 位就够（同 scope 下撞概率可忽略，
        # 真撞了下一轮循环会再换一个）。
        import secrets

        for _ in range(8):
            out.append(f"i-{secrets.token_hex(4)}")
        return out


__all__ = [
    "DISPLAY_NAME_MAX_LENGTH",
    "INSTANCE_ID_MAX_LENGTH",
    "ConnectorInstanceError",
    "ConnectorInstanceService",
    "instance_view",
    "normalize_display_name",
    "normalize_instance_id",
    "suggest_instance_id",
]
