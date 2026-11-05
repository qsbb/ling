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
# → http://127.0.0.1:8080（开发默认端口，与容器里的 6520 无关）
```

开发默认 `LING_TEST_MODE=1`，对话由确定性 fake provider 生成，界面显著标注
“测试模式：回复由确定性 fake provider 生成，非真实模型能力”。

## 2. Docker Compose（Linux）

> 本节及 §2 各小节描述的是**源码模式**（`install.sh` 在宿主构建当前 checkout）。
> 镜像模式（固定 OCI 摘要，宿主不需要源码树 / Node / pnpm）见
> [§11「镜像模式部署（推荐）」](#11-镜像模式部署推荐)；把现有源码部署切过去见
> [§12「从源码模式迁移到镜像模式」](#12-从源码模式迁移到镜像模式)。

```bash
cd /opt/ling
bash deploy/install.sh              # 一条命令：生成配置、构建、起服务、等就绪
# 结束时会打印实际控制台地址：新生成的 .env 默认 18080，已有 .env 则按
# LING_PORT。--port 可以改新生成 .env 的端口。
```

或手动：

```bash
cp deploy/env.example deploy/.env
# 编辑 deploy/.env：至少设置 POSTGRES_PASSWORD 与 LING_ROOT_KEY
# （固定值，用于加密供应商密钥），并把 LING_TEST_MODE 设为 0

cd clients/web-console && pnpm install && pnpm build && cd ../..
docker compose -f deploy/compose.yaml --project-name ling-dev up -d --build
curl http://127.0.0.1:6520/readyz
```

**端口只有一个来源**：api 容器内**固定**监听 6520（`entrypoint.sh` 的 uvicorn、
`expose`、`healthcheck` 三处一致），宿主发布端口由 `deploy/.env` 的 `LING_PORT`
决定（compose 默认 6520）。安装器的就绪等待、安装摘要与 `--status` 都以
`docker compose config` 解析出的**api 实际发布端口**（`target=6520`）为准，
不再假定脚本里写死的 18080：

- 新装（没有 `deploy/.env`）时安装器生成 `LING_PORT=18080`，也可以用
  `--port 6520` 改；摘要里打印的就是真实端口。
- 已有 `.env`（例如照 `env.example` 用了 6520）时，安装器按 6520 等 `/readyz`
  并打印 6520，不会再错等 18080。
- 已有 `.env` 时 `--port` **不会**改写它（安装器不写已有 env）；如果
  `--port` 与真实发布端口冲突，安装器直接失败并让你改 `LING_PORT`，
  绝不静默等一个错端口。
- 解析不出 `target=6520` 的确定发布端口（缺 ports、区间/随机分配、或只有
  别的 target）时安装器 **fail closed**，不会对着猜的端口等 120 秒。
- **从旧的 edge 拓扑升级**（`docker ps` 里有 `ling-edge-1`、`.env` 里是
  `LING_EDGE_PORT=20443`）**必须先按 §7「从 edge 拓扑迁移」做一遍**：那些键
  已经不起作用，不迁移的话对外端口会从 20443 静默变成 `LING_PORT` 的默认值
  6520，而 OneBot 上报、防火墙、外部反代还指着旧端口。安装器与 `--status`
  都会检测并点名这些废弃键（只读，不改写 `.env`）。

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

服务：`db`（不暴露端口）、`api`（容器内固定 6520，**自己发布宿主端口 `LING_PORT`**，
默认 6520）、`worker`（outbox/恢复）、前端产物只读挂到 api 的 `/app/web`。
**没有 `edge`/nginx 服务**：api 就是对外入口（详见 §7）。

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
  （python/postgres）的解析结果只 `export` 到本次进程环境，不写回 env。
- **新生成的 `.env` 不再写死本检出的绝对 dist**：`LING_WEB_DIST_HOST` 用相对
  `deploy/` 的默认值 `../clients/web-console/dist`。canonical env 被多个
  release 共享时，绝对值会指向上一个 release 树，让后续**手动**安装挂到旧前端
  并在校验时 fail-closed（更新执行器会自己传本次 release 的 dist，不受影响）。
  已存在的绝对值不会被自动修改：在另一棵树手动升级时显式
  `export LING_WEB_DIST_HOST="$(pwd)/clients/web-console/dist"`，并保留
  `LING_INSTALL_COMPOSE_OVERRIDE`、更新通道目录等其它 bind 上下文。
- **基础镜像显式选择不会被替换**：shell / `--python-base` / env-file 里的
  `LING_PYTHON_BASE`、`LING_POSTGRES_IMAGE` 一律原样保留（`LING_NGINX_IMAGE`
  随 edge 服务一起删除，安装器不再解析它）；
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
  目标（更新通道目录、运行数据等）；把 `/app/web` 指到别处会让安装
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
  新增挂载、更新通道目录的绝对源，以及本次 release 的 `/app/web` 更新不受影响。失败信息只报服务名/卷名，不打印 compose config
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
# 默认 compose 不发布 db 端口、宿主机也没有 DSN，所以要用 compose 模式。
# **`LING_COMPOSE_PROJECT` 必填且承重**：Compose v5 的 `docker compose exec`
# 是按 **project + service 的 label** 找正在运行的容器，**不看 `-f`**
# （真机实测：`-f /nonexistent.yaml` 仍 rc=0 且命令照常执行；`-f` 指向插值
# 必失败的 compose 也 rc=0）。项目名对不上就是 `service "db" is not running`
# ——这时该查的是**项目名**，不是 db 容器。`-f` 只对 `up`/`config` 这类"读文件"
# 的子命令有意义，脚本因此仍带着它，并在备份前先用 `config -q` 做一次静态
# 校验（compose 文件坏掉时 exit 2，不会等到 `up` 才暴露）：
LING_BACKUP_VIA_COMPOSE=1 \
LING_COMPOSE_PROJECT=ling \
LING_COMPOSE_FILE="$(pwd)/deploy/compose.yaml" \
deploy/backup/backup.sh /path/to/backup
# 产物（文件名由 backup.sh 的日期戳决定）：
#   ling-YYYYmmdd-HHMMSS.dump       数据库 dump（pg_dump custom 格式）
#   manifest-YYYYmmdd-HHMMSS.txt    清单：产物名 + 各产物 sha256 + keys_root/media 状态
#   tombstones-YYYYmmdd-HHMMSS.txt  撤回墓碑 id（无撤回记录时为空；0 字节是合法产物，轮转也认它）
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
> `docker compose exec` 找不到容器（报 `service "db" is not running`，
> 与"db 容器挂了"是同一句文案——**先怀疑项目名**）。

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

### 在界面里看日志（`/v1/logs`）

进程除了写 stdout，还会在内存里留一份**固定容量**的环形缓冲，供 Web 控制台的
「日志」页（左导航 → 日志）读取。不必 SSH 上机器 `docker logs` 也能看到
"她刚才为什么没回"。

| 变量 | 作用 |
|---|---|
| `LING_LOG_BUFFER_SIZE` | 缓冲条数，默认 `500`，上限 `10000`（写错/越界会在启动日志里说明按哪个值处理） |
| `LING_LOG_ACCESS_DENIALS` | 白名单拒绝是否**逐条**写一条 `inbound_denied` 事件，默认 `true`。设成 `false` 只关日志，**拒绝照旧**、被拒计数照常累加（见下） |

> `LING_LOG_ACCESS_DENIALS` 只是"还没有发布过配置修订"时的默认值。设置页
> 「模型设置 → 入站拒绝日志」发布的修订优先；改环境变量不会覆盖已发布的值
> （与 `LING_TEST_MODE` 同一个模式）。
>
> 为什么受理路径不每条消息查库：拒绝是**最该便宜**的路径（被刷时每秒可能几十
> 条）。开关值在每个 API 进程内缓存 30 秒，发布/回滚时本进程的缓存立即失效；
> 多副本部署时其它副本最多晚 30 秒。读库失败退回环境变量给的默认值（默认开），
> 拒绝本身不受影响。

**关了逐条日志，还能看到被拒次数吗？** 能。进程内有一个 `denied_total`
计数器，无论开关如何都随每次拒绝 +1，通过 `GET /v1/logs`（日志页顶部）和
`GET /v1/onebot/health` 暴露。它回答的是"最近有没有人在扫我的机器人"。
边界与日志缓冲一致：**本进程**、重启归零、多副本各算各的。

**边界（界面上也写着同样的三句）**：

- 只有**本进程**。多副本部署时每个副本各有一份缓冲，看到的是被负载均衡分到
  的那一个进程，不是全集群。
- 只有**最近 N 条**。写满后最旧的被覆盖，接口没有分页/游标/持久化。要长历史
  请继续用容器日志采集（`LING_LOG_FORMAT=json` 输出到 stdout 的那份）。
- 条目在**写进缓冲前**就用同一套 `redact_mapping` / `redact_value` 脱敏过，
  所以接口不会把明文密钥交出去；`GET /v1/logs` 只对 `owner` / `admin` 开放，
  普通成员拿到 403（不是空列表）。

接口参数：`limit`（默认 200，最大 1000）、`level`（`debug|info|warning|error`，
语义是**不低于**该级别）、`logger`（前缀匹配）、`q`（消息子串，不区分大小写）。
返回 `{"items": [...], "buffered": N, "capacity": M, "denied_total": K}`，`items`
按**时间正序**（最新在最后）。`denied_total` 是本进程启动以来被白名单拒掉的
入站条数，与 `LING_LOG_ACCESS_DENIALS` 开关无关。

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

- Docker Compose 单机部署：`api` / `worker` / `db` 全部起得来，`/readyz` 通过
- 多副本 + 前置反代（`deploy/nginx.conf`，**不再是 compose 服务**）：**旧 edge 拓扑下**
  验证过滚动更新期间不断服；改为 api 直连（edge 已删除）后，这个组合**本次没有复验**
- 删掉 edge、api 直连 6520：**只在 stub 测试与静态 YAML 解析里验证过**，
  未在真实 Docker 上跑（见 §7 的「改动后」小节）
- 备份 → 恢复 → 校验整条链在真实部署上跑通（**口径**：真机上验过的是
  "dump + 墓碑清单 → 隔离库恢复 → 关键表/墓碑一致"这一段；之后新增的
  `root.key`/媒体归档、产物 sha256、`media_ref` 覆盖检查只有**本地 fake**
  与单元测试验证过，**未上真机**，见 §8.3）
- **镜像模式在隔离项目 `ling-drill` 上真机演练过**（端口 6521、独立卷、独立
  根密钥，同一台宿主）：首次安装、从真实备份恢复、**三次连续无人值守升级
  A→B→C→D**、维护门禁（容器 healthy 时宿主端口确实关闭）、已迁移失败不回滚 /
  进人工门禁、更新器 idle 重启对账。记录见
  `shots/deploy-research/P3-DRILL.md` 与 `shots/deploy-research/P5-DRILL.md`
  （演练产物目录，不入仓库）。
  **注意边界：这是隔离项目，不是生产 `ling`。**
- 真实模型对话：配好供应商与密钥后能正常回复

**未验证**：

- 微信 / 手机端 / 具身设备（这些渠道的适配代码在仓库里，但没有可用的对照端）
- 公网部署所需的域名、TLS、反代加固
- 长时间（数周）无人值守运行的稳定性
- **生产 `ling` 已不再是源码模式**：2026-11-05 迁移到镜像模式（停机 → 老源码目录改名
  `/vol1/@appdata/ling-srcmode` 保留 → 用 `ling install` 按项目名 `ling`、端口 6520 重装，
  卷仍是 `ling_app-data` / `ling_db-data`）。那次安装本身**失败过**：新卷的 postgres 两段式
  启动让 `pg_isready` 在临时服务器阶段就报 healthy，安装器查 `alembic_version` 撞进"数据库
  正在关闭"的窗口，停在 `failed_after_migration`；随后手工跑完迁移、起 api/worker、补 OneBot
  配置与 WS override，用 `ling adopt --force` 把记录改写成实况。该竞态后来已修（`0e9ad2a`：
  有界重试 + 真实 `select 1` 就绪探针 + 阶段标签改为 `failed_db_ready`），修复后用全新空卷
  在真机重装验证通过。生产实况：api/worker 同一摘要 `ghcr.io/qsbb/ling-api@sha256:42bba216…`、
  `alembic_version = 0045_turn_control_attempt` == 镜像内 head、`/readyz` 200、OneBot 反向
  WS 6799 可用。安装记录是 `adopt` 写的，14 项标 `unknown`（`ling doctor` = ok=29 warn=14
  fail=0，这些 warn 是设计如此，不是故障）。
- **演练意义上的几项仍未完成**（与上面那次迁移不是一回事）：两次连续网页升级、head 不一致 /
  并发请求 / 旧执行器残留的故障注入、生产库有真实数据时的恢复（生产库 `messages=0`、
  `memories=0`）、arm64 运行、真实浏览器端到端点一遍
- 源码模式的完整升级演练（备份 → 构建 → 切换 → 失败恢复）仍未在真实 Docker 上做
- 从 edge 拓扑迁移的**整条链路**（`down --remove-orphans` → 改 `.env` →
  `install.sh` → `adopt-current`）只在 stub 测试与文档里走过，**没有在真实
  Docker 上演练**；迁移前请务必先备份，并按 §7 的第 5、6 步逐条核对
- 备份链路的**新增**检查在真机上跑通：`docker compose exec` 的退出码透传、
  `tar -cf -` 的 stdout 字节干净性、大媒体卷耗时、并发上传时的 `media_state=partial`、
  以及媒体/密钥临时文件"创建时 group/other 位全 0"在**真实分钟级流式传输**下的全程观测
  （本地测试用的是真实文件系统 + 严格 fake 的 docker/tar，不是真实容器）
- 升级失败后的数据库回滚（执行器只保留备份并转人工，不自动回滚）
- 镜像模式演练**没有覆盖**的项：**中断中任务的对账**（只有 3 条单测 + 变异
  验证，真机四次尝试都因工具/时序失败）、**生产库有真实数据时的恢复**（演练库
  `messages=0/memories=0/turns=0`，墓碑/媒体是"0 对 0"）、**arm64 运行**（镜像
  构建了双架构，但宿主是 x86_64）、**浏览器端到端点一遍**（前端测试通过，没在
  真实部署的浏览器里点过）。详见 `P5-DRILL.md` 与 `DELIVERY.md`。

> 判断某项是否真的能用，看它有没有对应的测试或本节的验证记录；
> 只写了代码没有记录的地方，按未验证对待。

## 7. 拓扑：api 直连（改动后）

```bash
# deploy/.env
LING_API_REPLICAS=1          # **必须是 1**，见下
LING_PORT=6520               # api 的宿主发布端口

docker compose -f deploy/compose.yaml --project-name ling up -d --build
```

拓扑：`浏览器/CLI → api:6520`，`worker`、`db` 不对外。
网络有两张：`internal`（`internal: true`，无外网，只有 db/api/worker 之间通信）
和 `egress`（普通 bridge，api 与 worker 靠它出网访问 OneBot / embedding /
rerank / LLM / 告警 webhook）。

> **`egress` 绝对不能加 `internal: true`**。加了之后 api/worker 会彻底失去外网，
> 而且不报错——worker 表现为"模型提炼不可用，降级为正则: ModelError"，api
> 表现为"QQ 连不上、语义检索永远走 BM25"。这个坑踩过一次，别再踩。

### 为什么副本数只能是 1

api 自己发布固定 host 端口（`ports: "${LING_PORT:-6520}:6520"`）。Compose
**非 swarm 模式**下 `ports` 只能给一个容器：2 个副本会直接端口冲突，第二个
容器起不来。所以 `deploy/compose.yaml` 的默认值改成了
`replicas: ${LING_API_REPLICAS:-1}`，`env.example` 与 `install.sh` 生成的
`.env` 也都是 1。

多副本的前提（状态不在进程内存）本身仍然成立（见下方小节），但要真的跑
多副本，必须自己前置一层反代：

1. 自己在 compose 里加一个 Caddy/nginx 容器，或者用宿主上的 nginx；
2. upstream 指到 `api:6520`（容器内服务名解析到所有副本，轮询即负载均衡）；
3. 把 api 的 `ports` 收回成只 `expose`，只暴露反代端口；
4. 把 `LING_API_REPLICAS` 调上去。

`deploy/nginx.conf` / `deploy/nginx-tls.conf` 保留给这种用法（顶部注释写了
完整步骤），但它们**不再是 compose 服务**，Ling 不会替你启动。

### 原来的 edge 拓扑（历史记录，已删除）

删之前是 `edge(nginx):80 → api×N:8080`：`edge` 发布 `LING_EDGE_PORT`
（默认 8080）与 `LING_EDGE_TLS_PORT`（默认 8443），api 只 `expose` 不发布
host 端口。删掉的原因：单副本个人部署里这层纯属多一个容器、多一份配置漂移
风险（证书路径跟着 release 树漂），而 nginx 提供的唯一硬价值
（`client_max_body_size 32m`）现在由应用自己承担
（`LING_MAX_BODY_BYTES`，默认 32MB，见 `src/ling/api/body_limit.py`）。

还在跑这套旧拓扑的机器，按下一节迁移；直接升级会静默改端口并留下占端口的
`edge` 孤儿容器。

### 从 edge 拓扑迁移（旧部署升级必读）

**这一节只针对已经在跑旧拓扑的机器**：`docker ps` 里有 `ling-edge-1`，
`deploy/.env` 里是 `LING_EDGE_PORT=20443`（可能还有 `LING_EDGE_TLS_PORT` /
`LING_EDGE_CONF` / `LING_EDGE_CERTS` / `LING_NGINX_IMAGE`）。新版本删掉了
`edge`，api 直接发布 `LING_PORT`；新 compose 里没有 `edge`，旧容器不会自己
消失，`.env` 里那些键也不再有任何引用。**不做下面这几步，升级会静默断链**：
对外端口从 `LING_EDGE_PORT` 漂到 `LING_PORT` 的默认值 6520，而 OneBot 上报、
防火墙、外部反代、书签还都指着 20443。

1. **先停掉旧拓扑并清 orphan。**

   ```bash
   cd /path/to/ling        # 你的部署目录，见本文 §1
   docker compose -f deploy/compose.yaml --project-name ling --env-file deploy/.env \
     down --remove-orphans
   ```

   本文示例统一用 `/path/to/ling` 代指仓库根目录（§1/§2 里写作 `/opt/ling`）；
   实际路径以你的部署为准，不要照抄示例。

   - 在**更新代码之前**跑最稳（此时 `deploy/compose.yaml` 还是带 `edge` 的旧
     版本，`down` 会正常停掉它）。如果你已经先同步了代码，这条命令也照样执行——
     只是此时 `edge` 已经不在 compose 文件里，**必须靠 `--remove-orphans`** 才会
     被清掉；不加的话它会作为 orphan 继续运行、继续占着 20443/8443，而它反代的
     上游 api 已经没人监听，用户看到的就是 502。
   - `--remove-orphans` 的作用域是**整个 compose 项目**（默认 `ling`），不是只针对
     `edge`：同一个项目里**你自己手工创建、但不在 compose 文件中的容器也会被删掉**。
     迁移前先确认没有要紧的手工容器；要保留的请换一个项目名（`--project-name`）
     或改用独立的 `docker run` 起。
   - 项目名用安装时的那个（`install.sh` 默认 `ling`，用 `--project` 改过就填那个；
     `-p` 省略时 Compose 按 compose 文件的顶层 `name:` 推，老版本这里也是 `ling`）。
   - `down` **会停服务**（有停机窗口），但**不会删数据卷**：不加 `-v` 就不存在
     `db-data` / `app-data` 被删的风险。
2. **改 `deploy/.env`**（这份文件安装器只读，不会替你改）：
   - 删掉 `LING_EDGE_PORT`、`LING_EDGE_TLS_PORT`、`LING_EDGE_CONF`、
     `LING_EDGE_CERTS`、`LING_NGINX_IMAGE`；
   - 加 `LING_PORT=20443`（**保持对外地址不变**，OneBot/防火墙/书签都不用动），
     或者 `LING_PORT=6520`（换新端口，那么第 6 步的对外集成都要跟着改）；
   - 证书和 nginx 配置如果还要用，改成你自己的前置反代（见 §9.1）——Ling 的
     compose 不会再把它挂进去。
3. **把代码树更新到新版本，在该树跑一次安装器。**

   ```bash
   bash deploy/install.sh --status      # 先看现状；会逐条点名 .env 里残留的废弃键
   bash deploy/install.sh               # 构建 → 启动 → 等 /readyz → 发布前端
   ```

   安装器现在给 `docker compose up` 和 `--down` 都带 `--remove-orphans`（只清
   本项目 `ling` 的孤儿容器，不碰别的项目；但同一个项目里你手工创建、不在
   compose 文件中的容器也在清理范围内，见第 1 步）。这是**兜底**，不是第 1 步的替代：
   旧容器已经占着宿主端口时，别指望 `up` 顺手把冲突解决掉——先 `down` 清场再
   安装。安装摘要里打印的端口就是 compose 解析出的真实端口。
4. **更新执行器（如果装了自助更新）。** 两件事都要做：
   - 把执行器配置里的 `port` 改成新的 `LING_PORT`（`/etc/ling-update.toml`；
     它用这个端口探宿主 `/readyz`，不改就永远 `verify_failed`）；
   - **重建基线**：让执行器核验并记下“现在实际运行的是什么”。

   ```bash
   sudo python3 deploy/update/executor.py --config /etc/ling-update.toml adopt-current
   ```

   `adopt-current` 会先取宿主锁与安装锁，再逐副本自证 commit/version、容器
   挂载与宿主端口 `/readyz` 2xx；**不能自证就拒绝**（不写 installed、不解除
   `needs_manual` 门禁）。命令语义见 `deploy/update/README.md` 第三节，
   失败处理见 [UPDATES.md](UPDATES.md) §4/§8。

   > 跨拓扑更新**不要**指望自助更新：旧 live release 的 compose 有 `edge`、
   > 新 release 没有，plan 阶段判「服务集合必须一致」不成立，直接
   > `compose_bind_changed`（`manual=False`，重试也不会成功）。这个判据是
   > fail-closed 的安全设计，不会放宽；必须先按本节人工迁移，再重建基线。
   > 执行器现在会把差异写清楚（例如“服务集合从（api、db、edge、worker）变成
   > （api、db、worker）；少了 edge”），落在宿主 stdout 与
   > `state_dir/recovery-<request_id>.txt` 里（Web 上仍只显示固定文案）。
5. **验证。**

   ```bash
   # 端口换成你实际用的 LING_PORT；期望 200
   curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:6520/readyz
   # 不应再出现 ling-edge-1
   docker ps --format '{{.Names}}' | grep -E '^ling-(api|worker|db|edge)-'
   bash deploy/install.sh --status
   ```

6. **外部集成自查**（端口只要变了，这些都要逐条核对）：
   - OneBot / NapCat 的 HTTP 上报地址：`http://<宿主>:<端口>/v1/onebot/events`；
   - 防火墙 / 安全组 / 端口映射：旧 20443、8443 → 新的 `LING_PORT`；
   - 外部反代（Nginx / Caddy / 1Panel 等）的 upstream 与监听端口；
   - 书签、手机端/客户端配置、监控与告警里的 URL。

### 多副本的前提：状态不能在进程内存

已在 PostgreSQL 实现（迁移 0007）：
- `turn_controls`：停止信号跨实例传递（A 点停止、B 跑轮次）
- `speak_leases`：发言租约，同一角色同时只在一个窗口说话

**仍限单实例**（有意降级，非缺陷）：
- 流式片段续传 → 多实例下 resume 退化为权威快照（前端仍拿到完整文本）
- 群发言频率 → 实际限额约为单实例 × 副本数（宽松方向）

### 前置反代（如果自己加了）必须关闭 buffering

`deploy/nginx.conf` 里 `/v1/` 段设了 `proxy_buffering off` 与 300 秒超时。
不关的话 SSE 会被 nginx 攒成一整段，流式就失效了。Caddy 等价写法是
`flush_interval -1`。**直连 api 时不需要这一层**，uvicorn 自己就是逐条刷出。
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
LING_ONEBOT_ACCESS_TOKEN=<出站调用凭据：对应 NapCat 的 httpServers[].token>
LING_ONEBOT_SECRET=<入站签名密钥：对应 NapCat 的 httpClients[].token，必填>
LING_ONEBOT_SELF_ID=<机器人 QQ 号>
LING_ONEBOT_INSTANCE_ID=onebot-main
```

#### ⚠️ 破坏性变更：`LING_ONEBOT_SECRET` 现在是**必填**（HTTP 上报模式）

**不配 = `/v1/onebot/events` 拒绝所有上报（403）。** 容器会照常 healthy、
`/readyz` 照常 200，但 QQ **完全不回话**——这是最容易被忽略的一种失败形态。

为什么改成 fail-closed：这条路由没有任何鉴权依赖，而 6520 默认对外发布。
`X-Self-ID` 是请求方自己填的，不构成凭据。任何能访问 6520 的人只要伪造
`X-Self-ID` 和 `user_id=<白名单里的 QQ 号>`，就能以该用户身份对话、写记忆、
让她向真实好友发言。所以 secret 成了这条入口的**必要条件**。

启动日志里会有对应的一条记录：配了其它 `LING_ONEBOT_*` 时是 ERROR，
完全没配 OneBot 时是 WARNING（后者可以忽略）。

#### `LING_ONEBOT_SECRET` 与 `LING_ONEBOT_ACCESS_TOKEN` 不是一回事

| 变量 | 管什么 | NapCat 侧对应字段 |
| --- | --- | --- |
| `LING_ONEBOT_ACCESS_TOKEN` | **出站**：ling 主动调 NapCat 的 HTTP API 时带的 `Authorization: Bearer` | `network.httpServers[].token`（那个 HTTP 服务器的口令） |
| `LING_ONEBOT_SECRET` | **入站**：NapCat 推上来时用 `X-Signature: sha1=<hmac_sha1(secret, body)>` 证明是它 | `network.httpClients[].token`（HTTP 上报那一条的 token） |

**关键坑**：NapCat 的 `httpClients` 里**没有独立的 `secret` 字段**，
`token` 一个字段同时承担「调 API 鉴权」与「上报签名密钥」——源码
`napcat-onebot/network/http-client.ts` 用的是 `this.config.token` 作为 HMAC key：

```ts
const msgStr = JSON.stringify(event);
if (this.config.token) {
  const hmac = createHmac('sha1', this.config.token);
  hmac.update(msgStr);
  headers['x-signature'] = 'sha1=' + hmac.digest('hex');
}
```

所以如果 NapCat 侧只填了一个 token，`LING_ONEBOT_SECRET` 必须与它**逐字符相同**。
配成不同的值 ⇒ 每条上报都验签失败 ⇒ **仍然全 403**，而日志里只看到"签名不匹配"。
生成一个：`openssl rand -hex 16`。

NapCat 侧要开：
- **HTTP 服务器**：端口 5700（对应 `LING_ONEBOT_API_BASE`）
- **HTTP 上报（反向 HTTP）**：地址填 `http://<ling 地址>:6520/v1/onebot/events`
  （用你自己的 `LING_PORT`）
- 上报那一条的 **`token` = `LING_ONEBOT_SECRET`**；HTTP 服务器的 `token`
  对应 `LING_ONEBOT_ACCESS_TOKEN`（两者可以相同，但必须各自对上）

### 6.1.0 两种 WS 模式（以及容器部署要多做的那一步）

HTTP 上报要求 **NapCat 能访问到 ling**。不想让 API 绑 `0.0.0.0` 暴露在局域网时，
有两条 WS 路：

| 模式 | 谁连谁 | 配置 | 要不要发布端口 |
| --- | --- | --- | --- |
| **反向 WS 服务端**（推荐） | ling 监听，NapCat 连进来 | `LING_ONEBOT_WS_PORT` + `LING_ONEBOT_WS_TOKEN`（必填，缺了拒绝启动）+ `LING_ONEBOT_WS_HOST=0.0.0.0` | **要**（容器里还得另加一份 override，见下） |
| **反向 WS 客户端** | ling 主动连 NapCat 的 wsServer | `LING_ONEBOT_WS_URL=ws://<napcat>:<端口>` | **不要**（ling 是连出去的一方） |

**容器部署（compose）里服务端模式的端口默认不发布**：基础 `deploy/compose.yaml`
的 api 只发布 `${LING_PORT:-6520}:6520`。不配 WS 的部署不该多出一个对外端口，
而 Compose 无法条件发布端口（变量留空时那条映射会变成非法条目，`up` 直接报错；
`profiles` 只能条件启用**服务**，不能条件启用某个服务的 `ports`）。

所以要用服务端模式时，复制仓库里那份现成的 override 并追加 `-f`：

```bash
cp deploy/compose.ws.example.yaml deploy/compose.override.ws.yaml
docker compose -f deploy/compose.yaml -f deploy/compose.override.ws.yaml \
  --project-name ling --env-file deploy/.env up -d
# 或用安装器（它会把该文件追加成第二个 -f，并始终保留 --env-file）：
LING_INSTALL_COMPOSE_OVERRIDE="$PWD/deploy/compose.override.ws.yaml" bash deploy/install.sh
```

漏了这一步的表现：容器内已经监听了 6799，宿主上的 NapCat 却连不进去，
**日志里没有任何异常**。容器内的监听地址还必须显式设成 `0.0.0.0`
（默认 `127.0.0.1` 在容器里等于只有容器自己能连）。

客户端模式（`LING_ONEBOT_WS_URL`）**不需要**发布任何入站端口，也不需要
`LING_HOST=0.0.0.0`。但它同样必须进容器：`deploy/compose.yaml` 的 `x-app-env`
是逐项白名单，漏列的变量在 `.env` 里配好也进不去，表现为"配了却一次都没连过"。

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

ling 的 OneBot 连接器支持 **HTTP 上报**（`httpClients` 推给 ling）与两种 WS
（见 6.1.0）。HTTP 上报这条路的 NapCat 侧配置如下。

在 NapCat 的 `onebot11_<机器人QQ>.json` 里：

```json
{
  "network": {
    "httpServers": [
      { "name": "http-local", "enable": true, "port": 5700,
        "token": "<= LING_ONEBOT_ACCESS_TOKEN>" }
    ],
    "httpClients": [
      {
        "name": "ling",
        "enable": true,
        "url": "http://<ling 所在机器>:6520/v1/onebot/events",
        "messagePostFormat": "array",
        "reportSelfMessage": false,
        "token": "<= LING_ONEBOT_SECRET，必填>",
        "heartInterval": 30000
      }
    ]
  }
}
```

四个容易踩的点：

1. **`httpClients[].token` 必须填**，且与 `LING_ONEBOT_SECRET` 逐字符相同。
   NapCat 用它当 HMAC key 算 `x-signature`；ling 侧没配 secret 或两边不一致，
   上报一律 403，而容器是 healthy 的——表现为"机器人在线但完全不回话"。
2. **`httpServers[].token` 对应 `LING_ONEBOT_ACCESS_TOKEN`**（出站调用），
   不是 `LING_ONEBOT_SECRET`。两处填同一个值也可以，但必须各自对上，
   否则 API 调用会 401/403（能收事件却发不出去）。
3. **`LING_HOST` 必须是 `0.0.0.0`**。只绑 `127.0.0.1` 时 NapCat 连不上
   （实测：同一局域网内 `readyz` 直接 000）。走 WS 客户端模式则不需要。
4. **`X-Self-ID` 必须匹配**。配了 `LING_ONEBOT_SELF_ID` 之后，
   上报不带或带错这个头会被直接拒绝（防止别的实例误配到同一入口）。
   另外 `LING_ONEBOT_INSTANCE_ID` 要与绑定账号时用的实例名一致，
   账号绑定按 instance 存，写错会「事件进来了但解析不到身份」。

连通后应该看到这样的记录（数值为示例）：

```
NapCat 日志：HTTP上报服务: http://<ling-host>:6520/v1/onebot/events, : 已启动
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
TOKEN=$(curl -s -X POST http://127.0.0.1:6520/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"ling","password":"<你的密码>"}' -c /tmp/c.txt >/dev/null && echo ok)

# 绑定（proof=pending：先建立关系，不能用于身份解析）
curl -b /tmp/c.txt -X POST http://127.0.0.1:6520/v1/accounts/bind \
  -H 'Content-Type: application/json' \
  -d '{"connector_instance":"onebot-main","external_account_id":"<QQ号>"}'

# 确认账号确实属于本人后，升级为已证明（此后才能解析身份）
curl -b /tmp/c.txt -X POST http://127.0.0.1:6520/v1/accounts/<account_id>/prove
```

`proof=pending` 只能建关系、不能解析身份——否则"自称某账号"就能拿到那个人的记忆。

### 6.3 验证

```bash
# OneBot 连通性（调 get_login_info）
curl http://127.0.0.1:6520/v1/onebot/health

# 在 QQ 里给机器人发一条私聊，应收到回复；
# 服务器侧可核对：
curl -b /tmp/c.txt http://127.0.0.1:6520/v1/accounts   # 绑定状态
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
curl -b /tmp/c.txt -X POST http://127.0.0.1:6520/v1/accounts/bind \
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
   curl -s http://127.0.0.1:6520/v1/qqbot/health | python -m json.tool
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

`LING_COMPOSE_PROJECT` 不能省，两条独立理由：

1. `docker compose -f 路径` 会按**文件所在目录名**推导项目名（这里是 `deploy`），
   而 `install.sh` 默认用 `-p ling`；名字不一致时 `exec` 找不到正在运行的容器。
2. **Compose v5 的 `exec` 根本不看 `-f`**（真机实测：`-f /nonexistent.yaml` 仍
   返回 0 并照常执行）。它按 project + service 的 label 找容器，`-f` 只对
   `up`/`config` 有意义。

   项目名（`-p`）是否承重**取决于 compose 文件有没有顶层 `name:`**：
   - 有 `name: ling`（本仓库 `deploy/compose.yaml` 就是）→ 它覆盖了"按目录名
     推导"，**不给 `-p` 也照样命中**（真机实测 rc=0）
   - **没有** `name:` → 按目录名推导成 `deploy`，与 `install.sh` 的 `-p ling`
     不一致，`exec` 找不到容器

   所以 cron/备份**仍然要给 `-p`**（不能依赖 compose 恰好写了 `name:`），
   但别理解成"`-p` 是唯一承重参数"。
   `backup.sh` 在 compose 模式下会先跑一次 `docker compose config -q` 做静态
   校验（坏文件 → exit 2），因为 `exec` 阶段发现不了 compose 文件的问题。

用 `--project` 改过项目名的话，这里填那个名字。

产物五件：`ling-YYYYmmdd-HHMMSS.dump`、`manifest-YYYYmmdd-HHMMSS.txt`、
`tombstones-YYYYmmdd-HHMMSS.txt`（**无撤回记录时是 0 字节，这是合法产物**）、
`keys/root.key`
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
exit 0；媒体归档**致命**失败：compose 的 `tar` rc≥2 或 `tar -tf` 读不出目录、
直接模式 `tar` 非零）时 `backup.sh` 非零退出，并删掉**本次**已产出的
片段（dump / 墓碑 / 媒体 / `version-*.txt`）——不会留下一个"看起来像备份"的
残目录。cron 里表现为 `backup.log` 有错误、当天没有新 manifest。
**注意**：compose 的 `tar` rc=1 只是 `media_state=partial`，脚本仍 exit 0（见下面
退出码表）；墓碑导出失败也仍 exit 0，只在 manifest 写 `tombstones_state=FAILED`。

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

`keys/.root.key.prev.<pid>` 与临时文件 `.root.key.part.<pid>` 都是**创建瞬间就
"同机其他用户读不到"**（`backup.sh` 在 `umask 077` 的子 shell 里**用重定向创建**：
暂存副本与直接模式的密钥临时文件是 `cat … > 目标`，compose 模式是 `docker … > 目标`）。
这里特意不用 `cp`：`cp` 会把源文件的 mode 带到目标上（源带 owner-x 位时目标创建
瞬间是 0700），重定向创建在 `umask 077` 下则恒定满足 `mode & 0o077 == 0`，随后的
`chmod 600` 只是兜底。先按默认 umask 落一个 0644 再 `chmod 600` 的写法会在两步之间
留一个"同机其他用户可读根密钥字节"的窗口，cron 路径的备份目录本来就是 0755，所以
这件事不是理论风险。

> **口径是 `mode & 0o077 == 0`，不是"恰好 0600"**：`0600` 只是"常见 umask + 常见
> 文件系统"下的**结果**（ext4/xfs 上 `0666 & ~077` 就是 0600）。真 Docker 验证
> （`shots/docker-real/DOCKER-REAL.md` §5 #3）在 Synology NAS 的 `/vol2`
>（btrfs + `trimacl`）上实测：**所有新建文件恒为 0700**（`umask 077` 得 700、
> `umask 022` 也得 700、`mkdir` 得 700）——owner 多一个 x 位，group/other 仍是 0，
> 所以**同样安全**。真正要挡的是"同机其他用户"，不是 owner 自己的 x 位；测试断言
> 也因此钉在 `mode & 0o077 == 0` 上（否则这套测试原样跑在 NAS 上会红），只有显式
> `chmod 600` 之后的**正式产物**才断言精确 0600。

媒体临时文件
`.media-<STAMP>.tar.part` 走的是**同一条路径**：compose 模式把
`docker compose exec -T api … sh -c 'tar -cf -'` 的 stdout 重定向放进
`umask 077` 的子 shell 内，直接模式同样把 `tar -cf - … > "$MEDIA_TMP"` 放进子
shell 内，所以 `.part` **创建瞬间就满足 `mode & 0o077 == 0`**（ext4 上是 0600，
NAS 的 btrfs + trimacl 上是 0700；随后的 `chmod 600` 只是兜底）。
两种模式都是**先 `chmod` 再 `mv`**，正式名 `media-<STAMP>.tar` 一出现就是精确
0600（显式 `chmod`，与 umask / 文件系统无关），不存在"正式名还是 0644"的中间态；
compose 的 `.media-*.err` 也在同一个子 shell 里
创建，同样是"group/other 位为 0"。`umask 077` 只作用于媒体/密钥这两**类**路径（脚本里共 5 处
子 shell）：同一份备份里的 `manifest-*` / `tombstones-*` / `dump` 仍按外层 umask
写成 0644——**这是「默认 umask 022 下」的口径**，脚本不 chmod 它们；ambient
`umask 077` 下同样的文件会是 0600，所以别把 0644 当成脚本保证（测试断言
`manifest` / `tombstones` 是 0644；`dump` 同样是 0644，但测试没有单独断言它，
属于实测/umask 行为，见下）。

验证方式：`tests/test_deploy_backup_archive.py` 的
`test_compose_media_part_is_created_0600_and_mv_keeps_it` 与
`test_direct_mode_media_part_is_created_0600` 在 umask **022** 下同时做三路观测：
① 严格替身在**自己启动那一刻** `fstat` 脚本的 stdout（即 `.part`），断言"创建
瞬间 `mode & 0o077 == 0`"（不是精确 600：NAS 上会是 700，同样安全；出现 0644 即
失败）；② 主线程约每 **2ms** 轮询一次（`time.sleep(0.002)`）`.part` 与
正式名，断言观察到的 mode
集合**全部满足** `mode & 0o077 == 0`（出现任何 0644 中间态即失败）；③ 断言终态
正式名 0600（显式 `chmod` 后的精确值）、
成功路径不留 `.part`，且 `manifest` / `tombstones` 仍是 0644（`umask 077` 没有
泄漏到其它产物）。另有 `test_compose_media_part_nas_btrfs_0700_is_also_safe` /
`test_direct_mode_media_part_nas_btrfs_0700_is_also_safe` /
`test_keys_tmp_nas_btrfs_0700_is_also_safe` 三条用例把 `.part` 与 `KEYS_TMP` 的
"创建瞬间"桩成 NAS 的 **0700**，证明这套断言在 btrfs + trimacl 上同样通过。

（墓碑导出失败仍 exit 0（脚本头部与退出码表已写明），但 manifest 里
`tombstones_state=FAILED`，执行器与 `restore_drill.sh` 都会据此拒绝这份备份；
`media_state=partial` 也是 exit 0 的"不完整"情形。所以 **exit 0 不等于产物
齐全**，必须看 manifest。）

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

- **空间按天线性累积，但脚本内置了可选轮转（默认关闭）**：每次备份（含每日 cron）
  都完整复制一份媒体；不显式给参数时 `backup.sh` **一份都不删**（默认行为与历史
  版本一致）。开启方式（位置参数仍只有 `OUT_DIR`，开关追加在后面）：

  ```bash
  # 先干跑：只打印计划，不建目录/不建锁/不跑 docker/不写不删
  deploy/backup/backup.sh /mnt/backup/ling --prune-keep 14 --dry-run
  # 确认计划后再真删
  deploy/backup/backup.sh /mnt/backup/ling --prune-keep 14
  # 更保守：份数上限 + 年龄下限（--prune-days 必须与 --prune-keep 一起给）
  deploy/backup/backup.sh /mnt/backup/ling --prune-keep 14 --prune-days 30
  ```

  `--prune-keep N` 的口径是 **N 份含本次新备份**（本次跑完目录里最多留 N 份完整
  组合），所以每日备份时**历史回看是 N−1 天**；`--prune-keep 1` 合法，但会
  **删光全部历史完整组合**、只剩本次这一份（零撤回部署同样如此，只要历史里没有
  "不合格组"）。**前提是本次备份自己也是完整组合**：如果本次不合格（例如墓碑导出
  `FAILED`、脚本仍 exit 0），本次不参与保留计数，`--prune-keep 1` 保留的是**最新
  一份历史完整组**，目录里会有 **2 份** manifest（前提是已有历史完整组；空目录
  加本次不合格时轮转直接 no-op，目录里只有这 1 份）——即本节末那条"不合格组永远不
  参与轮转、也不会被删，所以实际 manifest 份数可能多于 N"。实测（直接模式、
  零撤回、连跑两次）：第一次后目录里 2 份，第二次加 `--prune-keep 1` 后只剩
  **1 份**；`--prune-keep 2` 在 3 份历史 + 本次的目录里剩 **2 份**。`--prune-days D`
  是额外的年龄下限：只删 manifest 的 mtime 早于 D 天的组（`find -mtime +D` 按整
  24h 向下取整，`+30` 实际要等满 **31** 天才命中，30–31 天这段不算；按文件时间
  取整，不是按组名里的日期），单独给会 exit 2（没有 keep 上限时静默忽略 days 更
  危险）。

  轮转只删「dump + manifest + tombstones + media 四件齐全、且 manifest 可解析」
  的组合（`media=absent` 也算齐全——那表示确实没有媒体），并且**真正删除**发生在
  **本次备份成功之后**（dry-run 只打印计划；备份失败根本不进轮转）：

  - **0 字节墓碑是合法产物**：没有任何撤回记录（最常见）时
    `tombstones-<STAMP>.txt` 就是 0 字节（`psql` 查询成功、输出为空），manifest
    里 `tombstones_state=ok`。轮转把"文件存在 + 状态 ok"与"内容非空"分开判：
    0 字节**算完整组合**，参与保留计数、也能被删；只有**墓碑文件缺失**或
    `tombstones_state=FAILED` 才整组跳过并告警。注意这条放宽**只针对墓碑**：
    0 字节的 `dump` / `media` 仍然判坏（见下一条）；
  - 缺产物、缺 manifest、旧格式 manifest（没有 `media=`）、`tombstones_state=FAILED`、
    manifest 声称 `keys_root=present` 但 `keys/root.key` 不在 → 整组跳过并告警，
    一个文件都不动；
  - 没有 manifest 的孤儿 dump / 墓碑 / 媒体 → 只告警"不动它"；
  - `keys/root.key` **永不**在删除范围内（它没有日期戳，是当前这把密钥的唯一副本）；
  - 删除顺序是**先数据文件、manifest 最后删**：中途被打断时这组立刻变成"不完整"，
    下次轮转不会再碰它，也不会被 `restore_drill.sh` 当成有效备份；
  - 轮转失败（目录只读等）只告警、不改写备份结论（本次备份仍 exit 0）。

  **不要按日期手工 `rm` 旧产物**：那会绕开"四件齐全 + manifest 可解析"的门槛，
  很容易删出半组（例如 manifest 还在、`media-*.tar` 已没），等到恢复时才发现备不
  了；手工删也没有"`keys/root.key` 永不删"这道保险（例如 `rm -rf` 整个旧目录时）。
  要清理就用上面的开关，并先 `--dry-run` 看计划。

  已知限制（照实写）：轮转**不清理**直接模式的 `version-<STAMP>.txt`（删除白名单
  只含 dump / tombstones / media / manifest），组被删后它可能残留，也不会有孤儿
  告警；`media_state=partial` 的组在轮转眼里仍算"四件齐全"，**会占用保留名额**
  ——当最新的 N 份里有 partial 时，可能挤掉一份更早的干净备份（partial 本身仍可
  恢复，只是恢复前要人工核对 `media_ref`）；**不合格组永远不参与轮转、也不会被
  删**（缺产物、缺 manifest、`tombstones_state=FAILED`、旧格式等），所以目录里
  实际 manifest 份数可能多于 N——"最多 N 份"只对完整组合成立。

- **耗时也随媒体体积增长**：执行器里 `BACKUP_TIMEOUT_SECONDS=1800`（`deploy/update/
  executor.py`）现在是**含当前 `/data/media` 全量打包**的总预算；媒体卷几十 GB /
  慢盘时备份会超时 → `backup_failed` → 升级被阻断（fail closed，不丢数据但要重跑）。
  要缩短**本次**打包时间只能从数据量/速度入手：先缩小 `/data/media`（清理不需要的
  媒体或归档到冷存储）、调大 `BACKUP_TIMEOUT_SECONDS`、或换更快的磁盘/存储。
  **减少历史备份份数不会让本次 `tar` 变快**：执行器每次备份都新开
  `backups_dir/<request_id>` 目录、调用 `backup.sh` 时只传位置参数、**从不传
  `--prune-*`**，本次打包的仍是当前 `/data/media` 全量。轮转（上一段）只省磁盘
  空间，不是超时的解法。

**并发锁（同一备份目录不能被两个实例同时写）**：`backup.sh` 用 `mkdir` 做原子锁，
锁目录是 `$OUT_DIR/.backup.lock`，里面写持有者的 `pid` 与 `started_at`（启动时间）。
拿不到锁时：

- 持有者 PID 仍然存在（`kill -0` 成功）→ 打印 `pid=`、启动时间、锁路径，**exit 6**。
  第二个实例被拒绝，不会两边一起写盘，也不会删对方的产物（清理逻辑在没拿到锁时
  直接返回，避免把同秒 STAMP 正在写的文件删掉）；
- 锁是崩溃残留（`pid` 不存在，或锁目录里没有 `pid` 文件）→ 告警后**接管**并继续；
  接管再失败（目录仍被占用）仍 exit 6；
- `--force-unlock` 会先清掉锁再抢。**只应在确认那个进程已经死掉/是残留锁时用**：
  对**活着的**实例强开会让两个进程同时写同一批固定名字的产物（`keys/root.key`、
  `.part`、同秒 STAMP）。后启动的那份**通常**会在媒体完整性检查处失败（QA 观察到
  exit 5），但脚本**没有**对并发两写的最终产物做一致性校验（没有并发场景下的
  dump/manifest 事后比对），所以**不能保证**任何时序都不会以 exit 0 结束。覆盖
  已经发生，仍应避免；
- 正常结束（含失败）由 EXIT trap 释放锁，成功路径**不留锁**，下一次 cron 不会
  被永久拒掉。若残留锁里的 PID 恰好被无关进程复用，会被当成"还在跑"而 exit 6
  （安全方向：拒绝运行），人工确认后用 `--force-unlock` 解。

`--dry-run` 不建锁：目录上已有锁时它只告警（打印持有者 pid/启动时间），仍以
exit 0 返回轮转计划。

`backup.sh` 的退出码（脚本头部注释是契约，两边必须一致）：

| 码 | 含义 |
|---|---|
| 0 | **流程正常结束**，但**不保证产物齐全/可恢复**：可能带 `tombstones_state=FAILED`、`media=absent`、`keys_root=absent`、`media_state=partial`，必须看 manifest。轮转只告警也算 0 |
| 1 | 未分类的意外失败：`set -e` **透传底层命令的状态**（常见 1，例如直接模式 `pg_dump` 失败；脚本**没有**显式 `exit 1`）。命令缺失是 **127**，`psql` 自身 exit 2 会与"用法/配置错误"同码；其余状态不保证落在 0–7 |
| 2 | 用法/配置错误：缺 `OUT_DIR`、未知参数、`--prune-*` 取值非法、缺 `LING_DATABASE_URL`、compose 模式缺 `docker` |
| 3 | compose 模式 `pg_dump` 失败或导出为空（直接模式没有显式映射，`pg_dump` 的非零码由 `set -e` 原样透传，**不保证是 3**） |
| 4 | 容器内根密钥读取失败**或完整性自校验失败**（容器侧 `sha256` 与归档字节不一致 / 容器没回传哈希；无法确认密钥状态，本次备份作废） |
| 5 | 媒体归档**致命**失败：compose 的 `tar` rc≥2 或 `tar -tf` 读不出目录、直接模式 `tar` 非零。compose 的 `tar` rc=1 是 `media_state=partial`，**不算 5**，仍 exit 0 |
| 6 | **备份锁被占用**（另一个实例正在同一 `OUT_DIR` 运行；或残留锁接管失败） |
| 7 | 缺少 sha256 工具（`sha256sum` / `shasum` / `openssl` 都没有；在任何磁盘动作之前退出） |

> 0–7 是脚本**显式或常见**的状态，**不是封闭集合**：`set -e` 会把底层命令的
> 退出状态原样透传（例如 `psql` 命令缺失 → **127**；`psql` 自身 exit 2 会与
> "用法/配置错误"同码）。脚本没有显式 `exit 1`，1 只是"常见透传值"。

升级执行器**先看退出码**：非 0 一律判 `backup_failed`（fail closed，**不会**因为 6
而自动重试；`6` 的语义是"另有一个备份在跑"，确认后重跑即可）。但 **0 不等于成功**：
它还会用 manifest 再做三层校验（`tombstones_state=ok`、dump 头/哈希、keys/media
枚举与哈希等），校验不过仍是 `backup_failed`。cron 不解释退出码，只把脚本输出追加
进 `backup.log`——`6`、0 之后校验失败的告警、以及真正非零的失败都要人去日志里看。

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

**既有权限口径（脚本有意不改父目录，避免影响已部署的目录）**：cron 路径下备份目录
由 `mkdir -p` 创建，权限是 **0755**（受 umask 影响）；`dump` / `manifest` /
`tombstones` 按外层 umask 写成 **0644**（默认 umask 022，脚本不 chmod 它们）。
`media-*.tar`、`keys/root.key` 以及 `.root.key.part.*` / `.root.key.prev.*` /
`.media-*.tar.part` 都是 **0600**——不是"先 0644 再 chmod"，而是创建时就在
`umask 077` 的子 shell 里落盘；媒体正式名也是先 `chmod` 再 `mv`（见上文）。
也就是说 dump 里是业务数据、同机其它用户可读——请把备份的**父目录**自己收紧
（例如 `chmod 700 /mnt/backup/ling`，或建在宿主私有目录下），不要放在 `/tmp`、
Web 根目录或共享盘的可浏览位置。执行器自己的备份目录是 `0700`（`state_dir` 也是
`0700`），不受这条影响。需要更严就自己 `chmod` 父目录：脚本不会替你改（改了会让
既有部署上其他属主跑的 cron 写入失败）。

数据库里的 provider 凭据是加密的，根密钥丢了就全部解不开——表现为"机器人突然
连不上模型"，很难第一时间想到是密钥问题。恢复顺序见
[§4「恢复顺序」](#恢复顺序先-db--再-rootkey-与-media--最后起服务)。

### 8.2 运维状态端点

```bash
# 端口按 deploy/.env 的 LING_PORT（安装器默认 18080，env.example 示例 6520）
curl -b cookies.txt "http://127.0.0.1:${LING_PORT:-6520}/v1/health/ops"
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
curl -b cookies.txt -X POST http://127.0.0.1:6520/v1/health/ops/notify
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
| 只在同一台机器 / 可信 VPN / SSH 隧道内 | **不必须**（api 直连 6520，明文 HTTP） |
| 手机在外网直连、或暴露到公网 | **必须**（自己前置一层 TLS 反代，见 §9.1） |

判断标准不是"是不是自己的服务器"，而是**登录凭证与私聊内容会不会经过不可信网络**。

### 9.1 启用 TLS（自己前置反代）

**Ling 默认不再自带 TLS**：`edge` 服务已删除，api 直连 6520 且是明文。
要上 HTTPS 就得自己在前置一层，`deploy/nginx-tls.conf` 是一份可直接用的
示例配置（顶部注释有完整步骤）：

```bash
# 1) 生成自签证书（内部使用足够；浏览器会提示不受信任）
mkdir -p deploy/certs
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout deploy/certs/privkey.pem -out deploy/certs/fullchain.pem \
  -subj "/CN=ling.local"

# 2) 自己起一个反代容器（Ling 不替你启动它），把 nginx-tls.conf 与 certs
#    挂进去，upstream 指向 api:6520；同时把 api 的 ports 收回成只 expose。

# 3) 打开 cookie Secure
export LING_COOKIE_SECURE=1        # 少了这一条，凭证仍可能走明文
```

`LING_COOKIE_SECURE=1` 不是可选项：上了 TLS 但 cookie 不带 Secure，
浏览器仍可能在明文请求里带上会话 cookie。`scripts/check_compose.py`
会在检测到 443 端口时强制要求 `env.example` 里有这一项。

更省事的替代方案是 Caddy（自动签证书）：

```
api.example.com {
    reverse_proxy api:6520 {
        flush_interval -1   # SSE 必须逐条刷出，等价于 proxy_buffering off
    }
}
```

### 9.2 证书用 certbot 签发

`nginx-tls.conf` 已放行 `/.well-known/acme-challenge/`，
把 webroot 指到 `/var/www/certbot` 即可用 certbot 的 webroot 模式签发。
（前提是你确实用了这份配置作为前置反代。）

### 9.3 TLS 下的 SSE

如果你用 `nginx-tls.conf` 前置，`proxy_buffering off` 在 TLS 配置里同样必须存在，
否则流式回复会被攒成整段。

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

## 10. 自助更新（源码模式，默认关闭）

> 本节只讲**源码模式**的部署边界：宿主执行器 `deploy/update/executor.py` 取 tag
> 源码 → 构建 → 调 `install.sh`。它**不能**用于镜像模式；镜像模式是另一套通道
> （`/v1/version/image-update-requests` + 独立 `ling-updater`），见
> [UPDATES.md §10](UPDATES.md#10-镜像模式的更新通道新协议隔离项目已真机演练) 与本文 §11。
> 完整管线、宿主执行器命令、失败语义与人工恢复步骤见 [UPDATES.md](UPDATES.md)。
> **演练范围要分开说**：源码模式的完整升级仍未真机演练；镜像模式已在隔离项目
> `ling-drill` 上真机跑通（新装、备份恢复、三次连续升级），但**没有**在生产
> `ling` 上演练。两者都不应被说成"已验证的无人值守升级能力"。

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
也不挂宿主源码树。**默认关闭不等于目录没挂**：通道目录一直在（api 挂 requests `rw` + status `ro`），
`LING_UPDATE_ENABLED=0` 只是让 `reason=disabled`；`GET /v1/version/update` 仍会读
status 里的心跳来回答 `executor.online`，但没有任何路径会去写 requests 或触发部署。

> 镜像模式的主文件 `deploy/compose.image.yaml` 不是这样：两条宿主路径是**必填
> 插值**（缺值 `config` 直接失败），还要 `LING_UPDATE_SHARED_GID`（API 靠
> `group_add` 进共享组），而 `LING_UPDATE_ENABLED` 在那份文件里是**插值且默认 `0`**（`${LING_UPDATE_ENABLED:-0}`）
> ——只改 `.env` 打不开。见 §11。

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

**跨拓扑更新会被拒绝，这是有意的**：服务集合必须一致（新增/删除服务一律拒绝）
是 fail-closed 判据。旧 live release 的 compose 有 `edge`、新 release 没有时，
执行器在 plan 阶段直接判 `compose_bind_changed`（`manual=False`，**重试也不会
成功**），并在宿主 stdout 与 `state_dir/recovery-<request_id>.txt` 里写明
“服务集合从（…）变成（…）”（Web 上仍只显示固定文案）。遇到它不要反复点更新，
按 §7「从 edge 拓扑迁移」人工迁移后再 `adopt-current` 重建基线。

## 11. 镜像模式部署（推荐）

镜像模式的日常发布路径是「CI 构建并推送不可变镜像 → 宿主按**摘要**拉取 → Compose
切换」，宿主不再同步源码、不跑 `pnpm` / `pip`、也不 `docker compose build`。
设计与边界以 [IMAGE-DEPLOYMENT-DESIGN.md](IMAGE-DEPLOYMENT-DESIGN.md) 为准；
本节只写**怎么用现有的宿主脚本**，不重复设计论证；每处都区分"在隔离项目
`ling-drill` 真机演练过"与"未演练"。

### 11.1 两套并行路径（择一，不要混用）

| | 源码模式（旧路径） | 镜像模式 |
| --- | --- | --- |
| 安装入口 | `bash deploy/install.sh`（在源码 checkout 里） | `ling install --digest ...`（宿主不需要源码树） |
| 主 Compose 文件 | `deploy/compose.yaml`（api/worker 有 `build:`，Web 从宿主 bind 到 `/app/web`） | `deploy/compose.image.yaml`（api/worker 只认必填 `LING_APP_IMAGE`，Web 已烤进镜像） |
| 版本标识 | `LING_COMMIT` 构建参数 + 本地镜像名 | 固定 OCI **index digest**（`/v1/version` 读容器内 `LING_APP_IMAGE`；commit 取自镜像内烤入的 `LING_COMMIT`，与 OCI 标注 `org.opencontainers.image.revision` 同源，发布/更新器再交叉核对） |
| 日常升级 | 人工取新源码 → `install.sh`（[UPDATES.md §7](UPDATES.md)） | `publish.py <digest>`（人工）或网页更新（需独立更新器） |
| 自助更新执行器 | `deploy/update/executor.py`（**只支持源码模式**） | 独立项目 `ling-updater` + `/v1/version/image-update-requests`（[UPDATES.md §10](UPDATES.md)） |
| 可选 WS 端口 override | `deploy/compose.ws.example.yaml` | `deploy/compose.image.ws.example.yaml` |

两份主文件**只能择一，禁止叠加 `-f`**：叠加会让旧 `build:`/前端 bind 仍然生效。

### 11.2 完整流程（全新安装）

1. **从可信发布目录取一个固定摘要。** 目录文件是 `deploy/releases.json`
   （schema 1，`version` ↔ `digest` 的映射，格式与校验规则见
   `deploy/release/catalog.py` 文件头）：

   ```bash
   python3 deploy/release/catalog.py validate    # CI 也跑这一条，坏了就不该用
   ```

   > 这条校验工具用到了 `datetime.UTC`，需要 **Python 3.11+**（本机 `python3` 是
   > 3.9/3.10 时会直接 `ImportError`）；CI 用 `setup-python` 安装的版本跑，没问题。
   > `ling` / `publish.py` 本身不需要第三方依赖，也没有这个版本限制。

   > **现状**：仓库里的 `deploy/releases.json` 现在是空列表
   > （`{"schema": 1, "releases": []}`），也就是说**目前没有可用的镜像目标**。
   > 发布新版本时按上面的格式追加一条（digest 与源提交来自该次 release workflow
   > 的 job summary），不要手填一个"看起来像"的摘要。

2. **安装。** 宿主不需要源码树，但需要 `python3`（脚本是纯标准库，3.9+）、
   Docker/Compose 与 GHCR 拉取权限：

   ```bash
   python3 deploy/cli/ling.py install --non-interactive \
     --digest 'ghcr.io/qsbb/ling-api@sha256:<64 位小写 hex>' \
     --dir /opt/ling --backup-dir /vol1/ling-backups \
     --host 127.0.0.1 --port 6520 --admin-user ling
   ```

   - `ling` 只接受**固定摘要**：`latest`、可变 tag、空值、非
     `ghcr.io/qsbb/ling-api` 的仓库一律在跑任何 docker 命令之前拒绝。它不帮你
     找最新版本。
   - 它**只做全新安装**：已有受限 `.env`、或已存在同名 Compose 项目/容器/卷时
     直接停下（不是升级/修复命令）。
   - 交互模式会问安装目录、备份目录、监听地址/端口、是否有反代、管理员用户名；
     `--non-interactive` 全部走参数/环境变量。地址默认 `127.0.0.1`，选
     `0.0.0.0` 会提示 TLS/反代/防火墙，**不会自动开放公网**。
   - **更新器默认不拉起**：不给 `--updater-digest`（或 `LING_UPDATER_IMAGE`）就
     不配置更新器，输出与安装状态都明确"网页更新不可用"；给了 `--with-updater`
     才会起独立项目 `ling-updater`。当前也不该指望网页更新（见
     [UPDATES.md §10](UPDATES.md)）。
   - 产物落在 `<dir>` 下：`compose/compose.image.yaml`（由源文件生成，唯一改动是
     api 的宿主端口绑定变成 `${LING_HOST:-127.0.0.1}:${LING_PORT:-6520}:6520`，
     避免默认发布到 `0.0.0.0`）、`.env`（0600）、`install-state.json`、
     `updater/{requests,state}`（**requests 2770、archive 2750、state 0750**）与
     `updater/.env`（0600）。三者共用**共享组**（`LING_UPDATE_SHARED_GID`，
     默认是更新器的 gid），API 容器用 compose 的 `group_add` 加入同一个组。

     > **为什么不是 0700**：api（uid 10001）与更新器（另一个 uid）是两个进程。
     > `requests` 必须让 API **以组身份写入**、更新器再按组读走，所以是
     > `2770`（组可写 + setgid，新建的 `request-*.json` 继承共享组）；
     > `archive` 只需更新器写、API 只 stat 文件名，`2750`；`state` 由更新器
     > **独占写**、API 只读 `status.json`，`0750`（组位故意不给写，API 伪造不了
     > 状态）。api 的挂载方向是 requests `rw` / state `ro`，worker 两个都不挂。
     > 安装器把这套权限记进 `install-state.json` 并逐项 `stat` 复核；按旧文档
     > 手工改成 0700 会把 API 写请求的能力改没（请求永远进不来）。
     后续所有 `ling` 子命令与备份/cron 都用这**同一组**文件与项目名。

   > **`ling` 命令从哪来，尚未交付**：`.github/workflows/release.yml` 不上传任何
   > 可执行资产（只发镜像与 Release 正文），所以现在没有官方安装包。仓库里的实现
   > 是 `deploy/cli/ling.py`，只能从仓库/发行包目录直接运行；正式分发方式
   > **待现场确认**，本节不提供 `curl | sh` 之类的捷径。

3. **核对（这三条都是只读，随时可以跑）。**

   | 命令 | 作用 |
   | --- | --- |
   | `ling status` | 以**实况为准**核对：容器、实际运行镜像摘要/ID、`/readyz`、**容器内进程自报的 version/commit**、卷身份、`alembic_version`、最近一次备份；安装记录与实况不一致会逐条指出。版本/提交走 `compose exec api python -c` 的进程证据，**不打 `/v1/version`**（它需要鉴权；见下方说明） |
   | `ling doctor` | 只读诊断：Docker/Compose 版本与权限、镜像与备份两处磁盘余量、目录/`.env`/状态/文件组权限、记录 vs 实况、GHCR 可达性、更新通道、备份 cron；按 `ok`/`warn`/`fail` 分级，有 `fail` 退出非 0。**不修改任何东西** |
   | `ling logs [--service api|worker|db] [--tail N] [-f]` | 代理到 `docker compose logs`，沿用同一组 `-f`、同一 `--env-file`、同一项目名 |

   > **为什么版本核对不用 HTTP**：`GET /v1/version` 挂 `CurrentAuth`，安装器 /
   > `status` / `doctor` 不持有用户凭据，打它只会拿到 401。没有为探活新增免鉴权
   > 端点，是因为那等于向匿名调用方公开部署指纹（版本 + 完整提交 = 精确到补丁
   > 级的情报），也和"只读端点一律鉴权"的既有决定冲突。现在改为在 api 容器里
   > 跑 `python -c 'from ling import __version__; ... LING_COMMIT'`：零 HTTP、
   > 零凭据，读的是应用进程自己的版本与镜像里烤入的提交。
   > 全仓库只保留 `/readyz` 一个免鉴权探活（容器 healthcheck 必须用它，且它
   > 未登录时只回状态、不含部署指纹）。

4. **备份与恢复演练（必须是同一组文件与环境）。**

   ```bash
   ling backup            # 复用 deploy/backup/backup.sh，校验 manifest 关键字段
   ling restore-drill     # 复用 restore_drill.sh，恢复到隔离库（库名 *_drill）
   ```

   两者默认用安装时记录的 Compose 文件组、`--env-file` 与项目名，不重新猜。
   备份产物、`manifest` 字段与墓碑重放的口径见
   [§4](#4-备份与恢复) 与 [§8.1](#81-定时备份)，镜像模式没有区别。

### 11.3 镜像模式的升级

**人工（推荐、也是当前唯一可宣称的路径）**：

```bash
python3 deploy/release/publish.py \
  --digest 'ghcr.io/qsbb/ling-api@sha256:<64 位小写 hex>' \
  --backup-dir <备份根，与 cron 同一个> \
  --env-file <受限 env 的绝对路径> \
  --compose-file <当前生效的镜像模式主文件>
#  例：--env-file /opt/ling/.env
#      --compose-file /opt/ling/compose/compose.image.yaml   （ling install 生成的那份）
# 启用了反向 WS 时补 --compose-override ...（与备份/cron 用同一组 -f）
```

它做的事与设计 §5 一致：参数校验（拒绝非 digest）→ 抢 `.ling-install.lock` 与
备份锁 → 只读预检 → 归档回退材料（旧镜像日期化 tag + `docker save`、旧 Compose
文件组、旧 env、当前 digest/commit/卷身份）→ `pull api worker` 并核验两个服务
**同一摘要** → 维护窗口（先停 api+worker 再备份）→ 单次显式迁移并核对
`alembic_version` == 目标镜像内 head → 写回受限 env 的 `LING_APP_IMAGE` 并启动 →
验证 `/readyz`、Web 清单、**容器内进程自报的 version/commit** 与容器实际镜像。

> 版本/提交的核对**不打 `/v1/version`**：该端点挂 `CurrentAuth`，发布流程不持有
> 用户凭据；也没有为它新增免鉴权端点（那会把部署指纹暴露给匿名方）。`publish.py`
> 用 `CONTAINER_VERSION_PROBE` 在容器内让应用自报 `ling.__version__` 与
> `LING_COMMIT`，并与 OCI 标注 `org.opencontainers.image.revision`、
> `RepoDigests`、容器 `Image` ID 交叉核对。HTTP 只保留 `/readyz` 一个探活
> （宿主端口那条用 `--network host` 的一次性容器打）。

退出码能区分失败阶段：`0` 成功、`2` 参数/非 digest、`10` 预检、`11` 锁、`12`
归档、`13` 备份、`14` 拉取/身份、`15` 迁移、`16` 迁移后的切换/验证失败。

**网页更新**：需要独立 `ling-updater` 与新的镜像更新通道。通道代码已就绪，且在
隔离项目 `ling-drill` 上真机跑通了三次连续升级；生产 `ling` 已于 2026-11-05 迁到
镜像模式，但**两次连续网页升级没在生产上走完**——目前只发布过一个镜像摘要
（`42bba216`），没有第二个 digest。镜像模式 compose 里 `LING_UPDATE_ENABLED` 仍
默认 `0`（`${LING_UPDATE_ENABLED:-0}`，可用 `ling install --with-updater --enable-updates` 或改 `.env` 打开），仓库里的 `deploy/releases.json` 仍是空骨架（真实部署会显示 `no_available_target`）。
所以现在仍不要对用户宣称"可以在网页里更新"（细节与未验证项见
[UPDATES.md §10](UPDATES.md#10-镜像模式的更新通道新协议隔离项目已真机演练)）。

### 11.4 三条硬边界

1. **`install.sh` 只支持源码模式**。它构建当前 checkout、跑 `pnpm build`、用
   `deploy/compose.yaml` 并把前端 bind 到 `/app/web`，**不读** `LING_APP_IMAGE`。
   **镜像模式的升级不能用 `install.sh`**：那不是升级镜像，而是把拓扑切回源码模式
   （或直接失败）。镜像模式的升级只有两条路——人工 `publish.py`，或网页更新
   （需更新器，当前不可宣称可用）。
2. **数据库迁移是单向的**。`publish.py` 单次显式迁移后，旧镜像不一定还认识新
   schema；两条通道都**不**做自动 schema 回滚，也**不**承诺"换回旧 digest 就回滚"。
   失败时的语义见 [UPDATES.md §10.6](UPDATES.md#106-失败语义) 与 §12.5。
3. **单机、计划内停机**。镜像模式不假装零停机：维护窗口内 api 与 worker 都会停，
   web 与机器人入站会断；备份、迁移、验证在当前单实例拓扑上串行完成。

### 11.5 `deploy/env.example` 里与镜像模式相关的变量

| 变量 | 模式 | 说明 |
| --- | --- | --- |
| `LING_APP_IMAGE` | **镜像模式必填** | 固定 `ghcr.io/qsbb/ling-api@sha256:<64 位 hex>`；`compose.image.yaml` 里是 `:?` 必填插值，缺值 `config` 直接失败；不认可变 tag |
| `LING_UPDATE_ENABLED` | 两种模式 | 默认 `0`；镜像模式也是插值（`${LING_UPDATE_ENABLED:-0}`），改用 `ling install --with-updater --enable-updates` 或编辑 `.env` 后重启 api |
| `LING_UPDATE_REQUESTS_HOST` / `LING_UPDATE_STATUS_HOST` | 镜像模式必填 | 宿主绝对路径；API 分别 `rw` / `ro` 挂载。源码模式有相对默认值（只适合本机试验） |
| `LING_UPDATE_SHARED_GID` | **镜像模式必填**、源码模式不需要 | 共享组 gid：requests 目录要被 api（uid 10001）与更新器（另一个 uid）同时用；`ling install` 会写它并设目录权限。不要写 0 |
| `LING_UPDATER_IMAGE` | 可选 | 更新器镜像摘要；只影响网页更新，缺失即"网页更新不可用" |

字段级注释（含会被拒绝的写法）都在 [`deploy/env.example`](../deploy/env.example)，
本节不复制。

> **两条通道不共用同一套开关**：镜像模式主文件的 `LING_UPDATE_ENABLED` 默认取
> `0`；源码模式的旧执行器 `deploy/update/executor.py` 会在它自己拉起的
> `install.sh` **子进程环境**里设 `LING_UPDATE_ENABLED=1`（只影响该子进程，不写
> 宿主 `.env`）。看到仓库里有 `=1` 不代表镜像模式开关被打开。

> **演练边界（按位置分开说）**：镜像模式的完整新装（`ling install` → `status` →
> `doctor` → `backup` → `restore-drill`）与 `publish.py` 升级**已在隔离项目
> `ling-drill` 上真机跑通**（不是 stub；记录见 `shots/deploy-research/`）。
> **生产 `ling` 已于 2026-11-05 迁移到镜像模式**（过程见 §6：首装曾失败、手工补齐、
> `ling adopt --force` 登记，记录里 14 项 `unknown`），但那是**一次人工迁移**，
> 不是演练；演练意义上的下面这些**仍未完成**：两次连续网页升级（目前只发布过一个
> 镜像摘要 `42bba216`，没有第二个 digest 可升）、head 不一致 / 并发请求 / 旧执行器
> 残留的故障注入、中断中任务的对账（仅单测）、生产库有真实数据时的恢复（生产库
> `messages=0`/`memories=0`）、arm64 运行、真实浏览器端到端点一遍。
> GHCR package 可见性、宿主架构、真实部署路径与端口仍要现场确认，
> 不要照抄 `/opt/ling`、`/vol1/ling-backups` 这类示例。

## 12. 从源码模式迁移到镜像模式

这是**人工维护作业**，不是自动流程；设计依据是
[IMAGE-DEPLOYMENT-DESIGN.md §6](IMAGE-DEPLOYMENT-DESIGN.md)，本节把它落成可执行的
检查清单。**默认保留数据**（除非你明确决定清空）。

### 12.1 为什么不能自动

- `publish.py` 只服务镜像模式的**日常发布**。它在预检里要求项目下 api、worker 各
  恰好 1 个 running 容器，且两者 `image_ref` **相同**、实际 image ID 也相同；源码
  模式下这两个容器分别跑本地 build 的 `<project>-api:latest` 与
  `<project>-worker:latest`——引用不同，预检直接拒绝（exit 10），提示
  “首次从源码模式迁移请按设计 §6 单独作业，不要用本脚本”。
- `ling install` 只做**全新安装**：发现已有受限 `.env` 或同名 Compose 项目/容器/卷
  就停下（“这不是升级命令”）。空白安装绝不会覆盖既有实例，也不会另造同名空卷。
- 旧执行器（`deploy/update/executor.py`）在镜像模式拓扑上会去构建另一套主文件，
  迁移前必须关闭 `LING_UPDATE_ENABLED` 并停用它（先备份其状态与配置）。

### 12.2 必须保留的东西

- **项目名** `ling`（Compose 的 `--project-name`，也是容器/卷名的一部分）；用
  `--project` 改过就沿用那个名字。
- **命名卷** `ling_app-data` / `ling_db-data`（`db-data` / `app-data` 键不变）。
  迁移时**不要** `down -v`、`docker volume rm`、`prune`，也不要在新项目名下误造
  空卷；用 `docker volume inspect` 与 `docker compose config` 核对挂载前后卷名与
  物理来源一致。
- **`.env` 里的密钥与既有配置**：`POSTGRES_PASSWORD`、实际生效的 `LING_ROOT_KEY`
  （或 `/data/root.key`）、渠道/WS 密钥、端口、Cookie 设置、`LING_POSTGRES_IMAGE`
  等。安装器不会自动重写这份文件。
- **端口不变**时记得同步核对 OneBot 上报地址、防火墙/反代与书签——它们指向的是宿主
  `LING_PORT`，不是容器内 6520。

### 12.3 必须补齐的东西

镜像模式主文件用了**必填插值**，老 `.env` 缺这些会让 `docker compose config`
**直接失败（这是有意的 fail-closed，不是 bug）**：

| 变量 | 缺了会怎样 |
| --- | --- |
| `LING_APP_IMAGE` | `config` 失败并指名缺变量（api/worker 都走同一条必填插值） |
| `LING_UPDATE_REQUESTS_HOST` | `config` 失败（不再有相对默认值） |
| `LING_UPDATE_STATUS_HOST` | 同上 |
| `LING_UPDATE_SHARED_GID` | `config` 失败（API 靠 `group_add` 进共享组才写得进 requests） |

做法：参考 §11.2 的布局准备宿主目录——`<dir>/updater/requests`（2770，带 setgid）、
`<dir>/updater/requests/archive`（2750）、`<dir>/updater/state`（0750，API 只读
`status.json`）与 `<dir>/updater/.env`（0600，更新器 Compose 的 `--env-file`），
属组都设成 `LING_UPDATE_SHARED_GID`；`status` 只允许宿主/更新器写，**不要 0777**。
然后核对（`<dir>` 用你实际安装根；由 `ling install` 生成的主文件在
`<dir>/compose/compose.image.yaml`）：

```bash
docker compose -f /opt/ling/compose/compose.image.yaml --project-name ling \
  --env-file /opt/ling/.env config -q
```

按设计 §4 还应清理源码模式专用的键：`LING_WEB_DIST_HOST`、`LING_PYTHON_BASE`、
`PIP_INDEX_URL`、`LING_COMMIT`，以及确已失效的 `LING_EDGE_*` / `LING_NGINX_IMAGE`。
**改端口前先核对现场拓扑**，不要把端口静默改掉。更新器镜像
（`LING_UPDATER_IMAGE`）可选，只影响网页更新。

备份任务也要跟着换文件组：镜像模式用 `compose.image.yaml`（+ WS override 时再加
`-f`）与同一份受限 env。`backup.sh` / `restore_drill.sh` / `install-cron.sh` 现在
支持 `LING_COMPOSE_OVERRIDE`（第二个 `-f`）与 `LING_COMPOSE_ENV_FILE`，但**这套
组合尚未在真机跑过**——迁移后要重装 cron，并核对 `crontab -l` 里确实带着同一组
文件与环境，再看下一个定时产物真的产出了 dump。

### 12.4 迁移前必须做的两件事

1. **一次真实备份**：迁移前还在源码模式，用 `deploy/backup/backup.sh`
   （`LING_BACKUP_VIA_COMPOSE=1`、`LING_COMPOSE_PROJECT=ling`、
   `LING_COMPOSE_FILE=<当前生效的 compose.yaml 绝对路径>`，见 §4）；装好镜像模式之后
   才用 `ling backup`。核对 `manifest` 的 `tombstones_state`、`media_state`、
   `keys_root` / `keys_env_set`，并把 `.env` 与 `keys/root.key` 作为**密钥材料**
   另存受限位置（与 dump 分开）。
2. **一次恢复演练**：`deploy/backup/restore_drill.sh <dump> <库名>_drill`
   （镜像模式装好后也可 `ling restore-drill`）恢复到隔离库，确认关键表、墓碑与
   `media_ref` 覆盖都对得上。没做过演练的备份，不能当作“有恢复点”。

同时记录**当前 api/worker 的精确 image ID**（`docker inspect --format '{{.Image}}'`）
——它是回退材料的锚点，事后从 `latest` 反推是无效的。

### 12.5 回滚材料与诚实边界

迁移前归档、离机保存：

- 旧镜像：分别 `docker save` api 与 worker 的**精确 image ID**。本地
  `<project>-api:latest` 这类标签会被后续 build 覆盖，“没 prune”不等于“可回退”。
- 旧 Compose 文件组（源码模式 `compose.yaml` + 当时生效的 override）。
- 旧 env（0600，含密钥）与源码模式下 `/app/web` 的 dist bind 路径；原来如果还有
  `edge` 反代，连它的网络/端口配置一起归档。
- 卷身份（`ling_app-data` / `ling_db-data` 的挂载点与 owner）。

**边界必须说清**：迁移/重启之后，旧二进制可以用于恢复应用，但 **schema 可能不再
与旧代码兼容**——严禁承诺“换回旧 digest 即回滚”。真需要回到旧版时，只能按**演练
过的同一恢复点**（DB dump + `app-data` 媒体/根密钥 + 同一份 env）恢复，恢复点之后
写入的数据会丢，须人工确认；导出并合并墓碑后再重放（见
[§4「重新应用撤回墓碑」](#重新应用撤回墓碑恢复后必做)）。不做自动 DB downgrade。

### 12.6 顺序概要（不是可复制脚本）

设计 §5 给的顺序是：预检并记录现状 → 用目标 digest 先 `pull`（不影响在线服务）→
维护窗口内停写、停 worker、用**当前** Compose 备份并校验 → 停旧 API →
（若有 `edge` 一并停并只删该容器）→ 单次显式迁移并核对 head → 起新 API 验证 →
起 worker 验证 → 重装/核对备份 cron → 记录 digest 与恢复点。

设计原文明确写了“这是待实现的操作协议，**不是可直接复制到现有宿主执行的命令清单**”；
本仓库能直接用的是 `ling install` / `publish.py`，而它们都**不处理首次源码→镜像
切换**（§12.1）。所以实际执行时按现场逐条核对，不要拼接示例命令。

> **本节记录的迁移已在生产真机执行过一次**（2026-11-05，但**没有按本节顺序做**：
> 那次首装失败、靠手工补齐与 `ling adopt --force` 收尾，见 §6），所以"整条链路能否
> 一遍走完"**仍未被验证**；设计 §8 的现场项（宿主架构、Docker/Compose 版本、
> GHCR 可见性、真实路径/端口/卷对应关系、备份可恢复性）也都**待现场确认**。
> 拿到一次按本节顺序走完的演练记录之前，不要把本节当成“已验证流程”。
