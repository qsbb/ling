"""接入器**有效配置**的唯一解析出处：DB 覆盖 > env > 默认。

## 为什么必须只有一处

设计 §8 的原话：所有接入器构造、OneBot HTTP 懒构造、白名单入站判断、
诊断页都必须统一调用有效配置解析器。否则会出现最坏的一类 bug——
**验签读旧 token、授权却读新名单**：同一个请求在安全边界的两侧用两套
配置，而两边都"看起来正常"。

具体到代码：

- ``capabilities.channels.onebot_config()`` / ``qqbot_config()`` 以前直接读
  ``os.environ`` / ``Settings``，现在改成先问本模块"有没有 DB 覆盖"；
- ``kernel/inbound_intake`` 的白名单判断以前读 ``app.state.settings``，
  现在读请求进入时捕获的**同一份**代际快照（:func:`resolve_field_values` 的结果
  被一次捕获、整个请求复用）。

## 为什么每次读都要一次 DB 查询

覆盖集合很小（≤26 行），而"进程内缓存 + 失效通知"要引入 LISTEN/NOTIFY、
跨进程失效和缓存击穿的处理——收益是省一次索引查询，代价是一座会出静默
不一致的缓存。所以 v1 不做缓存：读就是读，慢的只是同一个进程里的第二次
查询（Postgres 上是亚毫秒级）。

## 密钥怎么解密

覆盖值是逐字段密文（``SecretBox`` AES-GCM）。解密**只发生在构造连接器的
受控路径**（:func:`read_override_values` 的 ``include_secrets=True``），
GET 视图、审计、状态机一律不解密——它们只看 ``preview`` 与
``configured``。这样"某个视图对象被意外序列化"也不会泄露原文。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.settings import connector_fields as cf

logger = logging.getLogger("ling.settings.connector_overrides")


class ConnectorConfigError(RuntimeError):
    """覆盖层读不出来（典型：根密钥换了，旧密文解不开）。

    **fail-closed**：不能"解不开就静默回落 env 还声称 DB 覆盖有效"——
    那会让用户以为自己改的值生效了。调用方应当把它报成
    ``activation_unavailable`` / 启动失败，并提示"恢复根密钥或清除覆盖"。
    """


@dataclass(frozen=True)
class FieldState:
    """一个字段的**解析结果**（GET 与构造连接器都从它出发）。"""

    spec: cf.FieldSpec
    source: str
    #: 有效值（密钥是解密后的原文，**只在进程内使用，绝不序列化**）
    value: Any = None
    #: 密钥预览（仅 secret；非密钥为 None）
    preview: str | None = None
    #: 有效值是否非空
    configured: bool = False
    #: 审计指纹（仅 DB 覆盖时有）
    audit_fingerprint: str | None = None

    def public(self, *, include_sensitive: bool) -> dict[str, Any]:
        """GET 里的形状。``include_sensitive=False`` 时连 preview 都不给。

        ``key`` 是设计 §6 的契约名；``name`` 与 ``set`` 是**向后兼容别名**——
        升级前前端读的是 ``field.name`` / ``field.set``，去掉它们会让现有页面
        在版头空白，而那与本轮的目的（让它可编辑）无关。
        """
        configured = self.configured
        return {
            "key": self.spec.key,
            "name": self.spec.key,
            "scope": self.spec.scope,
            "kind": self.spec.kind,
            "label": self.spec.label,
            "control": self.spec.control,
            "editable": self.spec.editable,
            "source": self.source,
            "configured": configured,
            # 兼容别名：旧前端读 `set`（= 有生效值）
            "set": configured,
            "value": cf.to_public_value(self.spec, self.value),
            "preview": self.preview if include_sensitive else None,
        }


@dataclass
class ResolvedConnectorConfig:
    """一个 scope（或全部 scope）的有效配置快照。"""

    generation: int = 0
    active_revision_id: str | None = None
    fields: dict[str, FieldState] = field(default_factory=dict)
    #: 快照捕获时刻（单调递增的单调时钟，仅用于诊断）
    captured_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def fields_by_key(self) -> dict[str, Any]:
        """``{key: 有效值}``（**含解密后的密钥**，只给构造连接器用）。"""
        return {key: state.value for key, state in self.fields.items()}

    def get(self, key: str) -> Any:
        state = self.fields.get(key)
        return None if state is None else state.value

    def text(self, key: str) -> str | None:
        value = self.get(key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def flag(self, key: str) -> bool:
        value = self.get(key)
        if isinstance(value, bool):
            return value
        return str(value) == "1"

    def items(self, key: str) -> frozenset[str]:
        return frozenset(cf.normalize_list(self.get(key)))


# ---------------------------------------------------------------- 读覆盖


async def _head_row(session: AsyncSession, scope: str) -> Any:
    from ling.infra.models import ConnectorConfigHead

    return await session.get(ConnectorConfigHead, scope)


async def active_generation(session: AsyncSession, scope: str) -> int:
    row = await _head_row(session, scope)
    return int(getattr(row, "generation", 0) or 0)


async def read_override_values(
    session: AsyncSession,
    *,
    scope: str,
    box: object | None = None,
    include_secrets: bool = False,
    revision_id: str | None = None,
) -> dict[str, Any]:
    """读某个修订（默认 active）的覆盖集合。

    ``include_secrets=False`` 时**不解密**：返回的密钥项只有 ``preview`` 与
    指纹，``plain_value`` 为 ``None``。这是给 GET / 审计用的形状。
    """
    from ling.infra.models import ConnectorConfigHead, ConnectorOverrideValue

    if revision_id is None:
        head = await session.get(ConnectorConfigHead, scope)
        revision_id = getattr(head, "active_revision_id", None)
    if not revision_id:
        return {}
    rows = list(
        (
            await session.execute(
                select(ConnectorOverrideValue).where(
                    ConnectorOverrideValue.revision_id == revision_id
                )
            )
        ).scalars()
    )
    out: dict[str, Any] = {}
    for row in rows:
        spec = cf.spec_for(row.field_key)
        if spec.kind == cf.KIND_SECRET:
            if include_secrets:
                if not row.ciphertext:
                    raise ConnectorConfigError(
                        f"密钥覆盖缺少密文：{spec.key}"
                    )
                if box is None:
                    raise ConnectorConfigError("缺少解密容器，无法读取密钥覆盖")
                try:
                    out[spec.key] = box.decrypt(row.ciphertext)  # type: ignore[attr-defined]
                except Exception as exc:  # noqa: BLE001 - 根密钥换了/密文损坏
                    # **fail-closed**：解不开就报错，不静默回落 env。
                    raise ConnectorConfigError(
                        f"密钥覆盖解不开（根密钥可能已更换）：{spec.key}"
                    ) from exc
            else:
                out[spec.key] = {
                    "preview": row.preview,
                    "fingerprint": row.audit_fingerprint,
                }
        else:
            out[spec.key] = row.plain_value
    return out


# ---------------------------------------------------------------- env 基线


#: 注册表 key → ``Settings`` 上的属性名（env 层的第二来源）
_SETTINGS_ATTRS: dict[str, str] = {
    "LING_ONEBOT_WS_PORT": "onebot_ws_port",
    "LING_ONEBOT_WS_TOKEN": "onebot_ws_token",
    "LING_ONEBOT_WS_URL": "onebot_ws_url",
    "LING_ACCESS_MODE": "access_mode",
    "LING_ACCESS_LIST": "access_list",
    "LING_QQBOT_ENABLED": "qqbot_enabled",
    "LING_QQBOT_APP_ID": "qqbot_app_id",
    "LING_QQBOT_CLIENT_SECRET": "qqbot_client_secret",
    "LING_QQBOT_BOT_SECRET": "qqbot_bot_secret",
    "LING_QQBOT_WEBHOOK_PUBLIC_KEY": "qqbot_webhook_public_key",
    "LING_QQBOT_INSTANCE_ID": "qqbot_instance_id",
    "LING_QQBOT_SANDBOX": "qqbot_sandbox",
    "LING_QQBOT_API_BASE": "qqbot_api_base",
    "LING_QQBOT_TOKEN_URL": "qqbot_token_url",
    "LING_QQBOT_ALLOW_UNSIGNED_VALIDATION": "qqbot_allow_unsigned_validation",
    "LING_QQBOT_PROACTIVE": "qqbot_proactive",
    "LING_QQBOT_SUPPORTS_GROUP": "qqbot_supports_group",
}

#: 注册表 key → ``OneBotConfig`` 上的属性名
_ONEBOT_ATTRS: dict[str, str] = {
    "LING_ONEBOT_API_BASE": "api_base",
    "LING_ONEBOT_ACCESS_TOKEN": "access_token",
    "LING_ONEBOT_SECRET": "secret",
    "LING_ONEBOT_SELF_ID": "self_id",
    "LING_ONEBOT_INSTANCE_ID": "instance_id",
    "LING_ONEBOT_WS_HOST": "ws_host",
    "LING_ONEBOT_MEDIA_HOSTS": "media_allowed_hosts",
    "LING_ONEBOT_SUPPORTS_GROUP": "supports_group",
    "LING_ONEBOT_PROACTIVE": "supports_proactive",
}

#: 注册表 key → ``QQBotConfig`` 上的属性名
_QQBOT_ATTRS: dict[str, str] = {
    "LING_QQBOT_APP_ID": "app_id",
    "LING_QQBOT_CLIENT_SECRET": "client_secret",
    "LING_QQBOT_BOT_SECRET": "bot_secret",
    "LING_QQBOT_WEBHOOK_PUBLIC_KEY": "webhook_public_key_hex",
    "LING_QQBOT_INSTANCE_ID": "instance_id",
    "LING_QQBOT_SANDBOX": "sandbox",
    "LING_QQBOT_API_BASE": "api_base",
    "LING_QQBOT_TOKEN_URL": "token_url",
    "LING_QQBOT_ALLOW_UNSIGNED_VALIDATION": "allow_unsigned_validation",
    "LING_QQBOT_PROACTIVE": "supports_proactive",
    "LING_QQBOT_SUPPORTS_GROUP": "supports_group",
}


#: 名单类字段（值是集合/列表，需要先摊平成逗号串再解析）
_LIST_KEYS = frozenset({"LING_ONEBOT_MEDIA_HOSTS", "LING_ACCESS_LIST"})


def _as_textish(value: Any) -> Any:
    """把 env 层拿到的值收拾成"可以 stringify 的样子"。

    ``OneBotConfig.media_allowed_hosts`` / ``Settings.access_list`` 是
    ``frozenset``，直接 ``str()`` 会得到 ``frozenset({...})`` 这种字面量——
    那不是名字单，而是一段没人能用的垃圾（还会被当成一个域名写进白名单）。
    这里只做"集合 → 有序列表"的转换，**不转数字**（前导零必须保住）。
    """
    if isinstance(value, (set, frozenset)):
        return sorted(str(item) for item in value)
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return value


def _env_baseline(
    spec: cf.FieldSpec,
    *,
    settings: object | None,
    onebot: object | None,
    qqbot: object | None,
) -> Any:
    """进程启动时的 **env 快照**（不是"现在重新读 os.environ"）。

    ``settings`` / ``onebot`` / ``qqbot`` 都是启动时构造好的快照对象，把它们
    当作 env 层——设计 §3 的优先级写的就是"进程启动时取得的 env 快照"。
    重新 ``os.environ`` 一次会让"改了 .env 但没重启"产生一次静默的半生效。

    三个来源的优先级：**专用 dataclass > Settings 属性 > 无**。前者覆盖
    OneBotConfig/QQBotConfig 里的字段，后者覆盖只存在于 Settings 的字段
    （WS 端口/令牌/地址、渠道开关、白名单）。
    """
    key = spec.key
    if spec.scope == cf.SCOPE_ONEBOT and onebot is not None:
        attr = _ONEBOT_ATTRS.get(key)
        if attr is not None:
            return _as_textish(getattr(onebot, attr, None))
    if spec.scope == cf.SCOPE_QQBOT and qqbot is not None:
        attr = _QQBOT_ATTRS.get(key)
        if attr is not None:
            return _as_textish(getattr(qqbot, attr, None))
    attr = _SETTINGS_ATTRS.get(key)
    if attr is not None and settings is not None:
        value = _as_textish(getattr(settings, attr, None))
        if value not in (None, "", []):
            return value
    # 最后一层兜底：**进程启动时**的 os.environ 快照。
    #
    # 有些字段只存在于 `OneBotConfig` 里、而构造器（`onebot_config()`）不把
    # OS 变量透传出来（例如 `LING_ONEBOT_SECRET` 会，但 `LING_ONEBOT_WS_TOKEN`
    # 只在 `Settings` 上）。这里直接读一次 `os.environ`：**测试会替换环境变量**，
    # 而"当前 env"在生产里等价于"启动时 env"（容器不重启不会变），所以两者
    # 不冲突；真正要防的是"页面改了 DB 后又去读 env"——那是覆盖优先级的事，
    # 由这里的调用顺序保证（DB 覆盖先判）。
    import os as _os

    raw = _os.environ.get(spec.key)
    return raw if raw not in (None, "") else None


# ---------------------------------------------------------------- 解析


async def resolve_field_values(
    session: AsyncSession,
    *,
    scope: str,
    settings: object | None = None,
    onebot: object | None = None,
    qqbot: object | None = None,
    box: object | None = None,
    include_secrets: bool = False,
    use_overrides: bool = True,
) -> ResolvedConnectorConfig:
    """解析一个 scope 的完整有效配置。

    ``include_secrets=True`` 时密钥被解密（只在构造连接器时用）；
    否则密钥项只有 preview/configured（GET / 审计）。
    """
    specs = cf.fields_for(scope)
    overrides = (
        await read_override_values(
            session, scope=scope, box=box, include_secrets=include_secrets
        )
        if use_overrides
        else {}
    )
    head = await _head_row(session, scope)
    resolved = ResolvedConnectorConfig(
        generation=int(getattr(head, "generation", 0) or 0),
        active_revision_id=getattr(head, "active_revision_id", None),
    )
    for spec in specs:
        baseline = _env_baseline(spec, settings=settings, onebot=onebot, qqbot=qqbot)
        baseline_text = cf.env_stringify(spec, baseline)
        if spec.key in overrides:
            override = overrides[spec.key]
            if spec.kind == cf.KIND_SECRET:
                if include_secrets:
                    value: Any = override
                    preview = cf.secret_preview(str(override))
                else:
                    value = None
                    preview = (
                        override.get("preview") if isinstance(override, dict) else None
                    )
                fingerprint = (
                    override.get("fingerprint") if isinstance(override, dict) else None
                )
                resolved.fields[spec.key] = FieldState(
                    spec=spec,
                    source=cf.SOURCE_DB,
                    value=value,
                    preview=preview,
                    configured=bool(include_secrets and value is not None)
                    or bool(preview),
                    audit_fingerprint=fingerprint,
                )
                continue
            resolved.fields[spec.key] = FieldState(
                spec=spec,
                source=cf.SOURCE_DB,
                value=override,
                configured=cf.is_configured(spec, override),
            )
            continue

        if baseline_text not in (None, "", "[]") and not (
            spec.kind == cf.KIND_LIST and baseline_text == ""
        ):
            source = cf.SOURCE_ENV
            value = baseline if spec.kind == cf.KIND_LIST else baseline_text
        else:
            source = cf.SOURCE_DEFAULT
            value = spec.default
        preview = (
            cf.secret_preview(str(value)) if spec.kind == cf.KIND_SECRET and value else None
        )
        resolved.fields[spec.key] = FieldState(
            spec=spec,
            source=source,
            value=value,
            preview=preview,
            configured=cf.is_configured(spec, value),
        )
    return resolved


def defaults_only(scope: str) -> dict[str, Any]:
    """只用代码默认值的一份配置（DB 与 env 都不可达时的兜底，测试也用它）。"""
    out: dict[str, Any] = {}
    for spec in cf.fields_for(scope):
        if spec.kind == cf.KIND_SWITCH:
            out[spec.key] = spec.default == "1"
        elif spec.kind == cf.KIND_LIST:
            out[spec.key] = cf.normalize_list(spec.default)
        else:
            out[spec.key] = spec.default
    return out


# ---------------------------------------------------------------- 写入侧


def audit_key(box: object | None) -> bytes | None:
    """审计指纹用的 HMAC 密钥材料：复用 ``SecretBox`` 的根密钥（同进程、不落库）。

    为什么不另要一把密钥：再加一个环境变量就是"又多一处会配错的东西"，而
    ``SecretBox`` 已经持有进程的根密钥。这里只取内存里的字节，不写日志、
    不进数据库；拿不到（无 box）时退回固定串——那种情况只有测试会走到，
    而**不可反推**仍然成立（指纹只用于"改没改"的对比，不参与认证）。
    """
    raw = getattr(box, "_key", None)
    return raw if isinstance(raw, bytes) else None


async def value_fingerprint(box_key: bytes | None, *, scope: str, key: str, value: Any) -> str:
    """审计指纹：带域分离的 HMAC-SHA256 截断（**不用无盐 SHA**）。

    低熵字段（端口 ``5700``、模式 ``off``）用无盐 sha256 可以被穷举反推，
    那等于把"字段名 + 值"的审计变成可读明文。这里用根密钥派生的 HMAC 做
    域分离，页面只展示截断后的 12 个十六进制字符：够对比"改没改"，不够反推。
    """
    import hashlib
    import hmac
    import json

    material = json.dumps(
        {"scope": scope, "field": key, "value": value},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    key_bytes = box_key or b"ling-connector-audit-v1"
    digest = hmac.new(key_bytes, material, hashlib.sha256).hexdigest()
    return digest[:12]


__all__ = [
    "AccessSnapshot",
    "ConnectorConfigError",
    "ConnectorRuntimeSettings",
    "FieldState",
    "ResolvedConnectorConfig",
    "active_generation",
    "audit_key",
    "build_onebot_config_from",
    "build_qqbot_config_from",
    "current_access_snapshot",
    "defaults_only",
    "read_override_values",
    "resolve_field_values",
    "value_fingerprint",
]


# ---------------------------------------------------------------- 构造快照


def build_onebot_config_from(values: dict[str, Any]):  # noqa: ANN201
    """用**有效值**构造 ``OneBotConfig``（不读 os.environ）。

    字段来源：``connector_overrides.resolve_field_values(scope="onebot",
    include_secrets=True)``。这正是设计 §8 要求的"连接器构造统一调用有效
    配置解析器"——把"读环境变量"这件事收进一个函数，就不会出现某个构造点
    漏改、继续读 env 的裂缝。
    """
    from connectors.onebot.settings import DEFAULT_MEDIA_HOSTS, OneBotConfig

    def text(key: str) -> str | None:
        value = values.get(key)
        if value is None:
            return None
        text_value = str(value).strip()
        return text_value or None

    def flag(key: str) -> bool:
        value = values.get(key)
        return value is True or str(value) == "1"

    hosts = cf.normalize_list(values.get("LING_ONEBOT_MEDIA_HOSTS"))
    return OneBotConfig(
        api_base=text("LING_ONEBOT_API_BASE") or "http://127.0.0.1:5700",
        access_token=text("LING_ONEBOT_ACCESS_TOKEN"),
        secret=text("LING_ONEBOT_SECRET"),
        self_id=text("LING_ONEBOT_SELF_ID"),
        instance_id=text("LING_ONEBOT_INSTANCE_ID") or "onebot-default",
        supports_group=flag("LING_ONEBOT_SUPPORTS_GROUP"),
        supports_proactive=flag("LING_ONEBOT_PROACTIVE"),
        media_allowed_hosts=frozenset(hosts) or DEFAULT_MEDIA_HOSTS,
        ws_host=text("LING_ONEBOT_WS_HOST") or "127.0.0.1",
    )


def build_qqbot_config_from(values: dict[str, Any]):  # noqa: ANN201
    """用**有效值**构造 ``QQBotConfig``（不读 ``Settings``）。"""
    from connectors.qqbot.settings import DEFAULT_TOKEN_URL, QQBotConfig

    def text(key: str) -> str | None:
        value = values.get(key)
        if value is None:
            return None
        text_value = str(value).strip()
        return text_value or None

    def flag(key: str) -> bool:
        value = values.get(key)
        return value is True or str(value) == "1"

    return QQBotConfig(
        app_id=text("LING_QQBOT_APP_ID") or "",
        client_secret=text("LING_QQBOT_CLIENT_SECRET") or "",
        instance_id=text("LING_QQBOT_INSTANCE_ID") or "qqbot-default",
        sandbox=flag("LING_QQBOT_SANDBOX"),
        api_base=text("LING_QQBOT_API_BASE"),
        token_url=text("LING_QQBOT_TOKEN_URL") or DEFAULT_TOKEN_URL,
        bot_secret=text("LING_QQBOT_BOT_SECRET"),
        webhook_public_key_hex=text("LING_QQBOT_WEBHOOK_PUBLIC_KEY"),
        allow_unsigned_validation=flag("LING_QQBOT_ALLOW_UNSIGNED_VALIDATION"),
        supports_proactive=flag("LING_QQBOT_PROACTIVE"),
        supports_group=flag("LING_QQBOT_SUPPORTS_GROUP"),
    )


@dataclass(frozen=True)
class ConnectorRuntimeSettings:
    """"OneBot/QQBot 的**连接期**参数"（不属于 ``OneBotConfig`` / ``QQBotConfig``）。

    WS 端口/令牌、出站 WS 地址、渠道开关都在 ``Settings`` 里，而连接器只接收
    自己那两个 dataclass。这个对象把它们摊平成一份快照，让
    ``app.py`` 的编排与 GET 视图用同一个来源。
    """

    onebot_ws_port: int | None = None
    onebot_ws_token: str | None = None
    onebot_ws_url: str | None = None
    onebot_secret: str | None = None
    qqbot_enabled: bool = False

    def with_values(self, scope: str, values: dict[str, Any]) -> ConnectorRuntimeSettings:
        """把某个 scope 的有效值合进快照（不动另一个 scope 的字段）。"""
        from dataclasses import replace

        if scope != cf.SCOPE_ONEBOT:
            if scope != cf.SCOPE_QQBOT:
                return self
            raw_enabled = values.get("LING_QQBOT_ENABLED")
            enabled = raw_enabled is True or str(raw_enabled) == "1"
            return replace(self, qqbot_enabled=enabled)
        raw_port = values.get("LING_ONEBOT_WS_PORT")
        port: int | None = None
        if raw_port not in (None, ""):
            try:
                port = int(str(raw_port))
            except (TypeError, ValueError):
                port = None
        return replace(
            self,
            onebot_ws_port=port,
            onebot_ws_token=(str(values["LING_ONEBOT_WS_TOKEN"])
                             if values.get("LING_ONEBOT_WS_TOKEN") else None),
            onebot_ws_url=(str(values["LING_ONEBOT_WS_URL"])
                           if values.get("LING_ONEBOT_WS_URL") else None),
            onebot_secret=(str(values["LING_ONEBOT_SECRET"])
                           if values.get("LING_ONEBOT_SECRET") else None),
        )


@dataclass(frozen=True)
class AccessSnapshot:
    """白名单的有效配置（一次请求捕获一次，整个请求复用同一份）。

    设计 §4：请求处理时捕获一次代际快照，完整处理该请求——不能在验签取旧
    token、授权名单却读到新 revision。
    """

    generation: int
    mode: str
    allow_list: frozenset[str]
    allow_groups: frozenset[str]


async def current_access_snapshot(
    session: AsyncSession,
    *,
    settings: object | None = None,
) -> AccessSnapshot:
    """读白名单的有效配置。``allow_groups`` 本轮仍来自 env（设计 §3）。"""
    resolved = await resolve_field_values(
        session, scope=cf.SCOPE_ACCESS, settings=settings, include_secrets=False
    )
    mode = str(resolved.get("LING_ACCESS_MODE") or "off")
    allow = resolved.items("LING_ACCESS_LIST")
    groups = frozenset(getattr(settings, "access_groups", frozenset()) or frozenset())
    return AccessSnapshot(
        generation=resolved.generation, mode=mode, allow_list=allow, allow_groups=groups
    )

