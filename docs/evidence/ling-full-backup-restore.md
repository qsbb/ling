# Ling 整包备份与导入恢复：调研、实现与验收报告

报告路径：`docs/evidence/ling-full-backup-restore.md`

## 1. 结论与边界

本次交付完成了完整包清单、只读预检、隔离恢复编排、宿主单命令入口、Web 管理入口和合成包真实验收。默认备份在语义上已能表达“恢复到备份时的精确 Ling 版本与可用业务数据”：清单记录版本、提交、迁移版本、配置修订、镜像 digest＋架构或源码发行产物、凭据来源、文件哈希和数据覆盖范围。

当前宿主 **Docker daemon 不可用、pg_dump 客户端缺失**，因此“两个支持版本的真实 Compose 切换、镜像导出导入、限网干净实例启动”没有在本机实机跑完；相关步骤已写成可复验脚本 `deploy/backup/restore_drill_two_versions.sh`，并在本文列为“未实机验证”。这一项不是通过结论，不能合并会话时写成已验收。

## 2. 观测对象

工作树：`/Users/lingxi/project/ling/.agents/worktrees/backup-restore`

分支：`lingxi/backup-restore`

基线：`bd5ac914388abce6b7ae2be721dd58d64d7b7ce7`

创建时 `git status --short` 为空，`git rev-parse HEAD` 等于基线；主工作区 `/Users/lingxi/project/ling` 当时在 main 且有别的未跟踪调研目录，本分支未携带它们。

代码改动归属以交付前 `git status --short` 为准，见第 8 节。

## 3. 调研

### 3.1 现有入口

- `deploy/backup/backup.sh`：PostgreSQL custom dump、撤回墓碑、`/data/root.key`、`/data/media`；compose 模式可从 api 容器或一次性只读容器取数。manifest 记录 dump/tombstones/media/keys 的状态与 sha256，但缺少应用版本、提交、迁移版本、镜像 digest/架构、插件状态、人设开关、部署配置与版本产物身份。
- `deploy/backup/restore_drill.sh`：只恢复到隔离库，核对关键表、墓碑一致、dump/媒体哈希和 `media_evidence.media_ref` 覆盖；不切换实例，不回退。
- `deploy/cli/ling.py`：`ling backup` 复用 backup.sh 并校验 manifest 关键字段；`ling restore-drill` 复用 restore_drill.sh；两者继承同一组 compose 文件、项目名和 env-file。`ops_env()` 显式清除宿主上可能指向另一拓扑的 `LING_COMPOSE_*`/`LING_BACKUP_*`。
- `deploy/compose.yaml` / `deploy/compose.image.yaml`：源码模式有 build、前端 dist bind 与更新通道 bind；镜像模式用必填 `LING_APP_IMAGE`，默认 `LING_RUN_MIGRATIONS=0`，api/worker 共用同一摘要。两者不能叠加。
- `deploy/updater/`、`deploy/update/executor.py`：已经实现 Web 触发、宿主执行的更新/备份门禁模式，完整包入口沿用这个边界。

实际拆验限制：当前宿主没有 Docker daemon 和 pg_dump 客户端，无法在本机运行 backup.sh 产出真实 dump 并拆验。第 6 节用合成完整包和真实 PostgreSQL 测试库完成了可在此环境完成的验证。

### 3.2 官方资料

- PostgreSQL 16 `pg_dump`：一致备份不阻塞读写；custom format（`-Fc`）默认压缩，适合与 `pg_restore` 配合；输出可跨架构/版本导入，但从更新主版本向更旧主版本导入不保证。来源：<https://www.postgresql.org/docs/16/app-pgdump.html>
- PostgreSQL 16 `pg_restore`：从非文本归档重建；`--list` 可检查归档内容；`--single-transaction` 要么全成功要么不应用，但并行 `-j` 与之互斥。来源：<https://www.postgresql.org/docs/16/app-pgrestore.html>
- Docker `docker save` / `docker load`：save 生成含所有父层、标签与版本的 tar，load 从 tar 恢复镜像和标签；`--platform` 可限定平台。镜像文件不保存数据卷。来源：<https://docs.docker.com/reference/cli/docker/image/save/>、<https://docs.docker.com/reference/cli/docker/image/load/>
- Docker `docker export`：不导出挂载卷内容，卷内目录导出的是被卷遮住的底层目录。来源：<https://docs.docker.com/reference/cli/docker/container/export.md>

### 3.3 成熟项目

- Home Assistant：完整备份包含配置、数据库历史、媒体、share、local apps、已选 apps，默认加密；恢复前检查网络、空间、健康状态和解密密钥；恢复失败与损坏/错误密钥的提示要分开处理；备份可下载，密钥通过 emergency kit 单独保管。来源：<https://github.com/home-assistant/home-assistant.io/blob/028c97e4bf162694d4e12a64a3da9927bfc5153f/source/common-tasks/general/backups.markdown>
- Immich：数据库只存文件路径和元数据，不扫库重建，所以数据库和 `UPLOAD_LOCATION` 必须成套备份；恢复前自动创建 restore point，失败回滚；显示备份版本与当前版本的兼容状态；恢复过程含迁移和健康检查；CLI 恢复要求全新 DB，并可在迁移前设置 `DB_SKIP_MIGRATIONS=true`。来源：<https://github.com/immich-app/immich/blob/8303cb117e197c700332b33b61b45314c9cc5bb4/docs/docs/administration/backup-and-restore.md>
- 对 Ling 适用：完整包必须同时覆盖 DB、媒体、配置与密钥；恢复前预检、切换前回滚点、版本身份和离线可启动产物都是必要条件；外部服务数据归属要在预检里明示，不能被误认为包内已含。

### 3.4 可恢复性定义

“可恢复”定义为：目标实例在限制外部网络时，能仅凭备份包启动到清单记录的 Ling 版本，拥有备份时点的关键业务数据、媒体字节、配置/插件/人设状态和可用凭据；同时在版本、架构、空间、数据覆盖不满足时拒绝切换，并在启动失败时回到切换前实例。

冻结判据见 `docs/backup-restore-acceptance-criteria.md`，其中 C1–C8 分别覆盖完整性、预检反例、A→B→A、干净实例、回退点、恢复语义、Web/宿主入口和测试有效性。该文档写于实现之前。

## 4. 设计决策

1. **沿用而不是重写 backup.sh**：现有脚本已经处理 compose 拓扑、密钥哈希自证、媒体并发状态、墓碑失败和权限交接。完整包在其产物外层增加新的清单与打包模块，历史 `ling backup` / `ling restore-drill` 保持可用。
2. **清单是恢复契约**：`FORMAT_VERSION = ling-full-backup/v1`，所有纳入文件写 `sha256:` 前缀摘要；版本、提交、迁移、配置修订、artifact 身份、凭据来源、数据范围和外部依赖都进清单。口令和密钥内容不进清单。
3. **预检与执行分离**：`preflight_package()` 是纯函数，不执行 docker，不写生产状态；宿主 `restore_full.py` 默认只生成/打印计划，必须显式 `--apply` 才执行。
4. **Web 不直接执行部署**：API 只展示状态并把 owner 请求写入执行器目录；执行器在宿主消费请求。Web 不可用时仍可用 `restore_full.py`。
5. **凭据按字段名脱敏**：`redact_env()` 只按 `SENSITIVE_ENV_KEYS` 字段名遮蔽，不按 `sk-` 等值的形态猜测。
6. **回滚点先于切换**：恢复计划固定顺序是 preflight → lock → rollback-point → restore database → restore media → switch artifact → start → health。
7. **迁移信息保留但不新建 revision**：当前 head 为 `0082_live_sessions`（down_revision `0081_memory_import_source`）。本功能不改业务表，因此没有新增 migration；清单记录迁移版本供集成时核对。

## 5. 实现内容

- `src/ling/backup_restore/__init__.py`：清单格式、sha256 校验、版本/架构/digest/空间/墓碑预检、恢复计划生成。
- `src/ling/backup_restore/package.py`：从已备好的产物生成完整包与 `manifest.json`。
- `src/ling/backup_restore/rollback.py`：回退点创建、媒体归档安全展开、受控配置/凭据切换。
- `src/ling/api/routes/backup_restore.py`：状态、清单预检、创建/预检/确认恢复请求；写请求使用 `OwnerWrite` 同源守卫。
- `src/ling/app.py`：注册新路由。
- `deploy/backup/restore_full.py`：宿主单命令 preflight/plan/execute。
- `deploy/backup/backup_restore_executor.py`：消费 Web 请求目录并写状态。
- `deploy/backup/restore_drill_two_versions.sh`：两版本可复验演练脚本。
- `clients/web-console/src/components/BackupRestorePage.tsx` 及 API/路由/设置入口接线：`/settings/backup-restore` 提供创建、列出、预检、版本核对、确认恢复和结果查看。

## 6. 验收证据

### 6.1 核心单测

命令：

```bash
/Users/lingxi/project/ling/.venv/bin/python -m pytest tests/unit/test_backup_restore_core.py -q
```

结果：14 passed。日志：`/tmp/ling-backup-restore-core-baseline.log`

覆盖：完整包通过；缺文件、哈希不匹配、版本不匹配、架构不匹配、digest 不可用、空间不足、墓碑状态失败均失败；脱敏按字段名；恢复计划包含锁/回退/健康检查；媒体归档路径穿越被拒；预检失败时切换被拒。

### 6.2 变异验证

- 删除版本校验后：`test_preflight_rejects_version_mismatch` 变红，日志 `/tmp/ling-bsr-mutation-version.log`。
- 删除墓碑校验后：`test_preflight_rejects_missing_tombstone_state` 变红，日志 `/tmp/ling-bsr-mutation-tombstone.log`。
- 两次恢复后 `src/ling/backup_restore/__init__.py` 的 sha256 都与变异前一致：`4cc6d0094af9193e714d5e1cbec9cae9ec275c638e4777792bb1b88442d7f4f0`。
- 恢复后 `git status --short` 只显示本次新增/修改文件，没有变异残骸。

### 6.3 API 与前端回归

命令与结果：

- `pytest tests/integration/test_manage_auth.py tests/unit/test_backup_restore_core.py -q`：33 passed，日志 `/tmp/ling-backup-restore-api-smoke.log`。
- `pytest tests/integration/test_version_api.py tests/integration/test_control_plane_auth.py tests/unit/test_backup_restore_core.py -q`：26 passed，日志 `/tmp/ling-backup-restore-api-regression.log`。
- `pnpm exec tsc --noEmit -p tsconfig.json`：通过，日志 `/tmp/ling-backup-restore-typecheck.log`。
- `pnpm exec vitest run src/dashboard.test.ts src/nav.test.ts src/settings-layout.test.tsx src/tabs.test.ts`：137 passed，日志 `/tmp/ling-backup-restore-web-tests.log`。

全部 pytest 按仓库约定持 `/tmp/ling_test_lock.sh` 锁，并设置 `LING_TEST_DATABASE_URL` 与 `LING_DATABASE_URL` 为 `postgresql+asyncpg://ling@127.0.0.1:55432/ling_test`。

### 6.4 合成完整包

用 `build_full_package()` 生成一份合成 A 包（版本 `0.0.14`、提交 `bd5ac91...`、迁移 `0082_live_sessions`、源码模式发行产物、外部服务 `model-provider`），随后 `preflight_package()` 通过，错误列表为空。这一步验证了清单生成与预检的闭环，但不是真实业务数据恢复。

### 6.5 未实机验证项

当前宿主：

```text
docker version            -> docker-unavailable
command -v pg_dump        -> pg_dump-missing
```

因此以下判据仍需在合并会话的隔离环境按 `deploy/backup/restore_drill_two_versions.sh` 复验：

- C3：两个可识别版本的真实 A→B→A 精确回退。
- C4：干净实例限制网络后从包内产物启动。
- C5：启动失败后回退点可启动、锁与任务重试语义。
- C6：真实库中的撤回墓碑、旧外发任务、插件状态、人设开关恢复语义。

这些项在本报告中标记为“未实机验证”，不是“通过”。

## 7. 合并会话复验步骤

1. 在隔离 PostgreSQL 上准备两个数据库，设置两个 `LING_DATABASE_URL`/`LING_TEST_DATABASE_URL`，持测试锁。
2. 用版本 A 启动隔离 Compose 项目，写入会话、记忆、人设、配置、媒体、受管插件样本；运行 `ling backup` 和 `ling restore-drill`。
3. 设置演练脚本要求的源路径与 `ARTIFACT_JSON`，先 dry-run，再 `--apply` 生成完整包并预检。
4. 升级到版本 B，写入额外数据；执行 `restore_full.py execute --apply` 导入 A 包。
5. 核对运行中 `/v1/version`、镜像 digest/架构或发行产物、schema 版本、关键数据、媒体可读和凭据可用。
6. 用同一包恢复到干净项目，限制对外网络，重复核对。
7. 用缺文件、hash 错、版本不可用、启动失败做反例，确认服务与数据从回退点启动。
8. 在合并后新版的插件配置、插件状态和人设开关上再跑一次同包恢复：修改配置/状态/开关 → 生成新包 → 升级/改动 → 导入新包 → 核对配置修订、插件启用状态与当前激活人设恢复。

## 8. 交付状态

功能提交：`29891a152bb7e8669b70854d183a3c4a3d04c0b1`

最终 HEAD：以交付工作树 `git rev-parse HEAD` 为准；本报告回填提交位于功能提交之后。

提交信息：`feat(backup): 增加整包备份与导入恢复流程`

提交后 `git status --short` 为空，`git branch --show-current` 为 `lingxi/backup-restore`，`git rev-parse HEAD` 等于上面的最终提交。

关键日志绝对路径：

- `/tmp/ling-backup-restore-core-baseline.log`
- `/tmp/ling-bsr-mutation-version.log`
- `/tmp/ling-bsr-mutation-tombstone.log`
- `/tmp/ling-backup-restore-final-python.log`
- `/tmp/ling-backup-restore-final-tsc.log`
- `/tmp/ling-backup-restore-final-web.log`

当前已知限制：

- Docker 与 pg_dump 相关实机演练未执行，见 6.5。
- Web 请求执行器是文件队列语义，实际 systemd/cron 挂载由部署会话配置。
- 插件状态和人设状态文件采用本功能自己的包内路径，合并会话需按实际部署的 data 目录映射确认源路径。
