"""接入器配置的**写入与生效编排**：候选修订 → 切换实例 → 提交/回滚。

## 一次 PUT 的完整顺序（顺序本身就是正确性）

```text
① 校验请求（字段名、类型、范围、交叉约束）      ← 422，此时 stop() 调用数必须为 0
② advisory lock（同 scope 串行）+ generation CAS ← 409
③ 合并候选：旧覆盖 + set - clear → 完整有效配置 → 整体再校验一次
④ 有效值没变 → 直接返回当前 generation（unchanged，不建修订、不重连）
⑤ 短事务：写候选修订 + 覆盖行 + head.candidate_revision_id → 提交
⑥ **事务外**：stop(旧) → construct(新) → start(新)   ← 长连接绝不在事务里
⑦ 成功：短事务里 state=active、head.active_revision_id=新、generation+1、写 ACK
   失败：用**旧配置**重建 → state=rolled_back + 稳定 error_code → head 指向旧版
```

几个刻意的选择：

- **先校验再动手**。校验失败时一个连接都没停过——用户填错一个端口号不该
  让她的 QQ 掉线。所以 ``stop()`` 只可能出现在 ⑤ 之后。
- **候选修订与切换分开**。候选落库之后即使进程崩了，重启时它既不会自己
  变成 active（head 还指着旧版），也不会丢掉"曾经想改成什么"的证据。
- **切换在事务外**。长连接 stop/start 可能等上几百毫秒到几秒（端口释放、
  TLS 握手），把它放进事务就是拿着行锁等网络——同一 scope 的其他写入、
  甚至读 head 的请求都会被拖住。
- **失败必须回滚到旧实例，而不是"停在半路"**。旧实例 stop 之后不能无条件
  再 start（见设计 §4），所以回退路径是 **用旧配置重新构造一个新实例**。
- **不虚报**：只有 ``start()`` 没抛错且 head 真的指向新修订，才回 ``active``。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import DEFAULT_INSTANCE_ID
from ling.infra.security import new_id
from ling.settings import connector_fields as cf
from ling.settings import connector_overrides as ov

logger = logging.getLogger("ling.settings.connector_service")

#: 修订状态（与迁移里的 CHECK/注释一致）
STATE_PENDING = "pending"
STATE_ACTIVATING = "activating"
STATE_ACTIVE = "active"
STATE_FAILED = "failed"
STATE_ROLLED_BACK = "rolled_back"
STATE_DEGRADED = "degraded"

#: 操作终态（对外的 ``activation.state``）
OP_ACTIVATING = "activating"
OP_ACTIVE = "active"
OP_ROLLED_BACK = "rolled_back"
OP_DEGRADED = "degraded"
OP_UNCHANGED = "unchanged"

#: 可安全展示的错误码 → 一句人话（**不含值、不含异常原文**）
SAFE_ERRORS: dict[str, str] = {
    "invalid_field": "有一项填写不合法",
    "invalid_config": "这份配置组合起来不能用（例如监听地址与端口不匹配）",
    "generation_conflict": "配置刚被别处改过，请刷新后重试",
    "activation_in_progress": "另一处保存正在生效，请稍后再试",
    "activation_unavailable": "现在没法完成切换（切换器不可用）",
    "bind_failed": "新配置的端口起不来（可能被占用或没有权限）",
    "connector_start_failed": "新配置的连接器启动失败，已恢复上一份配置",
    "rollback_failed": "恢复上一份配置也失败了，需要人工检查",
    "secret_undecryptable": "旧密钥解不开（根密钥可能已更换）",
}

#: 哪些字段属于"监听类"，变更后需要重建 OneBot 的 WS 组件
ONEBOT_LISTEN_KEYS = frozenset(
    {"LING_ONEBOT_WS_PORT", "LING_ONEBOT_WS_TOKEN", "LING_ONEBOT_WS_HOST"}
)
ONEBOT_OUTBOUND_KEYS = frozenset(
    {"LING_ONEBOT_WS_URL", "LING_ONEBOT_API_BASE", "LING_ONEBOT_ACCESS_TOKEN", "LING_ONEBOT_SELF_ID"}
)


class ConnectorServiceError(Exception):
    """对外错误：稳定 code + 中文 message + 可选 field/operation_id。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 422,
        field_name: str | None = None,
        operation_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.field_name = field_name
        self.operation_id = operation_id


class RuntimeSwap(Protocol):
    """切换器协议：把某个 scope 的新配置应用到本副本。

    ``values`` 是解密后的完整有效配置，切换器在进程内保留以支持重建。
    禁止写入日志或序列化到对外状态。
    """

    async def apply(
        self, scope: str, *, values: dict[str, Any], operation_id: str,
        instance_id: str = DEFAULT_INSTANCE_ID,
    ) -> SwapOutcome: ...

    async def stop(self, scope: str, instance_id: str = DEFAULT_INSTANCE_ID) -> None: ...


@dataclass
class SwapOutcome:
    ok: bool
    #: ok | degraded（切过去了但远端没连上）
    health: str = "ok"
    error_code: str | None = None
    error_safe: str | None = None
    #: 只在诊断里用：stop_started_at / listener_ready_at（秒，相对切换开始）
    timings: dict[str, float] = field(default_factory=dict)
    #: 本次切换是否真的停过旧实例（校验失败时必须为 0 次）
    stop_calls: int = 0


@dataclass
class ActivationResult:
    operation_id: str
    scope: str
    generation: int
    state: str
    changed: bool
    revision_id: str | None
    active_revision_id: str | None
    started_at: datetime
    finished_at: datetime
    ack_count: int
    expected_count: int
    error_code: str | None = None
    error_safe: str | None = None
    timings: dict[str, float] = field(default_factory=dict)
    #: 被改字段的"旧>新"审计指纹（HMAC 截断，不含值/预览）
    fingerprints: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------- 校验


@dataclass(frozen=True)
class ChangeRequest:
    """一次 PUT 的意图：``set`` 与 ``clear``（未出现 = 不改）。"""

    set_values: dict[str, Any]
    clear_keys: tuple[str, ...]

    @classmethod
    def build(cls, scope: str, body: dict[str, Any]) -> ChangeRequest:
        """解析并**先做静态校验**（不碰数据库、不碰连接）。

        这一步之后才允许进入锁与切换；任何问题都在这里以 422 抛出，
        从而保证"校验阶段 stop() 调用数为 0"。
        """
        raw_set = body.get("set") or {}
        raw_clear = body.get("clear") or []
        if not isinstance(raw_set, dict):
            raise ConnectorServiceError("invalid_field", "set 必须是对象")
        if isinstance(raw_clear, str) or not isinstance(raw_clear, (list, tuple)):
            raise ConnectorServiceError("invalid_field", "clear 必须是字符串数组")

        clear: list[str] = []
        for key in raw_clear:
            name = str(key)
            spec = _spec_or_422(name)
            if spec.scope != scope:
                raise ConnectorServiceError(
                    "invalid_field", f"{name} 不属于这个接入器", field_name=name
                )
            if name in clear:
                raise ConnectorServiceError("invalid_field", f"clear 里重复了 {name}", field_name=name)
            clear.append(name)

        normalized: dict[str, Any] = {}
        for raw_key, value in raw_set.items():
            key = str(raw_key)
            spec = _spec_or_422(key)
            if spec.scope != scope:
                raise ConnectorServiceError(
                    "invalid_field", f"{key} 不属于这个接入器", field_name=key
                )
            if key in clear:
                raise ConnectorServiceError(
                    "invalid_field", f"{key} 不能同时出现在 set 与 clear 里", field_name=key
                )
            if spec.kind == cf.KIND_SECRET:
                # 空密码串 = 不改。前端"密码框总是空的"不能变成一次删除。
                if value is None or (isinstance(value, str) and value == ""):
                    continue
                if not isinstance(value, str):
                    raise ConnectorServiceError(
                        "invalid_field", "密钥必须是字符串", field_name=key
                    )
            try:
                normalized[key] = cf.normalize_value(spec, value)
            except cf.FieldError as exc:
                raise ConnectorServiceError(
                    "invalid_field", exc.reason, field_name=key
                ) from exc
        return cls(set_values=normalized, clear_keys=tuple(clear))

    def touches(self, keys: frozenset[str]) -> bool:
        return bool((set(self.set_values) | set(self.clear_keys)) & keys)


def _spec_or_422(key: str) -> cf.FieldSpec:
    try:
        return cf.spec_for(key)
    except cf.FieldError as exc:
        # 关键安全点：**任意 env 名被拒**（LING_ROOT_KEY / DATABASE_URL …）
        raise ConnectorServiceError(
            "invalid_field", exc.reason, field_name=key
        ) from exc


def validate_full(scope: str, values: dict[str, Any]) -> None:
    """校验**合并后的完整有效配置**（不是只校验这次改的那几项）。

    为什么整体再校验一次：单个字段各自合法、合起来不成立的组合是存在的
    （配了 WS 端口却没令牌 → 服务端 fail-closed 起不来；白名单模式没名单
    → 收紧成"谁都不能说话"，而用户以为自己只是清了名单）。
    只校验增量的话，这些要到切换时才发现，代价是白停一次连接。
    """
    for spec in cf.fields_for(scope):
        raw = values.get(spec.key)
        if raw is None or raw == "":
            continue
        try:
            if spec.kind == cf.KIND_LIST:
                cf.normalize_list(raw)
            elif spec.kind == cf.KIND_SWITCH:
                cf.normalize_switch(raw)
            else:
                spec.check(str(raw))
        except cf.FieldError as exc:
            raise ConnectorServiceError("invalid_field", exc.reason, field_name=spec.key) from exc

    if scope == cf.SCOPE_ONEBOT:
        port = str(values.get("LING_ONEBOT_WS_PORT") or "").strip()
        token = str(values.get("LING_ONEBOT_WS_TOKEN") or "").strip()
        if port and not token:
            raise ConnectorServiceError(
                "invalid_config",
                "配了 WS 端口就必须同时配 WS 令牌（没有令牌时服务端拒绝启动）",
                field_name="LING_ONEBOT_WS_TOKEN",
            )
    if scope == cf.SCOPE_ACCESS:
        mode = str(values.get("LING_ACCESS_MODE") or "off").strip()
        if mode == "whitelist" and not cf.normalize_list(values.get("LING_ACCESS_LIST")):
            raise ConnectorServiceError(
                "invalid_config",
                "白名单模式但名单是空的：这样谁都不能跟她说话",
                field_name="LING_ACCESS_LIST",
            )


async def lock_instance_resources(session: AsyncSession, scope: str, instance_id: str) -> None:
    for key in (f"connector:{scope}:{instance_id}", "connector-resources"):
        await session.execute(
            sql_text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key},
        )


async def validate_instance_config(
    session: AsyncSession, *, scope: str, instance_id: str, values: dict[str, Any],
    settings: object, box: object | None,
) -> None:
    """更新连接前校验账号命名空间及监听端口的独占资源。"""
    from ling.infra.models import ConnectorConfigHead, ConnectorInstance

    if scope == cf.SCOPE_ACCESS:
        return
    target = await session.get(ConnectorInstance, (scope, instance_id))
    namespace_key = f"LING_{scope.upper()}_INSTANCE_ID"
    namespace = str(values.get(namespace_key) or "")
    if not namespace or ":" in namespace or len(namespace) > 64:
        raise ConnectorServiceError(
            "invalid_config", "账号命名空间必须为 1 到 64 个字符且不能含冒号",
            field_name=namespace_key,
        )
    current = await ov.resolve_field_values(
        session, scope=scope, instance_id=instance_id, settings=settings,
        box=box, include_secrets=True,
    )
    old_namespace = current.text(namespace_key)
    if old_namespace and old_namespace != namespace:
        from ling.infra.models import ActorAccount, Window

        used_account = await session.scalar(select(ActorAccount.id).where(
            ActorAccount.connector_instance == old_namespace,
        ).limit(1))
        used_window = await session.scalar(select(Window.id).where(
            Window.title.startswith(old_namespace + ":", autoescape=True),
        ).limit(1))
        if used_account or used_window:
            raise ConnectorServiceError(
                "invalid_config", "这个账号命名空间已有会话记录，请创建新实例接入另一个账号",
                field_name=namespace_key,
            )
    rows = list((await session.execute(select(ConnectorInstance).where(
        ConnectorInstance.scope != cf.SCOPE_ACCESS,
    ))).scalars())
    keys = {(row.scope, row.instance_id) for row in rows}
    running = {(row.scope, row.instance_id) for row in rows
               if row.enabled and row.deleted_at is None}
    # 环境变量默认实例在第一次保存配置前也会参与运行。
    for protocol in (cf.SCOPE_ONEBOT, cf.SCOPE_QQBOT):
        default = await session.get(ConnectorInstance, (protocol, DEFAULT_INSTANCE_ID))
        if default is None:
            keys.add((protocol, DEFAULT_INSTANCE_ID))
            running.add((protocol, DEFAULT_INSTANCE_ID))
    for protocol, other_id in keys - {(scope, instance_id)}:
        resolved = await ov.resolve_field_values(
            session, scope=protocol, instance_id=other_id, settings=settings,
            box=box, include_secrets=True,
        )
        candidates = [resolved.fields_by_key()]
        head = await session.get(ConnectorConfigHead, (protocol, other_id))
        if head is not None and head.candidate_revision_id:
            pending = await ov.read_override_values(
                session, scope=protocol, instance_id=other_id,
                revision_id=head.candidate_revision_id, box=box, include_secrets=True,
            )
            baseline = await ov.resolve_field_values(
                session, scope=protocol, instance_id=other_id, settings=settings,
                box=box, include_secrets=True, use_overrides=False,
            )
            candidates.append({**baseline.fields_by_key(), **pending})
        for candidate in candidates:
            if namespace == str(candidate.get(f"LING_{protocol.upper()}_INSTANCE_ID") or ""):
                raise ConnectorServiceError(
                    "invalid_config", "账号命名空间已由另一个实例使用", field_name=namespace_key
                )
            port = str(values.get("LING_ONEBOT_WS_PORT") or "")
            if (target is None or target.enabled) and (protocol, other_id) in running and (
                scope == protocol == cf.SCOPE_ONEBOT and port and port == str(
                candidate.get("LING_ONEBOT_WS_PORT") or ""
            )):
                raise ConnectorServiceError(
                    "invalid_config", "监听端口已由另一个实例使用",
                    field_name="LING_ONEBOT_WS_PORT",
                )


# ---------------------------------------------------------------- 合并


async def _current_effective(
    session: AsyncSession,
    *,
    scope: str,
    instance_id: str = DEFAULT_INSTANCE_ID,
    settings: object | None,
    onebot: object | None,
    qqbot: object | None,
    box: object | None,
) -> tuple[dict[str, Any], int, str | None]:
    """读当前 active 覆盖 + env 基线，返回 (完整有效值, generation, revision_id)。"""
    resolved = await ov.resolve_field_values(
        session,
        scope=scope,
        instance_id=instance_id,
        settings=settings,
        onebot=onebot,
        qqbot=qqbot,
        box=box,
        include_secrets=True,
    )
    values = {key: state.value for key, state in resolved.fields.items()}
    return values, resolved.generation, resolved.active_revision_id


# ---------------------------------------------------------------- 写入


class ConnectorConfigService:
    """接入器配置的写入与生效。一个实例绑定一个 ``app.state``（切换器）。

    ``runtime`` 为 ``None`` 时是"没法切换"的部署（例如 worker 进程）：
    写入会以 ``activation_unavailable`` 失败——**不写一份假装生效的修订**。
    """

    def __init__(
        self,
        *,
        settings: object,
        box: object | None,
        runtime: RuntimeSwap | None,
        onebot: object | None = None,
        qqbot: object | None = None,
        replica_id: str = "local",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._box = box
        self._runtime = runtime
        self._onebot = onebot
        self._qqbot = qqbot
        self._replica_id = replica_id
        self._clock = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------ 读

    async def resolved(
        self,
        session: AsyncSession,
        *,
        scope: str,
        instance_id: str = DEFAULT_INSTANCE_ID,
        include_secrets: bool = False,
    ) -> ov.ResolvedConnectorConfig:
        """解析某个 scope 在**某个实例**上的有效配置。

        ``instance_id`` 缺省 ``'default'``：单实例部署与升级前行为一致。
        """
        return await ov.resolve_field_values(
            session,
            scope=scope,
            instance_id=instance_id,
            settings=self._settings,
            onebot=self._onebot,
            qqbot=self._qqbot,
            box=self._box,
            include_secrets=include_secrets,
        )

    async def set_running(
        self, session: AsyncSession, *, scope: str, instance_id: str,
        enabled: bool, delete: bool = False,
    ) -> Any:
        """串行执行目录启停与运行切换，失败时由事务保留原目录状态。"""
        from ling.infra.models import ConnectorConfigHead
        from ling.settings.connector_instances import ConnectorInstanceService

        await lock_instance_resources(session, scope, instance_id)
        instances = ConnectorInstanceService(session)
        row = await instances.require_live(scope=scope, instance_id=instance_id)
        await session.refresh(row)
        await instances.require_live(scope=scope, instance_id=instance_id)
        head = await session.get(ConnectorConfigHead, (scope, instance_id), populate_existing=True)
        if head is not None and head.candidate_revision_id:
            raise ConnectorServiceError(
                "activation_in_progress", "配置正在生效，请稍后再启停实例", status_code=409,
            )
        if self._runtime is None:
            raise ConnectorServiceError(
                "activation_unavailable", SAFE_ERRORS["activation_unavailable"], status_code=503,
            )
        if delete:
            # 先校验默认实例保护，再释放连接。
            row = await instances.soft_delete(scope=scope, instance_id=instance_id)
        elif enabled:
            row = await instances.set_enabled(scope=scope, instance_id=instance_id, enabled=True)
            resolved = await self.resolved(
                session, scope=scope, instance_id=instance_id, include_secrets=True,
            )
            values = resolved.fields_by_key()
            validate_full(scope, values)
            await validate_instance_config(
                session, scope=scope, instance_id=instance_id, values=values,
                settings=self._settings, box=self._box,
            )
            outcome = await self._runtime.apply(
                scope, instance_id=instance_id, values=values, operation_id=new_id("enable"),
            )
            if not outcome.ok:
                raise ConnectorServiceError(
                    outcome.error_code or "connector_start_failed",
                    outcome.error_safe or SAFE_ERRORS["connector_start_failed"], status_code=503,
                )
            return row
        else:
            row = await instances.set_enabled(scope=scope, instance_id=instance_id, enabled=False)
        await self._runtime.stop(scope, instance_id=instance_id)
        return row

    # ------------------------------------------------------------ 写

    async def apply(
        self,
        session: AsyncSession,
        *,
        scope: str,
        change: ChangeRequest,
        expected_generation: int | None,
        actor_id: str | None,
        instance_id: str = DEFAULT_INSTANCE_ID,
    ) -> ActivationResult:
        """执行一次保存。返回终态（active / unchanged / rolled_back / degraded）。

        ``expected_generation`` 是 ``If-Match``；``None`` 表示调用方没带
        （只在内部重试路径里允许，HTTP 入口一律要求带）。

        ``instance_id`` 是**实例身份**（``connector_instances`` 的主键列），
        不是 actor 命名空间。它决定这次保存写进哪个实例的修订链、动哪个实例
        的 HEAD 指针与 generation。默认值 ``'default'``——存量单实例部署与
        升级前行为完全一致。

        实例是否存在由调用方先 ``require_live`` 保证，取得锁后复查删除状态。
        传入的 id 原样用于全部读写，
        **不会**在查不到时退回默认实例——静默退回会造成"保存到实例 B，结果
        覆盖了默认实例"，而写入方看不到任何错误。
        """
        started_at = self._clock()
        # operation_id 就是候选修订 id：一次保存一个 id，客户端拿它轮询，
        # 不必再建一张 operation 表（v1 不做独立操作历史）。
        operation_id = new_id("crev")

        # ② 串行化：同一**实例**一次只允许一个写入者。锁随事务释放。
        #
        # 锁键必须带实例：`generation` 是每实例递增的，两个实例的保存互不
        # 相干。只按 scope 锁会让它们互相排队，且掩盖"其实不需要互斥"这件事。
        await lock_instance_resources(session, scope, instance_id)
        from ling.infra.models import ConnectorConfigHead, ConnectorInstance
        from ling.settings.connector_instances import ConnectorInstanceError

        directory = await session.get(ConnectorInstance, (scope, instance_id), populate_existing=True)
        if directory is not None and directory.deleted_at is not None:
            raise ConnectorInstanceError("instance_deleted", "这个实例已被删除", status_code=409)
        head = await session.get(ConnectorConfigHead, (scope, instance_id), populate_existing=True)
        generation = int(getattr(head, "generation", 0) or 0)
        candidate_id = getattr(head, "candidate_revision_id", None)
        if candidate_id:
            raise ConnectorServiceError(
                "activation_in_progress",
                "另一处保存正在生效，等它完成后再试",
                status_code=409,
                operation_id=candidate_id,
            )
        if expected_generation is not None and expected_generation != generation:
            raise ConnectorServiceError(
                "generation_conflict",
                "配置刚被别处改过，请刷新后再保存",
                status_code=409,
            )

        current_values, _gen, active_revision_id = await _current_effective(
            session,
            scope=scope,
            instance_id=instance_id,
            settings=self._settings,
            onebot=self._onebot,
            qqbot=self._qqbot,
            box=self._box,
        )

        # ③ 合并候选：以**当前有效覆盖集合**为底，而非以 env 为底。
        #    以 env 为底会把"上一版 DB 覆盖"一起丢掉，表现是"只改了一项，
        #    别的项悄悄回到 env"。
        overrides = await ov.read_override_values(
            session,
            scope=scope,
            instance_id=instance_id,
            box=self._box,
            include_secrets=True,
        )
        merged = dict(overrides)
        for key in change.clear_keys:
            merged.pop(key, None)
        merged.update(change.set_values)

        # 合并后的完整有效配置 = 未覆盖的取 env/默认基线。
        #
        # 基线必须**另算一份不带覆盖的**：清除某项的意思是"回到 env/默认"，
        # 而 `current_values` 里那一项正是要被清掉的 DB 值。拿 current_values
        # 当基线会让"清除"变成空操作（值没变 → unchanged），用户以为清掉了。
        baseline = await ov.resolve_field_values(
            session,
            scope=scope,
            instance_id=instance_id,
            settings=self._settings,
            onebot=self._onebot,
            qqbot=self._qqbot,
            box=self._box,
            include_secrets=True,
            use_overrides=False,
        )
        effective: dict[str, Any] = {}
        for spec in cf.fields_for(scope):
            if spec.key in merged:
                effective[spec.key] = merged[spec.key]
            else:
                effective[spec.key] = baseline.get(spec.key)
        validate_full(scope, effective)
        await validate_instance_config(
            session, scope=scope, instance_id=instance_id, values=effective,
            settings=self._settings, box=self._box,
        )

        # ④ 有效值没变 → unchanged（不建修订、不重连、不写审计）
        fingerprints = await self._fingerprints(
            scope, current_values, effective, change
        )
        if self._same_effective(scope, current_values, effective, change):
            return ActivationResult(
                operation_id=operation_id,
                scope=scope,
                generation=generation,
                state=OP_UNCHANGED,
                changed=False,
                revision_id=None,
                active_revision_id=active_revision_id,
                started_at=started_at,
                finished_at=self._clock(),
                ack_count=1,
                expected_count=1,
                fingerprints=fingerprints,
            )

        # ⑤ 短事务：候选修订 + 覆盖行 + head 指针
        if self._runtime is None:
            raise ConnectorServiceError(
                "activation_unavailable",
                "这个进程不负责切换连接器，改配置请到 API 进程",
                status_code=503,
            )
        revision_id = await self._write_candidate(
            session,
            scope=scope,
            instance_id=instance_id,
            revision_id=operation_id,
            parent_id=active_revision_id,
            merged=merged,
            actor_id=actor_id,
        )
        await session.commit()

        # ⑥ 事务外切换
        outcome: SwapOutcome
        try:
            from ling.infra.models import ConnectorInstance

            instance = await session.get(ConnectorInstance, (scope, instance_id))
            if instance is not None and not instance.enabled:
                outcome = SwapOutcome(ok=True)
            else:
                outcome = await self._runtime.apply(
                    scope, instance_id=instance_id, values=effective, operation_id=operation_id
                )
        except Exception as exc:  # noqa: BLE001 - 切换器不该抛，抛了也必须回滚
            logger.exception("接入器切换抛错（已按失败处理）：scope=%s", scope)
            outcome = SwapOutcome(
                ok=False,
                error_code="connector_start_failed",
                error_safe=f"切换时发生异常：{type(exc).__name__}",
            )

        finished_at = self._clock()
        if not outcome.ok:
            # ⑦ 失败：修订标 rolled_back/error，head 仍指旧版（不增 generation）
            await self._mark_failed(
                session,
                scope=scope,
                instance_id=instance_id,
                revision_id=revision_id,
                outcome=outcome,
                state=STATE_ROLLED_BACK if outcome.error_code != "rollback_failed" else STATE_DEGRADED,
            )
            await session.commit()
            return ActivationResult(
                operation_id=operation_id,
                scope=scope,
                generation=generation,
                state=OP_ROLLED_BACK
                if outcome.error_code != "rollback_failed"
                else OP_DEGRADED,
                changed=False,
                revision_id=revision_id,
                active_revision_id=active_revision_id,
                started_at=started_at,
                finished_at=finished_at,
                ack_count=1,
                expected_count=1,
                error_code=outcome.error_code,
                error_safe=outcome.error_safe
                or SAFE_ERRORS.get(str(outcome.error_code), SAFE_ERRORS["connector_start_failed"]),
                timings=outcome.timings,
                fingerprints=fingerprints,
            )

        await self._mark_active(
            session,
            scope=scope,
            instance_id=instance_id,
            revision_id=revision_id,
            generation=generation + 1,
            operation_id=operation_id,
            health=outcome.health,
        )
        await session.commit()
        return ActivationResult(
            operation_id=operation_id,
            scope=scope,
            generation=generation + 1,
            state=OP_ACTIVE if outcome.health == "ok" else OP_DEGRADED,
            changed=True,
            revision_id=revision_id,
            active_revision_id=revision_id,
            started_at=started_at,
            finished_at=finished_at,
            ack_count=1,
            expected_count=1,
            error_code=None if outcome.health == "ok" else "remote_unreachable",
            error_safe=None
            if outcome.health == "ok"
            else "配置已在本机生效，但远端还没连上（对端地址/令牌或网络的问题）",
            timings=outcome.timings,
            fingerprints=fingerprints,
        )

    # ------------------------------------------------------------ 内部

    async def _fingerprints(
        self,
        scope: str,
        current: dict[str, Any],
        effective: dict[str, Any],
        change: ChangeRequest,
    ) -> dict[str, str]:
        """每个被改字段的 ``旧指纹>新指纹``。

        在**写入之前**算：POST 之后再读库，"旧值"已经被覆盖了，两边指纹会
        一样（那就等于没记）。用带域分离的 HMAC 截断，不含值也不含预览。
        """
        key = ov.audit_key(self._box)
        out: dict[str, str] = {}
        for name in sorted(set(change.set_values) | set(change.clear_keys)):
            old = await ov.value_fingerprint(
                key, scope=scope, key=name, value=current.get(name)
            )
            new = await ov.value_fingerprint(
                key, scope=scope, key=name, value=effective.get(name)
            )
            out[name] = f"{old}>{new}"
        return out

    def _same_effective(
        self,
        scope: str,
        current: dict[str, Any],
        effective: dict[str, Any],
        change: ChangeRequest,
    ) -> bool:
        """这次请求是否**没有改变任何有效值**。

        比较的是有效值而不是"覆盖行"：把 ``ACCESS_MODE`` 从 env 的 ``off``
        显式覆盖成 ``off`` 也是 unchanged——用户看到的行为一模一样，不该
        因此断一次连接、也不该在审计里留一条"改过"。
        """
        for spec in cf.fields_for(scope):
            before = self._comparable(spec, current.get(spec.key))
            after = self._comparable(spec, effective.get(spec.key))
            if before != after:
                return False
        return True

    @staticmethod
    def _comparable(spec: cf.FieldSpec, value: Any) -> Any:
        if spec.kind == cf.KIND_LIST:
            return tuple(cf.normalize_list(value))
        if spec.kind == cf.KIND_SWITCH:
            return str(value) == "1"
        if value is None:
            return None
        return str(value)

    async def _write_candidate(
        self,
        session: AsyncSession,
        *,
        scope: str,
        revision_id: str,
        parent_id: str | None,
        merged: dict[str, Any],
        actor_id: str | None,
        instance_id: str = DEFAULT_INSTANCE_ID,
    ) -> str:
        from ling.infra.models import (
            ConnectorConfigHead,
            ConnectorConfigRevision,
            ConnectorOverrideValue,
        )

        now = self._clock()
        session.add(
            ConnectorConfigRevision(
                id=revision_id,
                scope=scope,
                # 实例维度**必须显式传**：模型的 `default=DEFAULT_INSTANCE_ID`
                # 只在"单实例时代"是对的。漏传的后果不是报错，而是**所有实例
                # 的配置都写进默认实例**，表现为"另一个实例的配置被覆盖了"。
                instance_id=instance_id,
                parent_id=parent_id,
                state=STATE_PENDING,
                created_by=actor_id,
                created_at=now,
            )
        )
        for key, value in merged.items():
            spec = cf.spec_for(key)
            row = ConnectorOverrideValue(
                id=new_id("covr"),
                revision_id=revision_id,
                field_key=key,
                value_kind=spec.kind,
                created_at=now,
            )
            if spec.kind == cf.KIND_SECRET:
                if self._box is None:
                    raise ConnectorServiceError(
                        "activation_unavailable",
                        "这个进程没有密钥容器，不能保存密钥",
                        status_code=503,
                    )
                row.ciphertext = self._box.encrypt(str(value))  # type: ignore[attr-defined]
                row.preview = cf.secret_preview(str(value))
            else:
                # 名单存数组（保留前导零），其余存字符串；读取侧统一走
                # `plain_value` 的标量/数组分支。
                row.plain_value = (
                    list(cf.normalize_list(value))
                    if spec.kind == cf.KIND_LIST
                    else str(value)
                )
            row.audit_fingerprint = await ov.value_fingerprint(
                ov.audit_key(self._box), scope=scope, key=key, value=value
            )
            session.add(row)

        head = await session.get(ConnectorConfigHead, (scope, instance_id), populate_existing=True)
        if head is None:
            # 显式写 instance_id 而不是靠模型默认值：这行代码的语义就是
            # "给这个实例建指针"，写出来读的人不用翻模型定义才知道建的是哪个实例。
            head = ConnectorConfigHead(
                scope=scope,
                instance_id=instance_id,
                generation=0,
                updated_at=now,
            )
            session.add(head)
        head.candidate_revision_id = revision_id
        head.updated_at = now
        await session.flush()
        return revision_id

    async def _mark_active(
        self,
        session: AsyncSession,
        *,
        scope: str,
        revision_id: str,
        generation: int,
        operation_id: str,
        health: str,
        instance_id: str = DEFAULT_INSTANCE_ID,
    ) -> None:
        from ling.infra.models import (
            ConnectorConfigHead,
            ConnectorConfigRevision,
            ConnectorRuntimeAck,
        )

        now = self._clock()
        revision = await session.get(ConnectorConfigRevision, revision_id)
        if revision is None:  # pragma: no cover - 刚写完就被删只可能是人工干预
            raise ConnectorServiceError("activation_unavailable", "候选修订不见了", status_code=503)
        revision.state = STATE_ACTIVE
        revision.activated_at = now
        revision.error_code = None
        revision.error_safe = None
        head = await session.get(ConnectorConfigHead, (scope, instance_id), populate_existing=True)
        if head is not None:
            head.active_revision_id = revision_id
            head.candidate_revision_id = None
            head.generation = generation
            head.updated_at = now
        existing = (
            await session.execute(
                select(ConnectorRuntimeAck).where(
                    ConnectorRuntimeAck.scope == scope,
                    # 实例维度必须带上：唯一键已经不会让两个实例的第 N 代
                    # 互相冲突，但**不带它的 select 仍会跨实例命中**——表现是
                    # 实例 B 的 ACK 更新了实例 A 那一行（health 被改写），
                    # 且不报错。这与报告 R6 是同一个故障，只是位置从约束挪到
                    # 了查询。
                    ConnectorRuntimeAck.instance_id == instance_id,
                    ConnectorRuntimeAck.generation == generation,
                    ConnectorRuntimeAck.replica_id == self._replica_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                ConnectorRuntimeAck(
                    id=new_id("cack"),
                    scope=scope,
                    instance_id=instance_id,
                    generation=generation,
                    replica_id=self._replica_id,
                    phase="activated",
                    health=health,
                    updated_at=now,
                )
            )
        else:
            existing.phase = "activated"
            existing.health = health
            existing.updated_at = now
        await session.flush()
        logger.info(
            "接入器配置已生效 scope=%s generation=%d revision=%s operation=%s",
            scope,
            generation,
            revision_id,
            operation_id,
        )

    async def _mark_failed(
        self,
        session: AsyncSession,
        *,
        scope: str,
        revision_id: str,
        outcome: SwapOutcome,
        state: str,
        instance_id: str = DEFAULT_INSTANCE_ID,
    ) -> None:
        from ling.infra.models import (
            ConnectorConfigHead,
            ConnectorConfigRevision,
        )

        now = self._clock()
        revision = await session.get(ConnectorConfigRevision, revision_id)
        if revision is not None:
            revision.state = state
            revision.error_code = outcome.error_code or "connector_start_failed"
            revision.error_safe = (
                outcome.error_safe
                or SAFE_ERRORS.get(str(outcome.error_code), SAFE_ERRORS["connector_start_failed"])
            )
        head = await session.get(ConnectorConfigHead, (scope, instance_id), populate_existing=True)
        if head is not None:
            # head 仍指向旧 active 修订；只清掉候选指针。
            head.candidate_revision_id = None
            head.updated_at = now
        await session.flush()
        logger.warning(
            "接入器配置未生效（已回滚）scope=%s revision=%s code=%s",
            scope,
            revision_id,
            revision.error_code if revision is not None else outcome.error_code,
        )


# ---------------------------------------------------------------- 读视图


def activation_payload(
    *,
    scope: str,
    generation: int,
    active_revision_id: str | None,
    candidate_revision_id: str | None,
    last: Any = None,
) -> dict[str, Any]:
    """``activation`` 段：只放无敏感信息的运行状态（设计 §6）。"""
    if candidate_revision_id:
        state = OP_ACTIVATING
    elif last is not None and str(getattr(last, "state", "")) in {
        STATE_ROLLED_BACK,
        STATE_FAILED,
        STATE_DEGRADED,
    }:
        state = (
            OP_ROLLED_BACK
            if str(getattr(last, "state", "")) != STATE_DEGRADED
            else OP_DEGRADED
        )
    else:
        state = OP_ACTIVE
    return {
        "scope": scope,
        "state": state,
        "generation": generation,
        "active_generation": generation,
        "candidate_generation": generation + 1 if candidate_revision_id else None,
        "active_revision_id": active_revision_id,
        "candidate_revision_id": candidate_revision_id,
        "last_revision_id": getattr(last, "id", None),
        "last_state": getattr(last, "state", None),
        "started_at": _iso(getattr(last, "created_at", None)),
        "activated_at": _iso(getattr(last, "activated_at", None)),
        "error_code": getattr(last, "error_code", None),
        "error_safe": getattr(last, "error_safe", None),
        "ack_count": 1 if active_revision_id and not candidate_revision_id else 0,
        "expected_count": 1,
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


async def latest_revision(
    session: AsyncSession, scope: str, *, instance_id: str = DEFAULT_INSTANCE_ID
) -> Any:
    """该 **scope + 实例** 最近一条修订（用于 activation 段回显上次结果）。

    实例维度不是可选装饰：修订表里同一 scope 下有两个实例的修订链，
    只按 scope 过滤会让实例 B 的 ``activation`` 段回显出实例 A 的
    ``last_revision_id`` / ``last_state``——而形状完全正常，看不出串了。
    """
    from sqlalchemy import select

    from ling.infra.models import ConnectorConfigRevision

    return (
        await session.execute(
            select(ConnectorConfigRevision)
            .where(
                ConnectorConfigRevision.scope == scope,
                ConnectorConfigRevision.instance_id == instance_id,
            )
            .order_by(ConnectorConfigRevision.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def pending_revision(
    session: AsyncSession, scope: str, *, instance_id: str = DEFAULT_INSTANCE_ID
) -> Any:
    """该 **scope + 实例** 上正在 pending/activating 的修订（没有则 ``None``）。

    与 :func:`latest_revision` 同罪同罚：只按 scope 过滤会把实例 A 的"正在
    切换"算到实例 B 头上，而"另一处正在生效"正是 409 的依据。
    """
    from sqlalchemy import select

    from ling.infra.models import ConnectorConfigRevision

    return (
        await session.execute(
            select(ConnectorConfigRevision)
            .where(
                ConnectorConfigRevision.scope == scope,
                ConnectorConfigRevision.instance_id == instance_id,
                ConnectorConfigRevision.state.in_((STATE_PENDING, STATE_ACTIVATING)),
            )
            .order_by(ConnectorConfigRevision.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def operation_view(session: AsyncSession, operation_id: str) -> dict[str, Any] | None:
    """按 operation_id 查一次生效操作（幂等重试用同一个 id）。

    v1 不建单独的 operation 表：operation_id **就是**候选修订 id（写入时
    一次生成、随修订落库），所以这里按修订 id 查。
    """
    from ling.infra.models import ConnectorConfigHead, ConnectorConfigRevision

    revision = await session.get(ConnectorConfigRevision, operation_id)
    if revision is None:
        return None
    head = await session.get(ConnectorConfigHead, (revision.scope, revision.instance_id))
    generation = int(getattr(head, "generation", 0) or 0)
    generation_snapshot = await _revision_generation(session, revision)
    if revision.state == STATE_ACTIVE:
        state = OP_ACTIVE
    elif revision.state in (STATE_ROLLED_BACK, STATE_FAILED):
        state = OP_ROLLED_BACK
    elif revision.state == STATE_DEGRADED:
        state = OP_DEGRADED
    else:
        state = OP_ACTIVATING
    return {
        "schema_version": "v1",
        "operation_id": revision.id,
        "scope": revision.scope,
        "state": state,
        "generation": generation_snapshot,
        "active_generation": generation,
        "started_at": _iso(revision.created_at),
        "activated_at": _iso(revision.activated_at),
        "error_code": revision.error_code,
        "error_safe": revision.error_safe,
    }


async def _revision_generation(session: AsyncSession, revision: Any) -> int:
    """这个修订对应几代：active 的修订=当前 generation，否则=下一版。"""
    from ling.infra.models import ConnectorConfigHead

    head = await session.get(ConnectorConfigHead, (revision.scope, revision.instance_id))
    generation = int(getattr(head, "generation", 0) or 0)
    return generation if revision.state == STATE_ACTIVE else generation + 1


__all__ = [
    "ChangeRequest",
    "ConnectorConfigService",
    "ConnectorServiceError",
    "OP_ACTIVE",
    "OP_ACTIVATING",
    "OP_DEGRADED",
    "OP_ROLLED_BACK",
    "OP_UNCHANGED",
    "SAFE_ERRORS",
    "STATE_ACTIVE",
    "STATE_DEGRADED",
    "STATE_PENDING",
    "STATE_ROLLED_BACK",
    "ActivationResult",
    "RuntimeSwap",
    "SwapOutcome",
    "activation_payload",
    "latest_revision",
    "operation_view",
    "pending_revision",
    "validate_full",
]
