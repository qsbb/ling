#!/usr/bin/env bash
# 恢复演练：把备份恢复到**隔离数据库**，验证可用性、墓碑一致性与媒体覆盖。
# 用法：deploy/backup/restore_drill.sh DUMP_FILE TARGET_URL
#
# 绝不覆盖生产库：TARGET_URL 的库名必须以 _drill 结尾。
#
# 本脚本只读备份目录，不碰生产库、不做真实恢复上线；它回答三个问题：
#   1) 这份 dump 能不能恢复（关键表数据在不在）；
#   2) 墓碑与备份时记录**是否一致**（少了墓碑 = 已撤回记忆会复活）；
#   3) `media_evidence.media_ref` 指向的实体文件**是否都在媒体归档里**
#      （不备媒体字节的备份，恢复后引用全部悬空——库看起来完好，图全没了）。
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

STAMP="$(basename "$DUMP" | sed -E 's/^ling-(.*)\.dump$/\1/')"
DIR="$(dirname "$DUMP")"
MANIFEST="$DIR/manifest-$STAMP.txt"
TOMB_FILE="$DIR/tombstones-$STAMP.txt"

# ---- 小工具 -------------------------------------------------------------
# 校验产物哈希需要 sha256 工具；一个都没有时**跳过哈希校验并提示**，
# 不引入新依赖也不因此判定备份无效（哈希只是加分项）。
SHA256_TOOL=""
if command -v sha256sum >/dev/null 2>&1; then
  SHA256_TOOL=sha256sum
elif command -v shasum >/dev/null 2>&1; then
  SHA256_TOOL=shasum
elif command -v openssl >/dev/null 2>&1; then
  SHA256_TOOL=openssl
fi

sha256_of() {
  case "$SHA256_TOOL" in
    sha256sum) sha256sum "$1" | awk '{print $1}' ;;
    shasum) shasum -a 256 "$1" | awk '{print $1}' ;;
    *) openssl dgst -sha256 "$1" | awk '{print $NF}' ;;
  esac
}

# 从 manifest 里取 k=v 的值；键不存在时打印空串（旧版 manifest 没有新字段）。
manifest_value() {
  local file line key
  file="$1"
  key="$2"
  [[ -f "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      "$key="*) printf '%s\n' "${line#*=}"; return 0 ;;
    esac
  done <"$file"
  return 0
}

# ---- 重建隔离库并恢复 ---------------------------------------------------
if [ "$VIA_COMPOSE" = "1" ]; then
  DRILL_DB="${TARGET:-ling_drill}"
  PG_USER="${LING_DB_USER:-ling}"
  if [[ "$DRILL_DB" != *_drill ]]; then
    echo "拒绝执行：目标库名必须以 _drill 结尾（当前 ${DRILL_DB}）" >&2
    exit 2
  fi
  echo "[drill] 重建隔离库 ${DRILL_DB}（容器 db）"
  docker compose "${COMPOSE_ARGS[@]}" exec -T db \
    psql -U "$PG_USER" -d postgres -c "DROP DATABASE IF EXISTS $DRILL_DB" >/dev/null 2>&1 || true
  docker compose "${COMPOSE_ARGS[@]}" exec -T db \
    psql -U "$PG_USER" -d postgres -c "CREATE DATABASE $DRILL_DB" >/dev/null
  echo "[drill] 恢复 dump"
  docker compose "${COMPOSE_ARGS[@]}" exec -T db \
    pg_restore --no-owner --no-privileges -U "$PG_USER" -d "$DRILL_DB" < "$DUMP"
else
  if [[ "$TARGET" != *_drill ]]; then
    echo "拒绝执行：目标库名必须以 _drill 结尾（当前 ${TARGET}）" >&2
    exit 2
  fi
  PG_URL="${TARGET/postgresql+asyncpg:\/\//postgresql://}"
  echo "[drill] 重建隔离库"
  psql "${PG_URL%/*}/postgres" -c "DROP DATABASE IF EXISTS ${PG_URL##*/}" >/dev/null 2>&1 || true
  psql "${PG_URL%/*}/postgres" -c "CREATE DATABASE ${PG_URL##*/}" >/dev/null
  echo "[drill] 恢复"
  pg_restore --no-owner --no-privileges --dbname "$PG_URL" "$DUMP"
fi

# 隔离库查询入口：两种模式共用后面的校验逻辑，只有“怎么连库”不同。
drill_sql() {
  if [ "$VIA_COMPOSE" = "1" ]; then
    docker compose "${COMPOSE_ARGS[@]}" exec -T db \
      psql -U "$PG_USER" -d "$DRILL_DB" -tAc "$1"
  else
    psql "$PG_URL" -tAc "$1"
  fi
}

echo "[drill] 校验关键表"
for table in users windows turns messages memories; do
  count=$(drill_sql "SELECT count(*) FROM $table" 2>/dev/null || echo "MISSING")
  echo "  $table = $count"
done

# **墓碑必须和备份时记录的对上**。
#
# 原来这里只打印一个数字，没有基准可比——数字是 0 也算"校验通过"，
# 而"0 个墓碑"恰恰是最危险的信号：要么备份时导出失败（那时 manifest
# 会写 tombstones_state=FAILED），要么恢复把一个本该有墓碑的库恢复成了
# 没有墓碑，已撤回的记忆就会复活。
if [[ ! -f "$MANIFEST" ]]; then
  echo "[drill] 失败：找不到备份清单 $MANIFEST" >&2
  exit 1
fi

if grep -q '^tombstones_state=FAILED' "$MANIFEST"; then
  echo "[drill] 失败：这份备份的墓碑清单导出失败，不能用于恢复" >&2
  exit 1
fi

if [[ ! -f "$TOMB_FILE" ]]; then
  echo "[drill] 失败：清单说墓碑已导出，但文件不存在（${TOMB_FILE}）" >&2
  exit 1
fi

echo "[drill] 校验墓碑仍在"
EXPECTED="$(awk -F, '{print $1}' "$TOMB_FILE" | sed '/^[[:space:]]*$/d' | sort)"
RESTORED="$(drill_sql "SELECT id FROM memories WHERE retracted = true ORDER BY id" | sed '/^[[:space:]]*$/d' | sort)"
echo "  备份记录 $(printf '%s' "$EXPECTED" | grep -c . || true) 条，恢复后 $(printf '%s' "$RESTORED" | grep -c . || true) 条"

if [[ "$EXPECTED" != "$RESTORED" ]]; then
  echo "[drill] 失败：墓碑不一致——恢复后的库与备份时记录不符" >&2
  echo "  多出来的墓碑可能是备份后新增的撤回（正常，请核对）" >&2
  echo "  **少了的墓碑是危险的**：那些已撤回的记忆会复活" >&2
  exit 1
fi

# ---- 产物哈希（manifest 里有才校验；旧版备份没有这些键，跳过并提示）----
verify_hash() {
  local path key expected actual
  path="$1"
  key="$2"
  if [[ -z "$SHA256_TOOL" ]]; then
    echo "  [drill] 提示：本机没有 sha256sum/shasum/openssl，跳过 $key 校验" >&2
    return 0
  fi
  expected="$(manifest_value "$MANIFEST" "$key")"
  if [[ -z "$expected" ]]; then
    echo "  [drill] 提示：manifest 没有 ${key}（旧版备份），跳过：$(basename "$path")"
    return 0
  fi
  if [[ "$expected" == "-" ]]; then
    return 0
  fi
  if [[ ! -f "$path" ]]; then
    echo "[drill] 失败：manifest 记了 ${key}，但文件不存在（${path}）" >&2
    return 1
  fi
  actual="$(sha256_of "$path")"
  if [[ "$actual" != "$expected" ]]; then
    echo "[drill] 失败：$key 与文件内容不符（清单 ${expected}，实际 ${actual}）：${path}" >&2
    return 1
  fi
  echo "  [drill] $key 一致（$(basename "$path")）"
  return 0
}

echo "[drill] 校验产物哈希"
verify_hash "$DUMP" dump_sha256 || exit 1
verify_hash "$TOMB_FILE" tombstones_sha256 || exit 1

# ---- 媒体覆盖：media_ref 指向的文件是否都在媒体归档里 -------------------
# 不备媒体字节时，`pg_restore` 与墓碑校验都发现不了问题：库看起来是好的，
# 但 `media_evidence.media_ref`（file:/data/media/…）指向的文件全丢。
# 旧版备份没有 media 字段 → 只能人工核对，这里给出可执行步骤。
REFS_FILE="$(mktemp "${TMPDIR:-/tmp}/ling-drill-refs.XXXXXX")"
LIST_FILE="$(mktemp "${TMPDIR:-/tmp}/ling-drill-list.XXXXXX")"
cleanup_tmp() { rm -f "$REFS_FILE" "$LIST_FILE"; }
trap cleanup_tmp EXIT

if ! drill_sql "SELECT DISTINCT media_ref FROM media_evidence WHERE media_ref IS NOT NULL ORDER BY media_ref" \
    > "$REFS_FILE" 2>/dev/null; then
  echo "[drill] 提示：读不到 media_evidence.media_ref（表不存在？），跳过媒体核对" >&2
  : > "$REFS_FILE"
fi
REF_COUNT="$(grep -c . "$REFS_FILE" || true)"

MEDIA_NAME="$(manifest_value "$MANIFEST" media)"
if [[ -z "$MEDIA_NAME" ]]; then
  echo "[drill] 提示：这份备份的 manifest 没有 media 字段（旧版备份）：它不含媒体字节。"
  echo "  人工核对步骤："
  echo "    1) 在有数据卷的机器上 docker compose exec -T api ls -l /data/media | wc -l"
  echo "    2) 与恢复后库里 file: 开头的 media_ref 条数（当前 ${REF_COUNT} 条）对比"
  echo "    3) 缺的档案只能从其他副本/历史备份里找；找不到就让相关记忆的媒体标注为不可用"
elif [[ "$MEDIA_NAME" == "absent" ]]; then
  if [[ "$REF_COUNT" -gt 0 ]]; then
    echo "[drill] 失败：这份备份不含媒体字节（manifest media=absent），" \
      "但恢复后的库里有 ${REF_COUNT} 条 media_ref——引用全部悬空" >&2
    exit 1
  fi
  echo "  [drill] media=absent，且库里没有 media_ref：一致"
else
  MEDIA_TAR="$DIR/$MEDIA_NAME"
  verify_hash "$MEDIA_TAR" media_sha256 || exit 1
  if ! command -v tar >/dev/null 2>&1; then
    echo "[drill] 提示：本机没有 tar，无法展开媒体归档；请人工核对 ${REF_COUNT} 条 media_ref" >&2
  elif ! tar -tf "$MEDIA_TAR" > "$LIST_FILE" 2>/dev/null; then
    echo "[drill] 失败：媒体归档无法列出内容（归档损坏？）：$MEDIA_TAR" >&2
    exit 1
  else
    echo "[drill] 校验媒体覆盖（${MEDIA_NAME}）"
    TOTAL=0
    MISSING=0
    UNVERIFIABLE=0
    while IFS= read -r ref; do
      if [[ -z "$ref" ]]; then
        continue
      fi
      # `pending:upload:…` 是上传过程中才存在的占位引用，不对应文件，跳过。
      case "$ref" in
        file:*"/media/"*) ;;
        *)
          UNVERIFIABLE=$((UNVERIFIABLE + 1))
          continue
          ;;
      esac
      TOTAL=$((TOTAL + 1))
      rel="media/${ref##*/media/}"
      if ! grep -Fxq "$rel" "$LIST_FILE"; then
        MISSING=$((MISSING + 1))
        if [[ $MISSING -le 5 ]]; then
          echo "  [drill] 缺失：${rel}（media_ref=${ref}）" >&2
        fi
      fi
    done <"$REFS_FILE"
    echo "  media_ref 可核对 ${TOTAL} 条，归档中缺 ${MISSING} 条，无法核对 ${UNVERIFIABLE} 条"
    if [[ $MISSING -gt 0 ]]; then
      echo "[drill] 失败：媒体归档与库里的 media_ref 不一致——恢复后这些媒体会悬空" >&2
      exit 1
    fi
  fi
fi

if [ "$VIA_COMPOSE" = "1" ]; then
  echo "[drill] 完成（隔离库 $DRILL_DB 未触碰生产库，墓碑一致）"
else
  echo "[drill] 完成：备份可恢复，墓碑一致。"
fi
