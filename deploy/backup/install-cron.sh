#!/usr/bin/env bash
# 安装每日备份定时任务（无人值守）。
#
# 用法：deploy/backup/install-cron.sh /path/to/backup/dir [HH:MM]
#
# 为什么必须定时：个人服务器只有一份数据源。手动备份等于"想起来才备"，
# 而事故不会等你想起来。备份脚本本身已能跑，缺的只是"每天自动跑"。
#
# 为什么 crontab 行里必须显式写 LING_COMPOSE_PROJECT：
# `docker compose -f <路径>` 在没有 -p/--project-name 时按**文件所在目录名**
# 推导项目名（compose.yaml 在 deploy/ 下 → 推导成 `deploy`），而 install.sh 用
# `--project-name`（默认值见 deploy/install.sh 的 `PROJECT=` 行）。名字不一致时
# `docker compose exec db` 找不到正在运行的容器，cron 里的备份会静默一份都备
# 不出来（"已安装每日备份"≠"备出来了"）。所以这里把项目名写进 cron 行，值从
# install.sh 的默认值解析（不写死字面 ling），允许 LING_COMPOSE_PROJECT 覆盖。
set -euo pipefail

BACKUP_DIR="${1:?用法: $0 BACKUP_DIR [HH:MM]}"
RUN_AT="${2:-04:30}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_SH="$SCRIPT_DIR/backup.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
INSTALL_SH="$REPO_ROOT/deploy/install.sh"

if [[ ! -x "$BACKUP_SH" ]]; then
  echo "找不到可执行的备份脚本: $BACKUP_SH" >&2
  exit 1
fi

# 时间必须是 HH:MM：这个值会原样进 crontab，不接受任意字符串。
if [[ ! "$RUN_AT" =~ ^[0-9]{2}:[0-9]{2}$ ]]; then
  echo "时间格式必须是 HH:MM（拿到: ${RUN_AT}）" >&2
  exit 1
fi
HOUR="${RUN_AT%%:*}"
MIN="${RUN_AT##*:}"
if (( 10#$HOUR > 23 || 10#$MIN > 59 )); then
  echo "时间超出范围（拿到: ${RUN_AT}）" >&2
  exit 1
fi

# Compose 项目名：与 install.sh 同源。install.sh 里 `PROJECT="..."` 是 --project
# 的默认值；这里解析那一行，避免两处默认值漂移（也不硬编码 ling）。
install_default_project() {
  local line value
  [[ -r "$INSTALL_SH" ]] || return 1
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      PROJECT=*) ;;
      *) continue ;;
    esac
    value="${line#PROJECT=}"
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    # 只接受 Compose 项目名允许的形状，任何可疑内容都拒绝（而不是猜）。
    case "$value" in
      ''|*[!a-z0-9_-]*|[!a-z0-9]*) return 1 ;;
    esac
    printf '%s\n' "$value"
    return 0
  done <"$INSTALL_SH"
  return 1
}

PROJECT="${LING_COMPOSE_PROJECT:-}"
if [[ -z "$PROJECT" ]]; then
  if ! PROJECT="$(install_default_project)"; then
    echo "无法从 $INSTALL_SH 解析默认 Compose 项目名；" \
      "请显式设置 LING_COMPOSE_PROJECT=<install.sh 用过的 --project 值>" >&2
    exit 1
  fi
fi
case "$PROJECT" in
  ''|*[!a-z0-9_-]*|[!a-z0-9]*)
    echo "Compose 项目名不合法（拿到: ${PROJECT}）" >&2
    exit 1
    ;;
esac

mkdir -p "$BACKUP_DIR"

# 宿主机的 cron 里**没有 DSN**，数据库也只在本 compose 内网：
# 直接跑 pg_dump 必然失败（"已安装每日备份"其实一份都备不出来）。
# 因此默认走 compose 模式，通过 docker compose exec 进 db 容器导出。
# 项目名/文件/目录全部显式写进这一行：cron 环境没有 LING_* 变量可继承。
# 默认 compose 文件与 `cd` 用同一个 $PWD，避免两处推导出不同目录。
COMPOSE_FILE="${LING_COMPOSE_FILE:-$PWD/deploy/compose.yaml}"
LINE="$MIN $HOUR * * * cd '$PWD' && LING_BACKUP_VIA_COMPOSE=1 LING_COMPOSE_PROJECT='$PROJECT' LING_COMPOSE_FILE='$COMPOSE_FILE' LING_BACKUP_DIR='$BACKUP_DIR' '$BACKUP_SH' '$BACKUP_DIR' >> '$BACKUP_DIR/backup.log' 2>&1"

# 幂等：先删掉同脚本的旧条目（含旧版本没写项目名的行），再加当前这一条。
# 用纯 bash 过滤而不再调 grep：少一个"参数写错就静默留下旧行"的可能。
# crontab -l 的 exit 1 只表示"当前用户还没有 crontab"；其它失败（权限/文件损坏）
# 一律中止——否则会把"读不到"当成"空的"，用一行覆盖掉别人的全部定时任务。
if existing="$(crontab -l 2>/dev/null)"; then
  :
else
  list_status=$?
  if (( list_status != 1 )); then
    echo "[install-cron] 读取现有 crontab 失败（crontab -l exit=${list_status}），未做任何修改" >&2
    exit 1
  fi
  existing=""
fi
lines=()
while IFS= read -r old_line; do
  if [[ -z "$old_line" ]]; then
    continue
  fi
  case "$old_line" in
    *"$BACKUP_SH"*) continue ;;
  esac
  lines+=("$old_line")
done <<<"$existing"
lines+=("$LINE")
printf '%s\n' "${lines[@]}" | crontab -

if [[ -n "${LING_COMPOSE_PROJECT:-}" ]]; then
  PROJECT_SOURCE="环境变量 LING_COMPOSE_PROJECT"
else
  PROJECT_SOURCE="deploy/install.sh 的 PROJECT= 默认值"
fi

echo "[install-cron] 已安装每日备份：$RUN_AT"
echo "[install-cron] 备份目录：$BACKUP_DIR"
echo "[install-cron] Compose 项目：${PROJECT}（来源: ${PROJECT_SOURCE}）"
echo
echo "注意事项："
echo "  - 该目录必须**与数据库所在磁盘分开**（异机/网盘/外置盘），"
echo "    否则磁盘坏掉时备份与原文一起丢。"
echo "  - manifest 的 keys_root=present 表示根密钥被归档到 keys/root.key，"
echo "    它必须**单独保管**（数据库里的 provider 凭据是加密的，根密钥丢了"
echo "    就全部解不开）。keys_root_source=file 只说明'密钥字节来自文件'，"
echo "    不说明应用用哪一把：LING_ROOT_KEY 非空时应用**优先用环境变量**，"
echo "    此时 manifest 会记 keys_env_set=true——恢复时那份 deploy/.env 也"
echo "    必须一起恢复，否则只回灌 keys/root.key 可能解不开。"
echo "  - media-*.tar 是**全量**媒体副本，每天一份会线性累积；脚本不删历史"
echo "    备份，请自行估空间并轮转（见 docs/DEPLOYMENT.md §8.1）。"
echo "  - 备份失败时脚本会删掉**本次**已产出的片段（dump/墓碑/媒体/version），"
echo "    不会留下 '看起来像备份' 的残目录；成功的标志是当天有新 manifest。"
echo "    共享目录里历史备份的 dump/manifest/media 一概不动；keys/root.key 也一样"
echo "    （它没有日期戳，同时就是前一天那份备份的密钥归档）——本次覆盖过它会先"
echo "    把原字节放回，放不回去也只告警、绝不删。"
echo "  - 权限现状（脚本有意不改，免得让既有部署写不进去）：这个目录由 mkdir -p"
echo "    创建 → 0755；dump/manifest/tombstones 是 0644；只有 media-*.tar 与"
echo "    keys/root.key 是 0600。dump 里有业务数据，请把父目录自己收紧"
echo "    （chmod 700），别放在 /tmp 或共享盘的可浏览位置。"
echo "  - 装好后用 'crontab -l' 确认那一行带着 LING_COMPOSE_PROJECT=${PROJECT}，"
echo "    并在 48 小时内检查 GET /v1/health/ops 的 backup_age_hours 是否正常"
echo "    （也要 tail 一下 $BACKUP_DIR/backup.log）。"
