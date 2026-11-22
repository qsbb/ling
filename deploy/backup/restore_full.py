#!/usr/bin/env python3
"""宿主整包检查和恢复计划入口。"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ling.backup_restore import (  # noqa: E402
    BackupRestoreError,
    build_restore_plan,
    current_runtime_arch,
    preflight_package,
)


def _disk_free(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _preflight(args: argparse.Namespace) -> int:
    package = Path(args.package).expanduser().absolute()
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
    package = Path(args.package).expanduser().absolute()
    report = preflight_package(
        package,
        target_version=args.target_version,
        target_arch=current_runtime_arch(),
        free_bytes=_disk_free(package.parent),
    )
    if not report.ok:
        print("预检失败：" + "；".join(report.errors), file=sys.stderr)
        return 2
    if not args.apply:
        return _plan(args)
    print("整包恢复尚未接通：缺少版本切换和失败回退。", file=sys.stderr)
    return 2


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

    execute = sub.add_parser("execute", help="预检并显示恢复计划")
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
