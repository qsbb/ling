#!/usr/bin/env bash
# 数据库一致性备份（PostgreSQL + app-data 卷里的根密钥与媒体字节）。
# 用法：deploy/backup/backup.sh OUT_DIR
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
set -euo pipefail

OUT_DIR="${1:?用法: $0 OUT_DIR}"
STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT_DIR"

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
# 清理里**每条命令都带 `|| true`**：EXIT trap 里任何一条失败（磁盘只读、文件被别人
# 挪走……）都会被 `set -e` 当成"trap 自己失败"，把脚本原本精心区分的退出码
# （3 pg_dump / 4 根密钥 / 5 媒体）改写成 1——更糟的是成功路径上会让一次**成功**
# 的备份变成非零退出。清理只做"尽力而为"，绝不改写结论。
cleanup_partial() {
  rm -f "${OUT_DIR}/.root.key.part.$$" "${OUT_DIR}/.media-${STAMP}.tar.part" \
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
trap cleanup_partial EXIT

# 归档根密钥前先给**已有的** `keys/root.key` 留一份原字节副本（0600）。
# 失败时 `cleanup_partial` 用它把历史备份的密钥放回原位，成功时由 trap 删掉。
# 为什么是"复制"而不是"移走"：复制不会出现"文件暂时不存在"的窗口——进程被
# kill -9 打断时，`keys/root.key` 仍在原地（最坏只是多一个 `.root.key.prev.<pid>`
# 垃圾）。这里只复制**本目录内**已经落盘的那份副本，用重定向完成，不再调外部
# `cp`（命名卷里的密钥没法靠宿主 `cp` 取，见文件头的取数方式说明）。
#
# **创建即 0600**：重定向创建文件走 `0666 & ~umask`，默认 umask 022 会先落一个
# **0644**，随后的 `chmod 600` 之间就有一个"同机其他用户可读根密钥字节"的窗口
# （cron 路径的备份目录本来就是 0755）。所以把重定向放进 `umask 077` 的子 shell
# **内**执行——重定向在子 shell 里做，文件一出生就是 0600（子 shell 内的 `cat`
# 失败仍会让整个子 shell 非零退出，fail closed 语义不变）。
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
  exit 6
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
    echo "[backup] pg_dump 失败（容器是否在运行？COMPOSE_FILE=${COMPOSE_FILE}）" >&2
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
  KEYS_STATE=absent
  KEYS_FILE=-
  KEYS_SOURCE=none
  KEYS_HASH=-
  KEYS_TMP="$OUT_DIR/.root.key.part.$$"
  # 与 `stash_existing_keys` 同一个理由（而且这条窗口更长：跨 `docker compose
  # exec` 的一次网络往返）：`> "$KEYS_TMP"` 若在外层做，文件先以 umask 推出的
  # **0644** 落盘，`chmod 600` 要等容器读完才执行。把重定向挪进 `umask 077`
  # 的子 shell 里，文件从创建那一刻就是 0600，根密钥字节全程不落 0644。
  set +e
  (
    umask 077
    docker compose "${COMPOSE_ARGS[@]}" exec -T api \
      sh -c 'if [ -f /data/root.key ]; then exec cat /data/root.key; else exit 3; fi' \
      > "$KEYS_TMP" 2>/dev/null
  )
  KEY_RC=$?
  set -e
  if [[ $KEY_RC -eq 0 ]] && [[ -s "$KEYS_TMP" ]]; then
    mkdir -p "$OUT_DIR/keys"
    # 兜底再收一次（创建时已是 0600）；文件内容不进任何日志（只记哈希）。
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
    echo "[backup] 已归档根密钥到 keys/root.key（务必与 dump 分开存放）"
    if [[ "${KEYS_ENV_SET}" == "true" ]]; then
      echo "[backup] 警告：备份时 LING_ROOT_KEY 非空，应用**优先用环境变量**，" \
        "所以实际生效的根密钥可能不是 keys/root.key 这一把（manifest 记 " \
        "keys_env_set=true）。恢复时必须同时恢复 deploy/.env 的 LING_ROOT_KEY，" \
        "只回灌 keys/root.key 不够" >&2
    fi
  elif [[ $KEY_RC -ne 3 ]]; then
    rm -f "$KEYS_TMP"
    echo "[backup] 读取容器内 /data/root.key 失败（exit=${KEY_RC}）；" \
      "无法确认根密钥状态，本次备份作废" >&2
    exit 4
  elif [[ -n "${LING_ROOT_KEY:-}" ]]; then
    # 密钥来自环境变量（deploy/.env）：没有文件可归档，只能靠 env 备份，
    # 因此这里显式写 absent + source=env，而不是装作"已经备好了"。
    KEYS_SOURCE=env
    rm -f "$KEYS_TMP"
    echo "[backup] 根密钥来自环境变量 LING_ROOT_KEY（未落盘）；请确认 deploy/.env 已单独安全保管" >&2
  else
    KEYS_SOURCE=none
    rm -f "$KEYS_TMP"
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
  set +e
  docker compose "${COMPOSE_ARGS[@]}" exec -T api \
    sh -c 'if [ -d /data/media ]; then exec tar -cf - -C /data media; else exit 3; fi' \
    > "$MEDIA_TMP" 2>"$MEDIA_ERR"
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
    mv -f "$MEDIA_TMP" "$OUT_DIR/$MEDIA_NAME"
    chmod 600 "$OUT_DIR/$MEDIA_NAME"
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
  # 与 compose 模式的重定向不同：`cp` 建目标文件用的是**源文件的 mode & ~umask**
  # （默认 umask 022 下：源 0600 → 0600，源 0644 → 0644），不是 `0666 & ~umask`。
  # 下面的 `( umask 077; cp … )` 子 shell 让目标文件**创建时就不带 group/other 位**
  # （0644/0666 → 0600，带 owner-x 位的源 → 0700），随后 `chmod 600` 兜底。真实部署里
  # `security.py` 会 `chmod 600`，所以这条窗口平时不出现；收紧是为了覆盖源本身
  # 就偏宽的情况（`write_text` 与 `chmod` 之间被打断、被别的工具还原成 0644 等）。
  ( umask 077; cp "$KEY_FILE" "$KEYS_TMP" )
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
  if ! tar -cf "$MEDIA_TMP" -C "$DATA_DIR" media; then
    echo "[backup] 媒体归档失败（${MEDIA_DIR}）" >&2
    rm -f "$MEDIA_TMP"
    exit 5
  fi
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
echo "[backup] 完成：$OUT_DIR/ling-$STAMP.dump"
