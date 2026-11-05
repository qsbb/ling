#!/usr/bin/env bash
# 安装每日备份定时任务（无人值守）。
#
# 用法：deploy/backup/install-cron.sh /path/to/backup/dir [HH:MM]
#
# 为什么必须定时：个人服务器只有一份数据源。手动备份等于"想起来才备"，
# 而事故不会等你想起来。备份脚本本身已能跑，缺的只是"每天自动跑"。
#
# 为什么 crontab 行里必须显式写 LING_COMPOSE_PROJECT：
# `docker compose -f <路径>` 在没有 -p/--project-name 时按**文件所在目录名**
# 推导项目名（compose.yaml 在 deploy/ 下 → 推导成 `deploy`），而安装器用
# `--project-name`（源码模式默认值见 deploy/install.sh 的 `PROJECT=` 行）。名字
# 不一致时 `docker compose exec db` 找不到正在运行的容器，cron 里的备份会静默
# 一份都备不出来（"已安装每日备份"≠"备出来了"）。所以这里把项目名写进 cron 行：
# 优先 LING_COMPOSE_PROJECT，其次 install.sh 的默认值，**最后回退内置 `ling`**
# ——镜像模式的独立部署包不含旧 install.sh，没有回退就装不上定时任务。
#
# 同一行里还写着整个 Compose **文件组**与 `--env-file` 的来源
# （LING_COMPOSE_FILE / LING_COMPOSE_OVERRIDE / LING_COMPOSE_ENV_FILE），
# 保证 cron 环境解析出的拓扑与安装/运行时一致（尤其是 LING_APP_IMAGE）。
#
# 为什么还要在 cron 行里**导出 LING_ROOT_KEY**：
# 镜像模式的 `ling install` 把根密钥放在受限 `.env`（compose 以环境变量注入
# 容器），容器里**没有** `/data/root.key`。cron 环境里也没有任何 `LING_*` 可
# 继承，于是 backup.sh 既读不到文件、又看不到环境变量 → manifest 只能写
# `keys_env_set=false` + `keys_root=absent`，`ling doctor` 对每天的定时备份告警、
# `ling backup` 的完整性校验把它判成"恢复到新卷后凭据解不开"（见
# deploy/cli/ling.py 的 has_keys 判定）。手动 `ling backup` 之所以合格，是因为
# 它自己从 `.env` 读值并注入子进程环境——cron 这条自动化路径缺的正是这一步。
#
# 做法：cron 行在调 backup.sh **之前**用命令替换从同一份受限 env 读出该项并
# 导出。两条红线：
#   1. **明文密钥绝不进 crontab**（crontab 通常全局可读）：行里只有文件路径与
#      `sed` 表达式，值在运行时展开，只存在于 backup.sh 的进程环境；
#   2. **不 source** 那个文件（不执行其中的任意 shell 片段），只按 `KEY=VALUE`
#      取一行。
# env 里没有这一项时（源码模式：密钥在容器卷 `/data/root.key`）**整段不加**，
# cron 行与改动前逐字节一致，安装也不会因此失败。
set -euo pipefail

BACKUP_DIR="${1:?用法: $0 BACKUP_DIR [HH:MM]}"
RUN_AT="${2:-04:30}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_SH="$SCRIPT_DIR/backup.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
INSTALL_SH="$REPO_ROOT/deploy/install.sh"

if [[ ! -x "$BACKUP_SH" ]]; then
  echo "找不到可执行的备份脚本: $BACKUP_SH" >&2
  exit 1
fi

# 时间必须是 HH:MM：这个值会原样进 crontab，不接受任意字符串。
if [[ ! "$RUN_AT" =~ ^[0-9]{2}:[0-9]{2}$ ]]; then
  echo "时间格式必须是 HH:MM（拿到: ${RUN_AT}）" >&2
  exit 1
fi
HOUR="${RUN_AT%%:*}"
MIN="${RUN_AT##*:}"
if (( 10#$HOUR > 23 || 10#$MIN > 59 )); then
  echo "时间超出范围（拿到: ${RUN_AT}）" >&2
  exit 1
fi

# Compose 项目名：显式写进 crontab 行，取值优先级为
#   1) 环境变量 LING_COMPOSE_PROJECT（显式覆盖）；
#   2) deploy/install.sh 的 `PROJECT=` 默认值（源码模式部署，保持单一真源）；
#   3) **内置默认值 `ling`**。
# 第 3 条是本次修正的关键：镜像模式的独立部署包**不含旧 install.sh**，旧实现
# 在缺 install.sh 时直接失败，等于"备份 cron 装不上"；而设计要求"缺失旧
# install.sh 时必须显式指定 LING_COMPOSE_PROJECT"。所以默认值必须自带，
# 不能依赖旧安装器存在。
install_default_project() {
  local line value
  [[ -r "$INSTALL_SH" ]] || return 1
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      PROJECT=*) ;;
      *) continue ;;
    esac
    value="${line#PROJECT=}"
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    # 只接受 Compose 项目名允许的形状，任何可疑内容都拒绝（而不是猜）。
    case "$value" in
      ''|*[!a-z0-9_-]*|[!a-z0-9]*) return 1 ;;
    esac
    printf '%s\n' "$value"
    return 0
  done <"$INSTALL_SH"
  return 1
}

# 受限 env 里有没有**非空**的 LING_ROOT_KEY？只看"有没有"，**绝不回显值**。
#
# 语义必须与下面生成进 cron 行的 `sed | tail -n 1 | tr` 片段一致，否则会出现
# "安装时判定有、运行时读出空"（或反之）的静默分歧：
#   - 同一项出现多次以**最后一行**为准（compose 的 env-file 语义）；
#   - 去掉一层成对引号（compose 会去引号，应用实际用的就是去引号后的值）；
#   - 忽略空行与注释行（含前导空白后的 `#`），行尾 CR 不算内容；
#   - 键必须**紧贴** `=`（`LING_ROOT_KEY` 后面不能有空格），与 sed 的匹配范围相同。
# **不 source** 文件：`.env` 是数据，不是可执行脚本。
env_file_root_key_present() {
  local file="$1" line key value found=0
  [[ -r "$file" ]] || return 1
  # `bash -x` 调试时不要把读到的值打进 trace：这些赋值只用于判空。
  local xtrace_was_on=0
  case "$-" in
    *x*)
      set +x
      xtrace_was_on=1
      ;;
  esac
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    line="${line#"${line%%[![:space:]]*}"}"
    case "$line" in
      ''|'#'*) continue ;;
      *=*) ;;
      *) continue ;;
    esac
    key="${line%%=*}"
    [[ "$key" == "LING_ROOT_KEY" ]] || continue
    value="${line#*=}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    case "$value" in
      \"*\") value="${value#\"}" && value="${value%\"}" ;;
      \'*\') value="${value#\'}" && value="${value%\'}" ;;
    esac
    found=1
  done <"$file"
  # 判空也放在 trace 关闭期间：`bash -x` 不能把密钥打进日志（连 `[[ -n V ]]`
  # 的 trace 都会原样打印操作数）。
  local has_key=0
  if [[ "$found" == "1" && -n "$value" ]]; then
    has_key=1
  fi
  if [[ "$xtrace_was_on" == "1" ]]; then
    set -x
  fi
  # 最后一行为空（`LING_ROOT_KEY=`）等于没给：与 sed 取出空串的行为一致。
  [[ "$has_key" == "1" ]]
}

if [[ -n "${LING_COMPOSE_PROJECT:-}" ]]; then
  PROJECT="$LING_COMPOSE_PROJECT"
  PROJECT_SOURCE="环境变量 LING_COMPOSE_PROJECT"
elif PROJECT="$(install_default_project)"; then
  PROJECT_SOURCE="deploy/install.sh 的 PROJECT= 默认值"
else
  PROJECT="ling"
  PROJECT_SOURCE="内置默认值 ling（未找到 ${INSTALL_SH}）"
  echo "[install-cron] 提示：未找到 ${INSTALL_SH}，改用内置默认项目名 ling；" \
    "若宿主实际项目名不同，请显式设置 LING_COMPOSE_PROJECT=<值> 后重装" >&2
fi
case "$PROJECT" in
  ''|*[!a-z0-9_-]*|[!a-z0-9]*)
    echo "Compose 项目名不合法（拿到: ${PROJECT}）" >&2
    exit 1
    ;;
esac

mkdir -p "$BACKUP_DIR"

# 宿主机的 cron 里**没有 DSN**，数据库也只在本 compose 内网：
# 直接跑 pg_dump 必然失败（"已安装每日备份"其实一份都备不出来）。
# 因此默认走 compose 模式，通过 docker compose exec 进 db 容器导出。
# 项目名/文件组/目录全部显式写进这一行：cron 环境没有 LING_* 变量可继承。
# 默认 compose 文件与 `cd` 用同一个 $PWD，避免两处推导出不同目录。
# 源码里已经能轮转，但**默认关闭**：这一行不写任何 `--prune-*` 参数，
# 于是 backup.sh 永不删除历史产物（与历史行为逐字节一致）。见下面「推荐 cron 参数」。
COMPOSE_FILE="${LING_COMPOSE_FILE:-$PWD/deploy/compose.yaml}"
# 可选 override（镜像模式 + WS 端口时才需要）：与主文件同属一个文件组，
# 顺序是"主文件在前、override 在后"。显式给了却不存在的路径直接拒绝安装——
# 让 cron 每天失败重试没有意义，宁可安装时就报错。
COMPOSE_OVERRIDE="${LING_COMPOSE_OVERRIDE:-}"
if [[ -n "$COMPOSE_OVERRIDE" && ! -f "$COMPOSE_OVERRIDE" ]]; then
  echo "LING_COMPOSE_OVERRIDE 指向的不是一个存在的文件（${COMPOSE_OVERRIDE}）" >&2
  exit 1
fi
# `--env-file`：cron 里没有交互式 shell 的 LING_* 变量，compose 的插值
# （镜像模式的 LING_APP_IMAGE、POSTGRES_PASSWORD 等）必须靠受限 env 文件。
# 显式 LING_COMPOSE_ENV_FILE 优先；否则取主文件同目录的 .env（与 backup.sh
# 的默认发现逻辑同一份）。找不到时不写这个变量，并提示后果。
COMPOSE_ENV_FILE="${LING_COMPOSE_ENV_FILE:-}"
COMPOSE_ENV_FILE_IS_DEFAULT=0
if [[ -z "$COMPOSE_ENV_FILE" ]]; then
  COMPOSE_ENV_FILE_IS_DEFAULT=1
  case "$COMPOSE_FILE" in
    */*) COMPOSE_ENV_FILE="${COMPOSE_FILE%/*}/.env" ;;
    *) COMPOSE_ENV_FILE="$PWD/.env" ;;
  esac
fi
if [[ ! -f "$COMPOSE_ENV_FILE" ]]; then
  if [[ "$COMPOSE_ENV_FILE_IS_DEFAULT" == "0" ]]; then
    echo "LING_COMPOSE_ENV_FILE 指向的不是一个存在的文件（${COMPOSE_ENV_FILE}）" >&2
    exit 1
  fi
  echo "[install-cron] 警告：没有找到 env 文件（候选 ${COMPOSE_ENV_FILE}），" \
    "cron 行里不写 LING_COMPOSE_ENV_FILE。镜像模式下 LING_APP_IMAGE 等插值变量" \
    "将无法解析，备份会以 'compose 配置校验失败'（exit 2）告警——" \
    "请把受限 env 放到该路径，或用 LING_COMPOSE_ENV_FILE 指定" >&2
  COMPOSE_ENV_FILE=""
fi

# 根密钥导出（镜像模式的关键一步，见文件头）：**安装时判定、运行时取值**。
# 判定只回答"要不要加这段"；值的真源始终是运行时的那份 env 文件——安装完再改
# `.env` 里的值，cron 下一天就用新的；把这一项删掉，导出为空、backup.sh 回到
# 旧行为（不再报错，只是 manifest 又会记 keys_env_set=false）。
ROOT_KEY_EXPORT=""
ROOT_KEY_NOTE="没有可读的受限 env 文件 → cron 行不导出（密钥只能来自容器卷 /data/root.key）"
if [[ -n "$COMPOSE_ENV_FILE" ]]; then
  if [[ ! -r "$COMPOSE_ENV_FILE" ]]; then
    # 装 cron 的用户读不到 env（属主/权限不对）：装成功但**不能假装**"没有密钥"。
    # cron 是同一个用户跑的，运行时同样读不到，所以这里必须当场告警。
    ROOT_KEY_NOTE="受限 env 不可读（${COMPOSE_ENV_FILE}）→ cron 行不导出"
    echo "[install-cron] 警告：${COMPOSE_ENV_FILE} 不可读（当前用户），无法确认" \
      "LING_ROOT_KEY。cron 定时备份将无法导出根密钥，manifest 会记" \
      "keys_env_set=false（ling doctor 会告警）。请修正该文件的属主/权限" \
      "（0600 且属主是装 cron 的用户）后重装。" >&2
  elif env_file_root_key_present "$COMPOSE_ENV_FILE"; then
    # 这段是**运行时**由 /bin/sh（cron 的 SHELL）执行的，只能写 POSIX 语法：
    #   - sed 只挑 `[空白]LING_ROOT_KEY=` 开头的行，tail 取最后一次赋值（最后者生效）；
    #   - tr 去掉行尾 CR 与引号（`\042`=`"`、`\047`=`'`，八进制转义在 BSD/GNU tr 上一致）；
    #   - 命令替换的结果只进入 backup.sh 的进程环境：不落盘、不进 crontab、不打印。
    # 不用 `source`：那会执行 env 文件里的任意 shell 片段。
    # 文件在运行时读不到时 sed 会在 backup.log 里留一行错误，而导出为空 → 备份照旧
    # 继续（不会因为密钥读不到就整晚不备份），但日志里有迹可循。
    ROOT_KEY_EXPORT="LING_ROOT_KEY=\"\$(sed -n 's/^[[:space:]]*LING_ROOT_KEY=//p' '$COMPOSE_ENV_FILE' | tail -n 1 | tr -d '\\r\\042\\047')\" "
    ROOT_KEY_NOTE="受限 env 里有 LING_ROOT_KEY → cron 行在运行时从该文件读取并导出（明文不进 crontab）"
  else
    ROOT_KEY_NOTE="受限 env 里没有 LING_ROOT_KEY（源码模式：密钥在容器卷 /data/root.key）→ cron 行不导出"
  fi
fi

# 文件组/环境变量部分：只写**确实需要**的项（override/env 未启用时不留空变量，
# 保持与历史 cron 行同样的形状）。
GROUP_VARS="LING_COMPOSE_PROJECT='$PROJECT' LING_COMPOSE_FILE='$COMPOSE_FILE'"
if [[ -n "$COMPOSE_OVERRIDE" ]]; then
  GROUP_VARS="$GROUP_VARS LING_COMPOSE_OVERRIDE='$COMPOSE_OVERRIDE'"
fi
if [[ -n "$COMPOSE_ENV_FILE" ]]; then
  GROUP_VARS="$GROUP_VARS LING_COMPOSE_ENV_FILE='$COMPOSE_ENV_FILE'"
fi
# ${ROOT_KEY_EXPORT} 为空时（源码模式）拼出来的行与改动前**逐字节一致**。
LINE="$MIN $HOUR * * * cd '$PWD' && ${ROOT_KEY_EXPORT}LING_BACKUP_VIA_COMPOSE=1 $GROUP_VARS LING_BACKUP_DIR='$BACKUP_DIR' '$BACKUP_SH' '$BACKUP_DIR' >> '$BACKUP_DIR/backup.log' 2>&1"

# ---------------------------------------------------------------- 推荐 cron 参数
#
# **默认不开启删除**：上面的 LINE 里一个 `--prune-*` 都没有，所以"永不删除"
# 是装完之后的实际行为。要轮转时，把下面这组参数**手工**加到 LINE 里
# `'$BACKUP_DIR'` 之后（保持默认不动是为了不让一次安装决定删掉历史备份）：
#
#   --prune-keep 14                     # 推荐：保留最近 14 份完整组合（历史回看 13 天）
#   --prune-keep 14 --prune-days 30     # 更保守：份数上限 + 只删 30 天以上的组（取整见 backup.sh）
#   --prune-keep 1                      # 反例：会删光全部历史完整组合（只剩本次），别手滑
#
# 口径：`--prune-keep N` 的 **N 含本次新备份**（跑完目录里最多留 N 份完整组合），
# 每日备份时**历史回看 N−1 天**。所以 `--prune-keep 14` 是"今天 + 13 天历史"，
# 不是"14 天历史"；`--prune-keep 1` 合法但会删光全部历史**完整组合**（不合格组
# 永远不删，所以目录里实际份数可能多于 N）。
#
# 推荐理由：`media-*.tar` 是**全量**媒体副本，每天一份线性累积，媒体是有增长的
# 那一部分；`--prune-keep 14` 给出 13 天历史回看窗口，比"永远不删"稳。想更久就调大
# N，但不要只靠"磁盘还够用"。
#
# 轮转的安全边界（backup.sh 里实现）：只删「dump + manifest + tombstones + media
# 齐全且 manifest 可解析」的组合。零撤回时 `tombstones-*.txt` 是 0 字节，这是
# **合法产物**（文件存在 + tombstones_state=ok 即算齐全），正常参与轮转；孤儿
# 文件、缺 manifest 的目录、墓碑文件缺失、墓碑导出失败（state=FAILED）、manifest
# 读不懂的旧格式一律不动并告警；`keys/root.key` **永不**被删（它没有日期戳，是
# 当前这把密钥的唯一副本）。
#
# 加参数之前先看计划（--dry-run 不动盘）：
#   '$BACKUP_SH' '$BACKUP_DIR' --prune-keep 14 --dry-run
#
# 另外两个开关：`--force-unlock` 清掉崩溃残留的备份锁（锁占用时脚本 exit 6）；
# 同目录并发跑的第二个实例会被锁拒绝（exit 6），不会两边一起写盘。`--force-unlock`
# **只对已死进程的残留锁用**：对活着的实例强开会让两边同时写同一批产物，后启动
# 的那份通常会在媒体完整性检查处失败（QA 观察到 exit 5），但脚本没有并发两写的
# 最终一致性校验，不能保证绝不会以 0 结束；覆盖已经发生。

# 幂等：先删掉同脚本的旧条目（含旧版本没写项目名的行），再加当前这一条。
# 用纯 bash 过滤而不再调 grep：少一个"参数写错就静默留下旧行"的可能。
# crontab -l 的 exit 1 只表示"当前用户还没有 crontab"；其它失败（权限/文件损坏）
# 一律中止——否则会把"读不到"当成"空的"，用一行覆盖掉别人的全部定时任务。
if existing="$(crontab -l 2>/dev/null)"; then
  :
else
  list_status=$?
  if (( list_status != 1 )); then
    echo "[install-cron] 读取现有 crontab 失败（crontab -l exit=${list_status}），未做任何修改" >&2
    exit 1
  fi
  existing=""
fi
lines=()
while IFS= read -r old_line; do
  if [[ -z "$old_line" ]]; then
    continue
  fi
  case "$old_line" in
    *"$BACKUP_SH"*) continue ;;
  esac
  lines+=("$old_line")
done <<<"$existing"
lines+=("$LINE")
printf '%s\n' "${lines[@]}" | crontab -

echo "[install-cron] 已安装每日备份：$RUN_AT"
echo "[install-cron] 备份目录：$BACKUP_DIR"
echo "[install-cron] Compose 项目：${PROJECT}（来源: ${PROJECT_SOURCE}）"
echo "[install-cron] Compose 文件组：${COMPOSE_FILE}${COMPOSE_OVERRIDE:+ + ${COMPOSE_OVERRIDE}}"
echo "[install-cron] Compose env 文件（--env-file）：${COMPOSE_ENV_FILE:-（未找到，未写入 cron 行）}"
echo "[install-cron] 根密钥导出：${ROOT_KEY_NOTE}"
echo
echo "注意事项："
echo "  - 该目录必须**与数据库所在磁盘分开**（异机/网盘/外置盘），"
echo "    否则磁盘坏掉时备份与原文一起丢。"
echo "  - 镜像模式的根密钥在受限 env 的 LING_ROOT_KEY 里（容器里通常没有"
echo "    /data/root.key）。cron 行会在**运行时**从那份 env 读这一项并导出给"
echo "    backup.sh：行里只有路径和 sed 表达式，**明文密钥不进 crontab**（crontab"
echo "    通常全局可读）。所以那份 env 必须对 cron 用户可读，装完也不要把它挪走"
echo "    或删掉——读不到时备份仍继续跑，但 manifest 会记 keys_env_set=false，"
echo "    恢复时凭据就解不开了。改过 env 里**已有**的值不用重装，下一次 cron"
echo "    自动用新值；原来是**没有**这一项的（那时判定不导出），加进去之后要"
echo "    重跑本脚本一次。"
echo "  - manifest 的 keys_root=present 表示根密钥被归档到 keys/root.key，"
echo "    它必须**单独保管**（数据库里的 provider 凭据是加密的，根密钥丢了"
echo "    就全部解不开）。keys_root_source=file 只说明'密钥字节来自文件'，"
echo "    不说明应用用哪一把：LING_ROOT_KEY 非空时应用**优先用环境变量**，"
echo "    此时 manifest 会记 keys_env_set=true——恢复时那份 deploy/.env 也"
echo "    必须一起恢复，否则只回灌 keys/root.key 可能解不开。"
echo "  - media-*.tar 是**全量**媒体副本，每天一份会线性累积。本安装**不删**任何"
echo "    历史备份（cron 行里没有 --prune-*）；要轮转请按脚本内「推荐 cron 参数」"
echo "    手工加上 --prune-keep N [--prune-days D]，先用 --dry-run 看计划。"
echo "    N 含本次新备份、历史回看 N−1 天（--prune-keep 1 会删光全部历史完整组合，"
echo "    不合格组不删）；零撤回时 0 字节墓碑是合法产物，正常参与轮转。轮转只删"
echo "    「dump+manifest+tombstones+media 齐全且 manifest 可解析」的组合，"
echo "    keys/root.key 永不删（见 docs/DEPLOYMENT.md §8.1）。"
echo "  - 同目录并发：backup.sh 用 mkdir 锁互斥，第二次运行会以 exit 6 退出并打印"
echo "    持锁的 pid/启动时间；崩溃残留的锁会被识别并接管。--force-unlock 只对"
echo "    已死的残留锁用（对活实例强开会两写；后启动的那份通常会在媒体完整性检查"
echo "    处 exit 5，但脚本没有并发两写的最终一致性校验，不保证绝不会以 0 结束）。"
echo "  - 备份失败时脚本会删掉**本次**已产出的片段（dump/墓碑/媒体/version），"
echo "    不会留下 '看起来像备份' 的残目录；成功的标志是当天有新 manifest。"
echo "    共享目录里历史备份的 dump/manifest/media 一概不动；keys/root.key 也一样"
echo "    （它没有日期戳，同时就是前一天那份备份的密钥归档）——本次覆盖过它会先"
echo "    把原字节放回，放不回去也只告警、绝不删。"
echo "  - 权限现状（脚本有意不改，免得让既有部署写不进去）：这个目录由 mkdir -p"
echo "    创建 → 0755；dump/manifest/tombstones 是 0644；media-*.tar 与"
echo "    keys/root.key 创建时就**无可读位**（group/other 全 0；ext4 上是 0600，"
echo "    NAS 的 btrfs+trimacl 上是 0700，两者同样安全：重定向在 umask 077 的子"
echo "    shell 里做，不再有'先 0644 再 chmod'的窗口）。dump 里有业务数据，请把父目录自己"
echo "    收紧（chmod 700），别放在 /tmp 或共享盘的可浏览位置。"
echo "  - 装好后用 'crontab -l' 确认那一行带着 LING_COMPOSE_PROJECT=${PROJECT}" \
  "与同一组 LING_COMPOSE_FILE/LING_COMPOSE_OVERRIDE/LING_COMPOSE_ENV_FILE，"
echo "    并在 48 小时内检查 GET /v1/health/ops 的 backup_age_hours 是否正常"
echo "    （也要 tail 一下 $BACKUP_DIR/backup.log）。镜像模式下还要在最新"
echo "    manifest-*.txt 里看到 keys_env_set=true（定时备份与手动 ling backup"
echo "    必须给出同样的结论）。"
