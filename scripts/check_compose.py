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
    env_text = (ROOT / "deploy" / "env.example").read_text(encoding="utf-8")
    tls_port = str((services.get("edge", {}).get("ports") or [""])[-1])
    if "443" in tls_port:
        if "LING_COOKIE_SECURE" not in env_text:
            errors.append(
                "edge 暴露了 443，但 env.example 没有 LING_COOKIE_SECURE："
                "上 TLS 却不开 cookie Secure 等于白上"
            )
        nginx_tls = ROOT / "deploy" / "nginx-tls.conf"
        if not nginx_tls.exists():
            errors.append("edge 暴露了 443，但缺少 nginx-tls.conf")
        else:
            conf = nginx_tls.read_text(encoding="utf-8")
            if "proxy_buffering off" not in conf:
                errors.append("nginx-tls.conf 未关闭 proxy_buffering：SSE 会被缓冲")
            if "X-Forwarded-Proto" not in conf:
                errors.append("nginx-tls.conf 未设置 X-Forwarded-Proto")

    # --- 多副本约束 ---
    replicas = (api.get("deploy") or {}).get("replicas")
    if replicas is not None and str(replicas) not in {"1", "${LING_API_REPLICAS:-2}"}:
        # 只是提示：副本数可由环境变量覆盖
        pass
    if api.get("ports"):
        errors.append(
            "api 不应发布 host 端口：多副本会端口冲突，且应只经 edge 暴露"
        )
    if "edge" not in services:
        errors.append("缺少 edge 反代：多副本需要单一入口")
    else:
        nginx = ROOT / "deploy" / "nginx.conf"
        if not nginx.exists():
            errors.append("edge 缺少 nginx.conf")
        else:
            conf = nginx.read_text(encoding="utf-8")
            if "proxy_buffering off" not in conf:
                errors.append("nginx 未关闭 proxy_buffering：SSE 流式会被缓冲成一整段")
    worker_deploy = (worker.get("deploy") or {})
    replicas_value = worker_deploy.get("replicas")
    if replicas_value not in {1, "1", None, "${LING_WORKER_REPLICAS:-1}"}:
        warnings.append(
            "worker 副本数非常量：确认 handler 幂等（唯一约束已就位，见 migration 0008/0009）"
        )
    if not api.get("expose"):
        warnings.append("api 未声明 expose，edge 可能无法解析到端口")

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
