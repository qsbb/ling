#!/usr/bin/env python3
"""Create and recover complete snapshots of the standard Ling Compose stack."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from deploy.cli.ling import CONTAINER_VERSION_PROBE, ComposeGroup, InstallLock, LingError  # noqa: E402

from ling.backup_restore import (  # noqa: E402
    FORMAT,
    SERVICES,
    SUFFIX,
    BackupError,
    backup_details,
    file_digest,
    read_manifest,
)

DB_PATH = "/var/lib/postgresql/data"
RELEASE_REF = re.compile(r"ghcr\.io/qsbb/ling-api@sha256:[a-f0-9]{64}")
SETTINGS_KEY = re.compile(r"(?:LING_[A-Z0-9_]+|POSTGRES_PASSWORD|PGDATA)")
HOST_SETTINGS = {
    "LING_APP_IMAGE", "LING_BACKUP_DIR", "LING_DATABASE_URL", "LING_DATA_DIR",
    "LING_ENV", "LING_PORT", "LING_PROJECT_ROOT", "LING_RUN_MIGRATIONS",
    "LING_WEB_DIST", "LING_WEB_DIST_HOST", "LING_COMMIT",
    "LING_POSTGRES_IMAGE", "LING_POSTGRES_DATA_DIR",
}
READY = (
    "import urllib.request; "
    "urllib.request.urlopen('http://127.0.0.1:6520/readyz', timeout=3).close()"
)
UNPACK = (
    "import sys,tarfile; "
    "archive=tarfile.open(fileobj=sys.stdin.buffer, mode='r|*'); "
    "archive.extractall('/data', filter='data')"
)


def docker(*args: str, source: Path | None = None, output: Path | None = None,
           values: dict[str, str] | None = None, timeout: int | None = None) -> str:
    """Stream large payloads and report the failed operation without dumping its input."""
    command = ["docker", *args]
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith(("LING_", "POSTGRES_", "COMPOSE_")) or k == "PGDATA")}
    if values:
        env.update(values)
    with (
        source.open("rb") if source else open(os.devnull, "rb") as stdin,
        output.open("wb") if output else open(os.devnull, "wb") as stdout,
    ):
        result = subprocess.run(
            command, stdin=stdin, stdout=stdout if output else subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, timeout=timeout, check=False,
        )
    if result.returncode:
        operation = " ".join(args[:2]) if args[0] != "compose" else args[args.index("--env-file") + 2]
        raise BackupError(f"Docker {operation} 失败（退出码 {result.returncode}）")
    return result.stdout.decode("utf-8") if not output else ""


@dataclass(frozen=True)
class Stack:
    group: ComposeGroup

    def with_env(self, path: Path) -> Stack:
        return Stack(ComposeGroup(self.group.project, self.group.files, path))

    def run(self, *args: str, **kwargs: Any) -> str:
        return docker(*self.group.argv()[1:], *args, **kwargs)

    def config(self) -> dict[str, Any]:
        config = json.loads(self.run("config", "--format", "json"))
        # Compose escapes dollars in rendered config so it can be read back as input.
        for service in config["services"].values():
            if "environment" in service:
                service["environment"] = {
                    key: value.replace("$$", "$") if isinstance(value, str) else value
                    for key, value in service["environment"].items()
                }
        return config

    def running(self, services=SERVICES) -> dict[str, Any]:
        containers = {}
        for service in services:
            ids = self.run("ps", "-q", service).split()
            if len(ids) != 1:
                raise BackupError(f"备份需要一个运行中的 {service} 容器")
            containers[service] = inspect_container(ids[0])
        return containers

    def migration(self) -> str:
        return self.run(
            "exec", "-T", "db", "psql", "-U", "ling", "-d", "ling", "-At",
            "-v", "ON_ERROR_STOP=1", "-c", "select version_num from alembic_version",
        ).strip()

    @contextmanager
    def locked(self):
        self.group.env_file.parent.mkdir(parents=True, exist_ok=True)
        lock = InstallLock(self.group.env_file)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()


def inspect_container(container: str) -> dict[str, Any]:
    return json.loads(docker("container", "inspect", container))[0]


def container_environment(container: dict[str, Any]) -> dict[str, str]:
    return dict(item.split("=", 1) for item in container["Config"]["Env"] if "=" in item)


def application_settings(values: dict[str, str]) -> dict[str, str]:
    return {
        key: value for key, value in values.items()
        if key.startswith("LING_") and key not in HOST_SETTINGS
        and not key.startswith(("LING_UPDATE_", "LING_RESTORE_"))
    }


def mount(container: dict[str, Any], destination: str) -> dict[str, Any]:
    return next(
        (item for item in container["Mounts"] if item["Destination"] == destination), {}
    )


def image_tag(image_id: str) -> str:
    return "ling-backup:" + image_id.split(":", 1)[1]


def local_image_id(reference: str) -> str | None:
    try:
        return docker("image", "inspect", "--format", "{{.Id}}", reference).strip()
    except BackupError:
        return None


def volume_identity(container: dict[str, Any], service: str) -> tuple[str, str | None]:
    data = mount(container, DB_PATH if service == "db" else "/data")
    return container["Image"], data.get("Name")


def verify_rollback_target(stack: Stack, original: dict[str, Any]) -> None:
    config = stack.config()
    for service, container in original.items():
        definition = config["services"][service]
        key = "db-data" if service == "db" else "app-data"
        expected = (
            local_image_id(definition["image"]),
            config["volumes"][key]["name"],
        )
        used = container_environment(container)
        # Older source Compose files rely on the entrypoint's default.
        used["LING_RUN_MIGRATIONS"] = used.get("LING_RUN_MIGRATIONS") or "1"
        if volume_identity(container, service) != expected or any(
            used.get(name) != value
            for name, value in definition["environment"].items() if value is not None
        ):
            raise BackupError(f"{service} 的运行状态与当前 Compose 配置不同，请先对齐部署配置")


def running_version(stack: Stack) -> dict[str, str]:
    output = stack.run("exec", "-T", "api", "python", "-c", CONTAINER_VERSION_PROBE, timeout=20)
    return json.loads(output.strip().splitlines()[-1])


def write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def create_snapshot(stack: Stack, output: Path, *, shared: bool = False) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    with stack.locked(), TemporaryDirectory(dir=output.parent, prefix=".snapshot-") as directory:
        if output.exists():
            raise BackupError("备份文件已存在")
        stage = Path(directory)
        containers = stack.running()
        app_data = mount(containers["api"], "/data")
        if app_data.get("Type") != "volume" or app_data != mount(containers["worker"], "/data"):
            raise BackupError("api 和 worker 需要使用同一个应用数据卷")
        if mount(containers["db"], DB_PATH).get("Type") != "volume":
            raise BackupError("备份需要 Compose 管理的 PostgreSQL 数据卷")
        version = running_version(stack)
        images = {service: containers[service]["Image"] for service in SERVICES}
        settings = {
            "app": application_settings(container_environment(containers["api"])),
            "postgres": {
                key: value
                for key, value in container_environment(containers["db"]).items()
                if key in {"POSTGRES_PASSWORD", "PGDATA"}
            },
        }
        (stage / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        try:
            stack.run("stop", "api", "worker", timeout=120)
            migration = stack.migration()
            stack.run("exec", "-T", "db", "pg_dump", "-Fc", "-U", "ling", "-d", "ling",
                      output=stage / "database.dump")
            docker("run", "--rm", "--pull", "never", "--network", "none",
                   "--mount", f"type=volume,src={app_data['Name']},dst=/data,readonly",
                   "--entrypoint", "tar", images["api"], "-C", "/data", "-cf", "-", ".",
                   output=stage / "data.tar")
            web = mount(containers["api"], "/app/web")
            if web.get("Type") == "bind":
                with tarfile.open(stage / "web.tar", "w") as archive:
                    archive.add(web["Source"], arcname=".")
        finally:
            # start preserves the existing containers and their effective settings.
            stack.run("start", "api", "worker", timeout=120)
        tags = []
        for image in dict.fromkeys(images.values()):
            tag = image_tag(image)
            docker("image", "tag", image, tag)
            tags.append(tag)
        docker("image", "save", *tags, output=stage / "images.tar")
        manifest = {
            "format": FORMAT, **version, "migration": migration, "images": images,
            "created_at": datetime.now(UTC).isoformat(),
            "files": {path.name: file_digest(path) for path in stage.iterdir()},
        }
        reference = containers["api"]["Config"]["Image"]
        if RELEASE_REF.fullmatch(reference) and images["api"] == images["worker"]:
            manifest["release_image"] = reference
        bundle_path = stage / "snapshot"
        with ZipFile(bundle_path, "w") as bundle:
            bundle.writestr("manifest.json", json.dumps(manifest))
            for name in manifest["files"]:
                bundle.write(stage / name, name)
        bundle_path.chmod(0o660 if shared else 0o600)
        os.replace(bundle_path, output)
    return backup_details(output)


def unpack_snapshot(path: Path, stage: Path) -> dict[str, Any]:
    try:
        with ZipFile(path) as bundle:
            manifest = read_manifest(bundle)
            for name, digest in manifest["files"].items():
                target = stage / name
                with bundle.open(name) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                if file_digest(target) != digest:
                    raise BackupError(f"备份文件校验失败：{name}")
    except (BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise BackupError("备份包已损坏") from exc
    return manifest


def dotenv_lines(values: dict[str, str]) -> str:
    return "".join(
        f"{key}={json.dumps(value.replace('$', '$$'), ensure_ascii=False)}\n"
        for key, value in values.items()
    )


def read_archived_settings(stage: Path) -> tuple[dict[str, str], dict[str, str]]:
    settings = json.loads((stage / "settings.json").read_bytes())
    if not isinstance(settings, dict):
        raise BackupError("备份部署配置无效")
    app, postgres = settings.get("app"), settings.get("postgres")
    for values in (app, postgres):
        if not isinstance(values, dict) or any(
            not SETTINGS_KEY.fullmatch(key) or not isinstance(value, str) or "\0" in value
            for key, value in values.items()
        ):
            raise BackupError("备份部署配置无效")
    if not postgres.get("POSTGRES_PASSWORD") or not postgres.get("PGDATA", "").startswith(DB_PATH + "/"):
        raise BackupError("备份缺少 PostgreSQL 初始化配置")
    return application_settings(app), {key: postgres[key] for key in ("POSTGRES_PASSWORD", "PGDATA")}


def wait_for(check, message: str, *, seconds: int = 90) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            check()
            return
        except (BackupError, subprocess.TimeoutExpired):
            time.sleep(1)
    raise BackupError(message)


def collect_memory_privacy(stack: Stack) -> dict[str, Any]:
    """Read content-free source markers after the original writers have stopped."""
    def query(sql: str) -> str:
        return stack.run(
            "exec", "-T", "db", "psql", "-U", "ling", "-d", "ling", "-At",
            "-v", "ON_ERROR_STOP=1", "-c", sql,
        ).strip()

    available = query("""
        SELECT to_regclass('memory_source_forget_markers') IS NOT NULL,
               EXISTS(SELECT 1 FROM information_schema.columns
                      WHERE table_schema = 'public' AND table_name = 'messages'
                        AND column_name = 'memory_hidden_at'),
               EXISTS(SELECT 1 FROM information_schema.columns
                      WHERE table_schema = 'public' AND table_name = 'subjects'
                        AND column_name = 'forget_generation')
    """)
    has_markers, has_message_flags, has_generations = available.split("|")
    sources = []
    if has_markers == "t":
        sources = json.loads(query("""
            SELECT COALESCE(json_agg(marker), '[]') FROM memory_source_forget_markers AS marker
        """))
    elif has_message_flags == "t":
        sources = json.loads(query("""
            SELECT COALESCE(json_agg(json_build_object(
                'message_id', id, 'subject_id', sender_subject_id, 'window_id', window_id,
                'turn_id', turn_id, 'forgotten_at', memory_hidden_at
            )), '[]') FROM messages
            WHERE role = 'user' AND sender_subject_id IS NOT NULL AND turn_id IS NOT NULL
              AND memory_hidden_at IS NOT NULL
        """))
    generations = {}
    if has_generations == "t":
        generations = json.loads(query("""
            SELECT COALESCE(json_object_agg(id, forget_generation), '{}')
            FROM subjects WHERE forget_generation > 0
        """))
    return {"sources": sources, "generations": generations}


def replay_memory_privacy(
    container: str, stage: Path, image: str, postgres: dict[str, str], state: dict[str, Any],
) -> None:
    if not any(state.values()):
        return
    compatible = docker(
        "exec", container, "psql", "-U", "ling", "-d", "ling", "-At",
        "-v", "ON_ERROR_STOP=1", "-c", "SELECT to_regclass('memory_source_forget_markers') IS NOT NULL",
    ).strip()
    if compatible != "t":
        raise BackupError("备份版本无法重放现有遗忘记录，恢复已停止")
    from urllib.parse import quote

    request = stage / "memory-privacy.json"
    write_private(request, json.dumps(state).encode("utf-8"))
    password = quote(postgres["POSTGRES_PASSWORD"], safe="")
    docker(
        "run", "--rm", "-i", "--pull", "never", "--network", f"container:{container}",
        "-e", "LING_DATABASE_URL", "--entrypoint", "python", image,
        "-m", "ling.domains.memory.restore_privacy",
        source=request, values={"LING_DATABASE_URL": f"postgresql+asyncpg://ling:{password}@127.0.0.1/ling"},
        timeout=180,
    )


def prepare_database(
    stage: Path, image: str, volume: str, postgres: dict[str, str], name: str, *,
    before_stop: Callable[[str], None] | None = None,
) -> None:
    docker(
        "run", "-d", "--name", name, "--pull", "never", "--network", "none",
        "--mount", f"type=volume,src={volume},dst={DB_PATH}",
        "-e", "POSTGRES_USER=ling", "-e", "POSTGRES_DB=ling", "-e", "POSTGRES_PASSWORD",
        "-e", "PGDATA", image, values=postgres,
    )
    try:
        wait_for(
            lambda: docker("exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "ling",
                           "-d", "ling", timeout=5),
            "用于恢复的 PostgreSQL 启动失败",
        )
        docker(
            "exec", "-i", name, "pg_restore", "-U", "ling", "-d", "ling",
            "--no-owner", "--no-privileges", "--single-transaction", "--exit-on-error",
            source=stage / "database.dump",
        )
        if before_stop is not None:
            before_stop(name)
    finally:
        try:
            docker("stop", "-t", "60", name, timeout=70)
        finally:
            docker("container", "rm", "-f", name, timeout=15)


def verify_deployment(stack: Stack, manifest: dict[str, Any]) -> None:
    wait_for(
        lambda: stack.run("exec", "-T", "api", "python", "-c", READY, timeout=6),
        "恢复版本未通过就绪检查",
    )
    observed = running_version(stack)
    if any(observed[key] != manifest[key] for key in ("version", "commit")):
        raise BackupError("运行版本与备份清单不一致")
    if stack.migration() != manifest["migration"]:
        raise BackupError("数据库迁移版本与备份清单不一致")


def restore_snapshot(stack: Stack, snapshot: Path) -> dict[str, Any]:
    env = stack.group.env_file
    project = stack.group.project
    preparation_id = uuid4().hex[:12]
    with stack.locked(), TemporaryDirectory(dir=env.parent, prefix=".restore-") as directory:
        fresh = not env.exists()
        if fresh and docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={project}").strip():
            raise BackupError(f"项目 {project} 已有容器，请指定原实例的 --env-file 或使用新的 --project")
        stage = Path(directory)
        manifest = unpack_snapshot(snapshot, stage)
        archived_app, postgres = read_archived_settings(stage)
        image_archive = stage / "images.tar"
        try:
            with tarfile.open(image_archive) as archive:
                if not any(member.isfile() for member in archive.getmembers()):
                    raise BackupError("备份镜像归档中没有文件")
        except (EOFError, OSError, tarfile.TarError) as exc:
            raise BackupError("备份镜像归档已损坏或不完整") from exc
        docker("image", "load", "-i", str(image_archive))
        images = manifest["images"]
        tags = {service: image_tag(image) for service, image in images.items()}
        for service in SERVICES:
            if local_image_id(tags[service]) != images[service]:
                raise BackupError(f"{service} 镜像与备份清单不一致")

        before = env.read_bytes() if not fresh else b""
        original = {} if fresh else stack.running(
            stack.run("ps", "--status", "running", "--services").split()
        )
        if original:
            verify_rollback_target(stack, original)
        restore_root = env.parent / ".ling-restores" / preparation_id
        volumes = {key: f"{project}_{key}-restore-{preparation_id}" for key in ("app-data", "db-data")}
        reference = manifest.get("release_image", "")
        if (isinstance(reference, str) and RELEASE_REF.fullmatch(reference)
                and images["api"] == images["worker"] == local_image_id(reference)):
            tags["api"] = tags["worker"] = reference
        values = {
            **archived_app,
            "POSTGRES_PASSWORD": postgres["POSTGRES_PASSWORD"],
            "LING_POSTGRES_DATA_DIR": postgres["PGDATA"],
            "LING_POSTGRES_IMAGE": tags["db"],
            "LING_APP_IMAGE": tags["api"],
            "LING_RESTORE_API_IMAGE": tags["api"],
            "LING_RESTORE_WORKER_IMAGE": tags["worker"],
            "LING_RESTORE_APP_VOLUME": volumes["app-data"],
            "LING_RESTORE_DB_VOLUME": volumes["db-data"],
            "LING_RESTORE_MIGRATIONS": "0",
        }
        if fresh:
            requests = env.parent / ".run" / "update" / "requests"
            status = requests.with_name("status")
            requests.mkdir(parents=True, exist_ok=True)
            status.mkdir(exist_ok=True)
            values.update(
                LING_UPDATE_REQUESTS_HOST=str(requests),
                LING_UPDATE_STATUS_HOST=str(status),
                LING_UPDATE_SHARED_GID=str(requests.stat().st_gid),
            )
        if "web.tar" in manifest["files"]:
            values["LING_RESTORE_WEB_DIST"] = str(restore_root / "web")
        candidate = stage / "deploy.env"
        write_private(candidate, before + b"\n" + dotenv_lines(values).encode("utf-8"))
        target = stack.with_env(candidate)
        config = target.config()
        for key, volume in volumes.items():
            if config["volumes"][key]["name"] != volume:
                raise BackupError("当前 Compose 文件未配置恢复卷，请按部署文档补齐卷名配置")
        for service in SERVICES:
            if config["services"][service].get("image") != tags[service]:
                raise BackupError(f"当前 Compose 文件无法恢复 {service} 的备份镜像")
            volume_key = "db-data" if service == "db" else "app-data"
            destination = DB_PATH if service == "db" else "/data"
            if not any(
                item.get("type") == "volume" and item.get("source") == volume_key
                and item.get("target") == destination
                for item in config["services"][service].get("volumes", [])
            ):
                raise BackupError(f"当前 Compose 文件无法挂载 {service} 的恢复卷")
        target_api = config["services"]["api"]
        if target_api["environment"].get("LING_RUN_MIGRATIONS") != "0":
            raise BackupError("当前 Compose 文件无法按备份迁移版本启动")
        database_env = config["services"]["db"]["environment"]
        if any(database_env.get(key) != value for key, value in postgres.items()):
            raise BackupError("Compose 配置未使用备份中的 PostgreSQL 设置")
        for key, value in archived_app.items():
            if key in target_api["environment"] and target_api["environment"][key] != value:
                raise BackupError(f"Compose 配置未使用备份中的 {key}")
        restore_root.mkdir(mode=0o700, parents=True)
        mount_web = next(
            (item for item in target_api.get("volumes", []) if item["target"] == "/app/web"), None
        )
        if mount_web and mount_web["type"] == "bind" and "web.tar" not in manifest["files"]:
            # A published image carries its own UI. Materialize it for the source template.
            docker("run", "--rm", "--pull", "never", "--network", "none", "--entrypoint", "tar",
                   tags["api"], "-C", "/app/web", "-cf", "-", ".", output=stage / "web.tar")
            values["LING_RESTORE_WEB_DIST"] = str(restore_root / "web")
            write_private(candidate, before + b"\n" + dotenv_lines(values).encode("utf-8"))
        if "LING_RESTORE_WEB_DIST" in values:
            web_dir = restore_root / "web"
            web_dir.mkdir()
            with tarfile.open(stage / "web.tar") as archive:
                archive.extractall(web_dir, filter="data")
        if "web.tar" in manifest["files"] and not mount_web:
            raise BackupError("此备份包含独立 Web 产物，请使用源码模式的 Compose 文件恢复")

        if original:
            write_private(restore_root / "previous.env", before)
        created = []
        switched = False

        def preserve_privacy(container: str) -> None:
            nonlocal switched
            if fresh:
                return
            if "db" not in original:
                raise BackupError("无法读取当前数据库的遗忘记录")
            switched = True
            writers = [service for service in ("worker", "api") if service in original]
            if writers:
                stack.run("stop", *writers, timeout=120)
            privacy = collect_memory_privacy(stack)
            replay_image = original.get("api", {}).get("Image", tags["api"])
            replay_memory_privacy(container, stage, replay_image, postgres, privacy)

        try:
            for key, volume in volumes.items():
                docker("volume", "create", "--label", f"com.docker.compose.project={project}",
                       "--label", f"com.docker.compose.volume={key}", volume)
                created.append(volume)
            prepare_database(
                stage, tags["db"], volumes["db-data"], postgres,
                f"{project}-restore-{preparation_id}", before_stop=preserve_privacy,
            )
            docker("run", "--rm", "-i", "--pull", "never", "--network", "none",
                   "--mount", f"type=volume,src={volumes['app-data']},dst=/data",
                   "--entrypoint", "python", tags["api"], "-c", UNPACK, source=stage / "data.tar")
            switched = True
            if original:
                stack.run("stop", "worker", "api", "db", timeout=120)
            write_private(env, candidate.read_bytes())
            stack.run("up", "-d", "--no-build", "--pull", "never", "db", "api", timeout=180)
            verify_deployment(stack, manifest)
            stack.run("up", "-d", "--no-build", "--pull", "never", "--wait",
                      "--wait-timeout", "120", "worker", timeout=140)
            running = stack.running()
            for service, container in running.items():
                volume = volumes["db-data" if service == "db" else "app-data"]
                if volume_identity(container, service) != (images[service], volume):
                    raise BackupError(f"{service} 未使用备份镜像和恢复卷")
        except (Exception, KeyboardInterrupt) as exc:
            if switched:
                try:
                    if fresh:
                        target.run("down", timeout=120)
                        env.unlink(missing_ok=True)
                    else:
                        write_private(env, before)
                        if original:
                            stack.run("up", "-d", "--no-build", "--pull", "never", "--wait",
                                      "--wait-timeout", "120", *original, timeout=140)
                            restarted = stack.running(original)
                            if any(
                                volume_identity(restarted[service], service) != volume_identity(container, service)
                                for service, container in original.items()
                            ):
                                raise BackupError("重新启动的容器未使用原来的镜像和数据卷")
                        else:
                            target.run("stop", "worker", "api", "db", timeout=120)
                except (Exception, KeyboardInterrupt) as rollback:
                    raise BackupError(f"恢复失败，原实例重启失败：{rollback}。回退配置位于 {restore_root}") from exc
            for volume in created:
                with suppress(BackupError, subprocess.SubprocessError):
                    docker("volume", "rm", volume, timeout=20)
            shutil.rmtree(restore_root)
            raise
    return backup_details(snapshot)


def stack_from_args(args: argparse.Namespace) -> Stack:
    main = args.compose_file or [
        os.environ.get("LING_COMPOSE_FILE") or str(ROOT / "deploy" / "compose.yaml")
    ]
    override = os.environ.get("LING_COMPOSE_OVERRIDE")
    if override and not args.compose_file:
        main.append(override)
    files = tuple(Path(item).expanduser().absolute() for item in main)
    env = args.env_file or os.environ.get("LING_COMPOSE_ENV_FILE") or files[0].with_name(".env")
    return Stack(ComposeGroup(args.project, files, Path(env).expanduser().resolve()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-file", action="append")
    parser.add_argument("--env-file")
    parser.add_argument("--project", default=os.environ.get("LING_COMPOSE_PROJECT") or "ling")
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("create", "inspect", "restore"):
        commands.add_parser(action).add_argument("file", type=Path)
    commands.add_parser("serve")
    args = parser.parse_args()
    try:
        if args.action == "inspect":
            result = backup_details(args.file)
        elif args.action == "serve":
            from deploy.backup.backup_restore_executor import serve

            serve(stack_from_args(args))
            return 0
        elif args.action == "create":
            if not args.file.name.endswith(SUFFIX):
                raise BackupError(f"备份文件应以 {SUFFIX} 结尾")
            result = create_snapshot(stack_from_args(args), args.file)
        else:
            result = restore_snapshot(stack_from_args(args), args.file)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (BackupError, OSError, subprocess.SubprocessError, ValueError, tarfile.TarError) as exc:
        print(f"备份操作失败。{exc}", file=sys.stderr)
        return 1
    except LingError as exc:
        print(exc.message, file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
