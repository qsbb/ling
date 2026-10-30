"""宿主更新执行器的隔离测试。

真实执行器会调用 git/docker/install.sh；这里全部用注入的假进程替代：
不会联网、不会执行 Docker、不会碰真实部署。临时目录里只有最小仓库骨架。

覆盖（对应交接清单的 8 个修复点）：
- env 受控 symlink + 宿主预持安装锁 FD 继承；
- compose config 结构化核验 + 私有 override 固定运行 bind；
- journal 持久消费 ID、损坏 fail closed、needs_manual 门禁与 adopt 解除；
- 宿主独立核验 Release（tag/commit/stable/newer）；
- 全部 api 副本与 web manifest 校验；
- 备份使用可信 env、alembic 真实 DSN、pg_restore --list；
- 阶段失败分类（preflight/build 可重试，其余 manual）。
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import io
import json
import multiprocessing
import os
import re
import sys
import tarfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXECUTOR_PATH = REPO_ROOT / "deploy" / "update" / "executor.py"

spec = importlib.util.spec_from_file_location("ling_update_executor", EXECUTOR_PATH)
assert spec is not None and spec.loader is not None
executor = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = executor
spec.loader.exec_module(executor)

CURRENT_COMMIT = "a" * 40
TARGET_COMMIT = "b" * 40
CURRENT_VERSION = "1.0.0"
TARGET_VERSION = "1.2.0"
TARGET_TAG = "v1.2.0"
RELEASE_ID = 4242
REPLICA_IDS = ("111111111111", "222222222222")

BASE_COMPOSE = """services:
  api:
    volumes:
      - ${LING_WEB_DIST_HOST:-../clients/web-console/dist}:/app/web:ro
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - ./certs:/etc/nginx/certs:ro
      - app-data:/data
"""

EDGE_COMPOSE = """services:
  api:
    volumes:
      - ${LING_WEB_DIST_HOST:-../clients/web-console/dist}:/app/web:ro
      - app-data:/data
  edge:
    volumes:
      - ${LING_EDGE_CONF:-./nginx-tls.conf}:/etc/nginx/conf.d/default.conf:ro
      - ${LING_EDGE_CERTS:-./certs}:/etc/nginx/certs:ro
"""

ENV_TEXT = "POSTGRES_PASSWORD=secret\nLING_ROOT_KEY=root-secret\n"


@dataclass
class Call:
    argv: list[str]
    env: dict[str, str] | None
    cwd: Path | None
    capture: bool
    log_path: Path | None = None
    pass_fds: tuple[int, ...] = ()
    stdin_file: Path | None = None
    capture_limit: int | None = None


@dataclass
class FakeProcess:
    """严格假执行器：**没有**兜底成功。

    旧版本对未识别 argv 直接返回 rc=0（QA L3）：那会让“忘记实现某个 docker 命令”
    静默变成成功。现在未匹配的命令一律抛 AssertionError，逼迫每个测试显式声明
    它依赖的命令集合。
    """

    handlers: list[tuple[Any, Any]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

    def on(self, predicate: Any, handler: Any) -> FakeProcess:
        self.handlers.append((predicate, handler))
        return self

    def __call__(
        self,
        argv: Any,
        *,
        timeout: float,
        env: Any = None,
        cwd: Any = None,
        log_path: Any = None,
        capture: bool = False,
        pass_fds: Any = (),
        stdin_file: Any = None,
        capture_limit: Any = None,
    ) -> Any:
        call = Call(
            argv=list(argv),
            env=dict(env) if env else None,
            cwd=Path(cwd) if cwd else None,
            capture=capture,
            log_path=Path(log_path) if log_path else None,
            pass_fds=tuple(pass_fds),
            stdin_file=Path(stdin_file) if stdin_file else None,
            capture_limit=capture_limit,
        )
        self.calls.append(call)
        for predicate, handler in self.handlers:
            if predicate(call):
                return handler(call)
        raise AssertionError(f"假执行器收到未声明的命令：{call.argv}")

    def called(self, fragment: str) -> list[Call]:
        return [call for call in self.calls if any(fragment in str(part) for part in call.argv)]

    def install_calls(self) -> list[Call]:
        return [
            call
            for call in self.calls
            if len(call.argv) > 1 and str(call.argv[1]).endswith("install.sh")
        ]

    def backup_calls(self) -> list[Call]:
        return [call for call in self.calls if str(call.argv[1]).endswith("backup.sh")]


def _argv_has(call: Call, *fragments: str) -> bool:
    joined = " ".join(call.argv)
    return all(fragment in joined for fragment in fragments)


def _drop_handler(fake: FakeProcess, argv: list[str]) -> None:
    probe = Call(argv=argv, env=None, cwd=None, capture=False)
    fake.handlers = [
        (predicate, handler)
        for predicate, handler in fake.handlers
        if not predicate(probe)
    ]


# ---------------------------------------------------------------- 测试数据


def _write_exe(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _make_repo(
    tmp_path: Path, *, compose_text: str = BASE_COMPOSE, env_text: str = ENV_TEXT
) -> Path:
    root = tmp_path / "repo"
    # 备份脚本必须是可执行的普通文件（执行器只认 live release 里的可执行脚本）。
    _write_exe(root / "deploy" / "backup" / "backup.sh", "#!/bin/bash\nexit 0\n")
    (root / "deploy" / "compose.yaml").write_text(compose_text, encoding="utf-8")
    (root / "deploy" / "install.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    (root / "deploy" / ".env").write_text(env_text, encoding="utf-8")
    (root / "deploy" / "nginx.conf").write_text("# plain\n", encoding="utf-8")
    (root / "deploy" / "nginx-tls.conf").write_text("# tls\n", encoding="utf-8")
    (root / "deploy" / "certs").mkdir()
    return root


def _config(tmp_path: Path, root: Path) -> Any:
    return executor.UpdateConfig(
        source_root=root,
        state_dir=tmp_path / "state",
        requests_dir=tmp_path / "run" / "requests",
        status_dir=tmp_path / "run" / "status",
        project="ling",
        port=18080,
        env_file=root / "deploy" / ".env",
        poll_seconds=1,
        max_seconds=30,
        api_uid=os.getuid(),
        api_gid=os.getgid(),
    )


def _request(
    *,
    created_at: str | None = None,
    current_version: str = CURRENT_VERSION,
    request_id: str | None = None,
) -> Any:
    return executor.load_protocol(REPO_ROOT).build_request(  # type: ignore[no-any-return]
        target_tag=TARGET_TAG,
        target_commit=TARGET_COMMIT,
        current_version=current_version,
        current_commit=CURRENT_COMMIT,
        created_at=created_at,
        request_id=request_id,
    )


def _write_pending(config: Any, protocol: Any, request: Any) -> None:
    config.requests_dir.mkdir(parents=True, exist_ok=True)
    protocol.atomic_write_json(
        config.requests_dir / protocol.PENDING_FILE, request.to_dict(), mode=0o640
    )


def _add(tar: tarfile.TarFile, name: str, content: str, *, mode: int = 0o644) -> None:
    data = content.encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    tar.addfile(info, io.BytesIO(data))


def _release_tar(
    path: Path,
    *,
    version: str = TARGET_VERSION,
    compose_text: str = BASE_COMPOSE,
    pyproject_version: str | None = None,
    include_env: bool = False,
) -> None:
    with tarfile.open(path, "w") as tar:
        _add(
            tar,
            "pyproject.toml",
            f'[project]\nname = "ling"\nversion = "{pyproject_version or version}"\n',
        )
        _add(tar, "src/ling/__init__.py", f'__version__ = "{version}"\n')
        _add(
            tar,
            "clients/web-console/package.json",
            json.dumps({"name": "web", "version": version}),
        )
        _add(tar, "deploy/compose.yaml", compose_text)
        _add(tar, "deploy/install.sh", "#!/bin/bash\nexit 0\n", mode=0o755)
        # live release 里的备份脚本：adopt 会核验它可执行。
        _add(tar, "deploy/backup/backup.sh", "#!/bin/bash\nexit 0\n", mode=0o755)
        _add(tar, "deploy/nginx.conf", "# plain\n")
        _add(tar, "deploy/nginx-tls.conf", "# tls\n")
        if include_env:
            _add(tar, "deploy/.env", "POSTGRES_PASSWORD=attacker\n")


INDEX_BYTES = b"<!doctype html>\n"
ASSET_BYTES = b"console.log(1)\n"

# 运行容器 ID（12 位十六进制，满足 CONTAINER_ID_RE）。
WORKER_IDS = ("333333333333",)
DB_ID = "444444444444"
EDGE_ID = "555555555555"
API_IMAGE = "sha256:" + "a" * 64
WORKER_IMAGE = "sha256:" + "b" * 64
DB_IMAGE = "sha256:" + "d" * 64
EDGE_IMAGE = "sha256:" + "e" * 64


def _manifest_bytes() -> bytes:
    return (
        json.dumps(
            {
                "schema": 1,
                "release_inputs": {"package.json": "c" * 64},
                "dist_files": {
                    "index.html": hashlib.sha256(INDEX_BYTES).hexdigest(),
                    "assets/app.js": hashlib.sha256(ASSET_BYTES).hexdigest(),
                },
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


# ---------------------------------------------------------------- compose 替身


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _resolve(value: str, env: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2) or ""
        if name in env:
            return env[name]
        if match.group(2) is None:
            raise AssertionError(f"测试 fixture 缺少变量 {name}")
        return default

    return _ENV_PATTERN.sub(replace, value)


def _parse_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _compose_payload(
    base: Path,
    env: dict[str, str],
    override: dict[str, Any],
    *,
    replicas: int,
    dsn: str | None = None,
) -> dict[str, Any]:
    """把一份 compose 文本 + override 解析成 ``docker compose config`` 形状。

    额外补上真实 project 里必然存在的 ``worker`` 与 ``db`` 服务（db 带
    POSTGRES_* 与网络 alias），这样「api/worker 两个镜像」「数据库身份」这两条
    契约在测试里真的被驱动到。
    """
    text = base.read_text(encoding="utf-8")
    services: dict[str, dict[str, Any]] = {}
    current: str | None = None
    for line in text.splitlines():
        service = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if service:
            current = service.group(1)
            services[current] = {"volumes": []}
            continue
        if current is None or "=" in line:
            continue
        volume = re.match(r"^\s+-\s+(\S.*?)\s*$", line)
        if volume is None:
            continue
        spec = volume.group(1).strip().strip('"').strip("'")
        resolved = _resolve(spec, env)
        parts = resolved.split(":")
        if len(parts) < 2:
            continue
        source, target = parts[0], parts[1]
        read_only = len(parts) >= 3 and parts[2] == "ro"
        if source.startswith("."):
            source = str((base.parent / source).resolve())
        volume_type = "bind" if source.startswith("/") else "volume"
        services[current]["volumes"].append(
            {
                "type": volume_type,
                "source": source,
                "target": target,
                "read_only": read_only,
            }
        )
    for service, spec in (override.get("services") or {}).items():
        entry = services.setdefault(service, {"volumes": []})
        volumes = {item["target"]: item for item in entry["volumes"]}
        for item in spec.get("volumes") or []:
            volumes[item["target"]] = dict(item)
        entry["volumes"] = list(volumes.values())
    password = env.get("POSTGRES_PASSWORD", "")
    db = services.setdefault("db", {"volumes": []})
    db["environment"] = {
        "POSTGRES_USER": "ling",
        "POSTGRES_DB": "ling",
        "POSTGRES_PASSWORD": password,
    }
    db["networks"] = {"default": {"aliases": ["db"]}}
    services.setdefault("worker", {"volumes": []})
    api = services.setdefault("api", {"volumes": []})
    api["environment"] = {
        "LING_DATABASE_URL": dsn
        or f"postgresql+asyncpg://ling:{password}@db:5432/ling",
    }
    for name, entry in services.items():
        entry["deploy"] = {"replicas": replicas if name == "api" else 1}
    return {"services": services}


# ---------------------------------------------------------------- fake runner


class FakeExecutor(executor.Executor):
    edge_ready = True

    def _wait_edge_ready(self) -> bool:  # noqa: D102 - 测试替身
        return self.edge_ready


class Runner:
    """模拟宿主上的**运行集群**：容器、镜像、挂载、就绪、运行时版本。"""

    def __init__(
        self,
        fake: FakeProcess,
        *,
        runtime_after: str = TARGET_COMMIT,
        install_rc: int = 0,
        phase: str | None = None,
        timeout_mode: str | None = None,
        version: str = TARGET_VERSION,
        current_version: str = CURRENT_VERSION,
        compose_text: str = BASE_COMPOSE,
        pyproject_version: str | None = None,
        rev_parse_commit: str = TARGET_COMMIT,
        replicas: int = 2,
        bad_manifest: bool = False,
        release: executor.ReleaseInfo | None | object = ...,
        toc: str = "1; 1259 16001 TABLE public alembic_version ling\n"
        "2; 1259 16002 TABLE public memories ling\n"
        "3; 1259 16003 TABLE public users ling\n"
        "4; 1259 16004 TABLE public sessions ling\n"
        "5; 1259 16005 TABLE public messages ling\n",
        include_env: bool = False,
        dsn: str | None = None,
        worker_containers: tuple[str, ...] = WORKER_IDS,
        image_api: str = API_IMAGE,
        image_worker: str = WORKER_IMAGE,
        psql_identity: str = "ling|ling",
        backup_manifest: Any = None,
    ) -> None:
        self.fake = fake
        self.runtime_after = runtime_after
        self.install_rc = install_rc
        self.phase = phase
        self.timeout_mode = timeout_mode
        self.version = version
        self.current_version = current_version
        self.compose_text = compose_text
        self.pyproject_version = pyproject_version
        self.rev_parse_commit = rev_parse_commit
        self.replicas = replicas
        self.bad_manifest = bad_manifest
        self.toc = toc
        self.include_env = include_env
        self.release = release
        self.dsn = dsn
        self.worker_containers = worker_containers
        self.image_api = image_api
        self.image_worker = image_worker
        self.psql_identity = psql_identity
        # 自定义 manifest 生成器：给了就由它落盘（含产物文件），用于测 manifest 的新字段。
        self.backup_manifest = backup_manifest
        # 之后可改的“运行集群”状态。
        self.unready: set[str] = set()
        self.stale: set[str] = set()
        self.extra_mounts: dict[str, dict[str, Any]] = {}
        self.image_overrides: dict[str, str] = {}
        self.unreadable_images: set[str] = set()
        self.bad_manifest_containers: set[str] = set()
        self.ps_when_installed: dict[str, tuple[str, ...]] = {}
        self.state: dict[str, Any] = {
            "installed": False,
            "release_root": None,
            "override": {},
            "install_env": {},
        }
        self._wire()

    # ---- 集群模型

    @property
    def installed(self) -> bool:
        return bool(self.state["installed"])

    def containers(self, service: str) -> list[str]:
        if service == "api":
            return list(REPLICA_IDS[: self.replicas])
        if service == "worker":
            return list(self.worker_containers)
        if service == "db":
            return [DB_ID]
        if service == "edge":
            return [EDGE_ID]
        return []

    def service_of(self, container_id: str) -> str | None:
        for service in ("api", "worker", "db", "edge"):
            if container_id in self.containers(service):
                return service
        return None

    def live_env(self) -> dict[str, str]:
        env_file = self.state["install_env"].get("__env_file__")
        if self.installed and isinstance(env_file, str) and Path(env_file).is_file():
            return _parse_env_text(Path(env_file).read_text(encoding="utf-8"))
        return {}

    def live_model(self) -> dict[str, Any]:
        """当前**实际运行**的 compose 模型（安装后 = release 树 + override）。"""
        if self.installed:
            base = Path(self.state["release_root"]) / "deploy" / "compose.yaml"
            env = self.live_env()
            override = self.state["override"]
        else:
            base = Path(self.config_source) / "deploy" / "compose.yaml"
            env = _parse_env_text(
                (Path(self.config_source) / "deploy" / ".env").read_text(encoding="utf-8")
            )
            override = {}
        return _compose_payload(
            base, env, override, replicas=self.replicas, dsn=self.dsn
        )

    def mounts(self, container_id: str) -> list[dict[str, Any]]:
        service = self.service_of(container_id)
        if service is None:
            return []
        model = self.live_model()
        payload = []
        for entry in (model["services"].get(service) or {}).get("volumes") or []:
            source = entry["source"]
            if entry["type"] == "volume":
                source = f"/var/lib/docker/volumes/ling_{entry['source']}/_data"
            payload.append(
                {
                    "Type": entry["type"],
                    "Source": source,
                    "Destination": entry["target"],
                    "RW": not entry["read_only"],
                }
            )
        extra = self.extra_mounts.get(container_id)
        if extra is not None:
            payload.append(extra)
        return payload

    def image_id(self, container_id: str) -> str:
        if container_id in self.image_overrides:
            return self.image_overrides[container_id]
        service = self.service_of(container_id)
        if service == "worker":
            return self.image_worker
        if service == "db":
            return DB_IMAGE
        if service == "edge":
            return EDGE_IMAGE
        return self.image_api

    def runtime(self, container_id: str) -> tuple[str, str]:
        if self.installed and container_id not in self.stale:
            return self.runtime_after, self.version
        return CURRENT_COMMIT, self.current_version

    # ---- handlers

    def _release_info(self, tag: str) -> executor.ReleaseInfo | None:
        if self.release is ...:
            return executor.ReleaseInfo(
                release_id=RELEASE_ID, tag=tag, commit=TARGET_COMMIT, tag_version=TARGET_VERSION
            )
        return self.release  # type: ignore[return-value]

    def _ps(self, call: Call) -> Any:
        service = call.argv[-1]
        if self.installed and service in self.ps_when_installed:
            ids = list(self.ps_when_installed[service])
        else:
            ids = self.containers(service)
        return executor.ProcessResult(0, "\n".join(ids) + "\n")

    def _inspect(self, call: Call) -> Any:
        fmt = call.argv[3]
        container_id = call.argv[4]
        if fmt == executor.INSPECT_IMAGE_FORMAT:
            if container_id in self.unreadable_images:
                return executor.ProcessResult(1, "no such object")
            return executor.ProcessResult(0, self.image_id(container_id) + "\n")
        if fmt == executor.INSPECT_MOUNTS_FORMAT:
            return executor.ProcessResult(0, json.dumps(self.mounts(container_id)))
        if fmt == executor.INSPECT_NETWORKS_FORMAT:
            service = self.service_of(container_id)
            alias = service or "unknown"
            networks = {"default": {"Aliases": [alias, container_id]}}
            return executor.ProcessResult(0, json.dumps(networks))
        raise AssertionError(f"未知的 docker inspect 格式：{fmt}")

    def _exec(self, call: Call) -> Any:
        container_id = call.argv[2]
        script = call.argv[-1]
        if script == executor.MANIFEST_SCRIPT:
            release_root = self.state["release_root"]
            assert release_root is not None
            path = (
                Path(release_root)
                / "clients"
                / "web-console"
                / "dist"
                / ".ling-web-manifest.json"
            )
            if self.bad_manifest or container_id in self.bad_manifest_containers:
                return executor.ProcessResult(0, json.dumps({"sha256": "0" * 64}) + "\n")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            return executor.ProcessResult(0, json.dumps({"sha256": digest}) + "\n")
        if script == executor.RUNTIME_SCRIPT:
            commit, version = self.runtime(container_id)
            return executor.ProcessResult(
                0, json.dumps({"commit": commit, "version": version}) + "\n"
            )
        if script == executor.READYZ_SCRIPT.format(
            port=executor.API_READYZ_PORT, path=executor.API_READYZ_PATH
        ):
            status = 500 if container_id in self.unready else 200
            return executor.ProcessResult(0, json.dumps({"status": status}) + "\n")
        raise AssertionError(f"未知的容器内脚本：{script}")

    def _config(self, call: Call) -> Any:
        files = [call.argv[index + 1] for index, arg in enumerate(call.argv) if arg == "-f"]
        assert files
        base = Path(files[0])
        text = base.read_text(encoding="utf-8")
        if text.lstrip().startswith("{"):
            # 已经合并过的模型（compose-resolved.yaml 里存的是 JSON）。
            return executor.ProcessResult(0, text)
        env: dict[str, str] = {}
        if "--env-file" in call.argv:
            env_file = Path(call.argv[call.argv.index("--env-file") + 1])
            if env_file.is_file():
                env.update(_parse_env_text(env_file.read_text(encoding="utf-8")))
        env.update(call.env or {})
        override: dict[str, Any] = {}
        if len(files) > 1:
            override = json.loads(Path(files[1]).read_text(encoding="utf-8"))
        payload = _compose_payload(
            base, env, override, replicas=self.replicas, dsn=self.dsn
        )
        return executor.ProcessResult(0, json.dumps(payload))

    def _config_yaml(self, call: Call) -> Any:
        # ``-f resolved.yaml`` 之前的那次 canonical 输出：直接给合并模型的 JSON
        # （JSON 是合法 YAML，执行器只把它当文本落盘再解析）。
        return self._config(call)

    def _psql(self, call: Call) -> Any:
        return executor.ProcessResult(0, self.psql_identity + "\n")

    def _backup(self, call: Call) -> Any:
        out = Path(call.argv[-1])
        out.mkdir(parents=True, exist_ok=True)
        if self.backup_manifest is not None:
            (out / "manifest-1.txt").write_text(self.backup_manifest(out), encoding="utf-8")
            return executor.ProcessResult(0, "")
        (out / "ling-1.dump").write_bytes(b"PGDMP" + b"\x00" * 32)
        (out / "tombstones-1.txt").write_text("", encoding="utf-8")
        (out / "manifest-1.txt").write_text(
            "created_at=x\nmode=compose\ndump=ling-1.dump\n"
            "tombstones=tombstones-1.txt\ntombstones_state=ok\n",
            encoding="utf-8",
        )
        return executor.ProcessResult(0, "")

    def _pg_restore(self, call: Call) -> Any:
        assert call.stdin_file is not None and call.stdin_file.is_file()
        assert call.log_path is not None
        call.log_path.write_text(self.toc, encoding="utf-8")
        return executor.ProcessResult(0, "")

    def _archive(self, call: Call) -> Any:
        output = None
        for part in call.argv:
            if str(part).startswith("--output="):
                output = Path(str(part).split("=", 1)[1])
        assert output is not None
        _release_tar(
            output,
            version=self.version,
            compose_text=self.compose_text,
            pyproject_version=self.pyproject_version,
            include_env=self.include_env,
        )
        return executor.ProcessResult(0, "")

    def _install(self, call: Call) -> Any:
        if self.timeout_mode is not None:
            # 超时前安装器可能已经写过阶段标记：先落标记，再模拟超时。
            if self.phase is not None and call.env is not None:
                phase_path = Path(call.env["LING_INSTALL_PHASE_FILE"])
                phase_path.parent.mkdir(parents=True, exist_ok=True)
                phase_path.write_text(json.dumps({"stage": self.phase}), encoding="utf-8")
            raise executor.CommandTimeout(
                "installer timed out",
                group_terminated=self.timeout_mode == "terminated",
            )
        assert call.cwd is not None
        release_root = call.cwd
        self.state["release_root"] = release_root
        dist = release_root / "clients" / "web-console" / "dist"
        dist.mkdir(parents=True, exist_ok=True)
        (dist / ".ling-web-manifest.json").write_bytes(_manifest_bytes())
        (dist / "index.html").write_bytes(INDEX_BYTES)
        (dist / "assets").mkdir(exist_ok=True)
        (dist / "assets" / "app.js").write_bytes(ASSET_BYTES)
        if call.env is not None:
            self.state["install_env"] = dict(call.env)
            self.state["install_env"]["__env_file__"] = str(
                Path(call.env["LING_INSTALL_PHASE_FILE"]).parent.parent.parent
                / "repo"
                / "deploy"
                / ".env"
            )
            override = call.env.get("LING_INSTALL_COMPOSE_OVERRIDE")
            if override:
                self.state["override"] = json.loads(Path(override).read_text(encoding="utf-8"))
            if self.phase is not None:
                phase_path = Path(call.env["LING_INSTALL_PHASE_FILE"])
                phase_path.parent.mkdir(parents=True, exist_ok=True)
                phase_path.write_text(json.dumps({"stage": self.phase}), encoding="utf-8")
        self.state["installed"] = bool(self.install_rc == 0)
        return executor.ProcessResult(self.install_rc, "")

    def _wire(self) -> None:
        fake = self.fake
        fake.on(
            lambda call: _argv_has(call, "compose", "config") and "--format" in call.argv,
            self._config,
        )
        fake.on(
            lambda call: _argv_has(call, "compose", "config") and "--format" not in call.argv,
            self._config_yaml,
        )
        fake.on(
            lambda call: _argv_has(call, "compose", "ps", "-q", "--status", "running"),
            self._ps,
        )
        fake.on(lambda call: call.argv[:2] == ["docker", "inspect"], self._inspect)
        fake.on(lambda call: call.argv[:2] == ["docker", "exec"], self._exec)
        fake.on(lambda call: "psql" in call.argv, self._psql)
        fake.on(lambda call: "pg_restore" in call.argv, self._pg_restore)
        fake.on(
            lambda call: _argv_has(call, "git", "status"),
            lambda call: executor.ProcessResult(0, ""),
        )
        fake.on(
            lambda call: _argv_has(call, "git", "init"),
            lambda call: executor.ProcessResult(0, ""),
        )
        fake.on(lambda call: str(call.argv[1]).endswith("backup.sh"), self._backup)
        fake.on(
            lambda call: _argv_has(call, "alembic", "current"),
            lambda call: executor.ProcessResult(0, "abc123 (head)\n"),
        )
        fake.on(
            lambda call: _argv_has(call, "remote", "get-url"),
            lambda call: executor.ProcessResult(0, executor.REPO_URL + "\n"),
        )
        fake.on(
            lambda call: _argv_has(call, "git", "fetch"),
            lambda call: executor.ProcessResult(0, ""),
        )
        fake.on(
            lambda call: _argv_has(call, "rev-parse"),
            lambda call: executor.ProcessResult(0, self.rev_parse_commit + "\n"),
        )
        fake.on(lambda call: _argv_has(call, "git", "archive"), self._archive)
        fake.on(lambda call: _argv_has(call, "install.sh"), self._install)

    # 由 _make_executor 填：source_root（未安装时的 live 根）。
    config_source: str = ""


def _make_executor(
    tmp_path: Path,
    fake: FakeProcess,
    *,
    compose_text: str = BASE_COMPOSE,
    env_text: str = ENV_TEXT,
    runner: Runner | None = None,
) -> Any:
    root = _make_repo(tmp_path, compose_text=compose_text, env_text=env_text)
    config = _config(tmp_path, root)
    protocol = executor.load_protocol(root)
    if runner is not None:
        runner.config_source = str(root)
    lookup = runner._release_info if runner is not None else (lambda tag: None)
    return FakeExecutor(config, protocol, process=fake, release_lookup=lookup), protocol


def _setup(
    tmp_path: Path,
    *,
    compose_text: str = BASE_COMPOSE,
    candidate_compose_text: str | None = None,
    env_text: str = ENV_TEXT,
    **kwargs: Any,
) -> tuple[Any, Any, Runner]:
    fake = FakeProcess()
    runner = Runner(fake, compose_text=candidate_compose_text or compose_text, **kwargs)
    instance, protocol = _make_executor(
        tmp_path, fake, compose_text=compose_text, env_text=env_text, runner=runner
    )
    return instance, protocol, runner


def _current(executor_instance: Any) -> Any:
    return executor_instance.read_current()


def _assert_job(
    executor_instance: Any, state: str, code: str | None, stage: str | None = None
) -> None:
    job = _current(executor_instance)
    assert job is not None
    assert job.state == state, job
    assert job.error_code == code, job
    if stage is not None:
        assert job.stage == stage, job


def _update_once(instance: Any, protocol: Any, runner: Runner) -> str:
    """跑一次成功更新（用于构造“已安装”的既有部署）。"""
    _write_pending(instance.config, protocol, _request())
    result = instance.execute_once()
    assert result == "ran", result
    assert runner.installed
    return result


@pytest.fixture(autouse=True)
def _no_verify_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor, "VERIFY_TIMEOUT_SECONDS", 0)


# ---------------------------------------------------------------- 配置/目录


def test_config_requires_absolute_paths_and_outside_source(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    config_path = tmp_path / "update.toml"
    config_path.write_text(
        "[update]\nsource_root = 'relative'\nstate_dir = '/tmp/x'\n"
        "requests_dir = '/tmp/r'\nstatus_dir = '/tmp/s'\n"
        "project = 'ling'\nport = 18080\nenv_file = '/tmp/e'\n",
        encoding="utf-8",
    )
    with pytest.raises(executor.ConfigError):
        executor.load_config(config_path)
    config_path.write_text(
        f"[update]\nsource_root = '{root}'\nstate_dir = '{root / 'state'}'\n"
        f"requests_dir = '/tmp/r'\nstatus_dir = '/tmp/s'\n"
        f"project = 'ling'\nport = 18080\nenv_file = '{root / 'deploy' / '.env'}'\n",
        encoding="utf-8",
    )
    with pytest.raises(executor.ConfigError):
        executor.load_config(config_path)


def test_init_creates_secure_permissions(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    config = _config(tmp_path, root)
    protocol = executor.load_protocol(root)
    instance = FakeExecutor(config, protocol, process=FakeProcess())
    instance.ensure_runtime_dirs()
    modes = {
        "state": os.stat(config.state_dir).st_mode & 0o777,
        "requests": os.stat(config.requests_dir).st_mode & 0o777,
        "status": os.stat(config.status_dir).st_mode & 0o777,
    }
    assert modes == {"state": 0o700, "requests": 0o770, "status": 0o755}
    for mode in modes.values():
        assert mode & 0o002 == 0, "不允许世界可写"
    assert os.stat(config.requests_dir).st_gid == os.getgid()
    assert config.overrides_dir.is_dir()


# ---------------------------------------------------------------- 成功路径


def test_happy_path_env_symlink_lock_fd_and_override(tmp_path: Path) -> None:
    instance, protocol, runner = _setup(tmp_path)
    request = _request()
    _write_pending(instance.config, protocol, request)
    env_path = instance.config.source_root / "deploy" / ".env"
    before = env_path.read_text(encoding="utf-8")

    assert instance.execute_once() == "ran"

    _assert_job(instance, "succeeded", None, "complete")
    installed = instance.read_installed()
    assert installed["target_commit"] == TARGET_COMMIT
    assert installed["version"] == TARGET_VERSION
    heartbeat = protocol.read_json_file(
        instance.config.status_dir / protocol.HEARTBEAT_FILE
    )
    assert heartbeat is not None and heartbeat["deployed_commit"] == TARGET_COMMIT
    assert not (instance.config.requests_dir / protocol.PENDING_FILE).exists()
    journal = (instance.config.state_dir / "journal.jsonl").read_text(encoding="utf-8")
    assert '"start"' in journal and '"end"' in journal
    assert request.request_id in journal

    # 不 checkout/stash/reset/clean 本仓库，也不改原 env 文件。
    for call in instance._process.calls:  # type: ignore[attr-defined]
        joined = " ".join(call.argv)
        for forbidden in (" checkout ", " stash ", " reset ", " clean "):
            assert forbidden not in f" {joined} ", joined
    assert env_path.read_text(encoding="utf-8") == before

    # release 里是受控 symlink：指向原 env 的真实路径，且 release 未自带 .env。
    release_root = Path(installed["release_root"])
    env_link = release_root / "deploy" / ".env"
    assert env_link.is_symlink()
    assert os.path.realpath(env_link) == os.path.realpath(env_path)

    # 宿主预持安装锁，并把同一个 FD 交给安装器继承。
    install = instance._process.install_calls()  # type: ignore[attr-defined]
    assert len(install) == 1
    env = install[0].env or {}
    assert env["LING_COMMIT"] == TARGET_COMMIT
    assert env["LING_UPDATE_ENABLED"] == "1"
    assert env["LING_WEB_DIST_HOST"].endswith("clients/web-console/dist")
    assert env["LING_UPDATE_REQUESTS_HOST"] == str(instance.config.requests_dir)
    assert env["LING_INSTALL_PHASE_FILE"] == str(instance.config.phase_path)
    # 安装锁 FD 必须存在、被继承，并且锁文件锚在原 env 父目录。
    lock_path = instance.config.install_lock_path()
    assert lock_path == Path(os.path.realpath(env_path)).parent / ".ling-install.lock"
    assert lock_path.is_file()
    assert install[0].pass_fds and len(install[0].pass_fds) == 1
    assert env["LING_INSTALL_LOCK_FD"] == str(install[0].pass_fds[0])
    # 私有 override 持久化并交给安装器。
    override = Path(installed["compose_override"])
    assert override.is_file() and override.parent == instance.config.overrides_dir
    assert override.stat().st_mode & 0o777 == 0o600
    assert env["LING_INSTALL_COMPOSE_OVERRIDE"] == str(override)
    override_payload = json.loads(override.read_text(encoding="utf-8"))
    api_web = [
        entry
        for entry in override_payload["services"]["api"]["volumes"]
        if entry["target"] == "/app/web"
    ]
    assert api_web and api_web[0]["source"] == str(
        (release_root / "clients" / "web-console" / "dist").resolve()
    )
    # 所有核对都用真实 compose 解析，并且至少有一次带上了这次生成的 override
    # （安装后执行器自己的 compose 调用也走同一份 override）。
    config_calls = [
        call
        for call in instance._process.calls  # type: ignore[attr-defined]
        if "config" in call.argv
    ]
    assert len(config_calls) >= 2
    overrides_used = [call for call in config_calls if call.argv.count("-f") >= 2]
    assert overrides_used, "verify_source 必须用真实 compose 解析核对 override"
    for call in overrides_used:
        files = [call.argv[i + 1] for i, arg in enumerate(call.argv) if arg == "-f"]
        assert Path(files[1]).parent == instance.config.overrides_dir

    # 备份目录在 state 内（不会挂进 API），且 alembic 记录写下了。
    backup_dir = Path(installed["backup_dir"])
    assert instance.config.state_dir in backup_dir.parents
    assert (backup_dir / "alembic-current.txt").is_file()
    assert (backup_dir / "deploy.env.bak").stat().st_mode & 0o777 == 0o600
    assert (backup_dir / "dump-toc.txt").stat().st_mode & 0o777 == 0o600
    recovery_files = list(instance.config.state_dir.glob("recovery-*.txt"))
    assert recovery_files == []
    del runner


def test_release_with_bundled_env_is_refused(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path, include_env=True)
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "source_invalid", "fetch")
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]


def test_private_override_pins_tls_selection_and_certs(tmp_path: Path) -> None:
    env_text = ENV_TEXT + "LING_EDGE_CONF=./nginx-tls.conf\n"
    instance, protocol, _ = _setup(
        tmp_path, compose_text=EDGE_COMPOSE, env_text=env_text
    )
    _write_pending(instance.config, protocol, _request())
    assert instance.execute_once() == "ran"
    installed = instance.read_installed()
    release_root = Path(installed["release_root"])
    live_root = instance.config.source_root
    override = json.loads(Path(installed["compose_override"]).read_text(encoding="utf-8"))
    edge = {
        entry["target"]: entry for entry in override["services"]["edge"]["volumes"]
    }
    # TLS 选择保留：仍用新 release 的 nginx-tls.conf，而不是退回明文配置。
    assert edge["/etc/nginx/conf.d/default.conf"]["source"] == str(
        (release_root / "deploy" / "nginx-tls.conf").resolve()
    )
    # 证书是运行数据：固定为当前（旧）部署的绝对目录，不跟 release 漂移。
    assert edge["/etc/nginx/certs"]["source"] == str((live_root / "deploy" / "certs").resolve())
    assert edge["/etc/nginx/certs"]["read_only"] is True
    # 即使安装器还不支持 override，也通过环境变量把 TLS 选择/证书钉成绝对路径。
    install_env = instance._process.install_calls()[0].env or {}  # type: ignore[attr-defined]
    assert install_env["LING_EDGE_CONF"] == edge["/etc/nginx/conf.d/default.conf"]["source"]
    assert install_env["LING_EDGE_CERTS"] == edge["/etc/nginx/certs"]["source"]


def test_external_nginx_conf_stays_absolute(tmp_path: Path) -> None:
    external = tmp_path / "custom-nginx.conf"
    external.write_text("# custom\n", encoding="utf-8")
    env_text = ENV_TEXT + f"LING_EDGE_CONF={external}\n"
    instance, protocol, _ = _setup(tmp_path, compose_text=EDGE_COMPOSE, env_text=env_text)
    _write_pending(instance.config, protocol, _request())
    assert instance.execute_once() == "ran"
    installed = instance.read_installed()
    override = json.loads(Path(installed["compose_override"]).read_text(encoding="utf-8"))
    edge = {entry["target"]: entry for entry in override["services"]["edge"]["volumes"]}
    assert edge["/etc/nginx/conf.d/default.conf"]["source"] == str(external)


def test_update_channel_mounts_are_allowed(tmp_path: Path) -> None:
    """首次接线：候选新增更新通道挂载（白名单目标）应被固定为绝对来源。"""
    changed = BASE_COMPOSE.replace(
        "      - app-data:/data\n",
        "      - app-data:/data\n"
        "      - ${LING_UPDATE_REQUESTS_HOST}:/run/ling-update/requests\n"
        "      - ${LING_UPDATE_STATUS_HOST}:/run/ling-update/status:ro\n",
    )
    instance, protocol, _ = _setup(tmp_path, candidate_compose_text=changed)
    _write_pending(instance.config, protocol, _request())
    assert instance.execute_once() == "ran"
    installed = instance.read_installed()
    override = json.loads(Path(installed["compose_override"]).read_text(encoding="utf-8"))
    api = {entry["target"]: entry for entry in override["services"]["api"]["volumes"]}
    assert api["/run/ling-update/requests"]["source"] == str(
        Path(os.path.realpath(instance.config.requests_dir))
    )
    assert api["/run/ling-update/requests"]["read_only"] is False
    assert api["/run/ling-update/status"]["read_only"] is True


def test_changed_bind_mount_refused(tmp_path: Path) -> None:
    changed = BASE_COMPOSE.replace(
        "      - app-data:/data\n",
        "      - app-data:/data\n      - /srv/secrets:/etc/ling-extra:ro\n",
    )
    instance, protocol, _ = _setup(tmp_path, candidate_compose_text=changed)
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "compose_bind_changed")
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]


def test_changed_certs_source_is_pinned_not_rejected(tmp_path: Path) -> None:
    """候选 release 自带 certs 目录也不会改变运行来源（override 钉死）。"""
    changed = BASE_COMPOSE.replace("./certs:/etc/nginx/certs:ro", "./new-certs:/etc/nginx/certs:ro")
    instance, protocol, _ = _setup(tmp_path, candidate_compose_text=changed)
    _write_pending(instance.config, protocol, _request())
    assert instance.execute_once() == "ran"
    installed = instance.read_installed()
    override = json.loads(Path(installed["compose_override"]).read_text(encoding="utf-8"))
    api = {entry["target"]: entry for entry in override["services"]["api"]["volumes"]}
    assert api["/etc/nginx/certs"]["source"] == str(
        (instance.config.source_root / "deploy" / "certs").resolve()
    )


# ---------------------------------------------------------------- 门控/阶段


def test_dirty_worktree_refused_without_touching_deploy(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path)
    instance._process.handlers.insert(  # type: ignore[attr-defined]
        0,
        (
            lambda call: _argv_has(call, "git", "status"),
            lambda call: executor.ProcessResult(0, " M src/ling/app.py\n"),
        ),
    )
    _write_pending(instance.config, protocol, _request())
    assert instance.execute_once() == "ran"
    _assert_job(instance, "failed", "source_dirty")
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]
    assert instance._process.called("backup.sh") == []  # type: ignore[attr-defined]


def test_untracked_dirty_worktree_refused(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path)
    instance._process.handlers.insert(  # type: ignore[attr-defined]
        0,
        (
            lambda call: _argv_has(call, "git", "status"),
            lambda call: executor.ProcessResult(0, "?? new-file.py\n"),
        ),
    )
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "source_dirty")


def test_backup_failure_never_calls_installer(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path)
    _drop_handler(instance._process, ["bash", "/x/backup.sh", "/tmp/out"])  # type: ignore[attr-defined]
    instance._process.on(  # type: ignore[attr-defined]
        lambda call: str(call.argv[1]).endswith("backup.sh"),
        lambda call: executor.ProcessResult(1, ""),
    )
    _write_pending(instance.config, protocol, _request())
    assert instance.execute_once() == "ran"
    _assert_job(instance, "failed", "backup_failed", "backup")
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]


def test_backup_manifest_tombstone_failure_is_failure(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path)
    _drop_handler(instance._process, ["bash", "/x/backup.sh", "/tmp/out"])  # type: ignore[attr-defined]

    def broken_backup(call: Call) -> Any:
        out = Path(call.argv[-1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "ling-1.dump").write_bytes(b"PGDMP" + b"\x00" * 8)
        (out / "tombstones-1.txt").write_text("", encoding="utf-8")
        (out / "manifest-1.txt").write_text(
            "dump=ling-1.dump\ntombstones=tombstones-1.txt\ntombstones_state=FAILED\n",
            encoding="utf-8",
        )
        return executor.ProcessResult(0, "")

    instance._process.on(  # type: ignore[attr-defined]
        lambda call: str(call.argv[1]).endswith("backup.sh"), broken_backup
    )
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "backup_failed")
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]


def test_backup_uses_trusted_env_and_verifies_dump_toc(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path)
    _write_pending(instance.config, protocol, _request())
    assert instance.execute_once() == "ran"
    backup_calls = instance._process.called("backup.sh")  # type: ignore[attr-defined]
    assert len(backup_calls) == 1
    env = backup_calls[0].env or {}
    # compose 插值需要口令；只从可信 env 文件取白名单键。
    assert env["POSTGRES_PASSWORD"] == "secret"
    assert env["LING_COMPOSE_PROJECT"] == "ling"
    assert "LD_PRELOAD" not in env
    # pg_restore --list 真读了 dump（stdin 传入，不落临时副本）。
    restore = instance._process.called("pg_restore")  # type: ignore[attr-defined]
    assert restore and restore[0].stdin_file is not None


def test_dump_toc_without_relevant_tables_is_failure(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path, toc="1; 1259 1 TABLE public other ling\n")
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "backup_failed")
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]


# ---------------------------------------------------------------- manifest 覆盖字段
#
# QA（task60 Q2）：执行器原来只验墓碑状态 + PGDMP 头 + TOC，不看 manifest 的
# keys_root / media / media_state / *sha256，于是 `media_state=partial` 的备份也会
# 判"通过"并继续升级。下面五个方向覆盖：present 正常、absent、partial、哈希不符、
# 旧格式缺字段；另加一条"枚举越界"确认 fail closed。


def _sha256_of_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _backup_payload(
    *,
    keys_root: str = "present",
    keys_source: str = "file",
    keys_env_set: str | None = None,
    media_state: str = "present",
    media_sha256: str | None = None,
    dump_sha256: str | None = None,
    omit: tuple[str, ...] = (),
    keys_bytes: bytes | None = None,
    keys_missing: bool = False,
    keys_sha256: str | None = None,
    keys_file_field: str | None = None,
) -> Any:
    """造一份 backup.sh 新格式 manifest（含真实产物与真实 sha256）。

    ``omit`` 用来模拟旧格式 manifest（缺新字段）；其余参数模拟脚本应写出的取值。
    QA task62 F2 补的四个旋钮用来模拟"present 但密钥文件缺失/为空/哈希不符"：
    ``keys_missing``（不写文件）、``keys_bytes``（指定字节，空串即 0 字节）、
    ``keys_sha256``（覆盖 manifest 里记的哈希）、``keys_file_field``（覆盖
    ``keys_root_file`` 的取值）。
    """

    def write(out: Path) -> str:
        dump = out / "ling-1.dump"
        dump.write_bytes(b"PGDMP" + b"\x00" * 32)
        (out / "tombstones-1.txt").write_text("", encoding="utf-8")
        lines = [
            "created_at=x",
            "mode=compose",
            "dump=ling-1.dump",
            f"dump_sha256={dump_sha256 or _sha256_of_file(dump)}",
            "tombstones=tombstones-1.txt",
            "tombstones_state=ok",
            "tombstones_sha256=-",
        ]
        key_file = out / "keys" / "root.key"
        keys_hash = "-"
        keys_ref = "-"
        if keys_root == "present":
            keys_ref = "keys/root.key"
            if not keys_missing:
                key_file.parent.mkdir(exist_ok=True)
                key_file.write_bytes(b"fake-root-key\n" if keys_bytes is None else keys_bytes)
                keys_hash = _sha256_of_file(key_file)
        if keys_sha256 is not None:
            keys_hash = keys_sha256
        if keys_file_field is not None:
            keys_ref = keys_file_field
        lines += [
            f"keys_root={keys_root}",
            f"keys_root_file={keys_ref}",
            f"keys_root_source={keys_source}",
            f"keys_root_sha256={keys_hash}",
        ]
        if keys_env_set is not None:
            lines.append(f"keys_env_set={keys_env_set}")
        media_name = "absent"
        media_hash = "-"
        if media_state != "absent":
            media_name = "media-1.tar"
            media_file = out / media_name
            media_file.write_bytes(b"fake-media-tar-bytes")
            media_hash = media_sha256 or _sha256_of_file(media_file)
        lines += [
            f"media={media_name}",
            f"media_state={media_state}",
            f"media_sha256={media_hash}",
        ]
        kept = [line for line in lines if line.split("=", 1)[0] not in omit]
        return "\n".join(kept) + "\n"

    return write


def _verify_warnings(instance: Any) -> str:
    """读备份目录里的校验告警文件（不存在时返回空串）。"""
    files = sorted(instance.config.backups_dir.glob("*/verify-warnings.txt"))
    assert len(files) <= 1, files
    return files[0].read_text(encoding="utf-8") if files else ""


def test_backup_manifest_with_coverage_fields_passes(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path, backup_manifest=_backup_payload())
    _write_pending(instance.config, protocol, _request())
    assert instance.execute_once() == "ran"
    assert instance._process.install_calls()  # type: ignore[attr-defined]
    # 全新格式、一切自洽：不该有任何"未能校验/不完整"的告警。
    assert _verify_warnings(instance) == ""


def test_backup_manifest_absent_key_and_media_passes_with_warning(tmp_path: Path) -> None:
    """absent 是合法状态（脚本 exit 0），但必须留痕：恢复后凭据解不开。"""
    factory = _backup_payload(keys_root="absent", keys_source="none", media_state="absent")
    instance, protocol, _ = _setup(tmp_path, backup_manifest=factory)
    _write_pending(instance.config, protocol, _request())
    assert instance.execute_once() == "ran"
    warnings = _verify_warnings(instance)
    assert "keys_root=absent" in warnings
    assert "无法解密" in warnings


def test_backup_manifest_partial_media_passes_with_explicit_warning(tmp_path: Path) -> None:
    """partial 有意**不**硬失败，但绝不能显示成"备份完整"（取舍理由见实现注释）。

    升级不替换 app-data 卷，partial 只是"归档时恰好有文件在写"；硬失败会让每次
    "上传中提交升级"都失败，收益小于代价。因此这里判通过 + 写显式告警，
    恢复时必须按"可能缺文件"核对 media_ref。
    """
    instance, protocol, _ = _setup(tmp_path, backup_manifest=_backup_payload(media_state="partial"))
    _write_pending(instance.config, protocol, _request())
    assert instance.execute_once() == "ran"
    assert instance._process.install_calls()  # type: ignore[attr-defined]
    warnings = _verify_warnings(instance)
    assert "media_state=partial" in warnings
    assert "media_ref" in warnings
    # 告警同时进备份日志（cron/运维读的地方）。
    logs = sorted(instance.config.logs_dir.glob("*-backup.log"))
    assert logs and "media_state=partial" in logs[0].read_text(encoding="utf-8")


@pytest.mark.parametrize("field", ["media_sha256", "dump_sha256"])
def test_backup_manifest_hash_mismatch_is_failure(tmp_path: Path, field: str) -> None:
    """哈希不符 = 这份备份不是 manifest 记的字节，必须 fail closed、不进构建。"""
    wrong = hashlib.sha256(b"not-the-real-bytes").hexdigest()
    kwargs: dict[str, Any] = {"media_sha256": None, "dump_sha256": None}
    kwargs[field] = wrong
    instance, protocol, _ = _setup(tmp_path, backup_manifest=_backup_payload(**kwargs))
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "backup_failed")
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]


def test_backup_manifest_legacy_without_new_fields_is_flagged_not_failed(
    tmp_path: Path,
) -> None:
    """旧格式 manifest 缺新字段：不失败（旧备份确实无从校验），但必须写"未能校验"。"""
    factory = _backup_payload(
        omit=(
            "keys_root",
            "keys_root_file",
            "keys_root_source",
            "keys_root_sha256",
            "media",
            "media_state",
            "media_sha256",
            "dump_sha256",
        )
    )
    instance, protocol, _ = _setup(tmp_path, backup_manifest=factory)
    _write_pending(instance.config, protocol, _request())
    assert instance.execute_once() == "ran"
    warnings = _verify_warnings(instance)
    assert "旧格式" in warnings
    assert warnings.count("未能校验") == 3  # 根密钥 / 媒体 / dump 字节


@pytest.mark.parametrize("omitted", ["media_state", "keys_root_source"])
def test_backup_manifest_half_missing_coverage_fields_is_failure(
    tmp_path: Path, omitted: str
) -> None:
    """只写了一半的覆盖字段（有 media= 没 media_state= 等）不可信，必须 fail closed。"""
    instance, protocol, _ = _setup(
        tmp_path, backup_manifest=_backup_payload(omit=(omitted,))
    )
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "backup_failed")
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]


def test_backup_manifest_unexpected_enum_value_is_failure(tmp_path: Path) -> None:
    """枚举越界（脚本回归/手改 manifest）一律 fail closed。"""
    factory = _backup_payload(keys_root="present", keys_source="env")
    instance, protocol, _ = _setup(tmp_path, backup_manifest=factory)
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "backup_failed")
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]


# QA task62 F2（Low）：`keys_root=present` 原先只校验枚举组合——媒体侧有
# 存在性/哈希校验，密钥侧没有，属不对称。下面四条把"present 必须有文件为证"
# 钉死：缺失 / 空 / 哈希不符 / 字段自相矛盾都必须 `backup_failed`，且不进构建。


def test_backup_manifest_present_key_missing_file_is_failure(tmp_path: Path) -> None:
    """manifest 说 present 但 `keys/root.key` 不在：这份备份恢复后凭据解不开。"""
    factory = _backup_payload(keys_missing=True)
    instance, protocol, _ = _setup(tmp_path, backup_manifest=factory)
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "backup_failed", "backup")
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]


def test_backup_manifest_present_key_empty_file_is_failure(tmp_path: Path) -> None:
    """0 字节的 root.key 等于"没有密钥"，但它看起来像"已经备好了"。"""
    factory = _backup_payload(keys_bytes=b"")
    instance, protocol, _ = _setup(tmp_path, backup_manifest=factory)
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "backup_failed", "backup")
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]


def test_backup_manifest_present_key_hash_mismatch_is_failure(tmp_path: Path) -> None:
    """密钥归档的字节与 manifest 记的哈希不符：不是同一把密钥，fail closed。"""
    factory = _backup_payload(keys_sha256=hashlib.sha256(b"other-key").hexdigest())
    instance, protocol, _ = _setup(tmp_path, backup_manifest=factory)
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "backup_failed", "backup")
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]


def test_backup_manifest_present_key_without_hash_is_failure(tmp_path: Path) -> None:
    """present 却写 `keys_root_sha256=-`：无法证明归档字节，只能拒绝。"""
    factory = _backup_payload(keys_sha256="-")
    instance, protocol, _ = _setup(tmp_path, backup_manifest=factory)
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "backup_failed", "backup")
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]


@pytest.mark.parametrize("field_value", ["-", "keys/other.key", "../root.key"])
def test_backup_manifest_present_key_with_contradictory_field_is_failure(
    tmp_path: Path, field_value: str
) -> None:
    """`keys_root=present` 却把 `keys_root_file` 写成 `-`/别的路径：自相矛盾。"""
    factory = _backup_payload(keys_file_field=field_value)
    instance, protocol, _ = _setup(tmp_path, backup_manifest=factory)
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "backup_failed", "backup")
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]


def test_tag_commit_mismatch_refused_before_build(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path, rev_parse_commit="c" * 40)
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "target_mismatch", "fetch")
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]


def test_release_lookup_mismatch_is_unverifiable(tmp_path: Path) -> None:
    bad = executor.ReleaseInfo(
        release_id=RELEASE_ID, tag=TARGET_TAG, commit="d" * 40, tag_version=TARGET_VERSION
    )
    instance, protocol, _ = _setup(tmp_path, release=bad)
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "release_unverifiable", "fetch")
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]


def test_missing_release_is_unverifiable(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path, release=None)
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "release_unverifiable", "fetch")


def test_downgrade_tag_is_refused(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path, current_version="2.0.0")
    _write_pending(instance.config, protocol, _request(current_version="2.0.0"))
    instance.execute_once()
    _assert_job(instance, "failed", "not_newer", "fetch")
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]


def test_release_version_mismatch_refused(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path, pyproject_version="9.9.9")
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "source_invalid", "verify_source")
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]


def test_build_failure_keeps_old_service(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path, install_rc=1, phase="build")
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "build_failed")


def test_deploy_failure_needs_manual(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path, install_rc=1, phase="deploy")
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "needs_manual", "deploy_failed")
    notes = list(instance.config.state_dir.glob("recovery-*.txt"))
    assert len(notes) == 1
    text = notes[0].read_text(encoding="utf-8")
    assert "恢复前提与限制" in text
    # 旧镜像必须**按服务**分别记录（api 与 worker 是两个镜像）。
    assert f"previous_image_api={API_IMAGE}" in text
    assert f"previous_image_worker={WORKER_IMAGE}" in text
    # 已捕获的恢复上下文：绝对路径 + 项目名，而不是“去猜”。
    assert f"live_release_root={instance.config.source_root}" in text
    assert "compose_project=ling" in text
    assert "canonical_env_file=" in text
    assert "docs/UPDATES.md" in text
    assert "rolled_back" not in text


def test_failure_without_phase_marker_is_manual(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path, install_rc=1, phase=None)
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "needs_manual", "deploy_state_unknown")


def test_installer_timeout_is_manual(tmp_path: Path) -> None:
    """超时但无法证明进程组已终止：保守按人工处理（M3）。"""
    instance, protocol, _ = _setup(tmp_path, timeout_mode="running")
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "needs_manual", "installer_timeout")


def test_installer_timeout_without_phase_marker_is_manual(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path, timeout_mode="terminated")
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "needs_manual", "installer_timeout")


def test_installer_timeout_in_build_with_dead_group_is_retryable(tmp_path: Path) -> None:
    """阶段标记=build 且安装进程组确认终止，才允许判 failed（M3）。"""
    instance, protocol, _ = _setup(
        tmp_path, timeout_mode="terminated", phase="build"
    )
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "build_failed")


def test_installer_timeout_in_build_with_live_group_is_manual(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path, timeout_mode="running", phase="build")
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "needs_manual", "installer_timeout")


def test_installer_timeout_after_deploy_marker_is_manual(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(
        tmp_path, timeout_mode="terminated", phase="deploy"
    )
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "needs_manual", "installer_timeout")


def test_unexpected_exception_before_build_is_retryable_failure(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path)

    def boom(request: Any) -> None:
        raise RuntimeError("unexpected")

    instance._preflight = boom  # type: ignore[method-assign]
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "config_invalid", "preflight")


def test_unexpected_exception_after_build_is_manual(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path)

    def boom(request: Any, release_root: Path) -> Any:
        raise RuntimeError("unexpected")

    instance._verify_deployed = boom  # type: ignore[method-assign]
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "needs_manual", "deploy_state_unknown")


def test_verify_failure_is_manual(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path, runtime_after=CURRENT_COMMIT)
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "needs_manual", "verify_failed")


def test_replica_count_mismatch_is_manual(tmp_path: Path) -> None:
    instance, protocol, runner = _setup(tmp_path)
    # 安装后只剩 1 个 api 副本（期望 2）：绝不能声称更新完成。
    runner.ps_when_installed["api"] = (REPLICA_IDS[0],)
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "needs_manual", "verify_failed")


def test_web_manifest_mismatch_is_manual(tmp_path: Path) -> None:
    instance, protocol, runner = _setup(tmp_path)
    runner.bad_manifest = True
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "needs_manual", "verify_failed")


def test_stale_phase_marker_removed_before_install(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path)
    instance.ensure_runtime_dirs()
    instance.config.phase_path.write_text('{"stage": "complete"}', encoding="utf-8")
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "succeeded", None, "complete")


# ---------------------------------------------------------------- 消费/门禁


def test_interrupted_running_job_is_not_retried(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path)
    instance.ensure_runtime_dirs()
    request = _request()
    protocol.atomic_write_json(
        instance.config.status_dir / protocol.CURRENT_FILE,
        request.to_job().with_progress(state="running", stage="deploy").to_dict(),
        mode=0o644,
    )
    assert instance.execute_once() == "interrupted"
    _assert_job(instance, "needs_manual", "interrupted")
    assert instance._process.calls == [], "中断的任务不应触发任何部署命令"  # type: ignore[attr-defined]


def test_journal_started_request_is_not_rerun(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path)
    request = _request()
    _write_pending(instance.config, protocol, request)
    instance.ensure_runtime_dirs()
    instance.journal("start", request.request_id)
    assert instance.execute_once() == "needs_manual"
    _assert_job(instance, "needs_manual", "interrupted")
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]
    assert not (instance.config.requests_dir / protocol.PENDING_FILE).exists()


def test_completed_request_cannot_be_replayed(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path)
    request = _request()
    _write_pending(instance.config, protocol, request)
    instance.ensure_runtime_dirs()
    instance.journal("start", request.request_id)
    instance.journal("end", request.request_id, state="succeeded")
    assert instance.execute_once() == "duplicate"
    _assert_job(instance, "failed", "invalid_request")
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]


def test_corrupt_journal_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, protocol, _ = _setup(tmp_path)
    instance.ensure_runtime_dirs()
    instance.config.journal_path.write_text("not-json\n", encoding="utf-8")
    _write_pending(instance.config, protocol, _request())
    assert instance.execute_once() == "manual_gate"
    _assert_job(instance, "needs_manual", "interrupted")
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]

    monkeypatch.setattr(executor, "MAX_JOURNAL_BYTES", 8)
    instance.config.journal_path.write_text(
        json.dumps({"event": "start", "request_id": "x" * 40}) + "\n", encoding="utf-8"
    )
    instance.protocol.remove_file(instance.config.status_dir / protocol.CURRENT_FILE)
    request = _request()
    _write_pending(instance.config, protocol, request)
    assert instance.execute_once() == "manual_gate"
    _assert_job(instance, "needs_manual", "interrupted")


def test_needs_manual_blocks_pending_until_adopt(tmp_path: Path) -> None:
    instance, protocol, runner = _setup(tmp_path)
    _update_once(instance, protocol, runner)
    old = _request(request_id="upd_" + "c" * 32)
    protocol.atomic_write_json(
        instance.config.status_dir / protocol.CURRENT_FILE,
        old.to_job().with_progress(state="needs_manual", error_code="verify_failed").to_dict(),
        mode=0o644,
    )
    new = _request()
    _write_pending(instance.config, protocol, new)
    assert instance.execute_once() == "manual_gate"
    # 人工门禁期间**不执行也不删除**新请求（它不是失败任务的一部分）。
    pending_path = instance.config.requests_dir / protocol.PENDING_FILE
    assert pending_path.exists()
    stored = protocol.read_json_file(pending_path)
    assert stored is not None and stored["request_id"] == new.request_id
    # 旧的人工记录不能被新请求覆盖。
    _assert_job(instance, "needs_manual", "verify_failed")
    installs_before = len(instance._process.install_calls())  # type: ignore[attr-defined]

    # adopt-current 在完全自证（取两把锁 + 核验副本/挂载/镜像/edge）之后才解除门禁。
    assert instance.adopt_current().commit == TARGET_COMMIT
    assert _current(instance) is None
    assert len(instance._process.install_calls()) == installs_before  # type: ignore[attr-defined]
    journal = (instance.config.state_dir / "journal.jsonl").read_text(encoding="utf-8")
    assert "manual-cleared" in journal
    # 解除门禁后心跳必须立刻回到 ok：否则 API 仍会报 manual_required。
    heartbeat = protocol.read_json_file(instance.config.status_dir / protocol.HEARTBEAT_FILE)
    assert heartbeat is not None and heartbeat["status"] == "ok"


def test_expired_pending_fails_without_running(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path)
    _write_pending(instance.config, protocol, _request(created_at="2020-01-01T00:00:00Z"))
    assert instance.execute_once() == "expired"
    _assert_job(instance, "failed", "expired")
    assert instance._process.calls == []  # type: ignore[attr-defined]


def test_install_lock_busy_returns_busy(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path)
    instance.ensure_runtime_dirs()
    _write_pending(instance.config, protocol, _request())
    holder = executor.InstallLock(instance.config.install_lock_path())
    assert holder.acquire()
    try:
        assert instance.execute_once() == "busy"
    finally:
        holder.release()
    # 没有开始执行：pending 仍在，也不会写 running 状态。
    assert (instance.config.requests_dir / protocol.PENDING_FILE).exists()
    assert _current(instance) is None
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]


def test_adopt_current_records_runtime(tmp_path: Path) -> None:
    instance, protocol, runner = _setup(tmp_path)
    _update_once(instance, protocol, runner)
    runtime = instance.adopt_current()
    assert runtime.commit == TARGET_COMMIT
    assert runtime.replicas == REPLICA_IDS
    installed = instance.read_installed()
    assert installed["target_commit"] == TARGET_COMMIT
    assert installed["adopted"] is True
    # api 与 worker 的镜像 ID 都记下来了（回退两个都要）。
    assert installed["images"]["api"] == [API_IMAGE]
    assert installed["images"]["worker"] == [WORKER_IMAGE]
    heartbeat = protocol.read_json_file(instance.config.status_dir / protocol.HEARTBEAT_FILE)
    assert heartbeat is not None and heartbeat["current_version"] == TARGET_VERSION


def test_read_phase_stage_accepts_formats(tmp_path: Path) -> None:
    phase = tmp_path / "phase"
    phase.write_text('{"stage": "deploy"}', encoding="utf-8")
    assert executor.read_phase_stage(phase) == "deploy"
    phase.write_text("build\ndeploy\n", encoding="utf-8")
    assert executor.read_phase_stage(phase) == "deploy"
    phase.write_text('"verify"', encoding="utf-8")
    assert executor.read_phase_stage(phase) == "verify"
    phase.write_text("nonsense", encoding="utf-8")
    assert executor.read_phase_stage(phase) is None
    assert executor.read_phase_stage(tmp_path / "missing") is None


def test_parse_env_file_ignores_unsafe_lines() -> None:
    values = executor.parse_env_file(
        "POSTGRES_PASSWORD=\"p a s s\"\nexport LING_DB_USER=ling\n"
        "BAD KEY=1\nNOEQUALS\n# comment\nPATH=/evil\n"
    )
    assert values["POSTGRES_PASSWORD"] == "p a s s"
    assert values["LING_DB_USER"] == "ling"
    assert "BAD KEY" not in values
    assert values["PATH"] == "/evil"  # 解析层保留，取用层靠白名单过滤


def test_env_file_symlink_is_supported_and_lock_anchors_real_target(
    tmp_path: Path,
) -> None:
    root = _make_repo(tmp_path)
    target = tmp_path / "shared.env"
    target.write_text(ENV_TEXT, encoding="utf-8")
    env_link = root / "deploy" / ".env"
    env_link.unlink()
    env_link.symlink_to(target)
    config = _config(tmp_path, root)
    assert config.env_file == env_link
    assert config.install_lock_path() == target.parent / ".ling-install.lock"


def test_lookup_release_requires_stable_published_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    commit = "e" * 40
    payloads: dict[str, dict[str, object]] = {
        f"{executor.GITHUB_API_ROOT}/repos/{executor.REPO_SLUG}/releases/tags/v1.2.0": {
            "id": 7,
            "tag_name": "v1.2.0",
            "draft": False,
            "prerelease": False,
        },
        f"{executor.GITHUB_API_ROOT}/repos/{executor.REPO_SLUG}/git/ref/tags/v1.2.0": {
            "object": {"sha": commit, "type": "commit"}
        },
    }

    def fake(url: str) -> dict[str, object] | None:
        return payloads.get(url)

    monkeypatch.setattr(executor, "_github_json", fake)
    release = executor.lookup_release("v1.2.0")
    assert release == executor.ReleaseInfo(
        release_id=7, tag="v1.2.0", commit=commit, tag_version="1.2.0"
    )
    # 非严格 vX.Y.Z 的 tag 连查询都不发。
    assert executor.lookup_release("v1.2") is None
    assert executor.lookup_release("1.2.0") is None
    for key, value in (("draft", True), ("prerelease", True), ("tag_name", "v9.9.9")):
        payloads[f"{executor.GITHUB_API_ROOT}/repos/{executor.REPO_SLUG}/releases/tags/v1.2.0"] = {
            "id": 7,
            "tag_name": "v1.2.0",
            "draft": False,
            "prerelease": False,
            **{key: value},
        }
        assert executor.lookup_release("v1.2.0") is None


def test_lookup_release_dereferences_annotated_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    commit = "f" * 40
    tag_sha = "1" * 40
    root = executor.GITHUB_API_ROOT
    payloads: dict[str, dict[str, object]] = {
        f"{root}/repos/{executor.REPO_SLUG}/releases/tags/v2.0.0": {
            "id": 9,
            "tag_name": "v2.0.0",
            "draft": False,
            "prerelease": False,
        },
        f"{root}/repos/{executor.REPO_SLUG}/git/ref/tags/v2.0.0": {
            "object": {"sha": tag_sha, "type": "tag"}
        },
        f"{root}/repos/{executor.REPO_SLUG}/git/tags/{tag_sha}": {
            "object": {"sha": commit, "type": "commit"}
        },
    }
    monkeypatch.setattr(executor, "_github_json", lambda url: payloads.get(url))
    release = executor.lookup_release("v2.0.0")
    assert release is not None and release.commit == commit


# ================================================================ QA B1：pending 所有权


def test_clear_pending_only_removes_matching_request(tmp_path: Path) -> None:
    """CAS 清理：只删自己那个 id，窗口内新到达的请求绝不能被删。"""
    instance, protocol, _ = _setup(tmp_path)
    instance.ensure_runtime_dirs()
    request = _request()
    _write_pending(instance.config, protocol, request)
    path = instance.config.requests_dir / protocol.PENDING_FILE

    assert instance.clear_pending("upd_" + "d" * 32) is False
    assert path.exists()
    assert instance.clear_pending(request.request_id) is True
    assert not path.exists()


def test_claim_pending_waits_for_api_lock_and_rechecks_id(tmp_path: Path) -> None:
    """跨进程（真实 flock）：API 持锁替换 pending 时，宿主 claim 必须等锁并放弃旧 id。"""
    instance, protocol, _ = _setup(tmp_path)
    instance.ensure_runtime_dirs()
    request = _request()
    _write_pending(instance.config, protocol, request)
    pending_path = instance.config.requests_dir / protocol.PENDING_FILE
    lock_path = instance.config.requests_dir / protocol.LOCK_FILE
    replacement = _request(request_id="upd_" + "e" * 32)

    ctx = multiprocessing.get_context("fork")
    started = ctx.Event()
    release = ctx.Event()

    def api_submit() -> None:
        # API 侧：同一把 requests/.lock，锁内替换成**另一个** request_id。
        with protocol.FileLock(lock_path):
            started.set()
            assert release.wait(15)
            protocol.atomic_write_json(
                pending_path, replacement.to_dict(), mode=0o640
            )

    process = ctx.Process(target=api_submit)
    process.start()
    try:
        assert started.wait(10), "API 子进程没拿到锁"
        timer = threading.Timer(0.3, release.set)
        timer.start()
        # 宿主 claim：阻塞在锁上；拿到锁后必须重新读并发现 id 变了。
        assert instance.claim_pending(request) is False
        timer.join(5)
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.kill()
            process.join(5)

    stored = protocol.read_json_file(pending_path)
    assert stored is not None and stored["request_id"] == replacement.request_id
    assert instance.read_current() is None
    # 没有为旧 id 写 journal start（否则新请求会被当成“已消费”）。
    assert instance.journal_started(request.request_id) is False


def test_interrupted_manual_clear_keeps_newer_pending(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path)
    instance.ensure_runtime_dirs()
    old = _request()
    protocol.atomic_write_json(
        instance.config.status_dir / protocol.CURRENT_FILE,
        old.to_job().with_progress(state="running", stage="deploy").to_dict(),
        mode=0o644,
    )
    new = _request(request_id="upd_" + "f" * 32)
    _write_pending(instance.config, protocol, new)

    assert instance.execute_once() == "interrupted"
    stored = protocol.read_json_file(
        instance.config.requests_dir / protocol.PENDING_FILE
    )
    assert stored is not None and stored["request_id"] == new.request_id
    _assert_job(instance, "needs_manual", "interrupted")


def test_invalid_pending_fails_closed_without_removal(tmp_path: Path) -> None:
    """M1：坏 JSON / 未来 schema 不能被当 idle，也不能被删除。"""
    instance, protocol, _ = _setup(tmp_path)
    instance.ensure_runtime_dirs()
    pending = instance.config.requests_dir / protocol.PENDING_FILE
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text('{"schema_version": "v9", "request_id": "x"}', encoding="utf-8")

    assert instance.execute_once() == "pending_invalid"
    assert pending.exists(), "不可信文件必须留给人工处理"
    heartbeat = protocol.read_json_file(instance.config.status_dir / protocol.HEARTBEAT_FILE)
    assert heartbeat is not None
    assert heartbeat["status"] == protocol.EXECUTOR_STATUS_PROTOCOL_UNSUPPORTED
    assert heartbeat["pending_state"] == protocol.PENDING_STATE_INVALID
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]


# ================================================================ QA B2：逐副本就绪 + edge 2xx


def _edge_server(mode: str) -> Any:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - HTTP 方法名
            if self.server.mode == "redirect":  # type: ignore[attr-defined]
                self.send_response(301)
                self.send_header("Location", "https://example.invalid/readyz")
                self.end_headers()
            elif self.server.mode == "error":  # type: ignore[attr-defined]
                self.send_response(503)
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

        def log_message(self, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.mode = mode  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_edge_ready_accepts_only_2xx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """301/503 都不能算部署成功；不跟随重定向（不连外部域名）。"""
    monkeypatch.setattr(executor, "EDGE_READY_ATTEMPTS", 2)
    monkeypatch.setattr(executor, "EDGE_READY_INTERVAL_SECONDS", 0.0)
    root = _make_repo(tmp_path)
    protocol = executor.load_protocol(root)
    config = dataclasses.replace(_config(tmp_path, root), port=0)
    server = _edge_server("redirect")
    try:
        config = dataclasses.replace(config, port=server.server_address[1])
        instance = executor.Executor(config, protocol, process=FakeProcess())
        assert instance._wait_edge_ready() is False

        server.mode = "error"  # type: ignore[attr-defined]
        assert instance._wait_edge_ready() is False

        server.mode = "ok"  # type: ignore[attr-defined]
        assert instance._wait_edge_ready() is True
    finally:
        server.shutdown()
        server.server_close()


def test_single_unready_replica_fails_verify(tmp_path: Path) -> None:
    """只探一个副本会漏掉另一个未就绪的副本。"""
    instance, protocol, runner = _setup(tmp_path)
    runner.unready = {REPLICA_IDS[1]}
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "needs_manual", "verify_failed")


# ================================================================ QA B3：恢复文档


def test_recovery_note_never_recommends_no_build_rollback(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path, install_rc=1, phase="deploy")
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    text = next(instance.config.state_dir.glob("recovery-*.txt")).read_text(
        encoding="utf-8"
    )
    assert "up -d --no-build" in text
    assert "build 已经" in text
    assert "两个" in text and "image ID" in text
    assert "逻辑" in text and "PITR" in text
    assert "墓碑" in text
    assert "schema" in text
    assert f"previous_image_api={API_IMAGE}" in text
    assert "compose_project=ling" in text
    assert "docs/UPDATES.md" in text
    # 不生成“复制即执行”的一键命令块。
    assert "```" not in text


# ================================================================ QA B4：adopt 自证


def test_adopt_ignores_stale_installed_release_root(tmp_path: Path) -> None:
    """B4：旧 release_root + 新运行 commit 不能被盖章成基线。"""
    instance, protocol, runner = _setup(tmp_path)
    _update_once(instance, protocol, runner)
    release = Path(instance.read_installed()["release_root"])
    stale = tmp_path / "stale-release"
    (stale / "deploy").mkdir(parents=True)
    (stale / "deploy" / "compose.yaml").write_text(BASE_COMPOSE, encoding="utf-8")
    tampered = instance.read_installed()
    tampered["release_root"] = str(stale)
    instance.write_installed(tampered)

    runtime = instance.adopt_current()
    assert runtime.commit == TARGET_COMMIT
    installed = instance.read_installed()
    assert Path(installed["release_root"]) == release.resolve()
    assert installed["release_root"] != str(stale)


def test_adopt_refuses_on_mount_mismatch_without_side_effects(tmp_path: Path) -> None:
    instance, protocol, runner = _setup(tmp_path)
    _update_once(instance, protocol, runner)
    old = _request(request_id="upd_" + "9" * 32)
    protocol.atomic_write_json(
        instance.config.status_dir / protocol.CURRENT_FILE,
        old.to_job().with_progress(state="needs_manual", error_code="verify_failed").to_dict(),
        mode=0o644,
    )
    before = instance.read_installed()
    runner.extra_mounts[REPLICA_IDS[0]] = {
        "Type": "bind",
        "Source": "/srv/evil",
        "Destination": "/srv/evil",
        "RW": True,
    }
    with pytest.raises(executor.UpdateFailure) as info:
        instance.adopt_current()
    assert info.value.code == "installed_commit_unknown"
    assert instance.read_installed() == before
    current = _current(instance)
    assert current is not None and current.state == "needs_manual"


def test_adopt_refuses_without_worker_image(tmp_path: Path) -> None:
    instance, protocol, runner = _setup(tmp_path)
    _update_once(instance, protocol, runner)
    before = instance.read_installed()
    runner.unreadable_images.add(WORKER_IDS[0])
    with pytest.raises(executor.UpdateFailure):
        instance.adopt_current()
    assert instance.read_installed() == before
    assert instance.read_installed().get("adopted") is None


def test_adopt_refuses_incomplete_release_without_writing(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path)
    instance.ensure_runtime_dirs()
    with pytest.raises(executor.UpdateFailure) as info:
        instance.adopt_current()
    assert info.value.code == "installed_commit_unknown"
    assert not instance.config.installed_path.exists()


# ================================================================ QA B5 / M5 / M6：备份


def test_backup_uses_live_release_script_and_resolved_compose(tmp_path: Path) -> None:
    instance, protocol, runner = _setup(tmp_path)
    _update_once(instance, protocol, runner)
    installed = instance.read_installed()
    release = Path(installed["release_root"])
    # 破坏 source_root 的备份脚本：若仍从旧树取脚本，这次备份会失败。
    (instance.config.source_root / "deploy" / "backup" / "backup.sh").write_text(
        "#!/bin/bash\nexit 9\n", encoding="utf-8"
    )

    request = _request(request_id="upd_" + "a" * 32)
    out = instance._backup(request)  # noqa: SLF001 - 直接验证备份契约

    call = instance._process.backup_calls()[-1]  # type: ignore[attr-defined]
    assert Path(call.argv[1]).resolve().is_relative_to(release.resolve())
    assert call.cwd == release
    resolved = out / "compose-resolved.yaml"
    assert resolved.is_file()
    assert resolved.stat().st_mode & 0o777 == 0o600
    env = call.env or {}
    assert env["LING_COMPOSE_FILE"] == str(resolved)
    assert env["LING_COMPOSE_PROJECT"] == "ling"
    # 安装时实际使用的是 release/deploy/.env（指向 canonical env 的受控 symlink）。
    assert Path(env["LING_COMPOSE_ENV_FILE"]).resolve() == instance.config.env_file.resolve()
    assert env["LING_COMPOSE_OVERRIDE"] == installed["compose_override"]
    # 合并模型真的包含 api/worker/db 三个服务（不是仅从 env 猜）。
    model = json.loads(resolved.read_text(encoding="utf-8"))
    assert {"api", "worker", "db"} <= set(model["services"])


def test_backup_rejects_loopback_database_dsn(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(
        tmp_path, dsn="postgresql+asyncpg://ling:secret@127.0.0.1:5432/ling"
    )
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "backup_failed", "backup")
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]


def test_backup_rejects_password_mismatch(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(
        tmp_path, dsn="postgresql+asyncpg://ling:other@db:5432/ling"
    )
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "backup_failed", "backup")


def test_backup_rejects_live_database_identity_mismatch(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path, psql_identity="postgres|postgres")
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "backup_failed", "backup")


# ================================================================ QA M2：两个镜像


def test_preflight_requires_worker_image_id(tmp_path: Path) -> None:
    instance, protocol, _ = _setup(tmp_path, worker_containers=())
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "installed_image_unknown", "preflight")
    assert instance._process.install_calls() == []  # type: ignore[attr-defined]


def test_preflight_rejects_split_api_image_ids(tmp_path: Path) -> None:
    instance, protocol, runner = _setup(tmp_path)
    runner.image_overrides[REPLICA_IDS[1]] = "sha256:" + "9" * 64
    _write_pending(instance.config, protocol, _request())
    instance.execute_once()
    _assert_job(instance, "failed", "installed_image_unknown", "preflight")


# ================================================================ QA L1：捕获截断


def test_run_process_truncates_and_keeps_stderr_separate(tmp_path: Path) -> None:
    log = tmp_path / "capture.log"
    result = executor.run_process(
        ["/bin/sh", "-c", "printf '0123456789abcdef'; printf 'warn' 1>&2"],
        timeout=10,
        capture=True,
        capture_limit=8,
        log_path=log,
    )
    assert result.returncode == 0
    assert result.truncated is True
    assert result.stdout == "01234567"
    assert "warn" not in result.stdout
    assert "warn" in log.read_text(encoding="utf-8")
