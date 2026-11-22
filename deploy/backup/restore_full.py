#!/usr/bin/env python3
"""宿主单命令恢复入口：现有服务无法启动时也能执行。

示例：
  python3 deploy/backup/restore_full.py plan --package /vol1/backups/ling-full-001 \\
    --install-dir /opt/ling --compose-file /opt/ling/deploy/compose.yaml \\
    --env-file /opt/ling/.env --project ling --target-db ling

  python3 deploy/backup/restore_full.py execute --package /vol1/backups/ling-full-001 \\
    --install-dir /opt/ling --compose-file /opt/ling/deploy/compose.yaml \\
    --env-file /opt/ling/.env --project ling --target-db ling \\
    --rollback-dir /vol1/backups/rollback-001 --apply

默认只生成计划；必须显式 --apply 才执行数据库/数据卷/产物切换。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ling.backup_restore import (  # noqa: E402
    BackupRestoreError,
    build_restore_plan,
    current_runtime_arch,
    estimate_required_bytes,
    preflight_package,
)


def _disk_free(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _run(command: str, *, log: Path) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as stream:
        stream.write(f"$ {command}\n")
        stream.flush()
        result = subprocess.run(command, shell=True, stdout=stream, stderr=subprocess.STDOUT)
        stream.write(f"[exit={result.returncode}]\n")
    return result.returncode


def _preflight(args: argparse.Namespace) -> int:
    package = Path(args.package).expanduser().resolve()
    report = preflight_package(
        package,
        target_version=args.target_version,
        target_arch=args.target_arch or current_runtime_arch(),
        free_bytes=_disk_free(package.parent),
        available_image_digests=args.available_digest,
    )
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    return 0 if report.ok else 2


def _plan(args: argparse.Namespace) -> int:
    plan = build_restore_plan(
        Path(args.package).expanduser().resolve(),
        install_dir=Path(args.install_dir).expanduser().resolve(),
        compose_files=[Path(item).expanduser().resolve() for item in args.compose_file],
        env_file=Path(args.env_file).expanduser().resolve(),
        project=args.project,
        target_db=args.target_db,
        rollback_dir=Path(args.rollback_dir).expanduser().resolve(),
    )
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


def _execute(args: argparse.Namespace) -> int:
    package = Path(args.package).expanduser().resolve()
    install_dir = Path(args.install_dir).expanduser().resolve()
    rollback_dir = Path(args.rollback_dir).expanduser().resolve()
    report = preflight_package(
        package,
        target_version=args.target_version,
        target_arch=current_runtime_arch(),
        free_bytes=_disk_free(package.parent),
    )
    if not report.ok:
        print("预检失败：" + "；".join(report.errors), file=sys.stderr)
        return 2
    required = estimate_required_bytes(package)
    available = _disk_free(package.parent)
    if available is not None and available < required:
        print(f"空间不足：需要 {required}，可用 {available}", file=sys.stderr)
        return 2
    plan = build_restore_plan(
        package,
        install_dir=install_dir,
        compose_files=[Path(item).expanduser().resolve() for item in args.compose_file],
        env_file=Path(args.env_file).expanduser().resolve(),
        project=args.project,
        target_db=args.target_db,
        rollback_dir=rollback_dir,
    )
    log_dir = Path(args.log_dir).expanduser().resolve() if args.log_dir else install_dir / "restore-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / "restore.log"
    print(f"计划已生成；日志：{log}")
    if not args.apply:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        print("未执行：加 --apply 才会切换实例。")
        return 0
    lock = install_dir / ".restore.lock"
    try:
        lock.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        print(f"恢复锁被占用：{lock}", file=sys.stderr)
        return 6
    try:
        for step in plan["steps"]:
            rc = _run(step["command"], log=log)
            if rc != 0:
                with log.open("a", encoding="utf-8") as stream:
                    stream.write(f"FAILED step={step['name']} rc={rc}\n")
                print(f"恢复失败于 {step['name']}（exit {rc}）；详见 {log}", file=sys.stderr)
                return rc or 1
        print(f"恢复完成；日志：{log}")
        return 0
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ling 整包导入恢复宿主入口")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser) -> None:
        command.add_argument("--package", required=True)
        command.add_argument("--target-version")

    pre = sub.add_parser("preflight", help="只读预检备份包")
    common(pre)
    pre.add_argument("--target-arch")
    pre.add_argument("--available-digest", action="append", default=[])
    pre.set_defaults(func=_preflight)

    plan = sub.add_parser("plan", help="生成恢复步骤，不执行")
    common(plan)
    plan.add_argument("--install-dir", required=True)
    plan.add_argument("--compose-file", action="append", required=True, dest="compose_file")
    plan.add_argument("--env-file", required=True)
    plan.add_argument("--project", required=True)
    plan.add_argument("--target-db", required=True)
    plan.add_argument("--rollback-dir", required=True)
    plan.set_defaults(func=_plan)

    execute = sub.add_parser("execute", help="执行恢复；需 --apply 才真正切换")
    common(execute)
    execute.add_argument("--install-dir", required=True)
    execute.add_argument("--compose-file", action="append", required=True, dest="compose_file")
    execute.add_argument("--env-file", required=True)
    execute.add_argument("--project", required=True)
    execute.add_argument("--target-db", required=True)
    execute.add_argument("--rollback-dir", required=True)
    execute.add_argument("--log-dir")
    execute.add_argument("--apply", action="store_true")
    execute.set_defaults(func=_execute)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        return int(args.func(args))
    except BackupRestoreError as exc:
        print(f"[restore-full] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
