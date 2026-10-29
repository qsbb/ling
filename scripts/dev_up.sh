#!/usr/bin/env bash
# 起开发环境的两个进程：API + worker。
#
# ## 为什么必须有这个脚本
#
# 只起 API 的话，**所有自主行为都不会发生**：
# 群聊轮询、意图过期、主动收集、记忆提炼、关系更新、窗口摘要——
# 它们全在 worker 里。API 能回话，所以她"看起来能用"，
# 但"会自己做事"这件事完全没发生，而且没有任何报错。
#
# 实测踩过：跑了很久的 API，库里攒了 769 条 outbox 没人消费、
# 群聊从未被轮询过、到点的念头从未变成行动。
#
# ## 用法
#
#   . scripts/dev_env.sh && bash scripts/dev_up.sh      # 起
#   bash scripts/dev_up.sh stop                         # 停
#   bash scripts/dev_up.sh status                       # 看状态
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
# 固定 /tmp：macOS 的 $TMPDIR 是 /var/folders/...，与既有 pid 文件位置不一致
PID_DIR="/tmp"
API_PID="$PID_DIR/ling8080.pid"
WORKER_PID="$PID_DIR/ling_worker.pid"

start_one() {
  local name="$1" pidfile="$2" cmd="$3" logfile="$4"
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile" 2>/dev/null)" 2>/dev/null; then
    echo "$name 已在运行（pid $(cat "$pidfile")）"
    return 0
  fi
  nohup $cmd > "$logfile" 2>&1 &
  echo $! > "$pidfile"
  echo "$name 已启动（pid $(cat "$pidfile")，日志 ${logfile}）"
}

stop_one() {
  local name="$1" pidfile="$2"
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile" 2>/dev/null)" 2>/dev/null; then
    kill "$(cat "$pidfile")" 2>/dev/null
    echo "$name 已停止"
  else
    echo "$name 没在运行"
  fi
  rm -f "$pidfile"
}

case "${1:-start}" in
  start)
    start_one "api"    "$API_PID"    "python -m ling"        "$PID_DIR/ling8080.log"
    start_one "worker" "$WORKER_PID" "python -m ling.worker" "$PID_DIR/ling_worker.log"
    ;;
  stop)
    stop_one "api" "$API_PID"
    stop_one "worker" "$WORKER_PID"
    ;;
  status)
    for pair in "api:$API_PID" "worker:$WORKER_PID"; do
      name="${pair%%:*}"; pidfile="${pair##*:}"
      if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile" 2>/dev/null)" 2>/dev/null; then
        echo "$name: 运行中（pid $(cat "$pidfile")）"
      else
        echo "$name: 未运行"
      fi
    done
    ;;
  *)
    echo "用法: $0 {start|stop|status}" >&2
    exit 2
    ;;
esac
