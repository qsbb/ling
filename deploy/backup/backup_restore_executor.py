"""宿主执行器：消费 Web 写入的备份/恢复请求目录。

这个脚本按请求文件逐个执行（或只检查），适合放在宿主定时任务或 systemd 中。
请求状态写到同级的 status 目录，Web 的状态接口直接展示这些文件。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ling.backup_restore import preflight_package  # noqa: E402


def _write_status(status_dir: Path, request_id: str, payload: dict) -> None:
    status_dir.mkdir(parents=True, exist_ok=True)
    temp = status_dir / f".{request_id}.tmp"
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(status_dir / f"{request_id}.json")


def process_request(path: Path, status_dir: Path, *, apply: bool) -> None:
    request_id = path.stem
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        _write_status(status_dir, request_id, {"status": "failed", "error": str(exc)})
        return
    action = payload.get("action")
    package = Path(str(payload.get("package") or ""))
    if action == "preflight_import" or action == "confirm_restore":
        if not package.is_dir():
            _write_status(status_dir, request_id, {"status": "failed", "error": "包目录不存在"})
            return
        report = preflight_package(package)
        _write_status(
            status_dir,
            request_id,
            {
                "status": "ok" if report.ok else "failed",
                "action": action,
                "errors": report.errors,
                "warnings": report.warnings,
            },
        )
        return
    if action == "create_backup" and apply:
        command = [sys.executable, str(REPO_ROOT / "deploy" / "cli" / "ling.py"), "backup"]
        result = subprocess.run(command, capture_output=True, text=True)
        _write_status(
            status_dir,
            request_id,
            {"status": "ok" if result.returncode == 0 else "failed", "output": result.stdout[-4000:]},
        )
        return
    _write_status(
        status_dir,
        request_id,
        {"status": "applied" if apply else "queued", "action": action, "payload": payload},
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests-dir", required=True)
    parser.add_argument("--status-dir", required=True)
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
            process_request(path, status, apply=args.apply)
            processed.add(path.stem)
            if args.apply:
                path.unlink(missing_ok=True)
        if args.once or not args.apply:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
