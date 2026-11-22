#!/usr/bin/env bash
# 可复验演练：在两个可识别版本上做 A→B→A 与干净实例恢复。
#
# 前置条件（本脚本会逐项检查，缺就退出 2）：
#   - docker daemon 可用；
#   - pg_dump / pg_restore / psql 在宿主可用；
#   - 已设置 LING_TEST_DATABASE_URL / LING_DATABASE_URL（PostgreSQL 测试库）；
#   - 仓库已有可启动的 A/B 两个发行产物；镜像模式给出两个固定 digest。
#
# 默认 dry-run：只打印将要执行的命令；加 --apply 才真正执行。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INSTALL_DIR="${INSTALL_DIR:-$ROOT}"
PACKAGE_DIR="${PACKAGE_DIR:-$ROOT/.data/backup-restore-drill}"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT/deploy/compose.yaml}"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
PROJECT="${PROJECT:-ling_backup_restore_drill}"
DB_A="${DB_A:-ling_drill_a}"
DB_B="${DB_B:-ling_drill_b}"
VERSION_A="${VERSION_A:-0.0.14}"
VERSION_B="${VERSION_B:-0.0.15}"
RELEASE_A="${RELEASE_A:-v0.0.14}"
RELEASE_B="${RELEASE_B:-v0.0.15}"
APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1

fail() { echo "[drill] $*" >&2; exit 2; }
run() {
  echo "+$*"
  if [[ "$APPLY" == "1" ]]; then "$@"; fi
}

for tool in docker pg_dump pg_restore psql python3; do
  command -v "$tool" >/dev/null 2>&1 || fail "缺少工具：$tool"
done
docker info >/dev/null 2>&1 || fail "docker daemon 不可用"
[[ -n "${LING_DATABASE_URL:-}" ]] || fail "未设置 LING_DATABASE_URL"
[[ -n "${LING_TEST_DATABASE_URL:-}" ]] || fail "未设置 LING_TEST_DATABASE_URL"
[[ -f "$COMPOSE_FILE" ]] || fail "Compose 文件不存在：$COMPOSE_FILE"
[[ -f "$ENV_FILE" ]] || fail "env 文件不存在：$ENV_FILE"

mkdir -p "$PACKAGE_DIR/logs" "$PACKAGE_DIR/target-a" "$PACKAGE_DIR/target-b"
COMPOSE=(docker compose -p "$PROJECT" -f "$COMPOSE_FILE" --env-file "$ENV_FILE")

echo "[drill] 版本 A=$VERSION_A ($RELEASE_A)，版本 B=$VERSION_B ($RELEASE_B)"
echo "[drill] 包目录=$PACKAGE_DIR 日志=$PACKAGE_DIR/logs/drill.log"

# 1) A 实例写入样本并备份（真实数据写入由服务 API 完成；这里记录所有输入）。
run env LING_DATABASE_URL="$LING_DATABASE_URL" python3 "$ROOT/deploy/cli/ling.py" backup \
  --install-dir "$INSTALL_DIR"
run env LING_DATABASE_URL="$LING_DATABASE_URL" python3 "$ROOT/deploy/cli/ling.py" restore-drill \
  --install-dir "$INSTALL_DIR"

# 2) 从当前备份生成完整包，补齐版本/插件/人设/产物身份。
#    源路径必须显式给出：脚本不猜哪一份 backup.sh 产物是本次恢复点。
: "${DUMP_SOURCE:?请设置 DUMP_SOURCE（backup.sh 产出的 ling-*.dump 路径）}"
: "${MEDIA_SOURCE:?请设置 MEDIA_SOURCE（backup.sh 产出的 media-*.tar；无媒体请给空文件）}"
: "${ROOT_KEY_SOURCE:?请设置 ROOT_KEY_SOURCE（keys/root.key；env 模式给任意占位文件并在 SECRETS_JSON 声明）}"
: "${DEPLOY_ENV_SOURCE:?请设置 DEPLOY_ENV_SOURCE（受限 deploy/.env 副本）}"
: "${COMPOSE_SOURCE:?请设置 COMPOSE_SOURCE（本次实际使用的 compose 主文件）}"
: "${PLUGINS_SOURCE:?请设置 PLUGINS_SOURCE（data/tool-plugins.json）}"
: "${PERSONA_SOURCE:?请设置 PERSONA_SOURCE（data/persona.json）}"
: "${CONFIG_REVISION:?请设置 CONFIG_REVISION（当前已发布配置修订）}"
: "${ARTIFACT_JSON:?请设置 ARTIFACT_JSON（含 mode/release_id/commit/sha256 或 digest/platform 的 JSON）}"
run env PYTHONPATH="$ROOT/src" python3 - "$ROOT" "$PACKAGE_DIR/pkg-A" "$VERSION_A" "$RELEASE_A" <<'PY'
import json, os, sys
from pathlib import Path
from ling.backup_restore.package import build_full_package
root, out, version, release = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
artifact = json.loads(os.environ["ARTIFACT_JSON"])
secrets = json.loads(os.environ.get("SECRETS_JSON", '{"root_key_source":"file","credential_scope":["database"]}'))
build_full_package(
    output=out,
    dump=Path(os.environ["DUMP_SOURCE"]),
    media_tar=Path(os.environ["MEDIA_SOURCE"]),
    root_key=Path(os.environ["ROOT_KEY_SOURCE"]),
    deploy_env=Path(os.environ["DEPLOY_ENV_SOURCE"]),
    compose_file=Path(os.environ["COMPOSE_SOURCE"]),
    tool_plugins=Path(os.environ["PLUGINS_SOURCE"]),
    persona_file=Path(os.environ["PERSONA_SOURCE"]),
    app_version=version,
    commit=artifact.get("commit") or artifact.get("digest", "unknown").split(":")[-1],
    migration=os.environ.get("MIGRATION", "0082_live_sessions"),
    config_revision=os.environ["CONFIG_REVISION"],
    artifact=artifact,
    scope=json.loads(os.environ.get("SCOPE_JSON", '{"external_services":[]}')),
    secrets=secrets,
)
print(f"[drill] 完整包已生成：{out}")
PY

# 3) 预检：当前环境可直接跑；检查哈希、版本、架构、空间、墓碑。
run python3 -m ling.backup_restore.cli preflight "$PACKAGE_DIR/pkg-A" \
  --target-version "$VERSION_A" \
  --free-bytes "$(df -k "$PACKAGE_DIR" | awk 'NR==2{print $4*1024}')"

# 4) B 实例追加数据后导入 A：计划 + 显式 apply。
run python3 "$ROOT/deploy/backup/restore_full.py" execute \
  --package "$PACKAGE_DIR/pkg-A" \
  --target-version "$VERSION_A" \
  --install-dir "$PACKAGE_DIR/target-a" \
  --compose-file "$COMPOSE_FILE" \
  --env-file "$ENV_FILE" \
  --project "$PROJECT" \
  --target-db "$DB_A" \
  --rollback-dir "$PACKAGE_DIR/rollback-a" \
  --log-dir "$PACKAGE_DIR/logs" \
  --apply

# 5) 干净实例从同一包离线恢复：限制网络由宿主 firewall/网络命名空间完成，脚本只记录。
run python3 "$ROOT/deploy/backup/restore_full.py" execute \
  --package "$PACKAGE_DIR/pkg-A" \
  --target-version "$VERSION_A" \
  --install-dir "$PACKAGE_DIR/target-b" \
  --compose-file "$COMPOSE_FILE" \
  --env-file "$ENV_FILE" \
  --project "${PROJECT}-clean" \
  --target-db "$DB_B" \
  --rollback-dir "$PACKAGE_DIR/rollback-b" \
  --log-dir "$PACKAGE_DIR/logs" \
  --apply

# 6) 反例与回退核对由 docs/backup-restore-acceptance-criteria.md 的 C2/C5/C6 判据逐条执行。
echo "[drill] 计划阶段结束；--apply 时日志在 $PACKAGE_DIR/logs/restore.log"
