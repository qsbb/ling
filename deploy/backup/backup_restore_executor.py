"""Host-side worker for backup and restore requests from the owner console."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from deploy.backup.restore_full import BackupError, Stack, create_snapshot, restore_snapshot
from deploy.cli.ling import LingError

from ling.backup_restore import SUFFIX, backup_path, write_channel_json


def run_job(stack: Stack, job: Path, archives: Path) -> None:
    request = json.loads(job.read_text(encoding="utf-8"))
    action = request.get("action")
    name = request.get("name")
    status = {"id": job.stem, "action": action, "name": name, "status": "running"}
    write_channel_json(job, status)
    try:
        if action == "create":
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            name = f"ling-{stamp}-{uuid4().hex[:8]}{SUFFIX}"
            create_snapshot(stack, archives / name, shared=True)
        elif action == "restore" and isinstance(name, str):
            restore_snapshot(stack, backup_path(archives, name))
        else:
            raise BackupError("备份任务无效")
    except (BackupError, LingError, OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as exc:
        status.update(status="failed", error=str(exc))
    else:
        status.update(status="succeeded", name=name)
    write_channel_json(job, status)
    print(f"{action} {status['status']} ({job.stem}): {status.get('error', name)}", flush=True)


def serve(stack: Stack) -> None:
    api = stack.config()["services"]["api"]
    requests = next(
        (item["source"] for item in api["volumes"] if item["target"] == "/run/ling-update/requests"),
        None,
    )
    if requests is None:
        raise BackupError("Compose 未配置 Ling 请求目录")
    requests_dir = Path(requests)
    group = int(api.get("group_add", [10001])[0])
    if not requests_dir.exists():
        requests_dir.mkdir(parents=True)
        os.chown(requests_dir, -1, group)
        requests_dir.chmod(0o2770)
    channel = requests_dir / "backup-restore"
    archives, jobs = channel / "archives", channel / "jobs"
    for path in (channel, archives, jobs):
        if not path.exists():
            path.mkdir(mode=0o770)
            if path.stat().st_gid != group:
                os.chown(path, -1, group)
            path.chmod(0o2770)

    with (channel / ".worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError("此 Compose 项目的备份程序已启动") from exc
        heartbeat = channel / "worker.json"
        for job in jobs.glob("*.json"):
            record = json.loads(job.read_text(encoding="utf-8"))
            if record.get("status") == "running":
                record.update(status="failed", error="宿主备份程序中断，请核对实例后重试")
                write_channel_json(job, record)
        print(f"整包备份程序已就绪：{channel}", flush=True)
        try:
            while True:
                write_channel_json(heartbeat, {"status": "ready"})
                for job in sorted(jobs.glob("*.json"), key=lambda path: path.stat().st_mtime):
                    if json.loads(job.read_text(encoding="utf-8")).get("status") == "queued":
                        run_job(stack, job, archives)
                        break
                else:
                    time.sleep(2)
        finally:
            heartbeat.unlink(missing_ok=True)
