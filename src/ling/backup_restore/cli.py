"""``python -m ling.backup_restore.cli`` 的薄入口。

核心逻辑在 :mod:`ling.backup_restore`，这里只负责参数解析与退出码。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import (
    FORMAT_VERSION,
    BackupRestoreError,
    build_restore_plan,
    detect_artifact_mode,
    load_manifest,
    preflight_package,
    redact_env,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ling-backup-restore")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("preflight", help="只读预检一份完整备份包")
    check.add_argument("package", type=Path)
    check.add_argument("--target-version")
    check.add_argument("--target-arch")
    check.add_argument("--free-bytes", type=int)
    check.add_argument(
        "--available-digest",
        action="append",
        default=[],
        dest="available_digests",
        help="本机可用的镜像 digest，可重复；给了就核对包内 digest",
    )

    plan = sub.add_parser("plan", help="生成宿主恢复命令计划，不执行")
    plan.add_argument("package", type=Path)
    plan.add_argument("--install-dir", type=Path, required=True)
    plan.add_argument("--compose-file", action="append", default=[], dest="compose_files")
    plan.add_argument("--env-file", type=Path, required=True)
    plan.add_argument("--project", required=True)
    plan.add_argument("--target-db", required=True)
    plan.add_argument("--rollback-dir", type=Path, required=True)

    inspect = sub.add_parser("inspect", help="打印脱敏后的清单摘要")
    inspect.add_argument("package", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "preflight":
            report = preflight_package(
                args.package,
                target_version=args.target_version,
                target_arch=args.target_arch,
                free_bytes=args.free_bytes,
                available_image_digests=args.available_digests,
            )
            print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
            return 0 if report.ok else 2
        if args.command == "inspect":
            manifest, _ = load_manifest(args.package)
            summary = {
                "format_version": manifest.get("format_version"),
                "version": manifest.get("version"),
                "artifact": {
                    key: value
                    for key, value in (manifest.get("artifact") or {}).items()
                    if key != "env"
                },
                "scope": manifest.get("scope"),
                "secrets": manifest.get("secrets"),
                "integrity": manifest.get("integrity"),
            }
            env_path = args.package / "config" / "deploy.env"
            if env_path.is_file():
                summary["deploy_env"] = redact_env(env_path.read_text(encoding="utf-8"))
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        if not args.compose_files:
            raise BackupRestoreError("plan 至少需要一个 --compose-file")
        plan = build_restore_plan(
            args.package,
            install_dir=args.install_dir,
            compose_files=[Path(item) for item in args.compose_files],
            env_file=args.env_file,
            project=args.project,
            target_db=args.target_db,
            rollback_dir=args.rollback_dir,
        )
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    except BackupRestoreError as exc:
        print(f"[ling-backup-restore] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
