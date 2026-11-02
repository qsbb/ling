#!/usr/bin/env python3
"""Compose 静态校验：不启动容器，只检查配置结构与安全基线。

用法：python scripts/check_compose.py
退出码：0 通过，1 有错误。

**这不能替代实际 `docker compose up`**：镜像构建、健康检查、迁移时序、
网络与卷挂载都需要真实 Docker 才能验证，无 Docker 时由本脚本跳过。
"""

from __future__ import annotations

import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


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
    """
    if isinstance(entry, dict):
        target = entry.get("target")
        return None if target is None else str(target).strip()
    text = str(entry).strip()
    # 去掉协议后缀：`8080:6520/tcp` → `8080:6520`
    text = text.split("/", 1)[0]
    # 容器侧 = 最后一段：`6520` / `8080:6520` / `127.0.0.1:8080:6520` / `[::1]:8080:6520`
    return text.rsplit(":", 1)[-1].strip()


def main() -> int:
    compose_path = ROOT / "deploy" / "compose.yaml"
    data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    warnings: list[str] = []

    services: dict = data.get("services") or {}
    volumes = set(data.get("volumes") or {})
    networks = set(data.get("networks") or {})

    missing = {"db", "api", "worker"} - set(services)
    if missing:
        errors.append(f"缺少服务: {sorted(missing)}")

    for name, svc in services.items():
        dep = svc.get("depends_on") or {}
        for d in (dep.keys() if isinstance(dep, dict) else dep):
            if d not in services:
                errors.append(f"{name}.depends_on 引用了不存在的服务 {d}")
        for vol in svc.get("volumes") or []:
            src = str(vol).split(":")[0]
            if not src.startswith((".", "/", "$")) and src not in volumes:
                errors.append(f"{name}.volumes 引用了未声明的卷 {src}")
        for net in svc.get("networks") or []:
            if net not in networks:
                errors.append(f"{name}.networks 引用了未声明的网络 {net}")
        if svc.get("security_opt") is None:
            warnings.append(f"{name} 未设置 security_opt（建议 no-new-privileges）")
        if name == "db" and svc.get("ports"):
            errors.append("db 不应暴露端口（设计文档 §11）")
        if name in {"api", "db"} and svc.get("healthcheck") is None:
            errors.append(f"{name} 必须有 healthcheck（启动顺序依赖它）")

    api = services.get("api") or {}
    dep = api.get("depends_on") or {}
    if isinstance(dep, dict) and (dep.get("db") or {}).get("condition") != "service_healthy":
        errors.append("api 必须等 db 健康后再启动")

    worker = services.get("worker") or {}
    if not worker.get("command"):
        errors.append("worker 缺少 command")
    if worker.get("ports"):
        errors.append("worker 不应暴露端口")

    # 变量文档一致性
    env_example = (ROOT / "deploy" / "env.example").read_text(encoding="utf-8")
    referenced = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", compose_path.read_text(encoding="utf-8")))
    documented = set(re.findall(r"^([A-Z_][A-Z0-9_]*)=", env_example, re.M))
    undocumented = sorted(referenced - documented)
    if undocumented:
        warnings.append(f"env.example 未说明的变量: {undocumented}")

    # Dockerfile 必须非 root
    dockerfile = (ROOT / "deploy" / "Dockerfile.api").read_text(encoding="utf-8")
    if "USER " not in dockerfile:
        errors.append("Dockerfile.api 必须以非 root 用户运行")

    # --- 长期运行约束 ---
    # 个人服务器长期无人值守，容器日志必须有上限，否则会写满磁盘
    # （磁盘满后数据库直接不可写，而且很难第一时间想到是日志导致的）。
    for name, svc in services.items():
        logging_conf = svc.get("logging")
        if not logging_conf:
            errors.append(f"服务 {name} 未配置 logging 上限（长时间运行会撑满磁盘）")
        else:
            opts = (logging_conf.get("options") or {}) if isinstance(logging_conf, dict) else {}
            if not opts.get("max-size"):
                errors.append(f"服务 {name} 的 logging 缺少 max-size")

    # --- YAML 重复键（PyYAML 静默接受，docker compose 直接报错）---
    # 实测踩过：x-app-env 里 LING_COOKIE_SECURE 写了两遍，
    # 本地 yaml.safe_load 毫无异常，但用户第一次 `docker compose up`
    # 就会 "mapping key already defined" 起不来。
    # 用 yaml.compose 只走节点树（不构造对象，因此不受锚点影响）。
    try:
        import yaml as _yaml

        root = _yaml.compose(
            (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
        )

        def _scan(node) -> None:  # noqa: ANN001
            if isinstance(node, _yaml.MappingNode):
                seen: set = set()
                for key_node, value_node in node.value:
                    key = getattr(key_node, "value", None)
                    if key in seen:
                        errors.append(
                            f"compose.yaml 第 {key_node.start_mark.line + 1} 行："
                            f"重复的键 {key!r}（docker compose 会拒绝启动）"
                        )
                    seen.add(key)
                    _scan(value_node)
            elif isinstance(node, _yaml.SequenceNode):
                for item in node.value:
                    _scan(item)

        _scan(root)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"compose.yaml 无法解析: {type(exc).__name__}")

    # --- 访问边界耦合 ---
    # 用 TLS 配置就必须开 cookie Secure，否则登录凭证仍可能走明文。
    #
    # edge 服务已删除（api 直连），所以这里不再检查"edge 是否暴露 443"。
    # 仍然保留的检查：如果有人把 api 的发布端口改成 443（自己在前置反代后面
    # 又直连了 TLS），就必须同时有 LING_COOKIE_SECURE 与可用的 TLS 配置。
    env_text = (ROOT / "deploy" / "env.example").read_text(encoding="utf-8")
    api_ports = [str(item) for item in (api.get("ports") or [])]
    if any(":443" in item for item in api_ports):
        if "LING_COOKIE_SECURE" not in env_text:
            errors.append(
                "api 发布了 443，但 env.example 没有 LING_COOKIE_SECURE："
                "上 TLS 却不开 cookie Secure 等于白上"
            )
        nginx_tls = ROOT / "deploy" / "nginx-tls.conf"
        if not nginx_tls.exists():
            errors.append("api 发布了 443，但缺少 nginx-tls.conf 示例")
        else:
            conf = nginx_tls.read_text(encoding="utf-8")
            if "proxy_buffering off" not in conf:
                errors.append("nginx-tls.conf 未关闭 proxy_buffering：SSE 会被缓冲")
            if "X-Forwarded-Proto" not in conf:
                errors.append("nginx-tls.conf 未设置 X-Forwarded-Proto")

    # --- 入口约束（edge 已删除，api 直连）---
    # api **必须**发布 host 端口：它是唯一入口，安装器的就绪等待与摘要都按
    # compose 解析出的 target=6520 发布端口工作（取不到就 fail closed）。
    api_ports = [str(item) for item in (api.get("ports") or [])]
    # 只看**容器侧**：宿主侧是 `${LING_PORT:-6520}`，它的默认值就是 6520，
    # 用子串匹配会把"容器侧写错"误判为通过（见 container_side_port 的说明）。
    api_container_ports = [
        port for port in (container_side_port(item) for item in (api.get("ports") or [])) if port
    ]
    if not api_ports:
        errors.append(
            "api 必须发布 host 端口（api 是唯一入口）：ports 里应有 "
            "`${LING_PORT:-6520}:6520`"
        )
    elif "6520" not in api_container_ports:
        errors.append(
            "api 的 ports 必须在**容器侧**发布 6520（容器内固定端口），"
            f"实际容器侧：{api_container_ports or api_ports}（宿主侧是 LING_PORT，不参与判断）"
        )
    # 不能再有 edge：留着它等于 nginx 又变成一个要维护的 compose 服务，
    # 而它的配置/证书会跟着 release 树漂。
    if "edge" in services:
        errors.append("edge 服务已被删除（api 直连 6520），不要把它加回来")
    # 网络：egress 必须是普通 bridge（不能 internal），否则 api/worker 失去外网
    # 且**不报错**（表现为模型提炼降级、QQ 连不上、检索永远走 BM25）。
    egress = (data.get("networks") or {}).get("egress")
    if egress is not None and isinstance(egress, dict) and egress.get("internal"):
        errors.append(
            "egress 不能是 internal：api/worker 靠它出网访问 OneBot/embedding/LLM"
        )
    if "egress" not in networks:
        errors.append("缺少 egress 网络：api/worker 需要出网口")
    if "internal" not in networks:
        errors.append("缺少 internal 网络：db 只应在内网可见")
    for name in ("api", "worker"):
        svc_nets = services.get(name, {}).get("networks") or []
        if "egress" not in svc_nets:
            errors.append(f"{name} 必须挂 egress（否则失去外网访问）")
        if "internal" not in svc_nets:
            errors.append(f"{name} 必须挂 internal（否则连不上 db）")

    # --- 多副本约束 ---
    # api 直连固定 host 端口 → compose 非 swarm 模式下 ports 只能给一个容器，
    # 默认副本数必须是 1（2 副本会端口冲突、第二个起不来）。
    replicas = (api.get("deploy") or {}).get("replicas")
    if replicas is not None and str(replicas) != "${LING_API_REPLICAS:-1}":
        errors.append(
            f"api 默认副本数必须是 1（api 发布固定 host 端口），实际：{replicas}"
        )
    worker_deploy = (worker.get("deploy") or {})
    replicas_value = worker_deploy.get("replicas")
    if replicas_value not in {1, "1", None, "${LING_WORKER_REPLICAS:-1}"}:
        warnings.append(
            "worker 副本数非常量：确认 handler 幂等（唯一约束已就位，见 migration 0008/0009）"
        )
    # expose 与 ports 的**容器侧**必须一致，都是 6520：一个写 6520、另一个写
    # 别的，等于对外声明的端口和容器真正监听的端口对不上。
    api_expose = [str(item) for item in (api.get("expose") or [])]
    if not api_expose:
        warnings.append("api 未声明 expose：容器内端口没有声明")
    elif "6520" not in api_expose:
        errors.append(
            "api 的 expose 必须是 6520（与 entrypoint/healthcheck 一致），"
            f"实际：{api_expose}"
        )
    elif api_ports and "6520" not in api_container_ports:
        # ports 那边已经报过错；这里只补一句"两边不一致"，避免只改一处。
        errors.append(
            "api 的 expose 是 6520，但 ports 的容器侧不是 6520"
            f"（{api_container_ports or api_ports}）：两边必须一致"
        )
    # 容器内固定端口三处一致：entrypoint 的 uvicorn 默认值 + healthcheck URL。
    entrypoint = (ROOT / "deploy" / "entrypoint.sh").read_text(encoding="utf-8")
    if '"${LING_PORT:-6520}"' not in entrypoint:
        errors.append("entrypoint.sh 的 uvicorn 端口默认值必须是 6520")
    healthcheck = str((api.get("healthcheck") or {}).get("test") or "")
    if "127.0.0.1:6520/readyz" not in healthcheck:
        errors.append("api 的 healthcheck 必须探 127.0.0.1:6520/readyz")

    print("=== compose 静态校验 ===")
    print(f"服务: {sorted(services)}")
    print(f"卷: {sorted(volumes)}  网络: {sorted(networks)}")
    print(f"compose 引用变量 {len(referenced)} 个，env.example 说明 {len(documented)} 个")
    for e in errors:
        print("  ✗", e)
    for w in warnings:
        print("  !", w)
    if errors:
        print("结果: FAIL")
        return 1
    print("结果: PASS（仅结构校验，未启动容器）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
