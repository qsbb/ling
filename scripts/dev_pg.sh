#!/usr/bin/env bash
# 本地便携 PostgreSQL（无 Docker 时的真实数据库开发/测试环境）。
# 依赖 pgserver（仅 Python 3.11 有 wheel），由 uv 在 .data/pgvenv 内安装。
# 用法：
#   scripts/dev_pg.sh start     # 启动（默认端口 55432）
#   scripts/dev_pg.sh stop
#   scripts/dev_pg.sh psql
#   scripts/dev_pg.sh reset     # 删除数据目录后重建
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PGDATA="${LING_PGDATA:-$ROOT/.data/pg}"
PGPORT="${LING_PGPORT:-55432}"
PGUSER="${LING_PGUSER:-ling}"
PGDB="${LING_PGDATABASE:-ling}"
VENV="$ROOT/.data/pgvenv"

ensure_bin() {
  if [ ! -d "$VENV" ]; then
    echo "[dev_pg] 安装 pgserver (python3.11) ..."
    uv venv -q --python 3.11 "$VENV"
    uv pip install -q --python "$VENV/bin/python" pgserver
  fi
  PGBIN="$("$VENV/bin/python" - <<'PY'
import os, pgserver
print(os.path.join(os.path.dirname(pgserver.__file__), "pginstall", "bin"))
PY
)"
  export PATH="$PGBIN:$PATH"
}

case "${1:-start}" in
  start)
    ensure_bin
    if [ ! -f "$PGDATA/PG_VERSION" ]; then
      mkdir -p "$PGDATA"
      initdb -D "$PGDATA" -U "$PGUSER" -A trust --encoding=UTF8 --locale=C >/dev/null
    fi
    if pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
      echo "[dev_pg] 已在运行"
    else
      pg_ctl -D "$PGDATA" -o "-p $PGPORT -k /tmp -c listen_addresses=127.0.0.1" \
        -l "$PGDATA/server.log" start >/dev/null
      sleep 1
    fi
    psql -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER" -d postgres -tAc \
      "SELECT 1 FROM pg_database WHERE datname='$PGDB'" | grep -q 1 \
      || createdb -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER" "$PGDB"
    psql -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER" -d postgres -tAc \
      "SELECT 1 FROM pg_database WHERE datname='${PGDB}_test'" | grep -q 1 \
      || createdb -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER" "${PGDB}_test"
    echo "[dev_pg] 就绪 postgresql+asyncpg://$PGUSER@127.0.0.1:$PGPORT/$PGDB"
    ;;
  stop)
    ensure_bin
    pg_ctl -D "$PGDATA" stop -m fast >/dev/null 2>&1 || true
    echo "[dev_pg] 已停止"
    ;;
  status)
    ensure_bin
    pg_ctl -D "$PGDATA" status || true
    ;;
  psql)
    ensure_bin
    psql -h 127.0.0.1 -p "$PGPORT" -U "$PGUSER" -d "$PGDB"
    ;;
  reset)
    ensure_bin
    pg_ctl -D "$PGDATA" stop -m immediate >/dev/null 2>&1 || true
    rm -rf "$PGDATA"
    echo "[dev_pg] 已删除 $PGDATA，重新执行 start"
    ;;
  *)
    echo "用法: $0 {start|stop|status|psql|reset}" >&2
    exit 2
    ;;
esac
