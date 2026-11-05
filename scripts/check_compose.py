#!/usr/bin/env python3
"""Compose 静态校验：不启动容器，只检查配置结构与安全基线。

校验两个**互斥的主文件**及镜像模式的 WS override：

- `deploy/compose.yaml`：源码模式（build: + 前端 dist bind）
- `deploy/compose.image.yaml`：镜像模式（api/worker 同一个必填镜像摘要，
  无 build、无 `/app/web` bind），见设计文档 §4
- `deploy/compose.image.ws.example.yaml`：镜像模式的可选反向 WS override

两份主文件是**择一**关系（设计 §4），不允许 `-f compose.yaml -f compose.image.yaml`
叠加。用 `--check-override-pair FILE [FILE ...]` 可以单独校验一组 `-f`：

    python scripts/check_compose.py                                    # 结构校验（默认）
    python scripts/check_compose.py --check-override-pair deploy/compose.yaml \
        deploy/compose.image.ws.example.yaml                           # 叠加对校验

退出码：0 通过，1 有错误（2 = 命令行用法错误）。

**这不能替代实际 `docker compose up` / `docker compose config`**：镜像拉取、
健康检查、迁移时序、网络与卷挂载都需要真实 Docker 才能验证；无 Docker 时
本脚本只做结构校验，并如实说明"未在真实 compose 上验证"。脚本只打印结构
摘要，**不**打印完整 config（`.env` 里的口令不会进终端/CI 日志）。
"""

from __future__ import annotations

import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]

SOURCE_COMPOSE = pathlib.Path("deploy") / "compose.yaml"
IMAGE_COMPOSE = pathlib.Path("deploy") / "compose.image.yaml"
IMAGE_WS_OVERRIDE = pathlib.Path("deploy") / "compose.image.ws.example.yaml"
ENV_EXAMPLE = pathlib.Path("deploy") / "env.example"

REQUIRED_SERVICES = {"db", "api", "worker"}
REQUIRED_VOLUME_KEYS = {"db-data", "app-data"}
PROJECT_NAME = "ling"
# 两份主文件共用的变量白名单锚点（`x-app-env: &app-env`）。锚点里每一行就是
# 容器真正会收到的变量，所以镜像模式必须逐条一致：少一行 = 用户在 `.env` 里配了
# 也进不了容器（没有任何提示），多一行 = 镜像模式偷偷多出源码模式没有的配置，
# 值被改 = 默认行为直接变掉。
APP_ENV_KEY = "x-app-env"
# 逐条比对的豁免：变量名 → 为什么允许不同。当前**没有任何豁免**（空字典），
# 两份主文件的 `x-app-env` 必须逐条相同；确有例外时在这里写理由，不要放宽比较。
X_APP_ENV_DIFF_ALLOWED: dict[str, str] = {}
# 镜像模式必须用的**必填**插值：`${LING_APP_IMAGE:?...}`。
# `:-`（带默认值）与固定 tag 都不接受——前者会悄悄回退到别的镜像，
# 后者说不清部署了哪组字节（设计 §4）。
IMAGE_APP_REQUIRED = re.compile(r"^\$\{LING_APP_IMAGE:\?[^}]*\}$")

# --- 更新通道（P5 接线后的最小权限）---
#
# P1 时镜像主文件按设计"初期不挂 update 通道"，所以旧规则是
# 「api/worker 一律不许出现 /run/ling-update*」。P5 已经交付了
# `POST /v1/version/image-update-requests`（写请求）与
# `GET /v1/version/image-update`（读更新器状态），**必须**给 api 挂上这两个
# 目录，否则真实部署里 POST 必然失败。设计要求是「API 仅获得**写请求/读状态**
# 的最小目录权限」，所以规则从"禁止"改成**精确允许**：
#
# - 只有 api 能挂，且只能是下面这两个**精确**目标（多一个都不行）；
# - requests 必须 rw（原子写请求文件）、status 必须 ro（更新器是唯一写者，
#   API 写不了才伪造不了状态）；
# - 挂载源必须是**必填插值** `${VAR:?...}`：缺值时 `docker compose config`
#   直接失败，而不是静默少挂一条（少挂的表现是 POST 永远被拒，很难追）；
# - api 还必须加入共享组（`group_add`），否则 requests 目录的组写位用不上。
UPDATE_TARGET_PREFIX = "/run/ling-update"
UPDATE_REQUESTS_TARGET = f"{UPDATE_TARGET_PREFIX}/requests"
UPDATE_STATUS_TARGET = f"{UPDATE_TARGET_PREFIX}/status"
# 目标 → 必须的挂载模式。顺序即 api 上的期望顺序（不强制，但便于报错）。
UPDATE_MOUNTS: dict[str, str] = {
    UPDATE_REQUESTS_TARGET: "rw",
    UPDATE_STATUS_TARGET: "ro",
}
# 共享组变量：`ling install` 把宿主 requests/state 的属组设成更新器 gid，
# API 容器靠这个补充组写请求、读状态。
UPDATE_SHARED_GID_VAR = "LING_UPDATE_SHARED_GID"
# 网页更新开关：必须是插值且**默认解析为 0**（`${LING_UPDATE_ENABLED:-0}`）。
# 写死 "0" = 用户改 `.env` 也打不开（只能去安装目录手改 compose）；
# 写死 "1" = 默认就开。两者都不是"默认关、可显式开"。
UPDATE_ENABLED_VAR = "LING_UPDATE_ENABLED"
UPDATE_ENABLED_SWITCH_RE = re.compile(
    r"^\$\{" + re.escape(UPDATE_ENABLED_VAR) + r"(:?-0)\}$"
)
REQUIRED_INTERPOLATION_RE = re.compile(
    r"^\$\{([A-Za-z_][A-Za-z0-9_]*):\?[^}]*\}$"
)
# 变量文档：`VAR=` 与注释态的 `# VAR=` 都算“已说明”（env.example 里
# LING_APP_IMAGE 就是注释态：源码模式用不到它，镜像模式必须配）。
ENV_ASSIGN_RE = re.compile(r"^(?:#\s*)?([A-Z_][A-Z0-9_]*)=", re.M)
REFERENCE_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)")

# 只对源码构建/源码模式有意义的变量：镜像主文件里不应再出现（设计 §4）。
SOURCE_ONLY_VARS = {
    "LING_PYTHON_BASE",
    "PIP_INDEX_URL",
    "LING_WEB_DIST_HOST",
    "LING_COMMIT",
}


def split_outside_braces(text: str, sep: str = ":") -> list[str]:
    """按 `sep` 切分，但**不**切入 `${...}` 内部。

    Compose 的插值允许 `${VAR:?错误信息}`，信息里还可能有冒号；直接
    `rsplit(":")` 会把容器侧端口切成 `?错误信息}`。这里按花括号深度切分。
    """
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    index = 0
    while index < len(text):
        if text.startswith("${", index):
            depth += 1
            buf.append("${")
            index += 2
            continue
        char = text[index]
        if char == "}" and depth:
            depth -= 1
        if char == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(char)
        index += 1
    parts.append("".join(buf))
    return parts


def _strip_protocol(text: str) -> str:
    for proto in ("/tcp", "/udp"):
        if text.endswith(proto):
            return text[: -len(proto)]
    return text


def container_side_port(entry: object) -> str | None:
    """取一条 compose `ports` 条目的**容器侧**端口（原样字符串，不做数值解析）。

    支持 Compose 的两种写法：

    - 短语法：`"8080:6520"`、`"6520"`（只写容器侧）、`"127.0.0.1:8080:6520"`、
      `"[::1]:8080:6520"`，可带 `/tcp`、`/udp` 协议后缀。容器侧永远是**最后
      一段**（IPv6 里冒号很多，按最后一段取同样成立）。
    - 长语法：`{target: 6520, published: "8080", protocol: tcp}`，取 `target`。

    为什么不能用 `"6520" in item` 这种子串判断：宿主侧默认值也写着 6520
    （`"${LING_PORT:-6520}:16520"`），子串匹配命中的是**宿主侧**，容器侧明明
    写错了照样 PASS，安装器就会对着一个不是 API 的端口等就绪。

    变量内部（`${VAR:?信息}`）的冒号不参与切分，见 `split_outside_braces`。
    """
    if isinstance(entry, dict):
        target = entry.get("target")
        return None if target is None else str(target).strip()
    text = _strip_protocol(str(entry).strip())
    parts = split_outside_braces(text)
    return parts[-1].strip()


def host_side_port(entry: object) -> str | None:
    """取一条 `ports` 条目的**宿主侧**（发布）端口；只写容器侧时返回 None。

    只用于校验镜像模式的发布端口表达式，不做数值解析。
    """
    if isinstance(entry, dict):
        published = entry.get("published")
        return None if published is None else str(published).strip()
    parts = split_outside_braces(_strip_protocol(str(entry).strip()))
    return ":".join(parts[:-1]).strip() if len(parts) >= 2 else None


def service_environment(svc: dict) -> dict[str, object]:
    """把服务的 environment 规整成 dict（兼容 `KEY=VALUE` 列表写法）。"""
    env = svc.get("environment") or {}
    if isinstance(env, dict):
        return env
    result: dict[str, object] = {}
    for item in env:
        key, _, value = str(item).partition("=")
        result[key] = value
    return result


def volume_mount(entry: object) -> tuple[str, str] | None:
    """取一条 volumes 条目的 (source, target)；匿名卷/无法解析时返回 None。

    短语法 `source:target[:mode]`、长语法 `{type, source, target}` 都支持。
    """
    if isinstance(entry, dict):
        source = entry.get("source")
        target = entry.get("target")
        if source is None or target is None:
            return None
        return str(source).strip(), str(target).strip()
    # 短语法是 `SOURCE:TARGET[:MODE]`：target 是**第二段**，最后一段是模式
    # （`ro`/`rw`/`z`/`ro,Z`…）。取 parts[-1] 会把模式当成挂载点，
    # 于是 `/app/web:ro` 被当成挂到了 `ro`，越危险的 bind 反而越查不出来。
    #
    # 必须用 `split_outside_braces`：裸 `split(":")` 会切入 `${VAR:-/path}`，
    # 切点落在 `:-` 的冒号上，source/target 全错位，宿主 bind 就此绕过检查。
    parts = split_outside_braces(str(entry).strip())
    if len(parts) < 2:
        return None
    return parts[0].strip(), parts[1].strip()


def volume_mode(entry: object) -> str:
    """取一条 volumes 条目的挂载模式，归一到 `"rw"` / `"ro"`。

    短语法 `SOURCE:TARGET[:MODE]`：MODE 是第三段，可能带选项（`ro,Z`、`rw,z`），
    只要含 `ro` 就算只读；不写 MODE 时 Compose 的默认是读写。
    长语法 `{type, source, target, read_only}`：按 `read_only` 布尔判定。

    单列一个函数是因为"挂上了"和"以什么模式挂"是两件事：requests 挂成 ro
    时目录在，但 API 写不进去——只查 target 会把这种缺陷放过去。
    """
    if isinstance(entry, dict):
        return "ro" if entry.get("read_only") else "rw"
    parts = split_outside_braces(str(entry).strip())
    if len(parts) < 3:
        return "rw"
    options = {item.strip().lower() for item in parts[2].split(",") if item.strip()}
    return "ro" if "ro" in options else "rw"


def effective_volume_name(project: str, key: str, conf: object) -> str:
    """按 Compose 的规则算出卷的**实际名称**（`<项目>_<键>` 是默认派生）。

    - 显式 `name:` → 就是它（Compose 不再加项目前缀）；
    - `external: true` 且没有 `name:` → 用键名（外部卷由用户自己命名）；
    - 其它 → `f"{project}_{key}"`。

    为什么单独抽出来：镜像主文件断言的是**最终挂载的卷名**，而不是写法。
    只看键名推不出 `name:` 的效果，于是"卷名必须还是 ling_db-data"这条断言
    在 `name:` 变异下根本不会被触发（永远跟着顶层 `name` 走），等于没有校验。
    """
    if isinstance(conf, dict):
        name = conf.get("name")
        if name:
            return str(name).strip()
        if conf.get("external"):
            return key
    return f"{project}_{key}"


def interpolation_default(expr: str) -> str | None:
    """取 `${VAR:-默认值}` 里的默认值；没有默认值时返回 None。

    支持 Compose 的全部变量运算符：

    - `:-` / `-`：未设置（或为空）时的默认值 → 返回该默认值；
    - `:?` / `?`：未设置（或为空）时报错 → 返回该错误信息（不是路径，但仍是
      一个**确定的**字面量，可继续按路径判定）；
    - `:+` / `+`：有值时的替代值 → 静态拿不到挂载源，返回 None；
    - 裸 `${VAR}`：静态拿不到值，返回 None。

    变量名只认前导 `[A-Za-z_][A-Za-z0-9_]*`；`${VAR:-${B}}` 这类嵌套按花括号
    深度找匹配的 `}`，默认值原样返回（由调用方递归判定）。
    """
    text = expr.strip()
    if not text.startswith("${"):
        return None
    depth = 0
    end = -1
    index = 0
    while index < len(text):
        if text.startswith("${", index):
            depth += 1
            index += 2
            continue
        if text[index] == "}":
            depth -= 1
            if depth == 0:
                end = index
                break
        index += 1
    if end < 0:
        return None
    body = text[2:end]
    name = re.match(r"[A-Za-z_][A-Za-z0-9_]*", body)
    if name is None:
        return None
    rest = body[name.end() :]
    if not rest:
        return None
    for op in (":-", "-", ":?", "?", ":+", "+"):
        if rest.startswith(op):
            argument = rest[len(op) :]
            return None if op in {":+", "+"} else argument
    return None


def update_switch_off_by_default(value: object) -> bool:
    """`LING_UPDATE_ENABLED` 是否**来自插值且默认解析为 0**。

    只接受 `${LING_UPDATE_ENABLED:-0}`（以及等价写法 `${LING_UPDATE_ENABLED-0}`）：

    - 写死 `"0"`：用户改 `.env` 也打不开（本缺陷现场），FAIL；
    - 写死 `"1"`：默认值直接变成开启，FAIL；
    - 裸 `${LING_UPDATE_ENABLED}`：没有默认值，缺值时是空串，FAIL；
    - `${OTHER:-0}` 之类复用别的变量：改名换姓地绕开门禁，FAIL。
    """
    text = str(value).strip()
    return UPDATE_ENABLED_SWITCH_RE.match(text) is not None


def is_host_bind(source: str) -> bool:
    """宿主路径 bind（相对/绝对/家目录）。

    带默认值的插值（`${WEB_DIST:-/srv/dist}`）按默认值递归判定：默认值就是
    没有配 `.env` 时**真正会挂上去的路径**，拿它当"变量所以不管"正是假绿的
    来源。裸插值/无默认值不属于宿主 bind（由 `mount_source_unknown` 另行报错）。
    """
    text = source.strip()
    if text.startswith("${"):
        default = interpolation_default(text)
        return False if default is None else is_host_bind(default)
    return text.startswith((".", "/", "~"))


def mount_source_unknown(source: str) -> bool:
    """挂载源是插值且静态拿不到值（裸 `${VAR}` 或 `:+`/`+`）。

    这种写法无法判断会挂宿主路径还是命名卷——镜像主文件只允许命名卷，所以
    静态无法判定时必须报错，而不是"$ 开头就跳过"。
    """
    text = source.strip()
    if not text.startswith("${"):
        return False
    default = interpolation_default(text)
    if default is None:
        return True
    return mount_source_unknown(default)


def check_service_structure(
    data: dict, label: str, errors: list[str], warnings: list[str]
) -> None:
    """两个主文件都必须满足的通用结构：服务集/依赖/挂载引用/日志上限等。"""
    services: dict = data.get("services") or {}
    volumes = set(data.get("volumes") or {})
    networks = set(data.get("networks") or {})

    missing = REQUIRED_SERVICES - set(services)
    if missing:
        errors.append(f"[{label}] 缺少服务: {sorted(missing)}")

    for name, svc in services.items():
        dep = svc.get("depends_on") or {}
        for d in dep.keys() if isinstance(dep, dict) else dep:
            if d not in services:
                errors.append(f"[{label}] {name}.depends_on 引用了不存在的服务 {d}")
        for vol in svc.get("volumes") or []:
            mount = volume_mount(vol)
            if mount is None:
                continue
            src = mount[0]
            if not is_host_bind(src) and "$" not in src and src not in volumes:
                errors.append(f"[{label}] {name}.volumes 引用了未声明的卷 {src}")
        for net in svc.get("networks") or []:
            if net not in networks:
                errors.append(f"[{label}] {name}.networks 引用了未声明的网络 {net}")
        if svc.get("security_opt") is None:
            warnings.append(f"[{label}] {name} 未设置 security_opt（建议 no-new-privileges）")
        if name == "db" and svc.get("ports"):
            errors.append(f"[{label}] db 不应暴露端口（设计文档 §11）")
        if name in {"api", "db"} and svc.get("healthcheck") is None:
            errors.append(f"[{label}] {name} 必须有 healthcheck（启动顺序依赖它）")

    api = services.get("api") or {}
    dep = api.get("depends_on") or {}
    if isinstance(dep, dict) and (dep.get("db") or {}).get("condition") != "service_healthy":
        errors.append(f"[{label}] api 必须等 db 健康后再启动")

    # 容器内固定端口三处一致：entrypoint 的 uvicorn 默认值 + healthcheck URL
    # （entrypoint 的检查在 check_required_files 里，两个主文件共用）。
    api_healthcheck = str((api.get("healthcheck") or {}).get("test") or "")
    if "127.0.0.1:6520/readyz" not in api_healthcheck:
        errors.append(f"[{label}] api 的 healthcheck 必须探 127.0.0.1:6520/readyz")

    worker = services.get("worker") or {}
    if not worker.get("command"):
        errors.append(f"[{label}] worker 缺少 command")
    if worker.get("ports"):
        errors.append(f"[{label}] worker 不应暴露端口")

    # 不能再有 edge：留着它等于 nginx 又变成一个要维护的 compose 服务，
    # 而它的配置/证书会跟着 release 树漂。
    if "edge" in services:
        errors.append(f"[{label}] edge 服务已被删除（api 直连），不要把它加回来")

    # --- 长期运行约束 ---
    # 个人服务器长期无人值守，容器日志必须有上限，否则会写满磁盘
    # （磁盘满后数据库直接不可写，而且很难第一时间想到是日志导致的）。
    for name, svc in services.items():
        logging_conf = svc.get("logging")
        if not logging_conf:
            errors.append(
                f"[{label}] 服务 {name} 未配置 logging 上限（长时间运行会撑满磁盘）"
            )
        else:
            opts = (logging_conf.get("options") or {}) if isinstance(logging_conf, dict) else {}
            if not opts.get("max-size"):
                errors.append(f"[{label}] 服务 {name} 的 logging 缺少 max-size")


def check_network_policy(data: dict, label: str, errors: list[str]) -> None:
    """网络：egress 必须是普通 bridge（不能 internal），api/worker 两边都挂。"""
    services: dict = data.get("services") or {}
    networks = set(data.get("networks") or {})

    egress = (data.get("networks") or {}).get("egress")
    if egress is not None and isinstance(egress, dict) and egress.get("internal"):
        errors.append(
            f"[{label}] egress 不能是 internal：api/worker 靠它出网访问 OneBot/embedding/LLM"
        )
    if "egress" not in networks:
        errors.append(f"[{label}] 缺少 egress 网络：api/worker 需要出网口")
    if "internal" not in networks:
        errors.append(f"[{label}] 缺少 internal 网络：db 只应在内网可见")
    for name in ("api", "worker"):
        svc_nets = services.get(name, {}).get("networks") or []
        if "egress" not in svc_nets:
            errors.append(f"[{label}] {name} 必须挂 egress（否则失去外网访问）")
        if "internal" not in svc_nets:
            errors.append(f"[{label}] {name} 必须挂 internal（否则连不上 db）")


def check_replicas(data: dict, label: str, errors: list[str], warnings: list[str]) -> None:
    """多副本约束（两个主文件共用）。"""
    services: dict = data.get("services") or {}
    api = services.get("api") or {}
    worker = services.get("worker") or {}

    # api 直连固定 host 端口 → compose 非 swarm 模式下 ports 只能给一个容器，
    # 默认副本数必须是 1（2 副本会端口冲突、第二个起不来）。
    replicas = (api.get("deploy") or {}).get("replicas")
    if replicas is not None and str(replicas) != "${LING_API_REPLICAS:-1}":
        errors.append(f"[{label}] api 默认副本数必须是 1（api 发布固定 host 端口），实际：{replicas}")
    replicas_value = (worker.get("deploy") or {}).get("replicas")
    if replicas_value not in {1, "1", None, "${LING_WORKER_REPLICAS:-1}"}:
        warnings.append(
            f"[{label}] worker 副本数非常量：确认 handler 幂等"
            "（唯一约束已就位，见 migration 0008/0009）"
        )


def check_api_expose(api: dict, label: str, errors: list[str]) -> list[str]:
    """api 的 `expose` 必须声明 6520，返回 expose 列表。

    三处固定 6520：entrypoint 的 uvicorn、healthcheck、expose（compose 文件
    注释里写明了）。expose 缺失时 `docker compose config` 不会报错，只是对外
    少声明一个容器内端口；把它当 warning 等于"删掉 expose 照样 PASS"。
    """
    expose = [str(item) for item in (api.get("expose") or [])]
    if "6520" not in expose:
        errors.append(
            f"[{label}] api 必须声明 expose: 6520（与 entrypoint/healthcheck 的容器内端口"
            f"一致），实际 {expose or '未声明 expose'}"
        )
    return expose


def check_source_ports(
    data: dict, env_text: str, label: str, errors: list[str]
) -> None:
    """源码模式：api 发布 host 端口，容器侧固定 6520；TLS 与 cookie Secure 耦合。"""
    services: dict = data.get("services") or {}
    api = services.get("api") or {}

    api_ports = [str(item) for item in (api.get("ports") or [])]
    api_container_ports = [
        port for port in (container_side_port(item) for item in (api.get("ports") or [])) if port
    ]
    if not api_ports:
        errors.append(
            f"[{label}] api 必须发布 host 端口（api 是唯一入口）：ports 里应有 "
            "`${LING_PORT:-6520}:6520`"
        )
    elif "6520" not in api_container_ports:
        errors.append(
            f"[{label}] api 的 ports 必须在**容器侧**发布 6520（容器内固定端口），"
            f"实际容器侧：{api_container_ports or api_ports}（宿主侧是 LING_PORT，不参与判断）"
        )

    # 用 TLS 配置就必须开 cookie Secure，否则登录凭证仍可能走明文。
    if any(":443" in item for item in api_ports):
        if "LING_COOKIE_SECURE" not in env_text:
            errors.append(
                f"[{label}] api 发布了 443，但 env.example 没有 LING_COOKIE_SECURE："
                "上 TLS 却不开 cookie Secure 等于白上"
            )
        nginx_tls = ROOT / "deploy" / "nginx-tls.conf"
        if not nginx_tls.exists():
            errors.append(f"[{label}] api 发布了 443，但缺少 nginx-tls.conf 示例")
        else:
            conf = nginx_tls.read_text(encoding="utf-8")
            if "proxy_buffering off" not in conf:
                errors.append(f"[{label}] nginx-tls.conf 未关闭 proxy_buffering：SSE 会被缓冲")
            if "X-Forwarded-Proto" not in conf:
                errors.append(f"[{label}] nginx-tls.conf 未设置 X-Forwarded-Proto")

    # expose 与 ports 的**容器侧**必须一致，都是 6520：一个写 6520、另一个写
    # 别的，等于对外声明的端口和容器真正监听的端口对不上。expose 缺失同样是
    # 错误（不是 warning）——它和 entrypoint/healthcheck 一样是硬约定。
    api_expose = check_api_expose(api, label, errors)
    if "6520" in api_expose and api_ports and "6520" not in api_container_ports:
        # ports 那边已经报过错；这里只补一句"两边不一致"，避免只改一处。
        errors.append(
            f"[{label}] api 的 expose 是 6520，但 ports 的容器侧不是 6520"
            f"（{api_container_ports or api_ports}）：两边必须一致"
        )


def check_image_structure(data: dict, text: str, label: str, errors: list[str]) -> None:
    """镜像模式主文件的结构断言（设计 §4 / §7 第 2 步）。

    `text` 是原始 YAML 文本，用来看变量引用里有没有源码模式残留。
    """
    services: dict = data.get("services") or {}

    # --- 服务集与项目名 ---
    if set(services) != REQUIRED_SERVICES:
        errors.append(
            f"[{label}] 服务集必须恰为 {sorted(REQUIRED_SERVICES)}，"
            f"实际 {sorted(services)}（多/少都会让备份、发布与健康门禁对不上）"
        )
    if data.get("name") != PROJECT_NAME:
        errors.append(
            f"[{label}] 顶层 name 必须是 {PROJECT_NAME}（决定卷的实际名称与项目归属），"
            f"实际 {data.get('name')!r}"
        )

    # --- 卷：键名与**实际名称** ---
    volumes: dict = data.get("volumes") or {}
    if set(volumes) != REQUIRED_VOLUME_KEYS:
        errors.append(
            f"[{label}] 卷键必须恰为 {sorted(REQUIRED_VOLUME_KEYS)}，实际 {sorted(volumes)}"
        )
    # `external:` 单独断言：它意味着"卷不由本项目管理"，备份/恢复按项目卷名
    # 工作，挂一个外部卷会让备份悄悄指向别的卷。这是另一件事，与下面的
    # "实际名称"断言分开报，免得两条错误互相掩护。
    for key in sorted(set(volumes) & REQUIRED_VOLUME_KEYS):
        conf = volumes[key] or {}
        if isinstance(conf, dict) and conf.get("external"):
            errors.append(
                f"[{label}] 卷 {key} 不能指定 external:："
                "本项目的卷必须由 compose 自己管理（备份/恢复按项目卷名工作）"
            )
    # 实际名称：逐个键算出来再和 `f"{PROJECT_NAME}_{key}"` 比，**只报一条**。
    # 这样 `name: ling-db-data` 这种变异只产生"卷名不对"这一条错误，可以独立
    # 证明这条断言有效（改前它是跟着顶层 name 走的，`name:` 变异只有另一条
    # "不能指定 name:/external:"，卷名断言本身永远不会单独失败）。
    #
    # 取舍：显式写 `name: ling_db-data`（最终名与派生名一致）现在会 PASS。
    # 实际挂的还是同一个卷，多写一行只是啰嗦；这里断言的是**最终名**，不是写法。
    project = str(data.get("name") or PROJECT_NAME)
    renamed: list[str] = []
    for key in sorted(REQUIRED_VOLUME_KEYS):
        actual = effective_volume_name(project, key, volumes.get(key) or {})
        if actual != f"{PROJECT_NAME}_{key}":
            renamed.append(f"{key} → {actual}")
    if renamed:
        errors.append(
            f"[{label}] 卷的实际名称必须仍是 {PROJECT_NAME}_<键>（不允许用 name:/"
            f"external: 改掉，否则会挂到另一个卷上，历史数据在旧卷里）："
            f"{'，'.join(renamed)}"
        )

    # --- 镜像引用：api/worker 同一条必填插值 ---
    api = services.get("api") or {}
    worker = services.get("worker") or {}
    api_image = api.get("image")
    worker_image = worker.get("image")
    if not api_image or not worker_image:
        errors.append(f"[{label}] api/worker 都必须写 image:（镜像模式不允许 build）")
    else:
        if str(api_image) != str(worker_image):
            errors.append(
                f"[{label}] api 与 worker 的 image 必须完全相同（同一镜像摘要），"
                f"实际 api={api_image!r} worker={worker_image!r}"
            )
        if not IMAGE_APP_REQUIRED.match(str(api_image)):
            errors.append(
                f"[{label}] image 必须是必填插值 ${{LING_APP_IMAGE:?...}}"
                f"（缺值让 `docker compose config` 直接失败），实际 {api_image!r}"
            )

    for name, svc in services.items():
        if "build" in svc:
            errors.append(
                f"[{label}] {name} 不允许 build:（镜像模式只认固定摘要 LING_APP_IMAGE）"
            )

    # --- 挂载：无 /app/web、无 Docker socket、无宿主 bind ---
    # --- 更新通道：只允许 api 的两个**精确**挂载（见文件头 UPDATE_MOUNTS）---
    #
    # P1 时这里是"一律禁止 /run/ling-update*"，前提是"初期不挂 update 通道"。
    # P5 已经把 API 的写请求/读状态端点做完，不挂这两个目录 = POST 在真实部署里
    # 必然失败。设计要求「API 仅获得写请求/读状态的最小目录权限」，所以改成
    # 精确断言（而不是删掉这条检查）：worker 一条都不许挂，api 只许这两个目标、
    # 模式必须对、源必须是必填插值。
    api_update_mounts: dict[str, tuple[str, str]] = {}
    for name, svc in services.items():
        for vol in svc.get("volumes") or []:
            mount = volume_mount(vol)
            if mount is None:
                continue
            source, target = mount
            is_update_target = target == UPDATE_TARGET_PREFIX or target.startswith(
                f"{UPDATE_TARGET_PREFIX}/"
            )
            if is_update_target:
                if name != "api":
                    errors.append(
                        f"[{label}] {name} 不允许挂更新通道目录（{target}）："
                        "只有 api 用共享组写请求/读状态；worker 挂上等于后台任务能触发部署"
                    )
                    continue
                expected = UPDATE_MOUNTS.get(target)
                if expected is None:
                    errors.append(
                        f"[{label}] api 只能挂 {sorted(UPDATE_MOUNTS)} 两个更新通道目录，"
                        f"实际还挂了 {target}：多给一个目录就是多一份写面"
                    )
                    continue
                mode = volume_mode(vol)
                api_update_mounts[target] = (source, mode)
                if not REQUIRED_INTERPOLATION_RE.match(source):
                    errors.append(
                        f"[{label}] api 的 {target} 挂载源必须是**必填插值** "
                        f"${{VAR:?...}}（缺值让 `docker compose config` 直接失败），"
                        f"实际 {source!r}：固定路径会让镜像模式挂宿主目录，"
                        "可选默认值则会静默少挂一条"
                    )
                if mode != expected:
                    errors.append(
                        f"[{label}] api 的 {target} 必须以 {expected} 挂载，实际 {mode}："
                        + (
                            "requests 只读等于 API 写不了请求（POST 必然失败）"
                            if expected == "rw"
                            else "status 可写等于 API 能伪造更新器状态"
                        )
                    )
                continue
            if mount_source_unknown(source):
                errors.append(
                    f"[{label}] {name} 的挂载源 {source} 是插值且没有默认值："
                    "静态无法判定是否宿主路径，而镜像主文件只允许命名卷"
                )
            if target == "/app/web" or target.startswith("/app/web/"):
                errors.append(
                    f"[{label}] {name} 不允许把 {source} 挂到 {target}："
                    "Web 产物已烤进镜像，源码模式的前端 bind 会让镜像内容被遮蔽"
                )
            if "docker.sock" in source:
                errors.append(
                    f"[{label}] {name} 不允许挂载 Docker socket（{source}）："
                    "容器获得宿主 Docker 控制权等于拿到 root"
                )
            if is_host_bind(source):
                errors.append(
                    f"[{label}] {name} 不应有宿主 bind 挂载（{source}:{target}）："
                    "镜像模式只保留命名卷，需要额外挂载时放 override"
                )

    # 两个目录都必须挂上：少一个不是"更安全"，而是对应端点必然失败
    # （status 缺失 → GET 一直报 updater 状态 unknown/409；requests 缺失 → POST 失败）。
    for target, expected_mode in UPDATE_MOUNTS.items():
        if target not in api_update_mounts:
            errors.append(
                f"[{label}] api 必须挂更新通道目录 {target}（{expected_mode}）："
                "P5 接线后 API 要写请求/读更新器状态，缺了这条网页更新在真实部署里必然失败"
            )

    # 共享组：requests 目录的属组是更新器 gid（安装器设置），API 只有加入该组
    # 才写得进去。写成固定值或去掉 group_add 都会让权限悄悄失效（EACCES）。
    group_add = [str(item).strip() for item in (api.get("group_add") or [])]
    shared_group_re = re.compile(
        r"^\$\{" + re.escape(UPDATE_SHARED_GID_VAR) + r":\?[^}]*\}$"
    )
    if len(group_add) != 1 or not shared_group_re.match(group_add[0]):
        errors.append(
            f"[{label}] api 必须恰好有一条 group_add: "
            f"${{{UPDATE_SHARED_GID_VAR}:?...}}（共享组：requests/status 目录的属组），"
            f"实际 {group_add}：没有这个组，API 只能写自己 uid 的文件、"
            "读不到更新器写的状态（权限悄悄失效）"
        )

    # --- 安全：只读根 + tmpfs + 无特权 ---
    for name in ("api", "worker"):
        svc = services.get(name) or {}
        if svc.get("read_only") is not True:
            errors.append(f"[{label}] {name} 必须 read_only: true（根文件系统只读）")
        tmpfs = [str(item) for item in (svc.get("tmpfs") or [])]
        if not any(item.split(":", 1)[0].strip() == "/tmp" for item in tmpfs):
            errors.append(f"[{label}] {name} 必须挂 tmpfs: /tmp（只读根下要有可写临时目录）")
        sec_opt = [str(item) for item in (svc.get("security_opt") or [])]
        if not any(item.startswith("no-new-privileges") for item in sec_opt):
            errors.append(f"[{label}] {name} 必须有 security_opt: no-new-privileges")
        if svc.get("privileged"):
            errors.append(f"[{label}] {name} 不允许 privileged: true")

    # --- 迁移与更新通道：镜像模式的默认值 ---
    api_env = service_environment(api)
    migrations = api_env.get("LING_RUN_MIGRATIONS")
    if str(migrations) != "0":
        errors.append(
            f"[{label}] api 必须显式 LING_RUN_MIGRATIONS=0（镜像模式启动时不隐式迁移；"
            f"单次迁移用 run -e LING_RUN_MIGRATIONS=1），实际 {migrations!r}"
        )
    update_enabled = api_env.get("LING_UPDATE_ENABLED")
    # 开关必须**来自插值且默认解析为 0**：`LING_UPDATE_ENABLED=0` 是写死的
    # 字面量，用户改 `--env-file` 里的 `.env` 也没用（缺陷现场：开关打不开、
    # 只能去安装目录手改 compose）。`"1"` 则把默认值变成开启。两者都必须 FAIL。
    if not update_switch_off_by_default(update_enabled):
        errors.append(
            f"[{label}] api 的 LING_UPDATE_ENABLED 必须写成插值 "
            f"${{LING_UPDATE_ENABLED:-0}}（来自 --env-file，默认解析为 0），"
            f"实际 {update_enabled!r}：字面量 \"0\" 用户改 .env 也打不开，"
            "字面量 \"1\" 会把默认值变成开启"
        )
    worker_env = service_environment(worker)
    if "LING_UPDATE_ENABLED" in worker_env and str(worker_env["LING_UPDATE_ENABLED"]) != "0":
        errors.append(
            f"[{label}] worker 不得启用更新通道，实际 {worker_env['LING_UPDATE_ENABLED']!r}"
        )

    # --- 发布端口：只发 api 的 LING_PORT:6520 ---
    api_ports = api.get("ports") or []
    if len(api_ports) != 1:
        errors.append(
            f"[{label}] api 只应有 1 条发布端口（WS 端口走 override 叠加），"
            f"实际 {len(api_ports)} 条"
        )
    elif container_side_port(api_ports[0]) != "6520":
        errors.append(
            f"[{label}] api 的 ports 容器侧必须是 6520（容器内固定端口），"
            f"实际 {container_side_port(api_ports[0])!r}"
        )
    else:
        published = host_side_port(api_ports[0])
        if published not in {"${LING_PORT:-6520}", "6520"}:
            errors.append(
                f"[{label}] api 的宿主侧端口必须由 LING_PORT 控制（默认 6520），"
                f"实际 {published!r}"
            )

    check_api_expose(api, label, errors)

    # --- 源码模式残留：这些变量只对源码构建/前端 bind 有意义 ---
    leftover = sorted(SOURCE_ONLY_VARS & set(REFERENCE_RE.findall(text)))
    if leftover:
        errors.append(
            f"[{label}] 镜像主文件不应引用源码模式变量 {leftover}："
            "构建参数与前端 bind 只属于 compose.yaml"
        )


def check_app_env_parity(
    source: dict,
    image: dict,
    source_label: str,
    image_label: str,
    errors: list[str],
) -> None:
    """两份主文件的 `x-app-env` 必须逐条相同（变量名 + 值的类型与字面量）。

    `x-app-env` 是**逐项白名单**：锚点 `&app-env` 里的每一行就是容器真正会收到
    的变量。三种偏差都必须是错误，而不是"结构校验照样通过"：

    - 源码模式有、镜像模式没有（`LING_MAX_BODY_BYTES`、`LING_ONEBOT_WS_TOKEN`、
      `LING_QQBOT_ENABLED` 都实测漏过）：少一行就等于用户在 `.env` 里配了也
      进不了容器，表现为"配了却没生效"，而且日志里一句异常都没有；
    - 镜像模式多出来：镜像模式会静默多出源码模式没有的配置；
    - 共同键的值不同（含 `"1"`→`"0"`、`true`/`1` 这类类型差异）：默认行为直接
      变掉，而所有结构断言都查不出来。

    比较用 `(type(value).__name__, value)`：YAML 里 `PYTHONUNBUFFERED: "1"` 是
    字符串、`1` 是整数，`True` 与 `1` 在 Python 里相等（`True == 1`），只比
    值会把它们当成同一个。

    **只列变量名，不打印值**：值里有 `${POSTGRES_PASSWORD:-ling-dev-password}`
    这类口令引用，打印出来就进终端/CI 日志（tests 里有专门的泄漏断言）。
    """
    source_env = source.get(APP_ENV_KEY)
    image_env = image.get(APP_ENV_KEY)
    if not isinstance(source_env, dict) or not source_env:
        errors.append(f"[{source_label}] 缺少非空的 {APP_ENV_KEY}（变量白名单锚点）")
        return
    if not isinstance(image_env, dict) or not image_env:
        errors.append(f"[{image_label}] 缺少非空的 {APP_ENV_KEY}（变量白名单锚点）")
        return

    allowed = set(X_APP_ENV_DIFF_ALLOWED)
    missing = sorted(set(source_env) - set(image_env) - allowed)
    if missing:
        errors.append(
            f"[{image_label}] {APP_ENV_KEY} 比 {source_label} 少了 {missing}："
            "少一行就等于用户在 .env 里配了也进不了容器（不会有任何提示）"
        )
    extra = sorted(set(image_env) - set(source_env) - allowed)
    if extra:
        errors.append(
            f"[{image_label}] {APP_ENV_KEY} 比 {source_label} 多了 {extra}："
            "镜像模式不应有源码模式没有的变量"
        )
    changed = sorted(
        key
        for key in set(source_env) & set(image_env)
        if key not in allowed
        and (type(source_env[key]).__name__, source_env[key])
        != (type(image_env[key]).__name__, image_env[key])
    )
    if changed:
        errors.append(
            f"[{image_label}] {APP_ENV_KEY} 里这些变量的值与 {source_label} 不同："
            f"{changed}（只列变量名，不打印值）"
        )


def check_image_ws_override(override: dict, base: dict, label: str, errors: list[str]) -> None:
    """镜像模式 WS override：只允许给 api.ports 追加一条必填插值的端口。"""
    services = override.get("services") or {}
    if set(services) != {"api"}:
        errors.append(f"[{label}] 只能覆盖 api 服务（只加端口），实际 {sorted(services)}")
    api = services.get("api") or {}
    extra = sorted(set(api) - {"ports"})
    if extra:
        errors.append(
            f"[{label}] 只允许出现 api.ports，不允许 {extra}：镜像/挂载/构建方式属于主文件"
        )
    ports = api.get("ports") or []
    if len(ports) != 1:
        errors.append(f"[{label}] 必须且只能追加 1 条 WS 端口，实际 {len(ports)} 条")
        return
    entry = str(ports[0]).strip()
    container = container_side_port(ports[0])
    published = host_side_port(ports[0])
    for side, value in (("容器侧", container), ("宿主侧", published)):
        if not value or not value.startswith("${LING_ONEBOT_WS_PORT:?"):
            errors.append(
                f"[{label}] WS 端口{side}必须是必填插值 ${{LING_ONEBOT_WS_PORT:?...}}"
                f"（漏配时 config 直接失败），实际 {value!r}"
            )
    if container != published:
        errors.append(
            f"[{label}] WS 端口两侧必须是同一个变量（宿主机:容器内同值），"
            f"实际 {container!r} vs {published!r}"
        )
    if "LING_PORT" in entry:
        errors.append(
            f"[{label}] override 不应再发布 LING_PORT：主文件已经发了，重复会端口冲突"
        )
    # 叠加语义：主文件 api 的发布端口必须仍在（Compose 的 ports 是追加而非覆盖）。
    base_ports = ((base.get("services") or {}).get("api") or {}).get("ports") or []
    if not base_ports:
        errors.append(f"[{label}] 主文件 api 没有发布端口，override 无法叠加出完整入口")
    elif any(container_side_port(item) == container_side_port(ports[0]) for item in base_ports):
        errors.append(f"[{label}] WS 端口与主文件已发布的端口重复")


def check_duplicate_keys(text: str, label: str, errors: list[str]) -> None:
    """YAML 重复键（PyYAML 静默接受，docker compose 直接报错）。

    实测踩过：x-app-env 里 LING_COOKIE_SECURE 写了两遍，本地 yaml.safe_load
    毫无异常，但用户第一次 `docker compose up` 就会
    "mapping key already defined" 起不来。
    用 yaml.compose 只走节点树（不构造对象，因此不受锚点影响）。
    """
    try:
        root = yaml.compose(text)
    except yaml.YAMLError as exc:
        errors.append(f"[{label}] 无法解析: {type(exc).__name__}")
        return

    def scan(node: object) -> None:
        if isinstance(node, yaml.MappingNode):
            seen: set[object] = set()
            for key_node, value_node in node.value:
                key = getattr(key_node, "value", None)
                if key in seen:
                    errors.append(
                        f"[{label}] 第 {key_node.start_mark.line + 1} 行："
                        f"重复的键 {key!r}（docker compose 会拒绝启动）"
                    )
                seen.add(key)
                scan(value_node)
        elif isinstance(node, yaml.SequenceNode):
            for item in node.value:
                scan(item)

    scan(root)


def check_documented_vars(
    text: str, label: str, documented: set[str], warnings: list[str]
) -> set[str]:
    """compose 里引用的 `${VAR}` 必须在 env.example 里有说明（注释态也算）。"""
    referenced = set(REFERENCE_RE.findall(text))
    undocumented = sorted(referenced - documented)
    if undocumented:
        warnings.append(f"[{label}] env.example 未说明的变量: {undocumented}")
    return referenced


def check_required_files(errors: list[str]) -> None:
    """两个主文件共用的外部依赖：Dockerfile 非 root、entrypoint 端口 6520。"""
    dockerfile_path = ROOT / "deploy" / "Dockerfile.api"
    if not dockerfile_path.exists():
        errors.append("缺少 deploy/Dockerfile.api")
    elif "USER " not in dockerfile_path.read_text(encoding="utf-8"):
        errors.append("Dockerfile.api 必须以非 root 用户运行")

    entrypoint = (ROOT / "deploy" / "entrypoint.sh").read_text(encoding="utf-8")
    if '"${LING_PORT:-6520}"' not in entrypoint:
        errors.append("entrypoint.sh 的 uvicorn 端口默认值必须是 6520")


def _resolve_compose_path(raw: str) -> pathlib.Path:
    """把命令行给的文件名解析成路径：绝对路径原样，相对路径按 `ROOT`。

    按 `ROOT` 而不是当前工作目录：脚本既能在仓库根跑，也能从
    `deploy/update/` 之类的目录跑；测试还会 monkeypatch `ROOT` 指向临时副本。
    """
    path = pathlib.Path(raw).expanduser()
    return path if path.is_absolute() else ROOT / path


def _compose_label(path: pathlib.Path) -> str:
    """输出用的文件名：在 `ROOT` 内就显示相对路径，否则显示绝对路径。"""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def check_compose_overlay(paths: list[str]) -> int:
    """`--check-override-pair`：这组 `-f` 是不是把两份**主文件**叠起来了。

    设计 §4 要求源码模式与镜像模式**择一**。把 `compose.image.yaml` 当成
    override 叠到 `compose.yaml` 上不是"二选一"：后一份会把 `build:`、
    `/app/web` bind、更新通道 bind 一起带回来（实测叠加后 api 同时有 build
    与镜像摘要），镜像模式的契约全部落空。

    两条规则：

    1. 解析后（按真实路径去重，同一个文件传两次不算）已知主文件集合同时含
       `compose.yaml` 与 `compose.image.yaml` → error；
    2. 兜底防改名副本（`cp compose.image.yaml my-stack.yaml`）：某文件某服务
       有 `build:`，另一文件的**同名服务** `image` 恰好是必填插值
       `${LING_APP_IMAGE:?...}` → 这就是镜像主文件叠在源码主文件上。

    **刻意不做**：`build:` + 普通 `image: mytag` 不报错。那是合法的 tag
    override（源码构建、换个名字/标签），compose 允许，也不会把镜像模式的
    契约偷偷带进来。

    返回退出码：0 通过，1 有错误。
    """
    errors: list[str] = []
    print("=== compose 叠加（override）对校验 ===")
    if len(paths) < 2:
        errors.append(
            "--check-override-pair 至少需要两个文件（主文件 + override）："
            "只给一个文件无法判断是否叠加"
        )

    known = {
        (ROOT / SOURCE_COMPOSE).resolve(): "compose.yaml",
        (ROOT / IMAGE_COMPOSE).resolve(): "compose.image.yaml",
    }
    resolved: list[tuple[pathlib.Path, str, dict]] = []
    seen: set[pathlib.Path] = set()
    main_files: set[str] = set()
    for raw in paths:
        path = _resolve_compose_path(raw)
        label = _compose_label(path)
        real = path.resolve()
        if real in seen:
            continue
        seen.add(real)
        if not path.exists():
            errors.append(f"[{label}] 文件不存在：要叠加的文件必须真的在磁盘上")
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            errors.append(f"[{label}] 无法解析 YAML: {type(exc).__name__}")
            continue
        except OSError as exc:
            errors.append(f"[{label}] 无法读取: {type(exc).__name__}")
            continue
        if not isinstance(data, dict):
            errors.append(f"[{label}] 顶层不是映射：这不是一个 compose 文件")
            continue
        resolved.append((real, label, data))
        if real in known:
            main_files.add(known[real])

    if main_files == {"compose.yaml", "compose.image.yaml"}:
        errors.append(
            "禁止把 compose.yaml 与 compose.image.yaml 叠在一起：两份主文件是"
            "**择一**关系（设计 §4），叠加会同时带回 build:、/app/web bind 与"
            "更新通道 bind，镜像摘要也就失去意义"
        )
    else:
        # 兜底：改名副本就认不出"已知主文件"了，只能看内容。规则 1 已经命中时
        # 不再重复报一遍（同一条错误说三遍只会淹没真正的信息）。
        for real_a, label_a, data_a in resolved:
            for real_b, label_b, data_b in resolved:
                if real_a == real_b:
                    continue
                services_b = data_b.get("services") or {}
                for name, svc_a in (data_a.get("services") or {}).items():
                    if "build" not in (svc_a or {}):
                        continue
                    other_image = (services_b.get(name) or {}).get("image")
                    if IMAGE_APP_REQUIRED.match(str(other_image or "")):
                        errors.append(
                            f"[{label_b}] 的 {name} 是镜像模式主文件"
                            f"（image 是必填插值 ${{LING_APP_IMAGE:?...}}），"
                            f"却叠加在带 build: 的 {label_a} 上："
                            "禁止叠加主文件（设计 §4，择一）"
                        )

    for e in errors:
        print("  ✗", e)
    if errors:
        print("结果: FAIL")
        return 1
    print(f"结果: PASS（{len(resolved)} 个文件，未发现主文件叠加）")
    return 0


def main(argv: list[str] | None = None) -> int:
    """入口。`argv` 只用于 `--check-override-pair`；不传就是纯结构校验。

    **绝不能**默认成 `sys.argv[1:]`：测试直接调用 `main()`，那样会把 pytest 的
    参数（`-q`、`tests/unit/...`）当成交付的文件名，现有用例会集体变红。
    """
    args = list(argv or [])
    if args:
        if args[0] == "--check-override-pair":
            return check_compose_overlay(args[1:])
        print(f"未知参数: {args!r}（只支持 --check-override-pair FILE [FILE ...]）")
        return 2

    errors: list[str] = []
    warnings: list[str] = []

    env_text = (ROOT / ENV_EXAMPLE).read_text(encoding="utf-8")
    documented = set(ENV_ASSIGN_RE.findall(env_text))

    print("=== compose 静态校验 ===")

    # ---------------------------------------------------------------- 源码模式
    source_label = "compose.yaml"
    source_text = (ROOT / SOURCE_COMPOSE).read_text(encoding="utf-8")
    source = yaml.safe_load(source_text)
    check_service_structure(source, source_label, errors, warnings)
    check_network_policy(source, source_label, errors)
    check_replicas(source, source_label, errors, warnings)
    check_source_ports(source, env_text, source_label, errors)
    check_duplicate_keys(source_text, source_label, errors)
    source_referenced = check_documented_vars(source_text, source_label, documented, warnings)
    check_required_files(errors)

    print(f"[{source_label}] 服务: {sorted(source.get('services') or {})}")
    print(
        f"[{source_label}] 卷: {sorted(source.get('volumes') or {})}  "
        f"网络: {sorted(source.get('networks') or {})}"
    )
    print(f"[{source_label}] 引用变量 {len(source_referenced)} 个")

    # ---------------------------------------------------------------- 镜像模式
    image_label = "compose.image.yaml"
    image_path = ROOT / IMAGE_COMPOSE
    image: dict = {}
    if not image_path.exists():
        errors.append(
            "缺少 deploy/compose.image.yaml：镜像模式主文件必须存在"
            "（与 deploy/compose.yaml 择一，禁止叠加）"
        )
    else:
        image_text = image_path.read_text(encoding="utf-8")
        image = yaml.safe_load(image_text)
        check_service_structure(image, image_label, errors, warnings)
        check_network_policy(image, image_label, errors)
        check_replicas(image, image_label, errors, warnings)
        check_image_structure(image, image_text, image_label, errors)
        check_duplicate_keys(image_text, image_label, errors)
        image_referenced = check_documented_vars(image_text, image_label, documented, warnings)
        images = {
            name: str((image.get("services") or {}).get(name, {}).get("image"))
            for name in ("api", "worker")
        }
        print(f"[{image_label}] 服务: {sorted(image.get('services') or {})}")
        print(
            f"[{image_label}] 卷: {sorted(image.get('volumes') or {})}  "
            f"网络: {sorted(image.get('networks') or {})}"
        )
        print(f"[{image_label}] 引用变量 {len(image_referenced)} 个")
        print(f"[{image_label}] api/worker 镜像是否同一插值: {images['api'] == images['worker']}")
        api_ports = ((image.get("services") or {}).get("api") or {}).get("ports") or []
        print(f"[{image_label}] api 发布端口: {[str(item) for item in api_ports]}")
        # 更新通道摘要（P5 接线）：只放目标与模式，不放宿主路径（路径来自 .env）。
        api_update: list[str] = []
        for vol in ((image.get("services") or {}).get("api") or {}).get("volumes") or []:
            mount = volume_mount(vol)
            if mount is None:
                continue
            if mount[1] == UPDATE_TARGET_PREFIX or mount[1].startswith(
                f"{UPDATE_TARGET_PREFIX}/"
            ):
                api_update.append(f"{mount[1]}({volume_mode(vol)})")
        print(f"[{image_label}] api 更新通道挂载: {sorted(api_update)}")
        # x-app-env 是逐项白名单：两份主文件必须逐条相同（FG-3）。
        # 摘要里只放变量个数与布尔，不打印任何值（值里含口令引用）。
        app_env_errors: list[str] = []
        check_app_env_parity(source, image, source_label, image_label, app_env_errors)
        errors.extend(app_env_errors)
        image_app_env = image.get(APP_ENV_KEY)
        app_env_count = len(image_app_env) if isinstance(image_app_env, dict) else 0
        print(
            f"[{image_label}] {APP_ENV_KEY}: {app_env_count} 个变量，"
            f"与 {source_label} 逐条相同: {not app_env_errors}"
        )

    # ------------------------------------------------- 镜像模式 WS override
    ws_label = "compose.image.ws.example.yaml"
    ws_path = ROOT / IMAGE_WS_OVERRIDE
    if not ws_path.exists():
        errors.append(
            "缺少 deploy/compose.image.ws.example.yaml：镜像模式的反向 WS override 必须存在"
        )
    else:
        ws_text = ws_path.read_text(encoding="utf-8")
        try:
            ws = yaml.safe_load(ws_text) or {}
        except yaml.YAMLError as exc:
            errors.append(f"[{ws_label}] 无法解析: {type(exc).__name__}")
        else:
            check_image_ws_override(ws, image, ws_label, errors)
            check_duplicate_keys(ws_text, ws_label, errors)
            check_documented_vars(ws_text, ws_label, documented, warnings)
            ws_ports = [
                str(item)
                for item in ((ws.get("services") or {}).get("api") or {}).get("ports") or []
            ]
            print(f"[{ws_label}] 追加端口: {ws_ports}")

    for e in errors:
        print("  ✗", e)
    for w in warnings:
        print("  !", w)
    if errors:
        print("结果: FAIL")
        return 1
    print("结果: PASS（仅结构校验，未启动容器；未在真实 docker compose 上验证）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
