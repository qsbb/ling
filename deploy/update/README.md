# ling 受控 Web 更新链路（宿主执行器）

Web 控制台点“更新”时**不会**让 API 容器去部署。API 只写一个固定 JSON 请求，
宿主机上的独立执行器完成备份、取固定仓库 tag、构建、`docker compose` 切换和
验证。这样 API 容器不需要 Docker socket，也不持有宿主机凭据。

```
Web ──POST /v1/version/update-requests──▶ API（uid 10001）
                                          │ 原子写 pending.json（fcntl 单飞）
                                          ▼
                              /run/ling-update/requests  (rw, API 可写)
                                          │
                                 execute（宿主 root）
                                          ▼
                     /run/ling-update/status (ro，仅宿主可写)
                      ├─ heartbeat.json   心跳 + 实际部署 commit/version
                      └─ current.json     最近/当前任务（固定脱敏字段）
                                          ▲
Web ◀──GET /v1/version/update──────────── API（只读挂载 status）
```

## 一、安全模型

- **没有 Docker socket**：API 容器只挂 `requests`（rw）与 `status`（ro）。
  真正执行部署的是宿主机上的 `executor.py`。
- **目录权限**（`init` 生成，绝不 0777/世界可写）：
  - `requests_dir`：`0770`，owner 宿主用户、group 容器 API gid（10001）。
    API 通过 group 位写；宿主执行器是 owner。
  - `status_dir`：`0755`，宿主写、容器只读；文件 `0644`。
  - `state_dir`：`0700`，只给宿主：锁、journal、备份、git mirror、release 暂存、
    `installed.json`、恢复说明与安装日志。**不挂进任何容器**。
- **单飞 + pending 所有权**：`requests/.lock` 是 pending 的唯一所有权锁，**API 与
  宿主共享**。API 在锁内读一遍 pending：已存在合法请求就返回“进行中”，**绝不覆盖**
  （覆盖会吃掉宿主的在途 claim）；存在但不可解析就返回 `protocol_unsupported` 并
  保留文件给人工。宿主在锁内 claim（复查同一 `request_id` → 写 running + journal
  start → compare-and-unlink 删除该 ID），清理也只删自己那个 ID，因此窗口内新到达
  的请求不会被删掉。执行器另有 `state_dir/host.lock`，再拿 `install.sh` 同一个安装
  锁（`deploy/.env` 解析后的父目录 + `.ling-install.lock`），拒绝并发执行/并发安装。
- **不信任请求**：宿主自己按固定 URL（`api.github.com/repos/qsbb/ling`）查询
  Release 与 tag，并独立校验：stable、非 draft/prerelease、tag 严格 `vX.Y.Z`、
  annotated tag 解引用后的 commit 与 tag 一致、且**必须比当前版本新**。不采信
  API 传来的 `target_commitish`，也不接受客户端给任意 URL。
- **环境文件不复制**：`release/deploy/.env` 是指向原 env（`config.env_file` 解析
  后的真实路径）的受控 symlink，安装锁锚点因此与原部署一致；候选 tar 里自带
  `deploy/.env` 会被拒绝（`source_invalid`）。原 env 只读，绝不修改。
- **运行挂载不外漂**：候选 compose 与当前 compose 都由 `docker compose config
  --format json` 结构化解析（不打印任何配置），对每个 service/target 比较
  type/source/read_only。`/app/web` 指向新 release 的 dist；证书、数据卷、更新
  通道等运行数据固定为当前绝对来源。执行器把“只修运行 bind 来源”的私有
  override 写成 JSON 落盘（0600），安装和后续所有 compose 调用都带同一份。
- **不做盲重试**：journal 是持久消费记录（request_id 一旦 start 就不会再执行，
  end 过的 replay 直接 `invalid_request`）；`current.json` 停在 running/start
  无 end、journal 损坏或超限，一律转 `needs_manual` 并 fail closed。
- **人工门禁**：`needs_manual` 会阻止 API 再发起更新；执行器在此期间**不执行也
  不删除**新到达的 pending，直到宿主用 `adopt-current`（先取两把锁、再完全自证
  release/override/镜像/挂载）显式解除，之后按到达顺序处理。
- **不自动回滚数据库/镜像**：失败时保留旧部署树、旧镜像、备份和恢复说明。

## 二、部署与初始化

1. 在 `deploy/compose.yaml` 中把通道目录挂进 **api**（compose owner 负责）：
   - `${LING_UPDATE_REQUESTS_HOST}:/run/ling-update/requests`（rw）
   - `${LING_UPDATE_STATUS_HOST}:/run/ling-update/status`（ro）
   - 环境变量只在 api 传：`LING_UPDATE_ENABLED`、`LING_UPDATE_REQUESTS_DIR`、
     `LING_UPDATE_STATUS_DIR`。
2. 宿主准备（**显式执行**，本仓库开发流程不会自动跑）：
   ```bash
   sudo install -d /etc
   sudo cp deploy/update/config.example.toml /etc/ling-update.toml   # 按需改路径
   sudo /usr/bin/python3 deploy/update/executor.py --config /etc/ling-update.toml init
   sudo /usr/bin/python3 deploy/update/executor.py --config /etc/ling-update.toml adopt-current
   sudo /usr/bin/python3 deploy/update/executor.py --config /etc/ling-update.toml doctor
   sudo systemctl enable --now ling-update
   ```
   `init` 只在缺失时创建目录并设置权限，不碰现有部署；`adopt-current` 先拿
   宿主锁与安装锁、再逐副本核验真实 commit/version 与就绪状态，然后才记录
   “现在实际运行的是哪个 commit/version”，并解除 `needs_manual` 门禁；它不部署
   任何东西。
3. `LING_COMMIT` 必须由镜像构建/运行环境注入为**完整 40 位**提交号（compose /
   Dockerfile owner 负责）。注入缺失时执行器拒绝更新（`installed_commit_unknown`），
   因为无法证明“切换后跑的是目标提交”。
4. `install.sh` 契约（另一个 owner 实现，执行器只调用不修改）：
   - `LING_INSTALL_PHASE_FILE`：安装器在 `build`/`deploy`/`verify`/`complete`
     写入标记。失败且最后标记是 `build` → `failed`（旧服务仍在）；失败/超时且
     标记是 `deploy`/`verify`/`complete`，或标记不可读 → `needs_manual`。超时另
     要求“安装进程组已确认全部终止”才允许判 `build` 阶段失败（见第五节）。
   - `LING_INSTALL_LOCK_FD`：执行器以 `pass_fds` 传入**已持有**的安装锁 FD
     （fd ≥ 3，指向 `deploy/.env` 父目录的 `.ling-install.lock`）。当前
     `deploy/install.sh` 已验证 inode 并做真实 flock 探测，拒绝伪造 FD；两侧
     契约仍需在 staging 做一次真实联调。
   - `LING_INSTALL_COMPOSE_OVERRIDE`：执行器生成的私有 compose override 绝对
     路径（JSON）。当前 `deploy/install.sh` 会校验为绝对、普通、非 symlink
     文件，并作为额外 `-f` 追加到同一 compose 项目（不另起项目）；仍需真实
     Compose 联调确认 `/app/web`、证书、更新通道的合并结果。

## 三、执行器命令

| 命令 | 作用 |
| --- | --- |
| `init` | 创建 requests/status/state 及子目录，设置安全权限（不部署） |
| `doctor` | 检查 checkout 干净度、目录权限、env 权限、docker、installed 记录、journal 完整性、override 与安装锁可用性 |
| `run --once` | 只执行一轮（适合 cron / 手动排查） |
| `run` | 常驻轮询；构建期间心跳线程持续写 heartbeat |
| `status` | 打印 installed/heartbeat/current job（宿主私有输出） |
| `adopt-current [--release-root PATH] [--compose-override PATH]` | 核验并采用当前运行部署（不能自证就拒绝，见第五节） |

执行器导入共享协议的方式：先看 `installed.json` 记录的 **live release**
（`<release>/src`），其次 `<source_root>/src`，最后脚本自身 checkout 的 `src`；
并检查必需符号（`PendingFile`/`read_pending_file`/`heartbeat_payload` 等）都在，
缺一个就拒绝启动。**协议必须与真正在跑的 release 一致**：升级后 source_root
可能还停在旧代码，旧协议会把新 schema 的 pending 静默当成 `idle`（QA M1）。
宿主 Python 需 ≥ 3.12（`tarfile` 的 `filter="data"` 与 `tomllib`）。

心跳里显式写 `status`：`ok` / `manual` / `protocol_unsupported`，并写
`pending_state`（`absent`/`ok`/`invalid`）。API 只把显式 `ok` 且新鲜的心跳当作
在线；`status` 缺失/未知、或 `pending.json` 存在但不可解析时，API 返回
`protocol_unsupported` 并拒绝新请求，宿主也**不执行**该文件（既不删除也不猜）。
人工处理：先确认/修复或移走 `requests/pending.json`，再重启执行器；若同时还
卡在人工门禁，按第五节 `adopt-current`。

## 四、一次更新的阶段

`queued → preflight → backup → fetch → verify_source → build → deploy → verify → complete`

- **preflight**：`docker compose ps -q --status running api` 拿到**全部**运行副本
  （默认 2 个），逐个 `docker exec` 读取 `LING_COMMIT` 与 `ling.__version__`
  （只打印这两个值，不打印 secret），要求副本数等于 compose 期望值且所有副本
  一致，再与请求的当前版本比对；同时用 `docker inspect --format '{{.Image}}'`
  逐副本记录 **api 与 worker 各自的完整旧 image ID 集合**：每个服务必须非空、
  去重、所有副本同一个 ID；任一服务读不到/为空/不一致 → `installed_image_unknown`
  （此时还没动服务，可安全重试）。`git status --porcelain` 拒绝
  tracked/untracked 未提交改动（gitignored 的 runtime 不算；绝不
  checkout/stash/reset/clean）。
- **backup**：先证明**数据库身份**：从 `docker compose config --format json`
  解析 api 的 `LING_DATABASE_URL` 与 db 服务的 `POSTGRES_USER/DB/PASSWORD`，要求
  用户/库/口令逐字一致、host 必须是 db 服务名或其 alias（拒绝
  `localhost`/`127.x`/`::1`），再确认只有一个运行中的 db 容器、其网络 alias
  含该 host，并在容器里查 `current_database()/current_user` 与 DSN 一致。
  然后跑 **live release 自己的** `deploy/backup/backup.sh`（普通、可执行、
  realpath 仍在 live release 内），用**可信 env 文件的键白名单**补齐 compose
  变量（不整份继承宿主环境、不记日志）。备份使用的 compose 模型是
  `docker compose config` 的**合并结果**（base live compose + live override +
  canonical env + 原 project），写到 `backup_dir/compose-resolved.yaml`（0600，
  含口令，勿外传）并重新解析比对服务/镜像/环境/挂载与安装所用模型完全一致。
  脚本还收到 `LING_COMPOSE_ENV_FILE`/`LING_COMPOSE_OVERRIDE`（前向兼容，见第六
  节）。最后自己解析 manifest，分三层校验：

  1. **必备产物**：`tombstones_state` 必须是 `ok`、dump 非空且 `PGDMP` 头正确、
     墓碑清单存在、manifest 里 `dump=` 指的就是这份 dump；
  2. **覆盖字段**（fail closed）：`keys_root ∈ {present, absent}` 且
     `(keys_root, keys_root_source)` 只能取 `present+file` / `absent+env` /
     `absent+none`；`keys_root=present` 时 `keys_root_file` 必须是
     `keys/root.key`、该文件必须存在且非空、`keys_root_sha256` 与实际字节一致
     （和媒体侧对称：present 是"字节被归档了"的断言，必须由文件本身证明）；
     `media_state ∈ {present, partial, absent}`，且
     `media == "absent"` 必须 ⟺ `media_state == "absent"`；`media_state=present`
     时归档必须存在、非空，`media_sha256` 与实际字节一致；manifest 记了
     `dump_sha256` 也核；`media=` 只允许是备份目录里的文件名（带路径直接拒绝）；
  3. **dump 内容**：用 `pg_restore --list` 真读 dump 目录（stdin 传入，要求含
     `public.alembic_version` 与业务表）；`alembic current` 记录在
     `backup_dir/alembic-current.txt`。

  `media_state=partial`（归档时正在上传，可能漏文件）**有意不硬失败**：升级不
  替换 `app-data` 卷，硬失败只会让"上传中提交升级"反复失败；但会写显式告警到
  `backup_dir/verify-warnings.txt`（0600）与 `logs/<id>-backup.log`，恢复前必须
  核对 `media_ref`。旧格式 manifest 缺这些键时同样不失败，但会记"未能校验"告警
  （不静默）。备份目录 `state_dir/backups/<id>` **目录** 0700，
  `deploy.env.bak` / `compose-resolved.yaml` / `verify-warnings.txt` /
  `dump-toc.txt` / `alembic-current.txt` / `keys/root.key` / `media-*.tar` 是 0600，
  而 `dump` / `manifest` / `tombstones` 由 `backup.sh` 按 umask 写成 **0644**
  （脚本不 chmod 它们；0700 的目录才是这一层的实际屏障）。该目录不挂给 API。
  任何一步失败都不调用安装器。
- **fetch / verify_source**：`git mirror` 只 fetch 固定仓库的 `refs/tags/vX.Y.Z`，
  `rev-parse tag^{commit}` 得到 40 位 commit 与请求比对；同时独立查询 GitHub
  Release/tag（见安全模型），不通过即 `release_unverifiable`/`not_newer`。
  `tar` 解到 `state_dir/staging/<id>/repo`（宿主私有、`filter="data"` 防穿越，
  拒绝绝对路径、`..` 与自带 `deploy/.env`），再用 AST/JSON 安全解析（不执行候选
  Python）`pyproject.toml`、`src/ling/__init__.py`、`clients/web-console/package.json`
  三个版本必须都等于 tag。最后用 `docker compose config --format json` 结构化
  比较当前/候选挂载并生成 override（见安全模型）；无法证明的变化一律
  `compose_bind_changed`。
- **build / deploy / verify**：`release/deploy/.env` 建为指向原 env 的 symlink；
  调 `release/deploy/install.sh --project P --port N`，导出 `LING_COMMIT`、
  `LING_WEB_DIST_HOST`、`LING_UPDATE_REQUESTS_HOST`、`LING_UPDATE_STATUS_HOST`、
  `LING_UPDATE_ENABLED=1`、`LING_INSTALL_PHASE_FILE`、`LING_INSTALL_LOCK_FD`
  与 `LING_INSTALL_COMPOSE_OVERRIDE`。安装器自己
  构建镜像、启动、发布前端；本执行器只通过阶段标记判断失败有没有发生在切换之后。
- **verify**：先按 compose 期望副本数拿到**全部**运行副本，再在**每个 api
  容器内**探 `127.0.0.1:6520/readyz`：探针显式禁用重定向与代理，只接受 2xx
  （`urllib` 默认会跟随 3xx；301 只说明端口上有东西在应答，不是副本就绪）；
  同时要求每个
  副本的 commit/version 都等于目标。然后校验 web manifest：release 的
  `.ling-web-manifest.json`（schema=1、含 `index.html` 与至少一个 asset、
  sha256 与 dist 文件一致），并逐个副本读容器内 `/app/web/.ling-web-manifest.json`
  比对。最后探宿主端口（`LING_PORT`，api 直连）上的 `/readyz`，**严格只接受
  2xx**（301 判失败，不跟随）。
  全部通过才写 `installed.json`（含 api/worker 旧 image ID 集合、容器 ID、
  commit/version、备份目录、override 路径、副本 ID）并把任务置为
  `succeeded/complete`。

## 五、失败与恢复

`failed` 表示失败发生在**构建完成之前**（或阶段标记明确停在 `build`）：旧服务仍在
运行，可直接修复后用**新的确认**重新提交（`failed` 不阻止新任务；同一
`request_id` 不能重放）。意外异常按阶段分类：preflight/backup/fetch/verify_source
→ `failed`（可重试）；build 之后 → `needs_manual`（状态未知）。

超时按同一原则分类：只有「阶段标记可读且恰好是 `build`」**且**「宿主确认安装进程
组内已无存活进程」时才判 `failed / build_failed`。`docker compose build` 由 daemon
执行，杀进程组并不证明 daemon 已停止构建；但 daemon 无法切换容器（切换发生在
deploy 阶段）。其余情况（无标记、标记已是 deploy/verify/complete、进程组仍存活、
被外部 kill）一律 `needs_manual / installer_timeout`。

`needs_manual` 表示“无法证明服务仍停在旧版本”：执行器不碰数据库、不删镜像、不
自动回滚，只写 `state_dir/recovery-<request_id>.txt`（宿主私有）。该文件记录本次
已捕获的上下文：`previous_image_api` / `previous_image_worker` 各自的 image ID 集合
（缺失时明确写 `unrecorded`）、对应容器 ID、`live_release_root`、
`canonical_env_file`、`compose_override`、`compose_project`、备份目录与备份文件
清单。恢复**只在宿主上做**（Web 没有“清除失败任务/回滚”按钮，点了也不会让容器
回到旧版本）：

1. **先看清现状**。不要从旧源码 checkout 裸跑 `docker compose`（相对 bind 会静默
   指向旧树的前端/证书）。所有手工 compose 命令都必须组合：live 基础
   `compose.yaml` + 持久 override + canonical env + 原 project。`compose ps` 的
   IMAGE 一列只是名字，不是证据；要读实际运行 commit/version。
2. **`up -d --no-build` 不是回旧镜像**：`build` 已经把新构建重新指向 Compose 使用
   的镜像名（默认 `latest`），`--no-build` 只是“这次不再构建”，跑的仍是新镜像。
   真正的回退必须把**更新前核实保存的 api 与 worker 两个 image ID** 重新 pin 回
   Compose 使用的名字（先 `docker image inspect <id>` 确认还在本机）。`recovery`
   文件里任一服务是 `unrecorded` 时，**不能**声称“回旧版本”——只能向前修复或走
   第 4 步的数据库路径。
3. **数据库 schema 必须先核对**：新版本可能已经跑过迁移。回旧镜像前比较
   `backup_dir/alembic-current.txt` 与旧镜像是否兼容；旧代码不认识新 schema 时
   “镜像回旧”不等于“服务回旧”。执行器不做自动 schema 回滚。
4. **逻辑 dump 不是 PITR**：备份是 `pg_dump` 全量**逻辑**备份，恢复会丢掉备份之后
   写入的全部数据（不是回到某个时间点）。恢复后必须**重新应用**撤回墓碑（只核对
   清单不够），再校验 `/readyz` 与关键数据。
5. 旧 release 树、旧镜像与数据卷都不会被自动删除；确认新版本稳定前不要清理。

`recovery-*.txt` 只提供**安全检查清单与占位项**，不是可一键执行的脚本（路径/ID
必须先人工核实）。完整步骤以 `docs/UPDATES.md` 第 8 节为准。

处理完后由宿主执行 `adopt-current` 解除门禁。它要求**完全自证**：

- 持有宿主锁 + 安装锁（证明没有并发安装/更新）；
- 全部 api 副本 commit/version 一致、数量与 compose 期望一致；
- live release 根从**运行容器**的 `/app/web` 挂载反推（或人工 `--release-root`
  显式给出，同一标准核验）：compose 在、dist 清单与每个副本一致、三个版本文件都
  等于运行版本、live 备份脚本可执行；
- api 与 worker 的镜像 ID 都能完整读出（每服务唯一）；
- 运行容器的真实挂载与「该 release + 该 override」的 compose 模型逐项一致，且
  宿主端口 `/readyz` 严格 2xx。

任一条不满足：**不写 installed、不解除门禁**——旧 `release_root`/旧 override 配
新运行 commit 的半切换状态绝不会被盖章。此时先补齐完整来源（显式
`--release-root` / `--compose-override`，或按第二节重新安装整个 release）再重试。
`needs_manual` 期间执行器**不执行也不删除**新到达的 pending；解除门禁后按到达
顺序处理（请求里的 `current_commit` 不匹配时 preflight 会拒绝它）。

## 六、已知限制 / 边界

- `LING_INSTALL_PHASE_FILE`、`LING_INSTALL_LOCK_FD`、
  `LING_INSTALL_COMPOSE_OVERRIDE` 由**另一个 owner 的安装器实现**；本执行器
  不修改 `install.sh`。当前分支已实现，但两侧仍需在 staging 做一次真实联调；
  联调前任何阶段标记缺失都按 `needs_manual` fail closed。
- web manifest 由安装器构建产物生成；本执行器只校验，不替它构建。
- `LING_COMMIT` 注入同样依赖 compose/Dockerfile owner；缺失时拒绝更新。
- 宿主执行器需要宿主机 Python ≥ 3.12 与 `git`、`docker compose`、`pg_dump` 环境；
  它不依赖在线 API 进程或 API 的 venv。
- 不做自动取消/伪取消：取消一个已经进入构建的任务无法真正回滚，因此只保留
  `needs_manual` 语义。
- 备份校验覆盖：自定义 dump 头 + `dump_sha256`、墓碑状态与清单、manifest 的
  `keys_root`/`keys_root_source`/`media`/`media_state` 枚举与自洽、
  `keys/root.key` 的存在性与 `keys_root_sha256`、`media_sha256` 与产物存在性、
  `pg_restore --list` 目录与 alembic 版本。它**仍不代替**一次真实恢复演练（尤其
  `docker compose exec` 的退出码透传与 `tar -cf -` 的字节干净性只有真机能证明）。
- `media_state=partial` 不阻断升级（有意取舍：升级不替换 `app-data` 卷），只在
  `verify-warnings.txt` 与备份日志里留显式告警——看到它就在恢复前核对 `media_ref`。
- 备份超时 `BACKUP_TIMEOUT_SECONDS=1800` 现在包含当前 `/data/media` 的全量打包；
  媒体卷很大时会超时 → `backup_failed` → 升级阻断（fail closed）。要缩短本次打包
  只能缩小 `/data/media`、调大该上限或换更快存储；**减少历史备份份数不会让本次
  `tar` 变快**（执行器每次备份都新开 `backups_dir/<request_id>`，调用 `backup.sh`
  时只传位置参数）。`deploy/backup/backup.sh` 提供**默认关闭**的轮转
  （`--prune-keep N` / `--prune-days D` / `--dry-run`，N 含本次、历史回看 N−1 天；
  零撤回时 0 字节墓碑是合法产物，正常参与轮转），但执行器调用时只传位置参数、
  **不带任何 `--prune-*`**，所以升级备份目录不会被它轮转删除；`install-cron.sh`
  装出来的每日 cron 行同样默认不带。空间与轮转要运维显式开启并自己负责（先
  `--dry-run` 看计划）；轮转只省磁盘空间，不缩短本次备份时间。
- `deploy/backup/backup.sh`（另一个 owner 维护）当前只接受 `LING_COMPOSE_FILE` +
  `LING_COMPOSE_PROJECT`，还不接受 `-f override` / `--env-file`。执行器因此把
  **合并后的** compose 模型（base + override + env）落成 `compose-resolved.yaml`
  交给它，并重新解析比对与安装所用模型完全一致；同时仍传 `LING_COMPOSE_ENV_FILE`
  / `LING_COMPOSE_OVERRIDE` 供脚本后续支持。脚本若支持，应当把它们分别拼成
  `--env-file` 与额外的 `-f`（与安装器同 canonical 值）。
- 逐副本就绪探针固定访问容器内 `127.0.0.1:6520/readyz`（compose 里 api 的
  `expose: 6520`）；改容器内端口要同时改 `executor.py` 的 `API_READYZ_PORT`。
  宿主侧端口（`LING_PORT`）是另一回事，由安装器按 compose 解析结果决定。
- API 侧 GitHub 查询（Release/tag）固定 `trust_env=False` + `proxy=None` +
  不跟随重定向，并流式限长 256KB；这也意味着**不支持**通过环境代理访问 GitHub，
  需要代理的环境必须改为在网络上放行直连。
- journal 是持久消费账本，不会自动轮转；超过 4MB 后执行器按“损坏”fail closed
  （`needs_manual`）。需要人工归档时先确认没有 pending，再移动/清空
  `state_dir/journal.jsonl`；这会同时丢失历史消费记录。
