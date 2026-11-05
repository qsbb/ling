#!/usr/bin/env bash
# 便捷入口：委托到 deploy/backup/restore_drill.sh
exec "$(dirname "$0")/../deploy/backup/restore_drill.sh" "$@"
