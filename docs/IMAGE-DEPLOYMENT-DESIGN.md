# Ling 镜像发布与测试实例迁移设计

状态：设计方案，尚未实现；**不授权自动部署或删除现有测试数据**。当前安装是测试实例，不按生产零停机承诺设计。首次实施前须再次核对宿主现状、目标镜像摘要和备份可恢复性。

> **实施变更（P1–P6）**：状态行里的"尚未实现"已过期。镜像模式 Compose、`ling install`、
> `deploy/release/publish.py`、独立更新器 `deploy/updater/` 与「API 写请求 / 读状态」通道都已
> 落地并有测试（`scripts/check_compose.py`、`tests/test_release_publish.py`、
> `tests/test_cli_install.py`）。状态行里"当前安装是测试实例（源码模式）"的前提也已不成立：
> 生产 `ling` 自 2026-11-05 起跑的是镜像模式（迁移过程见下）。
>
> **演练进度要按位置分开说**：在**隔离项目 `ling-drill`**（端口 6521、独立卷、独立根密钥）
> 上真机跑通了首次安装、从真实备份恢复、三次连续无人值守升级 A→B→C→D、维护门禁与
> "已迁移失败不回滚"。**生产 `ling` 已于 2026-11-05 从源码模式迁移到镜像模式**：停机后把
> 老源码目录改名 `/vol1/@appdata/ling-srcmode` 保留，用 `ling install` 按项目名 `ling`、
> 端口 6520 重装（卷仍是 `ling_app-data` / `ling_db-data`）。**那次安装失败过**——新卷的
> postgres 两段式启动让 compose 的 `pg_isready` 在临时服务器阶段就报 healthy，安装器查
> `alembic_version` 撞进"数据库正在关闭"的窗口，停在 `failed_after_migration`；随后手工跑完
> 迁移、起 api/worker、补 OneBot 配置与 WS override，再用 `ling adopt --force` 把记录改写成
> 实况（旧记录先备份并核 sha256）。该竞态后来已修（`0e9ad2a`：有界重试 + 真实 `select 1`
> 就绪探针 + 阶段标签改为 `failed_db_ready`），修复后用全新空卷真机重装验证通过。
> 所以这**不是一次干净的成功迁移**：安装记录是 `adopt` 写的，14 项标 `unknown`，
> `ling doctor` = ok=29 warn=14 fail=0，那些 warn 是设计如此。
> **仍未完成**（演练意义）：两次连续网页升级（只发布过一个摘要 `42bba216`，缺第二个 digest）、
> 故障注入里的 head 不一致 / 并发请求 / 旧执行器残留（其余 14 类已做）、中断中任务的对账
> （仅单测）、生产库有真实数据时的恢复（生产库 `messages=0`/`memories=0`）、arm64 运行、
> 浏览器端到端点一遍。
> `LING_UPDATE_ENABLED` 在镜像主文件里仍写死 `"0"`，发布目录
> `deploy/releases.json` 仍是空骨架（真实部署会 `no_available_target`）。详见
> `docs/UPDATES.md` §10 与 `shots/deploy-research/P5-DRILL.md`。
>
> **实施变更（缺陷 6）**：自动探活**不再**打 `/v1/version`（该端点挂 `CurrentAuth`，未鉴权
> 恒 401），改用容器内进程证据（`compose exec python -c` 读 `ling.__version__` 与
> `LING_COMMIT`）；没有新增免鉴权版本端点（不向匿名方公开部署指纹）。HTTP 探活只剩
> `/readyz`。`docs/DEPLOYMENT.md` §11 与 `docs/UPDATES.md` §10 的对应描述已同步。

## 1. 决策与边界

采用「CI 构建并推送不可变镜像 -> 宿主按摘要拉取 -> Compose 切换」作为日常发布路径。宿主不再同步源码、运行 `pnpm` / `pip` 或 `docker compose build`。`api` 与 `worker` 使用**同一镜像摘要**，通过已有 `entrypoint.sh` 的 `api` / `worker` 参数区分角色；Web 静态文件随 API 镜像交付。保留 PostgreSQL、`app-data`、`db-data`、单实例语义及现有非 root/只读根文件系统/healthcheck 边界；宿主端口以实施时实测拓扑为准，不凭旧文档推断。

这是一种适合当前单机测试实例的选择，不是 Docker 强制要求的唯一部署方式。构建与运行分离可减少宿主工具链漂移，摘要使「部署了哪组字节」可核验；它不消除数据库迁移、配置、备份和回退风险。现有源码构建安装器暂时保留供旧安装路径使用，**不能**把它当作镜像模式升级命令。

范围内：镜像构建与身份、CI 门禁、Compose 镜像模式、人工部署脚本/操作手册、测试机迁移、验证及故障处置。范围外：这次直接修改服务器、立刻启用 Web 一键更新、自动推广到生产、多副本零停机、数据库大版本升级。`v0.0.1` 已发布，不重用、不移动其 tag；镜像流程从后续新版本或明确标识的测试构建开始。

## 2. 现状与需要跨越的契约

| 现状 | 镜像模式要求 |
| --- | --- |
| `deploy/compose.yaml` 中 api/worker 各有 `build:`，安装器在宿主执行 `dc build` | 新增独立镜像模式 Compose（建议 `deploy/compose.image.yaml`），api/worker 只使用同一个必填 `LING_APP_IMAGE`；无 `build:`、不默默回退默认镜像 |
| API 把宿主 `clients/web-console/dist` 只读挂到 `/app/web` | 在 CI 构建 Web 后复制入镜像；移除 `/app/web` bind。`LING_WEB_DIST=/app/web` 继续生效 |
| `deploy/install.sh` 负责构建、前端 manifest 校验、锁、就绪等待 | 保留旧路径；为镜像部署建立独立、短小的宿主发布流程，复用必要的互斥/就绪判据，不复用依赖前端 bind 的安装校验 |
| API entrypoint 默认在启动时运行 Alembic 迁移，worker 只等数据库 | 镜像模式将 API 默认设为 `LING_RUN_MIGRATIONS=0`，单次迁移用 `run -e LING_RUN_MIGRATIONS=1` 显式覆盖，再核对 Alembic head；失败不启动新 API。测试机允许维护窗口停机 |
| `/v1/version` 报 version、构建提交、Web 清单哈希 | 保持三项可观测；构建提交必须为实际源提交的完整 SHA，清单与镜像内文件须一致 |
| `deploy/update/executor.py` 下载 tag 源码，校验 release dist、构建并调用旧安装器 | **不兼容新拓扑**；迁移前关闭 `LING_UPDATE_ENABLED` 并停用对应宿主执行器。只读“检查 Release”可保留，不宣称“可一键更新” |

参考实现：[`compose.yaml`](../deploy/compose.yaml)、[`Dockerfile.api`](../deploy/Dockerfile.api)、[`install.sh`](../deploy/install.sh)、[`executor.py`](../deploy/update/executor.py)、[`version.py`](../src/ling/api/routes/version.py)、[`release.yml`](../.github/workflows/release.yml)。这些文件目前都未按本方案修改。

## 3. 目标发布物

1. 镜像名暂定 `ghcr.io/qsbb/ling-api`。CI 由受信任的后续正式 tag 触发，在 `release.yml` 已有版本一致性检查、Python/Web CI 与移动端检查通过后构建；先确认仓库写 package 权限和镜像可见性。单独推送 job 的 `permissions` 恰为 `contents: read`、`packages: write`（若选择 GitHub 原生证明，另单独授予该 job 所需的最小 `id-token`/`attestations` 权限），创建 Release 的 job 单独持有 `contents: write` 且不持有 package 推送权限。现有 workflow 的 `@v4`/`@v5` 等 action 主版本引用应固定到审核过的完整 commit SHA；隔离验证/构建与发布凭据，带写权限的步骤不执行不可信安装脚本。来自 fork/PR 的代码不得持有推送镜像的 token。
2. Web 使用 Node 22、pnpm 9 和锁文件冻结安装并打包；Python 运行层沿用 3.13 slim 与 UID/GID 10001。多阶段 Dockerfile 的**最终运行层**仅 COPY 运行所需产物，绝不把 `.env`、registry token、Docker socket、宿主 `.run` 放入 build context。现有 `.dockerignore` 排除了 `**/dist`，所以直接在宿主构建再 `COPY dist` **行不通**；新增 Docker Web 构建阶段或用经过校验的独立构建上下文。现有 `.ling-web-manifest.json` 的 schema 1 生成器内嵌于 `install.sh`，其 `release_inputs` 除 Web 的 package/锁文件/构建配置外还读取仓库根的 `pyproject.toml`、`README.md`、`alembic.ini`、`deploy/Dockerfile.api`、`deploy/entrypoint.sh` 及 `src`、`connectors`、`migrations` 全树。实现时将生成器抽成双方共用的脚本并保留字段和排序/哈希语义，或在镜像构建阶段提供全部输入；若改名/改语义，应同步修改旧安装器、执行器和测试并明确兼容策略。镜像内的 manifest 必须实际生成、校验并随 Web 文件交付，不以文件存在代替内容验证。
3. OCI 标注版本、完整源 SHA 与仓库来源；`LING_COMMIT` build arg 在镜像构建时注入，`/v1/version.commit` 与 OCI 标注应一致。CI 构建 `linux/amd64` 与 `linux/arm64`（先查实际宿主架构）；多架构 tag 的摘要是 manifest-list/index 摘要，宿主拉取后还要检查平台对应 image ID 与运行容器的镜像标识。禁止仅用 `latest` 作为运行依据。
4. 可额外发布方便检索的 `vX.Y.Z` 和 `sha-<full-commit>` tag，但宿主配置只写 `ghcr.io/qsbb/ling-api@sha256:<index-digest>`；CI 产出可审计的 tag -> digest -> source SHA 记录，Release 可以附上摘要和升级注意事项，但可编辑的 Release 正文**不是**摘要可信来源；发布前要求在 CI 授权步骤写入摘要后锁定 Release 内容、启用并核验 Release 不可变设置及受保护 tag/来源 run，或生成并由宿主核验绑定 OCI 摘要与源提交的 GitHub 构建证明（如 `gh attestation verify oci://...`），核验签名身份、仓库和期望的源提交。没有经过实测的不可变绑定则不发布可部署声明。镜像必须由**同一次**通过门禁的构建产生，不要各自为 API/worker 构建一份相同名字的镜像。若发布镜像失败，不能把 Release 宣称为“可镜像部署”：发布顺序及失败状态应在改造 `release.yml` 时做成明确门禁，避免现有 `gh release create` 先成功、镜像却缺席。
5. 依赖基镜像先固定 Python 3.13 和 PostgreSQL 16 系列；逐步引入按平台可复现的基镜像摘要与供应链扫描。**不在首次应用迁移时顺带升级 PostgreSQL 镜像/主版本**，否则把数据库风险与应用切换耦合。

GHCR 若为私有，宿主使用只有该 package `read:packages` 的凭据，独立受限 Docker 配置目录，登录时避免凭据出现在命令历史/日志；若为公开镜像，按匿名拉取验证。发布 token 不分发给宿主。记录摘要、来源和构建证明；digest pin 证明取到指定字节，不能单独证明代码安全。

## 4. 镜像模式的运行配置

新增独立 Compose 文件，不就地覆盖旧 `compose.yaml`，让既有安装器和备份作业在过渡期有明确归属。顶层 `name: ling`，宿主调用仍显式 `--project-name ling`；保留服务名 `db` / `api` / `worker`、命名卷键 `db-data` / `app-data`、仍适用的网络、持久化挂载、资源与健康检查，端口取实施时经核验的目标值。api/worker 的 `image:` 都指向必填 `LING_APP_IMAGE`；**不**加 `build:`、`/app/web` bind 或源码 bind。配置与敏感信息仍由宿主受限 `.env` / secret 注入，不写入镜像。不要求宿主保留全量源码；独立部署包至少包括新 Compose、发布脚本、`backup.sh`、`restore_drill.sh`、定时任务安装/配置及 WS override（启用时）。文件迁出原目录时校验脚本可执行、相对路径和自动化依赖（当前 `install-cron.sh` 的默认项目名会读取旧 `install.sh`）；明确指定 `LING_COMPOSE_PROJECT=ling` 并改造或提供该依赖，不依赖删掉的源码树。历史记录的部署路径为 `/vol1/1000/ling`、备份路径为 `/vol1/1000/ling-backups`，较新记录还出现不同源码工作树；现行路径和物理卷必须重新现场确认。

旧主文件 `compose.yaml` 与新主文件 `compose.image.yaml` 必须**择一**，禁止叠加，否则旧 `build`/前端挂载可能仍生效；镜像模式的可选 WS 端口 override 则可以叠加在**新**主文件上。反向 WS 启用时，提供仅给 `api.ports` 添加 `${LING_ONEBOT_WS_PORT:?}:${LING_ONEBOT_WS_PORT:?}` 的镜像模式 override（参考 `compose.ws.example.yaml`），核对 `LING_ONEBOT_WS_HOST=0.0.0.0`、token、宿主端口占用和 OneBot URL；从 `config`、`pull`、`run`、`up` 到备份、恢复演练都必须使用同一组 `-f` 文件和项目名。现有 `backup.sh`/`restore_drill.sh` 仅支持一个 `LING_COMPOSE_FILE`，实现时须扩展为受控的主文件加 override 参数，避免备份或演练命中错误的拓扑。上线前对 `docker compose ... config --format json` 做结构化断言：服务集、项目名、两个卷的实际名称、镜像引用、无 `build`、无 `/app/web` bind、无 Docker socket、发布端口、update bind 与 env 及安全设置都符合预期；不要把包含口令的完整 config 打到 CI 或终端日志。Compose 对缺失的 `LING_APP_IMAGE` 使用必填插值使 `config` 失败；发布脚本再校验其格式为完整 `ghcr.io/qsbb/ling-api@sha256:<64 位十六进制>`，拒绝 `latest`、单独的可变 tag、空值和非预期 registry，不能让 Compose 默默启动其他镜像。

> **实施变更（P1）**：本段"`backup.sh`/`restore_drill.sh` 仅支持一个 `LING_COMPOSE_FILE`、
> 实现时须扩展"已过期。两个脚本现在都接受 `LING_COMPOSE_OVERRIDE`（主文件在前、override 在后）
> 与 `LING_COMPOSE_ENV_FILE`，`install-cron.sh` 会把同一组文件组和 `--env-file` 写进 cron 行；
> 调用顺序与逐字节回归由 `tests/test_deploy_backup_archive.py`、`tests/test_deploy_backup_cron.py`
> 钉住。

初期建议**不挂载** `requests/status` 更新通道目录，API `LING_UPDATE_ENABLED=0`；worker 保持没有更新权限。如现有测试机更新执行器已经运行，应先确认并停止它、备份其状态与配置，再迁移，避免旧执行器尝试在新拓扑上重装旧模式。后续若要恢复 Web 发起更新，需设计按 digest 拉取、版本/tag 与摘要绑定、宿主锁、备份、状态机与逐容器校验的新执行器；不能仅把原执行器的 `install.sh` 调用替换成 `compose pull`。`/v1/version/check-release` 只是读 Release 元数据，不等同镜像已发布/该机器可更新，界面文案和 eligibility 判据需同步。

> **实施变更（P5）**：初版曾计划"初期不挂载 requests/status"。实现时按 §4 的
> 「API 仅获得写请求/读状态的最小目录权限」接上了：api 挂 requests(rw) + status(ro)，
> worker 不挂；`LING_UPDATE_ENABLED` 仍默认 0，挂载 ≠ 打开开关。
> 详见 `scripts/check_compose.py` 的断言与 `docs/UPDATES.md` §10。

备份任务仍需明确同一项目名和**新的** `LING_COMPOSE_FILE`；`backup.sh` 通过 `db` 和 `api` 服务执行。当前 `backup.sh`、`restore_drill.sh` 的 Compose 调用都不传 `--env-file`，而 cron 安装器生成的任务也不注入 `LING_APP_IMAGE`；因此优先改造脚本/cron 使它们明确加载同一受限 env、同一 Compose 文件组并实跑验证；若暂不改脚本，只能把含 `LING_APP_IMAGE` 的受限 `.env` 放在 Compose 自动发现的**项目目录**（须在目标 Compose 版本及 cron 的 `cd` 环境下通过 `config -q` 实测，不能仅凭文件同目录推断）。维护窗口暂停原 cron，变更文件路径后按明确项目名重装/调整任务，检查 `crontab -l`、手动模拟 cron 环境成功备份和下一个定时产物；旧 `install-cron.sh` 把工作目录、脚本及 Compose 路径固定进 crontab，且缺失旧 `install.sh` 时必须显式指定 `LING_COMPOSE_PROJECT`。部署文件搬出仓库时，把相对 bind 转成核实过的绝对宿主路径，避免空挂载。原有 `deploy/.env` 不被自动重写；新增固定摘要 `LING_APP_IMAGE`，逐项保留 `POSTGRES_PASSWORD`、**实际生效**的 `LING_ROOT_KEY` 或 `/data/root.key`、`LING_POSTGRES_IMAGE`（现场历史记录使用镜像代理）、端口、Cookie 和渠道/WS 密钥等有效配置；清理旧模式的 `LING_WEB_DIST_HOST`、`LING_PYTHON_BASE`、`PIP_INDEX_URL`、`LING_COMMIT` 及确已废弃的 `LING_EDGE_*`、`LING_NGINX_IMAGE`，不得在未核对目标拓扑时静默改端口。补齐请求目录的权限修复：只有将来重新启用更新通道时，由宿主初始化使 API UID/GID 10001 能写 requests、status 仍仅宿主可写（禁止 `0777`）；镜像模式关闭通道时不需要创建它们。

> **实施变更（P5）**：本段末尾"只有将来重新启用更新通道时，由宿主初始化 requests/status 权限、
> 关闭通道时不需要创建它们"已过期（本段前半句 `--env-file`/cron 的过期见上文 P1 标注）：正常
> 安装流程都会准备 `<dir>/updater/{requests,state}` 和更新器 `.env`（requests 2770 + 共享组、
> state 0750，对应 `deploy/compose.image.yaml` 的 `group_add`），与这次是否拉起更新器无关；
> 拉起仍要显式 `--with-updater`，`LING_UPDATE_ENABLED` 仍写死 `"0"`。

## 5. 人工发布协议（首版）

由受限宿主操作员执行，新脚本应以同一把跨发布锁覆盖**备份 -> 切换 -> 验证**，防止并发升级/备份与人工变更。现有 `install.sh` 的锁只保护旧模式，迁移时要先确认它和旧执行器都不在执行；新模式的锁位置、权限、与旧锁互斥方式需实现并测试，不能仅凭“约定同一时刻别操作”。脚本接受目标 **digest**，绝不代替人选择 `latest`。

1. 预检：核实目标 tag、CI 成功记录及构建证明/不可变发布证据中与源 SHA 绑定的 digest，Release 正文仅供阅读，不作为摘要权威；核对宿主架构和 GHCR 拉取权限。记录当前 Compose/env、运行镜像引用和 image ID、容器/卷 ID、端口、数据库 schema 版本及活动 cron。实际探测是否有 `ling-edge-1`、`ling_edge`、入口 `20443`/`20444`、容器内 `8080` 或直连 `6520`；2026-11-02 的旧安装报告记载 edge 拓扑，较新的外部核验记录已见 `6520` 可访问、`20443` 拒绝连接，均不能代替此刻宿主检查。检查磁盘余量（新旧镜像、全量媒体和备份）及迁移链；限制实际部署版本的最大变更跨度，跨版本先在隔离实例演练。
2. 先用新主文件和已启用的 override、`--project-name ling --env-file <受限env>` 执行 `pull api worker`；核实拉取摘要等于目标且两个服务相同。此步不影响在线服务。已镜像化的版本记录旧 digest；首次从源码版切换前还须核对旧 API/worker 的 image ID，将旧本地镜像分别打日期化保留标签并 `docker save` 离机归档，锁定旧提交、旧 Compose、当时生效的 env、Web dist 及其 bind 路径；旧 `ling-api:latest`/`ling-worker:latest` 是可被后续 build 覆盖的标签，`不 prune` 不等于可回退。
3. 进入维护窗口，暂停原备份 cron，阻断入口写入并排空外部事件和后台任务后停止旧 worker，确认没有其他写者，**旧 API 保持运行**以供备份脚本读取卷。通过现有 `backup.sh`（`LING_BACKUP_VIA_COMPOSE=1`、`LING_COMPOSE_PROJECT=ling`，并指定**当前**可解析的旧 Compose 文件）备份数据库、媒体、墓碑与根密钥；另备份 `.env` 及部署配置。**脚本退出 0 不代表可恢复**：检查 manifest 的 `tombstones_state`、`media_state`、`keys_root`/`keys_env_set`，核对校验和；此前须已在隔离环境恢复演练，最终备份仍需逐项校验。备份放在与数据库不同的故障域，把本次 dump/墓碑/媒体、manifest 与 Compose 配置组成受限恢复快照；将生效 `.env`（可能含 `LING_ROOT_KEY`/数据库口令）和当前 `keys/root.key`（若存在）作为**密钥材料**另在受限位置归档，并以哈希与同一恢复点关联，避免与 dump 同处泄漏。日常备份目录的 `keys/root.key` 是固定文件名，后续备份可能覆盖它，不能将未来的该文件直接配旧 dump。`LING_ROOT_KEY` 若来自 env 则必须随对应恢复点保留该 env，并确认它是实际生效的密钥，不把密钥写进工单或日志。静默/备份无法保证或备份不完整即停止，不宣称获得一致恢复点。
4. 备份完成后停旧 API；如有旧 `edge`，也停止并仅移除已确认的旧 `ling-edge-1` 容器（绝不运行会波及卷的 `down -v`），确认该容器不再占用目标端口；`ling_edge` 网络仅在查明无其他使用方后单独清理。保持 db 容器与命名卷；确认旧模式不会自动重启/改写，且旧 db 健康、新旧 db 配置和实际 image ID 一致。镜像模式以新镜像单次运行 `migrate`（在镜像 Compose 中把 API `LING_RUN_MIGRATIONS` 固定为 `0`，执行 `docker compose ... run --rm --no-deps -e LING_RUN_MIGRATIONS=1 api migrate`，仅连接仍在运行的旧 db；先在隔离实例验证依赖行为）；断言日志出现执行迁移且没有“跳过迁移”，再读取 `alembic_version` 并与镜像自带 Alembic head 比对。`python -m ling.migrate preflight` 仅检查缺表/缺列，不能代替 head 比对。任一门禁失败则保持停机并人工分析，**不自动启动旧 API**。先 `up -d --no-deps --no-build --pull never api` 并核验 API，之后以同样的 `--no-deps` 方式启动 worker；不在本次切换中 `up db` 或替换它。确保 `LING_RUN_MIGRATIONS=0` 使 API 不重复隐式迁移；worker 即使已健康也继续维持入口维护状态，直至第 5 步备份与业务验证全部通过。必须确实拦住直连宿主端口和机器人输入，否则仅停 Web 入口仍可写库，不能声称静默备份或验证期没有流量。单端口发布可能有短时中断，不能把 Compose `deploy.update_config` 的 `start-first` 当作零停机保证。
5. 严格验证三个服务及每个预期副本状态、宿主和容器内 `/readyz` 2xx（不接受 301/302；`/readyz` 只证明基础数据库连接等就绪，**不**证明 Alembic 已到 head）、登录与 Web 静态资源、worker 数据库/outbox 健康、`/v1/version` 的 version/完整 commit/清单摘要、容器实际 image digest 和 `/app/web` 内容；检查更新入口不可写、目标端口和 OneBot/QQbot 连接、必要的告警/备份日志，核对旧 edge 端口预期关闭或经单独反代设计继续服务。随后先在新配置上跑 `config -q` 和受控恢复演练；若在维护窗口内执行完整新备份，须在新 API 仍可供备份读取时再次停下 worker、排空全部写者，按相同一致性门槛校验产物后恢复 worker。重新安装/核对 cron，并确认其最早一次执行可获得镜像变量及正确文件组；门禁通过才恢复定时任务、解除维护状态并记录 digest、新 schema/备份位置，失败进入人工恢复决策。

以上为待实现的操作协议，**不是可直接复制到现有宿主执行的命令清单**；只有新 Compose、锁、校验脚本与演练齐全后才按照实际路径发布。

> **实施变更（P2）**：`deploy/release/publish.py` 已按本节实现宿主发布入口（预检、跨发布锁、
> 归档回退材料、一致备份、拉取、显式迁移、切换与验证），`tests/test_release_publish.py` 覆盖
> 各门禁；本节仍是操作协议与人工作业说明。
> **演练进度（按位置分开说）**：发布/更新链路已在隔离项目 `ling-drill` 真机跑通（新装、
> 备份恢复、三次连续升级）。**生产 `ling` 已于 2026-11-05 迁移到镜像模式**（首装失败 →
> 手工补齐 → `ling adopt --force` 登记，见 §1 的 P1–P6 说明）：宿主现场核对与完整新装
> 已实际发生，本节不再是"待实施"状态；**但演练意义上的几项仍未完成**：两次连续网页升级
> （缺第二个镜像摘要）、head 不一致 / 并发请求 / 旧执行器残留的故障注入、生产库有真实
> 数据时的恢复（生产库仍是零数据）；故障注入同样**未做** head 不一致 / 并发请求 /
> 旧执行器残留这三项。

## 6. 测试实例迁移与恢复

先在隔离 Compose 项目（不同项目名、端口与空白卷）从镜像完成首次安装、迁移、Web/worker 验证；另从测试实例备份恢复到隔离项目演练带数据升级。绝不能对隔离项目使用 `ling` 项目名或与现有 `ling_app-data`、`ling_db-data` 同名的 external 卷。

正式切换测试实例时默认**保留数据**，除非用户另行决定清空：沿用 `ling` 项目名与 `ling_app-data`、`ling_db-data`，用 `docker volume inspect` 与 Compose 解析结果确认挂载前后卷名和物理来源一致；不要执行 `down -v`、`volume rm`、`prune` 或在新项目名下误造空卷。历史现场记录出现 `/vol1/1000/ling` 与 `/vol1/1000/ling-backups`（以及后续不同源码工作树），但**当前实际路径待确认**；实施前核对真实挂载、空间、所有者和备份，不要据目录名推断卷已自动迁移。迁移端口也需按现场拓扑分支：若仍有 `edge` 反代 `api:8080` 并占 `20443`，新 API 容器内固定监听 `6520`，不能保留指向 `8080` 的旧反代；如业务要求继续使用 `20443`，先确认绑定可转给直连 API，设置 `LING_PORT=20443` 并核对防火墙、Cookie/TLS 策略、OneBot 回调与书签，然后按第 4 步移除旧 edge。若现网已直连 `6520`，维持实测端口，不凭旧 `LING_EDGE_PORT` 推断。清理失效的 `LING_EDGE_*`/`LING_NGINX_IMAGE` 仅在备份旧 env 并确认不再使用后进行。若决定放弃测试数据，应另外确认删库范围，先保留可恢复备份，再用**新的隔离项目/卷**做空白重建，不能在保留路径里隐式清理。

回退分两类：**迁移前**（预检或拉取失败）保持旧服务不变；**迁移/重启后**，旧二进制和旧配置可以用于恢复应用，但 schema 可能不再与旧代码兼容，严禁承诺“换回旧 digest 即回滚”。首轮回到源码模式必须使用已归档的旧 API/worker 镜像、与原 bind 路径对应的旧 Web dist、旧主 Compose + 旧 env；如果原本由 edge 提供入口，还要用旧网络/配置恢复 edge 和原端口，不能拿新端口冒充原入口。旧源码 Compose 带 `build:`，回退流程必须只运行归档 image ID 对应的镜像，不允许意外重建覆盖保留标签或让 Docker 自动创建空的 dist bind 目录。先判断新版本是否能修复/前向恢复；确需恢复旧版时维护窗口停写、明确恢复点，以演练过的同一恢复点所对应的 DB dump + `app-data` 媒体/根密钥 + 同一份 env 组合恢复；恢复前导出当前墓碑，与备份墓碑合并并在恢复后重放，防止已撤回数据重新可见；明确接受恢复点后的其他数据损失。执行前人工确认，不做自动 DB downgrade。记录何时重新开放外部端口/机器人上报。

## 7. 实施顺序与验收门槛

| 顺序 | 交付 | 必须通过的验收 |
| --- | --- | --- |
| 1 | 镜像构建流水线和版本发布门禁 | CI 全绿；tag 与三处版本一致；两平台镜像、Web 清单、完整 SHA、GHCR 权限和不可变证明绑定可验证；actions 固定完整 SHA、发布 token 最小权限，失败不宣传为可部署 Release |
| 2 | 独立 Compose 与部署包 | 扩展 `scripts/check_compose.py` 覆盖镜像主文件及 WS override，并在 `deploy/env.example` 中说明 `LING_APP_IMAGE`；结构化 `config` 断言 api/worker 同一摘要、无 `build` 和 `/app/web` bind、实际卷名仍为 `ling_db-data`/`ling_app-data`、端口/网络/只读策略符合现场核验值；密钥不进镜像 |
| 3 | 宿主人工发布、锁、备份及故障路径 | 空白隔离实例部署；备份不完整/registry 不可用/错误 digest/迁移实际跳过或 head 不匹配/健康检查失败均不得误报成功；按首次源码回退所需保留旧镜像、Web dist 与恢复材料 |
| 4 | 带数据隔离演练，再切测试实例 | 从真实备份恢复、核对根密钥和媒体；命名卷不丢，现场核验 edge/直连路径、WS override 与对外端口；重装/验证备份 cron 和恢复演练；Web、API、worker 与业务路径通过，记录实际停机时间 |
| 5 | 收口文档与旧路径 | 更新 `docs/DEPLOYMENT.md`、`docs/UPDATES.md`、`deploy/env.example`、相关测试；纠正 `UPDATES.md` 中“尚无正式 Release”的过期表述；明确旧安装器仅支持源码模式 |
| 6（另议） | 镜像版 Web 更新执行器 | 完成 digest 绑定、宿主权限、状态机、人工门禁、真实 Docker 升级和恢复演练后，才讨论启用 `LING_UPDATE_ENABLED=1` |

测试应覆盖镜像内静态文件/manifest、`/v1/version` 与镜像标注一致性、两架构拉取、Compose 挂载解析、迁移单次执行、备份恢复及失败注入；现有 `tests/test_deploy_install.py`、`tests/test_deploy_update_wiring.py`、`tests/test_update_executor.py` 针对源码模式的断言不能简单删除，新增镜像模式测试并显式划分旧契约。

## 8. 尚待现场确认

- 测试宿主真实 CPU 架构、Docker/Compose 版本、镜像拉取路径和 GHCR package 可见性；网络不可达时是否需要受控离线镜像导入（仍核对同一摘要）。
- 现有测试实例是否已启用宿主更新执行器、systemd/定时任务、旧安装锁及通道目录；若已启用，停用方案和旧请求/状态的保留方式。
- 当前 `ling` 项目的容器、端口、两个卷与宿主目录的真实对应关系，及备份恢复演练是否达到可接受状态。
- 最终发布版本号、维护窗口和业务路径验收人；不在设计阶段预定下一个 semver，也不移动 `v0.0.1`。

参考：[Docker Compose production guidance](https://docs.docker.com/compose/how-tos/production/)、[GitHub Actions publish Docker images](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)、[The Twelve-Factor App: Build, release, run](https://12factor.net/build-release-run)。外部做法提供决策依据；执行细节以本仓库实现和隔离演练为准。
