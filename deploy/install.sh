#!/usr/bin/env bash
# ling 一键安装 / 升级脚本。
#
#   bash deploy/install.sh                 # 构建前端、装到当前仓库所在机器
#   bash deploy/install.sh --prebuilt-web   # 仅接受通过校验的配套预构建产物
#   bash deploy/install.sh --build-web-only # 构建并原地更新 live dist（不碰 Docker）
#   bash deploy/install.sh --offline-web    # 前端依赖安装显式使用离线缓存
#   bash deploy/install.sh --port 18080    # 指定对外端口
#   bash deploy/install.sh --admin-user admin
#   bash deploy/install.sh --status        # 看状态
#   bash deploy/install.sh --logs api      # 看日志
#   bash deploy/install.sh --down          # 停掉（保留数据）
#
# 设计约束：已有 deploy/.env 不覆盖；升级前需单独备份数据库与 deploy/.env。
# 前端在临时暂存目录构建并校验，确认 compose 实际挂载后再原地发布；
# 构建、镜像构建、compose up、readyz 任一步失败都不会发布新前端；
# 构建若误改在线 dist 会尝试还原；发布失败会尝试恢复发布前前端。
# 清单只做版本一致性校验（含后端发布输入，避免“后端新、前端旧”），
# 不是发布签名或安全边界。
#
# 并发：install / restart / down / build-web-only 共用一把跨 release 的锁
# （deploy/.env 解析后所在目录的 .ling-install.lock；没有 .env 时在 ROOT/deploy）。
# 锁用 Python fcntl.flock，不依赖 Linux flock(1)，也不看任何“跳过锁”的环境变量。
# 拿不到锁直接失败，不会并发构建/切换容器。锁文件必须是普通文件：符号链接、
# 目录、FIFO、设备一律拒绝（打开前 lstat、O_NOFOLLOW 打开、fstat 核对 inode），
# 避免“锁文件被替换成 symlink 后截断/破坏 victim 文件”。
# 宿主/更新执行器可以先拿这把锁，再把 FD 以 LING_INSTALL_LOCK_FD 交给本脚本
# 继承：第二次进入不再抢锁，而是在**继承来的那个 FD 本身**上要求
# flock(LOCK_EX|LOCK_NB) 成功（同一 open file description 已持 LOCK_EX 时幂等
# 成功）；同时用独立 probe 证明“这个 inode 上确实有人持锁”。因此别人持锁时
# 传入一个同 inode 但未加锁的 FD 会被拒绝，共享锁也不会被当成互斥。这样
# “备份 → 安装”整段共用同一把锁。deploy/.env 允许是指向固定“原始 env”的
# symlink（跨 release 共享）；脚本只读它、不覆盖、不把密钥复制到 release 树。
#
# 构建提交：构建前从 build arg 来源确定 LING_COMMIT（本检出 git HEAD 的 40 位
# 十六进制，非 git 检出必须显式提供）；格式不对直接拒绝，未知就留空而不是伪造。
# 该值只是镜像里的构建标记，不是发布签名。
#
# 阶段标记（可选）：LING_INSTALL_PHASE_FILE 指向宿主可信绝对路径时，按
# build → deploy → verify → complete 原子写入当前阶段，供宿主更新执行器区分
# “构建前失败”和“可能已迁移/已重启，需要人工处理”。不设置时不产生任何输出。
#
# HOME / DOCKER_CONFIG：**只在确实没有可用 HOME 时才回退**。宿主已有可写 HOME
# 时原样保留（docker context、registry 凭据都跟着 HOME/DOCKER_CONFIG 走，无条件
# 改写到仓库内目录会让“明明登录过”的私有镜像拉不到）；只有 HOME 缺失/不可写
# （老 NAS 账号）才回退 ROOT/.run/home，且仅在 DOCKER_CONFIG 未显式设置时才回退
# ROOT/.run/docker。回退目录在仓库已忽略的 .run/ 下（不会弄脏旧 checkout，也不会
# 被 docker build context 带上 registry 配置）；回退会告警说明“这里没有宿主凭据”。
# 脚本不复制、不搬移任何凭据/secret，也不覆盖/删除旧版 .home、.docker。
#
# Compose override（可选）：LING_INSTALL_COMPOSE_OVERRIDE 指向宿主/更新执行器
# 生成的追加 compose 文件（可以是 JSON——YAML 的子集），用于跨独立 release 固定
# bind 来源。只作为 `-f` 追加在 `-f deploy/compose.yaml` 之后，始终保留
# `--env-file deploy/.env`；脚本不复制、不改写、不直接解析它的文本（交给 Compose
# 解析），也不把它写进 deploy/.env。校验是严格的：绝对路径、已存在、普通文件
# （拒绝 symlink/目录/FIFO）、可读且不大于 1 MiB；不满足直接失败。
# `--build-web-only` 不碰 Docker，忽略它。
# 信任边界：override 是宿主信任输入，**不是安全沙箱**。脚本只校验结构不变量
# （/app/web 指向本次校验产物、持久命名卷 db-data/app-data 身份不变），
# 不限制 privileged/cap_add/user/network_mode/docker.sock 等字段——那些都会原样
# 交给 Compose。详见 docs/DEPLOYMENT.md 的「Compose override」一节。
#
# 对外端口：就绪等待、安装摘要与 --status 用的端口**不是**脚本写死的默认值，
# 而是 `docker compose config` 解析出的 edge 服务 target=80 的发布端口——也就是
# Compose 实际会发布到宿主的那个端口（deploy/.env 的 LING_EDGE_PORT、shell 同名
# 变量、宿主 override 都只体现在这个解析结果里）。--port 只影响新生成 .env 的
# LING_EDGE_PORT；已有 .env 时若 --port 与 Compose 实际端口冲突，脚本 fail closed
# 并提示改 env，绝不按猜测端口等 120 秒。解析失败也不猜端口。
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
ROOT=$(pwd)
# 参数解析会 shift 掉 $@，而锁的 re-exec 需要原样参数，所以先留一份。
ORIG_ARGS=("$@")
ORIG_COUNT=$#

PROJECT="ling"
PORT="18080"
PORT_EXPLICIT=0
ACTION="install"
WEB_MODE="build"
WEB_OFFLINE=0
LOG_SVC="api"
PY_BASE=""
ADMIN_USER="${ADMIN_USER:-ling}"
while [ $# -gt 0 ]; do
  case "$1" in
    --port) [ $# -ge 2 ] || { echo "--port 需要参数" >&2; exit 2; }; PORT="$2"; PORT_EXPLICIT=1; shift 2 ;;
    --project) [ $# -ge 2 ] || { echo "--project 需要参数" >&2; exit 2; }; PROJECT="$2"; shift 2 ;;
    --admin-user) [ $# -ge 2 ] || { echo "--admin-user 需要参数" >&2; exit 2; }; ADMIN_USER="$2"; export ADMIN_USER; shift 2 ;;
    --python-base) [ $# -ge 2 ] || { echo "--python-base 需要参数" >&2; exit 2; }; PY_BASE="$2"; shift 2 ;;
    --build-web-only) ACTION="build-web-only"; shift ;;
    --prebuilt-web) WEB_MODE="prebuilt"; shift ;;
    --offline-web) WEB_OFFLINE=1; shift ;;
    --status) ACTION="status"; shift ;;
    --logs) ACTION="logs"; LOG_SVC="${2:-api}"; [ $# -ge 2 ] && shift 2 || shift ;;
    --down) ACTION="down"; shift ;;
    --restart) ACTION="restart"; shift ;;
    -h|--help) sed -n '2,/^[^#]/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

if [ "$ACTION" = "build-web-only" ] && [ "$WEB_MODE" = "prebuilt" ]; then
  echo "--prebuilt-web 与 --build-web-only 不能同时使用" >&2
  exit 2
fi

# 空串不算显式配置：Compose 会把 shell 里“已导出但为空”的变量当作覆盖值，
# 从而遮住 --env-file（deploy/.env）里的私有 PIP_INDEX_URL。这里在任何 dc
# 调用之前统一清掉这种空导出，让 Compose 回退去读 deploy/.env；非空导出
# 保持不动。策略只针对 PIP_INDEX_URL，不影响其它变量的 Compose 语义。
if [ "${PIP_INDEX_URL+set}" = "set" ] && [ -z "$PIP_INDEX_URL" ]; then
  unset PIP_INDEX_URL
fi

# HOME/DOCKER_CONFIG 的取舍见下面的 ensure_docker_home：只有缺可写 HOME 时才回退。
# --build-web-only 完全不碰 HOME，也不要求 Docker；它只需要 python3 生成清单。
if [ "$ACTION" = "build-web-only" ] && ! command -v python3 >/dev/null; then
  echo "缺 python3（生成前端校验清单需要）" >&2
  exit 1
fi

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; RST=$'\033[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s!%s %s\n' "$YEL" "$RST" "$*"; }
die()  { printf '%s✗%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }

# --- HOME / DOCKER_CONFIG -------------------------------------------------
# 宿主已有可写 HOME 就原样保留：docker context 与 registry 凭据都跟着
# HOME/DOCKER_CONFIG 走，无条件改写到仓库内目录等于把宿主的 docker 身份换掉，
# 典型症状是“明明 docker login 过，构建/拉取却私有镜像 401”。
# 只有 HOME 缺失/不是可写目录（老 NAS 账号）才回退到 ROOT/.run/home；DOCKER_CONFIG
# 只有在**未被显式设置**时才跟着回退到 ROOT/.run/docker。显式设置的值一律保留
# （只在该目录缺失且父目录可写时创建目录，不覆盖里面的任何文件）。
# 回退目录放在仓库内已忽略的 .run/ 下：旧版本用的 ROOT/.home、ROOT/.docker 会让
# 旧 checkout 变 dirty，还会把 registry 配置带进 docker build context。脚本
# 不删除、不搬移用户旧目录，只是不再使用它们。
# 脚本不复制、不搬移任何凭据/secret。
ensure_docker_home() {
  local fallback_home="$ROOT/.run/home" fallback_docker="$ROOT/.run/docker"
  if [ -n "${HOME:-}" ] && [ -d "${HOME:-}" ] && [ -w "${HOME:-}" ]; then
    :
  else
    warn "HOME 缺失或不可写（当前：${HOME:-<未设置>}）；回退到 ${fallback_home}"
    warn "回退目录里没有宿主的 docker context / registry 凭据；私有镜像请先在宿主 docker login，或用 --python-base 指定已就绪的基础镜像"
    if [ -d "$ROOT/.home" ] || [ -d "$ROOT/.docker" ]; then
      warn "检测到旧版本的回退目录 ROOT/.home、ROOT/.docker：本脚本不再使用，也不会删除/搬移它们（可人工确认后自行清理）"
    fi
    mkdir -p -m 700 "$fallback_home" || die "无法创建 ${fallback_home}"
    export HOME="$fallback_home"
    if [ -z "${DOCKER_CONFIG:-}" ]; then
      mkdir -p -m 700 "$fallback_docker" || die "无法创建 ${fallback_docker}"
      export DOCKER_CONFIG="$fallback_docker"
    fi
  fi
  # 显式 DOCKER_CONFIG 一律保留：不重置、不替换成回退目录。目录不可写/建不出来
  # （比如被指到一个普通文件）时只告警——真正的失败交给 docker 自己报（它会说清
  # 楚是哪个路径），安装器提前 fail closed 反而会挡住 --down/--logs 这类恢复操作。
  if [ -n "${DOCKER_CONFIG:-}" ]; then
    if [ -d "$DOCKER_CONFIG" ]; then
      [ -w "$DOCKER_CONFIG" ] || warn "DOCKER_CONFIG=${DOCKER_CONFIG} 不可写；保留该路径，不替你换配置（docker 若需要写会自行报错）"
    else
      mkdir -p "$DOCKER_CONFIG" 2>/dev/null || warn "DOCKER_CONFIG=${DOCKER_CONFIG} 不可用；保留该路径，不替你换配置（docker 若需要写会自行报错）"
    fi
  fi
}

# --- 宿主 Compose override（可选）-----------------------------------------
# 见文件头说明：追加 `-f`，保留 `--env-file deploy/.env`，不复制/不改写内容。
# 校验放在任何 docker 调用之前，避免“前端构建三分钟之后才发现路径写错”。
COMPOSE_OVERRIDE="${LING_INSTALL_COMPOSE_OVERRIDE:-}"
validate_compose_override() {
  python3 - "$COMPOSE_OVERRIDE" <<'PY' || die "LING_INSTALL_COMPOSE_OVERRIDE 不可用（未读取其内容）"
import os
import stat
import sys

path = sys.argv[1]
if not os.path.isabs(path):
    print(f"必须是绝对路径：{path}", file=sys.stderr)
    raise SystemExit(1)
if not os.path.lexists(path):
    print(f"文件不存在：{path}", file=sys.stderr)
    raise SystemExit(1)
if os.path.islink(path):
    print(f"不能是符号链接：{path}", file=sys.stderr)
    raise SystemExit(1)
try:
    info = os.lstat(path)
except OSError as exc:
    print(f"无法读取文件属性：{exc}", file=sys.stderr)
    raise SystemExit(1)
if not stat.S_ISREG(info.st_mode):
    print(f"必须是普通文件：{path}", file=sys.stderr)
    raise SystemExit(1)
if info.st_size > 1024 * 1024:
    print(f"文件超过 1 MiB，拒绝当作 compose override：{path}", file=sys.stderr)
    raise SystemExit(1)
try:
    with open(path, "rb") as stream:
        stream.read(1)
except OSError as exc:
    print(f"文件不可读：{exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

# --- 跨 release 安装锁 ----------------------------------------------------
# 锁路径 = deploy/.env **解析后的真实目标**所在目录 + .ling-install.lock；
# 没有 deploy/.env 时是 ROOT/deploy/.ling-install.lock。
# 这样同一份 .env（包括被多个 release 树用 symlink 共享）永远落在同一把锁上，
# 不会因为“不同 release 目录各有一把锁”而并发执行。
#
# 实现：Python 拿到 fcntl.flock 后清掉 FD_CLOEXEC，再 exec bash 本脚本，
# 把锁 FD 交给新的 bash 进程继承（锁随进程退出自动释放，异常退出也不会
# 留下“锁文件存在但没人持锁”的假状态）。bash 第二次进入时不再抢锁，只验证
# 继承来的 FD 确实指向这把锁、并且**它自己就是持锁者**；验证失败就中止。
# 故意不提供“跳过锁”的开关：任何伪环境标志都不能让安装脱离互斥。
acquire_install_lock() {
  python3 - "$ROOT" "$0" "$@" <<'PY'
import fcntl
import os
import shutil
import stat
import sys

root = sys.argv[1]
script = os.path.abspath(sys.argv[2])
args = sys.argv[3:]

env_path = os.path.join(root, "deploy", ".env")
if os.path.lexists(env_path):
    # symlink 也要解析到真实目标，锁跟着 .env 实际所在目录走。
    lock_dir = os.path.dirname(os.path.realpath(env_path))
else:
    lock_dir = os.path.join(root, "deploy")
lock_path = os.path.join(lock_dir, ".ling-install.lock")

# 平台没有 O_NOFOLLOW 就 fail closed：跟随符号链接去打开/加锁意味着
# “锁文件被换成 symlink”时会把操作作用到 victim 上。
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)


def inherited_lock_is_valid(raw_fd):
    try:
        fd = int(raw_fd)
    except ValueError:
        return False
    if fd < 3:
        return False
    try:
        st = os.fstat(fd)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    # 锁路径本身也不允许是符号链接（lstat，不跟随）：防御“锁文件被替换成
    # 指向别处的 symlink”，也让下面的 O_NOFOLLOW 打开语义一致。
    try:
        target = os.lstat(lock_path)
    except OSError:
        return False
    if stat.S_ISLNK(target.st_mode):
        return False
    if (target.st_dev, target.st_ino) != (st.st_dev, st.st_ino):
        return False
    if not NOFOLLOW:
        return False
    # 第 1 步：用**独立 probe FD** 证明这个 inode 上确实有人持锁。
    # 能加上 LOCK_EX 说明没有任何进程持有它，继承来的 FD 只是个普通描述符
    # （伪造/误传）——拒绝，避免伪环境变量绕过互斥。
    try:
        probe = os.open(lock_path, os.O_RDWR | NOFOLLOW)
    except OSError:
        return False
    probe_acquired = False
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            probe_acquired = True
        except OSError:
            probe_acquired = False
        if probe_acquired:
            try:
                fcntl.flock(probe, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(probe)
    if probe_acquired:
        return False
    # 第 2 步：在**继承来的那个 FD 本身**上加 LOCK_EX。这只在两种情况下成功：
    #   - 同一个 open file description 已经持 LOCK_EX（重复 flock 幂等成功）；
    #   - 它持的是共享锁且没有别的共享持有者（升级成独占，安全）。
    # 别人持锁时，一个同 inode 但未加锁的 FD 会在这一步失败——这正是
    # “只证明有人持锁”与“证明传入的 FD 是持锁者”的区别。
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


inherited = os.environ.get("LING_INSTALL_LOCK_FD")
if inherited:
    if not inherited_lock_is_valid(inherited):
        fail(
            "LING_INSTALL_LOCK_FD 不指向本脚本持有的安装锁（FD 未加锁 / 不是这把锁 / "
            "锁由别的进程持有）；已拒绝在无锁状态下运行（不要手工设置该变量）"
        )
    raise SystemExit(0)


def open_lock_file():
    """打开锁文件：只接受“我们自己的普通文件”，绝不跟随符号链接。

    先用 lstat 拒绝 symlink/目录/FIFO/设备，再用 O_NOFOLLOW 打开（关闭
    “检查后被替换”的窗口），最后 fstat 核对 inode 与 lstat 一致且仍是普通
    文件。绝不 ftruncate 写 PID：诊断信息不值得冒“截断 victim 文件”的风险。
    """
    try:
        before = os.lstat(lock_path)
    except FileNotFoundError:
        before = None
    except OSError as exc:
        fail(f"无法检查安装锁 {lock_path}：{exc}")
    if before is not None:
        if stat.S_ISLNK(before.st_mode):
            fail(
                f"安装锁不能是符号链接：{lock_path}；"
                "请先人工确认（cat/readlink）再删除它，安装器不会跟随/截断链接目标"
            )
        if not stat.S_ISREG(before.st_mode):
            fail(f"安装锁必须是普通文件：{lock_path}（当前不是普通文件）")
    if not NOFOLLOW:
        fail("当前平台不支持 O_NOFOLLOW，拒绝打开安装锁（避免跟随符号链接）")
    try:
        handle = os.open(lock_path, os.O_CREAT | os.O_RDWR | NOFOLLOW, 0o600)
    except OSError as exc:
        fail(f"无法创建安装锁 {lock_path}：{exc}")
    try:
        opened = os.fstat(handle)
    except OSError as exc:
        os.close(handle)
        fail(f"无法读取安装锁属性 {lock_path}：{exc}")
    if not stat.S_ISREG(opened.st_mode):
        os.close(handle)
        fail(f"安装锁必须是普通文件：{lock_path}（打开后不是普通文件）")
    if before is not None and (before.st_dev, before.st_ino) != (
        opened.st_dev,
        opened.st_ino,
    ):
        os.close(handle)
        fail(
            f"安装锁在打开期间被替换：{lock_path}；"
            "已拒绝（可能有人在用符号链接抢锁），请人工检查该路径"
        )
    return handle


fd = open_lock_file()
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    fail(
        f"另一个 install.sh 正在运行（锁：{lock_path}）；"
        "请等它结束后再试（安装/重启/停止/仅构建前端都共用这把锁）"
    )

flags = fcntl.fcntl(fd, fcntl.F_GETFD)
fcntl.fcntl(fd, fcntl.F_SETFD, flags & ~fcntl.FD_CLOEXEC)
env = os.environ.copy()
env["LING_INSTALL_LOCK_FD"] = str(fd)
bash = shutil.which("bash") or "/bin/bash"
os.execve(bash, [bash, script, *args], env)
PY
}

# --- 阶段标记（可选，给宿主更新执行器看）---------------------------------
# 只在 LING_INSTALL_PHASE_FILE 非空时生效。该变量由宿主/执行器提供，必须是
# 绝对路径；文件必须是普通文件（拒绝 symlink），不通过 Compose 暴露给 Web。
# 单阶段原子写：同目录临时文件 + fsync + rename，读到的永远是完整阶段名。
# 写失败必须中止整个安装——否则部署已经动了而标记还停在旧阶段，执行器会误判。
PHASE_FILE="${LING_INSTALL_PHASE_FILE:-}"
phase_write() {
  [ -n "$PHASE_FILE" ] || return 0
  python3 - "$PHASE_FILE" "$1" <<'PY' || die "无法写入安装阶段标记：${PHASE_FILE}（已在当前阶段中止）"
import os
import sys
import tempfile

path, stage = sys.argv[1], sys.argv[2]
allowed = {"build", "deploy", "verify", "complete"}
if stage not in allowed:
    print(f"未知安装阶段：{stage}", file=sys.stderr)
    raise SystemExit(1)
if not os.path.isabs(path):
    print(f"LING_INSTALL_PHASE_FILE 必须是绝对路径：{path}", file=sys.stderr)
    raise SystemExit(1)
if os.path.islink(path):
    print(f"阶段标记不能是符号链接：{path}", file=sys.stderr)
    raise SystemExit(1)
if os.path.exists(path) and not os.path.isfile(path):
    print(f"阶段标记必须是普通文件：{path}", file=sys.stderr)
    raise SystemExit(1)
parent = os.path.dirname(path)
if not os.path.isdir(parent):
    print(f"阶段标记所在目录不存在：{parent}", file=sys.stderr)
    raise SystemExit(1)

fd = None
tmp = None
try:
    fd, tmp = tempfile.mkstemp(prefix=".ling-phase-", dir=parent)
    os.write(fd, f"{stage}\n".encode())
    os.fsync(fd)
    os.close(fd)
    fd = None
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    tmp = None
    dir_fd = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
except OSError as exc:
    print(f"写入阶段标记失败：{exc}", file=sys.stderr)
    raise SystemExit(1)
finally:
    if fd is not None:
        os.close(fd)
    if tmp is not None:
        try:
            os.unlink(tmp)
        except OSError:
            pass
PY
}

# --- 构建提交 ------------------------------------------------------------
# 只认两类来源：显式 LING_COMMIT（宿主执行器会传正确的 40 位 sha）或本检出
# 的 git HEAD。非 git 检出（源码归档）且没有显式值时留空并明确告警：
# /v1/version 会报 commit=null，而不是把未知提交伪造成真实提交。
resolve_commit() {
  local provided resolved=""
  provided="${LING_COMMIT:-}"
  if [ -n "$provided" ]; then
    case "$provided" in
      *[!0-9a-fA-F]*) die "LING_COMMIT 格式错误：必须是 40 位十六进制提交号（未打印原值）" ;;
    esac
    [ "${#provided}" -eq 40 ] || die "LING_COMMIT 格式错误：必须是 40 位十六进制提交号（未打印原值）"
    resolved=$(printf '%s' "$provided" | tr 'A-F' 'a-f')
  elif command -v git >/dev/null 2>&1 && resolved=$(git -C "$ROOT" rev-parse --verify HEAD 2>/dev/null); then
    case "$resolved" in
      *[!0-9a-f]*) die "从 git 读到的 HEAD 不是 40 位十六进制提交号；拒绝继续（请检查仓库状态）" ;;
    esac
    [ "${#resolved}" -eq 40 ] || die "从 git 读到的 HEAD 不是完整提交号；拒绝继续（请检查仓库状态）"
  else
    warn "无法确定构建提交（当前目录不是 git 检出，且未设置 LING_COMMIT）；镜像不会标注提交号"
    warn "宿主更新执行器应从确切的 release 提交导出 LING_COMMIT；不要把这份镜像当成可追溯的发布"
  fi
  export LING_COMMIT="$resolved"
  if [ -n "$resolved" ]; then
    ok "构建提交：$resolved"
  fi
}

# 每次调用都重新判断 deploy/.env：首次安装时 .env 由 gen_env 在脚本启动后
# 才生成，预先组装的数组会漏掉 --env-file。不要依赖 Compose 的自动 .env 查找。
# 宿主 override（若配置）永远追加在 `-f deploy/compose.yaml` 之后：它在 Compose
# 语义里就是“后者覆盖前者”，且这里只追加路径——内容由宿主/更新执行器维护，
# 脚本不复制、不改写、不解析（因此也不会把里面的密钥带进日志或 deploy/.env）。
dc() {
  local -a cmd=(docker compose -f deploy/compose.yaml)
  if [ -n "$COMPOSE_OVERRIDE" ]; then
    cmd+=(-f "$COMPOSE_OVERRIDE")
  fi
  cmd+=(--project-name "$PROJECT")
  if [ -f deploy/.env ]; then
    cmd+=(--env-file deploy/.env)
  fi
  "${cmd[@]}" "$@"
}

WEB_DIR="clients/web-console"
LIVE_DIST="$ROOT/$WEB_DIR/dist"
TMP_BASE=""
PREV_DIST=""
WEB_STAGING=""
HAS_PREV=0
PUBLISHED=0
KEEP_TMP=0
COMPOSE_PIP_STATE=""

# 临时目录同时放“发布前备份”和“暂存构建产物”。备份用于：
# 1) 构建设置了 --outDir 却仍然改了在线 dist 时自动还原；
# 2) 发布前端失败（包括 inode/清单校验失败）时恢复发布前版本。
need_tmp() {
  if [ -n "$TMP_BASE" ]; then return 0; fi
  TMP_BASE=$(mktemp -d "${TMPDIR:-/tmp}/ling-install.XXXXXX") || die "无法创建临时目录"
  PREV_DIST="$TMP_BASE/previous-dist"
  WEB_STAGING="$TMP_BASE/web-dist-staging"
  mkdir -p "$PREV_DIST" || die "无法创建临时目录"
}

cleanup() {
  local rc=$?
  if [ -n "$TMP_BASE" ] && [ -d "$TMP_BASE" ]; then
    if [ "$KEEP_TMP" = "1" ]; then
      printf '%s!%s 保留安装临时目录供人工恢复：%s\n' "$YEL" "$RST" "$TMP_BASE" >&2
    else
      rm -rf "$TMP_BASE"
    fi
  fi
  exit "$rc"
}
trap cleanup EXIT

real_path() {
  python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1"
}

# 设备号 + inode。用来证明“原地替换内容”没有换掉被 bind mount 的目录。
path_id() {
  python3 - "$1" <<'PY'
import os
import sys

try:
    st = os.stat(sys.argv[1])
except OSError:
    print("")
else:
    print("%d:%d" % (st.st_dev, st.st_ino))
PY
}

# 把 SRC 的内容原地同步到 DST：DST 目录本身（inode）不换，只替换内容。
# 每个文件先写同目录临时文件再 os.replace，index.html 最后替换；最后清理旧
# 条目。这样旧 index 在替换前仍能看到旧资源，替换后只引用已经拷好的新资源；
# 但已加载旧 index 的客户端仍可能在清理后短暂 404。SRC 里出现符号链接直接拒绝。
# 这里只保证“单文件替换”和“先资源后 index”的顺序，不保证多文件同时可见，
# 也没有 fsync 掉电保证。
tree_replace() {
  python3 - "$1" "$2" <<'PY'
import os
import pathlib
import shutil
import sys
import tempfile

src = pathlib.Path(sys.argv[1])
dst = pathlib.Path(sys.argv[2])


def fail(message):
    print(f"前端目录同步失败：{message}", file=sys.stderr)
    raise SystemExit(1)


def copy_atomic(source, target):
    """同目录临时文件 + os.replace；避免读者看到写了一半的文件。"""
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".ling-tmp-", dir=str(target.parent))
        os.close(fd)
    except OSError as exc:
        fail(f"无法在 {target.parent} 创建临时文件：{exc}")
    try:
        shutil.copy2(str(source), tmp_name)
        os.replace(tmp_name, str(target))
    except OSError as exc:
        fail(f"写入 {target} 失败：{exc}")
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


if src.is_symlink() or not src.is_dir():
    fail("源必须是普通目录")
if dst.is_symlink():
    fail("目标目录不能是符号链接")
try:
    dst.mkdir(parents=True, exist_ok=True)
except OSError as exc:
    fail(f"无法创建目标目录：{exc}")

src_dirs = set()
src_files = set()
for root, dirs, files in os.walk(src, followlinks=False):
    root_path = pathlib.Path(root)
    dirs.sort()
    files.sort()
    for name in list(dirs):
        full = root_path / name
        if full.is_symlink():
            fail(f"源目录内不接受符号链接：{full.relative_to(src).as_posix()}")
        src_dirs.add(full.relative_to(src).as_posix())
    for name in files:
        full = root_path / name
        if full.is_symlink():
            fail(f"源目录内不接受符号链接：{full.relative_to(src).as_posix()}")
        src_files.add(full.relative_to(src).as_posix())

# 先解决“文件/目录同名冲突”，否则 mkdir 或 copy 会失败。
for rel in sorted(src_dirs):
    target = dst / rel
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        target.unlink()
for rel in sorted(src_files):
    target = dst / rel
    if target.is_symlink() or (target.exists() and not target.is_file()):
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()

for rel in sorted(src_dirs):
    try:
        (dst / rel).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fail(f"无法创建目录 {rel}：{exc}")

ordered = [rel for rel in sorted(src_files) if rel != "index.html"]
if "index.html" in src_files:
    ordered.append("index.html")
for rel in ordered:
    source = src / rel
    target = dst / rel
    if target.is_symlink() or (target.exists() and not target.is_file()):
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fail(f"无法创建目录 {rel} 的父目录：{exc}")
    copy_atomic(source, target)

# 最后清掉源里不存在的旧条目（保留 dst 目录自身）。
for root, dirs, files in os.walk(dst, topdown=False, followlinks=False):
    root_path = pathlib.Path(root)
    dirs.sort()
    files.sort()
    for name in files:
        full = root_path / name
        rel = full.relative_to(dst).as_posix()
        if rel not in src_files:
            full.unlink()
    for name in dirs:
        full = root_path / name
        rel = full.relative_to(dst).as_posix()
        if full.is_symlink():
            if rel not in src_files:
                full.unlink()
        elif rel not in src_dirs:
            shutil.rmtree(full)
PY
}

# 两棵目录树是否完全一致（普通文件按内容哈希，符号链接按目标字符串）。
tree_equal() {
  python3 - "$1" "$2" <<'PY'
import hashlib
import os
import pathlib
import sys


def scan(root_raw):
    root = pathlib.Path(root_raw)
    found = {}
    if root.is_symlink():
        return {"<root>": "symlink"}
    if not root.exists():
        return found
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = pathlib.Path(current)
        dirs.sort()
        files.sort()
        for name in dirs:
            full = current_path / name
            rel = full.relative_to(root).as_posix()
            if full.is_symlink():
                found[rel] = "symlink:" + os.readlink(full)
        for name in files:
            full = current_path / name
            rel = full.relative_to(root).as_posix()
            if full.is_symlink():
                found[rel] = "symlink:" + os.readlink(full)
                continue
            digest = hashlib.sha256()
            with full.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            found[rel] = digest.hexdigest()
    return found


sys.exit(0 if scan(sys.argv[1]) == scan(sys.argv[2]) else 1)
PY
}

# 发布前备份在线 dist。在线 dist 是符号链接时直接失败：既不能保证
# bind mount 的 inode 语义，也不能保证回滚是同一份内容。
snapshot_live_dist() {
  need_tmp
  rm -rf "$PREV_DIST" || die "无法清理临时备份目录"
  mkdir -p "$PREV_DIST" || die "无法创建临时备份目录"
  HAS_PREV=0
  if [ -L "$LIVE_DIST" ]; then
    die "在线前端目录是符号链接：${LIVE_DIST}；拒绝自动替换（请先人工确认真实目录）"
  fi
  if [ -e "$LIVE_DIST" ] && [ ! -d "$LIVE_DIST" ]; then
    die "在线前端路径不是目录：$LIVE_DIST"
  fi
  if [ -d "$LIVE_DIST" ]; then
    python3 - "$LIVE_DIST" "$PREV_DIST" <<'PY' || die "无法备份在线 dist"
import os
import pathlib
import shutil
import sys

src = pathlib.Path(sys.argv[1])
dst = pathlib.Path(sys.argv[2])
for root, dirs, files in os.walk(src, followlinks=False):
    current = pathlib.Path(root)
    for name in list(dirs) + files:
        if (current / name).is_symlink():
            print(
                f"在线 dist 内不接受符号链接：{(current / name).relative_to(src).as_posix()}",
                file=sys.stderr,
            )
            raise SystemExit(1)
shutil.copytree(str(src), str(dst), dirs_exist_ok=True)
PY
    HAS_PREV=1
  fi
}

# 构建前后对比：在线 dist 是否保持原样。outDir 覆盖失效时会被这里抓住，
# 尝试还原后退出；还原失败会保留临时备份并明确报错，不会继续启动/发布。
live_unchanged() {
  if [ "$HAS_PREV" = "1" ]; then
    tree_equal "$LIVE_DIST" "$PREV_DIST"
    return $?
  fi
  if [ -e "$LIVE_DIST" ] && [ ! -d "$LIVE_DIST" ]; then
    return 1
  fi
  if [ -d "$LIVE_DIST" ]; then
    python3 - "$LIVE_DIST" <<'PY'
import os
import sys

for root, dirs, files in os.walk(sys.argv[1], followlinks=False):
    if dirs or files:
        raise SystemExit(1)
raise SystemExit(0)
PY
    return $?
  fi
  return 0
}

restore_live_dist() {
  need_tmp
  if [ ! -d "$PREV_DIST" ]; then return 1; fi
  tree_replace "$PREV_DIST" "$LIVE_DIST"
}

# 清单输入 = 前端构建配置/src/public + 后端发布输入。后端也进清单，
# 才能在 --prebuilt-web 时拒绝“后端源码已变、前端还是旧包”的版本错配。
# dist 只收录普通文件，拒绝任何 symlink；不遍历 node_modules、.git、缓存等目录。
# 它只证明 dist 与当前发布输入一致，不是发布签名，不能当安全边界。
WEB_MANIFEST_NAME=".ling-web-manifest.json"
web_manifest() {
  local mode="${1:-}"
  local target="${2:-}"
  case "$mode" in
    create|verify) ;;
    *) die "未知清单操作：$mode" ;;
  esac
  [ -n "$target" ] || die "缺少前端清单目标目录"
  python3 - "$mode" "$ROOT" "$WEB_DIR" "$target" "$WEB_MANIFEST_NAME" <<'PY'
import hashlib
import json
import os
import pathlib
import sys

mode, root_raw, web_raw, target_raw, manifest_name = sys.argv[1:6]
root = pathlib.Path(root_raw)
web = root / web_raw
target = pathlib.Path(target_raw)
manifest = target / manifest_name

web_inputs = ["package.json", "pnpm-lock.yaml", "index.html", "tsconfig.json", "vite.config.ts"]
backend_files = [
    "pyproject.toml",
    "README.md",
    "alembic.ini",
    "deploy/Dockerfile.api",
    "deploy/entrypoint.sh",
]
backend_dirs = ["src", "connectors", "migrations"]
skip_names = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".venv",
    "node_modules",
    ".git",
    ".DS_Store",
}


def fail(message):
    print(f"前端校验失败：{message}", file=sys.stderr)
    raise SystemExit(1)


def skipped(name):
    return name in skip_names or name.endswith((".pyc", ".pyo")) or name.startswith("._")


def checked_regular(base, relative):
    rel = pathlib.PurePosixPath(relative)
    if rel.is_absolute() or not rel.parts or any(p in ("", ".", "..") for p in rel.parts):
        fail("非法路径")
    cur = base
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            fail("不接受符号链接")
    try:
        cur.resolve(strict=True).relative_to(base.resolve(strict=True))
    except (OSError, ValueError):
        fail("路径越界或文件不存在")
    if not cur.is_file():
        fail("缺少普通文件")
    return cur


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def add_tree(base, relative, required, prefix):
    tree = base / relative
    if tree.is_symlink():
        fail("不接受符号链接")
    if not tree.exists():
        if required:
            fail(f"缺少 {relative} 目录")
        return
    if not tree.is_dir():
        fail(f"{relative} 必须是目录")
    for current, dirs, files in os.walk(tree, followlinks=False):
        current_path = pathlib.Path(current)
        dirs[:] = sorted(name for name in dirs if not skipped(name))
        files.sort()
        for name in dirs:
            if (current_path / name).is_symlink():
                fail(f"{relative} 内不接受符号链接")
        for name in files:
            if skipped(name):
                continue
            full = current_path / name
            tree_relative = full.relative_to(tree).as_posix()
            checked_regular(tree, tree_relative)
            source[prefix + relative + "/" + tree_relative] = sha(full)


def scan_dist(base):
    if base.is_symlink() or not base.is_dir():
        fail("缺少普通 dist 目录")
    found = {}
    for current, dirs, files in os.walk(base, followlinks=False):
        current_path = pathlib.Path(current)
        dirs.sort()
        files.sort()
        for name in dirs:
            if (current_path / name).is_symlink():
                fail("dist 内不接受符号链接")
        for name in files:
            full = current_path / name
            relative = full.relative_to(base).as_posix()
            checked_regular(base, relative)
            found[relative] = sha(full)
    return found


source = {}
for rel in web_inputs:
    source[rel] = sha(checked_regular(web, rel))
add_tree(web, "src", True, "")
add_tree(web, "public", False, "")
for rel in backend_files:
    source["backend/" + rel] = sha(checked_regular(root, rel))
for rel in backend_dirs:
    add_tree(root, rel, True, "backend/")

if mode == "create":
    if manifest.is_symlink():
        fail("清单不能是符号链接")
    if target.is_symlink() or not target.is_dir():
        fail("缺少普通 dist 目录")
    assets = scan_dist(target)
    assets.pop(manifest_name, None)
    if "index.html" not in assets or len(assets) < 2:
        fail("dist 必须含 index.html 和至少一个构建资源")
    data = {"schema": 1, "release_inputs": source, "dist_files": assets}
    manifest.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
elif mode == "verify":
    if manifest.is_symlink() or not manifest.is_file():
        fail("缺少前端清单，请在同一发布源码上重新构建")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        fail("清单格式无效")
    if not isinstance(data, dict) or data.get("schema") != 1:
        fail("清单 schema 不支持")
    if data.get("release_inputs") != source:
        fail("源码/锁文件与预构建产物不是同一发布版本")
    assets = data.get("dist_files")
    if not isinstance(assets, dict) or "index.html" not in assets or len(assets) < 2:
        fail("清单缺少 index 或构建资源")
    actual = {}
    for rel, expected in assets.items():
        if not isinstance(rel, str) or not isinstance(expected, str) or len(expected) != 64:
            fail("清单文件项无效")
        actual[rel] = sha(checked_regular(target, rel))
        if actual[rel] != expected:
            fail(f"产物哈希不匹配：{rel}")
    disk = set(scan_dist(target))
    disk.discard(manifest_name)
    if disk != set(actual):
        fail("dist 文件集合与清单不一致")
else:
    fail("未知清单操作")
PY
}

# 不解析 deploy/.env 或 override 的文本，而是让 `docker compose config` 自己解析，
# 并以它的解析结果为准（含 shell 环境变量优先级）。这里只做三件事：
#   1) api 的 /app/web 必须正好挂本次校验产物（唯一、bind、只读）；
#   2) 有宿主 override 时，基础 compose 与 base+override 的**持久命名卷**必须在
#      卷 identity（name/driver/driver_opts/external）以及所有引用它们的挂载
#      （服务/target/只读）上完全一致；边缘配置/证书、更新通道这类 bind 挂载
#      和本次 release 的 /app/web 不在此限（前者本来就该由宿主固定，后者由
#      install.sh 自己核对）；
#   3) 把 compose 解析出的基础镜像名带回 bash（只含镜像名，不含任何密钥）。
# 绝不打印整份配置，也不把它写进临时文件：里面有数据库口令和 root key。
compose_layout() {
  need_tmp
  python3 - "$PROJECT" "$COMPOSE_OVERRIDE" <<'PY'
import json
import os
import re
import subprocess
import sys


def fail(message, code=4):
    print(message, file=sys.stderr)
    raise SystemExit(code)


project = sys.argv[1]
override = sys.argv[2]

base_cmd = ["docker", "compose", "-f", "deploy/compose.yaml", "--project-name", project]
if os.path.isfile("deploy/.env"):
    base_cmd += ["--env-file", "deploy/.env"]


def resolve(extra=()):
    """让 Compose 解析并在内存里读 JSON；任何输出都不打印、不落盘。"""
    try:
        proc = subprocess.run(
            [*base_cmd, *extra, "config", "--format", "json"],
            capture_output=True,
            text=True,
        )
    except OSError:
        fail("无法执行 docker compose config", 6)
    if proc.returncode != 0:
        # 绝不打印 compose 的 stdout/stderr：里面有解析后的口令与 root key。
        fail(
            f"docker compose config 执行失败（退出码 {proc.returncode}；"
            "未打印其输出，避免泄露 .env）",
            6,
        )
    try:
        config = json.loads(proc.stdout)
    except Exception:
        fail("docker compose config 的 JSON 输出无法解析", 3)
    if not isinstance(config, dict):
        fail("docker compose config 输出不是对象", 3)
    services = config.get("services")
    if not isinstance(services, dict):
        fail("compose 配置里没有 services", 4)
    return config


def volume_parts(item):
    if isinstance(item, dict):
        volume_type = item.get("type") or ""
        source = item.get("source") or ""
        target = item.get("target") or ""
        read_only = item.get("read_only")
        if isinstance(read_only, str):
            read_only = read_only.lower() == "true"
        return str(volume_type), str(source), str(target), bool(read_only)
    if isinstance(item, str):
        parts = item.split(":")
        if len(parts) < 2:
            return None
        source = parts[0]
        target = parts[1]
        mode = parts[2] if len(parts) > 2 else ""
        volume_type = "bind" if source.startswith(("/", ".", "~")) else "volume"
        return volume_type, source, target, "ro" in mode.split(",")
    return None


def top_volume_defs(config):
    """顶层 volumes 的 identity：name/driver/driver_opts/external（缺省归一化）。"""
    raw = config.get("volumes")
    defs = {}
    if not isinstance(raw, dict):
        return defs
    for key, spec in raw.items():
        if not isinstance(key, str):
            continue
        if not isinstance(spec, dict):
            spec = {}
        name = spec.get("name")
        driver = spec.get("driver") or "local"
        external = spec.get("external")
        if isinstance(external, dict):
            external = bool(external.get("name"))
        else:
            external = bool(external)
        opts = spec.get("driver_opts")
        if isinstance(opts, dict):
            opts_tuple = tuple(sorted((str(k), str(v)) for k, v in opts.items()))
        else:
            opts_tuple = ()
        defs[key] = {
            "name": name if isinstance(name, str) else "",
            "driver": str(driver),
            "external": external,
            "driver_opts": opts_tuple,
        }
    return defs


_ANONYMOUS_VOLUME = re.compile(r"^[0-9a-f]{32,64}$")


def volume_mounts(config, defs):
    """{(service, target): (type, 归一化卷身份, read_only)}。

    Compose 的 config 输出可能把命名卷 source 写成卷 key，也可能写成解析后的
    实际卷名（project_<key>）；两种都归一化回顶层 key。匿名卷每次解析都会生成
    不同的随机名，统一成 <anonymous>，避免被误判成“换卷”。bind 保留原始路径。
    """
    key_by_name = {spec["name"]: key for key, spec in defs.items() if spec["name"]}
    result = {}
    services = config.get("services")
    if not isinstance(services, dict):
        return result
    for service_name, service in services.items():
        if not isinstance(service_name, str) or not isinstance(service, dict):
            continue
        for item in service.get("volumes") or []:
            parsed = volume_parts(item)
            if parsed is None:
                continue
            volume_type, source, target, read_only = parsed
            if volume_type == "volume":
                if not source or _ANONYMOUS_VOLUME.match(source):
                    # Compose 不为匿名卷写 source（运行时才生成随机名），
                    # 少数版本会写随机 hex；两种都归一化，避免误判成“换卷”。
                    source = "<anonymous>"
                elif source in defs:
                    pass
                elif source in key_by_name:
                    source = key_by_name[source]
            result[(service_name, target)] = (volume_type, source, read_only)
    return result


def check_persistent_volumes(base, merged):
    """基础 compose 的持久命名卷不允许被宿主 override 搬走/换身份。

    比较两件事：基础 compose 声明的顶层卷 identity（name/driver/driver_opts/
    external），以及基础 compose 里每一处命名卷挂载的 服务/target/只读/卷身份。
    换掉或删掉任何一处都拒绝；新增挂载、以及合法的 edge 配置/证书 bind 不受
    影响。本次 release 的 /app/web 由上面的不变量单独核对。
    """
    base_defs = top_volume_defs(base)
    merged_defs = top_volume_defs(merged)
    for key in sorted(base_defs):
        want = base_defs[key]
        got = merged_defs.get(key)
        if got is None:
            fail(f"宿主 override 删除了持久数据卷定义：{key}；拒绝启动", 5)
        if got != want:
            fail(
                f"宿主 override 改变了持久数据卷定义：{key}"
                "（name/driver/driver_opts/external 之一）；拒绝启动",
                5,
            )

    base_mounts = volume_mounts(base, base_defs)
    merged_mounts = volume_mounts(merged, merged_defs or base_defs)
    for key in sorted(base_mounts):
        volume_type, source, read_only = base_mounts[key]
        # 匿名卷本身没有跨重启身份，也不承载“已知的持久数据”；只核对命名卷。
        if volume_type != "volume" or source == "<anonymous>":
            continue
        service_name, target = key
        want = base_mounts[key]
        got = merged_mounts.get(key)
        if got is None:
            fail(
                f"宿主 override 删除了持久数据卷挂载：{service_name} 的 {target}"
                f"（卷 {source}）；拒绝启动",
                5,
            )
        if got != want:
            fail(
                f"宿主 override 改变了持久数据卷挂载：{service_name} 的 {target}"
                f"（卷 {source}）；拒绝启动",
                5,
            )


def env_value(environment, key):
    if isinstance(environment, dict):
        return environment.get(key)
    if isinstance(environment, list):
        for item in environment:
            if isinstance(item, str):
                if item == key:
                    return ""
                if item.startswith(key + "="):
                    return item.split("=", 1)[1]
            elif isinstance(item, dict) and key in item:
                return item[key]
    return None


def build_arg(service, key):
    build = service.get("build") if isinstance(service, dict) else None
    args = build.get("args") if isinstance(build, dict) else None
    if isinstance(args, dict):
        value = args.get(key)
        return value if isinstance(value, str) else ""
    if isinstance(args, list):
        for item in args:
            if isinstance(item, str):
                if item == key:
                    return ""
                if item.startswith(key + "="):
                    return item.split("=", 1)[1]
            elif isinstance(item, dict):
                value = item.get(key)
                if isinstance(value, str):
                    return value
    return ""


def service_image(services, name):
    service = services.get(name)
    if not isinstance(service, dict):
        return ""
    image = service.get("image")
    return image if isinstance(image, str) else ""


def edge_published_port(services):
    """edge 服务 target=80 的宿主发布端口（Compose 解析后的最终值）。

    这是“Compose 实际会发布到哪个端口”的唯一权威来源：deploy/.env 的
    LING_EDGE_PORT、shell 同名变量、宿主 override 都只体现在这个解析结果里。
    取不到（缺 edge/ports、端口是区间/随机分配、非数字）就返回空串，由调用方
    fail closed——绝不回退到脚本里写死的默认端口去等就绪。
    """
    edge = services.get("edge")
    if not isinstance(edge, dict):
        return ""
    ports = edge.get("ports")
    if not isinstance(ports, list):
        return ""
    for item in ports:
        published = None
        if isinstance(item, dict):
            target = item.get("target")
            container_port = None
            if isinstance(target, bool):
                container_port = None
            elif isinstance(target, int):
                container_port = target
            elif isinstance(target, str) and target.strip().isdigit():
                container_port = int(target.strip())
            if container_port != 80:
                continue
            published = item.get("published")
        elif isinstance(item, str):
            # 兼容短语法 "18080:80" / "127.0.0.1:18080:80/tcp"。
            parts = item.split(":")
            if len(parts) < 2:
                continue
            if parts[-1].split("/")[0].strip() != "80":
                continue
            published = parts[-2].strip()
        else:
            continue
        if isinstance(published, bool):
            continue
        text = str(published).strip()
        if text.isdigit() and 0 < int(text) < 65536:
            return text
    return ""


# 基础 compose 与 base+override：只有传了 override 才多做一次只读解析。
base = None
if override:
    base = resolve()
    config = resolve(["-f", override])
else:
    config = resolve()
services = config["services"]

# --- /app/web 不变量 -------------------------------------------------------
mounts = []
for service_name, service in services.items():
    if not isinstance(service, dict):
        continue
    for item in service.get("volumes") or []:
        parsed = volume_parts(item)
        if parsed is None:
            continue
        if parsed[2] == "/app/web":
            mounts.append((service_name, parsed))

if not mounts:
    fail("compose 里没有任何服务挂载 /app/web")
if len(mounts) != 1:
    names = ", ".join(sorted(name for name, _ in mounts))
    fail(f"/app/web 挂载不唯一（出现在：{names}）")
service_name, (volume_type, source, target, read_only) = mounts[0]
if service_name != "api":
    fail(f"/app/web 应挂在 api 上，实际挂在 {service_name} 上")
if volume_type != "bind":
    fail(f"api 的 /app/web 不是 bind mount（type={volume_type or '未知'}）")
if not source:
    fail("api 的 /app/web 挂载缺少宿主机源路径")
if not read_only:
    fail("api 的 /app/web 必须是只读挂载（compose 里应为 :ro）")

environment = (services.get("api") or {}).get("environment") or {}
if env_value(environment, "LING_WEB_DIST") != "/app/web":
    fail("api 环境里的 LING_WEB_DIST 不是 /app/web")

# --- 持久命名卷不变量（仅在有宿主 override 时）-----------------------------
if base is not None:
    check_persistent_volumes(base, config)

# --- 基础镜像名（只回传镜像名，不含密钥）-----------------------------------
print(
    json.dumps(
        {
            "web": source,
            "edge_port": edge_published_port(services),
            "images": {
                "python": build_arg(services.get("api") or {}, "LING_PYTHON_BASE"),
                "worker_python": build_arg(services.get("worker") or {}, "LING_PYTHON_BASE"),
                "postgres": service_image(services, "db"),
                "nginx": service_image(services, "edge"),
            },
        },
        sort_keys=True,
    )
)
PY
}

# COMPOSE_LAYOUT：compose_layout 的 JSON 结果（web 源 + 镜像名），只在本进程用。
COMPOSE_LAYOUT=""

# 从 COMPOSE_LAYOUT 里取一个字段；缺失/解析失败一律输出空串（调用方据此 fail）。
layout_field() {
  python3 -c '
import json
import sys

try:
    data = json.loads(sys.argv[1])
except Exception:
    data = None
value = data
for part in sys.argv[2].split("."):
    value = value.get(part) if isinstance(value, dict) else None
print(value if isinstance(value, str) else "")
' "$COMPOSE_LAYOUT" "$1"
}

# --- 对外端口：以 Compose 解析出的 edge 发布端口为准 ----------------------
# USE_PORT = “Compose 实际会把服务发布到宿主哪个端口”。它绝不等于脚本里写死的
# 默认值：deploy/.env 的 LING_EDGE_PORT、shell 同名变量、宿主 override 都只体现
# 在 `docker compose config` 的解析结果里。install 复用 verify_compose_layout 的
# 同一次解析（不额外调用 Compose）；restart / --status 在这里现解析一次（只读）。
# 取不到就返回非 0，由调用方 fail closed 或明确告警——不按猜测端口等 120 秒。
USE_PORT=""

adopt_compose_edge_port() {
  local port
  port=$(layout_field edge_port)
  if [ -z "$port" ]; then
    die "无法从 compose 配置确定对外端口（edge 没有 target=80 的确定发布端口）；安装器不按猜测的端口等待就绪。请检查 deploy/compose.yaml 的 edge ports 与 deploy/.env 的 LING_EDGE_PORT"
  fi
  if [ "$PORT_EXPLICIT" = "1" ] && [ "$port" != "$PORT" ]; then
    die "--port ${PORT} 与 compose 实际发布的端口 ${port} 不一致（后者来自 deploy/.env 的 LING_EDGE_PORT 或 shell 环境）。安装器不写 deploy/.env，请改 deploy/.env 的 LING_EDGE_PORT 或去掉 --port"
  fi
  if [ "$port" != "$PORT" ]; then
    warn "对外端口以 compose 解析结果为准：${port}（脚本默认是 ${PORT}；这里来自 deploy/.env 的 LING_EDGE_PORT 或 shell 环境）"
  fi
  USE_PORT="$port"
  ok "对外端口（compose 解析）：${USE_PORT}"
}

# restart / --status 用：单独解析一次，取 edge 的 target=80 宿主端口。
# 规则与 compose_layout 里的 edge_published_port 一致（dict 长语法与短语法都认），
# 但只做端口判定，不校验 /app/web 与持久卷——诊断/重启不该因为别的不变量失败
# 而连端口都拿不到。只输出端口数字，绝不打印配置内容。
compose_edge_port() {
  need_tmp
  local py state="$TMP_BASE/edge-port.state"
  local -a pipe_rc
  USE_PORT=""
  py=$(cat <<'PY'
import json
import sys


def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(4)


try:
    config = json.load(sys.stdin)
except Exception:
    fail("docker compose config 的 JSON 输出无法解析")
if not isinstance(config, dict):
    fail("compose 配置输出不是对象")
services = config.get("services")
if not isinstance(services, dict):
    fail("compose 配置里没有 services")
edge = services.get("edge")
if not isinstance(edge, dict):
    fail("compose 配置里没有 edge 服务")
ports = edge.get("ports")
if not isinstance(ports, list):
    fail("compose 配置里 edge 没有发布端口")
for item in ports:
    published = None
    if isinstance(item, dict):
        target = item.get("target")
        container_port = None
        if isinstance(target, bool):
            container_port = None
        elif isinstance(target, int):
            container_port = target
        elif isinstance(target, str) and target.strip().isdigit():
            container_port = int(target.strip())
        if container_port != 80:
            continue
        published = item.get("published")
    elif isinstance(item, str):
        parts = item.split(":")
        if len(parts) < 2:
            continue
        if parts[-1].split("/")[0].strip() != "80":
            continue
        published = parts[-2].strip()
    else:
        continue
    if isinstance(published, bool):
        continue
    text = str(published).strip()
    if text.isdigit() and 0 < int(text) < 65536:
        print(text)
        raise SystemExit(0)
fail("compose 配置里 edge 的 target=80 端口不是确定的宿主端口（可能是区间/随机分配）")
PY
)
  rm -f "$state"
  dc config --format json 2>/dev/null | python3 -c "$py" >"$state"
  pipe_rc=("${PIPESTATUS[@]}")
  if [ "${pipe_rc[0]}" -ne 0 ]; then
    rm -f "$state"
    printf 'docker compose config 执行失败（退出码 %s；未打印其输出，避免泄露 .env）\n' "${pipe_rc[0]}" >&2
    return 6
  fi
  if [ "${pipe_rc[1]}" -ne 0 ]; then
    rm -f "$state"
    return "${pipe_rc[1]}"
  fi
  USE_PORT=$(cat "$state")
  rm -f "$state"
  case "$USE_PORT" in
    ''|*[!0-9]*) USE_PORT=""; printf '无法确定 edge 的对外端口\n' >&2; return 4 ;;
  esac
  return 0
}

ensure_use_port() {
  [ -n "$USE_PORT" ] && return 0
  compose_edge_port
}

verify_compose_layout() {
  need_tmp
  local out rc reason source actual expected
  out=$(compose_layout 2>"$TMP_BASE/layout.err")
  rc=$?
  if [ "$rc" -ne 0 ]; then
    reason=$(tail -n 1 "$TMP_BASE/layout.err" 2>/dev/null || true)
    if [ "$rc" -eq 6 ]; then
      # Compose 自己解析失败：保持这条可操作的提示（不打印配置内容）。
      die "无法从 compose 配置确认 api 的 /app/web 挂载：${reason:-docker compose config 失败（未打印配置内容）}"
    fi
    # 解析成功但布局/持久卷不变量不成立：不要说成“config 失败”。
    die "compose 配置校验失败（未启动任何容器）：${reason:-未知原因}"
  fi
  COMPOSE_LAYOUT="$out"
  source=$(layout_field web)
  [ -n "$source" ] || die "compose 配置没有给出 api 的 /app/web 挂载源"
  case "$source" in
    /*) ;;
    *) source="$ROOT/deploy/$source" ;;
  esac
  actual=$(real_path "$source") || die "无法解析 compose 挂载目录"
  expected=$(real_path "$LIVE_DIST") || die "无法解析本次校验的前端目录"
  [ "$actual" = "$expected" ] || die \
    "compose 实际挂载到 /app/web 的目录不是本次校验产物（实际：${actual}；应为：${expected}）；拒绝启动旧前端包。请检查 deploy/.env 或环境变量 LING_WEB_DIST_HOST（挂载源以 docker compose config 的解析结果为准；deploy/.env 请使用 KEY=VALUE）。若宿主传了 LING_INSTALL_COMPOSE_OVERRIDE，它不能把 /app/web 指到本次发布之外：固定 bind 来源只应针对 /app/web 以外的目标（edge 配置/证书、更新通道目录等），前端产物永远由本次 release 自己提供。"
  ok "compose 实际挂载的 /app/web 与本次校验产物一致"
  adopt_compose_edge_port
}

build_web() {
  command -v pnpm >/dev/null || die "缺 pnpm；若目标机离线，请先在构建机运行 --build-web-only，再传递同一源码与 dist 并使用 --prebuilt-web"
  need_tmp
  snapshot_live_dist
  say "用 pnpm 构建前端到暂存目录（pnpm $(pnpm --version)；锁文件固定安装）…"
  local -a install_args=(install --frozen-lockfile)
  if [ "$WEB_OFFLINE" = "1" ]; then
    install_args+=(--offline)
    warn "显式离线模式：仅适用于已缓存全部锁文件依赖的构建环境"
  fi
  if ! ( cd "$WEB_DIR" && pnpm "${install_args[@]}" ); then
    if live_unchanged; then
      if [ "$WEB_OFFLINE" = "0" ]; then
        die "前端依赖安装/构建失败；网络不可用时请使用匹配发布源码和 dist 的 --build-web-only / --prebuilt-web 流程"
      fi
      die "离线前端安装/构建失败；请在依赖缓存完整的环境构建，或用 --prebuilt-web 提供校验通过的产物"
    fi
    warn "依赖安装失败且在线 dist 被改动，正在还原"
    if ! restore_live_dist; then
      warn "自动还原失败；发布前备份保留在 $PREV_DIST"
      KEEP_TMP=1
    fi
    die "前端依赖安装/构建失败，且在线 dist 被改动；已拒绝继续（请检查 pnpm 输出）"
  fi

  rm -rf "$WEB_STAGING" || die "无法清理暂存目录"
  mkdir -p "$WEB_STAGING" || die "无法创建暂存目录"
  # 关键：把 Vite 的 outDir 指到仓库外的暂存目录。compose 把在线 dist
  # 以 bind mount 挂进 api，直接 `pnpm build` 会因 emptyOutDir 先删光
  # 在线 dist——旧容器在构建失败时会立刻失去前端。
  if ! ( cd "$WEB_DIR" && pnpm build --outDir "$WEB_STAGING" --emptyOutDir ); then
    if live_unchanged; then
      if [ "$WEB_OFFLINE" = "0" ]; then
        die "前端依赖安装/构建失败；网络不可用时请使用匹配发布源码和 dist 的 --build-web-only / --prebuilt-web 流程"
      fi
      die "离线前端安装/构建失败；请在依赖缓存完整的环境构建，或用 --prebuilt-web 提供校验通过的产物"
    fi
    warn "构建失败且在线 dist 被改动，正在还原"
    if ! restore_live_dist; then
      warn "自动还原失败；发布前备份保留在 $PREV_DIST"
      KEEP_TMP=1
    fi
    die "前端构建失败，且在线 dist 被改动；已还原并拒绝继续"
  fi

  if ! live_unchanged; then
    warn "构建写到了在线 dist（--outDir 覆盖没有生效），正在还原"
    if ! restore_live_dist; then
      warn "自动还原失败；发布前备份保留在 $PREV_DIST"
      KEEP_TMP=1
    fi
    die "构建改动了在线 dist；已还原并拒绝继续（构建脚本可能不接受 --outDir）"
  fi

  web_manifest create "$WEB_STAGING" || die "无法生成前端校验清单"
  web_manifest verify "$WEB_STAGING" || die "构建产物自检失败"
  ok "前端已构建到暂存目录并通过校验（在线 dist 未改动）"
}

ensure_web() {
  if [ "$WEB_MODE" = "prebuilt" ]; then
    web_manifest verify "$LIVE_DIST" || die "预构建前端校验失败；拒绝启动/升级"
    ok "预构建前端与源码（含后端发布输入）及清单匹配"
  else
    build_web
  fi
}

# 首次安装时必须在 `docker compose up` 之前创建 bind mount 的宿主目录：
# 否则 Docker 可能以 root 身份创建它，安装用户随后无法写入。目录已存在时
# 只确认它是普通目录（默认/构建模式还要求可写），不换 inode。
ensure_live_dist_dir() {
  if [ -L "$LIVE_DIST" ]; then
    die "在线前端目录是符号链接：${LIVE_DIST}；拒绝自动替换（请先人工确认真实目录）"
  fi
  if [ -e "$LIVE_DIST" ] && [ ! -d "$LIVE_DIST" ]; then
    die "在线前端路径不是目录：$LIVE_DIST"
  fi
  mkdir -p "$LIVE_DIST" || die "无法创建在线前端目录：$LIVE_DIST"
  # --prebuilt-web 只读现有 dist，不要求安装用户可写；默认/构建模式发布时要写。
  if [ "$WEB_MODE" != "prebuilt" ] && [ ! -w "$LIVE_DIST" ]; then
    die "在线前端目录不可写：${LIVE_DIST}（请检查所有者/权限）"
  fi
}

# 把暂存产物原地同步到在线 dist。目录本身不换名、不删除、不重建，
# 因此容器里已经 bind mount 的目录 inode 仍然指向同一份内容。
# 注意：install.sh 之间由 .ling-install.lock 互斥，但绕过脚本的外部构建/发布
# 不受这把锁约束；live_unchanged 只能抓“构建期间在线 dist 被改动”。
publish_web() {
  [ -d "$WEB_STAGING" ] || die "没有可发布的暂存前端产物"
  web_manifest verify "$WEB_STAGING" || die "暂存前端校验失败；拒绝发布"
  if ! live_unchanged; then
    die "在线 dist 在构建期间被其他进程改动；拒绝发布（请先确认没有并行构建/部署）"
  fi
  local before after
  before=$(path_id "$LIVE_DIST")
  say "发布前端到 ${LIVE_DIST}（原地替换内容，保留目录 inode）…"
  if ! tree_replace "$WEB_STAGING" "$LIVE_DIST"; then
    warn "发布失败，尝试恢复到发布前版本"
    if ! restore_live_dist; then
      warn "恢复失败；发布前备份保留在 $PREV_DIST"
      KEEP_TMP=1
    fi
    die "发布前端失败；服务已在新后端上运行，前端已尝试恢复发布前版本，请人工确认"
  fi
  PUBLISHED=1
  after=$(path_id "$LIVE_DIST")
  if [ -n "$before" ] && [ "$before" != "$after" ]; then
    warn "发布后在线目录 inode 发生变化（旧容器仍指向旧目录）；正在回滚"
    if ! rollback_web; then
      warn "回滚失败；发布前备份保留在 $PREV_DIST"
      KEEP_TMP=1
    fi
    die "发布破坏了在线 dist 的目录 inode；服务已在新后端上运行，请人工核查旧容器挂载并重启"
  fi
  if ! web_manifest verify "$LIVE_DIST"; then
    warn "发布后校验失败，正在回滚"
    if ! rollback_web; then
      warn "回滚失败；发布前备份保留在 $PREV_DIST"
      KEEP_TMP=1
    fi
    die "发布后前端校验失败；前端已尝试回滚，服务仍在新后端上运行，请人工确认"
  fi
  ok "前端已发布并通过校验（在线目录 inode 未变）"
}

rollback_web() {
  if [ "$PUBLISHED" != "1" ]; then return 0; fi
  if [ ! -d "$PREV_DIST" ]; then return 1; fi
  if ! tree_replace "$PREV_DIST" "$LIVE_DIST"; then return 1; fi
  PUBLISHED=0
  ok "前端已回滚到发布前版本"
  return 0
}

# 发布顺序：先构建镜像 → 启动新容器 → 等 /readyz → 最后才发布前端。
# 这样新前端不会配未就绪的后端；构建/启动/readyz 失败时不会发布新前端，
# 发布失败会尝试回滚。代价是启动到就绪之间会短暂出现“新后端 + 旧前端”，
# 要求后端保持向后兼容。
build_images() {
  say "构建后端镜像（在启动容器、发布前端之前）…"
  dc build || die "镜像构建失败；前端未发布、服务未启动"
  ok "镜像构建完成"
}

start_services() {
  local rc=0
  dc up -d --no-build || rc=$?
  if [ "$rc" -eq 0 ]; then
    ok "容器已启动"
    return 0
  fi
  warn "docker compose up 失败（退出码 ${rc}）"
  die "启动失败（上面有日志）；前端未发布，在线 dist 未被替换（首次安装时可能为空目录）；但新容器可能已部分创建/替换，请检查容器状态"
}

# --- 前置检查 -------------------------------------------------------------
preflight() {
  command -v docker >/dev/null || die "没装 docker"
  docker info >/dev/null 2>&1 || die "docker 守护进程不可用（权限或未启动）"
  docker compose version >/dev/null 2>&1 || die "缺 docker compose v2"
  command -v curl >/dev/null || die "缺 curl"
  command -v python3 >/dev/null || die "缺 python3（用来生成密钥与解析配置）"
  ok "环境检查通过（docker $(docker version --format '{{.Server.Version}}' 2>/dev/null)）"
}

# --- .env：只在缺失时生成（已有 canonical env 只读）-----------------------
gen_env() {
  local envf="deploy/.env"
  if [ -f "$envf" ]; then
    ok "$envf 已存在，保留（不重新生成密钥，也不追加/改写任何内容）"
    return 0
  fi
  say "生成 $envf …"
  local root
  root=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))') || die "无法生成 LING_ROOT_KEY"
  [ -n "$root" ] || die "生成的 LING_ROOT_KEY 为空"
  cat > "$envf" <<EOF || die "无法写入 $envf"
# 由 deploy/install.sh 生成。**不要提交到仓库**（已在 .gitignore 里）。
# LING_ROOT_KEY 一旦改变，已加密的通道密钥就解不开了 —— 别随手重生成。
POSTGRES_PASSWORD=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
# 管理员账号。密码故意留空：首次启动时生成随机口令。
LING_BOOTSTRAP_ADMIN=${ADMIN_USER:-ling}
LING_BOOTSTRAP_ADMIN_PASSWORD=
LING_ROOT_KEY=$root
LING_TEST_MODE=0
LING_COOKIE_SECURE=0
LING_EDGE_PORT=$PORT
# 与 deploy/compose.yaml 的默认值（replicas: ${LING_API_REPLICAS:-2}）、
# env.example 和文档保持一致：一条命令装出来的就是“默认 2 副本”，
# 不要让新生成的 .env 悄悄把副本数改成 1（多副本/滚动更新语义会变）。
LING_API_REPLICAS=2
LING_WORKER_REPLICAS=1
# 前端挂载源：相对 deploy/ 的默认值。**不要写死绝对路径**——这份 .env 可能被
# 多个 release 树共享（symlink），绝对路径会指向生成时的旧树，让后续手动安装
# 挂到旧前端并在校验时 fail-closed。手动升级另一棵树时，请在那次 shell 里
# 显式 export 该树已验证的 dist（执行器本来就会传自己 release 的 dist）。
LING_WEB_DIST_HOST=../clients/web-console/dist
# 本检出对应的绝对路径，仅作人工核对/复制 export 用，**未生效、不要取消注释**：
#   LING_WEB_DIST_HOST=$ROOT/clients/web-console/dist
LING_USER_TIMEZONE=Asia/Shanghai

LING_QQBOT_ENABLED=0
LING_QQBOT_APP_ID=
LING_QQBOT_CLIENT_SECRET=
LING_QQBOT_INSTANCE_ID=qqbot-default
LING_QQBOT_SANDBOX=1
LING_QQBOT_PROACTIVE=0
LING_QQBOT_SUPPORTS_GROUP=1
LING_ACCESS_MODE=whitelist
LING_ACCESS_LIST=
EOF
  chmod 600 "$envf" || die "无法设置 $envf 权限"
  ok "已生成 deploy/.env（密钥不会显示）"
}

# --- 基础镜像：只导出到本进程环境，绝不写 deploy/.env ---------------------
# deploy/.env 是宿主持有的 canonical env（允许 symlink 跨 release 共享），安装器
# 只读它：这里的结果一律通过 export 进入本次进程环境，Compose 的优先级
# （shell > --env-file）会让 build/up 用到；不追加、不改写 .env，也不把密钥
# 复制到 release 树。
#
# 是否“显式配置”不再由脚本逐行解析 deploy/.env（引号、重复键、export 前缀都
# 会误判），而是看 Compose 自己解析出的最终值（compose_layout 的一次解析）：
#   - shell 里已非空 → 原样保留；
#   - --python-base → 本次调用最显式，只导出，不探测/替换；
#   - 解析值 ≠ 文档默认值 → 视为 deploy/.env 的显式选择，原样保留；
#   - 只有等于默认值时才尝试拉取。拉不到仅换**同一个 tag** 的镜像源，绝不跨
#     主版本（postgres:16 不会退成 18，也不会动用户自选的 17）；两处都拿不到
#     就 fail closed，让问题在构建前暴露。
image_state() {
  layout_field "images.$1"
}

ensure_image() {
  # $1=变量名 $2=layout 键 $3=默认镜像 $4=同 tag 镜像源 $5=说明
  local var="$1" key="$2" default="$3" mirror="$4" label="$5" resolved
  if [ -n "${!var:-}" ]; then
    ok "${label}（环境显式设置，保留）：${!var}"
    return 0
  fi
  resolved=$(image_state "$key")
  [ -n "$resolved" ] || resolved="$default"
  if [ "$resolved" != "$default" ]; then
    ok "${label}（deploy/.env 显式指定，保留）：$resolved"
    return 0
  fi
  if docker image inspect "$resolved" >/dev/null 2>&1 || docker pull -q "$resolved" >/dev/null 2>&1; then
    export "$var=$resolved"
    ok "${label}：${resolved}（仅本次进程环境）"
    return 0
  fi
  if docker image inspect "$mirror" >/dev/null 2>&1 || docker pull -q "$mirror" >/dev/null 2>&1; then
    export "$var=$mirror"
    warn "拉不动 ${resolved}，改用同一 tag 的镜像源 ${mirror}（仅本次进程环境，不写 deploy/.env）"
    return 0
  fi
  die "${label} 拿不到：${resolved} 与 ${mirror} 都不可用；请手动 docker pull 后显式设置 ${var}（安装器不会退到其它版本）"
}

resolve_images() {
  say "准备基础镜像（官方名不通时只换同一 tag 的镜像源，不换版本）…"
  if [ -n "$PY_BASE" ]; then
    export LING_PYTHON_BASE="$PY_BASE"
    ok "python 基础镜像（--python-base 指定）：$PY_BASE"
  elif [ -n "${LING_PYTHON_BASE:-}" ]; then
    ok "python 基础镜像（环境显式设置，保留）：${LING_PYTHON_BASE}"
  else
    local resolved worker
    resolved=$(image_state python)
    worker=$(image_state worker_python)
    [ -n "$resolved" ] || resolved="python:3.13-slim"
    if [ -n "$worker" ] && [ "$worker" != "$resolved" ]; then
      warn "api 与 worker 解析出的 LING_PYTHON_BASE 不一致；不自动替换（按各自 compose 配置构建）"
    elif [ "$resolved" != "python:3.13-slim" ]; then
      ok "python 基础镜像（deploy/.env 显式指定，保留）：$resolved"
    else
      ensure_image LING_PYTHON_BASE python "python:3.13-slim" \
        "docker.1panel.live/library/python:3.13-slim" "python 基础镜像"
    fi
  fi
  # Postgres / nginx：显式选择永远不覆盖；默认值失败也只在同一 tag 上换镜像源。
  ensure_image LING_POSTGRES_IMAGE postgres "postgres:16-alpine" \
    "docker.1panel.live/library/postgres:16-alpine" "Postgres 基础镜像"
  ensure_image LING_NGINX_IMAGE nginx "nginx:alpine" \
    "docker.1panel.live/library/nginx:alpine" "nginx 基础镜像"
}

# --- PyPI 源探测（必须在 compose build 之前）------------------------------
# NAS/慢网络上 pypi.org 会超时，pip 只报 “No matching distribution found”，
# 看起来像缺包其实是网络。构建前探一次，探不通就回退国内镜像源。
#
# 是否“显式配置”不再由脚本逐行解析 deploy/.env 判断：重复键、引号、export、
# 变量插值和 shell 优先级都以 Compose 自己的解析为准。这里只读
# `docker compose config --format json` 里 api/worker 的 build args 最终值：
#   - api 与 worker 必须一致（漏一个，那个镜像仍会从官方源超时失败）；
#   - 非空 → 用户显式配置，探测跳过、绝不覆盖；
#   - 空   → 没有显式配置，探测 pypi.org，失败才导出清华镜像。
# 敏感索引值不进入 shell 变量、不打印、不写日志；compose config 的 stderr
# 整段丢弃，失败只报退出码和通用原因，避免把 .env 里的口令带出来。
compose_pip_index_state() {
  need_tmp
  local py out="$TMP_BASE/pip-index.state"
  COMPOSE_PIP_STATE=""
  # 同 compose_layout：heredoc 会占 stdin，所以程序放变量、再用 -c 读 JSON。
  py=$(cat <<'PY'
import json
import sys


def fail(message, code=4):
    print(message, file=sys.stderr)
    raise SystemExit(code)


def build_arg(services, service_name):
    service = services.get(service_name)
    if not isinstance(service, dict):
        fail(f"compose 配置里没有 {service_name} 服务", 4)
    build = service.get("build")
    if not isinstance(build, dict):
        fail(f"{service_name} 没有 build 配置", 4)
    args = build.get("args")
    if isinstance(args, dict):
        if "PIP_INDEX_URL" not in args:
            fail(f"{service_name} 的 build.args 缺少 PIP_INDEX_URL", 4)
        value = args.get("PIP_INDEX_URL")
    elif isinstance(args, list):
        # 兼容非标准/旧版输出。只按第一个 '=' 切分，值里再出现 '=' 不受影响。
        value = None
        found = False
        for item in args:
            if isinstance(item, str):
                if item == "PIP_INDEX_URL":
                    value = ""
                    found = True
                elif item.startswith("PIP_INDEX_URL="):
                    value = item.split("=", 1)[1]
                    found = True
            elif isinstance(item, dict) and "PIP_INDEX_URL" in item:
                value = item["PIP_INDEX_URL"]
                found = True
        if not found:
            fail(f"{service_name} 的 build.args 缺少 PIP_INDEX_URL", 4)
    else:
        fail(f"{service_name} 的 build.args 不是映射或列表", 4)
    if value is None:
        return ""
    if not isinstance(value, str):
        fail(f"{service_name} 的 PIP_INDEX_URL 构建参数不是字符串", 4)
    return value


try:
    config = json.load(sys.stdin)
except Exception:
    fail("docker compose config 的 JSON 输出无法解析", 3)
if not isinstance(config, dict):
    fail("docker compose config 输出不是对象", 3)
services = config.get("services")
if not isinstance(services, dict):
    fail("compose 配置里没有 services", 4)

api_value = build_arg(services, "api")
worker_value = build_arg(services, "worker")
if api_value != worker_value:
    # 只报服务名，不打印两边的值（可能是带 token 的私有源）。
    fail("api 与 worker 的 PIP_INDEX_URL 构建参数不一致", 4)
print("explicit" if api_value else "empty")
PY
)
  rm -f "$out"
  dc config --format json 2>/dev/null | python3 -c "$py" >"$out"
  local -a pipe_rc=("${PIPESTATUS[@]}")
  if [ "${pipe_rc[0]}" -ne 0 ]; then
    rm -f "$out"
    printf 'docker compose config 执行失败（退出码 %s；未打印其输出，避免泄露 .env）\n' "${pipe_rc[0]}" >&2
    return 6
  fi
  if [ "${pipe_rc[1]}" -ne 0 ]; then
    rm -f "$out"
    return "${pipe_rc[1]}"
  fi
  COMPOSE_PIP_STATE=$(cat "$out")
  rm -f "$out"
  case "$COMPOSE_PIP_STATE" in
    explicit|empty) return 0 ;;
    *) printf 'docker compose config 的 PIP_INDEX_URL 判定结果无法识别\n' >&2; return 4 ;;
  esac
}

resolve_pip_index() {
  if ! compose_pip_index_state; then
    die "无法从 compose 配置确认 PIP_INDEX_URL 构建参数（未打印配置内容，避免泄露 .env）"
  fi
  case "$COMPOSE_PIP_STATE" in
    explicit)
      if [ -n "${PIP_INDEX_URL:-}" ]; then
        ok "PIP_INDEX_URL 已由环境显式设置，构建沿用（不回显）"
      else
        ok "deploy/.env 已显式设置 PIP_INDEX_URL，构建沿用（不回显）"
      fi
      return 0
      ;;
    empty) ;;
    *) die "compose 配置的 PIP_INDEX_URL 判定结果无法识别" ;;
  esac
  if ! curl -sf -m 8 -o /dev/null https://pypi.org/simple/ 2>/dev/null; then
    export PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"
    warn "pypi.org 探测失败，构建改用镜像源：$PIP_INDEX_URL"
    warn "如需其它源，请在 shell 或 deploy/.env 显式设置 PIP_INDEX_URL（脚本不会覆盖显式配置）"
  fi
}

wait_ready() {
  # 端口以 Compose 解析结果为准（install 已在 verify_compose_layout 得到，
  # restart 在这里现解析）。解析不出来就中止：绝不对着猜的端口等 120 秒。
  if ! ensure_use_port; then
    die "无法从 compose 配置确定对外端口（未打印配置内容，避免泄露 .env）；安装器不按猜测的端口判断就绪。请检查 deploy/.env 的 LING_EDGE_PORT 与 deploy/compose.yaml 的 edge ports"
  fi
  say "等待 API 就绪（http://127.0.0.1:${USE_PORT}/readyz）…"
  local i
  for i in $(seq 1 60); do
    if curl -sf -m 3 "http://127.0.0.1:${USE_PORT}/readyz" >/dev/null 2>&1; then
      ok "API 就绪"
      return 0
    fi
    sleep 2
  done
  dc ps || true
  dc logs --tail 30 api || true
  warn "服务未就绪；前端尚未发布，在线 dist 未被替换（首次安装时可能是空目录）"
  warn "新的后端容器可能已经替换旧容器：请按容器日志/数据库迁移结果人工决定回滚镜像，不能只回滚前端"
  die "等了 120 秒还没就绪（检查端口 ${USE_PORT}，来自 compose 解析）"
}

install_summary() {
  local admin="" port
  if [ -f deploy/.env ]; then
    admin=$(grep '^LING_BOOTSTRAP_ADMIN=' deploy/.env | tail -n 1 | cut -d= -f2- || true)
  fi
  admin=${admin:-$ADMIN_USER}
  port="${USE_PORT:-$PORT}"
  say ""
  ok "安装完成"
  say "  控制台      http://127.0.0.1:${port}"
  say "  用户名      $admin"
  say "  密码        不在此显示；若留空，首次启动会生成随机口令并打印在 API 日志"
  say "              查看：bash deploy/install.sh --logs api | grep -A4 '首次安装'"
  say ""
  say "  首次登录会强制要求改密码；已有数据库不会覆盖。"
  say "  常用："
  say "    bash deploy/install.sh --status      # 状态"
  say "    bash deploy/install.sh --logs api    # 日志"
  say "    bash deploy/install.sh               # 升级（重新构建并滚动重启）"
  say "    bash deploy/install.sh --down        # 停掉（数据保留）"
}

do_install() {
  # 阶段语义：从这里开始算“构建阶段”；失败意味着还没有碰容器/数据库。
  # 标记先写，连提交号格式错误这类“构建前失败”也能被正确归类。
  phase_write build
  # 再确定构建提交：格式错误在构建前中止，而不是部署后才发现。
  resolve_commit
  # 先完成前端暂存构建/校验，再碰 .env、镜像和容器；构建失败绝不启动。
  # 构建若误改在线 dist，会从发布前备份还原；成功时在线 dist 保持旧版本。
  ensure_web
  preflight
  gen_env
  # 先用 compose 自己的解析结果确认 api 实际挂载的就是本次产物，并在有宿主
  # override 时核对持久数据卷没有被搬走；解析结果同时给 resolve_images 判断
  # “哪些基础镜像是显式配置的”（只读操作，仍在构建/变更之前）。
  verify_compose_layout
  # 基础镜像只导出到本进程环境：canonical deploy/.env 只读，不追加任何内容。
  resolve_images
  # 构建前决定 PyPI 源：显式配置（由 compose config 解析出的 build args）
  # 优先，官方源不通才回退镜像源。探测紧挨着 build，避免凭据/网络状态过期。
  resolve_pip_index
  # 先构建镜像、启动并确认就绪，最后才发布前端：避免新前端配旧/未就绪后端。
  build_images
  # 必须在 up 之前让 bind mount 源目录存在；默认模式还要求可写。
  ensure_live_dist_dir
  # 从这里开始会改运行状态（起/替容器）；此后的失败都可能是“已迁移/已重启”，
  # 执行器看到 deploy/verify 就应该走人工确认，而不是盲目回滚。
  phase_write deploy
  start_services
  phase_write verify
  wait_ready
  if [ "$WEB_MODE" = "prebuilt" ]; then
    ok "预构建前端保持不变（未替换在线 dist）"
  else
    publish_web
  fi
  # 只有前端发布（或预构建确认）也完成才算整条部署成功。
  phase_write complete
  install_summary
}

# Docker 相关动作的宿主环境准备。纯本地检查，不改运行状态、不碰容器，
# 所以放在锁之前：路径写错这类问题应该在动锁/构建之前就失败。
case "$ACTION" in
  build-web-only)
    if [ -n "$COMPOSE_OVERRIDE" ]; then
      warn "--build-web-only 不使用 Docker，忽略 LING_INSTALL_COMPOSE_OVERRIDE"
    fi
    ;;
  *)
    if ! command -v python3 >/dev/null; then
      echo "缺 python3（安装锁、配置校验与密钥生成都要用）" >&2
      exit 1
    fi
    if [ -n "$COMPOSE_OVERRIDE" ]; then
      validate_compose_override
      ok "使用宿主 Compose override：${COMPOSE_OVERRIDE}（本脚本不读取/不改写其内容）"
    fi
    ensure_docker_home
    ;;
esac

# 所有会改变运行状态的动作共用同一把跨 release 锁；只读动作（status/logs）
# 和帮助不走锁。拿不到锁直接失败，不排队、不跳过。
case "$ACTION" in
  install|restart|down|build-web-only)
    # 第一次进入：Python 抢到锁后 exec 出一个新的 bash 子进程（继承锁 FD）。
    # bash 会等待这个子进程；子进程结束后父进程必须立刻退出，否则同一份
    # 脚本会被执行第二遍（包括 down/restart/install 本身）。
    if [ -z "${LING_INSTALL_LOCK_FD:-}" ]; then
      if [ "$ORIG_COUNT" -gt 0 ]; then
        acquire_install_lock "${ORIG_ARGS[@]}"
      else
        acquire_install_lock
      fi
      exit $?
    fi
    # 继承锁 FD 的第二次进入：只验证 FD 真的是这把锁，不再抢锁。
    if [ "$ORIG_COUNT" -gt 0 ]; then
      acquire_install_lock "${ORIG_ARGS[@]}" || exit 1
    else
      acquire_install_lock || exit 1
    fi
    ;;
esac

case "$ACTION" in
  build-web-only)
    build_web
    warn "--build-web-only 会更新 ${LIVE_DIST}（保留目录 inode），但不会构建镜像或重启容器；不要在正在服务的机器上把它当成无影响操作"
    publish_web
    say "仅构建前端完成：将本次匹配源码（含后端发布输入）与 clients/web-console/dist 一并交付；未运行 Docker。"
    ;;
  install) do_install ;;
  status)
    # 只读诊断：端口同样取 Compose 解析结果（已有 .env 的 LING_EDGE_PORT 才
    # 是真实发布的端口）。拿不到端口就明确说明“没有检查 /readyz”，
    # 绝不拿脚本默认端口冒充结论。
    dc ps || warn "docker compose ps 失败（容器列表不可用）"
    say ""
    if ensure_use_port; then
      curl -s -m 5 "http://127.0.0.1:${USE_PORT}/readyz" || warn "readyz 不通（端口 ${USE_PORT}，来自 compose 解析）"
    else
      warn "无法从 compose 配置确定对外端口；本次没有检查 /readyz（可先确认 deploy/.env 的 LING_EDGE_PORT）"
    fi
    ;;
  logs)    dc logs --tail 80 -f "$LOG_SVC" ;;
  down)    dc down; ok "已停止（数据卷保留）" ;;
  restart) preflight; dc restart; wait_ready ;;
esac
