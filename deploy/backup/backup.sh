#!/usr/bin/env bash
# 数据库一致性备份（PostgreSQL）。用法：deploy/backup/backup.sh OUT_DIR
#
# 约束（设计文档 §10）：数据库备份与媒体清单要在同一恢复点验证；
# 恢复前先核对撤回墓碑，防止旧备份让已删除内容重新可见。
set -euo pipefail

OUT_DIR="${1:?用法: $0 OUT_DIR}"
STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT_DIR"

# 两种运行方式：
#   1) 直接模式：本机/容器内能连到数据库，用 LING_DATABASE_URL
#   2) compose 模式：宿主机上没有 DSN 也不该暴露端口，
#      用 `docker compose exec` 进 db 容器执行（LING_BACKUP_VIA_COMPOSE=1）
#
# 后者是默认部署的真实情况：cron 跑在宿主机，数据库只在 compose 内网。
# 不给这条路的话，"已安装每日备份"实际上一份都备不出来。
VIA_COMPOSE="${LING_BACKUP_VIA_COMPOSE:-0}"
COMPOSE_FILE="${LING_COMPOSE_FILE:-deploy/compose.yaml}"
# 项目名必须显式给：`docker compose -f 路径` 会按**文件所在目录名**推导项目名，
# 从 cron 里跑时目录往往与真实项目名不同（实测：项目叫 ling-verify，
# 目录叫 deploy），于是 exec 找不到正在运行的容器。
COMPOSE_PROJECT="${LING_COMPOSE_PROJECT:-}"
COMPOSE_ARGS=(-f "$COMPOSE_FILE")
if [ -n "$COMPOSE_PROJECT" ]; then
  COMPOSE_ARGS=(-p "$COMPOSE_PROJECT" "${COMPOSE_ARGS[@]}")
fi

if [ "$VIA_COMPOSE" = "1" ]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "[backup] compose 模式需要 docker" >&2
    exit 2
  fi
  PG_DB="${LING_DB_NAME:-ling}"
  PG_USER="${LING_DB_USER:-ling}"
  DUMP_FILE="$OUT_DIR/ling-$STAMP.dump"
  echo "[backup] compose 模式下导出（容器 db）"
  if ! docker compose "${COMPOSE_ARGS[@]}" exec -T db \
      pg_dump --no-owner --no-privileges --format=custom \
      -U "$PG_USER" -d "$PG_DB" > "$DUMP_FILE"; then
    echo "[backup] pg_dump 失败（容器是否在运行？COMPOSE_FILE=$COMPOSE_FILE）" >&2
    rm -f "$DUMP_FILE"
    exit 3
  fi
  if [ ! -s "$DUMP_FILE" ]; then
    echo "[backup] 导出文件为空，视为失败" >&2
    rm -f "$DUMP_FILE"
    exit 3
  fi
  # 墓碑清单同样通过容器取
  docker compose "${COMPOSE_ARGS[@]}" exec -T db \
    psql -U "$PG_USER" -d "$PG_DB" -tAc \
    "select id from memories where retracted = true order by id" \
    > "$OUT_DIR/tombstones-$STAMP.txt" 2>/dev/null || true
  {
    echo "created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "mode=compose"
    echo "dump=ling-$STAMP.dump"
    echo "tombstones=tombstones-$STAMP.txt"
  } > "$OUT_DIR/manifest-$STAMP.txt"
  echo "[backup] 完成：$DUMP_FILE"
  exit 0
fi

URL="${LING_DATABASE_URL:-}"
if [ -z "$URL" ]; then
  echo "缺少 LING_DATABASE_URL（宿主机部署请设 LING_BACKUP_VIA_COMPOSE=1）" >&2
  exit 2
fi
if [[ "$URL" != postgresql* ]]; then
  echo "只支持 PostgreSQL 备份（当前: ${URL%%:*})" >&2
  exit 2
fi

# postgresql+asyncpg://u:p@h:port/db → postgresql://u:p@h:port/db
PG_URL="${URL/postgresql+asyncpg:\/\//postgresql://}"
PG_URL="${PG_URL/postgresql+psycopg:\/\//postgresql://}"

echo "[backup] 导出 schema + 数据"
pg_dump --no-owner --no-privileges --format=custom --file "$OUT_DIR/ling-$STAMP.dump" "$PG_URL"

echo "[backup] 记录撤回墓碑清单（恢复前必须核对）"
psql "$PG_URL" -tAc \
  "SELECT id, retracted_at FROM memories WHERE retracted = true ORDER BY retracted_at" \
  > "$OUT_DIR/tombstones-$STAMP.txt" || echo "[backup] 警告：读取墓碑失败" >&2

# 加密根密钥必须一起备份，但**要分开存放**：
# 数据库里的 provider 凭据是 AES-GCM 加密的，根密钥丢了就全部解不开，
# 表现为"机器人突然连不上模型"，很难第一时间想到是密钥问题。
# 但也不能和 dump 放同一处——一次泄漏就等于同时拿到密文与密钥。
DATA_DIR="${LING_DATA_DIR:-/data}"
KEY_FILE="$DATA_DIR/root.key"
if [[ -f "$KEY_FILE" ]]; then
  mkdir -p "$OUT_DIR/keys"
  cp "$KEY_FILE" "$OUT_DIR/keys/root.key"
  chmod 600 "$OUT_DIR/keys/root.key"
  echo "[backup] 已复制根密钥到 keys/root.key（务必与 dump 分开存放）"
  KEY_NOTE="keys_root=keys/root.key（与 dump 分开存放，否则一次泄漏同时暴露密文与密钥）"
elif [[ -n "${LING_ROOT_KEY:-}" ]]; then
  KEY_NOTE="keys_root=来自环境变量 LING_ROOT_KEY（未落盘，请自行安全记录）"
  echo "[backup] 根密钥来自环境变量，未复制；请确认你已单独妥善保管"
else
  KEY_NOTE="keys_root=未找到根密钥（首次启动前可能尚未生成）"
  echo "[backup] 警告：未找到根密钥，恢复后加密凭据可能无法解密" >&2
fi

echo "[backup] 记录迁移版本"
psql "$PG_URL" -tAc "SELECT version()" > "$OUT_DIR/version-$STAMP.txt"

# manifest 里**不能**写完整连接串：它含数据库密码，
# 而备份目录往往会被同步到别的机器/网盘，等于把密码一起搬走。
# 只记录脱敏后的主机与库名，够复现"备份的是哪个库"。
# 用参数展开脱敏，避免 sed 转义陷阱
if [[ "$URL" == *"@"* ]]; then
  SAFE_DB="${URL%%://*}://<redacted>@${URL#*@}"
else
  SAFE_DB="$URL"
fi
cat > "$OUT_DIR/manifest-$STAMP.txt" <<MANIFEST
created_at=$STAMP
database=$SAFE_DB
dump=ling-$STAMP.dump
tombstones=tombstones-$STAMP.txt
$KEY_NOTE
MANIFEST

echo "[backup] 完成：$OUT_DIR/ling-$STAMP.dump"
