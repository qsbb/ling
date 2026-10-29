#!/usr/bin/env bash
# ling 一键安装 / 升级脚本。
#
#   一条命令装好：数据库 + API + worker + 反代 + 前端。
#
#   bash deploy/install.sh                 # 装到当前仓库所在机器
#   bash deploy/install.sh --port 18080    # 指定对外端口
#   bash deploy/install.sh --admin-user admin   # 指定管理员用户名
#   bash deploy/install.sh --status        # 看状态
#   bash deploy/install.sh --logs api      # 看日志
#   bash deploy/install.sh --down           # 停掉（保留数据）
#
# 设计上要解决的几个真实问题（都是踩过的）：
#
# 1. **没有 home 目录的用户**：某些 NAS 账号 `~` 不存在，docker build 需要写
#    `~/.docker`，会报 `mkdir /home/xxx: permission denied`。这里强制把
#    HOME/DOCKER_CONFIG 指到项目下的可写目录。
# 2. **Docker Hub 拉不动**：先用官方镜像名，失败则回退到 1panel 镜像源。
# 3. **幂等**：已有 .env 就不覆盖（不重新生成密钥，否则重启后 root key 变了，
#    已加密的密钥全部解不开）。升级只重建镜像和容器。
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
ROOT=$(pwd)

PROJECT="ling"
PORT="18080"
ACTION="install"
LOG_SVC="api"
PY_BASE=""
ADMIN_USER="${ADMIN_USER:-ling}"
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --project) PROJECT="$2"; shift 2 ;;
    --admin-user) ADMIN_USER="$2"; export ADMIN_USER; shift 2 ;;
    --python-base) PY_BASE="$2"; shift 2 ;;
    --status) ACTION="status"; shift ;;
    --logs) ACTION="logs"; LOG_SVC="${2:-api}"; shift 2 ;;
    --down) ACTION="down"; shift ;;
    --restart) ACTION="restart"; shift ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

# --- 可写目录兜底（没有 home 的用户）--------------------------------------
export HOME="$ROOT/.home"
export DOCKER_CONFIG="$ROOT/.docker"
mkdir -p "$HOME" "$DOCKER_CONFIG"

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; DIM=$'\033[2m'; RST=$'\033[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s!%s %s\n' "$YEL" "$RST" "$*"; }
die()  { printf '%s✗%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }

COMPOSE=(docker compose -f deploy/compose.yaml --project-name "$PROJECT")
if [ -f deploy/.env ]; then COMPOSE+=(--env-file deploy/.env); fi
dc() { "${COMPOSE[@]}" "$@"; }

# --- 前置检查 -------------------------------------------------------------
preflight() {
  command -v docker >/dev/null || die "没装 docker"
  docker info >/dev/null 2>&1 || die "docker 守护进程不可用（权限或未启动）"
  docker compose version >/dev/null 2>&1 || die "缺 docker compose v2"
  command -v curl >/dev/null || die "缺 curl"
  command -v python3 >/dev/null || die "缺 python3（用来生成密钥与解析配置）"
  ok "环境检查通过（docker $(docker version --format '{{.Server.Version}}' 2>/dev/null)）"
}

# --- .env：只在缺失时生成 ------------------------------------------------
gen_env() {
  local envf="deploy/.env"
  if [ -f "$envf" ]; then
    ok "$envf 已存在，保留（不重新生成密钥）"
    return 0
  fi
  say "生成 $envf …"
  local root
  root=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
  cat > "$envf" <<EOF
# 由 deploy/install.sh 生成。**不要提交到仓库**（已在 .gitignore 里）。
# LING_ROOT_KEY 一旦改变，已加密的通道密钥就解不开了 —— 别随手重生成。
POSTGRES_PASSWORD=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
# 管理员账号。密码**故意留空**：留空时应用会在首次启动时生成随机口令
# 并打印到日志，同时要求登录后立刻改密。这样随机口令只作为一次性登船票，
# 不会被永久写进 .env。
LING_BOOTSTRAP_ADMIN=${ADMIN_USER:-ling}
LING_BOOTSTRAP_ADMIN_PASSWORD=
LING_ROOT_KEY=$root
LING_TEST_MODE=0
LING_COOKIE_SECURE=0
LING_EDGE_PORT=$PORT
# 单副本：QQ 官方机器人的网关要保持一条长连接；被动回复窗口缓存在进程内存里
LING_API_REPLICAS=1
LING_WORKER_REPLICAS=1
LING_WEB_DIST_HOST=$ROOT/clients/web-console/dist
LING_USER_TIMEZONE=Asia/Shanghai

# QQ 官方机器人（WebSocket 网关，不需要公网地址、不需要扫码）
LING_QQBOT_ENABLED=0
LING_QQBOT_APP_ID=
LING_QQBOT_CLIENT_SECRET=
LING_QQBOT_INSTANCE_ID=qqbot-default
LING_QQBOT_SANDBOX=1
LING_QQBOT_PROACTIVE=0
LING_QQBOT_SUPPORTS_GROUP=1

# 出入许可。注意：qqbot 的发送者是 **openid**，NapCat 的才是 QQ 号
LING_ACCESS_MODE=whitelist
LING_ACCESS_LIST=
EOF
  chmod 600 "$envf"
  ok "已生成（面板密码在 $envf 的 LING_BOOTSTRAP_ADMIN_PASSWORD）"
}

# --- 基础镜像：官方名拉不到就换镜像源 ------------------------------------
resolve_images() {
  [ -f deploy/.env ] || return 0
  grep -q '^LING_PYTHON_BASE=' deploy/.env && return 0

  local py_local py_mirror
  py_local="python:3.13-slim"
  py_mirror="docker.1panel.live/library/python:3.13-slim"
  [ -n "$PY_BASE" ] && py_local="$PY_BASE" && py_mirror="$PY_BASE"

  say "准备基础镜像（Docker Hub 不通时会自动换镜像源）…"
  local chosen=""
  if docker image inspect "$py_local" >/dev/null 2>&1; then
    chosen="$py_local"
  elif docker pull -q "$py_local" >/dev/null 2>&1; then
    chosen="$py_local"
  else
    warn "拉不动 $py_local，换 $py_mirror"
    if docker image inspect "$py_mirror" >/dev/null 2>&1 || docker pull -q "$py_mirror" >/dev/null 2>&1; then
      chosen="$py_mirror"
    else
      die "基础镜像都拿不到，请手动 docker pull 后加 --python-base 指定"
    fi
  fi
  ok "python 基础镜像：$chosen"

  # nginx / postgres 同理
  local ngx="nginx:alpine" pg="postgres:16-alpine"
  docker image inspect "$ngx" >/dev/null 2>&1 || docker pull -q "$ngx" >/dev/null 2>&1 \
    || ngx="docker.1panel.live/library/nginx:alpine"
  docker image inspect "$pg" >/dev/null 2>&1 || docker pull -q "$pg" >/dev/null 2>&1 \
    || pg="postgres:18-alpine"

  {
    echo ""
    echo "# 由 install.sh 解析出的基础镜像"
    echo "LING_PYTHON_BASE=$chosen"
    echo "LING_POSTGRES_IMAGE=$pg"
    echo "LING_NGINX_IMAGE=$ngx"
  } >> deploy/.env
  ok "镜像已记录到 deploy/.env"
}

# --- 前端构建产物 --------------------------------------------------------
ensure_web() {
  [ -f clients/web-console/dist/index.html ] && { ok "前端产物已就绪"; return 0; }
  warn "缺 clients/web-console/dist —— 控制台页面会 404"
  if command -v pnpm >/dev/null; then
    say "用 pnpm 构建前端…"
    ( cd clients/web-console && pnpm install --frozen-lockfile && pnpm build ) \
      || die "前端构建失败"
    ok "前端已构建"
  else
    warn "本机没有 pnpm：请在有 node 的机器上构建，或直接把 dist 拷过来"
  fi
}

wait_ready() {
  say "等待 API 就绪…"
  local i
  for i in $(seq 1 60); do
    if curl -sf -m 3 "http://127.0.0.1:$PORT/readyz" >/dev/null 2>&1; then
      ok "API 就绪"
      return 0
    fi
    sleep 2
  done
  dc ps || true
  dc logs --tail 30 api || true
  die "等了 120 秒还没就绪"
}

do_install() {
  preflight
  gen_env
  resolve_images
  ensure_web
  say "构建并启动…"
  dc up -d --build || die "启动失败（上面有日志）"
  wait_ready
  local admin pw
  admin=$(grep '^LING_BOOTSTRAP_ADMIN=' deploy/.env | cut -d= -f2-)
  pw=$(grep '^LING_BOOTSTRAP_ADMIN_PASSWORD=' deploy/.env | cut -d= -f2-)
  cat <<EOF

$(ok "安装完成")

  控制台      http://127.0.0.1:$PORT
  用户名      $admin
EOF
  if [ -n "$pw" ]; then
    printf '  密码        %s\n' "$pw"
  else
    # 密码是应用在首次启动时随机生成并打到日志里的
    local logline
    logline=$(dc logs api 2>/dev/null | grep -A4 '首次安装：已创建管理员账号' | grep '密码' | tail -1 || true)
    if [ -n "$logline" ]; then
      printf '  密码        %s\n' "$(printf '%s' "$logline" | sed 's/.*密码 *//')"
    else
      printf '  密码        %s\n' "（在启动日志里：bash deploy/install.sh --logs api | grep -A4 首次安装）"
    fi
  fi
  cat <<EOF

  ${DIM}首次登录会强制要求改密码——随机口令只当一次性登船票${RST}
  ${DIM}这个账号只在全新数据库上创建；已有库不会覆盖${RST}

  常用：
    bash deploy/install.sh --status      # 状态
    bash deploy/install.sh --logs api    # 日志
    bash deploy/install.sh               # 升级（重新构建并滚动重启）
    bash deploy/install.sh --down        # 停掉（数据保留）

  接 QQ 官方机器人：编辑 deploy/.env 里的 LING_QQBOT_*，然后重跑本脚本。
EOF
}

case "$ACTION" in
  install) do_install ;;
  status)  dc ps; say ""; curl -s -m 5 "http://127.0.0.1:$PORT/readyz" || warn "readyz 不通" ;;
  logs)    dc logs --tail 80 -f "$LOG_SVC" ;;
  down)    dc down; ok "已停止（数据卷保留）" ;;
  restart) dc restart; wait_ready ;;
esac
