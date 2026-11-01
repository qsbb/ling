#!/usr/bin/env bash
# 数据库一致性备份（PostgreSQL + app-data 卷里的根密钥与媒体字节）。
#
# 用法：deploy/backup/backup.sh OUT_DIR [--prune-keep N] [--prune-days D]
#                                     [--dry-run] [--force-unlock]
#
#   位置参数只有一个 OUT_DIR。其余全是**可选**开关，默认一个都不启用：
#   - --prune-keep N   成功备份后只保留最近 N 份「dump+manifest+tombstones+media
#                      齐全且 manifest 可解析」的组合，更早的删掉。**不给就永不删除**
#                      （默认行为与历史版本完全一致）。
#                      **口径：N 份含本次新备份**（跑完目录里最多留 N 份完整组合），
#                      每日备份时历史回看 N−1 天；`--prune-keep 1` 会删光全部历史
#                      **完整组合**（不合格的组永远不删，见下）。
#                      零撤回时 tombstones-*.txt 是 **0 字节**，这是合法产物：
#                      文件存在 + tombstones_state=ok 即算齐全；只有文件缺失或
#                      state=FAILED 才跳过。
#                      已知限制：不清理直接模式的 version-*.txt；media_state=partial
#                      的组也算"四件齐全"，会占用保留名额（可能挤掉更早的干净备份）；
#                      不合格组永远留在目录里，实际份数可能多于 N。
#   - --prune-days D   额外的年龄下限：只删 manifest 修改时间早于 D 天的组
#                      （必须与 --prune-keep 一起给）。按 `find -mtime +D` 的语义
#                      按整 24h 向下取整：+D 实际匹配年龄 ≥ D+1 天（D=1 是"至少
#                      两天前"），D 到 D+1 天之间这段不算。
#   - --dry-run        只打印计划，不写盘、不建目录、不留锁、不删任何文件。
#   - --force-unlock   强制清掉同目录上遗留/占用的备份锁（见下方退出码 6）。
#                      **只对已死进程的残留锁用**：对活着的实例强开会让两个进程
#                      同时写同一批产物，后启动的那份可能因媒体归档不完整 exit 5。
#
# 退出码（**这张表就是契约，任何改动必须同步**）：
#   0  流程正常结束（**不保证产物齐全/可恢复**：可能带 tombstones_state=FAILED、
#      media=absent、keys_root=absent、media_state=partial，必须看 manifest；
#      轮转只是告警时也算 0）
#   1  未分类的意外失败：`set -e` **透传底层命令的状态**（常见 1，例如直接模式
#      pg_dump 失败；脚本没有显式 `exit 1`）。命令缺失是 127；psql 自身 exit 2
#      会与"用法/配置错误"同码；其余状态不保证落在 0–7
#   2  用法或配置错误（缺 OUT_DIR / 未知参数 / --prune-* 取值非法 /
#      缺 LING_DATABASE_URL / compose 模式缺 docker）
#   3  pg_dump 失败或导出为空（仅 compose 模式显式映射；直接模式下 pg_dump 的
#      非零退出码由 `set -e` 原样透传，**不保证是 3**）
#   4  容器内根密钥读取失败**或完整性自校验失败**（容器侧 sha256 与归档字节
#      不一致 / 容器没回传哈希；无法确认密钥状态，本次备份作废）
#   5  媒体归档**致命**失败（compose：tar rc>=2 或 tar -tf 读不出目录；
#      直接模式：tar 非零）。compose 的 tar rc=1 是 media_state=partial，
#      **不算 5**，仍 exit 0
#   6  **备份锁被占用**（另一个实例正在同一 OUT_DIR 上运行；或残留锁接管失败）
#   7  缺少 sha256 工具（sha256sum / shasum / openssl 都没有；在任何磁盘动作之前退出）
#
# 注：0–7 是脚本显式/常见的状态，**不是封闭集合**；`set -e` 会透传底层命令的
# 状态（命令缺失 127、psql 自身 exit 2 等），不能假设只会出现这 8 个值。
#
# 约束（设计文档 §10）：数据库备份与媒体清单要在同一恢复点验证；
# 恢复前先核对撤回墓碑，防止旧备份让已删除内容重新可见。
#
# **为什么 media 与 root.key 必须在备份里**：默认 Compose 部署里这两样都在
# **命名卷** `app-data`（挂到 `/data`）里，宿主机上不能直接 `cp`：
#   - `/data/root.key`：数据库里的 provider 凭据是 AES-GCM 加密的，
#     `LING_ROOT_KEY` 为空时由 `security.py` 首次自动生成到该文件；
#     根密钥丢了，库里的凭据**全部解不开**（现象是"机器人突然连不上模型"，
#     很难第一时间想到是密钥问题）；
#   - `/data/media`：`media_evidence.media_ref`（形如 `file:/data/media/…`）
#     指向的实体文件，不备就是"库回来了、图和语音全丢"，引用全部悬空。
# 只备 DB 的备份**看起来**成功（`pg_restore --list` 也过得去），所以这里必须
# 把它们显式纳入，并在 manifest 里写清状态与哈希（未生成时写 absent，不静默跳过）。
#
# 取数方式：`docker compose exec -T api` 在容器内 `tar`/`cat`，stdout 直接落到
# 宿主机备份目录——命名卷在宿主不可直接访问；api 根文件系统只读（不在容器内
# 写临时文件），大文件也不经过内存。任何一步失败都非零退出并清理半成品。
# **`-T` 是承重的**（真机实测）：少了它且宿主有 TTY 时 docker 会分配 pty，TTY 层
# 给字节流加 NUL/CRLF 而**退出码仍是 0**。媒体侧会因 `tar -tf` 读不出目录而响亮
# 失败（exit 5），根密钥侧却会静默写坏——所以根密钥那条自带"容器侧哈希 vs 归档
# 字节哈希"的自校验（见下方根密钥段），不再依赖"记得写 -T"。
set -euo pipefail

USAGE="用法: $0 OUT_DIR [--prune-keep N] [--prune-days D] [--dry-run] [--force-unlock]"

# 参数错误一律走这里：exit 2 且**在任何磁盘/容器动作之前**退出（fail closed）。
usage_error() {
  echo "[backup] $1" >&2
  echo "${USAGE}" >&2
  exit 2
}

# `--prune-keep` / `--prune-days` 的取值校验：只接受正整数。
# 0 / 负数 / 非数字 / 缺参数 / 前导 0 之外的怪值 一律 fail closed（exit 2）——
# "轮转"是唯一会删数据的动作，参数不合法时绝不能猜一个值继续跑。
COUNT_OK=""
validate_count() {
  local value="$1" name="$2"
  case "$value" in
    ''|*[!0-9]*)
      usage_error "${name} 需要一个正整数（拿到: ${value:-<空>}）"
      ;;
  esac
  # 上限只是防呆（`--prune-keep 999999999999` 之类），不影响正常取值。
  if [[ ${#value} -gt 6 ]]; then
    usage_error "${name} 过大（拿到: ${value}）"
  fi
  # 去掉前导 0：`08` 之类在 bash 算术里会被当八进制（非法），统一按十进制处理。
  while [[ ${#value} -gt 1 && "$value" == 0* ]]; do
    value="${value#0}"
  done
  if [[ "$value" == "0" ]]; then
    usage_error "${name} 必须 >= 1（拿到: $1）"
  fi
  COUNT_OK="$value"
}

OUT_DIR=""
PRUNE_KEEP=""
PRUNE_DAYS=""
DRY_RUN=0
FORCE_UNLOCK=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prune-keep)
      [[ $# -ge 2 ]] || usage_error "--prune-keep 缺参数（需要正整数，例如 --prune-keep 14）"
      validate_count "$2" "--prune-keep"
      PRUNE_KEEP="${COUNT_OK}"
      shift 2
      ;;
    --prune-keep=*)
      validate_count "${1#--prune-keep=}" "--prune-keep"
      PRUNE_KEEP="${COUNT_OK}"
      shift
      ;;
    --prune-days)
      [[ $# -ge 2 ]] || usage_error "--prune-days 缺参数（需要正整数天数，例如 --prune-days 30）"
      validate_count "$2" "--prune-days"
      PRUNE_DAYS="${COUNT_OK}"
      shift 2
      ;;
    --prune-days=*)
      validate_count "${1#--prune-days=}" "--prune-days"
      PRUNE_DAYS="${COUNT_OK}"
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --force-unlock)
      FORCE_UNLOCK=1
      shift
      ;;
    -h|--help)
      printf '%s\n' "${USAGE}"
      exit 0
      ;;
    --)
      shift
      while [[ $# -gt 0 ]]; do
        [[ -z "$OUT_DIR" ]] || usage_error "多余的参数 $1（只接受一个 OUT_DIR）"
        OUT_DIR="$1"
        shift
      done
      ;;
    -*)
      usage_error "未知参数 $1"
      ;;
    *)
      [[ -z "$OUT_DIR" ]] || usage_error "多余的参数 $1（只接受一个 OUT_DIR）"
      OUT_DIR="$1"
      shift
      ;;
  esac
done

if [[ -z "$OUT_DIR" ]]; then
  usage_error "缺少 OUT_DIR"
fi

# `--prune-days` 单独给没有意义：没有 keep 上限时"永不删除"是硬性默认值，
# 静默忽略这个开关比报错更危险（运维会以为自己已经限制住空间）。
if [[ -n "$PRUNE_DAYS" && -z "$PRUNE_KEEP" ]]; then
  usage_error "--prune-days 必须与 --prune-keep 一起用（默认永不删除，单独给会被静默忽略）"
fi

STAMP="$(date +%Y%m%d-%H%M%S)"

# 半成品清理：成功路径会把临时文件 mv 成正式产物，这条 trap 兜底两件事：
#   1) 删掉 .part / .err 临时文件；
#   2) **失败时**删掉本次已经产出的片段（dump / 墓碑 / 媒体 / 根密钥），不留一个
#      "看起来像备份"的残目录——cron 每天往同一个 OUT_DIR 写，残片会一直累积，
#      而 dump 里是业务数据。manifest 只在最后一步写，所以"有 manifest"才代表完整。
#
# **根密钥是这条规则里唯一不能删的东西**：`keys/root.key` 没有日期戳，而 cron
# 每天往同一个 OUT_DIR 写（见 install-cron.sh 的 crontab 行），所以这个路径同时
# 就是"前一天那份成功备份的密钥归档"。于是在"本次已归档密钥、随后媒体阶段失败"
# 时删掉它，等于让昨天的 `manifest-*.txt` 立刻变成悬空引用（`keys_root_file=
# keys/root.key` 指向不存在的文件），而那份密钥**没有第二份副本**——这是不可逆
# 的能力损失。两者的风险不对称：残留的 dump/墓碑没有 manifest 就不会被
# `restore_drill.sh` 当成有效备份，而删掉的密钥永远回不来。
# 因此失败清理按三条分支走（原则：**宁可留垃圾，也绝不删历史密钥**）：
#   - 本次归档、原本没有（`KEYS_PREEXISTED=0`）→ 删，这是本次新建的产物；
#   - 本次归档、原本就有（`KEYS_PREEXISTED=1`）→ 用归档前存下的
#     `keys/.root.key.prev.$$` 把**原字节**放回原位；连暂存副本都没了/移不动，
#     就保留当前文件并告警，**绝不 rm**；
#   - 本次没走到归档（在密钥步骤之前就失败）→ 一根汗毛都不动。
BACKUP_OK=0
KEYS_ARCHIVED=0
KEYS_PREEXISTED=0
KEYS_PREV=""

# ---------------------------------------------------------------- 并发锁
#
# 同目录并发跑两份备份会互相覆盖同一批固定名字的产物（`keys/root.key`、`.part`、
# 甚至同秒的 STAMP），是两个进程一起写盘的经典事故。这里用**可移植的 mkdir 锁**：
# `mkdir` 是原子的，成功即持锁，失败即"别人在跑"。
# 为什么不是 `flock(1)`：macOS 根本没有这个命令；脚本必须 bash 3.2 + 默认 macOS
# 工具链可用（不依赖 flock / jq / gdate）。
# 锁目录里写 PID 与启动时间，供"崩溃残留"时判断持有者是否还在（`kill -0`）。
LOCK_DIR="${OUT_DIR}/.backup.lock"
LOCK_ACQUIRED=0

lock_pid() {
  local pid=""
  if [[ -f "${LOCK_DIR}/pid" ]]; then
    pid="$(<"${LOCK_DIR}/pid")"
  fi
  case "$pid" in
    ''|*[!0-9]*) printf '%s' "" ; return 0 ;;
  esac
  printf '%s' "$pid"
}

lock_started() {
  local started=""
  if [[ -f "${LOCK_DIR}/started_at" ]]; then
    started="$(<"${LOCK_DIR}/started_at")"
    started="${started//$'\n'/ }"
  fi
  printf '%s' "${started:-未知}"
}

lock_write_holder() {
  printf '%s\n' "$$" > "${LOCK_DIR}/pid"
  printf '%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" > "${LOCK_DIR}/started_at"
}

# 拿不到锁就 exit 6（**专用退出码**）：调用方（cron / 更新执行器）据此区分
# "另一个实例正在跑"和"备份本身失败"——前者重跑即可，后者需要人看。
acquire_lock() {
  local pid
  if [[ "${FORCE_UNLOCK}" == "1" && -d "${LOCK_DIR}" ]]; then
    echo "[backup] --force-unlock：强制清理备份锁 ${LOCK_DIR}" \
      "（原持有者 pid=$(lock_pid) 启动时间=$(lock_started)）" >&2
    rm -rf "${LOCK_DIR}" 2>/dev/null || true
  fi
  if mkdir "${LOCK_DIR}" 2>/dev/null; then
    lock_write_holder
    LOCK_ACQUIRED=1
    return 0
  fi
  pid="$(lock_pid)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "[backup] 同目录已有备份实例在运行：pid=${pid}，启动时间=$(lock_started)" >&2
    echo "[backup] 备份锁：${LOCK_DIR}" >&2
    echo "[backup] 确认那个进程已经死掉/是残留锁时，用 --force-unlock 重跑" >&2
    exit 6
  fi
  # 残留锁（PID 不存在或没写进来）：告警后接管。`kill -0` 对"别人的进程"也会
  # 因 EPERM 返回非零，那种情况会被当成残留而接管——所以 pid/启动时间都留在
  # 锁目录里，接管时打印出来，出问题能对得上。
  echo "[backup] 警告：发现残留备份锁 ${LOCK_DIR}" \
    "（pid=${pid:-未知} 启动时间=$(lock_started) 已不存在），接管并继续" >&2
  rm -rf "${LOCK_DIR}" 2>/dev/null || true
  if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
    echo "[backup] 接管残留锁失败（${LOCK_DIR} 仍被占用），请人工检查后重跑" >&2
    exit 6
  fi
  lock_write_holder
  LOCK_ACQUIRED=1
}

# 清理里**每条命令都带 `|| true`**：EXIT trap 里任何一条失败（磁盘只读、文件被别人
# 挪走……）都会被 `set -e` 当成"trap 自己失败"，把脚本原本精心区分的退出码
# （3 pg_dump / 4 根密钥 / 5 媒体）改写成 1——更糟的是成功路径上会让一次**成功**
# 的备份变成非零退出。清理只做"尽力而为"，绝不改写结论。
cleanup_partial() {
  # 没拿到锁 = 本次一步都没开始（拿锁在产出任何文件之前）。此时**什么都不能删**：
  # 同一秒启动的两个实例 STAMP 完全相同，被拒的那个若照常清理，会把正在跑的那份
  # 备份的 dump/媒体删掉——"并发保护"反而变成破坏力。
  if [[ "${LOCK_ACQUIRED}" != "1" ]]; then
    return 0
  fi
  rm -f "${OUT_DIR}/.root.key.part.$$" "${OUT_DIR}/.root.key.sha.$$" \
    "${OUT_DIR}/.media-${STAMP}.tar.part" \
    "${OUT_DIR}/.media-${STAMP}.err" 2>/dev/null || true
  if [[ "${BACKUP_OK}" == "1" ]]; then
    if [[ -n "${KEYS_PREV}" ]]; then
      rm -f "${KEYS_PREV}" 2>/dev/null || true
    fi
    return 0
  fi
  rm -f "${OUT_DIR}/ling-${STAMP}.dump" "${OUT_DIR}/tombstones-${STAMP}.txt" \
    "${OUT_DIR}/media-${STAMP}.tar" "${OUT_DIR}/version-${STAMP}.txt" \
    2>/dev/null || true
  if [[ "${KEYS_ARCHIVED}" == "1" && "${KEYS_PREEXISTED}" == "1" ]]; then
    if [[ -n "${KEYS_PREV}" && -f "${KEYS_PREV}" ]] \
      && mv -f "${KEYS_PREV}" "${OUT_DIR}/keys/root.key"; then
      chmod 600 "${OUT_DIR}/keys/root.key" 2>/dev/null || true
      echo "[backup] 已把归档前的 keys/root.key 原样恢复（本次归档曾覆盖它）" >&2
    else
      echo "[backup] 警告：本次归档覆盖过 keys/root.key，但暂存副本" \
        "${KEYS_PREV:-（未设置）} 已不存在或移不动；为避免误删历史密钥，" \
        "保留当前文件，请人工确认它的来源" >&2
    fi
  elif [[ "${KEYS_ARCHIVED}" == "1" ]]; then
    # 原本没有、本次新建的：删掉，不留半成品。
    rm -f "${OUT_DIR}/keys/root.key" 2>/dev/null || true
    rmdir "${OUT_DIR}/keys" 2>/dev/null || true
  elif [[ -n "${KEYS_PREV}" ]]; then
    # 还没走到归档就失败了：目标文件根本没被动过，暂存副本是多余的一份拷贝。
    rm -f "${KEYS_PREV}" 2>/dev/null || true
  fi
  echo "[backup] 失败：已清理本次未完成的产物（${OUT_DIR} 里不会留下残缺备份）" >&2
}

# EXIT trap 同时负责释放并发锁：**成功路径也绝不能留锁**否则下一次 cron 会
# 永久被 exit 6 拒掉。释放放在半成品清理之后，且同样 `|| true`——
# 清理失败可以忽略，但绝不能改写脚本原本的退出码。
release_lock() {
  if [[ "${LOCK_ACQUIRED}" == "1" ]]; then
    rm -rf "${LOCK_DIR}" 2>/dev/null || true
    LOCK_ACQUIRED=0
  fi
}

on_exit() {
  cleanup_partial || true
  release_lock || true
}
trap on_exit EXIT

# 归档根密钥前先给**已有的** `keys/root.key` 留一份原字节副本（创建时 group/other
# 位即为 0，常见文件系统上是 0600，见下）。
# 失败时 `cleanup_partial` 用它把历史备份的密钥放回原位，成功时由 trap 删掉。
# 为什么是"复制"而不是"移走"：复制不会出现"文件暂时不存在"的窗口——进程被
# kill -9 打断时，`keys/root.key` 仍在原地（最坏只是多一个 `.root.key.prev.<pid>`
# 垃圾）。这里只复制**本目录内**已经落盘的那份副本，用重定向完成，不再调外部
# `cp`（命名卷里的密钥没法靠宿主 `cp` 取，见文件头的取数方式说明）。
#
# **创建瞬间就必须"同机其他用户读不到"**：重定向创建文件走 `0666 & ~umask`，默认
# umask 022 会先落一个 **0644**，随后的 `chmod 600` 之间就有一个"同机其他用户可读
# 根密钥字节"的窗口（cron 路径的备份目录本来就是 0755）。所以把重定向放进
# `umask 077` 的子 shell **内**执行——重定向在子 shell 里做，文件一出生 group/other
# 位就是 0（子 shell 内的 `cat` 失败仍会让整个子 shell 非零退出，fail closed 语义
# 不变）。
#
# 这里要保证的不变量是 **`mode & 0o077 == 0`**（group / other 位全为 0），**不是**
# "恰好 0600"。`0600` 只是"常见 umask + 常见文件系统"下的**结果**：在 ext4/xfs 上
# `0666 & ~077` 就是 0600；而 NAS 的 btrfs + Synology `trimacl` 上，同一段代码建
# 出来的文件是 **0700**（owner 多了一个 x 位，group/other 仍是 0）——同样安全，
# 因为真正要挡的是"同机其他用户"，不是 owner 自己的 x 位。所以注释、测试断言都
# 以 `mode & 0o077 == 0` 为准；只有显式 `chmod 600` 之后的**正式产物**才断言精确
# 0600（那是脚本自己设的，与 umask/文件系统无关）。
stash_existing_keys() {
  KEYS_PREV=""
  KEYS_PREEXISTED=0
  if [[ ! -f "${OUT_DIR}/keys/root.key" ]]; then
    return 0
  fi
  KEYS_PREV="${OUT_DIR}/keys/.root.key.prev.$$"
  ( umask 077; cat -- "${OUT_DIR}/keys/root.key" > "${KEYS_PREV}" )
  chmod 600 "${KEYS_PREV}"
  KEYS_PREEXISTED=1
}

# 产物哈希：manifest 要能证明"恢复时拿到的就是备份时写下的字节"。
# 只记录哈希，**绝不**记录 DSN、口令或 root.key 内容。
if command -v sha256sum >/dev/null 2>&1; then
  SHA256_TOOL=sha256sum
elif command -v shasum >/dev/null 2>&1; then
  SHA256_TOOL=shasum
elif command -v openssl >/dev/null 2>&1; then
  SHA256_TOOL=openssl
else
  echo "[backup] 需要 sha256sum / shasum / openssl 之一来记录产物哈希；都没有就拒绝声称备份完整" >&2
  # 7（不是 6）：6 已经留给"并发锁被占用"，退出码表见文件头。
  exit 7
fi

sha256_of() {
  case "$SHA256_TOOL" in
    sha256sum) sha256sum "$1" | awk '{print $1}' ;;
    shasum) shasum -a 256 "$1" | awk '{print $1}' ;;
    *) openssl dgst -sha256 "$1" | awk '{print $NF}' ;;
  esac
}

# 文件不存在时写 `-`（例如墓碑清单导出失败已被删掉），而不是留空值。
sha256_or_dash() {
  if [[ -f "$1" ]]; then
    sha256_of "$1"
  else
    printf '%s\n' '-'
  fi
}

# 根密钥的**实际生效来源**：`src/ling/infra/security.py` 里 `LING_ROOT_KEY` 优先，
# 文件其次（文件不存在才自动生成）。
# 因此 manifest 的 `keys_root` / `keys_root_sha256` 只回答"有没有把文件归档下来"，
# **不回答**"应用用的是哪一把"。这里额外记一个 `keys_env_set`，并在"文件也在、
# env 也非空"时打一行警告——否则很容易被读成"只恢复 keys/root.key 就够了"，
# 而那份文件未必是生效的那把密钥。
if [[ -n "${LING_ROOT_KEY:-}" ]]; then
  KEYS_ENV_SET=true
else
  KEYS_ENV_SET=false
fi

# ---------------------------------------------------------------- 保留/轮转策略
#
# 媒体是全量副本（`media-*.tar` 每天一份），不轮转就会线性撑满磁盘。但"自动删
# 备份"也是唯一会毁数据的操作，所以这里的规则是**默认完全不动**：
#   - 没有 `--prune-keep N` → 一行都不删（与历史行为逐字节一致）；
#   - 显式开启后，只有"四件齐全 + manifest 可解析"的组合才可能被删；
#   - 缺一件、缺 manifest、manifest 读不懂（旧版格式）、manifest 与文件对不上
#     → 整组跳过并告警（**宁可留垃圾，也不删看不懂的东西**）；
#   - `keys/root.key` **永不**在删除范围内：它没有日期戳，是"当前这把密钥"的
#     唯一副本，也是所有历史 manifest 里 `keys_root_file=` 指向的那一份
#     （见 cleanup_partial 上面的长注释）。
#
# 已知限制（如实保留，不美化）：
#   - 删除白名单只含 dump/tombstones/media/manifest，直接模式的 `version-*.txt`
#     **不会**被轮转清掉（组被删后它可能残留，也不会有孤儿告警）；
#   - `media_state=partial` 的组同样算"四件齐全"，会占用保留名额，可能挤掉更早的
#     干净备份（partial 仍可恢复，但恢复前要人工核对 media_ref）。
PRUNE_STAMPS=()

# 从 manifest 里取 `key=value` 的值；键不存在时返回 1（调用方据此判"不可解析"）。
# 用纯 bash 读，不引入 sed/awk 的转义陷阱（路径里可能有空格/unicode）。
manifest_field() {
  local file="$1" want="$2" line
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      "${want}="*) printf '%s' "${line#*=}"; return 0 ;;
    esac
  done < "$file"
  return 1
}

# 一组备份是否"四件齐全且 manifest 可解析"。不合格时把**原因**打到 stdout。
group_problem() {
  local stamp="$1" manifest dump tombs tombs_state media keys_root
  manifest="${OUT_DIR}/manifest-${stamp}.txt"
  if [[ ! -f "$manifest" ]]; then
    printf '%s' "缺 manifest"
    return 1
  fi
  if ! dump="$(manifest_field "$manifest" dump)" || [[ -z "$dump" ]]; then
    printf '%s' "manifest 里没有可用的 dump= 字段"
    return 1
  fi
  case "$dump" in
    */*|.|..)
      printf '%s' "manifest 的 dump= 不是纯文件名（${dump}）"
      return 1
      ;;
  esac
  if [[ ! -s "${OUT_DIR}/${dump}" ]]; then
    printf '%s' "manifest 指向的 ${dump} 不存在或为空"
    return 1
  fi
  if ! tombs_state="$(manifest_field "$manifest" tombstones_state)"; then
    printf '%s' "manifest 缺 tombstones_state 字段"
    return 1
  fi
  if [[ "$tombs_state" != "ok" ]]; then
    printf '%s' "墓碑导出状态是 ${tombs_state}（这份备份本来就不可用于恢复）"
    return 1
  fi
  if ! tombs="$(manifest_field "$manifest" tombstones)" || [[ -z "$tombs" ]]; then
    printf '%s' "manifest 里没有可用的 tombstones= 字段"
    return 1
  fi
  case "$tombs" in
    */*|.|..)
      printf '%s' "manifest 的 tombstones= 不是纯文件名（${tombs}）"
      return 1
      ;;
  esac
  # 墓碑清单**允许是 0 字节**：没有任何撤回记录是最常见的部署形态，此时 `psql`
  # 查询成功但输出为空，manifest 里 `tombstones_state=ok`。旧实现用 `-s` 判，
  # 把这种合法产物当成"缺墓碑"，零撤回部署上 `--prune-keep` 会完全空转。
  # 这里把"文件存在 + 状态 ok"与"内容非空"分开：只有**文件缺失**才判不完整
  # （状态不是 ok 已在上面的 tombstone_state 检查里跳过）。
  # **不要**把这条放宽推广到 dump/media：0 字节的 dump 或 media 仍然是坏的，
  # 它们下面的 `-s` 必须保留。
  if [[ ! -f "${OUT_DIR}/${tombs}" ]]; then
    printf '%s' "manifest 指向的 ${tombs} 不存在"
    return 1
  fi
  if ! media="$(manifest_field "$manifest" media)"; then
    printf '%s' "manifest 缺 media= 字段（旧版备份，无法确认媒体是否齐全）"
    return 1
  fi
  if [[ -z "$media" ]]; then
    printf '%s' "manifest 的 media= 是空值"
    return 1
  fi
  if [[ "$media" != "absent" ]]; then
    case "$media" in
      */*|.|..)
        printf '%s' "manifest 的 media= 不是纯文件名（${media}）"
        return 1
        ;;
    esac
    if [[ ! -s "${OUT_DIR}/${media}" ]]; then
      printf '%s' "manifest 指向的 ${media} 不存在或为空"
      return 1
    fi
  fi
  # 额外的一条安全网：manifest 声称归档过密钥，但 keys/root.key 已经不在了，
  # 说明这组备份恢复不出凭据——不删它（等人工判断，别把仅存的线索清掉）。
  if keys_root="$(manifest_field "$manifest" keys_root)" && [[ "$keys_root" == "present" ]]; then
    if [[ ! -f "${OUT_DIR}/keys/root.key" ]]; then
      printf '%s' "manifest 说 keys_root=present，但 keys/root.key 不存在"
      return 1
    fi
  fi
  return 0
}

# 孤儿产物（没有对应 manifest 的 dump/墓碑/媒体）：**一律不动**，只告警。
prune_warn_orphans() {
  local path stamp
  for path in "${OUT_DIR}"/ling-*.dump "${OUT_DIR}"/tombstones-*.txt \
    "${OUT_DIR}"/media-*.tar; do
    [[ -f "$path" ]] || continue
    stamp="${path##*/}"
    stamp="${stamp#ling-}"
    stamp="${stamp#tombstones-}"
    stamp="${stamp#media-}"
    stamp="${stamp%.dump}"
    stamp="${stamp%.txt}"
    stamp="${stamp%.tar}"
    if [[ ! -f "${OUT_DIR}/manifest-${stamp}.txt" ]]; then
      echo "[backup] 轮转告警：孤儿产物 ${path##*/}（没有对应 manifest），不动它" >&2
    fi
  done
}

# 算出计划：把"完整组合"按 stamp 倒序排，keep 之外的（再叠加 --prune-days 的
# 年龄下限）进 PRUNE_STAMPS。只算不删。
prune_plan() {
  local manifest stamp reason old_manifests
  local rank=0
  local keep="${PRUNE_KEEP}"
  local -a complete=() sorted=() selected=()
  PRUNE_STAMPS=()

  for manifest in "${OUT_DIR}"/manifest-*.txt; do
    [[ -f "$manifest" ]] || continue
    stamp="${manifest##*/}"
    stamp="${stamp#manifest-}"
    stamp="${stamp%.txt}"
    case "$stamp" in
      ''|*[!0-9-]*)
        echo "[backup] 轮转告警：${manifest##*/} 的文件名不是时间戳格式，不动它" >&2
        continue
        ;;
    esac
    if reason="$(group_problem "$stamp")"; then
      complete[${#complete[@]}]="$stamp"
    else
      echo "[backup] 轮转告警：跳过组 ${stamp}（${reason}），不动它的任何文件" >&2
    fi
  done

  if [[ ${#complete[@]} -eq 0 ]]; then
    echo "[backup] 轮转：没有可识别的完整备份组合，未做任何删除" >&2
    return 0
  fi

  # stamp 形如 YYYYMMDD-HHMMSS，字典序即时间序；倒序 = 最新在前。
  sorted=($(printf '%s\n' "${complete[@]}" | sort -r))

  if [[ ${#sorted[@]} -le $keep ]]; then
    echo "[backup] 轮转：完整组合 ${#sorted[@]} 份 <= 保留 ${keep} 份，不删除任何东西"
    return 0
  fi

  rank=0
  for stamp in "${sorted[@]}"; do
    rank=$((rank + 1))
    if [[ $rank -le $keep ]]; then
      continue
    fi
    selected[${#selected[@]}]="$stamp"
  done

  # 年龄下限：只删 manifest 的修改时间早于 D 天的组（`find -mtime +D` 可移植，
  # 不依赖 `date -d` / `date -v` / `gdate`）。
  if [[ -n "$PRUNE_DAYS" ]]; then
    old_manifests="$(find "$OUT_DIR" -maxdepth 1 -type f -name 'manifest-*.txt' \
      -mtime "+${PRUNE_DAYS}" 2>/dev/null || true)"
    PRUNE_STAMPS=()
    for stamp in "${selected[@]}"; do
      case $'\n'"${old_manifests}"$'\n' in
        *$'\n'"${OUT_DIR}/manifest-${stamp}.txt"$'\n'*)
          PRUNE_STAMPS[${#PRUNE_STAMPS[@]}]="$stamp"
          ;;
      esac
    done
    if [[ ${#PRUNE_STAMPS[@]} -eq 0 ]]; then
      echo "[backup] 轮转：超出保留份数的 ${#selected[@]} 组里没有早于 ${PRUNE_DAYS} 天的（按 manifest 时间），不删除"
      return 0
    fi
  else
    PRUNE_STAMPS=()
    for stamp in "${selected[@]}"; do
      PRUNE_STAMPS[${#PRUNE_STAMPS[@]}]="$stamp"
    done
  fi

  # 输出按时间正序（旧 → 新），日志好读、用例可断言。
  sorted=($(printf '%s\n' "${PRUNE_STAMPS[@]}" | sort))
  PRUNE_STAMPS=()
  for stamp in "${sorted[@]}"; do
    PRUNE_STAMPS[${#PRUNE_STAMPS[@]}]="$stamp"
  done

  echo "[backup] 轮转：完整组合 ${#complete[@]} 份，保留最近 ${keep} 份，计划删除 ${#PRUNE_STAMPS[@]} 份：${PRUNE_STAMPS[*]}"
  return 0
}

# 删一组：先删大的数据文件，**manifest 最后删**（这样中途被打断时这组立刻变成
# "不完整"，下次轮转不会再碰它，也不会被 restore_drill 当成有效备份）。
# 删不掉（目录只读等）时保留 manifest 并告警，绝不假装删掉了。
prune_delete_group() {
  local stamp="$1" manifest dump tombs media
  manifest="${OUT_DIR}/manifest-${stamp}.txt"
  if ! dump="$(manifest_field "$manifest" dump)" || [[ -z "$dump" ]]; then
    echo "[backup] 轮转告警：组 ${stamp} 的 manifest 在删除过程中变得不可解析，跳过" >&2
    return 1
  fi
  if ! tombs="$(manifest_field "$manifest" tombstones)" || [[ -z "$tombs" ]]; then
    echo "[backup] 轮转告警：组 ${stamp} 的 manifest 缺 tombstones，跳过" >&2
    return 1
  fi
  if ! media="$(manifest_field "$manifest" media)" || [[ -z "$media" ]]; then
    echo "[backup] 轮转告警：组 ${stamp} 的 manifest 缺 media，跳过" >&2
    return 1
  fi
  case "$dump" in */*|.|..) echo "[backup] 轮转告警：组 ${stamp} 的 dump 名不合法，跳过" >&2; return 1 ;; esac
  case "$tombs" in */*|.|..) echo "[backup] 轮转告警：组 ${stamp} 的 tombstones 名不合法，跳过" >&2; return 1 ;; esac
  case "$media" in
    absent) ;;
    */*|.|..) echo "[backup] 轮转告警：组 ${stamp} 的 media 名不合法，跳过" >&2; return 1 ;;
  esac

  rm -f -- "${OUT_DIR}/${dump}" "${OUT_DIR}/${tombs}" 2>/dev/null || true
  if [[ "$media" != "absent" ]]; then
    rm -f -- "${OUT_DIR}/${media}" 2>/dev/null || true
  fi
  if [[ -e "${OUT_DIR}/${dump}" || -e "${OUT_DIR}/${tombs}" ]]; then
    echo "[backup] 轮转告警：组 ${stamp} 的数据文件删不掉（目录不可写？），保留 manifest 等人工处理" >&2
    return 1
  fi
  if [[ "$media" != "absent" && -e "${OUT_DIR}/${media}" ]]; then
    echo "[backup] 轮转告警：组 ${stamp} 的 ${media} 删不掉，保留 manifest 等人工处理" >&2
    return 1
  fi
  rm -f -- "$manifest" 2>/dev/null || true
  if [[ -e "$manifest" ]]; then
    echo "[backup] 轮转告警：组 ${stamp} 的 manifest 删不掉（目录不可写？）" >&2
    return 1
  fi
  return 0
}

# 轮转入口：`--prune-keep` 没给就什么都不做（返回 0，绝不删）。
# 这里的所有失败都只告警 —— 备份本身已经成功，轮转失败不该把它判成失败。
run_prune() {
  local stamp deleted=0
  if [[ -z "$PRUNE_KEEP" ]]; then
    return 0
  fi
  prune_warn_orphans
  prune_plan
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[backup] dry-run：以上为轮转计划，未删除任何文件"
    return 0
  fi
  if [[ ${#PRUNE_STAMPS[@]} -eq 0 ]]; then
    return 0
  fi
  for stamp in "${PRUNE_STAMPS[@]}"; do
    if prune_delete_group "$stamp"; then
      echo "[backup] 轮转：已删除组 ${stamp}（dump/tombstones/media/manifest；keys/root.key 不动）"
      deleted=$((deleted + 1))
    fi
  done
  echo "[backup] 轮转：本次删除 ${deleted} 组，保留 ${PRUNE_KEEP} 份"
  return 0
}

# ---------------------------------------------------------------- 并发锁与目录
#
# 顺序很关键：**先建目录 → 立刻拿锁 → 再动容器/数据库**。拿不到锁就直接 exit 6，
# 不产生任何半成品；拿锁后的一切（含 exit 6 以外的失败）都由 EXIT trap 释放锁。
if [[ "$DRY_RUN" == "1" ]]; then
  # dry-run 完全不碰盘：不建目录、不建锁、不跑 docker、不写 dump/manifest、
  # 不删历史产物。目录不存在时按"首次运行"报告。
  echo "[backup] dry-run：只打印计划，不备份、不轮转、不建目录、不留锁"
  echo "[backup] dry-run：目标目录 ${OUT_DIR}"
  if [[ ! -d "$OUT_DIR" ]]; then
    echo "[backup] dry-run：目录还不存在（首次运行），没有历史产物可轮转"
    exit 0
  fi
  if [[ -d "${LOCK_DIR}" ]]; then
    echo "[backup] dry-run：警告：目录上已有备份锁（pid=$(lock_pid) 启动时间=$(lock_started)）；" \
      "真正运行会以 exit 6 中止（除非 --force-unlock）" >&2
  fi
  if [[ -n "$PRUNE_KEEP" ]]; then
    run_prune
  else
    echo "[backup] dry-run：轮转未启用（默认永不删除；要轮转请加 --prune-keep N）"
  fi
  exit 0
fi

mkdir -p "${OUT_DIR}"
acquire_lock

# 两种运行方式：
#   1) 直接模式：本机/容器内能连到数据库，用 LING_DATABASE_URL
#   2) compose 模式：宿主机上没有 DSN 也不该暴露端口，
#      用 `docker compose exec` 进 db 容器执行（LING_BACKUP_VIA_COMPOSE=1）
#
# 后者是默认部署的真实情况：cron 跑在宿主机，数据库只在 compose 内网。
# 不给这条路的话，"已安装每日备份"实际上一份都备不出来。
VIA_COMPOSE="${LING_BACKUP_VIA_COMPOSE:-0}"
COMPOSE_FILE="${LING_COMPOSE_FILE:-deploy/compose.yaml}"
# 项目名必须显式给。两条独立理由：
#   1) `docker compose -f 路径` 会按**文件所在目录名**推导项目名，从 cron 里跑时
#      目录往往与真实项目名不同（实测：项目叫 ling-verify，目录叫 deploy）；
#   2) **Compose v5 的 `exec` 根本不看 `-f`**：它按 project + service 的 label
#      找正在运行的容器（真机实测：`-f /nonexistent.yaml` 仍 rc=0 且命令照常
#      执行；`-f` 指向插值必失败的 compose 也 rc=0）。`-f` 在这里只起"留个自证
#      路径"的作用。
#
#      **项目名（`-p`）是否承重，取决于 compose 文件里有没有顶层 `name:`**：
#        - 有 `name: ling`（本仓库 `deploy/compose.yaml` 就是）→ 它覆盖了"按目录名
#          推导项目名"的默认行为，**不给 `-p` 也照样命中**（真机实测 rc=0）；
#        - **没有** `name:` → 项目名按 compose 文件所在目录名推导（这里是
#          `deploy`），与 `install.sh` 的 `-p ling` 不一致，`exec` 就找不到容器，
#          docker 报 `service "db" is not running`——和"db 容器挂了"文案一模一样，
#          运维极易查错方向。
#      所以 cron/备份脚本里**仍然要给 `-p`**（不能依赖 compose 恰好写了 `name:`，
#      也不能保证运维没改过项目名），但说"唯一承重参数"是错的。
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
  # ---- 前置静态校验：compose 文件必须能被解析 ----
  # `exec` 不看 `-f`（见上），所以一份坏掉的 compose 文件在 exec 阶段**完全
  # 不会被发现**：备份照样 exit 0，直到某天 `up`/`config` 才炸——那时是恢复/
  # 升级现场，最不该出现"配置坏了"的时刻。这里用 `config -q` 做一次纯静态
  # 解析（不连 docker daemon、不拉镜像、不启容器，cron 每日跑的开销可以忽略），
  # 让文件坏掉时 fail fast。
  #
  # 退出码 **2（配置错误）**，绝不是 3：3 是"pg_dump 失败"的契约语义，把配置
  # 问题混进去会让告警方以为数据库出事。
  #
  # 位置说明（**别照抄成"在任何动作之前"**）：这条校验在 `mkdir -p "$OUT_DIR"`
  # 与 `acquire_lock` **之后**（函数开头就建目录、抢锁，否则并发保护无从谈起）。
  # 所以配置坏掉时**会留下一个空 OUT_DIR**、锁由 `trap` 释放。真机实测确认过这个
  # 行为（shots/docker-real/P3-REAL-REVERIFY.md 的 B-2）。它拦住的是"导出/墓碑/密钥/
  # 媒体"这些**会留下半成品**的动作——那些确实都还没开始，所以退出时是干净的，
  # 只剩一个空目录。这条事实被
  # `test_compose_config_precheck_runs_after_outdir_creation` 显式锁住。
  if ! compose_config_err="$(docker compose "${COMPOSE_ARGS[@]}" config -q 2>&1)"; then
    echo "[backup] compose 配置校验失败：无法解析 compose 文件（exit 2，配置错误）" >&2
    echo "[backup]   项目名（-p）：${COMPOSE_PROJECT:-（空：会按 compose 文件所在目录名推导，cron 下极易错）}" >&2
    echo "[backup]   compose 文件（-f）：${COMPOSE_FILE}" >&2
    if [ ! -f "$COMPOSE_FILE" ]; then
      echo "[backup]   → 该路径不是一个存在的文件（LING_COMPOSE_FILE 指错了？）" >&2
    fi
    echo "[backup]   → 也可能是项目名与 compose 里的 name/top-level name 冲突，或插值变量未设置" >&2
    if [ -n "$compose_config_err" ]; then
      echo "[backup]   docker compose config 输出：" >&2
      printf '%s\n' "$compose_config_err" >&2
    fi
    echo "[backup]   （exec 不看 -f，所以坏文件不会在 exec 阶段暴露；这条校验就是补这个洞）" >&2
    exit 2
  fi
  PG_DB="${LING_DB_NAME:-ling}"
  PG_USER="${LING_DB_USER:-ling}"
  DUMP_FILE="$OUT_DIR/ling-$STAMP.dump"
  echo "[backup] compose 模式下导出（容器 db）"
  if ! docker compose "${COMPOSE_ARGS[@]}" exec -T db \
      pg_dump --no-owner --no-privileges --format=custom \
      -U "$PG_USER" -d "$PG_DB" > "$DUMP_FILE"; then
    # 这一条**同时**覆盖两种失败，而 docker 给它们的文案是同一句：
    #   - db 容器真没起来；
    #   - **项目名错**（`-p` 与真实项目名不一致 / 没给 `-p` 而按目录名推导），
    #     于是 exec 找不到容器，报 `service "db" is not running`。
    # 所以提示里必须带上"项目名"这条线索，否则运维会直奔 db 容器去查。
    # **退出码仍是 3**（pg_dump 失败语义，契约不变）。
    echo "[backup] pg_dump 失败（可能原因：db 容器没在运行；**或项目名/COMPOSE_PROJECT 不对**——" \
      "Compose v5 的 exec 按 project+service label 找容器、不看 -f，项目名不一致时报的正是" \
      "service \"db\" is not running。项目名=${COMPOSE_PROJECT:-（空，按目录名推导）}，" \
      "COMPOSE_FILE=${COMPOSE_FILE}）" >&2
    rm -f "$DUMP_FILE"
    exit 3
  fi
  if [ ! -s "$DUMP_FILE" ]; then
    echo "[backup] 导出文件为空，视为失败" >&2
    rm -f "$DUMP_FILE"
    exit 3
  fi
  # 墓碑清单同样通过容器取
  # **墓碑导出失败不能静默**：`|| true` 会让查询失败时留下一个空文件，
  # 而 manifest 仍然写着"有墓碑清单"。恢复时按空清单处理，已撤回的记忆
  # 就会复活——这正是墓碑机制要防的事，反而被备份脚本自己绕过。
  TOMBSTONE_STATE=ok
  if ! docker compose "${COMPOSE_ARGS[@]}" exec -T db \
    psql -U "$PG_USER" -d "$PG_DB" -tAc \
    "select id from memories where retracted = true order by id" \
    > "$OUT_DIR/tombstones-$STAMP.txt" 2>/dev/null; then
    TOMBSTONE_STATE=FAILED
    rm -f "$OUT_DIR/tombstones-$STAMP.txt"
    echo "[backup] 警告：墓碑导出失败，本次备份不可用于恢复" >&2
  fi

  # ---- 根密钥（app-data:/data/root.key）----
  # 环境变量 LING_ROOT_KEY 非空时应用**不读该文件**，两者都要在 manifest 里说清。
  #
  # 探测与读取合成**一条**容器内命令，用专用退出码 3 表示"文件不存在"：
  # 否则"api 容器没起来/docker 报错"（也是非零）会被当成"没有密钥"，
  # 于是产出一份 exit 0 但恢复后解不开凭据的备份——正是要防的那种静默。
  #
  # **密钥完整性必须能自证，不能只靠"别忘了写 -T"**：`docker compose exec` 少了
  # `-T` 而宿主有 TTY 时 docker 会分配 pty，TTY 层**可能**给流加前导 `\0`、并把
  # 行尾变成 CRLF，而**退出码仍是 0**——这是要防的失败形态。
  #
  # 关于"44 B → 46 B"这个具体字节数：它是早期在某个 Docker/Compose 版本上观测到的，
  # **在 Docker 29.6.2 + Compose v5.3.1 上复现不出来**（真机实测：pty 确实被分配，
  # 但 44 字节哑数据经 stdout 出来仍是 44 B、sha 相同、stderr 未合并，见
  # shots/docker-real/P3-REAL-REVERIFY.md 的 C-4）。所以**不要把这个字节数当成契约**：
  # 它想表达的是"没有 -T 时字节流可能被污染且不报错"，而不是"一定会多 2 字节"。
  # 自校验的价值正在于此——不管污染是 2 字节、NUL、还是某个版本上根本不发生，
  # 只要容器侧哈希与归档字节对不上就 exit 4，不会静默归档。
  # 旧实现只对**归档后**的文件算
  # sha256 写进 manifest，从不与容器里的原值比对，于是会**静默归档一把坏密钥**
  # ——恢复时库里的 provider 凭据全部解不开，而 manifest 看起来毫无异常
  # （哈希自洽，只是自洽的是坏字节）。
  # 现在在**同一次** exec 里多拿一份证据：容器先对 /data/root.key 算一次哈希写到
  # stderr（`sha256=<hex>`），再把同一份字节写到 stdout；宿主把 stderr 收进临时
  # 文件，取出容器侧哈希，与归档字节的哈希**逐字符比对**：
  #   - 一致 → 正常归档；
  #   - 不一致 / 拿不到容器侧哈希 → exit 4（"无法确认密钥状态，本次备份作废"），
  #     清理半成品，**绝不**写 keys/root.key，**绝不**写 manifest。
  # 日志里只出现哈希，**绝不**出现密钥内容；stderr 临时文件只解析、不回显
  # （无 -T 时 pty 会把两条流合并，那个文件里也可能有密钥字节），用完即删。
  KEYS_STATE=absent
  KEYS_FILE=-
  KEYS_SOURCE=none
  KEYS_HASH=-
  KEYS_TMP="$OUT_DIR/.root.key.part.$$"
  # 容器侧哈希的中转文件（`umask 077` 子 shell 里创建 → group/other 位为 0，常见
  # 文件系统上是 0600，用完即删）。
  KEYS_HASH_TMP="$OUT_DIR/.root.key.sha.$$"
  # 容器内脚本：**一次** exec 同时给出"内容"（stdout）与"容器侧真实哈希"
  # （stderr）。哈希只能走 stderr——stdout 必须是可直接落盘的纯密钥字节流。
  # 容器内退出码：3 = 文件不存在（宿主据此记 absent）；6 = 容器里没有
  # sha256sum / shasum / openssl 任何一种哈希工具；5 = 算了但结果为空。
  # 5 / 6 由宿主一律判成"无法确认"（exit 4），绝不放过未经验证的字节。
  KEYS_READ_SCRIPT='
if [ ! -f /data/root.key ]; then exit 3; fi
if command -v sha256sum >/dev/null 2>&1; then
  h=$(sha256sum /data/root.key | cut -d" " -f1)
elif command -v shasum >/dev/null 2>&1; then
  h=$(shasum -a 256 /data/root.key | cut -d" " -f1)
elif command -v openssl >/dev/null 2>&1; then
  h=$(openssl dgst -sha256 -r /data/root.key | cut -d" " -f1)
else
  exit 6
fi
[ -n "$h" ] || exit 5
printf "sha256=%s\n" "$h" >&2
exec cat /data/root.key
'
  # 与 `stash_existing_keys` 同一个理由（而且这条窗口更长：跨 `docker compose
  # exec` 的一次网络往返）：`> "$KEYS_TMP"` 若在外层做，文件先以 umask 推出的
  # **0644** 落盘，`chmod 600` 要等容器读完才执行。把重定向挪进 `umask 077`
  # 的子 shell 里，文件从创建那一刻 group/other 位就是 0，根密钥字节全程不落
  # 0644。判定口径是 `mode & 0o077 == 0`，不要求恰好 0600（btrfs + trimacl 的
  # NAS 上这里会是 0700，同样安全，见 `stash_existing_keys` 上方的长注释）。
  set +e
  (
    umask 077
    docker compose "${COMPOSE_ARGS[@]}" exec -T api sh -c "$KEYS_READ_SCRIPT" \
      > "$KEYS_TMP" 2>"$KEYS_HASH_TMP"
  )
  KEY_RC=$?
  set -e
  # 取容器侧哈希：只认最后一行 `sha256=<64 位小写十六进制>`。
  # 不用量词正则（`{64}`）是为了 BSD/GNU awk 都稳；长度与字符集分两步判。
  # docker/compose 自己的告警也在这条 stderr 上，所以只挑这一种形状。
  KEYS_CONTAINER_HASH=""
  if [[ -f "$KEYS_HASH_TMP" ]]; then
    KEYS_CONTAINER_HASH="$(awk '
      { line = $0
        gsub(/\r/, "", line)          # 行尾若被 CRLF 化也照收（值本身必须是纯 hex）
        if (substr(line, 1, 7) == "sha256=") {
          v = substr(line, 8)
          gsub(/[ \t]/, "", v)
          if (length(v) == 64 && v ~ /^[0-9a-f]+$/) last = v
        } }
      END { if (last != "") print last }
    ' "$KEYS_HASH_TMP" 2>/dev/null)" || true
  fi
  if [[ $KEY_RC -eq 0 ]] && [[ -s "$KEYS_TMP" ]]; then
    # ① **归档前**先自校验，且故意放在 stash / mv **之前**：字节不对时连历史那份
    #    `keys/root.key` 都不碰——不需要走"先覆盖再回填"那条更危险的路。
    KEYS_LOCAL_HASH="$(sha256_of "$KEYS_TMP")"
    KEYS_LOCAL_BYTES="$(wc -c < "$KEYS_TMP" | tr -d '[:space:]')"
    if [[ -z "$KEYS_CONTAINER_HASH" ]]; then
      rm -f "$KEYS_TMP" "$KEYS_HASH_TMP"
      echo "[backup] 容器内没有回传 /data/root.key 的 sha256；" \
        "无法证明归档字节就是容器里那把密钥，本次备份作废" >&2
      echo "[backup]   → 最常见的原因是 exec 少了 -T：pty 把 stdout/stderr 合并成" \
        "一条流，哈希行跟着密钥字节一起落到 stdout，stderr 什么都不剩" >&2
      exit 4
    fi
    if [[ "$KEYS_CONTAINER_HASH" != "$KEYS_LOCAL_HASH" ]]; then
      rm -f "$KEYS_TMP" "$KEYS_HASH_TMP"
      echo "[backup] 根密钥完整性校验失败：容器内 sha256=${KEYS_CONTAINER_HASH}，" \
        "宿主收到 ${KEYS_LOCAL_BYTES} 字节、sha256=${KEYS_LOCAL_HASH}，" \
        "两者必须一致。最常见的原因是 exec 少了 -T——宿主有 TTY 时 TTY 层会给" \
        "字节流被 TTY 层加 NUL/CRLF 污染。本次备份作废，" \
        "绝不归档一把坏密钥（恢复时 provider 凭据会全部解不开）" >&2
      exit 4
    fi
    rm -f "$KEYS_HASH_TMP"
    mkdir -p "$OUT_DIR/keys"
    # 兜底再收一次（创建时 group/other 位已是 0；这一行把它收成精确 0600，
    # 正式产物因此与 umask / 文件系统无关）；文件内容不进任何日志（只记哈希）。
    chmod 600 "$KEYS_TMP"
    # 覆盖前先给历史那份 keys/root.key 留原字节副本（失败时清理逻辑原样放回）。
    stash_existing_keys
    # 从这里开始"本次动过 keys/root.key"就成立了——即使下面的 mv 中途失败，
    # 清理逻辑也必须按"已归档"处理（否则会留下半截文件）。
    KEYS_ARCHIVED=1
    mv -f "$KEYS_TMP" "$OUT_DIR/keys/root.key"
    KEYS_STATE=present
    KEYS_FILE=keys/root.key
    KEYS_SOURCE=file
    KEYS_HASH="$(sha256_of "$OUT_DIR/keys/root.key")"
    # ② **归档后**再核对一次：落盘的那份文件必须仍是容器侧那把密钥。
    #    `mv` 只是同一目录内的改名（字节不变），这条理论上打不着；留着是因为
    #    密钥是整条链上唯一"错了就不可逆"的东西，多一次断言比少一次便宜。
    #    走到这里说明已经动过历史密钥，失败时由 cleanup_partial 原样回填。
    if [[ "$KEYS_HASH" != "$KEYS_CONTAINER_HASH" ]]; then
      echo "[backup] 根密钥完整性校验失败：归档后 keys/root.key 的 sha256=${KEYS_HASH}" \
        "与容器内 ${KEYS_CONTAINER_HASH} 不一致；本次备份作废" >&2
      exit 4
    fi
    echo "[backup] 已归档根密钥到 keys/root.key（务必与 dump 分开存放）" \
      "—— 容器侧与归档字节的 sha256 一致：${KEYS_HASH}"
    if [[ "${KEYS_ENV_SET}" == "true" ]]; then
      echo "[backup] 警告：备份时 LING_ROOT_KEY 非空，应用**优先用环境变量**，" \
        "所以实际生效的根密钥可能不是 keys/root.key 这一把（manifest 记 " \
        "keys_env_set=true）。恢复时必须同时恢复 deploy/.env 的 LING_ROOT_KEY，" \
        "只回灌 keys/root.key 不够" >&2
    fi
  elif [[ $KEY_RC -ne 3 ]]; then
    rm -f "$KEYS_TMP" "$KEYS_HASH_TMP"
    echo "[backup] 读取容器内 /data/root.key 失败（exit=${KEY_RC}）；" \
      "无法确认根密钥状态，本次备份作废" >&2
    # 5 / 6 是容器内脚本自己给的语义：6 = 三种哈希工具都没有，5 = 算出来是空的。
    # 两者都意味着"容器侧哈希拿不到"，一律 exit 4（绝不归档未经验证的字节）。
    if [[ $KEY_RC -eq 6 ]]; then
      echo "[backup]   → 容器里没有 sha256sum / shasum / openssl 任何一种哈希工具，" \
        "没有容器侧哈希就无法证明归档字节是原密钥" >&2
    elif [[ $KEY_RC -eq 5 ]]; then
      echo "[backup]   → 容器内算出的哈希是空的（读 /data/root.key 失败？）" >&2
    fi
    exit 4
  elif [[ -n "${LING_ROOT_KEY:-}" ]]; then
    # 密钥来自环境变量（deploy/.env）：没有文件可归档，只能靠 env 备份，
    # 因此这里显式写 absent + source=env，而不是装作"已经备好了"。
    KEYS_SOURCE=env
    rm -f "$KEYS_TMP" "$KEYS_HASH_TMP"
    echo "[backup] 根密钥来自环境变量 LING_ROOT_KEY（未落盘）；请确认 deploy/.env 已单独安全保管" >&2
  else
    KEYS_SOURCE=none
    rm -f "$KEYS_TMP" "$KEYS_HASH_TMP"
    echo "[backup] 警告：容器内没有 /data/root.key，环境里也没有 LING_ROOT_KEY；" \
      "这份备份恢复到新卷后，provider 凭据将无法解密" >&2
  fi

  # ---- 媒体字节（app-data:/data/media）----
  MEDIA_NAME=absent
  MEDIA_STATE=absent
  MEDIA_HASH=-
  MEDIA_TMP="$OUT_DIR/.media-$STAMP.tar.part"
  MEDIA_ERR="$OUT_DIR/.media-$STAMP.err"
  # 同上：探测与归档合成一条容器内命令，退出码 3 = 目录不存在。
  # `tar -cf -` 在容器内读、stdout 流式落宿主文件：不把大目录读进内存，
  # 也不在只读根文件系统的容器里写任何东西。
  #
  # **创建瞬间就"同机其他用户读不到"**（和根密钥那两条同一条理由）：外层重定向
  # `> "$MEDIA_TMP"` 建文件走 `0666 & ~umask`，默认 umask 022 会先落 **0644**，
  # 而媒体是用户的**原图/原声**——整个流式传输期间（大媒体可能要几分钟）它都对
  # 同机其他用户可读。把重定向挪进 `umask 077` 的子 shell 内，文件一出生
  # group/other 位就是 0。口径仍是 **`mode & 0o077 == 0`**（不是"恰好 0600"）：
  # ext4 上得 0600，NAS 的 btrfs + trimacl 上得 0700，两者同样安全——真正要挡的
  # 是"同机其他用户"。正式产物 `media-<STAMP>.tar` 在 `mv` 前有显式
  # `chmod 600`，那条才断言精确 0600。
  # 退出码仍是**这条 docker 的**退出码：子 shell 的退出状态就是它最后一条命令的
  # 状态（`$?` 直接取到 TAR_RC），`umask 077` 不会把它吃掉；下面的 `-eq 3` /
  # `-ge 2` 判定与原来逐字一致。
  set +e
  (
    umask 077
    docker compose "${COMPOSE_ARGS[@]}" exec -T api \
      sh -c 'if [ -d /data/media ]; then exec tar -cf - -C /data media; else exit 3; fi' \
      > "$MEDIA_TMP" 2>"$MEDIA_ERR"
  )
  TAR_RC=$?
  set -e
  if [[ $TAR_RC -eq 3 ]]; then
    rm -f "$MEDIA_TMP" "$MEDIA_ERR"
    echo "[backup] 容器内没有 /data/media（还没有任何媒体文件），manifest 记 media=absent"
  else
    # GNU tar 的退出码语义：0 成功、1 归档过程中有文件正在被写入、2 致命错误。
    # 只有 >=2 才是真失败；1 交给下面的完整性校验兜底。
    if [[ $TAR_RC -ge 2 ]]; then
      echo "[backup] 媒体归档失败（tar exit=${TAR_RC}，容器 api）" >&2
      sed -n '1,5p' "$MEDIA_ERR" >&2 || true
      rm -f "$MEDIA_TMP" "$MEDIA_ERR"
      exit 5
    fi
    # 完整性：本地能把这份 tar 的目录结构读出来才算完整（截断的归档在这里失败）。
    # 注意容器/docker 出错时 stdout 往往是空的，这里也会判失败（不会静默成 absent）。
    if [ ! -s "$MEDIA_TMP" ] || ! tar -tf "$MEDIA_TMP" >/dev/null 2>&1; then
      echo "[backup] 媒体归档不完整（tar 无法列出内容），视为失败" >&2
      sed -n '1,5p' "$MEDIA_ERR" >&2 || true
      rm -f "$MEDIA_TMP" "$MEDIA_ERR"
      exit 5
    fi
    if [[ $TAR_RC -eq 1 ]]; then
      # 并发上传时 tar 会报"file changed as we read it"：漏掉刚写入的那张图，
      # 不构成致命错误，但必须留痕，恢复时要人工核对 media_ref。
      MEDIA_STATE=partial
      echo "[backup] 警告：归档媒体时有文件正在写入（tar exit=1），可能漏掉刚上传的文件；" \
        "恢复后请核对 media_ref（见 restore_drill.sh 的媒体检查）" >&2
    else
      MEDIA_STATE=present
    fi
    rm -f "$MEDIA_ERR"
    MEDIA_NAME="media-$STAMP.tar"
    # **先 chmod 再 mv**（与直接模式同一顺序）：反过来的话，`mv` 之后到
    # `chmod 600` 之间有一个"正式文件名还是 0644"的窗口，恢复脚本/备份同步工具
    # 完全可能正好在这个窗口里读到它。
    chmod 600 "$MEDIA_TMP"
    mv -f "$MEDIA_TMP" "$OUT_DIR/$MEDIA_NAME"
    MEDIA_HASH="$(sha256_of "$OUT_DIR/$MEDIA_NAME")"
    echo "[backup] 已归档媒体字节到 $MEDIA_NAME"
  fi

  # manifest 必须能自证：
  #   - mode=compose（cron/执行器走的就是这条）
  #   - 每个产物的文件名 + sha256（恢复时核对"拿到的是不是备份时那份字节"）
  #   - keys_root / media 的状态：**未生成也显式写 absent**，不留空、
  #     不静默省略——静默省略正是"以为备了、其实没备"的来源。
  {
    echo "created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "mode=compose"
    echo "dump=ling-$STAMP.dump"
    echo "dump_sha256=$(sha256_of "$DUMP_FILE")"
    echo "tombstones=tombstones-$STAMP.txt"
    echo "tombstones_state=$TOMBSTONE_STATE"
    echo "tombstones_sha256=$(sha256_or_dash "$OUT_DIR/tombstones-$STAMP.txt")"
    echo "keys_root=$KEYS_STATE"
    echo "keys_root_file=$KEYS_FILE"
    echo "keys_root_source=$KEYS_SOURCE"
    echo "keys_root_sha256=$KEYS_HASH"
    echo "keys_env_set=$KEYS_ENV_SET"
    echo "media=$MEDIA_NAME"
    echo "media_state=$MEDIA_STATE"
    echo "media_sha256=$MEDIA_HASH"
  } > "$OUT_DIR/manifest-$STAMP.txt"
  BACKUP_OK=1
  # 轮转放在**成功之后**：BACKUP_OK 已经置 1，所以轮转里的任何意外都不会触发
  # 失败清理（那会把刚做好的这份备份删掉）；run_prune 自身也从不返回非零。
  run_prune || true
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
TOMBSTONE_STATE=ok
if ! psql "$PG_URL" -tAc \
  "SELECT id, retracted_at FROM memories WHERE retracted = true ORDER BY retracted_at" \
  > "$OUT_DIR/tombstones-$STAMP.txt"; then
  TOMBSTONE_STATE=FAILED
  rm -f "$OUT_DIR/tombstones-$STAMP.txt"
  echo "[backup] 警告：墓碑导出失败，本次备份不可用于恢复" >&2
fi

# 加密根密钥必须一起备份，但**要分开存放**：
# 数据库里的 provider 凭据是 AES-GCM 加密的，根密钥丢了就全部解不开，
# 表现为"机器人突然连不上模型"，很难第一时间想到是密钥问题。
# 但也不能和 dump 放同一处——一次泄漏就等于同时拿到密文与密钥。
DATA_DIR="${LING_DATA_DIR:-/data}"
KEY_FILE="$DATA_DIR/root.key"
KEYS_STATE=absent
KEYS_FILE=-
KEYS_SOURCE=none
KEYS_HASH=-
if [[ -f "$KEY_FILE" ]]; then
  mkdir -p "$OUT_DIR/keys"
  # 先落到临时文件再原子 mv：中途失败不会留下一个被截断的 keys/root.key
  # （半截密钥比没有密钥更坏——它看起来像"已经备好了"）。
  KEYS_TMP="$OUT_DIR/.root.key.part.$$"
  # 用 `cat` 重定向而不是 `cp`：`cp` 建目标文件用的是**源文件的 mode & ~umask**，
  # 所以目标权限取决于源：在这个 `umask 077` 子 shell 里 group/other 位一律被挡掉，
  # 源 0644 会得到 **0600**（看着"对了"，其实是 umask 兜住的——换成 umask 022 就是
  # 0644）；而源 0700 会**原样留下 owner-x，目标就是 0700**，窗口期可执行。也就是
  # 说 `cp` 下**不依赖 umask 的**"同机其他用户读不到"只在源恰好 group/other 位为 0
  # 时成立，其余情况只是碰巧更窄、或者仍然偏宽。重定向创建走 `0666 & ~umask`，在
  # umask 077 下**恒定满足 `mode & 0o077 == 0`**，与源权限无关（具体值是 0600 还是
  # 0700 取决于文件系统：ext4 得 0600，NAS 的 btrfs + trimacl 得 0700，两者都安全）；
  # 随后的 `chmod 600` 只是兜底，并把正式产物收成精确 0600。真实部署里
  # `security.py` 会 chmod 600，但这里不依赖它——窗口对任何源权限都关掉。
  ( umask 077; cat -- "$KEY_FILE" > "$KEYS_TMP" )
  chmod 600 "$KEYS_TMP"
  # 同 compose 模式：覆盖前先给历史那份留原字节副本。
  stash_existing_keys
  KEYS_ARCHIVED=1
  mv -f "$KEYS_TMP" "$OUT_DIR/keys/root.key"
  KEYS_STATE=present
  KEYS_FILE=keys/root.key
  KEYS_SOURCE=file
  KEYS_HASH="$(sha256_of "$OUT_DIR/keys/root.key")"
  echo "[backup] 已复制根密钥到 keys/root.key（务必与 dump 分开存放）"
  if [[ "${KEYS_ENV_SET}" == "true" ]]; then
    echo "[backup] 警告：备份时 LING_ROOT_KEY 非空，应用**优先用环境变量**，" \
      "所以实际生效的根密钥可能不是 keys/root.key 这一把（manifest 记 " \
      "keys_env_set=true）。恢复时必须同时恢复 deploy/.env 的 LING_ROOT_KEY" >&2
  fi
elif [[ -n "${LING_ROOT_KEY:-}" ]]; then
  KEYS_SOURCE=env
  echo "[backup] 根密钥来自环境变量，未复制；请确认你已单独妥善保管" >&2
else
  KEYS_SOURCE=none
  echo "[backup] 警告：未找到根密钥，恢复后加密凭据可能无法解密" >&2
fi

# 媒体字节：直接模式下数据目录就在本机，直接用 tar 归档（同样只记哈希）。
MEDIA_NAME=absent
MEDIA_STATE=absent
MEDIA_HASH=-
MEDIA_DIR="$DATA_DIR/media"
if [[ -d "$MEDIA_DIR" ]]; then
  MEDIA_NAME="media-$STAMP.tar"
  MEDIA_TMP="$OUT_DIR/.media-$STAMP.tar.part"
  # 与 compose 模式同一个理由：外层重定向创建 `.part` 走 `0666 & ~umask`
  # （默认 0644），媒体字节在整个 `tar` 期间都对同机其他用户可读。
  # 把重定向挪进 `umask 077` 的子 shell 内 → 创建瞬间就 `mode & 0o077 == 0`
  # （ext4 得 0600，btrfs + trimacl 得 0700，两者同样安全；正式名由下面显式的
  # `chmod 600` 收成精确 0600）；
  # `if ! ( … )` 判定的仍是 **tar 自己的**退出码（子 shell 最后一条命令）。
  if ! ( umask 077; tar -cf - -C "$DATA_DIR" media > "$MEDIA_TMP" ); then
    echo "[backup] 媒体归档失败（${MEDIA_DIR}）" >&2
    rm -f "$MEDIA_TMP"
    exit 5
  fi
  # 先 chmod 再 mv（既有顺序不变）：改名之后不存在"正式名还是 0644"的窗口。
  chmod 600 "$MEDIA_TMP"
  mv -f "$MEDIA_TMP" "$OUT_DIR/$MEDIA_NAME"
  MEDIA_STATE=present
  MEDIA_HASH="$(sha256_of "$OUT_DIR/$MEDIA_NAME")"
  echo "[backup] 已归档媒体字节到 $MEDIA_NAME"
else
  echo "[backup] 没有 ${MEDIA_DIR}（还没有任何媒体文件），manifest 记 media=absent"
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
mode=direct
database=$SAFE_DB
dump=ling-$STAMP.dump
dump_sha256=$(sha256_of "$OUT_DIR/ling-$STAMP.dump")
tombstones=tombstones-$STAMP.txt
tombstones_state=$TOMBSTONE_STATE
tombstones_sha256=$(sha256_or_dash "$OUT_DIR/tombstones-$STAMP.txt")
keys_root=$KEYS_STATE
keys_root_file=$KEYS_FILE
keys_root_source=$KEYS_SOURCE
keys_root_sha256=$KEYS_HASH
keys_env_set=$KEYS_ENV_SET
media=$MEDIA_NAME
media_state=$MEDIA_STATE
media_sha256=$MEDIA_HASH
MANIFEST

BACKUP_OK=1
run_prune || true
echo "[backup] 完成：$OUT_DIR/ling-$STAMP.dump"
