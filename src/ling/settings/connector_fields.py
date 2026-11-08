"""接入器**字段注册表**：哪些配置项允许在网页上改、怎么校验、怎么脱敏。

## 为什么必须有注册表，而不是"请求里给什么键就写什么键"

覆盖层是把值写进数据库、再由进程读出来构造连接器的。如果键名来自请求，
那等于开了一个"任意环境变量写入"的口子：``LING_ROOT_KEY``、``DATABASE_URL``、
``LING_BOOTSTRAP_ADMIN_PASSWORD`` 这些只要被写进去，下一次构造/启动就会
用上攻击者的值。所以**只有这里列出来的键**能被写入，其余一律 422
``invalid_field``——白名单在服务端，不接受前端或迁移"动态生成"。

## 三档来源（``source``）

    数据库已生效覆盖 > 进程启动时的 env 快照 > 代码默认值

- ``db``：这一项在本 scope 的 **active 修订**里有覆盖；
- ``env``：没有覆盖，但进程启动时 env 里给了值；
- ``default``：没有覆盖、env 也没有，用的是代码默认。

"清除"一项 = 删除它在下一修订里的覆盖记录（回退到 env），**不是写空串**：
空串在密钥上是"没设过"，在文本上是"显式空值"，两者语义不同，混用会让
"我明明清掉了，怎么还是空字符串"变成一个查不出来的问题。

## 密钥四档（``kind``）

- ``secret``：只进密文，GET 只回 ``preview``（前 7 个 Unicode 字符 + ``••••``）与
  ``configured``，``value`` 恒为 ``null``；
- ``text``：非密钥，GET 回规范化后的值；
- ``switch``：布尔，GET 回 ``true/false``；
- ``list``：有序去重字符串数组（名单），**保留字符串与前导零**。

``secret_preview`` 的规则（设计 §5）：按 Unicode 字符取前
``min(7, len-1)`` 个再接固定遮罩；1 个字符的密钥只显示遮罩。也就是说
7 字符的密钥最多露出 6 个字符，**任何长度都至少遮住 1 位**，且遮罩不暴露
剩余长度。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

# ------------------------------------------------------------------ 常量

KIND_SECRET = "secret"
KIND_TEXT = "text"
KIND_SWITCH = "switch"
KIND_LIST = "list"

SOURCE_DB = "db"
SOURCE_ENV = "env"
SOURCE_DEFAULT = "default"

SCOPE_ONEBOT = "onebot"
SCOPE_QQBOT = "qqbot"
SCOPE_ACCESS = "access"
SCOPES: tuple[str, ...] = (SCOPE_ONEBOT, SCOPE_QQBOT, SCOPE_ACCESS)

#: 遮罩：固定四颗点，不反映剩余长度（设计 §5）。
SECRET_MASK = "••••"

#: 密钥预览最多显示几个字符（「前 7 个」的口径见 :func:`secret_preview`）。
SECRET_PREVIEW_CHARS = 7

ACCESS_MODES: tuple[str, ...] = ("off", "whitelist", "blacklist")


class FieldError(ValueError):
    """某个字段的值不合法。

    ``code`` 是稳定错误码（``invalid_field``），``field`` 是注册表里的键名。
    **不携带用户输入值**：它会被打进错误响应与日志，带值等于把密码抄进日志。
    """

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(reason)
        self.field = field
        self.reason = reason
        self.code = "invalid_field"


# ------------------------------------------------------------------ 校验器


def _reject_control_chars(field: str, value: str) -> None:
    """控制字符一律拒绝（换行/回车/制表/其余 C0+DEL）。

    密钥或 URL 里混进控制字符只可能是粘贴事故或注入尝试，两种都不该写进库、
    更不该发给上游。**不 trim、不做 NFC**：设计 §5 明确密钥按原字节保存
    （只看有没有控制字符，不改写它）。
    """
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise FieldError(field, "值里不能有控制字符（换行、制表等）")


def _check_url(field: str, value: str, *, schemes: tuple[str, ...]) -> None:
    """URL：scheme 白名单 + 禁止 userinfo + 禁止敏感 query。

    - ``ws://user:pass@host`` 这类 **userinfo** 会把凭据塞进普通字段，
      GET 回显时等于明文回传，所以直接拒；
    - query 里出现 ``access_token``/``token``/``secret``/``key``/``password``
      同理（OneBot 的 WS 地址确实可能带 ``?access_token=``，那种情况请把
      令牌填在专用字段里）。
    """
    _reject_control_chars(field, value)
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise FieldError(field, "不是合法的 URL") from exc
    if parsed.scheme.lower() not in schemes:
        raise FieldError(field, f"只接受 {'/'.join(schemes)} 地址")
    if not parsed.hostname:
        raise FieldError(field, "URL 缺少主机名")
    if parsed.username or parsed.password:
        raise FieldError(field, "URL 里不能内嵌用户名或口令，请填到对应字段")
    for name in _SENSITIVE_QUERY_KEYS:
        if name in parsed.query.lower():
            raise FieldError(field, "URL 的查询串里不能放令牌，请填到对应字段")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise FieldError(field, "URL 端口必须在 1–65535 之间")


_SENSITIVE_QUERY_KEYS: tuple[str, ...] = (
    "access_token",
    "token",
    "secret",
    "password",
    "api_key",
    "apikey",
)

_HOST_RE = re.compile(r"^[A-Za-z0-9._:\-\[\]]+$")
_PORT_RE = re.compile(r"^\d{1,5}$")


def _check_host(field: str, value: str) -> None:
    _reject_control_chars(field, value)
    if not _HOST_RE.match(value):
        raise FieldError(field, "监听地址只能是主机名或 IP（如 127.0.0.1、0.0.0.0）")


def _check_port(field: str, value: str) -> None:
    _reject_control_chars(field, value)
    if not _PORT_RE.match(value):
        raise FieldError(field, "端口必须是数字")
    port = int(value)
    if not 1 <= port <= 65535:
        raise FieldError(field, "端口必须在 1–65535 之间")


def _check_access_mode(field: str, value: str) -> None:
    if value not in ACCESS_MODES:
        raise FieldError(field, f"只能填 {' / '.join(ACCESS_MODES)}")


def _check_plain_text(field: str, value: str, *, max_len: int) -> None:
    _reject_control_chars(field, value)
    if len(value) > max_len:
        raise FieldError(field, f"太长了（上限 {max_len} 个字符）")


# ------------------------------------------------------------------ 字段定义


@dataclass(frozen=True)
class FieldSpec:
    """注册表里的一项。

    ``default`` 是**代码默认值**（与 ``Settings`` / ``onebot_config()`` 保持一致），
    只用于回答"没有覆盖、env 也没有时生效的是什么"，**不写进数据库**——
    迁移期把默认复制一份进库，之后两边就一定会漂。
    """

    key: str
    scope: str
    kind: str
    label: str
    #: 取值上限（字符数）。密钥没有上限的业务含义，只防一个离谱的长串。
    max_len: int = 512
    default: str | None = None
    validate: Callable[[str, str], None] | None = None
    #: 单项是否允许在网页上编辑。本轮全部可编辑；保留位给将来的"只读字段"。
    editable: bool = True
    #: 前端控件类型提示（纯展示，不是校验依据）。
    control: str = "text"

    def check(self, value: str) -> str:
        """校验并返回**规范化后**的值（文本只做长度/控制字符检查，不改内容）。

        密钥**不做 trim/NFC**：设计 §5 要求按原字节加密并送上游。
        """
        _reject_control_chars(self.key, value)
        if len(value) > self.max_len:
            raise FieldError(self.key, f"太长了（上限 {self.max_len} 个字符）")
        if self.validate is not None:
            self.validate(self.key, value)
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "scope": self.scope,
            "kind": self.kind,
            "label": self.label,
            "control": self.control,
            "editable": self.editable,
            "default": self.default,
        }


def _secret(
    key: str, scope: str, label: str, *, max_len: int = 512, control: str = "password"
) -> FieldSpec:
    return FieldSpec(
        key=key, scope=scope, kind=KIND_SECRET, label=label, max_len=max_len, control=control
    )


def _text(
    key: str,
    scope: str,
    label: str,
    *,
    default: str | None = None,
    max_len: int = 512,
    validate: Callable[[str, str], None] | None = None,
) -> FieldSpec:
    return FieldSpec(
        key=key,
        scope=scope,
        kind=KIND_TEXT,
        label=label,
        default=default,
        max_len=max_len,
        validate=validate,
        control="text",
    )


def _switch(key: str, scope: str, label: str, *, default: bool) -> FieldSpec:
    return FieldSpec(
        key=key,
        scope=scope,
        kind=KIND_SWITCH,
        label=label,
        default="1" if default else "0",
        max_len=8,
        control="switch",
    )


def _list(key: str, scope: str, label: str, *, max_len: int = 64) -> FieldSpec:
    return FieldSpec(
        key=key, scope=scope, kind=KIND_LIST, label=label, max_len=max_len, control="list"
    )


# --- OneBot：12 项（设计 §3）---

ONEBOT_FIELDS: tuple[FieldSpec, ...] = (
    _text(
        "LING_ONEBOT_API_BASE",
        SCOPE_ONEBOT,
        "OneBot HTTP API 地址（发消息用）",
        default="http://127.0.0.1:5700",
        validate=lambda f, v: _check_url(f, v, schemes=("http", "https")),
    ),
    _secret("LING_ONEBOT_ACCESS_TOKEN", SCOPE_ONEBOT, "访问令牌（调 OneBot API 用）"),
    _text("LING_ONEBOT_SELF_ID", SCOPE_ONEBOT, "机器人自己的 QQ 号（校验上报来源）", max_len=32),
    _secret("LING_ONEBOT_SECRET", SCOPE_ONEBOT, "HTTP 上报签名密钥（HMAC-SHA1）"),
    _text(
        "LING_ONEBOT_WS_PORT",
        SCOPE_ONEBOT,
        "WS 服务端端口（NapCat 连进来；留空=不启用）",
        max_len=5,
        validate=_check_port,
    ),
    _secret("LING_ONEBOT_WS_TOKEN", SCOPE_ONEBOT, "WS 服务端令牌", control="password"),
    _secret("LING_ONEBOT_WS_URL", SCOPE_ONEBOT, "出站 WS 地址（ling 主动连 NapCat）"),
    _text(
        "LING_ONEBOT_WS_HOST",
        SCOPE_ONEBOT,
        "WS 服务端监听地址",
        default="127.0.0.1",
        max_len=64,
        validate=_check_host,
    ),
    _text(
        "LING_ONEBOT_INSTANCE_ID",
        SCOPE_ONEBOT,
        "实例标识（账号绑定按它隔离）",
        default="onebot-default",
        max_len=64,
    ),
    _switch("LING_ONEBOT_SUPPORTS_GROUP", SCOPE_ONEBOT, "声明支持群聊", default=True),
    _switch("LING_ONEBOT_PROACTIVE", SCOPE_ONEBOT, "声明支持主动推送", default=False),
    _list("LING_ONEBOT_MEDIA_HOSTS", SCOPE_ONEBOT, "媒体下载域名白名单（留空=用内置默认）"),
)

# --- QQBot：12 项（设计 §3）---

QQBOT_FIELDS: tuple[FieldSpec, ...] = (
    _switch("LING_QQBOT_ENABLED", SCOPE_QQBOT, "渠道开关", default=False),
    _secret(
        "LING_QQBOT_APP_ID", SCOPE_QQBOT, "AppID（开放平台「开发设置」）", control="password"
    ),
    _secret("LING_QQBOT_CLIENT_SECRET", SCOPE_QQBOT, "AppSecret（换 access_token 用）"),
    _secret("LING_QQBOT_BOT_SECRET", SCOPE_QQBOT, "Webhook 签名密钥 BotSecret"),
    _text(
        "LING_QQBOT_WEBHOOK_PUBLIC_KEY",
        SCOPE_QQBOT,
        "Ed25519 公钥 hex（给公钥时优先用它验签）",
        max_len=256,
    ),
    _text(
        "LING_QQBOT_INSTANCE_ID",
        SCOPE_QQBOT,
        "实例标识（openid 按它隔离）",
        default="qqbot-default",
        max_len=64,
    ),
    _switch("LING_QQBOT_SANDBOX", SCOPE_QQBOT, "沙箱环境", default=True),
    _text(
        "LING_QQBOT_API_BASE",
        SCOPE_QQBOT,
        "API 基址（留空按沙箱/正式自动选）",
        validate=lambda f, v: _check_url(f, v, schemes=("http", "https")),
    ),
    _text(
        "LING_QQBOT_TOKEN_URL",
        SCOPE_QQBOT,
        "取 access_token 的地址（留空用官方默认）",
        validate=lambda f, v: _check_url(f, v, schemes=("http", "https")),
    ),
    _switch(
        "LING_QQBOT_ALLOW_UNSIGNED_VALIDATION",
        SCOPE_QQBOT,
        "允许回调 URL 校验请求不带签名",
        default=False,
    ),
    _switch("LING_QQBOT_PROACTIVE", SCOPE_QQBOT, "声明支持主动推送", default=False),
    _switch("LING_QQBOT_SUPPORTS_GROUP", SCOPE_QQBOT, "声明支持群聊", default=True),
)

# --- 白名单：2 项（设计 §3；`LING_ACCESS_GROUPS` 本轮仍走 env）---

ACCESS_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        key="LING_ACCESS_MODE",
        scope=SCOPE_ACCESS,
        kind=KIND_TEXT,
        label="出入许可模式",
        default="off",
        max_len=16,
        validate=_check_access_mode,
        control="select",
    ),
    _list("LING_ACCESS_LIST", SCOPE_ACCESS, "发送者白名单（QQ 号，按字符串比较）", max_len=32),
)

REGISTRY: tuple[FieldSpec, ...] = (*ONEBOT_FIELDS, *QQBOT_FIELDS, *ACCESS_FIELDS)

_BY_KEY: dict[str, FieldSpec] = {spec.key: spec for spec in REGISTRY}
_BY_SCOPE: dict[str, tuple[FieldSpec, ...]] = {
    SCOPE_ONEBOT: ONEBOT_FIELDS,
    SCOPE_QQBOT: QQBOT_FIELDS,
    SCOPE_ACCESS: ACCESS_FIELDS,
}

#: 每个 scope 的字段数。设计 §3 写死 12/12/2，测试直接断言这三个数——
#: 少一项（比如漏了 ``ACCESS_TOKEN``）在界面上是"看不见"，不是报错。
EXPECTED_FIELD_COUNTS: dict[str, int] = {
    SCOPE_ONEBOT: 12,
    SCOPE_QQBOT: 12,
    SCOPE_ACCESS: 2,
}


def spec_for(key: str) -> FieldSpec:
    """按 key 取注册项；不在注册表里抛 ``FieldError``（**拒绝任意 env 名**）。"""
    spec = _BY_KEY.get(key)
    if spec is None:
        raise FieldError(key, "不认识的配置项（只能修改设置页列出的项）")
    return spec


def fields_for(scope: str) -> tuple[FieldSpec, ...]:
    spec = _BY_SCOPE.get(scope)
    if spec is None:
        raise FieldError(scope, f"不认识的接入器：{scope}")
    return spec


def is_known_scope(scope: str) -> bool:
    return scope in _BY_SCOPE


# ------------------------------------------------------------------ 预览与脱敏


def secret_preview(value: str | None) -> str | None:
    """密钥预览：前 ``min(7, len-1)`` 个字符 + 固定遮罩；空值返回 ``None``。

    7 字符的密钥 → 6 个字符 + 遮罩；1 字符 → 只有遮罩。**任何长度都至少
    遮住 1 位**，且遮罩长度固定，看不出原文有多长。
    """
    if not value:
        return None
    chars = list(value)
    shown = min(SECRET_PREVIEW_CHARS, max(len(chars) - 1, 0))
    return "".join(chars[:shown]) + SECRET_MASK


def normalize_list(value: str | list[Any] | None) -> list[str]:
    """名单规范化：按字符串 trim、去空、去重（**保持首次出现顺序**）。

    **绝不转 int**：QQ 号可能带前导零（``0123``），转数字会静默把它变成
    ``123``——那是一条永远匹配不上的白名单项。
    """
    if value is None:
        return []
    parts = value if isinstance(value, list) else str(value).split(",")
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = str(part).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def normalize_switch(value: Any) -> str:
    """布尔字段的规范化：接受 ``1/0``、``true/false``、真布尔。"""
    if isinstance(value, bool):
        return "1" if value else "0"
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return "1"
    if text in {"0", "false", "no", "off", ""}:
        return "0"
    raise FieldError("", "开关只能是开或关")


def normalize_value(spec: FieldSpec, value: Any) -> str | list[str]:
    """把请求里的值规范化成"要存的样子"。

    - ``switch`` → ``"1"``/``"0"`` 字符串；
    - ``list`` → 去重后的字符串数组；
    - 其余 → 字符串（控制字符/长度/字段校验器在 :meth:`FieldSpec.check` 里）。
    """
    if spec.kind == KIND_SWITCH:
        try:
            return normalize_switch(value)
        except FieldError as exc:
            raise FieldError(spec.key, exc.reason) from exc
    if spec.kind == KIND_LIST:
        return normalize_list(value if isinstance(value, list) else str(value))
    return spec.check("" if value is None else str(value))


def to_public_value(spec: FieldSpec, value: Any) -> Any:
    """GET 里的 ``value``：密钥恒 ``None``；开关给布尔；名单给数组。"""
    if spec.kind == KIND_SECRET:
        return None
    if value is None:
        return None
    if spec.kind == KIND_SWITCH:
        return str(value) == "1"
    if spec.kind == KIND_LIST:
        return normalize_list(value)
    return value


def is_configured(spec: FieldSpec, value: Any) -> bool:
    """``configured`` = **有效值非空**（不等于"DB 有覆盖"）。"""
    if value is None:
        return False
    if spec.kind == KIND_SWITCH:
        # 开关总有状态：配置了就是"有值"（值本身看 value）。
        return True
    if spec.kind == KIND_LIST:
        return bool(normalize_list(value))
    return str(value).strip() != ""


def env_stringify(spec: FieldSpec, value: Any) -> str | None:
    """env 值 → 存储用的字符串形状（名单用逗号拼接，开关用 1/0）。"""
    if value is None:
        return None
    if spec.kind == KIND_LIST:
        items = normalize_list(value)
        return ",".join(items) if items else None
    if spec.kind == KIND_SWITCH:
        return "1" if bool(value) or str(value) == "1" else "0"
    return str(value)


__all__ = [
    "ACCESS_FIELDS",
    "ACCESS_MODES",
    "EXPECTED_FIELD_COUNTS",
    "KIND_LIST",
    "KIND_SECRET",
    "KIND_SWITCH",
    "KIND_TEXT",
    "ONEBOT_FIELDS",
    "QQBOT_FIELDS",
    "REGISTRY",
    "SCOPES",
    "SCOPE_ACCESS",
    "SCOPE_ONEBOT",
    "SCOPE_QQBOT",
    "SECRET_MASK",
    "SECRET_PREVIEW_CHARS",
    "SOURCE_DB",
    "SOURCE_DEFAULT",
    "SOURCE_ENV",
    "FieldError",
    "FieldSpec",
    "env_stringify",
    "fields_for",
    "is_configured",
    "is_known_scope",
    "normalize_list",
    "normalize_switch",
    "normalize_value",
    "secret_preview",
    "spec_for",
    "to_public_value",
]
