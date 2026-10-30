# ling 部署与运维手册

## 1. 本地开发（无 Docker）

本机没有 Docker 时，用便携 PostgreSQL + Python 虚拟环境。

```bash
cd /opt/ling

# 1) 依赖
uv venv --python 3.13 .venv
. .venv/bin/activate
uv pip install -e ".[dev]"

# 2) 数据库（便携 PostgreSQL 16，端口 55432）
scripts/dev_pg.sh start

# 3) 前端构建（可选，首次需要）
cd clients/web-console && pnpm install && pnpm build && cd ../..

# 4) 启动
. scripts/dev_env.sh
export LING_WEB_DIST=$PWD/clients/web-console/dist
python -m ling
# → http://127.0.0.1:8080
```

开发默认 `LING_TEST_MODE=1`，对话由确定性 fake provider 生成，界面显著标注
“测试模式：回复由确定性 fake provider 生成，非真实模型能力”。

## 2. Docker Compose（Linux）

```bash
cd /opt/ling
bash deploy/install.sh              # 一条命令：生成配置、构建、起服务、等就绪
# 结束时会打印实际控制台地址：新生成的 .env 默认 18080，已有 .env 则按
# LING_EDGE_PORT。--port 可以改新生成 .env 的端口。
```

或手动：

```bash
cp deploy/env.example deploy/.env
# 编辑 deploy/.env：至少设置 POSTGRES_PASSWORD 与 LING_ROOT_KEY
# （固定值，用于加密供应商密钥），并把 LING_TEST_MODE 设为 0

cd clients/web-console && pnpm install && pnpm build && cd ../..
docker compose -f deploy/compose.yaml --project-name ling-dev up -d --build
curl http://127.0.0.1:8080/readyz
```

**端口只有一个来源**：Compose 发布到宿主的端口由 `deploy/.env` 的
`LING_EDGE_PORT`（compose 默认 8080）决定。安装器的就绪等待、安装摘要与
`--status` 都以 `docker compose config` 解析出的**实际发布端口**为准，不再假定
脚本里写死的 18080：

- 新装（没有 `deploy/.env`）时安装器生成 `LING_EDGE_PORT=18080`，也可以用
  `--port 8080` 改；摘要里打印的就是真实端口。
- 已有 `.env`（例如照 `env.example` 用了 8080）时，安装器按 8080 等 `/readyz`
  并打印 8080，不会再错等 18080。
- 已有 `.env` 时 `--port` **不会**改写它（安装器不写已有 env）；如果
  `--port` 与真实发布端口冲突，安装器直接失败并让你改 `LING_EDGE_PORT`，
  绝不静默等一个错端口。

### 首次登录的账号

`LING_BOOTSTRAP_ADMIN_PASSWORD` **留空**是推荐做法：应用会在首次启动时生成随机
口令，并打印在日志里：

```
==============================================================
  首次安装：已创建管理员账号（口令是随机生成的）
    用户名  ling
    密码    xxxxxxxxxxxxxxxx
==============================================================
```

```bash
docker logs ling-api-1 2>&1 | grep -A4 '首次安装'
```

拿到后登录，系统会**强制要求改密码**（改密前除 `/v1/auth/*` 外全部 403）。
改密时会吊销其它登录会话，只保留当前这次。

想自己指定初始口令，就显式设置 `LING_BOOTSTRAP_ADMIN_PASSWORD`——此时不会强制改密。

服务：`db`（不暴露端口）、`api`（容器内 8080，不发布宿主端口）、`worker`
（outbox/恢复）、`edge`（唯一入口，发布 `LING_EDGE_PORT`）、前端产物只读挂到 api 的 `/app/web`。

启动顺序：`db` 健康 → `api` 等库 → 跑迁移 → 起服务。迁移失败时 api 不启动。

### 安装锁、构建提交与阶段标记

- **跨 release 锁**：`install` / `restart` / `down` / `--build-web-only` 共用
  `deploy/.env` **解析后真实目标**所在目录的 `.ling-install.lock`（没有 `.env`
  时在 `ROOT/deploy/`）。多个 release 树共享同一份 `.env`（symlink）时仍然只有
  一把锁。实现是 Python `fcntl.flock` + 继承锁 FD 的 bash re-exec，不依赖
  Linux `flock(1)`，也没有“跳过锁”的开关；拿不到锁直接失败，不会并发部署。
- **锁文件必须是普通文件**：符号链接、目录、FIFO、设备一律拒绝（打开前
  `lstat`、`O_NOFOLLOW` 打开、`fstat` 核对 inode 与普通文件位，检测“检查后被
  替换”）。安装器不再往锁文件里写 PID，也**不会截断**它——即使有人把
  `.ling-install.lock` 换成指向 `.env`/证书的 symlink，也只会得到一条拒绝信息，
  目标文件字节不变。若看到“安装锁不能是符号链接”，请先人工确认该路径再删除。
- **宿主可以先持锁**：更新执行器在它自己的预检/备份之前就拿到同一把
  `.ling-install.lock`，再通过 `pass_fds` 把 FD 以 `LING_INSTALL_LOCK_FD`
  传给 `install.sh`。安装器不会二次抢锁，而是在**继承来的那个 FD 本身**上要求
  `flock(LOCK_EX|LOCK_NB)` 成功（同一 open file description 已持 `LOCK_EX` 时
  幂等成功），并用独立 probe 确认这个 inode 上确实有持有者。因此：
  别人持锁时传入一个“同 inode 但未加锁”的 FD 会被拒绝；共享锁不会被当成互斥；
  伪 FD、普通文件、已解锁的 FD 都过不去。执行器“备份 → 安装”整段仍是同一把锁，
  中间插不进第二个安装器。手工调用时不要设置这个变量。
- **`deploy/.env` 建议是固定“原始 env”的 symlink**：多个 release 树指向同一份
  env（例如 `/srv/ling/shared/ling.env`），锁落在它真实所在的目录，跨 release
  也只有一把锁；安装器**不覆盖、不追加**已存在的 `.env`（普通文件、只读文件、
  symlink 都保持字节不变），也不会把密钥复制到 release 树里。基础镜像
  （python/postgres/nginx）的解析结果只 `export` 到本次进程环境，不写回 env。
- **新生成的 `.env` 不再写死本检出的绝对 dist**：`LING_WEB_DIST_HOST` 用相对
  `deploy/` 的默认值 `../clients/web-console/dist`。canonical env 被多个
  release 共享时，绝对值会指向上一个 release 树，让后续**手动**安装挂到旧前端
  并在校验时 fail-closed（更新执行器会自己传本次 release 的 dist，不受影响）。
  已存在的绝对值不会被自动修改：在另一棵树手动升级时显式
  `export LING_WEB_DIST_HOST="$(pwd)/clients/web-console/dist"`，并保留
  `LING_INSTALL_COMPOSE_OVERRIDE`、edge 配置/证书等其它 bind 上下文。
- **基础镜像显式选择不会被替换**：shell / `--python-base` / env-file 里的
  `LING_PYTHON_BASE`、`LING_POSTGRES_IMAGE`、`LING_NGINX_IMAGE` 一律原样保留；
  只有“默认值且官方名不可用”才换成**同一 tag** 的镜像源（`postgres:16-alpine`
  不会退成 18），两处都拿不到就 fail closed。
- **构建提交**：构建前解析本检出 `git rev-parse HEAD`（40 位）或显式
  `LING_COMMIT`，作为 build arg 注入镜像的 `LING_COMMIT`。非 git 检出（源码
  归档）且没有显式值时会明确告警并留空；`/v1/version` 报 `commit=null`，
  不会把未知提交伪造成真实提交。宿主更新执行器必须导出它实际构建的 sha。
- **阶段标记**：宿主可传 `LING_INSTALL_PHASE_FILE`（可信绝对路径、普通文件、
  不接受 symlink）。安装器按 `build` → `deploy` → `verify` → `complete` 原子
  写入单个阶段名，用来区分“构建前失败（旧服务还在）”与“可能已经迁移/重启
  （需要人工处理）”。`deploy` 一定写在**第一个会改容器状态的动作之前**（`compose
  build` 还算 `build`）。不传时没有任何额外输出，行为与旧版一致。该文件不属于
  Web 可见内容，也不要放进容器挂载。

### HOME / DOCKER_CONFIG 与宿主 Compose override

`install.sh` 需要 Docker，但不该改宿主的 Docker 身份：

- **HOME / DOCKER_CONFIG 原样保留**。`docker context`、registry 凭据都在
  `$DOCKER_CONFIG`（默认 `$HOME/.docker`）里，无条件改写到仓库内目录会让
  “明明 `docker login` 过”的私有镜像拉取失败。所以安装器只在 **HOME 缺失或
  不可写**（老 NAS 账号常见，`docker build` 会报 `mkdir /home/xxx: permission
  denied`）时才回退到 `ROOT/.run/home`；`DOCKER_CONFIG` 未被显式设置时才跟着
  回退到 `ROOT/.run/docker`。这两个目录在仓库已忽略的 `.run/` 下（不会弄脏旧
  checkout，也不会把 registry 配置带进 docker build context），权限 `0700`。
  回退会在输出里明确告警“这里没有宿主的 docker context / registry 凭据”。
  脚本不复制、不搬移任何凭据。
- **显式 `DOCKER_CONFIG` 只告警、不重置**：如果它被指到一个普通文件、或目录建
  不出来，安装器保留该值并告警，让 docker 自己报具体路径错误；不会悄悄换成回退
  目录。这样 `--down` / `--logs` 这类恢复操作不会被一个坏掉的凭据路径提前挡住，
  也不会覆盖一个本来有效的 context。
- **旧版回退目录（`ROOT/.home`、`ROOT/.docker`）不再使用**：脚本不删除、不
  搬移它们，只在你确实触发回退时提示。若某个老 checkout 里已经有这两个目录，
  在**启用宿主更新执行器之前**，必须先确认该 checkout（执行器的 source_root）
  已经具备本版本的 `.gitignore` / `.dockerignore`（`.run/` 等）与跨 release
  锁支持，并且 `git status` 干净——执行器会把这个树当 release 源。清理旧目录请
  人工 `mv` 到别处或确认内容后自行删除；**不要**用 `git reset --hard` /
  `git clean -xfd` 去“顺手修”，那会连未跟踪的现场一起清掉。
- **宿主 Compose override（可选）**：`LING_INSTALL_COMPOSE_OVERRIDE=<绝对路径>`
  指向宿主/更新执行器生成的追加 compose 文件，用于跨独立 release 固定 bind
  来源。安装器只把它追加成
  `docker compose -f deploy/compose.yaml -f <override> --env-file deploy/.env …`：
  不复制、不改写、不直接解析它的文本（交给 Compose 解析；文件可以是 JSON，即
  YAML 的子集），也不会把它写进 `deploy/.env`，更不会回显内容。校验是严格的：
  绝对路径、已存在、普通文件（拒绝 symlink/目录/FIFO）、可读且 ≤1 MiB，否则直接
  失败；`--build-web-only` 不碰 Docker，会忽略并告警。
- **override 不能搬走 `/app/web`**：安装器的不变量是“compose 实际挂载的
  `/app/web` == 本次校验过的产物”。固定 bind 来源只应针对 `/app/web` 以外的
  目标（edge 配置/证书、更新通道目录等）；把 `/app/web` 指到别处会让安装
  fail-closed（报“不是本次校验产物”）。
- **override 也不能搬走持久数据卷**：有 override 时安装器会让 Compose 分别解析
  “基础 compose”和“base+override”，要求：
  1. 基础 compose 声明的顶层命名卷（`db-data`、`app-data`）的卷 identity
     （`name` / `driver` / `driver_opts` / `external`）完全一致；
  2. 基础 compose 里**每一处命名卷挂载**的 服务 / `target` / 只读 / 卷身份
     完全一致。

  把 `/data` 换成一个空卷、改 `db-data` 的来源、把 `app-data` 声明成
  external、或删掉这些挂载，都会被拒绝（构建/`up` 之前）。匿名卷（Compose 不写
  `source`）按“没有跨重启身份”处理，不会因为每次解析的随机名被误判；override
  新增挂载、合法的 edge 配置/证书绝对源、更新通道目录，以及本次 release 的
  `/app/web` 更新不受影响。失败信息只报服务名/卷名，不打印 compose config
  （里面有数据库口令与 root key）。
- **override 是宿主信任输入，不是安全沙箱**：安装器只保证上面两条结构不变量
  （`/app/web` 身份 + 持久命名卷身份），**没有安全 allowlist**。override 可以
  新增任意 bind（包括 `/var/run/docker.sock`）、`privileged`/`cap_add`/`user`/
  `network_mode`/`pid`，也可以覆盖 `image`/`command`/`build`——这些都会原样交给
  Compose 执行。不要把 override 交给 Web、半可信脚本或普通用户可写的路径生成；
  它应由 root/部署用户持有（建议 `0600`，不要 0777），内容只写你核实过的字段。
  需要更严格的边界时，请在宿主侧对 override 做代码评审/白名单，而不是指望安装器。
  这一条也是“API 容器没有 Docker socket、非 root”这一设计前提的宿主侧前提。

宿主自助更新（默认关闭）的边界见本文 §10 与 [UPDATES.md](UPDATES.md)。

### 非 root 与只读根

容器以 uid 10001 运行，根文件系统只读 + `/tmp` tmpfs，`no-new-privileges`。
数据卷只有 `/data`（本地密钥回退用）。

### 健康检查语义

| 端点 | 含义 |
| --- | --- |
| `/healthz` | 进程活着 |
| `/readyz` | 数据库可达 + 迁移完成 + 生效配置修订 |
| `/v1/health/model` | 各职责 profile 的熔断状态摘要 |

## 3. 设置模型（唯一入口）

1. 登录 → 设置 → 供应商：新增 `openai_compatible`，填 `base_url`（如
   `https://api.openai.com/v1`），保存密钥（只显示后四位）。
2. 连接检查（`/models` 探测），确认鉴权通过。
3. 职责绑定：至少给 `dialogue.fast` 绑定 provider + model_id。
4. 保存草稿 → 发布。发布后的修订对**下一轮**生效。
5. 出问题时回滚到上一个已发布修订。

**没有凭据时**：保持 `LING_TEST_MODE=1`，界面会一直标注测试模式。

## 4. 备份与恢复

```bash
# 备份（数据库 dump + 墓碑清单 + app-data 卷里的 root.key 与 media 字节）。
# 默认 compose 不发布 db 端口、宿主机也没有 DSN，所以要用 compose 模式；
# 两个项目变量不能省（原因见 §8.1）：
LING_BACKUP_VIA_COMPOSE=1 \
LING_COMPOSE_PROJECT=ling \
LING_COMPOSE_FILE="$(pwd)/deploy/compose.yaml" \
deploy/backup/backup.sh /path/to/backup
# 产物（文件名由 backup.sh 的日期戳决定）：
#   ling-YYYYmmdd-HHMMSS.dump       数据库 dump（pg_dump custom 格式）
#   manifest-YYYYmmdd-HHMMSS.txt    清单：产物名 + 各产物 sha256 + keys_root/media 状态
#   tombstones-YYYYmmdd-HHMMSS.txt  撤回墓碑 id（无撤回记录时为空）
#   keys/root.key                   根密钥（0600，**必须与 dump 分开保管**）
#   media-YYYYmmdd-HHMMSS.tar       app-data:/data/media 的实体文件（图片/语音）
#   （直接模式另有 version-YYYYmmdd-HHMMSS.txt：`SELECT version()` 的输出；
#     compose 模式通过容器取不到，不产出这一件——它不是 manifest 的必需产物。）

# 恢复演练（隔离库，不碰生产）
# 恢复演练（**两个参数**：dump 文件 + 演练目标库）
# 目标库名必须以 _drill 结尾，脚本拒绝覆盖生产库
LING_BACKUP_VIA_COMPOSE=1 \
LING_COMPOSE_PROJECT=ling \
LING_COMPOSE_FILE="$(pwd)/deploy/compose.yaml" \
deploy/backup/restore_drill.sh \
    /path/to/backup/ling-YYYYmmdd-HHMMSS.dump \
    ling_drill
```

> 直接模式（`LING_DATABASE_URL=... deploy/backup/backup.sh ...`）只适用于
> 能直连数据库的环境；默认 Compose 部署没有发布 db 端口，直接跑会以
> “缺少 LING_DATABASE_URL”失败——那不是“备份已完成”。`LING_COMPOSE_PROJECT`
> 必须与真实 Compose 项目名一致（`install.sh` 默认 `-p ling`），否则
> `docker compose exec` 找不到容器。

`manifest-*.txt` 里每个产物都带哈希与状态：`dump_sha256`、`tombstones_sha256`、
`keys_root_sha256`、`media_sha256`，以及 `keys_root=present|absent`、
`keys_root_source=file|env|none`、`keys_env_set=true|false`、
`media=media-….tar|absent`、`media_state=present|partial|absent`
（`partial` = 归档时有文件正在写入，可能漏掉刚上传的那一个）。
`keys_root_source` 只说明"密钥字节从哪来"，`keys_env_set` 才说明"应用会不会
优先用环境变量"——两者含义不同，见 §8.1。
`restore_drill.sh` 只用其中 **dump / 墓碑 / 媒体** 三份哈希核对文件
（`keys_root_sha256` **不在**演练脚本的校验范围内，见 §8.1），并检查恢复后库里
每条 `media_ref`（`file:/data/media/…`）指向的文件是否都在媒体归档里。

备份里**没有**的东西也要知道：provider 的明文密钥（库里只有 AES-GCM 密文）、
数据库口令（只在 `deploy/.env` 里）。根密钥单独放在 `keys/root.key`（或
`deploy/.env` 的 `LING_ROOT_KEY`），**不在 dump 内**——只恢复数据库不会把它带回来。

恢复前必须核对撤回墓碑：旧备份可能让已删除记忆重新可见。

注意语义边界：这是**全量逻辑 dump，不是 PITR**——恢复会丢掉备份之后写入的全部
数据（不是“回到某个时间点”）。备份之后新增的撤回墓碑不会自动回来，恢复后要
**重新应用**墓碑，而不是只核对清单。

### 恢复顺序：先 DB → 再 root.key 与 media → 最后起服务

`app-data` 是 **命名卷**（`compose.yaml` 里 `app-data:/data`），恢复到一个新卷时它
默认是空的。所以"把 dump 灌回去"只是第一步：

1. **先恢复数据库**：先用 [`restore_drill.sh`](#4-备份与恢复) 恢复到隔离库，确认
   关键表有数据、墓碑与备份时一致、没有 `tombstones_state=FAILED`，再按
   [UPDATES.md §8](UPDATES.md) 的完整 compose 上下文恢复到正式库。
2. **再把 `keys/root.key` 与 `media-*.tar` 放回 `app-data` 卷**（写卷，不要写进
   容器里：容器重建后容器内文件就没了，卷还在）：

   ```bash
   # <proj> 是 Compose 项目名（install.sh 默认 ling；用 --project 改过就填那个）
   # 根密钥：0600 且归属容器内的 uid 10001，否则 api 读不到
   docker run --rm -v <proj>_app-data:/data -v /path/to/backup:/backup:ro alpine \
     sh -c 'cp /backup/keys/root.key /data/root.key \
            && chmod 600 /data/root.key && chown 10001:10001 /data/root.key'
   # 媒体字节：归档里的顶层目录就是 media/
   docker run --rm -v <proj>_app-data:/data -v /path/to/backup:/backup:ro alpine \
     tar -xf /backup/media-YYYYmmdd-HHMMSS.tar -C /data
   ```
3. **最后启动服务**（`bash deploy/install.sh`，或带完整上下文的
   `docker compose … up -d`），再验证 `/readyz`、`/v1/health/ops`，并抽一条
   带媒体的记忆确认图/语音能被 `describe` 读到。

**顺序不能颠倒，尤其密钥必须在服务之前就位**：api/worker 启动时就会初始化
`SecretBox`；如果 `LING_ROOT_KEY` 为空且 `/data/root.key` 不存在，它会**自己生成
一把新密钥**。新密钥一旦落盘，后面再补进来的旧 `root.key` 就不会被采用，
库里那些用旧密钥加密的 provider 凭据**永久解不开**（只能重新录入密钥）。

> **root.key 丢失 = provider 凭据无法解密**。库里 provider 的密钥是 AES-GCM
> 密文，根密钥（`LING_ROOT_KEY` 或 `/data/root.key`）丢了/换了，全部密文都解不开，
> 现象是"机器人突然连不上模型"而不是"密钥不正确"，很难第一时间想到是密钥问题。
> 所以 `keys/root.key` 要**与 dump 分开存放**（一次泄漏就等于同时拿到密文与密钥），
> 也要写清在哪个备份目录里——它不在数据库里，`pg_restore` 找不回来。

### 重新应用撤回墓碑（恢复后必做）

墓碑是 `memories.retracted = true` 的行，恢复 dump 会把它们变回未撤回。步骤必须
**先导出、后恢复、再重放**——只重放备份时的清单会漏掉“备份之后才撤回”的记忆：

```bash
# 0) 先按 [UPDATES.md §8](UPDATES.md) 第 1 步定义 compose() 函数并设置
#    PROJECT / LIVE / ENV_FILE / OVERRIDE；下面所有命令都通过它执行，
#    绝不从旧源码树裸跑 docker compose。
# 1) 恢复之前，从**当前（还没被覆盖的）库**导出最新墓碑 id：
compose exec -T db psql -U ling -d ling -tAc \
  "select id from memories where retracted = true order by id" \
  > /path/to/backup/tombstones-live-$(date -u +%Y%m%d-%H%M%S).txt

# 2) 合并“备份时清单”与“恢复前清单”，只取 id 并去重（兼容两种文件格式：
#    compose 模式是 id，直接模式是 id|retracted_at）：
for f in /path/to/backup/tombstones-<stamp>.txt /path/to/backup/tombstones-live-*.txt; do
  [ -f "$f" ] && cat "$f" || true
done \
  | cut -d'|' -f1 | tr -d ' ' \
  | grep -E '^[A-Za-z0-9_-]{1,64}$' | sort -u \
  > /tmp/tombstones-merged.txt
wc -l /tmp/tombstones-merged.txt

# 3) 恢复 dump（§8 的完整上下文；先确认 schema 兼容，见 UPDATES.md §8 第 3 步），
#    然后把清单里的 id 标记回撤回状态。只更新还没撤回的行，重复执行安全：
IDS=$(paste -sd, /tmp/tombstones-merged.txt)
if [ -z "$IDS" ]; then
  echo "清单为空：不需要重放（请先确认备份的 tombstones_state=ok）"
else
  compose exec -T db psql -U ling -d ling -v ON_ERROR_STOP=1 -v ids="$IDS" <<'SQL'
BEGIN;
UPDATE memories
   SET retracted = true,
       retracted_at = COALESCE(retracted_at, now()),
       retract_reason = COALESCE(retract_reason, 'restore: reapply tombstone')
 WHERE id = ANY (string_to_array(:'ids', ','))
   AND retracted = false;
SELECT count(*) AS still_retracted
  FROM memories
 WHERE id = ANY (string_to_array(:'ids', ','));
COMMIT;
SQL
fi
```

核对与边界：

- 第 2 步的 `grep` 会拒绝不符合 `id` 格式的行；若你的库里 id 不是
  `mem_<hex>` 这种形式，先人工看一眼清单再放行，别把脏数据拼进 SQL。
- 恢复后重放的墓碑拿不到原始撤回时间：`retracted_at` 会用恢复时间兜底
  （`COALESCE` 不会覆盖已有值），审计含义是“至少在这个时间点已撤回”。
- 如果备份的 `manifest-*.txt` 里 `tombstones_state=FAILED`，这份备份**不能**
  用来做恢复（清单本身不可信）。
- 重放后校验 `/readyz`、`/v1/health/ops` 与几条关键记忆的可见性；恢复演练
  （`restore_drill.sh`）里同样要做这一步。

镜像/schema 级别的人工恢复（旧 image ID、`--no-build` 的真实语义、数据库
schema 兼容核对、完整的 Compose 上下文命令）见 [UPDATES.md](UPDATES.md) §8；
不要从旧源码树裸跑 `docker compose`，那会让相对 bind 静默指回旧树。

## 5. 故障排查

| 现象 | 检查 |
| --- | --- |
| 登录后立刻 401 | `LING_COOKIE_SECURE=1` 但走 HTTP；或数据库不可达导致会话查不到 |
| 对话一直失败 `provider_unavailable` | 设置里 provider 未启用 / 熔断中（`/v1/health/model`） |
| `secret_missing` | 该供应商没保存密钥 |
| `config_invalid` 发布被拒 | 草稿校验错误，响应里 `detail` 列出字段 |
| 界面显示“服务端仍在生成” | 上一进程崩溃；重启后 `_recover_dangling_turns` 会标记 failed，可点重试 |
| `/readyz` 503 | 数据库不可达或迁移未跑 |

## 5.9 日志：字段、级别与怎么查

默认输出一行一条 JSON（容器生产环境）或可读文本（本地）。用
`LING_LOG_FORMAT=json|text` 显式覆盖，`LING_LOG_LEVEL` 调级别。

### 信封字段

| 字段 | 说明 |
|---|---|
| `timestamp` / `severity_text` / `logger` | 标准三件套 |
| `process_role` | `api` 或 `worker`——两个进程的日志要能分开看 |
| `event_name` | 稳定的事件名，`snake_case`，**不含动态值**，用于检索聚合 |
| `message` | 中文说明，给人看 |
| `attributes` | 动态信息全在这里（计数、耗时、状态码、错误类型） |
| `trace_id` / `turn_id` / `session_id` | 关联 ID |
| `redaction_applied` / `redaction_fields` | 有字段被脱敏策略拦下时出现 |

### 脱敏是机制，不是自觉

`SENSITIVE_KEYS` 里的字段名（`content`/`prompt`/`memory`/`token`…）在
Formatter 层被整值替换成 `[REDACTED]`；值里夹带的 `Bearer xxx`、
带密码的连接串、长随机串也会被拦。**对话正文与记忆内容永远不会进日志**——
它们已经有自己的撤回/遗忘机制，日志不该成为另一个不受管的副本。

> 边界说清楚：Formatter 只认字段名和已知模式，判断不了一段中文自然语言
> 是不是敏感内容。所以真正的保证是"事件字段规范 + 这层拦截 + 测试里塞
> canary 断言不出现"三件事合力。

### 用 trace_id 查一次请求

```bash
# 用户报错时把界面上的 ID 发过来，直接定位那一次请求
docker logs ling-api-1 2>&1 | grep '<trace_id>'

# 看连接器为什么没连上
docker logs ling-api-1 2>&1 | grep -E 'connector_(connected|disconnected|reconnect_failed|retry_exhausted)'

# 只看错误
docker logs ling-api-1 2>&1 | grep '"severity_text": "ERROR"'
```

### 一轮对话的完整链路

```
turn_started → reply_decision_made → turn_completed
                                   ↘ delivery_failed
```

| 事件 | 回答什么 |
|---|---|
| `turn_started` | 这轮什么时候开始、谁触发（web / connector）、用的哪个配置修订 |
| `reply_decision_made` | 回不回、为什么（`skip_code` 受控枚举） |
| `turn_completed` | 结束了没有、产出了几段、投递完整不完整（`outcome`） |
| `delivery_failed` | 哪一段没送到、平台给的状态、是否可重试 |

按 trace_id 串起来就是一条完整证据链：

```bash
docker logs ling-api-1 2>&1 | grep '<trace_id>' | grep -E 'turn_|delivery_'
```

「她为什么没回这条消息」查这一条：

```bash
docker logs ling-api-1 2>&1 | grep reply_decision_made | grep '"skip_code"'
```

```json
{"event_name": "reply_decision_made", "message": "决定不回应",
 "attributes": {"decision": "silence", "skip_code": "group_not_addressed",
                "hard_rule": true, "is_group": true,
                "reason": "群聊里的普通聊天，没有指向角色，不插话"}}
```

`skip_code` 是**受控枚举**（`SKIP_CODES`），不是自由文本——自由文本没法聚合。
措辞在 `reason` 里，事实在 `skip_code` 里。同一个事件名在「连接器入站」与
「Web 对话」两条路径都会发，所以一条查询就能覆盖两个入口。

### 主动联系走哪条路

`proactive.dispatch` 按顺序找投递面：

1. **外部渠道**：OneBot / QQ 官方机器人（需 `supports_proactive`，官方机器人
   默认没有，要在开放平台单独申请）
2. **Web 会话**：没有外部渠道时，草稿直接落成她的一条消息，用户下次打开
   面板就能看到

两条路都受同一套门控约束：`proactive_delivery_enabled`、勿扰时段、日限额。
没有可用投递面时，`/v1/proactive` 会**逐个渠道**说明原因——别只报第一个，
那会让人去配一个根本不是问题的东西。

### 她的日程（life.plan / life.tick）

| 事件 | 说明 |
|---|---|
| `life_plans_proposed` | 今天排了几件（`created` / `parsed` / `date`）；没排出时带 `reason_code` |

规则：**一天只排一次**（醒来后、当天没有在手计划时）；模型不可用或给不出
可用条目就**不排**，也绝不硬编"她在看书"。已经过去的时段不排——否则下午
打开界面会看到"她正在做一件早就该结束的事"，比空着更假。

### 群聊轮询与主动联系

| 事件 | 回答什么 |
|---|---|
| `attention_tick_completed` | 这轮刷了几个群、花了几次决策调用、省了几次、回了几个 |
| `attention_window_polled` | **每个**群窗口的结论，包括"没回"（带 `decision` / `reason` / 未读数） |
| `attention_tick_skipped` | 为什么整轮没跑（如模型路由未就绪） |
| `proactive_dispatched` | 发了几条、失败几条、被拒几条 |
| `proactive_dispatch_skipped` | 为什么没发（`reason_code`：勿扰/限额/没有可用渠道…） |
| `proactive_draft_skipped` | 草稿为什么没生成 |

```bash
# 她今天为什么没在群里说话
docker logs ling-worker-1 2>&1 | grep attention_window_polled | grep '"decision": "ignored"'
# 她今天为什么没主动找我
docker logs ling-worker-1 2>&1 | grep proactive_dispatch_skipped
```

### 审计日志与安全日志（独立命名空间）

| 命名空间 | 回答什么问题 | 典型事件 |
|---|---|---|
| `ling.audit` | **谁**在什么时候**改了什么** | `audit_config_published`、`audit_config_rolled_back`、`audit_provider_secret_set`、`audit_memory_retracted`、`audit_memory_forgotten`、`audit_memory_superseded`、`audit_consent_changed`、`audit_password_changed` |
| `ling.security` | **谁**被拒绝了、为什么 | `security_auth_failed`、`security_inbound_denied`、`security_signature_invalid`、`security_must_change_password_blocked` |

```bash
# 最近谁动过配置
docker logs ling-api-1 2>&1 | grep '"log_kind": "audit"' | grep config_
# 谁被白名单拦下来了（只有哈希，没有原值）
docker logs ling-api-1 2>&1 | grep security_inbound_denied
```

**审计字段只放计数、枚举、ID、版本号**，绝不放消息正文或记忆内容。
外部账号标识在安全日志里只写 HMAC 哈希（用低熵字符串做裸哈希可以被字典
反查，所以用带密钥的 HMAC）。

> 目前审计只落日志，不落库。要做长期留存时，只需在
> `ling/audit.py` 的两个函数里加一个 sink，所有调用点一行都不用改。

连接器状态机的事件名是固定的：`connector_config_missing`、
`connector_hello`、`connector_connected`、`connector_resumed`、
`connector_disconnected`、`connector_reconnect_failed`、
`connector_retry_exhausted`。**"没配置""连上了""断开了""重试耗尽了"
可以各自搜到**，不用再靠猜。

## 6. 验证状态（哪些验过、哪些没验）

**已验证**（后文 §7–§9 是这些验证的记录）：

- Docker Compose 单机部署：`api` / `worker` / `db` / `edge` 全部起得来，`/readyz` 通过
- 多副本 + edge 反代：滚动更新期间不断服
- 备份 → 恢复 → 校验整条链在真实部署上跑通（**口径**：真机上验过的是
  "dump + 墓碑清单 → 隔离库恢复 → 关键表/墓碑一致"这一段；之后新增的
  `root.key`/媒体归档、产物 sha256、`media_ref` 覆盖检查只有**本地 fake**
  与单元测试验证过，**未上真机**，见 §8.3）
- 真实模型对话：配好供应商与密钥后能正常回复

**未验证**：

- 微信 / 手机端 / 具身设备（这些渠道的适配代码在仓库里，但没有可用的对照端）
- 公网部署所需的域名、TLS、反代加固
- 长时间（数周）无人值守运行的稳定性
- 自助更新/升级执行器在**真实 Docker** 上的完整演练（备份、构建、切换、失败恢复）
- 备份链路的**新增**检查在真机上跑通：`docker compose exec` 的退出码透传、
  `tar -cf -` 的 stdout 字节干净性、大媒体卷耗时、并发上传时的 `media_state=partial`
- 升级失败后的数据库回滚（执行器只保留备份并转人工，不自动回滚）

> 判断某项是否真的能用，看它有没有对应的测试或本节的验证记录；
> 只写了代码没有记录的地方，按未验证对待。

## 7. 多副本部署（已验证）

```bash
# deploy/.env
LING_API_REPLICAS=2          # api 副本数
LING_EDGE_PORT=8080          # 对外只暴露 edge
LING_NGINX_IMAGE=nginx:alpine

docker compose -f deploy/compose.yaml --project-name ling up -d --build
```

拓扑：`edge(nginx):80 → api×N:8080`，`worker`、`db` 不对外。

### 为什么 api 不发布 host 端口

多副本发布同一 host 端口会冲突。对外只经 edge，内部用 Compose DNS
（服务名 `api` 解析到所有副本，nginx upstream 轮询即负载均衡）。

### 多副本的前提：状态不能在进程内存

已在 PostgreSQL 实现（迁移 0007）：
- `turn_controls`：停止信号跨实例传递（A 点停止、B 跑轮次）
- `speak_leases`：发言租约，同一角色同时只在一个窗口说话

**仍限单实例**（有意降级，非缺陷）：
- 流式片段续传 → 多实例下 resume 退化为权威快照（前端仍拿到完整文本）
- 群发言频率 → 实际限额约为单实例 × 副本数（宽松方向）

### nginx 必须关闭 buffering

`deploy/nginx.conf` 里 `/v1/` 段设了 `proxy_buffering off` 与 300 秒超时。
不关的话 SSE 会被 nginx 攒成一整段，流式就失效了。
实测：delta 每 50ms 到达一次，26 个事件总耗时 1.3 秒。

### worker 保持单副本

outbox handler 里有非幂等副作用（记忆提取会写 Episode/Assertion）。
`SELECT ... FOR UPDATE SKIP LOCKED` 能防重复领取，但处理中途失败重试
仍可能重复写入。需要扩容时先让每个 handler 幂等。

### 部署踩过的坑（都已修）

| 现象 | 原因 | 修复 |
| --- | --- | --- |
| 启动后 500 `column "attempt" does not exist` | 镜像里没有 `alembic.ini`，迁移器检测不到修订，退化成 `create_all`（不会给已有表加列） | Dockerfile 补 `COPY alembic.ini`；设 `LING_PROJECT_ROOT=/app` |
| 迁移器找不到 `migrations/` | 镜像里包进 site-packages 后 `__file__.parents[2]` 指向 Python 安装目录 | 按 `LING_PROJECT_ROOT` → cwd → 包位置依次探测 |
| `SyntaxError: source code string cannot contain null bytes` | macOS tar 打包带上 `._xxx.py`（AppleDouble），Alembic 当迁移脚本加载 | `.dockerignore` 排除 + 迁移器跳过 `._` 与空文件 + 打包用 `COPYFILE_DISABLE=1` |

## 6. 接入 QQ（OneBot V11）

OneBot V11 是 NapCat / Lagrange / go-cqhttp 共同实现的协议。ling 提供
`POST /v1/onebot/events` 作为**事件上报地址**。

### 6.1 配置

```bash
# deploy/.env
LING_ONEBOT_API_BASE=http://127.0.0.1:5700   # OneBot 实现的 HTTP API
LING_ONEBOT_ACCESS_TOKEN=<与 NapCat 一致>
LING_ONEBOT_SECRET=<与 NapCat 一致，用于校验上报签名>
LING_ONEBOT_SELF_ID=<机器人 QQ 号>
LING_ONEBOT_INSTANCE_ID=onebot-main
```

NapCat 侧要开：
- **HTTP 服务器**：端口 5700（对应 `LING_ONEBOT_API_BASE`）
- **HTTP 上报（反向 HTTP）**：地址填 `http://<ling 地址>:8080/v1/onebot/events`
- 上报的 **secret / token** 与上面两项保持一致

### 6.1.1 谁能跟她说话（出入许可）

陌生 QQ 号原来会走完整个流程——建窗口、写消息、跑决策、调模型——
才可能被发现不该理。现在在解析完信封、**任何写库之前**就判定。

```bash
LING_ACCESS_MODE=whitelist        # off | whitelist | blacklist，默认 off
LING_ACCESS_LIST=10001       # 逗号分隔的 QQ 号
```

- 默认 `off`：与改动前行为一致，不会悄悄改变既有部署的表现
- 名单按**字符串**比较——QQ 号可能有前导零，转 int 会丢，于是"加了白名单却对不上"
- 配置写错（如 `white-list`）**按拒绝处理**：宁可漏接，不要因为一个拼写错误
  把所有陌生人放进来
- 被拒时返回 200 + `accepted:false`：拒绝不是服务出错，非 200 会让 OneBot
  反复重投同一条事件
- 名单外的人**不建窗口、不留消息**，只在日志里留一行便于核对

### 6.1.2 其它运行时开关

```bash
LING_USER_TIMEZONE=Asia/Shanghai  # 勿扰时段、每日限额按当地作息算
LING_CONTEXT_BUDGET_SCALE=1.0     # 整体缩放上下文预算（0.5 = 25k token）
LING_ONEBOT_PROACTIVE=0           # 主动推送能力位（平台权限探测结果，不是偏好）
```

上下文预算默认合计约 **50k token**（车道 38k + 最近原文层 20k）。
模型窗口装不下就调 `LING_CONTEXT_BUDGET_SCALE`，不必改代码。

### 6.1.3 NapCat 侧：必须配「HTTP 上报」（实测跑通）

ling 的 OneBot 连接器是 **HTTP-only**（`connectors/onebot/` 里没有 WebSocket 实现），
所以 NapCat 侧建议用 **HTTP 上报（httpClients）**，或在 ling 支持的服务端模式下反向连入。

在 NapCat 的 `onebot11_<机器人QQ>.json` 里：

```json
{
  "network": {
    "httpServers": [{ "name": "http-local", "enable": true, "port": 3000, "token": "" }],
    "httpClients": [
      {
        "name": "ling",
        "enable": true,
        "url": "http://<ling 所在机器>:8080/v1/onebot/events",
        "messagePostFormat": "array",
        "reportSelfMessage": false,
        "token": "",
        "heartInterval": 30000
      }
    ]
  }
}
```

三个容易踩的点：

1. **`LING_HOST` 必须是 `0.0.0.0`**。只绑 `127.0.0.1` 时 NapCat 连不上
   （实测：同一局域网内 `readyz` 直接 000）。
2. **`LING_ONEBOT_INSTANCE_ID` 要与绑定账号时用的实例名一致**。
   账号绑定按 instance 存，写错会「事件进来了但解析不到身份」。
3. **`X-Self-ID` 必须匹配**。配了 `LING_ONEBOT_SELF_ID` 之后，
   上报不带或带错这个头会被直接拒绝（防止别的实例误配到同一入口）。

连通后应该看到这样的记录（数值为示例）：

```
NapCat 日志：HTTP上报服务: http://<ling-host>:8080/v1/onebot/events, : 已启动
ling 响应：  {"accepted":true,"decision":"reply","delivered":true,
             "parts_total":2,"parts_sent":2}
NapCat 日志：<bot> | 发送 -> 私聊 (10001) 第一条测试消息
             <bot> | 发送 -> 私聊 (10001) 第二条测试消息
回执：        state=delivered platform_msg_id=1000000001 / 1000000002
```

**注意**：NapCat 的 httpClient 只在有**消息事件**时上报，不推心跳。
所以「配好之后 ling 一直没动静」不等于配错了——要有人给机器人发一条消息才算通。

### 6.2 绑定账号（**前置条件**）

OneBot 事件要先解析成自然人。**未绑定会被直接拒绝**（不猜身份）：

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8080/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"ling","password":"<你的密码>"}' -c /tmp/c.txt >/dev/null && echo ok)

# 绑定（proof=pending：先建立关系，不能用于身份解析）
curl -b /tmp/c.txt -X POST http://127.0.0.1:8080/v1/accounts/bind \
  -H 'Content-Type: application/json' \
  -d '{"connector_instance":"onebot-main","external_account_id":"<QQ号>"}'

# 确认账号确实属于本人后，升级为已证明（此后才能解析身份）
curl -b /tmp/c.txt -X POST http://127.0.0.1:8080/v1/accounts/<account_id>/prove
```

`proof=pending` 只能建关系、不能解析身份——否则"自称某账号"就能拿到那个人的记忆。

### 6.3 验证

```bash
# OneBot 连通性（调 get_login_info）
curl http://127.0.0.1:8080/v1/onebot/health

# 在 QQ 里给机器人发一条私聊，应收到回复；
# 服务器侧可核对：
curl -b /tmp/c.txt http://127.0.0.1:8080/v1/accounts   # 绑定状态
```

事件上报**业务失败也返回 200**（否则 OneBot 会反复重投），
验签失败返回 403。重复事件按 `(实例, message_id)` 去重，只处理一次。

### 6.4 QQ 官方机器人（QQ 开放平台 / QQ Bot）

这是与 OneBot **平行的第二条接入路径**：直接用 QQ 开放平台的官方接口，
不需要 NapCat。两者共用同一套入站受理（白名单、去重、身份、决策、投递），
差别只在协议层。

**先读这条**：官方接口和 NapCat 不是一回事，下面三件事在协议层就受限，
不是 ling 没实现——

| 限制 | 实际含义 |
| --- | --- |
| **只能被动回复** | 回复必须带用户那条消息的 `msg_id`。官方数值（已核实）：**单聊 60 分钟内 4 次，群聊 5 分钟内 5 次**；`msg_id + msg_seq` 相同会重复失败。窗口过期或用尽后再发平台会拒绝，ling 会如实报失败，不会假装送达 |
| **主动推送要单独申请** | 想让她主动找你（早安、提醒），必须在开放平台申请"主动消息"权限。没有权限时 `supports_proactive=false`，主动投递会被门控**拒绝并说明原因** |
| **openid ≠ QQ 号** | 事件里只有 `author.member_openid` / `author.user_openid`。同一个人在不同机器人下 openid 不同，也**拿不到** QQ 号。因此白名单、账号绑定都要用 openid |

> 本仓库没有任何真实 app_id/secret，**未与真实平台联调**。协议细节按官方文档实现，
> 标注为「未核实」的几处在下面 6.4.4 列明。

#### 6.4.0 收事件用 **WebSocket 网关**，不要用 Webhook（默认）

官方有两种收事件的方式，**这里默认并推荐 WebSocket**：

| | Webhook | WebSocket 网关 |
| --- | --- | --- |
| 要求 | 腾讯要能访问你的**公网 HTTPS** 回调地址 | ling 主动连出去 |
| 家里/NAS 内网 | ❌ 走不通（没公网 IP/域名/证书） | ✅ 直接可用 |
| 发消息 | 官方文档要求"机器人需连到 WebSocket 保持在线"，纯 Webhook 发不出去 | ✅ |

网关模式**不需要公网、不需要备案域名、也不需要任何扫码**——只需要
`AppID` + `AppSecret` 两个票据。

协议按官方文档 `api-v2 → 事件订阅与通知 → WebSocket 方式` 实现：

```
GET  {base}/gateway                    → {"url": "wss://api.bot.qq.com/websocket/"}
连接后 ← op=10 Hello {heartbeat_interval}
发   → op=2 Identify {token: "QQBot <access_token>", intents: 1<<25, shard:[0,1]}
收   ← op=0 t=READY {session_id}       此后每帧的 s 是序号
定时 → op=1 心跳 {d: 最近一次 s}
重连 → op=6 Resume {session_id, seq}   平台补发漏掉的事件
```

`intents = 1 << 25`（`GROUP_AND_C2C_EVENT`）覆盖 `C2C_MESSAGE_CREATE` 与
`GROUP_AT_MESSAGE_CREATE`——也就是单聊与群里 @ 她。

断线按关闭码处理：`4007/4006` 与 `4900~4913` 重新 Identify，`4008/4009`
走 Resume，`4914/4915`（只允许沙箱 / 已封禁）**停止重连**并报错。

#### 6.4.1 平台侧要做什么

1. 在 [QQ 开放平台](https://q.qq.com/) 创建机器人应用，进入
   **开发设置**，记下 `AppID` 与 `AppSecret`（= ling 的 `LING_QQBOT_CLIENT_SECRET`）
   以及 `BotSecret`（Webhook 验签用）。
2. **添加沙箱/正式环境的测试用户**：沙箱环境下只有名单里的人能跟机器人对话，
   不加人就会"发了没反应"。
3. **开通能力**：私聊（C2C）默认有；群聊需要在"功能配置"里开通
   （未开通时群事件根本不会推过来）。
4. **事件订阅**：在"开发设置 → 回调地址"填
   `https://<你的域名>/v1/qqbot/events`，并勾选消息事件：
   - `C2C_MESSAGE_CREATE`（私聊）
   - `GROUP_AT_MESSAGE_CREATE`（群里 @ 机器人）
   保存时平台会立刻回调一次 `op=13` 做**回调地址校验**，ling 必须在线，
   否则配置保存不成功。
5. 回调地址必须是 **HTTPS 且公网可达**（官方要求）。本机开发用内网穿透，
   并把穿透域名填进去。

> 频道（guild）产品线的事件（`AT_MESSAGE_CREATE` / `DIRECT_MESSAGE_CREATE`）
> **暂不支持**：它的窗口与发送语义和 QQ 群/私聊不同，当前会明确返回
> `unsupported`，不会静默丢弃。

#### 6.4.2 ling 侧配什么

```bash
# deploy/.env
LING_QQBOT_ENABLED=1
LING_QQBOT_APP_ID=<开放平台的 AppID>
LING_QQBOT_CLIENT_SECRET=<AppSecret>
LING_QQBOT_BOT_SECRET=<BotSecret，Webhook 验签密钥>
LING_QQBOT_INSTANCE_ID=qqbot-main        # 账号绑定按它隔离，别和 onebot 混用
LING_QQBOT_SANDBOX=1                     # 沙箱=1；上正式环境并过审后改 0
LING_QQBOT_SUPPORTS_GROUP=1              # 平台侧开通了群聊才设 1
LING_QQBOT_PROACTIVE=0                   # 平台权限探测结果，不是偏好开关
```

可选：

```bash
LING_QQBOT_API_BASE=            # 显式覆盖 API 基址（自建代理/平台改域名）
LING_QQBOT_TOKEN_URL=           # 覆盖换 token 的端点
LING_QQBOT_WEBHOOK_PUBLIC_KEY=  # 直接给 Ed25519 公钥（hex），跳过由 BotSecret 派生
LING_QQBOT_ALLOW_UNSIGNED_VALIDATION=0  # 平台不给 op=13 签名时才开
```

**没有 `LING_QQBOT_BOT_SECRET` 就收不到任何消息**：没有密钥无法确认来源，
验签一律失败并返回 403（不会"宽松通过"）。

账号绑定与 OneBot 相同，只是 `external_account_id` 填 **openid**，
`connector_instance` 填 `LING_QQBOT_INSTANCE_ID`：

```bash
curl -b /tmp/c.txt -X POST http://127.0.0.1:8080/v1/accounts/bind \
  -H 'Content-Type: application/json' \
  -d '{"connector_instance":"qqbot-main","external_account_id":"<openid>"}'
```

那么 openid 从哪来？**只能从事件里抄**：给机器人发一条消息，在
ling 日志里找 `拒绝入站：sender=<openid>`（白名单模式下）或直接看
`account_ref`。拿不到 openid 时不要猜 QQ 号——那会让身份绑到错误的行。

#### 6.4.3 如何验证接通（实测步骤）

本仓库只验证了协议层（用 `httpx.MockTransport` 模拟平台）。对上真实平台时按下面顺序走，
**每一步都有可核对的结果**，不要跳步：

1. **凭据能不能换到 token**（不涉及消息权限）：

   ```bash
   curl -s http://127.0.0.1:8080/v1/qqbot/health | python -m json.tool
   ```

   期望 `ok: true`，`detail` 里写着 `鉴权通过 ... 消息收发权限未验证`。
   `ok: false` 时 `detail` 会给出确切原因（缺凭据 / 401 / 不可达），按它排查。

2. **回调地址校验能不能过**：在开放平台点保存回调地址。
   - 成功：平台提示配置成功
   - 失败：ling 日志里会有一条 `QQ 机器人回调验签失败（op=13）` 或
     `无法完成回调地址校验：未配置 bot_secret`。前者说明 BotSecret 与平台不一致，
     后者说明根本没配。

3. **私聊一句，看是否真的发出去**：在 QQ 里给机器人发一条私聊，然后核对
   ling 侧回执（这一条最关键——只有拿到平台返回的消息 id 才算成功）：

   ```bash
   psql "$LING_DATABASE_URL" -c \
     "select channel, state, platform_message_id, detail from delivery_receipts
      where channel = 'qqbot' order by observed_at desc limit 5;"
   ```

   期望 `state = sent` 且 `platform_message_id` 非空。
   若 `state = uncertain`：平台超时/5xx，**不要重发**，按对账流程处理。
   若 `state = failed` + `detail` 含 `40034`/`msg_id`：被动回复窗口过期或用尽，
   属于平台限制，不是配置错。

4. **群里 @ 一次**：只有 `GROUP_AT_MESSAGE_CREATE` 会推过来（不 @ 的群消息
   官方不推）。收到即说明群聊能力已开通。

5. **多段回复要注意**：一条长回复会被拆成多条发送，每条都要各自带 `msg_id`
   与递增的 `msg_seq`。如果平台返回"重复回复"类错误，说明 `msg_seq` 没递增——
   这是连接器的 bug，不是配置问题。

#### 6.4.4 已知限制与「未核实」项

诚实清单（写在这里，避免以后有人把"没验证过"当成"验证过"):

- **未与真实平台联调**：本仓库没有真实凭据，全部按官方文档实现 + MockTransport 验证。
- **单条文本长度上限未核实**：官方未给稳定数字，默认按 1500 字符分片
  （`connectors/qqbot/settings.py: max_text_length`），部署时按实测调整。
- **被动回复的次数与时效已核实**（官方「消息收发概述 → 频率与时效规则」）：
  单聊 **60 分钟 / 4 次**、群聊 **5 分钟 / 5 次**，代码按场景分别计
  （`passive_quota()`）。窗口过期或用尽后按主动推送处理（默认拒绝）。
- **BotSecret → Ed25519 私钥的补齐规则未核实**：实现为"循环补足到 32 字节后截断"。
  若平台验签一直失败（现象：所有回调都 403），改用 `LING_QQBOT_WEBHOOK_PUBLIC_KEY`
  直接给公钥绕过派生。
- **换 token 的域名已核实**（官方 api-v2「获取访问凭证」）：
  `POST https://api.bot.qq.com/app/getAppAccessToken`，body 为
  `{"appId": ..., "clientSecret": ...}`；⚠️ 失败时 HTTP 仍是 200，必须按
  响应体的 `code` 判定（100007/100016/10004 是凭据或机器人状态问题）。
  平台改域名时用 `LING_QQBOT_TOKEN_URL` 覆盖。
- **被动窗口缓存在进程内存里**：多副本部署时，若回复由**另一个副本**发出，
  那个副本不知道 msg_id，会把这条当成主动推送并拒绝（`unsupported` + 原因），
  **不会**静默发丢。要么让 API 保持单副本，要么等后续把窗口落库。
- **富媒体（图片/语音）发送未实现**：`supports_media=false`，带媒体的投递会被
  明确拒绝而不是"发文字、丢图片、照样记成功"。入站附件下载已实现（https）。
- **频道（guild）产品线未接入**。
- **对账能力有限**：官方没有按消息 id 查投递状态的公开接口，`query_receipt`
  只能返回 `uncertain` 并说明原因，**不要**据此重发。

## 8. 无人值守：定时备份与运维告警

个人服务器长期没人看着，"有日志、有 healthcheck" 不足以保证你会第一时间
知道故障。这一节是让"有事能知道"成立的最小配置。

### 8.1 定时备份

```bash
# 安装每日 04:30 备份（幂等，可重复执行）
deploy/backup/install-cron.sh /mnt/backup/ling 04:30
crontab -l | grep backup.sh    # 确认已装
```

**关键**：宿主机 cron 里**没有 DSN**，数据库也只在本 compose 内网
（`compose.yaml` 有意不发布 db 端口）。直接跑 `pg_dump` 必然失败——
"已安装每日备份"其实一份都备不出来。

所以备份脚本有 compose 模式，通过 `docker compose exec` 进 db 容器导出：

```bash
# 手动验证一次
LING_BACKUP_VIA_COMPOSE=1 \
LING_COMPOSE_PROJECT=ling \
LING_COMPOSE_FILE=$(pwd)/deploy/compose.yaml \
deploy/backup/backup.sh /mnt/backup/ling
```

`LING_COMPOSE_PROJECT` 不能省：`docker compose -f 路径` 会按**文件所在目录名**
推导项目名（这里是 `deploy`），而 `install.sh` 默认用 `-p ling`；名字不一致时
`exec` 找不到正在运行的容器，备份会失败。用 `--project` 改过项目名的话，
这里填那个名字。

产物五件：`ling-YYYYmmdd-HHMMSS.dump`、`manifest-YYYYmmdd-HHMMSS.txt`、
`tombstones-YYYYmmdd-HHMMSS.txt`（无撤回记录时为空）、`keys/root.key`
（根密钥）、`media-YYYYmmdd-HHMMSS.tar`（`/data/media` 的实体文件）。
后两者来自 `app-data` **命名卷**，宿主机上不能直接 `cp`，由 `backup.sh` 通过
`docker compose exec -T api` 在容器内 `tar`/`cat` 流式取回；卷里没有对应内容时
manifest 会**显式**写 `keys_root=absent` / `media=absent`，不会静默跳过。
`manifest-*.txt` 同时记录四份产物的 `sha256`（`dump_sha256` /
`tombstones_sha256` / `keys_root_sha256` / `media_sha256`）。其中
`restore_drill.sh` 只核对 **dump / 墓碑 / 媒体** 三份；**keys 的哈希不在演练
脚本的校验范围内**——自动核对它的是升级流程的备份校验（`deploy/update/executor.py`
的 `_verify_backup_keys`，且只针对本次升级新建的那份备份目录），cron/历史备份
目录里的 `keys/root.key` 需要**人工**按 manifest 核对（理由见下面几段）。
备份**失败**（读取根密钥**出错**——区别于"文件不存在"，后者按 `absent` 记录且
exit 0；媒体归档失败或不完整）时 `backup.sh` 非零退出，并删掉**本次**已产出的
片段（dump / 墓碑 / 媒体 / `version-*.txt`）——不会留下一个"看起来像备份"的
残目录。cron 里表现为 `backup.log` 有错误、当天没有新 manifest。

`keys/root.key` 是这条清理规则里**唯一特殊**的一件：它**没有日期戳**，而 cron
每天写的是**同一个**备份目录，所以这个路径同时就是"前一天那份成功备份的密钥
归档"。清理逻辑因此按**"宁可留垃圾，绝不删历史密钥"**处理：

- 本次新建的（目录里原本没有）→ 失败时删掉；
- 本次**覆盖过**历史那份的 → 先用归档前存下的 `keys/.root.key.prev.<pid>` 把
  **原字节**放回原位；连暂存副本都移不回去，就**保留当前文件**并在 `backup.log`
  里告警，绝不 `rm`（删掉是不可逆的能力损失，而残留的 dump/墓碑没有 manifest
  就不会被 `restore_drill.sh` 当成有效备份）；
- 在密钥步骤之前就失败（例如 `pg_dump` 失败）→ 历史密钥一根汗毛都不动。

所以**失败绝不会让前几天的 `manifest-*.txt` 失去它的 `keys/root.key`**。但要注意
另一半：**成功**的那一天本来就会覆盖 `keys/root.key`（新 manifest 记新哈希），
备份目录里始终只有**一份**当前密钥，于是旧 manifest 的 `keys_root_sha256` 只对得
上"最近一次含密钥的成功备份"。需要长期留存的密钥请**单独归档**（见下文"必须与
dump 分开保管"），不要指望备份目录里留着多天的密钥副本。

> 上面这条要按字面理解：**回填失败**那一档里 `keys/root.key` **文件确实还在**，
> 但内容已经是**这次**那把，哈希可能和旧 manifest 里的 `keys_root_sha256` 对不上。
> 判断以 manifest 记的哈希为准（**不要只看文件在不在**），必要时人工核对，或从同
> 目录的 `keys/.root.key.prev.<pid>` 把原字节取回去。自动校验里核对 keys 哈希的是
> **升级流程的备份校验**（`deploy/update/executor.py` 的 `_verify_backup_keys`：
> `keys_root_sha256` 必须等于 `keys/root.key` 的实际字节，否则 `backup_failed`）；
> `restore_drill.sh` **不**校验 keys——它只核对 dump / 墓碑 / 媒体三份哈希，遇到这种
> 目录会**放过**，所以判断仍要人工按 manifest 的哈希核对。

`keys/.root.key.prev.<pid>` 与临时文件 `.root.key.part.<pid>` 都是**创建即 0600**
（`backup.sh` 在 `umask 077` 的子 shell 里做重定向/`cp`）：先按默认 umask 落一个
0644 再 `chmod 600` 的写法会在两步之间留一个"同机其他用户可读根密钥字节"的窗口，
cron 路径的备份目录本来就是 0755，所以这件事不是理论风险。**这个 "创建即 0600"
只覆盖密钥类临时文件**——媒体临时文件 `.media-<STAMP>.tar.part` 在流式写入期间是
**0644**（默认 umask 022 下；`backup.sh` 的 compose 媒体段由 `docker compose exec`
的外层重定向创建，直接媒体段由本机 `tar` 自建），要到 `chmod 600` 才收紧：
compose 模式先 `mv` 成正式名、之后才 `chmod 600`，中间还有极短一段也是 0644；
直接模式则是先 `chmod 600` 再 `mv`，没有这段尾巴。这是**既有行为**（先前版本
就是这样，尚未收紧）；它承载的是媒体字节（用户上传的图/语音），不是凭据，所以
没有按根密钥那套处理——同机可读的风险仍按下面"把备份的父目录自己收紧"来兜。

（墓碑导出失败是唯一**不**非零退出的情形：脚本仍 exit 0，但 manifest 里
`tombstones_state=FAILED`，执行器与 `restore_drill.sh` 都会据此拒绝这份备份。）

> **项目名已经写进 crontab 行**：`deploy/backup/install-cron.sh`
> 会把 `LING_COMPOSE_PROJECT` 连同 `LING_BACKUP_VIA_COMPOSE`、`LING_COMPOSE_FILE`、
> `LING_BACKUP_DIR` 一起显式写进那一行——cron 的环境里没有你 shell 里的变量可继承。
> 项目名的默认值**从 `deploy/install.sh` 的 `PROJECT=` 行解析**（与安装器用的
> `--project-name` 同源，脚本不写死 `ling`）；用 `--project` 改过项目名的，装定时
> 任务前先 `export LING_COMPOSE_PROJECT=<那个名字>`（环境变量优先于解析值）。
> 解析不到默认值、或名字不是合法的 Compose 项目名（`[a-z0-9][a-z0-9_-]*`）时，
> 脚本直接报错退出，不会写一行错的 cron。
>
> 重装是幂等的：脚本先删掉含同一个 `backup.sh` 路径的旧行（包括历史版本没带项目
> 名的行）再追加当前行。装完用 `crontab -l | grep backup.sh` 确认那一行确实带着
> `LING_COMPOSE_PROJECT='…'`，再 `tail -f /mnt/backup/ling/backup.log` 看第一次
> 是否真的产出了 dump——**“已安装每日备份”不等于“备出来了”**。

**备份目录必须与数据库所在磁盘分开**（异机 / 网盘 / 外置盘）。
放同一块盘上，磁盘坏了备份和原文一起丢。

`manifest-*.txt` 里的 `keys_root=` 记录这次到底有没有拿到根密钥：

- `keys_root=present` → 文件在 `keys/root.key`（0600），**必须与 dump 分开保管**；
- `keys_root=absent` + `keys_root_source=env` → 密钥来自 `deploy/.env` 的
  `LING_ROOT_KEY`，没有落盘文件，请把那份 env 安全保管；
- `keys_root=absent` + `keys_root_source=none` → 这次没有根密钥（还没生成过）。
  恢复到新卷后，库里已有的 provider 凭据**全部解不开**，只能重新录入。

> **`keys_root_source` 说的是"这次备份从哪里拿到了密钥字节"，不是"应用实际用了
> 哪一把"**。`src/ling/infra/security.py` 是 **`LING_ROOT_KEY` 优先、文件其次**：
> 两者都存在时，应用用环境变量那把，而 manifest 仍会写 `keys_root=present` +
> `keys_root_source=file`（因为文件确实被归档了）。为了不让人误以为"只恢复
> `keys/root.key` 就够了"，这种情况下脚本另记 `keys_env_set=true` 并在 stderr
> 打一行警告。**恢复时按 `keys_env_set` 判断**：`true` 就必须把 `deploy/.env` 的
> `LING_ROOT_KEY` 一起恢复；`false` 才是"`keys/root.key` 就是生效密钥"。
>
> **这两条来源不是同一把密钥，也没法互相核对**：`security.py` 对
> `LING_ROOT_KEY` 走 `sha256(文本)` 派生子密钥，对 `keys/root.key` 走
> `base64` 解码——同一串字符经两条路得到**不同**的密钥字节，`cmp`/哈希比对一定
> 不等。所以 `keys_env_set=true` 的部署**不能**靠"确认文件里那把和 env 那把一样"
> 来省事：要么把那份 `deploy/.env`（或 `LING_ROOT_KEY` 的值）一起恢复，要么恢复
> 后重新录入 provider 凭据。判断依据只有 manifest 的 `keys_env_set`，不是把两个
> 值拿出来比一比。

`media-YYYYmmdd-HHMMSS.tar` 是 `/data/media` 的**全量**副本。两点要有心理准备：

- **空间按天线性累积**：每次备份（含每日 cron）都完整复制一份媒体，本项目
  **不实现**保留/轮转策略，脚本也**不删**任何历史备份。先
  `du -sh <部署目录>/data/media`（或 `docker compose exec -T api du -sh /data/media`）
  估一份的大小，再定盘的容量和轮转办法（例如按日期戳清理旧目录）。
- **耗时也随媒体体积增长**：执行器里 `BACKUP_TIMEOUT_SECONDS=1800`（`deploy/update/
  executor.py`）现在是**含全量媒体打包**的总预算；媒体卷几十 GB / 慢盘时备份会
  超时 → `backup_failed` → 升级被阻断（fail closed，不丢数据但要重跑）。这种部署
  应先把媒体轮转掉、或调大该上限。

**"看着成功"和"真的备全"是两件事，而且告警不在界面里**。自助更新（§10）在备份后
会用 manifest 做三层校验，其中"备份通过但**不完整 / 未能校验**"的情形**不会**出现
在 Web 界面或 API 里——任务状态是固定码表（`src/ling/updates/protocol.py` 的
`ERROR_MESSAGES`，宿主不转发自由文本），所以 `media_state=partial` 这种"成功但不
完整"的任务在 UI 上完全看不出差别；每日 cron 备份则连任务都没有。要自己读：

- `<state_dir>/backups/<id>/verify-warnings.txt`（0600，每行以 `[backup-verify] `
  开头；`<state_dir>` 是执行器配置 `[update] state_dir`，默认 `/var/lib/ling-update`）；
- `<state_dir>/logs/<id>-backup.log`：同一条告警也会追加进备份日志（`backup.sh`
  的全部输出也落在这里）；
- 更新失败 / 中断时的 `<state_dir>/recovery-<id>.txt`：**第 8 条**就指向上面这个
  文件，恢复前必须读。

典型内容：`media_state=partial`（归档时有文件正在写入，可能漏掉刚上传的图/语音）、
`keys_root=absent`（没有根密钥，恢复后凭据解不开）、`keys_env_set=true`（必须同时
恢复 `deploy/.env`）、旧格式 manifest 的"未能校验"。**升级成功也要 `tail` 一下**
——`succeeded` 只表示"这一次升级完成了"，不表示"备份完整"。

**既有权限口径（脚本有意不改，避免影响已部署的目录）**：cron 路径下备份目录由
`mkdir -p` 创建，权限是 **0755**（受 umask 影响）；`dump` / `manifest` /
`tombstones` 写出来是 **0644**，只有 `media-*.tar` 与 `keys/root.key` 显式
`chmod 600`。也就是说 dump 里是业务数据、同机其它用户可读——请把备份的**父目录**
自己收紧（例如 `chmod 700 /mnt/backup/ling`，或建在宿主私有目录下），不要放在
`/tmp`、Web 根目录或共享盘的可浏览位置。执行器自己的备份目录是 `0700`
（`state_dir` 也是 `0700`），不受这条影响。需要更严就自己 `chmod`：脚本不会替你
改（改了会让既有部署上其他属主跑的 cron 写入失败）。

数据库里的 provider 凭据是加密的，根密钥丢了就全部解不开——表现为"机器人突然
连不上模型"，很难第一时间想到是密钥问题。恢复顺序见
[§4「恢复顺序」](#恢复顺序先-db--再-rootkey-与-media--最后起服务)。

### 8.2 运维状态端点

```bash
# 端口按 deploy/.env 的 LING_EDGE_PORT（安装器默认 18080，env.example 示例 8080）
curl -b cookies.txt "http://127.0.0.1:${LING_EDGE_PORT:-8080}/v1/health/ops"
```

返回每个检查项的 `severity` 与 `action`（只有出问题时才给建议）：

| 检查项 | 什么情况下报警 |
| --- | --- |
| `outbox_pending` | 积压 ≥50 条，或最老一条超过 30 分钟（worker 可能停了） |
| `outbox_dead` | 有事件连续失败转入 dead（派生记忆会一直缺席） |
| `stuck_turns` | 有轮次超过 15 分钟没推进 |
| `backup_age_hours` | 超过 48 小时没新备份，或未配置 `LING_BACKUP_DIR` |
| `disk_free_gb` | 可用空间低于 2GB（写满后数据库直接不可写） |

`status` 为 `ok` 或 `attention`。配 `LING_BACKUP_DIR` 后备份新鲜度才会被判为正常。

实测：未配备份时 `status=attention`（1 项告警），
配上并跑一次备份后 `status=ok`（0 项告警）。

### 8.3 恢复演练

```bash
LING_BACKUP_VIA_COMPOSE=1 \
LING_COMPOSE_PROJECT=ling \
LING_COMPOSE_FILE=$(pwd)/deploy/compose.yaml \
deploy/backup/restore_drill.sh /mnt/backup/ling/ling-YYYYmmdd-HHMMSS.dump ling_drill
```

目标库名必须以 `_drill` 结尾，脚本会拒绝覆盖生产库。compose 模式下第二个参数
传**库名**（如 `ling_drill`），不是 DSN——宿主机连不到库，演练也进容器做。

**本地 fake 演练的真实输出**（运行环境：本地 macOS + `bash 3.2`，
`docker` / `psql` / `pg_restore` 全部是**替身脚本**，数据是 3 行假数据、
2 条假 `media_ref`。**这不代表真实部署，也没有在真机上验证过**）：

```
[drill] 重建隔离库 ling_drill（容器 db）
[drill] 恢复 dump
[drill] 校验关键表
  users = 3
  windows = 3
  turns = 3
  messages = 3
  memories = 3
[drill] 校验墓碑仍在
  备份记录 2 条，恢复后 2 条
[drill] 校验产物哈希
  [drill] dump_sha256 一致（ling-20261031-033827.dump）
  [drill] tombstones_sha256 一致（tombstones-20261031-033827.txt）
  [drill] media_sha256 一致（media-20261031-033827.tar）
[drill] 校验媒体覆盖（media-20261031-033827.tar）
  media_ref 可核对 2 条，归档中缺 0 条，无法核对 0 条
[drill] 完成（隔离库 ling_drill 未触碰生产库，墓碑一致）
```

这份输出覆盖了**当前脚本的全部检查**：关键表计数、墓碑"备份时 vs 恢复后"的
逐条比对、三个产物的 sha256，以及 `media_ref` ↔ 媒体归档的覆盖检查。它可以证明
脚本逻辑与输出格式，但**不能**替代真机验证——尤其以下三件只有真机能证伪：

1. `docker compose exec` 是否可靠透传容器内退出码（脚本靠 `exit 3` 区分
   "文件/目录不存在"）；
2. `tar -cf -` 经 `docker compose exec` 的 stdout 是否字节干净（被多路复用头污染
   时会导致媒体永远备不出来）；
3. 真实大媒体卷下的耗时/断流，以及并发上传时 GNU tar 的 `exit 1 → partial` 行为。

> 历史提醒：本节早先贴过一份**重构前**的"实测"transcript，里面只有一行
> `[drill] 校验`，**不含**产物哈希与媒体覆盖两段——那与现在的脚本不一致，已删除。
> TASK59 新增的**备份侧根密钥归档（`backup.sh` 的 `keys/root.key`）**，以及**演练侧**的
> 媒体归档 / 产物哈希 / `media_ref` 覆盖检查，**尚无真机验证记录**；真机跑通后请把
> 真实输出替换到上面这段（并注明环境）。

### 8.4 多副本下的配置变更

默认部署是 2 个 API 副本 + 1 个 worker。密钥缓存是**进程内存**，
所以配置变更需要每个进程各自刷新，否则会出现"有的请求成功、有的报
`secret_missing`，worker 的记忆提炼静默退回正则"。

实现方式是**按配置修订号懒刷新**：每个进程记住自己加载到哪一版，
处理请求前比对一次（一个很小的查询），变了就重载。worker 在每次取任务前比对。

**实测验证**（本地双实例共享同一库）：

| 步骤 | 结果 |
| --- | --- |
| 在 A 上新建 provider 并写入（假）密钥、发布 | A/B 都返回 `unauthorized` |
| 同样的 provider 但**不写密钥** | 返回 `secret_missing` |

区别本身就是证据：`unauthorized` 说明密钥被解密并真的拿去请求了，
`secret_missing` 说明根本没有密钥。B 从未参与发布，却拿到了运行期
新增 provider 的密钥——传播生效。

### 8.5 告警推送

`/v1/health/ops` 解决"能查"，但不解决"会知道"——没人天天盯仪表盘。
配一个 webhook 就能主动推：

```bash
export LING_ALERT_WEBHOOK_URL=https://example.com/hook
```

内容是通用的 POST JSON，任意能收 HTTP 的服务都能用
（Telegram Bot API、Discord、钉钉/飞书机器人、ntfy、自建脚本）：

```json
{
  "text": "ling 需要处理：\n· backup_age_hours = None — 超过 48 小时没有新备份…",
  "status": "attention",
  "warnings": [{"name": "backup_age_hours", "severity": "warn", "action": "…"}],
  "source": "ling-worker"
}
```

worker 每 5 分钟检查一次（比小时级的维护任务密得多——"它不回话了"要尽快知道）。

**三条设计约束**：

| 约束 | 为什么 |
| --- | --- |
| 只推 `severity != ok` 的项 | 心跳推送会让人开始忽略通知，真出事那条就淹在噪声里 |
| 同一告警 30 分钟内只推一次 | 否则 worker 挂一小时推几百条，渠道直接废掉 |
| 恢复后**删除**去重状态 | 不然下次再出问题推不出来 |

手动验证一次（不用等 worker）：

```bash
curl -b cookies.txt -X POST http://127.0.0.1:8080/v1/health/ops/notify
```

**实测完整闭环**：未配备份 → worker 推送成功并记录 `count=1`、
再次触发被正确去重 → 配上备份后 `status=ok` 且去重状态清零
（下次出事能立刻再推）。

### 8.6 dead 事件的处理

连续失败 5 次的事件转入 `dead` 而不是静默丢弃。修好原因后重放：

```python
from ling.worker import replay_dead_events
await replay_dead_events()   # 把 dead 重新入队
```

**不做自动重放**：连续失败通常意味着规则或数据有问题，
自动重放只会反复失败刷日志。

## 9. 访问边界：什么时候必须上 TLS

先把场景说清楚，避免两个方向的错：

| 你怎么访问 | 需要 TLS 吗 |
| --- | --- |
| 只在同一台机器 / 可信 VPN / SSH 隧道内 | **不必须**（默认 `nginx.conf` 明文即可） |
| 手机在外网直连、或暴露到公网 | **必须** |

判断标准不是"是不是自己的服务器"，而是**登录凭证与私聊内容会不会经过不可信网络**。

### 9.1 启用 TLS

```bash
# 1) 生成自签证书（内部使用足够；浏览器会提示不受信任）
mkdir -p deploy/certs
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout deploy/certs/privkey.pem -out deploy/certs/fullchain.pem \
  -subj "/CN=ling.local"

# 2) 切到 TLS 配置并打开 cookie Secure
export LING_EDGE_CONF=./nginx-tls.conf
export LING_EDGE_CERTS=./certs
export LING_COOKIE_SECURE=1        # 少了这一条，凭证仍可能走明文
docker compose -f deploy/compose.yaml up -d
```

`LING_COOKIE_SECURE=1` 不是可选项：上了 TLS 但 cookie 不带 Secure，
浏览器仍可能在明文请求里带上会话 cookie。`scripts/check_compose.py`
会在检测到 443 端口时强制要求 `env.example` 里有这一项。

### 9.2 证书用 certbot 签发

`nginx-tls.conf` 已放行 `/.well-known/acme-challenge/`，
把 webroot 指到 `/var/www/certbot` 即可用 certbot 的 webroot 模式签发。

### 9.3 TLS 下的 SSE

`proxy_buffering off` 在 TLS 配置里同样必须存在，否则流式回复会被攒成整段。

**实测**（远端 Docker，自签证书，上游每 100ms 发一条）：

```
 0.006s data: tick-0
 0.107s data: tick-1
 0.207s data: tick-2
 0.308s data: tick-3
 0.408s data: tick-4
 0.509s data: tick-5
```

到达节奏与上游一致，说明 TLS 层没有缓冲。同时确认：HTTP/2 协商成功、
HSTS 头存在、TLS 1.1 握手被拒。

## 10. 自助更新（默认关闭）

> 本节只讲部署边界；完整管线、宿主执行器命令、失败语义与人工恢复步骤见
> [UPDATES.md](UPDATES.md)。真实 Docker 演练尚未完成，当前**不要**把它当成
> 已验证的自动升级能力。

`api` 容器本身没有任何 Docker 权限；它只能往宿主共享的 requests 目录写一条
固定 JSON，真正的构建/切换由宿主上的独立执行器完成。Compose 只给 **api** 注入
这三个变量，并挂两个目录：

```yaml
# api.environment
LING_UPDATE_ENABLED: ${LING_UPDATE_ENABLED:-0}          # 默认 0
LING_UPDATE_REQUESTS_DIR: /run/ling-update/requests
LING_UPDATE_STATUS_DIR: /run/ling-update/status
# api.volumes
${LING_UPDATE_REQUESTS_HOST:-../.run/update/requests}:/run/ling-update/requests:rw
${LING_UPDATE_STATUS_HOST:-../.run/update/status}:/run/ling-update/status:ro
```

`worker` 不挂这个通道、也不拿 `LING_UPDATE_*` 配置；Web 侧没有 Docker socket，
也不挂宿主源码树。默认关闭时 api 不读这两个目录。

宿主准备（**显式人工执行**，`install.sh` 不会替你启用）：

```bash
sudo cp deploy/update/config.example.toml /etc/ling-update.toml
sudo python3 deploy/update/executor.py --config /etc/ling-update.toml init
sudo python3 deploy/update/executor.py --config /etc/ling-update.toml doctor
sudo python3 deploy/update/executor.py --config /etc/ling-update.toml adopt-current
```

`init` 创建 requests（`0770`，group 为容器 uid 10001 的 gid）与 status
（`0755`，宿主写、容器只读），**不是 0777**；state 目录（锁、备份、journal）
不挂进任何容器。确认目录归属、备份与恢复步骤之后，才在 `deploy/.env` 里把
`LING_UPDATE_ENABLED` 改成 1，并让 api **重建**：

```bash
PROJECT=<project>
LIVE=<正在运行的那棵树>
ENV_FILE=<canonical env 的绝对路径>
OVERRIDE=<宿主持久 override 的绝对路径>     # 没有就留空

compose() {
  local -a args=(docker compose -p "$PROJECT" -f "$LIVE/deploy/compose.yaml")
  [ -n "$OVERRIDE" ] && args+=(-f "$OVERRIDE")
  args+=(--env-file "$ENV_FILE")
  "${args[@]}" "$@"
}

compose up -d --force-recreate api
```

`docker compose restart` **不会**重新读取 `.env`/环境变量，开关看起来改了、
容器里还是旧值。也不要从旧源码树裸跑 `docker compose`（相对 bind 会指回旧树）。

真实部署请把 `LING_UPDATE_REQUESTS_HOST` / `LING_UPDATE_STATUS_HOST` 显式设成
宿主绝对路径（如 `/run/ling-update/requests`），与执行器配置里的
`requests_dir` / `status_dir` 保持一致；仓库里的相对默认值只用于本机试验。
