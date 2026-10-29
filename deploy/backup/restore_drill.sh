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
  # 墓碑一致性：必须和备份时记录**对上**。只打印数字的话，0 也是"通过"，
  # 而 0 个墓碑恰恰是最危险的信号（要么导出失败，要么恢复丢了撤回标记，
  # 已撤回的记忆会复活）。
  STAMP="$(basename "$DUMP" | sed -E 's/^ling-(.*)\.dump$/\1/')"
  DIR="$(dirname "$DUMP")"
  MANIFEST="$DIR/manifest-$STAMP.txt"
  TOMB_FILE="$DIR/tombstones-$STAMP.txt"
  if [[ ! -f "$MANIFEST" ]]; then
    echo "[drill] 失败：找不到备份清单 $MANIFEST" >&2
    exit 1
  fi
  if grep -q '^tombstones_state=FAILED' "$MANIFEST"; then
    echo "[drill] 失败：这份备份的墓碑清单导出失败，不能用于恢复" >&2
    exit 1
  fi
  if [[ ! -f "$TOMB_FILE" ]]; then
    echo "[drill] 失败：清单说墓碑已导出，但文件不存在（$TOMB_FILE）" >&2
    exit 1
  fi
  EXPECTED="$(awk -F, '{print $1}' "$TOMB_FILE" | sed '/^[[:space:]]*$/d' | sort)"
  RESTORED="$(docker compose "${COMPOSE_ARGS[@]}" exec -T db \
    psql -U "$PG_USER" -d "$DRILL_DB" -tAc \
    "SELECT id FROM memories WHERE retracted = true ORDER BY id" \
    | sed '/^[[:space:]]*$/d' | sort)"
  echo "[drill] 墓碑：备份记录 $(printf '%s' "$EXPECTED" | grep -c . || true) 条，恢复后 $(printf '%s' "$RESTORED" | grep -c . || true) 条"
  if [[ "$EXPECTED" != "$RESTORED" ]]; then
    echo "[drill] 失败：墓碑不一致——恢复后的库与备份时记录不符" >&2
    exit 1
  fi

  echo "[drill] 完成（隔离库 $DRILL_DB 未触碰生产库，墓碑一致）"
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

# **墓碑必须和备份时记录的对上**。
#
# 原来这里只打印一个数字，没有基准可比——数字是 0 也算"校验通过"，
# 而"0 个墓碑"恰恰是最危险的信号：要么备份时导出失败（那时 manifest
# 会写 tombstones_state=FAILED），要么恢复把一个本该有墓碑的库恢复成了
# 没有墓碑，已撤回的记忆就会复活。
STAMP="$(basename "$DUMP" | sed -E 's/^ling-(.*)\.dump$/\1/')"
DIR="$(dirname "$DUMP")"
MANIFEST="$DIR/manifest-$STAMP.txt"
TOMB_FILE="$DIR/tombstones-$STAMP.txt"

if [[ ! -f "$MANIFEST" ]]; then
  echo "[drill] 失败：找不到备份清单 $MANIFEST" >&2
  exit 1
fi

if grep -q '^tombstones_state=FAILED' "$MANIFEST"; then
  echo "[drill] 失败：这份备份的墓碑清单导出失败，不能用于恢复" >&2
  exit 1
fi

if [[ ! -f "$TOMB_FILE" ]]; then
  echo "[drill] 失败：清单说墓碑已导出，但文件不存在（$TOMB_FILE）" >&2
  exit 1
fi

echo "[drill] 校验墓碑仍在"
EXPECTED="$(awk -F, '{print $1}' "$TOMB_FILE" | sed '/^[[:space:]]*$/d' | sort)"
RESTORED="$(psql "$PG_URL" -tAc "SELECT id FROM memories WHERE retracted = true ORDER BY id" | sed '/^[[:space:]]*$/d' | sort)"
echo "  备份记录 $(printf '%s' "$EXPECTED" | grep -c . || true) 条，恢复后 $(printf '%s' "$RESTORED" | grep -c . || true) 条"

if [[ "$EXPECTED" != "$RESTORED" ]]; then
  echo "[drill] 失败：墓碑不一致——恢复后的库与备份时记录不符" >&2
  echo "  多出来的墓碑可能是备份后新增的撤回（正常，请核对）" >&2
  echo "  **少了的墓碑是危险的**：那些已撤回的记忆会复活" >&2
  exit 1
fi

echo "[drill] 完成：备份可恢复，墓碑一致。"
