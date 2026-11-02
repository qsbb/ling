#!/usr/bin/env bash
# 容器入口：等待数据库 → 跑迁移 → 启动目标进程。
# 迁移失败时不启动 api（避免用错误 schema 服务用户）。
set -euo pipefail

MODE="${1:-api}"
echo "[entrypoint] mode=$MODE env=${LING_ENV:-dev}"

wait_for_db() {
  python - <<'PY'
import asyncio, os, sys

async def main() -> int:
    import asyncpg
    url = os.environ.get("LING_DATABASE_URL", "")
    if not url:
        print("[entrypoint] 缺少 LING_DATABASE_URL", file=sys.stderr)
        return 2
    dsn = url.replace("postgresql+asyncpg://", "postgresql://")
    for attempt in range(60):
        try:
            conn = await asyncpg.connect(dsn, timeout=3)
            await conn.execute("SELECT 1")
            await conn.close()
            print("[entrypoint] 数据库就绪")
            return 0
        except Exception as exc:  # noqa: BLE001
            if attempt % 5 == 0:
                print(f"[entrypoint] 等待数据库 ({attempt}/60): {exc}", file=sys.stderr)
            await asyncio.sleep(2)
    return 1

sys.exit(asyncio.run(main()))
PY
}

migrate() {
  if [ "${LING_RUN_MIGRATIONS:-1}" = "0" ]; then
    echo "[entrypoint] 跳过迁移（LING_RUN_MIGRATIONS=0）"
    return 0
  fi
  echo "[entrypoint] 执行迁移"
  python -m ling.migrate upgrade
}

case "$MODE" in
  api)
    wait_for_db
    migrate
    exec python -m uvicorn ling.app:app --host 0.0.0.0 --port "${LING_PORT:-6520}" \
      --proxy-headers --forwarded-allow-ips='*' --no-server-header
    ;;
  worker)
    wait_for_db
    exec python -m ling.worker
    ;;
  migrate)
    wait_for_db
    migrate
    ;;
  *)
    # 允许直接传命令（compose 的 command 覆盖会成为 $1，例如 "python"）
    # 此时不应把第一个词当成模式名
    exec "$@"
    ;;
esac
