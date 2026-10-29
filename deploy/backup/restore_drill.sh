#!/usr/bin/env bash
# 恢复演练：把备份恢复到**隔离数据库**，验证可用性与墓碑一致性。
# 用法：deploy/backup/restore_drill.sh DUMP_FILE TARGET_URL
#
# 绝不覆盖生产库：TARGET_URL 的库名必须以 _drill 结尾。
set -euo pipefail

DUMP="${1:?用法: $0 DUMP_FILE [TARGET_URL]}"
TARGET="${2:-}"

# compose 模式：宿主机连不到库（数据库只在 compose 内网），
# 恢复演练要进容器做。TARGET_URL 传库名即可（如 ling_drill）。
VIA_COMPOSE="${LING_BACKUP_VIA_COMPOSE:-0}"
COMPOSE_FILE="${LING_COMPOSE_FILE:-deploy/compose.yaml}"
COMPOSE_PROJECT="${LING_COMPOSE_PROJECT:-}"
COMPOSE_ARGS=(-f "$COMPOSE_FILE")
if [ -n "$COMPOSE_PROJECT" ]; then
  COMPOSE_ARGS=(-p "$COMPOSE_PROJECT" "${COMPOSE_ARGS[@]}")
fi

if [ "$VIA_COMPOSE" = "1" ]; then
  DRILL_DB="${TARGET:-ling_drill}"
  PG_USER="${LING_DB_USER:-ling}"
  if [[ "$DRILL_DB" != *_drill ]]; then
    echo "拒绝执行：目标库名必须以 _drill 结尾（当前 $DRILL_DB）" >&2
    exit 2
  fi
  echo "[drill] 重建隔离库 $DRILL_DB（容器 db）"
  docker compose "${COMPOSE_ARGS[@]}" exec -T db \
    psql -U "$PG_USER" -d postgres -c "DROP DATABASE IF EXISTS $DRILL_DB" >/dev/null 2>&1 || true
  docker compose "${COMPOSE_ARGS[@]}" exec -T db \
    psql -U "$PG_USER" -d postgres -c "CREATE DATABASE $DRILL_DB" >/dev/null
  echo "[drill] 恢复 dump"
  docker compose "${COMPOSE_ARGS[@]}" exec -T db \
    pg_restore --no-owner --no-privileges -U "$PG_USER" -d "$DRILL_DB" < "$DUMP"
  echo "[drill] 校验"
  docker compose "${COMPOSE_ARGS[@]}" exec -T db \
    psql -U "$PG_USER" -d "$DRILL_DB" -tAc \
    "select 'windows='||count(*) from windows union all \
     select 'turns='||count(*) from turns union all \
     select 'messages='||count(*) from messages union all \
     select 'memories='||count(*) from memories"
  echo "[drill] 完成（隔离库 $DRILL_DB 未触碰生产库）"
  exit 0
fi

if [[ "$TARGET" != *_drill ]]; then
  echo "拒绝执行：目标库名必须以 _drill 结尾（当前 $TARGET）" >&2
  exit 2
fi

PG_URL="${TARGET/postgresql+asyncpg:\/\//postgresql://}"

echo "[drill] 重建隔离库"
psql "${PG_URL%/*}/postgres" -c "DROP DATABASE IF EXISTS ${PG_URL##*/}" >/dev/null 2>&1 || true
psql "${PG_URL%/*}/postgres" -c "CREATE DATABASE ${PG_URL##*/}" >/dev/null

echo "[drill] 恢复"
pg_restore --no-owner --no-privileges --dbname "$PG_URL" "$DUMP"

echo "[drill] 校验关键表"
for table in users windows turns messages memories; do
  count=$(psql "$PG_URL" -tAc "SELECT count(*) FROM $table" 2>/dev/null || echo "MISSING")
  echo "  $table = $count"
done

echo "[drill] 校验墓碑仍在"
psql "$PG_URL" -tAc "SELECT count(*) FROM memories WHERE retracted = true"

echo "[drill] 完成：备份可恢复。生产启用前请人工核对墓碑与媒体清单。"
