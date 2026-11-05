"""``ling doctor`` / ``backup`` / ``restore-drill`` 的隔离回归测试。

复用 ``tests/test_cli_install.py`` 的 docker/compose stub 与 ``ws`` fixture：

- ``doctor``：在**真实安装**出来的记录上跑，断言分级（ok/warn/fail）、退出码、
  ``/readyz`` 302 不跟随、缺 ``.env`` 报 fail，以及**零写操作**（目录快照 +
  stub 调用日志里没有 run/up/pull/create/rm）。
- ``backup`` / ``restore-drill``：把 ``ling.py`` 复制到临时发行包布局
  （``deploy/cli/ling.py`` + ``deploy/backup/*.sh``），用只记录参数的 stub 脚本
  断言传给 ``backup.sh`` / ``restore_drill.sh`` 的 **env 与 argv**（同一组
  ``-f``、同一 ``--env-file``、同一项目名、隔离库以 ``_drill`` 结尾），并验证
  产物不完整 / 脚本非 0 时本命令非 0。

这里证明的是 CLI 的门禁与参数传递，**不是**真实 Docker/备份行为；真实
backup.sh/restore_drill.sh 的行为由 ``tests/test_deploy_backup_*.py`` 覆盖。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.test_cli_install import (
    COMMIT,
    HEAD,
    OTHER_COMMIT,
    OTHER_HEX,
    OTHER_REF,
    REPO_ROOT,
    TARGET,
    TARGET_HEX,
    UPDATER_TARGET,
    Workspace,
    _config_json,
    _Handler,
    _HTTPServer,
    _load_module,
    _other_updater_uid,
    _shared_group_gid,
    _write_executable,
    compose_subcommand,
    late_readyz,
    run_install,
    ws,  # noqa: F401 - pytest 按名字注册该 fixture；ops 用 getfixturevalue 取
)

LING_PY = REPO_ROOT / "deploy" / "cli" / "ling.py"


# ---------------------------------------------------------------- 脚本 stub


_BACKUP_STUB = r'''#!/usr/bin/env python3
import hashlib
import json
import os
import pathlib
import sys
import time

LOG = pathlib.Path(os.environ["STUB_OPS_LOG"])
out_dir = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else None
record = {
    "script": "backup",
    "argv": sys.argv[1:],
    "env": {
        key: value
        for key, value in os.environ.items()
        if key.startswith("LING_") and key != "LING_ROOT_KEY"
    },
    "root_key_set": bool(os.environ.get("LING_ROOT_KEY")),
    "cwd": os.getcwd(),
}
with LOG.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(record, sort_keys=True) + "\n")

code = int(os.environ.get("STUB_BACKUP_EXIT", "0"))
if code != 0:
    print("[stub backup] 按测试要求失败", file=sys.stderr)
    sys.exit(code)
if out_dir is None:
    print("[stub backup] 缺 OUT_DIR", file=sys.stderr)
    sys.exit(2)

out_dir.mkdir(parents=True, exist_ok=True)
mode = os.environ.get("STUB_BACKUP_MODE", "complete")
stamp = os.environ.get("STUB_BACKUP_STAMP", "20261104-030000")
created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
dump = out_dir / ("ling-%s.dump" % stamp)
dump.write_bytes(("PGDMP-stub-" + stamp).encode())
tomb = out_dir / ("tombstones-%s.txt" % stamp)
tomb.write_text("", encoding="utf-8")
keys_dir = out_dir / "keys"
keys_dir.mkdir(exist_ok=True)
root_key = keys_dir / "root.key"
root_key.write_bytes(("stub-root-key-" + stamp).encode())
media = out_dir / ("media-%s.tar" % stamp)
media.write_bytes(("stub-media-" + stamp).encode())

fields = {
    "created_at": created_at,
    "mode": "compose",
    "dump": dump.name,
    "dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
    "tombstones": tomb.name,
    "tombstones_state": "ok",
    "tombstones_sha256": hashlib.sha256(tomb.read_bytes()).hexdigest(),
    "keys_root": "present",
    "keys_root_file": "keys/root.key",
    "keys_root_source": "file",
    "keys_root_sha256": hashlib.sha256(root_key.read_bytes()).hexdigest(),
    "keys_env_set": "true" if os.environ.get("LING_ROOT_KEY") else "false",
    "media": media.name,
    "media_state": "present",
    "media_sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
}
if mode == "no_dump_sha":
    fields.pop("dump_sha256")
elif mode == "partial_media":
    fields["media_state"] = "partial"
manifest = out_dir / ("manifest-%s.txt" % stamp)
manifest.write_text(
    "\n".join("%s=%s" % (key, value) for key, value in fields.items()) + "\n",
    encoding="utf-8",
)
print("[stub backup] 完成：%s" % dump.name)
sys.exit(0)
'''

_DRILL_STUB = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys

LOG = pathlib.Path(os.environ["STUB_OPS_LOG"])
record = {
    "script": "restore_drill",
    "argv": sys.argv[1:],
    "env": {
        key: value
        for key, value in os.environ.items()
        if key.startswith("LING_") and key != "LING_ROOT_KEY"
    },
}
with LOG.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(record, sort_keys=True) + "\n")
target = sys.argv[2] if len(sys.argv) > 2 else ""
if not target.endswith("_drill"):
    print("[stub drill] 目标库名不是 _drill：%s" % target, file=sys.stderr)
    sys.exit(2)
print("[stub drill] 恢复到隔离库 %s" % target)
sys.exit(int(os.environ.get("STUB_DRILL_EXIT", "0")))
'''

_CRONTAB_STUB = r'''#!/usr/bin/env python3
import os
import sys

if os.environ.get("STUB_CRON_MISSING") == "1":
    sys.exit(1)
print(os.environ.get("STUB_CRON_LINE", "30 4 * * * /bin/true # no backup"))
sys.exit(0)
'''


# ---------------------------------------------------------------- fixture


@dataclass
class Ops:
    ws: Workspace
    ling_py: Path
    ops_log: Path

    def run(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float = 120.0,
        reset_ops_log: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        self.ws.reset()
        if reset_ops_log:
            self.ops_log.write_text("", encoding="utf-8")
        merged = dict(self.ws.env)
        merged["STUB_OPS_LOG"] = str(self.ops_log)
        merged.update(env or {})
        return subprocess.run(
            argv,
            cwd=str(self.ws.root),
            env=merged,
            text=True,
            capture_output=True,
            timeout=timeout,
        )

    def _records(self, script: str) -> list[dict[str, Any]]:
        text = self.ops_log.read_text(encoding="utf-8")
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
        return [record for record in records if record.get("script") == script]

    def backup_calls(self) -> list[dict[str, Any]]:
        return self._records("backup")

    def drill_calls(self) -> list[dict[str, Any]]:
        return self._records("restore_drill")


@pytest.fixture()
def ops(request: pytest.FixtureRequest) -> Ops:
    workspace: Workspace = request.getfixturevalue("ws")
    cli_dir = workspace.root / "deploy" / "cli"
    cli_dir.mkdir(parents=True, exist_ok=True)
    ling_py = cli_dir / "ling.py"
    shutil.copyfile(LING_PY, ling_py)

    backup_scripts = workspace.root / "deploy" / "backup"
    backup_scripts.mkdir(parents=True, exist_ok=True)
    _write_executable(backup_scripts / "backup.sh", _BACKUP_STUB, python=True)
    _write_executable(backup_scripts / "restore_drill.sh", _DRILL_STUB, python=True)

    stub_bin = Path(workspace.env["PATH"].split(os.pathsep)[0])
    _write_executable(stub_bin / "crontab", _CRONTAB_STUB, python=True)

    ops_log = workspace.root / "ops.log"
    ops_log.write_text("", encoding="utf-8")
    return Ops(ws=workspace, ling_py=ling_py, ops_log=ops_log)


@contextlib.contextmanager
def serving(workspace: Workspace) -> Any:
    """在安装端口上提供 /readyz（`/v1/*` 如实 401，安装结束后端口已释放）。"""
    server = _HTTPServer(("127.0.0.1", workspace.port), _Handler)
    server.http_state = workspace.http
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def install_ok(ops: Ops) -> None:
    with late_readyz(ops.ws) as server:
        result = run_install(ops.ws)
        assert server.wait_ready(10.0)
    assert result.returncode == 0, (result.stdout, result.stderr)


def state_paths(workspace: Workspace) -> tuple[Path, Path, Path]:
    """安装记录里的 (备份目录, compose 主文件, 受限 env)，均为解析后的绝对路径。"""
    state = workspace.install_state()
    backup_dir = Path(state["backup"]["dir"])
    compose_file = Path(state["layout"]["compose"]["files"][0])
    env_file = Path(state["layout"]["env_file"])
    return backup_dir, compose_file, env_file


def doctor_argv(ops: Ops) -> list[str]:
    return [
        sys.executable,
        str(ops.ling_py),
        "doctor",
        "--dir",
        str(ops.ws.install_dir),
        "--ready-timeout",
        "5",
        "--min-free-bytes",
        "1",
    ]


def backup_argv(ops: Ops) -> list[str]:
    return [
        sys.executable,
        str(ops.ling_py),
        "backup",
        "--install-dir",
        str(ops.ws.install_dir),
    ]


def drill_argv(ops: Ops, extra: list[str] | None = None) -> list[str]:
    argv = [
        sys.executable,
        str(ops.ling_py),
        "restore-drill",
        "--install-dir",
        str(ops.ws.install_dir),
    ]
    argv.extend(extra or [])
    return argv


def cron_line(ops: Ops) -> str:
    backup_dir, _compose_file, _env_file = state_paths(ops.ws)
    return (
        "30 4 * * * LING_BACKUP_VIA_COMPOSE=1 "
        f"LING_BACKUP_DIR='{backup_dir}' "
        f"'{ops.ws.root}/deploy/backup/backup.sh' '{backup_dir}'"
    )


def snapshot(*roots: Path) -> dict[str, tuple[int, int, str]]:
    files: dict[str, tuple[int, int, str]] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            info = path.stat()
            files[str(path)] = (
                info.st_mode,
                info.st_size,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return files


def assert_no_write_calls(calls: list[str]) -> None:
    for line in calls:
        assert not line.startswith("docker run"), line
        assert not line.startswith("docker create"), line
        assert not line.startswith("docker rm"), line
        assert not line.startswith("docker volume rm"), line
        if line.startswith("docker compose"):
            verb = compose_subcommand(line)
            assert verb not in {
                "up",
                "run",
                "pull",
                "down",
                "create",
                "rm",
                "start",
                "stop",
                "restart",
                "pause",
                "unpause",
                "build",
            }, line


# ---------------------------------------------------------------- doctor


def test_doctor_healthy_reports_ok_for_every_check(ops: Ops) -> None:
    install_ok(ops)
    backup = ops.run(backup_argv(ops))
    assert backup.returncode == 0, (backup.stdout, backup.stderr)

    with serving(ops.ws):
        result = ops.run(doctor_argv(ops), env={"STUB_CRON_LINE": cron_line(ops)})
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "[fail]" not in result.stdout
    assert "[warn]" not in result.stdout, result.stdout
    for label in (
        "Docker daemon",
        "Docker Compose",
        "安装目录",
        "受限 .env",
        "安装状态",
        "Compose 文件组",
        "容器 api",
        "运行镜像摘要",
        "/readyz",
        # 版本/提交来自容器内进程证据（compose exec），不再打 `/v1/version`
        # （它挂 CurrentAuth，doctor 不持有凭据 ⇒ 以前在这里永远一条假 fail）。
        "运行容器自报提交",
        "运行容器自报版本",
        "卷 app-data 身份",
        "卷 db-data 身份",
        "alembic_version vs 镜像 head",
        "最近备份",
        "恢复点数量",
        "备份 cron",
        "更新通道",
        "GHCR 可达性",
    ):
        assert f"[ok] {label}" in result.stdout, (label, result.stdout)
    assert "update.enabled=false" in result.stdout
    assert "本命令只读" in result.stdout


@pytest.mark.parametrize(
    "line",
    [
        "LING_UPDATE_ENABLED = 1",  # 等号两侧有空格
        "export LING_UPDATE_ENABLED=1",  # export 前缀
        'LING_UPDATE_ENABLED="1"',  # 成对引号
        "LING_UPDATE_ENABLED='1'",
    ],
)
def test_doctor_detects_update_switch_in_any_env_syntax(ops: Ops, line: str) -> None:
    """L1：开关检查必须是**按行解析后的键值**，不是子串匹配。

    旧实现 ``"LING_UPDATE_ENABLED=1" in env_text`` 发现不了 ``KEY = VALUE``、
    ``export`` 与带引号的写法；这四种都必须报 ``[fail] 更新通道``。
    """
    install_ok(ops)
    env_file = ops.ws.install_dir / ".env"
    env_file.write_text(
        env_file.read_text(encoding="utf-8") + line + "\n", encoding="utf-8"
    )

    with serving(ops.ws):
        result = ops.run(doctor_argv(ops), env={"STUB_CRON_LINE": cron_line(ops)})
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "[fail] 更新通道" in result.stdout, result.stdout
    assert "LING_UPDATE_ENABLED=1" in result.stdout


def test_doctor_ignores_commented_out_update_switch(ops: Ops) -> None:
    """注释里的 ``LING_UPDATE_ENABLED=1`` 不是开关；按行解析也不能把它当真。"""
    install_ok(ops)
    env_file = ops.ws.install_dir / ".env"
    env_file.write_text(
        env_file.read_text(encoding="utf-8") + "# LING_UPDATE_ENABLED=1\n",
        encoding="utf-8",
    )

    with serving(ops.ws):
        result = ops.run(doctor_argv(ops), env={"STUB_CRON_LINE": cron_line(ops)})
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "[ok] 更新通道" in result.stdout, result.stdout


def test_doctor_reports_update_switch_off_with_howto(ops: Ops) -> None:
    """开关是 0：显示 `update.enabled=false` + **打开方法**，且不说“尚未实现”。"""
    install_ok(ops)

    with serving(ops.ws):
        result = ops.run(doctor_argv(ops), env={"STUB_CRON_LINE": cron_line(ops)})
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "[ok] 更新通道" in result.stdout, result.stdout
    assert "update.enabled=false" in result.stdout
    assert "LING_UPDATE_ENABLED=0" in result.stdout
    assert "网页更新未启用" in result.stdout
    # 不是“不可用”，而是给出可执行的两条路。
    assert "--enable-updates" in result.stdout
    assert "LING_UPDATE_ENABLED 改成 1" in result.stdout
    combined = result.stdout + result.stderr
    assert "尚未实现" not in combined
    assert "执行器未实现" not in combined


def test_doctor_reports_update_switch_on(ops: Ops) -> None:
    """开关是 1（更新器已拉起）：`update.enabled=true`，不再说“未启用”。

    这里直接装成开启状态：`.env` 写 1 + 安装记录 update.enabled=true +
    更新器记录为已拉起 —— 三项一致时 doctor 只应报 ok。
    """
    with late_readyz(ops.ws) as server:
        result = run_install(
            ops.ws,
            extra=[
                "--updater-digest",
                UPDATER_TARGET,
                "--with-updater",
                "--enable-updates",
                "--docker-gid",
                "999",
            ],
        )
        assert server.wait_ready(10.0)
    assert result.returncode == 0, (result.stdout, result.stderr)
    # 开关开着时更新器**必须真在跑**（这份记录里 started=true，实况也要一致）。
    docker_state = ops.ws.state()
    docker_state["containers"]["updater"] = own_updater_container(ops)
    ops.ws.write_state(docker_state)
    (ops.ws.install_dir / "updater" / "state" / "status.json").write_text(
        json.dumps({"schema": 1, "protocol": 1}), encoding="utf-8"
    )

    with serving(ops.ws):
        result = ops.run(doctor_argv(ops), env={"STUB_CRON_LINE": cron_line(ops)})
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "[ok] 更新通道" in result.stdout, result.stdout
    assert "update.enabled=true" in result.stdout
    assert "LING_UPDATE_ENABLED=1" in result.stdout
    assert "网页更新未启用" not in result.stdout
    combined = result.stdout + result.stderr
    assert "尚未实现" not in combined
    assert "执行器未实现" not in combined


def test_doctor_reports_recorded_vs_live_digest_mismatch(ops: Ops) -> None:
    install_ok(ops)
    docker_state = ops.ws.state()
    docker_state["containers"]["api"]["image_id"] = "sha256:other-image"
    docker_state["containers"]["worker"]["image_id"] = "sha256:other-image"
    ops.ws.write_state(docker_state)

    with serving(ops.ws):
        result = ops.run(doctor_argv(ops), env={"STUB_CRON_LINE": cron_line(ops)})
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "[fail] 运行镜像摘要" in result.stdout
    assert TARGET_HEX in result.stdout and OTHER_HEX in result.stdout
    assert "[fail] 镜像源提交" in result.stdout
    assert "结论：存在 fail" in result.stdout


def test_doctor_does_not_follow_readyz_redirect(ops: Ops) -> None:
    install_ok(ops)
    ops.ws.http.readyz_redirect = True
    with serving(ops.ws):
        result = ops.run(doctor_argv(ops), env={"STUB_CRON_LINE": cron_line(ops)})
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "[fail] /readyz" in result.stdout
    assert "302" in result.stdout
    assert "重定向" in result.stdout
    assert "不跟随" in result.stdout


def test_doctor_reads_version_from_container_process(ops: Ops) -> None:
    """doctor 的提交/版本也走容器内进程证据，HTTP 只探 `/readyz`。

    `/v1/version` 挂 CurrentAuth、doctor 不持有凭据 ⇒ 以前在真机上它永远
    一条假 fail（退出码恒非 0）。stub 现在如实 401，所以退回 HTTP 就会红。
    """
    install_ok(ops)
    ops.ws.http.requests.clear()
    with serving(ops.ws):
        result = ops.run(doctor_argv(ops), env={"STUB_CRON_LINE": cron_line(ops)})
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "[ok] 运行容器自报提交" in result.stdout
    assert "[ok] 运行容器自报版本" in result.stdout
    assert set(ops.ws.http.requests) <= {"/readyz"}, ops.ws.http.requests
    assert [line for line in ops.ws.calls() if " exec -T api python -c " in line]


def test_doctor_reports_probe_failure(ops: Ops) -> None:
    """读不到容器内进程证据时报 fail，而不是编一个"一致"。"""
    install_ok(ops)
    with serving(ops.ws):
        result = ops.run(
            doctor_argv(ops),
            env={"STUB_CRON_LINE": cron_line(ops), "STUB_PROBE_EXIT": "1"},
        )
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "[fail] 运行容器自报版本/提交" in result.stdout
    assert "读不到" in result.stdout


def test_doctor_missing_env_is_a_fail(ops: Ops) -> None:
    install_ok(ops)
    (ops.ws.install_dir / ".env").unlink()
    with serving(ops.ws):
        result = ops.run(doctor_argv(ops), env={"STUB_CRON_LINE": cron_line(ops)})
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "[fail] 受限 .env" in result.stdout
    assert "缺失" in result.stdout


# ---------------------------------------------------------------- doctor / 端口来源


def readyz_row(stdout: str) -> str:
    rows = [line for line in stdout.splitlines() if "] /readyz：" in line]
    assert len(rows) == 1, stdout
    return rows[0]


def test_doctor_probes_live_port_without_install_record(ops: Ops) -> None:
    """缺陷 1（真机 ling-drill）：没有安装记录时必须探**实况发布端口**。

    真机：安装记录缺失、实况 6521，doctor 却探 6520（另一个部署/生产）并报
    ``[ok] /readyz``——把别人的健康当成了自己的。旧实现"没有记录就退回
    DEFAULT_PORT"且不说来源。
    """
    install_ok(ops)
    # 安装记录删掉后 `cron_line` 就没法从记录里取备份目录了，先算好。
    cron = cron_line(ops)
    (ops.ws.install_dir / "install-state.json").unlink()

    with serving(ops.ws):
        result = ops.run(doctor_argv(ops), env={"STUB_CRON_LINE": cron})

    row = readyz_row(result.stdout)
    assert "[ok] /readyz" in row, row
    assert f"127.0.0.1:{ops.ws.port}/readyz" in row, row
    assert "端口来自实况" in row, row
    assert "127.0.0.1:6520/readyz" not in row, row


def test_doctor_reports_recorded_vs_live_port_difference(ops: Ops) -> None:
    """记录 6520 / 实况 ws.port：按实况探，并**单独一行**报端口差异。

    这正是事故的形状：记录指向另一个端口，探活了别处部署却报"本部署 OK"。
    """
    install_ok(ops)
    state_file = ops.ws.install_dir / "install-state.json"
    state = ops.ws.install_state()
    state["listen"]["port"] = 6520
    state_file.write_text(json.dumps(state), encoding="utf-8")

    with serving(ops.ws):
        result = ops.run(doctor_argv(ops), env={"STUB_CRON_LINE": cron_line(ops)})

    row = readyz_row(result.stdout)
    assert "[ok] /readyz" in row, row
    assert f"127.0.0.1:{ops.ws.port}/readyz" in row, row
    assert "端口来自实况" in row, row
    assert "127.0.0.1:6520/readyz" not in row, row

    diff_rows = [
        line for line in result.stdout.splitlines() if "宿主端口（记录 vs 实况）" in line
    ]
    assert len(diff_rows) == 1, result.stdout
    assert "[warn]" in diff_rows[0], diff_rows[0]
    assert "记录 6520" in diff_rows[0], diff_rows[0]
    assert f"实况 {ops.ws.port}" in diff_rows[0], diff_rows[0]


def test_doctor_marks_unverified_port_instead_of_ok(ops: Ops) -> None:
    """取不到实况（compose config 失败）：记录端口 200 也只能 warn + "未核对"。

    诊断工具宁可保守：拿记录里的端口探通了，只能说明"记录里那个目标活着"，
    不能证明是本部署——所以不许给 ok。
    """
    install_ok(ops)
    with serving(ops.ws):
        result = ops.run(
            doctor_argv(ops),
            env={"STUB_CRON_LINE": cron_line(ops), "STUB_CONFIG_EXIT": "1"},
        )

    row = readyz_row(result.stdout)
    assert "[warn] /readyz" in row, row
    assert "200" in row, row
    assert f"127.0.0.1:{ops.ws.port}/readyz" in row, row
    assert "端口来自安装记录" in row, row
    assert "未与实况核对" in row, row
    assert "compose config 失败" in row, row


# ---------------------------------------------------------------- doctor / Compose 版本


@pytest.mark.parametrize(
    ("version", "level", "needle"),
    [
        ("2.19.1", "warn", "较旧（建议 2.20+）"),
        ("2.20.0", "ok", "v2.20.0"),
        ("2.24.6", "ok", "v2.24.6"),
        # 真机 v5.3.1 曾被报成"较旧"：旧实现拿 5.3.1 的 minor=3 去比 20。
        ("5.3.1", "ok", "v5.3.1"),
        ("1.29.2", "fail", "需要 v2"),
        ("无法解析", "warn", "版本无法解析"),
    ],
)
def test_compose_version_classification(version: str, level: str, needle: str) -> None:
    """Compose 的 major 不一定等于 2：major ≥ 3 一律是**更新**，不是更旧。"""
    ling = _load_module("ling_cli_compose_version", LING_PY)
    row = ling.compose_version_row(version)
    assert row.level == level, row
    assert row.label == "Docker Compose", row
    assert needle in row.detail, row


def test_doctor_reports_compose_5_as_current(ops: Ops) -> None:
    """真实 doctor 行：v5.3.1 必须是 [ok]，不能出现"较旧"。"""
    install_ok(ops)
    with serving(ops.ws):
        result = ops.run(
            doctor_argv(ops),
            env={"STUB_CRON_LINE": cron_line(ops), "STUB_COMPOSE_VERSION": "5.3.1"},
        )
    assert "[ok] Docker Compose：v5.3.1" in result.stdout, result.stdout
    assert "较旧" not in result.stdout, result.stdout


# ---------------------------------------------------------------- doctor / compose 权限


def test_doctor_accepts_scoped_group_read_on_compose(ops: Ops) -> None:
    """非 root + 另一个 updater uid：compose 0640（共享组可读）是设计，不是过宽。

    ``compose_access_for_updater`` 走共享组路线时必须有组读位，否则更新器
    读不到；doctor 若按旧的 ``mode & 0o077`` 判，就会把这种**有意为之**的
    权限报成 fail，反而逼人改回 0600 把更新器弄坏。
    """
    gid = _shared_group_gid()
    with late_readyz(ops.ws) as server:
        result = run_install(
            ops.ws,
            env={
                "LING_UPDATER_UID": str(_other_updater_uid()),
                "LING_UPDATER_GID": str(gid),
            },
        )
        assert server.wait_ready(10.0)
    assert result.returncode == 0, (result.stdout, result.stderr)

    state = ops.ws.install_state()
    compose_file = Path(state["layout"]["compose"]["files"][0])
    mode = stat.S_IMODE(compose_file.stat().st_mode)
    assert mode in (0o600, 0o640), mode
    if os.geteuid() != 0:
        # 非 root 安装器 + 另一个 uid：只能走共享组路线，文件必须是 0640。
        assert mode == 0o640, (mode, compose_file.stat())

    with serving(ops.ws):
        doctor = ops.run(doctor_argv(ops), env={"STUB_CRON_LINE": cron_line(ops)})
    row = next(line for line in doctor.stdout.splitlines() if "Compose 文件组" in line)
    assert "[ok] Compose 文件组" in row, (row, doctor.stdout)


def test_doctor_performs_no_write_operations(ops: Ops) -> None:
    install_ok(ops)
    backup = ops.run(backup_argv(ops))
    assert backup.returncode == 0, (backup.stdout, backup.stderr)
    backup_dir, _compose_file, _env_file = state_paths(ops.ws)

    before = snapshot(ops.ws.install_dir, backup_dir, ops.ws.root)
    with serving(ops.ws):
        result = ops.run(
            doctor_argv(ops), env={"STUB_CRON_LINE": cron_line(ops)}, reset_ops_log=False
        )
    after = snapshot(ops.ws.install_dir, backup_dir, ops.ws.root)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert before == after, "doctor 修改了文件"
    assert_no_write_calls(ops.ws.calls())
    assert "docker run" not in "\n".join(ops.ws.calls())


# ---------------------------------------------------------------- doctor / 更新器


def install_with_updater(ops: Ops) -> None:
    with late_readyz(ops.ws) as server:
        result = run_install(
            ops.ws,
            extra=[
                "--updater-digest",
                UPDATER_TARGET,
                "--with-updater",
                "--docker-gid",
                "999",
            ],
        )
        assert server.wait_ready(10.0)
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_doctor_updater_row_is_ok_when_not_configured(ops: Ops) -> None:
    install_ok(ops)
    with serving(ops.ws):
        result = ops.run(doctor_argv(ops), env={"STUB_CRON_LINE": cron_line(ops)})
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "[warn] 更新器" not in result.stdout, result.stdout
    assert "[fail] 更新器" not in result.stdout, result.stdout
    assert "[ok] 更新器" in result.stdout
    assert "网页更新不可用" in result.stdout


def test_doctor_reports_updater_not_running(ops: Ops) -> None:
    """安装时用 --with-updater 拉起过、实况却没有容器：doctor 必须报告不一致。"""
    install_with_updater(ops)
    with serving(ops.ws):
        result = ops.run(doctor_argv(ops), env={"STUB_CRON_LINE": cron_line(ops)})
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "[fail] 更新器" in result.stdout
    assert "没有运行中的更新器" in result.stdout
    assert "ling-updater" in result.stdout
    # 只读诊断：不因为更新器没跑就去 up/run。
    assert_no_write_calls(ops.ws.calls())


def own_updater_container(
    ops: Ops,
    *,
    project: str = "ling-updater",
    image_ref: str = UPDATER_TARGET,
    image_id: str = "sha256:updater-image",
) -> dict[str, Any]:
    """`docker inspect` 形态的**本安装**更新器容器。

    真 Compose 打的 ``com.docker.compose.project.working_dir`` 是**共享代码目录**
    （所有实例都一样），真正区分实例的是挂载源里的安装根路径；doctor / 安装冲突
    守卫都按这两样判归属。所以这里必须照实给出挂载，否则等价于"可能是别人的"。
    """
    return {
        "id": "c-updater",
        "name": project + "-updater-1",
        "project": project,
        "running": True,
        "image_id": image_id,
        "image_ref": image_ref,
        "health": "healthy",
        "labels": {
            "com.docker.compose.project.working_dir": "/srv/ling-code/deploy/updater",
        },
        "mounts": [
            {
                "Type": "bind",
                "Source": str(ops.ws.install_dir / "updater" / ".env"),
                "Destination": "/srv/ling/.env",
            },
            {
                "Type": "bind",
                "Source": str(ops.ws.install_dir / "compose"),
                "Destination": "/srv/ling/compose",
            },
        ],
    }


def test_doctor_checks_running_updater_digest_and_protocol(ops: Ops) -> None:
    install_with_updater(ops)
    docker_state = ops.ws.state()
    docker_state["containers"]["updater"] = own_updater_container(ops)
    ops.ws.write_state(docker_state)
    (ops.ws.install_dir / "updater" / "state" / "status.json").write_text(
        json.dumps({"schema": 1, "protocol": 1}), encoding="utf-8"
    )

    with serving(ops.ws):
        result = ops.run(doctor_argv(ops), env={"STUB_CRON_LINE": cron_line(ops)})
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "[ok] 更新器" in result.stdout
    assert UPDATER_TARGET in result.stdout
    assert "协议 1" in result.stdout


def test_doctor_reports_updater_digest_mismatch(ops: Ops) -> None:
    install_with_updater(ops)
    docker_state = ops.ws.state()
    docker_state["containers"]["updater"] = own_updater_container(
        ops,
        image_ref=f"ghcr.io/qsbb/ling-updater@sha256:{'d' * 64}",
        image_id="sha256:other-updater",
    )
    ops.ws.write_state(docker_state)
    (ops.ws.install_dir / "updater" / "state" / "status.json").write_text(
        json.dumps({"schema": 1, "protocol": 1}), encoding="utf-8"
    )

    with serving(ops.ws):
        result = ops.run(doctor_argv(ops), env={"STUB_CRON_LINE": cron_line(ops)})
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "[fail] 更新器" in result.stdout
    assert "≠" in result.stdout


def test_doctor_fails_when_updater_project_belongs_to_another_install(
    ops: Ops,
) -> None:
    """同名项目里的容器指向**别的安装** → fail（改前会按镜像/协议判成 ok）。

    这种形状正是"两个实例静默互相接管"：`ling-updater` 是实例 A 的，但 B 的
    doctor 只看项目名与 digest，会把 A 的更新器当自己的。归属判据必须先生效。
    """
    install_with_updater(ops)
    other_dir = ops.ws.root / "another-install"
    docker_state = ops.ws.state()
    docker_state["containers"]["updater"] = {
        **own_updater_container(ops),
        "labels": {
            "com.docker.compose.project.working_dir": str(other_dir / "deploy" / "updater")
        },
        "mounts": [
            {
                "Type": "bind",
                "Source": str(other_dir / "updater" / ".env"),
                "Destination": "/srv/ling/.env",
            }
        ],
    }
    ops.ws.write_state(docker_state)
    (ops.ws.install_dir / "updater" / "state" / "status.json").write_text(
        json.dumps({"schema": 1, "protocol": 1}), encoding="utf-8"
    )

    with serving(ops.ws):
        result = ops.run(doctor_argv(ops), env={"STUB_CRON_LINE": cron_line(ops)})
    assert result.returncode != 0, (result.stdout, result.stderr)
    row = next(line for line in result.stdout.splitlines() if "更新器：" in line)
    assert "[fail] 更新器" in row, row
    assert "不属于本安装" in row, row
    assert str(other_dir) in row, row
    assert_no_write_calls(ops.ws.calls())


def test_doctor_and_status_use_the_recorded_updater_project(ops: Ops) -> None:
    """记录里有 updater.project → doctor/status 用它，**不**重新派生。

    应用项目名与更新器项目名一旦不再由同一个公式决定（例如记录来自改过项目名
    的部署），重新派生会让命令去核对另一个项目：看起来一切正常，实际指向别人的
    更新器。优先级必须是"记录 > 派生"。
    """
    install_with_updater(ops)
    state_path = ops.ws.install_dir / "install-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["project"] == "ling"
    state["updater"]["project"] = "custom-updater"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    docker_state = ops.ws.state()
    docker_state["containers"]["updater"] = own_updater_container(
        ops, project="custom-updater"
    )
    ops.ws.write_state(docker_state)
    (ops.ws.install_dir / "updater" / "state" / "status.json").write_text(
        json.dumps({"schema": 1, "protocol": 1}), encoding="utf-8"
    )

    with serving(ops.ws):
        doctor = ops.run(doctor_argv(ops), env={"STUB_CRON_LINE": cron_line(ops)})
    assert doctor.returncode == 0, (doctor.stdout, doctor.stderr)
    assert "[ok] 更新器" in doctor.stdout, doctor.stdout
    assert "项目 custom-updater 运行中" in doctor.stdout, doctor.stdout
    calls = "\n".join(ops.ws.calls())
    assert "label=com.docker.compose.project=custom-updater" in calls, calls
    assert "label=com.docker.compose.project=ling-updater" not in calls, calls

    with serving(ops.ws):
        status = ops.run(status_argv(ops))
    assert status.returncode == 0, (status.stdout, status.stderr)
    assert (
        "更新器项目：custom-updater（来自安装记录 updater.project）" in status.stdout
    ), status.stdout
    assert "更新器项目：ling-updater" not in status.stdout, status.stdout


# ---------------------------------------------------------------- backup


def test_backup_uses_install_compose_group(ops: Ops) -> None:
    install_ok(ops)
    backup_dir, compose_file, env_file = state_paths(ops.ws)
    result = ops.run(backup_argv(ops))
    assert result.returncode == 0, (result.stdout, result.stderr)

    calls = ops.backup_calls()
    assert len(calls) == 1, calls
    call = calls[0]
    assert call["argv"] == [str(backup_dir)]
    assert call["root_key_set"] is True
    env = call["env"]
    assert env["LING_BACKUP_VIA_COMPOSE"] == "1"
    assert env["LING_COMPOSE_PROJECT"] == "ling"
    assert env["LING_COMPOSE_FILE"] == str(compose_file)
    assert env["LING_COMPOSE_ENV_FILE"] == str(env_file)
    assert "LING_COMPOSE_OVERRIDE" not in env
    assert "LING_DATABASE_URL" not in env
    assert "备份产物校验" in result.stdout
    assert "结论：备份完成" in result.stdout
    assert backup_dir.is_dir()


def test_backup_reports_incomplete_manifest(ops: Ops) -> None:
    install_ok(ops)
    result = ops.run(backup_argv(ops), env={"STUB_BACKUP_MODE": "no_dump_sha"})
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert result.returncode == 1
    assert "备份产物不完整" in result.stdout
    assert "dump_sha256" in result.stdout
    assert "可恢复的恢复点" in result.stdout


def test_backup_propagates_script_failure(ops: Ops) -> None:
    install_ok(ops)
    result = ops.run(backup_argv(ops), env={"STUB_BACKUP_EXIT": "5"})
    assert result.returncode == 5, (result.stdout, result.stderr)
    assert "备份失败" in result.stdout


# ---------------------------------------------------------------- restore-drill


def test_restore_drill_uses_latest_backup_and_drill_db(ops: Ops) -> None:
    install_ok(ops)
    backup = ops.run(backup_argv(ops))
    assert backup.returncode == 0, (backup.stdout, backup.stderr)
    backup_dir, compose_file, env_file = state_paths(ops.ws)

    result = ops.run(drill_argv(ops))
    assert result.returncode == 0, (result.stdout, result.stderr)

    calls = ops.drill_calls()
    assert len(calls) == 1, calls
    call = calls[0]
    assert len(call["argv"]) == 2
    dump = Path(call["argv"][0])
    assert dump.parent == backup_dir
    assert dump.name.startswith("ling-") and dump.name.endswith(".dump")
    assert call["argv"][1].endswith("_drill")
    assert call["argv"][1] == "ling_drill"
    env = call["env"]
    assert env["LING_BACKUP_VIA_COMPOSE"] == "1"
    assert env["LING_COMPOSE_PROJECT"] == "ling"
    assert env["LING_COMPOSE_FILE"] == str(compose_file)
    assert env["LING_COMPOSE_ENV_FILE"] == str(env_file)
    assert "恢复演练通过" in result.stdout


def test_restore_drill_propagates_failure(ops: Ops) -> None:
    install_ok(ops)
    backup = ops.run(backup_argv(ops))
    assert backup.returncode == 0, (backup.stdout, backup.stderr)
    result = ops.run(drill_argv(ops), env={"STUB_DRILL_EXIT": "7"})
    assert result.returncode == 7, (result.stdout, result.stderr)
    assert "恢复演练失败" in result.stdout


def test_restore_drill_rejects_non_drill_database(ops: Ops) -> None:
    install_ok(ops)
    backup = ops.run(backup_argv(ops))
    assert backup.returncode == 0, (backup.stdout, backup.stderr)
    result = ops.run(drill_argv(ops, ["--db", "ling"]))
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "_drill" in result.stderr
    assert ops.drill_calls() == []


# ---------------------------------------------------------------- status / 端口来源
#
# 缺陷 2（真机 ling-drill 的同型病）：`ling status` 还是"记录 → DEFAULT_PORT"
# 回退且**不标来源**。compose config 失败 + 没有安装记录时，它会静默去探生产
# 端口 6520，把别人的 200 当成本部署的健康。修法与 doctor 相同：复用
# `api_probe_target`（同一个 helper），非实况值必须标注"未与实况核对"，且 200
# 只能算**警告**，不算 OK。


def status_argv(ops: Ops) -> list[str]:
    return [
        sys.executable,
        str(ops.ling_py),
        "status",
        "--dir",
        str(ops.ws.install_dir),
        "--ready-timeout",
        "5",
    ]


def readyz_status_row(stdout: str) -> str:
    """取 status 的 /readyz 三行块（标记行 + 记录 + 实况），URL 在"实况"行里。"""
    lines = stdout.splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith("[") and "] /readyz" in line:
            return "\n".join(lines[index : index + 3])
    raise AssertionError(stdout)


def test_status_default_port_fallback_is_labeled_unverified(ops: Ops) -> None:
    """compose config 失败 + 无记录：探 6520 必须写明"默认值，未与实况核对"。

    改前：`status` 把 6520 当成本部署的端口，行里没有任何来源标注（200 时甚至
    是 `[OK  ]`）。改后：来源写死在行里，且 200 也只能是警告。
    """
    install_ok(ops)
    (ops.ws.install_dir / "install-state.json").unlink()

    result = ops.run(status_argv(ops), env={"STUB_CONFIG_EXIT": "1"})

    row = readyz_status_row(result.stdout)
    assert "[OK  ] /readyz" not in row, row
    assert "127.0.0.1:6520/readyz" in row, row
    assert "端口来自默认值" in row, row
    assert "未与实况核对" in row, row
    assert "docker compose config 失败" in row, row
    # 没有安装记录时 status 仍然拒绝给出"一致"结论（退出码 2）。
    assert result.returncode == 2, (result.stdout, result.stderr)


def test_status_recorded_port_fallback_is_warn_not_ok(ops: Ops) -> None:
    """取不到实况（compose config 失败）：记录端口 200 也只能 warn。

    与 ``test_doctor_marks_unverified_port_instead_of_ok`` 同一条规则：拿记录里
    的端口探通了，只能说明"记录里那个目标活着"，不能证明是本部署。
    """
    install_ok(ops)

    with serving(ops.ws):
        result = ops.run(status_argv(ops), env={"STUB_CONFIG_EXIT": "1"})

    row = readyz_status_row(result.stdout)
    assert "[警告] /readyz" in row, row
    assert "200" in row, row
    assert f"127.0.0.1:{ops.ws.port}/readyz" in row, row
    assert "端口来自安装记录" in row, row
    assert "未与实况核对" in row, row
    assert "未与实况核对，200 只算警告" in row, row
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "警告，不按 OK 处理" in result.stdout, result.stdout


def test_status_shows_update_enabled_and_howto(ops: Ops) -> None:
    """status 必须显示 update.enabled 当前值；0 时给出**具体怎么开**。"""
    install_ok(ops)

    with serving(ops.ws):
        result = ops.run(status_argv(ops))
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "[OK  ] 网页一键更新（update.enabled）" in result.stdout, result.stdout
    assert "update.enabled=false" in result.stdout or "0（未启用）" in result.stdout
    assert "LING_UPDATE_ENABLED" in result.stdout
    assert "--enable-updates" in result.stdout
    assert "尚未实现" not in result.stdout


def test_status_shows_update_enabled_true(ops: Ops) -> None:
    """开关是 1（实况 compose 模型解析出 1）：status 说已启用，不说“未启用”。"""
    with late_readyz(ops.ws) as server:
        result = run_install(
            ops.ws,
            extra=[
                "--updater-digest",
                UPDATER_TARGET,
                "--with-updater",
                "--enable-updates",
                "--docker-gid",
                "999",
            ],
        )
        assert server.wait_ready(10.0)
    assert result.returncode == 0, (result.stdout, result.stderr)

    with serving(ops.ws):
        result = ops.run(status_argv(ops))
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "[OK  ] 网页一键更新（update.enabled）" in result.stdout, result.stdout
    assert "1（已启用）" in result.stdout
    assert "**已启用**" in result.stdout
    assert "尚未实现" not in result.stdout


def test_status_uses_live_port_and_flags_stale_record(ops: Ops) -> None:
    """记录 6520 / 实况 ws.port：按实况探，并单独一行警告记录过期（与 doctor 同口径）。"""
    install_ok(ops)
    state_file = ops.ws.install_dir / "install-state.json"
    state = ops.ws.install_state()
    state["listen"]["port"] = 6520
    state_file.write_text(json.dumps(state), encoding="utf-8")

    with serving(ops.ws):
        result = ops.run(status_argv(ops))

    row = readyz_status_row(result.stdout)
    assert "[OK  ] /readyz" in row, row
    assert f"127.0.0.1:{ops.ws.port}/readyz" in row, row
    assert "端口来自实况" in row, row
    assert "127.0.0.1:6520/readyz" not in row, row

    port_rows = [
        index
        for index, line in enumerate(result.stdout.splitlines())
        if "宿主端口（记录 vs 实况）" in line
    ]
    assert len(port_rows) == 1, result.stdout
    block = "\n".join(result.stdout.splitlines()[port_rows[0] : port_rows[0] + 3])
    assert "[警告]" in block, block
    assert "记录 6520" in block, block
    assert f"实况 {ops.ws.port}" in block, block
    assert result.returncode == 0, (result.stdout, result.stderr)


# ---------------------------------------------------------------- adopt
#
# 设计 §5.2：源码模式 → 镜像模式是手工迁移，迁移完成后**没有** install-state.json。
# `ling adopt` 把现存部署的事实只读登记成安装记录：核对一致才写；任何一项核对
# 不上就拒绝（写错基线比不写更糟）。


def adopt_argv(ops: Ops, extra: list[str] | None = None) -> list[str]:
    argv = [
        sys.executable,
        str(ops.ling_py),
        "adopt",
        "--install-dir",
        str(ops.ws.install_dir),
        "--ready-timeout",
        "5",
    ]
    argv.extend(extra or [])
    return argv


def strip_state(ops: Ops) -> dict[str, Any]:
    """删掉 install 写的记录，模拟"手工迁移后没有安装记录"的现场。"""
    path = ops.ws.install_dir / "install-state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    path.unlink()
    return state


def test_adopt_registers_consistent_deployment(ops: Ops) -> None:
    """一致的健康部署：adopt 成功，写出与 install 同一 schema 的 adopted 记录。"""
    install_ok(ops)
    install_state = strip_state(ops)
    env_file = ops.ws.install_dir / ".env"
    compose_file = ops.ws.install_dir / "compose" / "compose.image.yaml"
    env_before = env_file.read_bytes()
    compose_before = compose_file.read_bytes()

    with serving(ops.ws):
        result = ops.run(adopt_argv(ops))

    assert result.returncode == 0, (result.stdout, result.stderr)
    state_file = ops.ws.install_dir / "install-state.json"
    assert state_file.is_file()
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    state = ops.ws.install_state()

    assert state["adopted"] is True
    assert state["adopted_at"]
    assert state["status"] == "complete"
    # 无法从实况推断的项不许编：started_at 等按 unknown 记。
    assert state["started_at"] == "unknown"
    assert state["finished_at"] == "unknown"
    assert state["installer"]["version"] == "unknown"
    assert {
        item["field"] for item in state["unverified"]
    } >= {"started_at", "migration.attempted", "backup.cron_owner"}

    # 与 ling install 同一 schema：顶层键一致（adopt 只额外加 adopted/adopted_at/
    # unverified），且 adopt 采集到的每个 section 的字段集合与 install 完全相同。
    # install 的 checks.disk/port/registry 等**预检证据** adopt 没有做，不伪造。
    assert set(install_state) <= set(state)
    assert set(state) - set(install_state) == {"adopted", "adopted_at", "unverified"}
    for section in (
        "image",
        "listen",
        "admin",
        "database",
        "migration",
        "backup",
        "update",
        "updater",
    ):
        assert set(install_state[section]) == set(state[section]), section
    for key in ("app-data", "db-data"):
        assert set(install_state["volumes"][key]) == set(state["volumes"][key]), key
    for service in ("api", "worker", "db"):
        assert (
            set(install_state["containers"][service])
            == set(state["containers"][service])
        ), service
    assert set(install_state["layout"]["compose"]) == set(state["layout"]["compose"])
    assert set(install_state["layout"]["updater"]) == set(state["layout"]["updater"])
    assert set(install_state["checks"]["compose"]) == set(state["checks"]["compose"])

    # 采集到的实况。
    assert state["image"]["requested_digest"] == TARGET
    assert state["image"]["index_digest_hex"] == TARGET_HEX
    assert state["image"]["platform_digest"] == TARGET_HEX
    assert state["image"]["image_id"] == "sha256:target-image"
    assert state["image"]["source_commit"] == COMMIT
    assert state["image"]["source_commit_source"] == "image+container"
    assert state["result"]["readyz_http"] == 200
    assert state["result"]["version"] == "9.9.9"
    assert state["result"]["version_commit"] == COMMIT
    assert state["database"]["alembic_version"] == HEAD
    assert state["database"]["target_head"] == HEAD
    assert state["listen"]["port"] == ops.ws.port
    assert state["volumes"]["app-data"]["name"] == "ling_app-data"
    assert state["volumes"]["db-data"]["name"] == "ling_db-data"
    assert (
        state["containers"]["api"]["image_id"]
        == state["containers"]["worker"]["image_id"]
        == "sha256:target-image"
    )
    assert state["containers"]["api"]["commit"] == COMMIT
    assert state["update"]["enabled"] is False
    assert state["updater"]["started"] is False

    # 只读：不碰 compose/容器/镜像，也不动 .env 与 compose 文件一个字节。
    assert env_file.read_bytes() == env_before
    assert compose_file.read_bytes() == compose_before
    assert "LING_UPDATE_ENABLED=1" not in env_file.read_text(encoding="utf-8")
    assert_no_write_calls(ops.ws.calls())
    assert [
        line for line in ops.ws.calls() if "compose" in line and "config --format json" in line
    ], ops.ws.calls()

    assert "adopted" in result.stdout
    assert "ling doctor" in result.stdout
    assert "ling backup" in result.stdout
    assert "更新已启用" not in result.stdout


def test_adopt_refuses_existing_record_without_force(ops: Ops) -> None:
    """已有记录：拒绝覆盖，旧文件一个字节都不动，也不留下 .bak。"""
    install_ok(ops)
    state_file = ops.ws.install_dir / "install-state.json"
    before = state_file.read_bytes()

    with serving(ops.ws):
        result = ops.run(adopt_argv(ops))

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "安装记录已存在" in result.stderr
    assert "ling adopt --force" in result.stderr
    assert state_file.read_bytes() == before
    assert not list(ops.ws.install_dir.glob("install-state.json.bak-*"))


def test_adopt_force_backs_up_old_record_first(ops: Ops) -> None:
    """--force：覆盖前先备份旧文件（0600、逐字节一致、打印路径与 sha256）。"""
    install_ok(ops)
    state_file = ops.ws.install_dir / "install-state.json"
    old = state_file.read_bytes()
    old_sha = hashlib.sha256(old).hexdigest()

    with serving(ops.ws):
        result = ops.run(adopt_argv(ops, ["--force"]))

    assert result.returncode == 0, (result.stdout, result.stderr)
    backups = list(ops.ws.install_dir.glob("install-state.json.bak-*"))
    assert len(backups) == 1, backups
    backup = backups[0]
    assert backup.read_bytes() == old
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == old_sha
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert str(backup) in result.stdout
    assert old_sha in result.stdout
    assert "旧记录已先备份" in result.stdout
    assert ops.ws.install_state()["adopted"] is True


def test_adopt_refuses_api_worker_different_image(ops: Ops) -> None:
    install_ok(ops)
    strip_state(ops)
    docker_state = ops.ws.state()
    docker_state["containers"]["worker"]["image_id"] = "sha256:other-image"
    docker_state["containers"]["worker"]["image_ref"] = OTHER_REF
    ops.ws.write_state(docker_state)

    with serving(ops.ws):
        result = ops.run(adopt_argv(ops))

    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "同一 image ID" in result.stderr
    assert "sha256:other-image" in result.stderr
    assert not (ops.ws.install_dir / "install-state.json").exists()


def test_adopt_refuses_compose_image_drift(ops: Ops) -> None:
    """compose 模型里的 api 镜像与运行容器的镜像引用不一致：拒绝（别写错摘要基线）。"""
    install_ok(ops)
    strip_state(ops)
    config = _config_json(ops.ws.port)
    config["services"]["api"]["image"] = OTHER_REF
    ops.ws.config_path.write_text(json.dumps(config), encoding="utf-8")

    with serving(ops.ws):
        result = ops.run(adopt_argv(ops))

    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "compose 模型里的 api 镜像引用" in result.stderr
    assert "不一致" in result.stderr
    assert not (ops.ws.install_dir / "install-state.json").exists()


def test_adopt_refuses_commit_disagreeing_with_oci_label(ops: Ops) -> None:
    install_ok(ops)
    strip_state(ops)

    with serving(ops.ws):
        result = ops.run(adopt_argv(ops), env={"STUB_PROBE_COMMIT": OTHER_COMMIT})

    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "容器内 LING_COMMIT" in result.stderr
    assert "OCI 标注" in result.stderr
    assert OTHER_COMMIT in result.stderr and COMMIT in result.stderr
    assert not (ops.ws.install_dir / "install-state.json").exists()


def test_adopt_refuses_readyz_not_200(ops: Ops) -> None:
    install_ok(ops)
    strip_state(ops)

    with serving(ops.ws):
        ops.ws.http.readyz_status = 503
        try:
            result = ops.run(adopt_argv(ops))
        finally:
            ops.ws.http.readyz_status = 200

    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "/readyz 不是恰好 200" in result.stderr
    assert "503" in result.stderr
    assert not (ops.ws.install_dir / "install-state.json").exists()


def test_adopt_refuses_unresolvable_volume(ops: Ops) -> None:
    install_ok(ops)
    strip_state(ops)
    docker_state = ops.ws.state()
    del docker_state["volumes"]["ling_app-data"]
    ops.ws.write_state(docker_state)

    with serving(ops.ws):
        result = ops.run(adopt_argv(ops))

    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "卷 app-data" in result.stderr
    assert "卷身份解析不出来" in result.stderr
    assert not (ops.ws.install_dir / "install-state.json").exists()


def test_adopt_refuses_to_guess_backup_dir(ops: Ops) -> None:
    """没有 --backup-dir、也没有更新器 .env：adopt 拒绝，不猜备份目录。"""
    install_ok(ops)
    strip_state(ops)
    (ops.ws.install_dir / "updater" / ".env").unlink()

    with serving(ops.ws):
        result = ops.run(adopt_argv(ops))

    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "没有备份目录" in result.stderr
    assert "--backup-dir" in result.stderr
    assert not (ops.ws.install_dir / "install-state.json").exists()


def test_status_and_doctor_use_adopted_baseline(ops: Ops) -> None:
    """adopt 后 status/doctor 的基线核对变成真实核对；unknown 项不算 OK。"""
    install_ok(ops)
    strip_state(ops)
    with serving(ops.ws):
        adopt = ops.run(adopt_argv(ops))
    assert adopt.returncode == 0, (adopt.stdout, adopt.stderr)

    with serving(ops.ws):
        status = ops.run(status_argv(ops))
    assert status.returncode == 0, (status.stdout, status.stderr)
    for label in ("api 运行 image ID", "api 运行镜像摘要", "镜像源提交", "卷 app-data 身份"):
        rows = [line for line in status.stdout.splitlines() if label in line]
        assert rows and all("[OK  ]" in line for line in rows), (label, status.stdout)
    assert "未能核实（adopted" in status.stdout
    assert "[unknown] started_at" in status.stdout
    assert "adopt 现场登记" in status.stdout

    with serving(ops.ws):
        doctor = ops.run(doctor_argv(ops), env={"STUB_CRON_LINE": cron_line(ops)})
    assert doctor.returncode == 0, (doctor.stdout, doctor.stderr)
    for label in (
        "安装记录",
        "运行镜像摘要",
        "镜像源提交",
        "运行容器自报提交",
        "卷 app-data 身份",
        "卷 db-data 身份",
        "alembic_version vs 镜像 head",
    ):
        assert f"[ok] {label}" in doctor.stdout, (label, doctor.stdout)
    assert "[warn] 未核实 started_at" in doctor.stdout, doctor.stdout
    assert "unknown" in doctor.stdout
    assert "安装记录里没有" not in doctor.stdout, doctor.stdout
    assert "安装记录里没有摘要基线" not in doctor.stdout, doctor.stdout
    assert "安装记录里没有提交基线" not in doctor.stdout, doctor.stdout
    assert "安装记录里没有该卷基线" not in doctor.stdout, doctor.stdout


@pytest.mark.parametrize("command", ["backup", "restore-drill"])
def test_ops_commands_without_record_suggest_adopt(ops: Ops, command: str) -> None:
    """没有安装记录时，backup/restore-drill 的错误提示必须指向 `ling adopt`。"""
    install_ok(ops)
    (ops.ws.install_dir / "install-state.json").unlink()
    argv = (
        backup_argv(ops)
        if command == "backup"
        else drill_argv(ops)
    )
    result = ops.run(argv)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "没有安装记录" in result.stderr
    assert "ling adopt" in result.stderr
