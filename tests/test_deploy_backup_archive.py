"""``deploy/backup/backup.sh``（compose 模式）与 ``deploy/backup/restore_drill.sh``
的隔离回归测试。

回归的是 TASK29 判定的 Medium-1：**默认 compose 模式的备份只含 dump + 墓碑 +
manifest，既没有 ``app-data`` 卷里的 ``/data/root.key``，也没有 ``/data/media``
字节**。恢复到一个新卷后，DB 里的 provider 凭据全部解不开、``media_ref`` 全部悬空。

覆盖：

- compose 模式把 ``/data/root.key`` 与 ``/data/media`` 纳入备份，manifest 写
  ``keys_root=`` / ``media=`` 与各产物 ``sha256=``；未生成时**显式写 absent**；
- 失败路径必须非零退出，且不留半成品（``.part`` / 半截产物 / manifest）；
- **失败清理绝不能碰历史备份的 ``keys/root.key``**（cron 每天写同一个目录，这个
  文件没有日期戳，所以它同时就是前一天那份成功备份的密钥归档）；本次新建的才删，
  本次覆盖过的要把原字节放回去（compose 与直接模式各验一遍）；
- 日志里不能出现 DSN 口令或 root.key 内容（只允许哈希）；
- 恢复演练的媒体覆盖检查：``media_ref`` 指向的文件必须在媒体归档里；
  旧版备份（manifest 没有 ``media``）退化为人工核对提示。

不跑真实 Docker / Compose / pg_dump / pg_restore / psql：
``docker``、``cp``、``psql``、``pg_restore``、``pg_dump``、``tar`` 全部是**严格
fake**（未声明的 argv 直接以非零退出并留痕），备份目录用 ``tmp_path``。
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKUP_SH = REPO_ROOT / "deploy" / "backup" / "backup.sh"
DRILL_SH = REPO_ROOT / "deploy" / "backup" / "restore_drill.sh"

# 日志里绝不允许出现的值（root.key 内容就是"口令级"秘密）。
ROOT_KEY_BODY = "cm9vdC1rZXktc2VjcmV0LWJvZHktZm9yLXRlc3Q="
DSN_PASSWORD = "S3cr3t-Db-Pa55w0rd"

# 严格 fake docker：只认 backup.sh 真正用到的 argv；其余一律非零退出。
_DOCKER_FAKE = r"""#!/usr/bin/env bash
# 严格 fake：未声明的 compose 子命令/服务直接失败（不允许"默认成功"）。
set -euo pipefail
printf 'docker %s\n' "$*" >> "${DOCKER_CALLS:?}"

fail() {
  echo "fake-docker: $*" >&2
  exit 90
}

[[ $# -ge 1 ]] || fail "没有任何参数"
[[ "$1" == "compose" ]] || fail "只支持 compose 子命令，收到 $1"
shift

COMPOSE_FILE_ARG=""
COMPOSE_PROJECT_ARG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -f)
      [[ $# -ge 2 ]] || fail "-f 缺参数"
      COMPOSE_FILE_ARG="$2"
      shift 2
      ;;
    -p)
      [[ $# -ge 2 ]] || fail "-p 缺参数"
      COMPOSE_PROJECT_ARG="$2"
      shift 2
      ;;
    exec)
      shift
      break
      ;;
    *)
      fail "未声明的 compose 参数 $1"
      ;;
  esac
done

[[ -n "$COMPOSE_FILE_ARG" ]] || fail "compose 调用必须带 -f（否则项目名/文件不可控）"
# 本仓库约定：cron/执行器都会显式给项目名，避免按目录名推导。
[[ -n "$COMPOSE_PROJECT_ARG" ]] || fail "compose 调用必须带 -p（项目名不能靠目录名推导）"

[[ "${1:-}" == "-T" ]] || fail "exec 必须带 -T（不加会在 cron 里分配 TTY）"
shift

SERVICE="${1:-}"
shift
[[ -n "$SERVICE" ]] || fail "exec 缺服务名"
case "$SERVICE" in
  db)
    case "${1:-}" in
      pg_dump)
        shift
        [[ "${1:-}" == "--no-owner" ]] || fail "pg_dump 必须带 --no-owner"
        # 非空即可：backup.sh 只检查 -s，真正解析 dump 的是执行器。
        printf 'PGDMP-fake-dump-payload\n'
        ;;
      psql)
        shift
        # 形式：psql -U <user> -d <db> -tAc <sql>
        SQL=""
        for arg in "$@"; do SQL="$arg"; done
        case "$SQL" in
          *"select id from memories where retracted = true"*)
            # backup.sh 的导出查询（小写 select）
            cat "${TOMBSTONES_PSQL_PAYLOAD:?}"
            ;;
          *"SELECT id FROM memories WHERE retracted = true"*)
            # restore_drill.sh 的对比查询（大写 SELECT）；只在 drill 模式下放开
            [[ -n "${DRILL_MODE:-}" ]] || fail "非 drill 调用里出现了 drill 的墓碑查询"
            cat "${DRILL_RESTORED_TOMBSTONES:?}"
            ;;
          *"DROP DATABASE"*|*"CREATE DATABASE"*)
            [[ -n "${DRILL_MODE:-}" ]] || fail "非 drill 调用里出现了 DROP/CREATE DATABASE"
            exit 0
            ;;
          *media_evidence*)
            [[ -n "${DRILL_MODE:-}" ]] || fail "非 drill 调用里出现了 media_evidence 查询"
            cat "${DRILL_REFS_PAYLOAD:?}"
            ;;
          *"SELECT count(*) FROM"*)
            [[ -n "${DRILL_MODE:-}" ]] || fail "非 drill 调用里出现了计数查询"
            echo "3"
            ;;
          *)
            fail "未声明的 psql 查询: $SQL"
            ;;
        esac
        ;;
      pg_restore)
        shift
        # 恢复演练走 `pg_restore ... < dump`；这里只消费 stdin，不碰真实库。
        [[ -n "${DRILL_MODE:-}" ]] || fail "非 drill 调用里出现了 pg_restore"
        cat > /dev/null
        exit 0
        ;;
      *)
        fail "db 服务上未声明的命令 ${1:-}"
        ;;
    esac
    ;;
  api)
    # backup.sh 用**一条** `sh -c` 命令探测+读取：退出码 3 表示"文件/目录不存在"，
    # 其它非零表示真失败（容器没起来、权限不对……），两者绝不能混为一谈。
    [[ "${1:-}" == "sh" && "${2:-}" == "-c" ]] \
      || fail "api 上只声明了 sh -c '<脚本>'，收到 ${1:-}"
    SCRIPT="${3:-}"
    case "$SCRIPT" in
      *"/data/root.key"*)
        [[ "${ROOT_KEY_EXISTS:-0}" == "0" ]] || exit 3
        [[ "${ROOT_KEY_CAT_FAIL:-0}" == "0" ]] || exit 1
        cat "${ROOT_KEY_PAYLOAD:?}"
        ;;
      *"/data/media"*)
        [[ "${MEDIA_DIR_EXISTS:-0}" == "0" ]] || exit 3
        # 测试注入点：在"媒体阶段失败"**之前**跑一条宿主命令。默认不启用；
        # 用来模拟"回填历史密钥时目录已经写不进去"（`chmod 500 keys/`）。
        if [[ -n "${MEDIA_STEP_HOOK:-}" ]]; then
          bash -c "${MEDIA_STEP_HOOK}" || fail "MEDIA_STEP_HOOK 失败"
        fi
        cat "${MEDIA_TAR_PAYLOAD:?}"
        exit "${MEDIA_TAR_EXIT:-0}"
        ;;
      *)
        fail "未声明的容器内脚本: $SCRIPT"
        ;;
    esac
    ;;
  *)
    fail "未声明的服务 $SERVICE"
    ;;
esac
"""

# compose 模式下不应该退化成宿主 `cp`（命名卷在宿主上根本不可直接访问）。
_CP_FAKE = r"""#!/usr/bin/env bash
printf 'cp %s\n' "$*" >> "${CP_CALLS:?}"
echo "fake-cp: compose 模式不应调用宿主 cp（命名卷在宿主不可直接访问）" >&2
exit 92
"""

_PSQL_FAKE = r"""#!/usr/bin/env bash
set -euo pipefail
printf 'psql %s\n' "$*" >> "${PSQL_CALLS:?}"
SQL=""
for arg in "$@"; do SQL="$arg"; done
case "$SQL" in
  *"DROP DATABASE"*|*"CREATE DATABASE"*)
    exit 0
    ;;
  *media_evidence*)
    [[ -z "${PSQL_REFS_FAIL:-}" ]] || exit 93
    cat "${REFS_PAYLOAD:?}"
    ;;
  *"SELECT id FROM memories WHERE retracted = true"*)
    cat "${RESTORED_TOMBSTONES:?}"
    ;;
  *"SELECT count(*) FROM"*)
    echo "3"
    ;;
  *)
    echo "fake-psql: 未声明的查询: $SQL" >&2
    exit 90
    ;;
esac
"""

_PG_RESTORE_FAKE = r"""#!/usr/bin/env bash
set -euo pipefail
printf 'pg_restore %s\n' "$*" >> "${PG_RESTORE_CALLS:?}"
exit "${PG_RESTORE_EXIT:-0}"
"""

# 直接模式的严格 fake：只认 backup.sh 真正用到的 argv 形状，绝不连真实数据库。
_PG_DUMP_FAKE = r"""#!/usr/bin/env bash
set -euo pipefail
printf 'pg_dump %s\n' "$*" >> "${PG_DUMP_CALLS:?}"
OUT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --file)
      [[ $# -ge 2 ]] || { echo "fake-pg_dump: --file 缺参数" >&2; exit 90; }
      OUT="$2"
      shift 2
      ;;
    --no-owner|--no-privileges|--format=custom)
      shift
      ;;
    *)
      shift
      ;;
  esac
done
[[ -n "$OUT" ]] || { echo "fake-pg_dump: 没有 --file，拒绝默认写 stdout" >&2; exit 90; }
printf 'PGDMP-direct-fake-dump\n' > "$OUT"
"""

_PSQL_DIRECT_FAKE = r"""#!/usr/bin/env bash
set -euo pipefail
printf 'psql %s\n' "$*" >> "${PSQL_DIRECT_CALLS:?}"
SQL=""
for arg in "$@"; do SQL="$arg"; done
case "$SQL" in
  *"SELECT id, retracted_at FROM memories"*)
    printf 'mem_yesterday|2026-01-01 00:00:00\n'
    ;;
  *"SELECT version()"*)
    printf 'PostgreSQL 16.3 (fake)\n'
    ;;
  *)
    echo "fake-psql: 未声明的查询: $SQL" >&2
    exit 91
    ;;
esac
"""

# 只声明"创建归档时致命失败"这一种行为：直接模式下 backup.sh 用 `tar -cf` 打媒体
# 目录（不像 compose 模式那样还有 `tar -tf` 完整性校验）。
_TAR_FAIL_FAKE = r"""#!/usr/bin/env bash
set -euo pipefail
printf 'tar %s\n' "$*" >> "${TAR_CALLS:?}"
echo "fake-tar: 致命错误（测试注入）" >&2
exit 2
"""


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _tar_bytes(entries: dict[str, bytes]) -> bytes:
    """在内存里造一份真实 tar：backup.sh 会用本地 ``tar -tf`` 校验完整性。"""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, payload in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


@dataclass
class BackupRun:
    """一次 backup.sh（compose 模式）运行的现场。"""

    repo: Path
    out: Path
    env: dict[str, str]
    proc: subprocess.CompletedProcess[str]

    def manifest(self) -> dict[str, str]:
        manifests = sorted(self.out.glob("manifest-*.txt"))
        assert len(manifests) == 1, [p.name for p in self.out.iterdir()]
        parsed: dict[str, str] = {}
        for line in manifests[0].read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                parsed[key] = value
        return parsed

    def calls(self, name: str) -> list[str]:
        path = Path(self.env[name])
        if not path.exists():
            return []
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line]

    def outputs(self) -> str:
        return self.proc.stdout + self.proc.stderr

    def leftovers(self) -> list[str]:
        """备份目录里的隐藏半成品（``.xxx.part`` 之类）。"""
        return sorted(p.name for p in self.out.iterdir() if p.name.startswith("."))


@pytest.fixture()
def backup_env(tmp_path: Path) -> dict[str, object]:
    """严格 fake 的 PATH + 一份真实（内存造的）媒体 tar 载荷。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "docker", _DOCKER_FAKE)
    _write_executable(bin_dir / "cp", _CP_FAKE)

    media_tar = tmp_path / "media.tar"
    media_tar.write_bytes(
        _tar_bytes({"media/ev_001.jpg": b"jpeg-bytes", "media/ev_002.wav": b"wav-bytes"})
    )
    root_key = tmp_path / "root.key.payload"
    root_key.write_text(ROOT_KEY_BODY, encoding="utf-8")
    tombstones = tmp_path / "tombstones.payload"
    tombstones.write_text("mem_aaa\nmem_bbb\n", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["DOCKER_CALLS"] = str(tmp_path / "docker.calls")
    env["CP_CALLS"] = str(tmp_path / "cp.calls")
    # 宿主环境里的真实 DSN/密钥不能渗进用例。
    for key in ("LING_DATABASE_URL", "LING_ROOT_KEY", "PGPASSWORD", "LING_BACKUP_DIR"):
        env.pop(key, None)
    env.update(
        {
            "LING_BACKUP_VIA_COMPOSE": "1",
            "LING_COMPOSE_PROJECT": "ling-test",
            "LING_COMPOSE_FILE": str(tmp_path / "compose.yaml"),
            "TOMBSTONES_PSQL_PAYLOAD": str(tombstones),
            "ROOT_KEY_PAYLOAD": str(root_key),
            "ROOT_KEY_EXISTS": "0",
            "MEDIA_DIR_EXISTS": "0",
            "MEDIA_TAR_PAYLOAD": str(media_tar),
            "MEDIA_TAR_EXIT": "0",
        }
    )
    return {"bin": bin_dir, "root_key": root_key, "tombs": tombstones, "env": env}


def _run_backup(tmp_path: Path, base_env: dict[str, str], **extra: str) -> BackupRun:
    out = tmp_path / "backup-out"
    out.mkdir()
    return _run_backup_in(out, base_env, **extra)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- 历史备份保护
#
# QA（task62 F1，Medium）：`keys/root.key` **没有日期戳**，而 cron 每天往同一个
# BACKUP_DIR 写。于是"本次归档的 keys"与"前一天那份成功备份的 keys"在共享目录里
# **是同一个文件**。旧实现只要 `KEYS_ARCHIVED=1` 就在失败时 `rm -f keys/root.key`，
# 后果是"今天在媒体阶段失败"会顺手删掉昨天那份备份的唯一密钥副本，昨天的
# `manifest-*.txt` 里的 `keys_root_file=keys/root.key` 立刻悬空。
#
# 下面四个用例把边界钉死：历史密钥**字节不变**、本次新建的才删、absent 时不动、
# 直接模式同一场景同样安全。

# 与"今天"的日期戳必然不同，避免和本次 STAMP 撞名。
PREVIOUS_STAMP = "20200101-043000"
PREVIOUS_KEY_BODY = "YESTERDAY-KEY-BYTES-must-survive\n"


def _seed_previous_backup(out: Path) -> dict[str, bytes]:
    """在备份目录里放一份"昨天那份成功备份"（5 件产物，manifest 自洽）。

    返回相对路径 → 字节的快照，供事后逐字节比对（"完好且字节不变"）。
    """
    out.mkdir(parents=True, exist_ok=True)
    dump = out / f"ling-{PREVIOUS_STAMP}.dump"
    dump.write_bytes(b"PGDMP-previous-dump\n")
    tombstones = out / f"tombstones-{PREVIOUS_STAMP}.txt"
    tombstones.write_text("mem_yesterday\n", encoding="utf-8")
    media = out / f"media-{PREVIOUS_STAMP}.tar"
    media.write_bytes(_tar_bytes({"media/ev_old.jpg": b"old-jpeg-bytes"}))
    keys = out / "keys"
    keys.mkdir()
    key = keys / "root.key"
    key.write_bytes(PREVIOUS_KEY_BODY.encode("utf-8"))
    key.chmod(0o600)
    manifest = out / f"manifest-{PREVIOUS_STAMP}.txt"
    manifest.write_text(
        "\n".join(
            [
                "created_at=2020-01-01T04:30:00Z",
                "mode=compose",
                f"dump=ling-{PREVIOUS_STAMP}.dump",
                f"dump_sha256={_sha256(dump)}",
                f"tombstones=tombstones-{PREVIOUS_STAMP}.txt",
                "tombstones_state=ok",
                f"tombstones_sha256={_sha256(tombstones)}",
                "keys_root=present",
                "keys_root_file=keys/root.key",
                "keys_root_source=file",
                f"keys_root_sha256={_sha256(key)}",
                "keys_env_set=false",
                f"media=media-{PREVIOUS_STAMP}.tar",
                "media_state=present",
                f"media_sha256={_sha256(media)}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        path.relative_to(out).as_posix(): path.read_bytes()
        for path in sorted(out.rglob("*"))
        if path.is_file()
    }


def _assert_previous_backup_intact(out: Path, snapshot: dict[str, bytes]) -> None:
    """历史备份的每一件产物都还在、且字节完全没变（含 keys/root.key）。"""
    for name, payload in snapshot.items():
        path = out / name
        assert path.is_file(), f"历史产物被删了: {name}（现存: {sorted(p.name for p in out.rglob('*'))}）"
        assert path.read_bytes() == payload, f"历史产物字节被改写: {name}"
    # 昨天 manifest 里的 keys_root_file 必须仍指向一个存在的文件（不能悬空）。
    manifest = (out / f"manifest-{PREVIOUS_STAMP}.txt").read_text(encoding="utf-8")
    parsed = dict(line.split("=", 1) for line in manifest.splitlines() if "=" in line)
    assert parsed["keys_root"] == "present"
    referenced = out / parsed["keys_root_file"]
    assert referenced.is_file(), "昨天 manifest 的 keys_root_file 悬空了"
    assert _sha256(referenced) == parsed["keys_root_sha256"]


def _run_backup_in(
    out: Path,
    base_env: dict[str, str],
    *,
    umask_bits: str | None = None,
    **extra: str,
) -> BackupRun:
    """在**已有内容**的备份目录上跑一次 backup.sh（注入"昨天那份备份"时用）。"""
    env = dict(base_env)
    env.update(extra)
    proc = subprocess.run(
        _backup_argv(out, umask_bits),
        cwd=out.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return BackupRun(repo=out.parent, out=out, env=env, proc=proc)


def _run_backup_observed(
    out: Path,
    base_env: dict[str, str],
    *,
    umask_bits: str,
    **extra: str,
) -> tuple[BackupRun, list[int]]:
    """跑一次 backup.sh，同时轮询 ``keys/.root.key.prev.*`` 的权限位。

    返回 ``(运行现场, 轮询到的权限位列表)``。轮询是替身之外的**第二路独立观察**：
    暂存副本只存在很短一段时间，替身报的是"创建瞬间"，轮询报的是"存续期间的每一眼"。
    """
    env = dict(base_env)
    env.update(extra)
    keys_dir = out / "keys"
    proc = subprocess.Popen(
        _backup_argv(out, umask_bits),
        cwd=out.parent,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    observed: list[int] = []
    try:
        while proc.poll() is None:
            for path in keys_dir.glob(".root.key.prev.*"):
                try:
                    observed.append(path.stat().st_mode & 0o777)
                except OSError:  # 刚好被回填/清理掉
                    continue
            time.sleep(0.005)
        stdout, stderr = proc.communicate(timeout=120)
    finally:
        if proc.poll() is None:  # pragma: no cover - 只在卡死时触发
            proc.kill()
            proc.communicate()
    return (
        BackupRun(
            repo=out.parent,
            out=out,
            env=env,
            proc=subprocess.CompletedProcess(
                args=[str(BACKUP_SH), str(out)],
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
            ),
        ),
        observed,
    )


def test_compose_backup_includes_root_key_and_media(
    tmp_path: Path, backup_env: dict[str, object]
) -> None:
    """Medium-1 的核心断言：默认 compose 模式必须备份 root.key 与 media 字节。"""
    env = backup_env["env"]
    assert isinstance(env, dict)
    run = _run_backup(tmp_path, env)
    assert run.proc.returncode == 0, run.outputs()

    manifest = run.manifest()
    assert manifest["mode"] == "compose"
    assert manifest["tombstones_state"] == "ok"

    # 根密钥：文件真的被取回来了，权限 0600，内容与容器里一致。
    key_path = run.out / "keys" / "root.key"
    assert key_path.is_file(), run.leftovers()
    assert key_path.read_text(encoding="utf-8") == ROOT_KEY_BODY
    assert (key_path.stat().st_mode & 0o777) == 0o600
    assert manifest["keys_root"] == "present"
    assert manifest["keys_root_file"] == "keys/root.key"
    assert manifest["keys_root_source"] == "file"
    assert manifest["keys_root_sha256"] == _sha256(key_path)

    # 媒体：真的归档了字节，且 manifest 记了产物名与哈希。
    media_name = manifest["media"]
    assert re.fullmatch(r"media-\d{8}-\d{6}\.tar", media_name), media_name
    media_path = run.out / media_name
    assert media_path.is_file()
    assert manifest["media_state"] == "present"
    assert manifest["media_sha256"] == _sha256(media_path)
    with tarfile.open(media_path) as archive:
        assert archive.getnames() == ["media/ev_001.jpg", "media/ev_002.wav"]

    # 既有产物与哈希。
    dump = run.out / manifest["dump"]
    assert dump.is_file() and dump.stat().st_size > 0
    assert manifest["dump_sha256"] == _sha256(dump)
    tombs = run.out / manifest["tombstones"]
    assert manifest["tombstones_sha256"] == _sha256(tombs)

    # 日志里不能出现 root.key 内容，也不能退化成宿主 cp。
    assert ROOT_KEY_BODY not in run.outputs()
    assert run.calls("CP_CALLS") == []
    # 半成品全部清理。
    assert run.leftovers() == []


def test_absent_key_and_media_are_recorded_explicitly(
    tmp_path: Path, backup_env: dict[str, object]
) -> None:
    """未生成时必须显式写 absent，而不是静默跳过（静默 = 以为备了）。"""
    env = backup_env["env"]
    assert isinstance(env, dict)
    run = _run_backup(tmp_path, env, ROOT_KEY_EXISTS="1", MEDIA_DIR_EXISTS="1")
    assert run.proc.returncode == 0, run.outputs()

    manifest = run.manifest()
    assert manifest["keys_root"] == "absent"
    assert manifest["keys_root_file"] == "-"
    assert manifest["keys_root_source"] == "none"
    assert manifest["keys_root_sha256"] == "-"
    assert manifest["media"] == "absent"
    assert manifest["media_state"] == "absent"
    assert manifest["media_sha256"] == "-"
    assert not (run.out / "keys").exists()
    assert list(run.out.glob("media-*.tar")) == []
    # 必须明确警告"恢复后凭据解不开"，而不是让运维以为一切正常。
    assert "解密" in run.proc.stderr


def test_env_provided_root_key_is_not_claimed_as_archived(
    tmp_path: Path, backup_env: dict[str, object]
) -> None:
    """LING_ROOT_KEY 非空时应用不读 /data/root.key：manifest 要写 absent+source=env。"""
    env = backup_env["env"]
    assert isinstance(env, dict)
    run = _run_backup(
        tmp_path,
        env,
        ROOT_KEY_EXISTS="1",
        MEDIA_DIR_EXISTS="1",
        LING_ROOT_KEY="env-provided-root-key-value",
    )
    assert run.proc.returncode == 0, run.outputs()

    manifest = run.manifest()
    assert manifest["keys_root"] == "absent"
    assert manifest["keys_root_source"] == "env"
    assert not (run.out / "keys").exists()
    # 环境变量里的密钥值本身不能被写进日志。
    assert "env-provided-root-key-value" not in run.outputs()


def test_media_archive_failure_exits_nonzero_and_cleans_up(
    tmp_path: Path, backup_env: dict[str, object]
) -> None:
    """媒体归档失败（tar 致命错误）必须非零退出，且不留半截 tar/manifest。"""
    env = backup_env["env"]
    assert isinstance(env, dict)
    run = _run_backup(tmp_path, env, MEDIA_TAR_EXIT="2")
    assert run.proc.returncode != 0, run.outputs()
    assert list(run.out.glob("media-*.tar")) == []
    assert list(run.out.glob("manifest-*.txt")) == []
    assert run.leftovers() == []
    assert "媒体归档失败" in run.proc.stderr


def test_truncated_media_archive_is_rejected(
    tmp_path: Path, backup_env: dict[str, object]
) -> None:
    """tar 返回 0 但归档不可读（截断）时也必须失败，不能写进 manifest。"""
    env = backup_env["env"]
    assert isinstance(env, dict)
    broken = tmp_path / "broken.tar"
    broken.write_bytes(b"not-a-tar-at-all")
    run = _run_backup(tmp_path, env, MEDIA_TAR_PAYLOAD=str(broken))
    assert run.proc.returncode != 0, run.outputs()
    assert list(run.out.glob("media-*.tar")) == []
    assert list(run.out.glob("manifest-*.txt")) == []
    assert run.leftovers() == []


def test_root_key_read_failure_is_fatal(
    tmp_path: Path, backup_env: dict[str, object]
) -> None:
    """容器里 root.key 存在但读不出来 → 非零退出（不能静默产出一份"没有密钥"的备份）。"""
    env = backup_env["env"]
    assert isinstance(env, dict)
    run = _run_backup(tmp_path, env, ROOT_KEY_CAT_FAIL="1")
    assert run.proc.returncode != 0, run.outputs()
    assert not (run.out / "keys" / "root.key").exists()
    assert list(run.out.glob("manifest-*.txt")) == []
    assert run.leftovers() == []


def test_undeclared_docker_argv_fails(
    tmp_path: Path, backup_env: dict[str, object]
) -> None:
    """严格 fake 自证：未声明的 compose 参数会让 backup.sh 失败（fake 不兜底成功）。"""
    fake = (tmp_path / "bin" / "docker")
    fake.write_text(
        "#!/usr/bin/env bash\nprintf 'docker %s\\n' \"$*\" >> \"${DOCKER_CALLS:?}\"\n"
        'echo "fake-docker: 一律拒绝" >&2\nexit 90\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)

    env = backup_env["env"]
    assert isinstance(env, dict)
    run = _run_backup(tmp_path, env)
    assert run.proc.returncode != 0
    assert "pg_dump 失败" in run.proc.stderr
    assert list(run.out.glob("manifest-*.txt")) == []


@pytest.fixture()
def direct_env(tmp_path: Path) -> dict[str, object]:
    """直接模式的严格 fake：``pg_dump`` / ``psql`` / ``tar``（失败注入）+ 假数据目录。

    ``tar`` 被换成一个"创建归档就 exit 2"的替身：直接模式的媒体失败路径最容易被
    遗漏（compose 模式另有 ``tar -tf`` 兜底，直接模式没有）。
    """
    bin_dir = tmp_path / "direct-bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "pg_dump", _PG_DUMP_FAKE)
    _write_executable(bin_dir / "psql", _PSQL_DIRECT_FAKE)
    _write_executable(bin_dir / "tar", _TAR_FAIL_FAKE)

    data = tmp_path / "data"
    (data / "media").mkdir(parents=True)
    (data / "media" / "ev_new.jpg").write_bytes(b"new-jpeg-bytes")
    (data / "root.key").write_bytes(b"TODAY-KEY-BYTES-different-from-yesterday\n")

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["PG_DUMP_CALLS"] = str(tmp_path / "pg_dump.calls")
    env["PSQL_DIRECT_CALLS"] = str(tmp_path / "psql.calls")
    env["TAR_CALLS"] = str(tmp_path / "tar.calls")
    # 宿主机/CI 环境里的真实 DSN、密钥、compose 变量都不能渗进用例。
    for key in (
        "LING_BACKUP_VIA_COMPOSE",
        "LING_COMPOSE_PROJECT",
        "LING_COMPOSE_FILE",
        "LING_ROOT_KEY",
        "LING_BACKUP_DIR",
        "PGPASSWORD",
    ):
        env.pop(key, None)
    # 假 DSN：fake pg_dump/psql 不连接任何数据库，只用它校验前缀与脱敏。
    env["LING_DATABASE_URL"] = "postgresql+asyncpg://ling:fake-pw@127.0.0.1:55432/ling"
    env["LING_DATA_DIR"] = str(data)
    return {"env": env, "data": data}


def test_failed_run_after_keys_keeps_previous_backup_intact(
    tmp_path: Path, backup_env: dict[str, object]
) -> None:
    """(a) 本次**已归档**密钥、随后媒体阶段失败：昨天那份备份必须完好、字节不变。

    这是 QA task62 F1 的危险路径：``keys/root.key`` 没有日期戳，共享备份目录里它
    同时是昨天那份成功备份的密钥归档。旧实现在这里会把整个 ``keys/`` 删掉，昨天
    ``manifest-*.txt`` 里的 ``keys_root_file=keys/root.key`` 立刻悬空。
    """
    env = backup_env["env"]
    assert isinstance(env, dict)
    out = tmp_path / "backup-out"
    snapshot = _seed_previous_backup(out)

    # 默认 ROOT_KEY_EXISTS="0" = 容器里有 /data/root.key（本次会归档密钥）。
    run = _run_backup_in(out, env, MEDIA_TAR_EXIT="2")
    assert run.proc.returncode == 5, run.outputs()

    # 本次 STAMP 的产物全清（manifest 只在成功时写，所以这里没有）。
    assert list(out.glob("manifest-*.txt")) == [out / f"manifest-{PREVIOUS_STAMP}.txt"]
    assert list(out.glob("ling-*.dump")) == [out / f"ling-{PREVIOUS_STAMP}.dump"]
    assert list(out.glob("tombstones-*.txt")) == [
        out / f"tombstones-{PREVIOUS_STAMP}.txt"
    ]
    assert list(out.glob("media-*.tar")) == [out / f"media-{PREVIOUS_STAMP}.tar"]
    # 昨天的 5 件产物逐字节不变；昨天 manifest 指向的密钥文件仍在且哈希对得上。
    _assert_previous_backup_intact(out, snapshot)
    # 临时暂存副本不能残留（成功/失败两条路都要收干净）。
    assert [p.name for p in (out / "keys").iterdir()] == ["root.key"]
    assert run.leftovers() == []
    assert "原样恢复" in run.proc.stderr


def test_failed_run_removes_key_created_by_this_run(
    tmp_path: Path, backup_env: dict[str, object]
) -> None:
    """(b) 目录里原本没有密钥、本次新建后又失败：这次新建的必须删掉。"""
    env = backup_env["env"]
    assert isinstance(env, dict)
    run = _run_backup(tmp_path, env, MEDIA_TAR_EXIT="2")
    assert run.proc.returncode == 5, run.outputs()
    assert not (run.out / "keys" / "root.key").exists()
    assert not (run.out / "keys").exists()
    assert list(run.out.glob("manifest-*.txt")) == []
    assert run.leftovers() == []


def test_failed_run_with_absent_key_touches_nothing(
    tmp_path: Path, backup_env: dict[str, object]
) -> None:
    """(c) 本次密钥为 ``absent``（容器里没有）时失败：历史密钥一根汗毛都不动。"""
    env = backup_env["env"]
    assert isinstance(env, dict)
    out = tmp_path / "backup-out"
    snapshot = _seed_previous_backup(out)

    run = _run_backup_in(out, env, ROOT_KEY_EXISTS="1", MEDIA_TAR_EXIT="2")
    assert run.proc.returncode == 5, run.outputs()

    _assert_previous_backup_intact(out, snapshot)
    assert [p.name for p in (out / "keys").iterdir()] == ["root.key"]
    assert list(out.glob("manifest-*.txt")) == [out / f"manifest-{PREVIOUS_STAMP}.txt"]


def test_direct_mode_failure_restores_previous_keys(
    tmp_path: Path, direct_env: dict[str, object]
) -> None:
    """(d) 直接模式同一场景同样安全：覆盖过的历史密钥必须被原样放回。"""
    env = direct_env["env"]
    assert isinstance(env, dict)
    out = tmp_path / "backup-out"
    snapshot = _seed_previous_backup(out)

    run = _run_backup_in(out, env)
    assert run.proc.returncode == 5, run.outputs()
    assert "媒体归档失败" in run.proc.stderr
    # 自证失败来自注入的 tar，而不是脚本在更早的步骤就退出了。
    assert run.calls("TAR_CALLS")

    assert list(out.glob("ling-*.dump")) == [out / f"ling-{PREVIOUS_STAMP}.dump"]
    # 直接模式才产出的 version-*.txt 也必须被清掉。
    assert list(out.glob("version-*.txt")) == []
    assert list(out.glob("manifest-*.txt")) == [out / f"manifest-{PREVIOUS_STAMP}.txt"]
    _assert_previous_backup_intact(out, snapshot)
    assert [p.name for p in (out / "keys").iterdir()] == ["root.key"]
    assert run.leftovers() == []


# ---------------------------------------------------------------- 密钥字节权限窗口
#
# QA（task65 N1，Low）：暂存副本 `keys/.root.key.prev.<pid>` 与临时文件
# `.root.key.part.<pid>`（`KEYS_TMP`）原来是"先重定向创建、再 `chmod 600`"。
# 重定向创建走 `0666 & ~umask`，默认 umask 022 就是 **0644**，于是 `chmod 600`
# 之前存在一个"同机其他用户能读到根密钥字节"的窗口——cron 路径的备份目录本来就是
# 0755，所以这个窗口是真的可被利用；`KEYS_TMP` 那条更宽（要跨一次
# `docker compose exec` 的往返）。
#
# 修法：把重定向挪进 `umask 077` 的子 shell **内**，文件一出生就是 0600。
# 下面两个替身从**创建瞬间**取权限位：文件是 shell 在 exec 之前用
# `O_CREAT|O_TRUNC` 建好的，替身启动时 `fstat(stdout)` / `stat(目标)` 拿到的
# 就是创建时的 mode（此时脚本的 `chmod 600` 还没跑）。
#   - `cat` 替身：`cat … > 目标` 的审计（暂存副本、`KEYS_TMP` 都走它）；
#   - `cp` 替身：直接模式 `cp 源 目标` 的审计。
_PYTHON = sys.executable

_CAT_MODE_SHIM = r"""#!/usr/bin/env bash
# 记录"重定向目标在 cat 被调用这一刻的权限位"= 脚本 chmod 之前的创建时 mode。
# 注意：fd 1 必须先复制到 fd 3——命令替换/重定向会把**子进程**的 fd 1 换掉，
# 只有脚本自己的 fd 1 才是那个重定向目标。
set -u
TARGET=""
for arg in "$@"; do TARGET="${arg}"; done   # cat 可能带 `--`，取最后一个参数
exec 3>&1
'__PYTHON__' -c 'import os, stat, sys
try:
    st = os.fstat(3)
except OSError:
    sys.exit(0)
if not stat.S_ISREG(st.st_mode):
    sys.exit(0)
sys.stdout.write("%s\t%o\n" % (sys.argv[1], st.st_mode & 0o777))' "${TARGET}" \
  >> "${CAT_MODE_LOG:-/dev/null}" || true
if [ -n "${CAT_STALL_SECONDS:-}" ]; then
  sleep "${CAT_STALL_SECONDS}"
fi
exec /bin/cat "$@"
""".replace("__PYTHON__", _PYTHON)

_CP_MODE_SHIM = r"""#!/usr/bin/env bash
# 记录 cp 目标文件"刚被创建、还没被 chmod"时的权限位，然后原样返回 cp 的退出码。
set -u
/bin/cp "$@"
RC=$?
DEST="${2:-}"
if [ -n "${DEST}" ] && [ -f "${DEST}" ]; then
  MODE="$(
    '__PYTHON__' -c 'import os, stat, sys
try:
    st = os.stat(sys.argv[1])
except OSError:
    sys.exit(0)
if not stat.S_ISREG(st.st_mode):
    sys.exit(0)
sys.stdout.write("%o\n" % (st.st_mode & 0o777))' "${DEST}"
  )"
  if [ -n "${MODE}" ]; then
    printf '%s\t%s\n' "${DEST}" "${MODE}" >> "${CP_MODE_LOG:?}"
  fi
fi
if [ -n "${CP_STALL_SECONDS:-}" ]; then
  sleep "${CP_STALL_SECONDS}"
fi
exit "${RC}"
""".replace("__PYTHON__", _PYTHON)


def _backup_argv(out: Path, umask_bits: str | None) -> list[str]:
    """backup.sh 的 argv；给 umask 时用一个外层 shell 显式设好再 exec。

    umask 必须显式给：用例结果不能随宿主/CI 的默认 umask 漂移（本用例断言的正是
    "umask 022 下文件创建即 0600"）。
    """
    if umask_bits is None:
        return ["bash", str(BACKUP_SH), str(out)]
    return [
        "/bin/bash",
        "-c",
        'umask "$1"; shift; exec /bin/bash "$@"',
        "runner",
        umask_bits,
        str(BACKUP_SH),
        str(out),
    ]


def _mode_records(log: Path) -> list[tuple[str, str]]:
    """替身记下的 (目标路径, 八进制权限位) 列表。"""
    if not log.exists():
        return []
    records: list[tuple[str, str]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if "\t" in line:
            target, mode = line.split("\t", 1)
            records.append((target, mode))
    return records


def test_stashed_key_copy_and_keys_tmp_are_created_0600(
    tmp_path: Path, backup_env: dict[str, object]
) -> None:
    """umask 022 下：`.root.key.prev.<pid>` 与 `KEYS_TMP` **创建即 0600**，无 0644 窗口。

    这是 task65 N1 的回归点。观察方式两路并进：
    1. `cat` 替身在"还没 chmod"的时刻记录重定向目标的权限位（确定性证据）；
    2. 主线程轮询暂存副本，记录运行期间看到的每一个权限位（不依赖替身）。
    """
    env = backup_env["env"]
    assert isinstance(env, dict)
    root_key_payload = str(backup_env["root_key"])
    tombstones_payload = str(backup_env["tombs"])

    shim_dir = tmp_path / "cat-bin"
    shim_dir.mkdir()
    _write_executable(shim_dir / "cat", _CAT_MODE_SHIM)
    mode_log = tmp_path / "cat-modes.log"

    # 预置"昨天那份备份"：暂存副本这条路径只有"目录里已有 keys/root.key"时才走。
    out = tmp_path / "backup-out"
    _seed_previous_backup(out)

    run, observed = _run_backup_observed(
        out,
        env,
        umask_bits="022",
        PATH=f"{shim_dir}{os.pathsep}{env['PATH']}",
        CAT_MODE_LOG=str(mode_log),
        CAT_STALL_SECONDS="0.4",
    )
    assert run.proc.returncode == 0, run.outputs()

    records = _mode_records(mode_log)
    # 前置条件自证：这次真的是 umask 022（墓碑清单按既有口径是 0644）。
    tomb_modes = {mode for target, mode in records if target == tombstones_payload}
    assert tomb_modes == {"644"}, (
        f"用例前置条件不成立：墓碑清单创建时是 {sorted(tomb_modes)}，说明这次不是 umask 022"
    )

    # ① KEYS_TMP：`( umask 077; docker … > "$KEYS_TMP" )` 的目标文件。容器读到的
    #    密钥字节就是写进它的，创建时若是 0644 就等于把根密钥对同机用户公开。
    keys_tmp_modes = {mode for target, mode in records if target == root_key_payload}
    assert keys_tmp_modes == {"600"}, (
        f"KEYS_TMP 创建时权限是 {sorted(keys_tmp_modes)}（应为 600）："
        "根密钥字节在 chmod 之前存在一个可读窗口"
    )

    # ② 暂存副本 `keys/.root.key.prev.<pid>`：stash 里 `cat <keys/root.key> > $KEYS_PREV`。
    prev_modes = {
        mode
        for target, mode in records
        if target == str(out / "keys" / "root.key")
    }
    assert prev_modes == {"600"}, (
        f"暂存密钥副本创建时权限是 {sorted(prev_modes)}（应为 600）"
    )

    # ③ 轮询到的中间态也必须全是 0600（防止"替身没被调到"式的空过）。
    assert observed, "运行期间一次都没观察到暂存副本，用例没测到东西"
    assert set(observed) == {0o600}, f"轮询到非 0600 的中间态: {observed}"

    # 成功路径：暂存副本零残留，正式产物 0600。
    assert [p.name for p in (out / "keys").iterdir()] == ["root.key"]
    assert ((out / "keys" / "root.key").stat().st_mode & 0o777) == 0o600
    assert run.leftovers() == []
    # 密钥内容不落日志。
    assert ROOT_KEY_BODY not in run.outputs()
    assert PREVIOUS_KEY_BODY.strip() not in run.outputs()


def test_direct_mode_keys_tmp_is_created_0600(
    tmp_path: Path, direct_env: dict[str, object]
) -> None:
    """直接模式的 `KEYS_TMP`（`cp 源 目标`）同样创建即 0600。"""
    env = direct_env["env"]
    assert isinstance(env, dict)

    shim_dir = tmp_path / "cp-bin"
    shim_dir.mkdir()
    _write_executable(shim_dir / "cp", _CP_MODE_SHIM)
    mode_log = tmp_path / "cp-modes.log"

    out = tmp_path / "backup-out"
    out.mkdir()
    # 直接模式的 tar 替身必然失败（exit 2），密钥步骤在它之前——正好只有密钥可观察。
    run = _run_backup_in(
        out,
        env,
        umask_bits="022",
        PATH=f"{shim_dir}{os.pathsep}{env['PATH']}",
        CP_MODE_LOG=str(mode_log),
        CP_STALL_SECONDS="0.2",
    )
    assert run.proc.returncode == 5, run.outputs()

    tmp_modes = {
        mode for target, mode in _mode_records(mode_log) if ".root.key.part." in target
    }
    assert tmp_modes == {"600"}, (
        f"直接模式的 KEYS_TMP 创建时权限是 {sorted(tmp_modes)}（应为 600）"
    )
    assert run.leftovers() == []


def test_failed_restore_keeps_0600_safety_copy_and_exits_nonzero(
    tmp_path: Path, backup_env: dict[str, object]
) -> None:
    """回填失败分支（task64 §3.3 的 fail-safe）：保留 0600 安全网副本、非零退出、绝不删。

    QA task65 场景 A2 的仓库内版本：本次已归档密钥、随后媒体失败，而清理时
    `keys/` 已经写不进去（`chmod 500`）——此时**不能** `rm`，只能把原字节留在
    `keys/.root.key.prev.<pid>`（0600）里等人工处理。
    """
    env = backup_env["env"]
    assert isinstance(env, dict)
    out = tmp_path / "backup-out"
    snapshot = _seed_previous_backup(out)
    keys_dir = out / "keys"

    try:
        run = _run_backup_in(
            out,
            env,
            MEDIA_TAR_EXIT="2",
            # 媒体阶段（密钥归档之后）把 keys/ 变只读：回填 mv 必然失败。
            MEDIA_STEP_HOOK=f'chmod 500 "{keys_dir}"',
        )
        prevs = sorted(keys_dir.glob(".root.key.prev.*"))

        assert run.proc.returncode == 5, run.outputs()
        # 绝不删：当前文件还在（内容是今天那把），历史那份留在 0600 暂存副本里。
        assert (keys_dir / "root.key").is_file()
        assert (keys_dir / "root.key").read_bytes() != PREVIOUS_KEY_BODY.encode("utf-8")
        assert len(prevs) == 1, [p.name for p in keys_dir.iterdir()]
        assert (prevs[0].stat().st_mode & 0o777) == 0o600
        assert prevs[0].read_bytes() == PREVIOUS_KEY_BODY.encode("utf-8")
        # 告警必须指向"人工确认"，而不是假装恢复成功。
        assert "保留当前文件" in run.proc.stderr
        assert "原样恢复" not in run.proc.stderr
        # 历史备份的其它四件产物逐字节不变（密钥文件这一件按设计被覆盖了）。
        for name in (
            f"ling-{PREVIOUS_STAMP}.dump",
            f"tombstones-{PREVIOUS_STAMP}.txt",
            f"media-{PREVIOUS_STAMP}.tar",
            f"manifest-{PREVIOUS_STAMP}.txt",
        ):
            assert (out / name).read_bytes() == snapshot[name], f"{name} 被改写了"
        # 本次的半成品（dump/墓碑/媒体/.part/.err）全部清掉。
        assert list(out.glob("manifest-*.txt")) == [out / f"manifest-{PREVIOUS_STAMP}.txt"]
        assert run.leftovers() == []
    finally:
        # 恢复可写，否则 pytest 清理 tmp_path 会失败。
        keys_dir.chmod(0o700)


# ---------------------------------------------------------------- 恢复演练


@dataclass
class DrillRun:
    proc: subprocess.CompletedProcess[str]
    env: dict[str, str]
    out: Path

    def calls(self, name: str) -> list[str]:
        path = Path(self.env[name])
        if not path.exists():
            return []
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line]

    def outputs(self) -> str:
        return self.proc.stdout + self.proc.stderr


def _make_backup_dir(
    tmp_path: Path,
    *,
    media: str | None,
    refs: str,
    dump_hash: str | None = None,
    with_media_fields: bool = True,
) -> Path:
    """造一份备份目录（不经过 backup.sh，直接写文件），供 drill 校验。"""
    backup = tmp_path / "backup"
    backup.mkdir()
    stamp = "20260101-000000"
    dump = backup / f"ling-{stamp}.dump"
    dump.write_bytes(b"PGDMP-fake-dump-payload\n")
    tombs = backup / f"tombstones-{stamp}.txt"
    tombs.write_text("mem_aaa\nmem_bbb\n", encoding="utf-8")

    lines = [
        "created_at=2026-01-01T00:00:00Z",
        "mode=compose",
        f"dump=ling-{stamp}.dump",
        f"dump_sha256={dump_hash if dump_hash is not None else _sha256(dump)}",
        f"tombstones=tombstones-{stamp}.txt",
        "tombstones_state=ok",
        f"tombstones_sha256={_sha256(tombs)}",
    ]
    if with_media_fields:
        lines += [
            "keys_root=present",
            "keys_root_file=keys/root.key",
            "keys_root_source=file",
            "keys_root_sha256=-",
        ]
        if media is None:
            lines += ["media=absent", "media_state=absent", "media_sha256=-"]
        else:
            media_path = backup / media
            media_path.write_bytes(
                _tar_bytes({"media/ev_001.jpg": b"jpeg-bytes", "media/ev_002.wav": b"wav-bytes"})
            )
            lines += [
                f"media={media}",
                "media_state=present",
                f"media_sha256={_sha256(media_path)}",
            ]
    (backup / f"manifest-{stamp}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    refs_file = tmp_path / "refs.payload"
    refs_file.write_text(refs, encoding="utf-8")
    return backup


@pytest.fixture()
def drill_env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "psql", _PSQL_FAKE)
    _write_executable(bin_dir / "pg_restore", _PG_RESTORE_FAKE)
    # compose 模式的演练走 docker compose exec，同样用严格 fake。
    _write_executable(bin_dir / "docker", _DOCKER_FAKE)

    restored = tmp_path / "restored-tombstones.payload"
    restored.write_text("mem_aaa\nmem_bbb\n", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["PSQL_CALLS"] = str(tmp_path / "psql.calls")
    env["PG_RESTORE_CALLS"] = str(tmp_path / "pg_restore.calls")
    env["DOCKER_CALLS"] = str(tmp_path / "docker.calls")
    env["REFS_PAYLOAD"] = str(tmp_path / "refs.payload")
    env["RESTORED_TOMBSTONES"] = str(restored)
    for key in ("LING_BACKUP_VIA_COMPOSE", "LING_COMPOSE_PROJECT", "LING_COMPOSE_FILE"):
        env.pop(key, None)
    return env


def _run_drill(
    tmp_path: Path,
    env: dict[str, str],
    backup: Path,
    *,
    via_compose: bool = False,
    **extra: str,
) -> DrillRun:
    dump = next(backup.glob("ling-*.dump"))
    run_env = dict(env)
    run_env.update(extra)
    target = (
        "ling_drill"
        if via_compose
        else "postgresql://ling:REDACTED@127.0.0.1:55432/ling_drill"
    )
    proc = subprocess.run(
        [
            "bash",
            str(DRILL_SH),
            str(dump),
            target,
        ],
        cwd=tmp_path,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return DrillRun(proc=proc, env=run_env, out=backup)


def test_drill_passes_when_media_archive_covers_refs(
    tmp_path: Path, drill_env: dict[str, str]
) -> None:
    backup = _make_backup_dir(
        tmp_path,
        media="media-20260101-000000.tar",
        refs="file:/data/media/ev_001.jpg\nfile:/data/media/ev_002.wav\n",
    )
    run = _run_drill(tmp_path, drill_env, backup)
    assert run.proc.returncode == 0, run.outputs()
    assert "media_ref 可核对 2 条，归档中缺 0 条" in run.proc.stdout


def test_drill_fails_when_media_ref_missing_from_archive(
    tmp_path: Path, drill_env: dict[str, str]
) -> None:
    """库里引用了归档里没有的文件 —— 正是"库回来了、图全丢"的形态。"""
    backup = _make_backup_dir(
        tmp_path,
        media="media-20260101-000000.tar",
        refs=(
            "file:/data/media/ev_001.jpg\n"
            "file:/data/media/ev_002.wav\n"
            "file:/data/media/ev_003.png\n"
        ),
    )
    run = _run_drill(tmp_path, drill_env, backup)
    assert run.proc.returncode != 0, run.outputs()
    assert "media/ev_003.png" in run.proc.stderr
    assert "悬空" in run.proc.stderr


def test_drill_fails_when_backup_has_no_media_but_db_has_refs(
    tmp_path: Path, drill_env: dict[str, str]
) -> None:
    backup = _make_backup_dir(
        tmp_path, media=None, refs="file:/data/media/ev_001.jpg\n"
    )
    run = _run_drill(tmp_path, drill_env, backup)
    assert run.proc.returncode != 0, run.outputs()
    assert "media=absent" in run.proc.stderr


def test_drill_degrades_to_manual_check_for_legacy_manifest(
    tmp_path: Path, drill_env: dict[str, str]
) -> None:
    """旧版备份没有 media 字段：不假装通过，给出人工核对步骤。"""
    backup = _make_backup_dir(
        tmp_path,
        media=None,
        refs="file:/data/media/ev_001.jpg\n",
        with_media_fields=False,
    )
    run = _run_drill(tmp_path, drill_env, backup)
    assert run.proc.returncode == 0, run.outputs()
    assert "人工核对" in run.proc.stdout
    assert "/data/media" in run.proc.stdout


def test_drill_rejects_dump_hash_mismatch(
    tmp_path: Path, drill_env: dict[str, str]
) -> None:
    backup = _make_backup_dir(
        tmp_path,
        media="media-20260101-000000.tar",
        refs="",
        dump_hash="0" * 64,
    )
    run = _run_drill(tmp_path, drill_env, backup)
    assert run.proc.returncode != 0, run.outputs()
    assert "dump_sha256" in run.proc.stderr


def test_drill_refuses_non_drill_target(
    tmp_path: Path, drill_env: dict[str, str]
) -> None:
    backup = _make_backup_dir(tmp_path, media=None, refs="")
    dump = next(backup.glob("ling-*.dump"))
    proc = subprocess.run(
        ["bash", str(DRILL_SH), str(dump), "postgresql://ling:x@127.0.0.1:55432/ling"],
        cwd=tmp_path,
        env=drill_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode != 0
    assert "_drill" in proc.stderr
    assert (tmp_path / "psql.calls").exists() is False


def test_compose_drill_checks_media_coverage(
    tmp_path: Path, drill_env: dict[str, str]
) -> None:
    """compose 模式（默认部署的真实路径）也要做媒体覆盖检查。"""
    backup = _make_backup_dir(
        tmp_path,
        media="media-20260101-000000.tar",
        refs="file:/data/media/ev_001.jpg\nfile:/data/media/ev_002.wav\n",
    )
    restored = tmp_path / "restored-tombstones.payload"
    run = _run_drill(
        tmp_path,
        drill_env,
        backup,
        via_compose=True,
        LING_BACKUP_VIA_COMPOSE="1",
        LING_COMPOSE_PROJECT="ling-test",
        LING_COMPOSE_FILE=str(tmp_path / "compose.yaml"),
        DRILL_MODE="1",
        DRILL_RESTORED_TOMBSTONES=str(restored),
        DRILL_REFS_PAYLOAD=str(tmp_path / "refs.payload"),
    )
    assert run.proc.returncode == 0, run.outputs()
    assert "media_ref 可核对 2 条，归档中缺 0 条" in run.proc.stdout
    assert "隔离库 ling_drill" in run.proc.stdout
    # 全程只通过 compose exec 进容器，不在宿主机直连数据库。
    psql_calls = run.calls("PSQL_CALLS")
    assert psql_calls == []


def test_compose_drill_fails_on_media_gap(
    tmp_path: Path, drill_env: dict[str, str]
) -> None:
    backup = _make_backup_dir(
        tmp_path,
        media="media-20260101-000000.tar",
        refs="file:/data/media/ev_001.jpg\nfile:/data/media/gone.png\n",
    )
    restored = tmp_path / "restored-tombstones.payload"
    run = _run_drill(
        tmp_path,
        drill_env,
        backup,
        via_compose=True,
        LING_BACKUP_VIA_COMPOSE="1",
        LING_COMPOSE_PROJECT="ling-test",
        LING_COMPOSE_FILE=str(tmp_path / "compose.yaml"),
        DRILL_MODE="1",
        DRILL_RESTORED_TOMBSTONES=str(restored),
        DRILL_REFS_PAYLOAD=str(tmp_path / "refs.payload"),
    )
    assert run.proc.returncode != 0, run.outputs()
    assert "media/gone.png" in run.proc.stderr


def test_media_tar_exit_one_is_recorded_as_partial(
    tmp_path: Path, backup_env: dict[str, object]
) -> None:
    """tar exit 1（归档时有文件正在写入）不是致命错误，但必须留下 partial 痕迹。"""
    env = backup_env["env"]
    assert isinstance(env, dict)
    run = _run_backup(tmp_path, env, MEDIA_TAR_EXIT="1")
    assert run.proc.returncode == 0, run.outputs()

    manifest = run.manifest()
    assert manifest["media_state"] == "partial"
    assert manifest["media"] != "absent"
    assert (run.out / manifest["media"]).is_file()
    assert "核对 media_ref" in run.proc.stderr
