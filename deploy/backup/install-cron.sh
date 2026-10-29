#!/usr/bin/env bash
# 安装每日备份定时任务（无人值守）。
#
# 用法：deploy/backup/install-cron.sh /path/to/backup/dir [HH:MM]
#
# 为什么必须定时：个人服务器只有一份数据源。手动备份等于"想起来才备"，
# 而事故不会等你想起来。备份脚本本身已能跑，缺的只是"每天自动跑"。
set -euo pipefail

BACKUP_DIR="${1:?用法: $0 BACKUP_DIR [HH:MM]}"
RUN_AT="${2:-04:30}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_SH="$SCRIPT_DIR/backup.sh"

if [[ ! -x "$BACKUP_SH" ]]; then
  echo "找不到可执行的备份脚本: $BACKUP_SH" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
HOUR="${RUN_AT%%:*}"
MIN="${RUN_AT##*:}"

# 宿主机的 cron 里**没有 DSN**，数据库也只在本 compose 内网：
# 直接跑 pg_dump 必然失败（"已安装每日备份"其实一份都备不出来）。
# 因此默认走 compose 模式，通过 docker compose exec 进 db 容器导出。
COMPOSE_FILE="${LING_COMPOSE_FILE:-$(pwd)/deploy/compose.yaml}"
LINE="$MIN $HOUR * * * cd $(pwd) && LING_BACKUP_VIA_COMPOSE=1 LING_COMPOSE_FILE='$COMPOSE_FILE' LING_BACKUP_DIR='$BACKUP_DIR' '$BACKUP_SH' '$BACKUP_DIR' >> '$BACKUP_DIR/backup.log' 2>&1"

# 幂等：先删掉同脚本的旧条目，再加
( crontab -l 2>/dev/null | grep -vF "$BACKUP_SH" || true; echo "$LINE" ) | crontab -

echo "[install-cron] 已安装每日备份：$RUN_AT"
echo "[install-cron] 备份目录：$BACKUP_DIR"
echo
echo "注意事项："
echo "  - 该目录必须**与数据库所在磁盘分开**（异机/网盘/外置盘），"
echo "    否则磁盘坏掉时备份与原文一起丢。"
echo "  - manifest 里的 keys/root.key 也要单独保管：数据库里的 provider"
echo "    凭据是加密的，根密钥丢了就全部解不开。"
echo "  - 装好后用 'crontab -l' 确认，并在 48 小时内检查"
echo "    GET /v1/health/ops 的 backup_age_hours 是否正常。"
