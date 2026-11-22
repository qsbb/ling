"""宿主备份请求的文件校验和处理结果。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ling.backup_restore import SAFE_NAME, preflight_package  # noqa: E402


def _write_status(status_dir: Path, request_id: str, payload: dict) -> None:
    status_dir.mkdir(parents=True, exist_ok=True)
    temp = status_dir / f".{request_id}.tmp"
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(status_dir / f"{request_id}.json")


def process_request(path: Path, status_dir: Path, packages_dir: Path, *, apply: bool) -> None:
    request_id = path.stem
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        _write_status(status_dir, request_id, {"status": "failed", "error": "请求格式无效"})
        return
    action = payload.get("action")
    if action == "preflight_import":
        name = payload.get("package")
        if not isinstance(name, str) or not SAFE_NAME.fullmatch(name):
            _write_status(status_dir, request_id, {"status": "failed", "error": "包名无效"})
            return
        version = payload.get("target_version")
        report = preflight_package(
            packages_dir / name, target_version=version if isinstance(version, str) else None
        )
        _write_status(
            status_dir,
            request_id,
            {
                "status": "validated" if report.ok else "failed",
                "action": action,
                "errors": report.errors,
                "warnings": report.warnings,
            },
        )
        return
    error = {
        "create_backup": "宿主整包备份尚未接通",
        "confirm_restore": "宿主整包恢复尚未接通",
    }.get(action, "未知任务") if isinstance(action, str) else "未知任务"
    _write_status(
        status_dir,
        request_id,
        {"status": "failed" if apply else "not_executed", "action": action, "error": error},
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests-dir", required=True)
    parser.add_argument("--status-dir", required=True)
    parser.add_argument("--packages-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()
    requests = Path(args.requests_dir)
    status = Path(args.status_dir)
    processed: set[str] = set()
    while True:
        for path in sorted(requests.glob("*.json")):
            if path.stem in processed:
                continue
            process_request(path, status, args.packages_dir, apply=args.apply)
            processed.add(path.stem)
            if args.apply:
                path.unlink(missing_ok=True)
        if args.once or not args.apply:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
