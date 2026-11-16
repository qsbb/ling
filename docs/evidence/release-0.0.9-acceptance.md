# 0.0.9 验收判据与执行记录

日期：2026-11-17（Asia/Shanghai）

## 判据冻结

以下判据由独立验收者仅根据本次交接任务撰写，完成时间早于本轮对产品实现、部署实现及既有验收证据的检查。验收结果必须绑定最终候选完整提交；来源不明的操作者转述、mock/stub 结果和仅有服务自报的版本均不得当作镜像或发布通过。

状态定义：

- **通过**：指定候选上的可观察条件有可复核证据支持。
- **失败**：观测结果违反判据。
- **未验**：缺证据、只做模拟，或不能绑定候选/目标环境。
- **需外部动作**：需用户授权、供应商凭据、远端操作员或特定网络环境；不计作通过。

| ID | 阶段 | 可证伪的通过条件 | 必要反例/故障注入 | 最低证据绑定 |
|---|---|---|---|---|
| A1 | 状态与隔离 | 冻结 HEAD、工作树、关键文件哈希、远端 main/Release/tag、镜像稳定 tag 和隔离库状态；只操作独立测试项目，不接触生产端。 | 检查目标项目/卷标识不符时立即停止；未授权地址不得建立连接。 | 时间、完整 SHA、status、文件 SHA、远端 ref/API 输出、测试项目/PG 身份；秘密只记哈希或脱敏摘要。 |
| A2 | 需求与真实用户路径 | 连接器实例详情（保存、失败回滚、密钥留空/清空）、关系页（列表、详情、权限、大量数据）、日程（当前活动、生成原因、手动生成、失败重试）、参考插件功能取舍及更新 UI 均逐项分类为交付/延期/外部依赖；真实浏览器+真实 API+隔离 PG 验证承诺的用户操作、权限、持久化和桌面/手机布局。对话、登录、设置和数据留存回归。 | 移除关键后端接线、拒绝权限、返回陈旧数据、强制窄屏或故障重试；对应验收必须失败或显示可执行错误。 | 需求矩阵、候选 SHA、浏览器/视口、可复放操作与 API 状态、脱敏数据/截图；fake/provider/平台替身必须显式标记。 |
| A3 | 源码安装/升级 | 在干净隔离环境按该候选 README 原文执行一行首装和升级；服务健康，并核验配置、PostgreSQL、媒体数据保留及恢复。与镜像模式分开签核。 | 重复升级、可控中断、构建/迁移/部署失败；不得误报成功，必须能核对运行提交与恢复状态。 | README commit/SHA、逐字命令、安装与升级起止提交、脚本 SHA、隔离卷/库身份、前后数据和脱敏日志。 |
| A4 | Release/CI | 版本声明一致；release workflow 的 verify、CI、mobile 门禁在准确候选上通过；发布所需 main 祖先条件满足或有明确、非破坏合入方案。 | 版本错配、必要 job 失败、产物来源提交不符时必须阻断。 | 候选完整 SHA、workflow/event/job 结果、版本对照、main/PR/合并基线。 |
| A5 | 镜像身份 | API/worker 与 updater 的 registry index 和平台摘要、OCI revision/version/source、Web 清单均与候选相符；实际运行容器 ID/image ID 与已验证 registry 摘要对应；核对两种架构。 | 使用同 tag 的错误提交/摘要时，身份检查必须检出并阻断。 | Workflow run、不可变 index/platform digest、OCI labels/attestation、CI summary、容器 inspect 与服务版本映射。image ID 不可替代 registry digest。 |
| A6 | 真实 updater | 隔离 PG 的真实镜像环境验证来源/摘要、阶段进度与耗时、可读日志、重复/并发、浏览器断线重连、updater idle/忙时重启、中断对账、错误摘要、迁移前/后失败、head 不一致、旧执行器残留，以及配置/PG/媒体/Cookie 前后状态。已迁移失败保持人工门禁，不得谎报仍在旧版。 | 按上述每个转换点注入故障，记录 task 状态、是否切换、maintenance gate、恢复动作、版本/数据实际状态。 | API/updater digest 与容器 ID、所选线路和请求、任务 ID/时间线、数据库/配置/媒体/Cookie 比对、脱敏日志。 |
| A7 | 稳定版网页更新 | v0.0.9 稳定 tag 实际可用后，从旧稳定版在隔离 PG 镜像部署中执行真实网页更新；覆盖 GHCR 直连和至少一条实际可达加速线路，核验目标及运行 digest、平台摘要、版本、健康和数据。若正式链路只有一个新 tag，连续两跳需使用有区分力的隔离 registry/发布序列并明确证据边界，不能伪造正式版本或放宽 updater 校验。 | 错误目标、来源错路、重复、断线或迁移错误必须被准确识别并对账。 | 用户授权、稳定 tag/Release、index/platform digest、每跳 task/container/health/data 记录。 |
| A8 | 文档与最终判定 | README、DEPLOYMENT、UPDATES、CHANGELOG、update-notes 和证据文档对同一最终 SHA/版本/模式保持一致；只有候选绑定的实测可标“通过”。发布前门禁与发布后 v0.0.9 GHCR 更新复验分阶段给出 Go/No-Go。 | 任一“已验证”声明找不到对应候选证据即判失败并修正文案。 | 文档 SHA、claim-to-evidence 对照、最终源/镜像身份、退出码、脱敏日志路径。 |

**决策规则**：任何必需项失败或未验均为 No-Go；需外部动作保持打开，不能折算为通过。版本选择与推送/打 tag/正式发布授权彼此独立。

## 执行记录

本节在判据冻结后填写。执行期间被测文件冻结；任何实现改动后，应重跑受影响的判据并更新候选 SHA 与证据。

### A1：快照、远端与隔离

- 代码候选完整 SHA：`62e41c96e96754cfe4a6057ec3c732008b9ce64d`；分支：`lingxi/release-0.0.8`。该代码候选在当时的 `origin/main` 之上 47 个提交，`git merge-base --is-ancestor HEAD origin/main` 返回 1。
- 开始验收时，工作树仅有五份文档已修改、此记录未跟踪；源代码、测试和部署实现没有未提交差异。后续只修改文档，测试期间冻结。源文件 SHA-256：`clients/web-console/src/connector-instance-acceptance.browser.test.tsx` = `0ed05d809bcf044adbfee3a7d19ab6310fc9a4e723b00c1da2ba0b1dd2b4c502`；`clients/web-console/src/components/ConnectorsPage.tsx` = `72f5848d240b8dead0d55938ef2abd952d189f74ab31f3f394e0fda21c8b9e6d`；`deploy/install.sh` = `bdafce6f36a9f7485c72f266f34a3f9afcf1d381eca1c400b6b8f19f055cd661`；`deploy/upgrade.sh` = `2567627ee95f92d18ed13a193d01e471fbe80a2086e262751dc10c4e6ee53623`；`deploy/compose.yaml` = `b8ad9834592dbec57eea272b7c73d1a7eb955fea64d2e41db05de9949b25fb10`。
- 后续复验快照（2026-11-17）：测试开始时的基线提交为 `68c25dc08bb389a20f30745b3e2536b341555676`，其上的五份源文件差异按文件 SHA 绑定；这些内容随后分别提交为 Web `1edc369ab874f589bf6a6d949948cbba058832f8` 与 CLI `981a815c67b689b1bb863a3a858ac5358e99ee6d`，并推送到 `lingxi/release-0.0.8`。当前 [PR #1](https://github.com/qsbb/ling/pull/1) 包含这两个实现提交和本验收记录；本记录提交后，以 PR head 作为本地候选 SHA。测试时五份源文件 SHA-256：`clients/web-console/src/components/ConnectorInstances.tsx` `209c860e947402a9e1f8481f8a663e74b8a6805f95316a84bc812240a2e3c3a5`；`clients/web-console/src/components/ConnectorsPage.tsx` `92f83aa93029a0132f25b7c3a1bceba050b8dd1ad7ce5f54f747298662ea91ec`；`clients/web-console/src/connector-instances.browser.test.tsx` `a5179d63d5d60051e6fe63fecfde79056b7c4430624fb17ecf701bc13c9ee975`；`deploy/cli/ling.py` `0e19469b63015ac4bb6ff83a98f9bed5814eaf560ffcf9602c798a49d3926f2f`；`tests/test_cli_install.py` `5458e249b57f02444f8bbd93391c1832942ce4af59906653413ff21f3587e243`。本地 `origin/main=0d36dbadc4c8f688df2775e81f1e5e795006a271`；实现提交时 `origin/main..HEAD` 为 56，候选不是 `origin/main` 祖先。
- GitHub API 于 2026-11-17 只读核验：`main=0d36dbadc4c8f688df2775e81f1e5e795006a271`；最新正式 Release 为 `v0.0.8`，`published_at=2026-11-13T16:48:41Z`（上海时间 2026-11-14 00:48:41），tag 解引用到同一 main SHA。最初快照 `62e41c9` 的 `v0.0.9` tag 查询为 404、对应 Actions run 数为 0。`v0.0.8` Release workflow run `43458504082` 的 verify、Python 3.12/3.13 CI、mobile、web、image、updater-image 和 release jobs 均成功；这是旧 Release 的结果，不是候选结果。
- 实现提交 `981a815c67b689b1bb863a3a858ac5358e99ee6d` 的 PR CI run 为 `43977747265`（`pull_request`）。初次查询时为 `in_progress`、三项 job pending；后续查询时 Web job passed，Python 3.12/3.13 仍 pending。这不是 Release workflow run；候选仍无稳定 GHCR 镜像。本记录提交后 PR head 会改变，最终 SHA 与对应 CI 必须重新读取。
- GHCR 只读核验：`ling-api` 与 `ling-updater` 稳定 tag 清单均为 `v0.0.2`、`v0.0.3`、`v0.0.4`、`v0.0.5`、`v0.0.6`、`v0.0.8`，没有 `v0.0.9`。在 `v0.0.8` registry index 下直接读取 manifest，并以响应内容 SHA-256 复核平台 descriptor；OCI labels 的 version/revision/source 均为 `0.0.8` / `0d36dbadc4c8f688df2775e81f1e5e795006a271` / `https://github.com/qsbb/ling`：

  | 镜像 | index digest | linux/amd64 | linux/arm64 |
  |---|---|---|---|
  | `ghcr.io/qsbb/ling-api:v0.0.8` | `sha256:77a20bb9cfde183e64eb1349b773666fc673c6f4a945abe9d889451c026207f1` | `sha256:3a27a6057756525808aa552dffb4c5dde43a0bb61da37d5512795f7a9d5a5216` | `sha256:406b1b9cc8696adc557d106a272d798fedf706617fa8518acbd5179d8c3bf834` |
  | `ghcr.io/qsbb/ling-updater:v0.0.8` | `sha256:281736db4dedcb29643b01e1be52638c6a019ce7b282bb0d83895a067281fd0f` | `sha256:9dad77f6253bc55dfe959ff8bb211973e5513337ab6a57a040de59ba540f93fd` | `sha256:a30d22af9d10d6a9587e030cf7ba30b4aceacf16f9c9b917252ecde5974d9e1b` |

- Python 测试显式设置 `LING_TEST_DATABASE_URL` 与 `LING_DATABASE_URL` 为 `postgresql+asyncpg://ling@127.0.0.1:55432/ling_test`，并持有 `/tmp/ling_test_lock.sh` 提供的原子锁。测试前后只读查询均为数据库 `ling_test`、角色 `ling`、地址 `127.0.0.1:55432`；共享库 `alembic_version` 保持 `0067_connector_instances`。仓库迁移 head 为 `0078_proactive_daily_quota`；集成 fixture 使用临时 schema，迁移验收在测试自建数据库运行，本轮没有升级共享测试库。
- 本机 PATH 没有 `docker`、`podman`、`buildah`。对 Linux 开发机（文档脱敏地址 `192.0.2.21`）只做 SSH 权限只读检查：Docker client `29.7.2`、Compose `5.5.0`；SSH 用户不在 docker 组，Docker socket 拒绝访问，`sudo -n docker` 返回“需要密码”。未成功调用 Docker daemon、未查看/改变容器或卷，也未连接 NAS/生产端。

**A1 判定：通过（候选、远端状态与本地数据库隔离均有快照；不代表发布门禁通过）。**

### A2：功能范围与真实路径

| 范围 | 本轮可核验证据 | 判定边界 |
|---|---|---|
| 连接器实例详情、保存目标与实例切换 | 原 S9 Playwright/Chromium 专项 9/9；把保存目标变异为 `default` 后按预期以 `default` 对 `main` 失败，恢复后通过。后续 generation 修复在 `connector-instances.browser.test.tsx` 加入三条 scoped GET / 连续保存 / 409 刷新场景；省略保存后 `refreshInstanceView` 的变异使同页连续保存用例以 generation `0` 对 `1` 失败，恢复文件 SHA 后定向三条 3/3 通过。当前 Web 全套为 39 文件、1042 passed、1 skipped。另据 5.21 隔离执行报告，候选 API OCI revision 精确匹配 `981a815c67b689b1bb863a3a858ac5358e99ee6d`；真实 Chromium 在非默认 OneBot 实例连续保存两次，分别以 `If-Match: 1`、`If-Match: 2` 收到 PUT `202`，每次随后从 PostgreSQL 读回配置。测试前后 `.env` SHA 未变。 | 本地 `/v1` 组件用例均由 `page.route` mock；mock 测试只证明客户端契约。5.21 两轮真实 API/PG 读回证明该连接器保存路径在候选 API 上生效，但不覆盖其他 A2 范围。PUT `202` 是变更请求受理结果，持久化结论另由后续 PG 读回支撑。密钥清空、所有者权限与部署失败回滚仍需各自证据。S9 和 generation 变异范围、文件 SHA、日志见 A1/A8 及 5.21 运行记录 `/tmp/ling-009-product-smoke-981a815.log`、`/tmp/ling-009-product-smoke-981a815-second.log`。 |
| 关系列表、详情、权限与大量数据/布局 | `identity.browser.test.tsx` 使用真 Chromium，造 2000 条关系，检查详情、分页、合并/拆开及 375/1440 视口。 | `/v1/**` 由 `page.route` 模拟；没有与真实 API、权限存储和 PostgreSQL 组成的端到端路径。 |
| 日程当前活动、原因、手动生成与失败重试 | 5.21 隔离执行报告记载一条独立日程作业 `POST 202 → GET 200 → degraded`，按预期终态验收通过。另一轮产品 smoke 的日程重排 `POST 409` 对应 `life_timeline.py` 的预算/模型闸拒绝，与连接器保存无关。 | `degraded` 不代表成功生成；单条作业路径不覆盖全部日程状态、PG 持久化和失败重试。UI mock 与这些隔离运行分开判定。 |
| 更新进度、历史耗时与下载线路 | `imageUpdate.browser.test.tsx` 在真浏览器中验证界面交互；另有 `v0.0.8` 旧稳定镜像隔离环境的真 Chromium 检查路径，细节见 A6。 | 浏览器组件的 `/v1` 请求由 route mock；旧镜像检查不是候选升级，也没有切换镜像。 |
| 登录、对话、设置及数据留存 | 历史 `0cfc9be` 隔离 Chromium 34/34 使用 deterministic fake provider。隔离镜像浏览器验收前，执行者报告已在隔离 PostgreSQL 将测试用户口令改为固定假口令；哈希指纹另行记录，未回显口令或哈希。 | 历史容器、Cookie 和数据留存绑定 `0cfc9be`；fake provider 只验证应用与 UI 流程，不证明真实模型能力。假口令只用于隔离测试，不能替代候选部署验收。 |
| 参考插件功能取舍 | 当前 `docs/UPDATES.md` 记录借鉴 AstrBot 控制台更新状态可见性，并明确不是 `git pull`。 | 冻结判据没有给出更广的参考插件功能清单或逐项取舍来源；除该已记录的更新 UI 取舍外，其余范围未能核验。 |

**A2 判定：部分通过，整体未验。** 非默认 OneBot 两轮保存已在真实 Chromium/API/PostgreSQL 中通过；关系、权限、批量数据、完整日程与失败路径等冻结范围仍没有全部组成候选绑定的真实浏览器/API/PG 验收。

### A3：源码首装与升级

当前 `deploy/install.sh` / `deploy/upgrade.sh` 只完成静态语法检查，未在本轮候选的干净 Docker 环境按 README 源码模式命令首装、升级或做失败恢复。历史隔离部署结果绑定 `0cfc9be`；脚本 hash 相同不足以替代完整候选运行记录。5.21 的 `ling install` 镜像模式首装记录见 A6，不等同于本项源码模式验收。

补充的旧镜像数据恢复证据来自隔离 `_drill` PostgreSQL：备份前写入的 `image-upgrade-009=persist` 在恢复后仍可读取；隔离执行者报告备份与恢复演练各自 0 错误。此证据支持旧镜像隔离环境的数据恢复行为，不是当前候选的源码一行首装/升级验收。初始验收者本机没有 Docker runtime，且当时的 SSH 身份不能直接调用远端 Docker；后续用户在 5.21 隔离项目执行的镜像模式 CLI 验收记在 A6，仍不替代源码模式首装/升级。

**A3 判定：需外部动作。**

### A4：版本、Release 与 CI

- `.venv/bin/python scripts/check_release.py --tag v0.0.9` 退出码 0，三处应用版本（`pyproject.toml`、`src/ling/__init__.py`、Web `package.json`）一致为 `0.0.9`。
- `.venv/bin/python deploy/release/catalog.py validate` 退出码 0；`deploy/update-notes.json` schema 有效并含 4 个版本条目。
- 候选不是远端 `main` 祖先；PR CI run `43977747265` 的 Web job 已通过，Python 3.12/3.13 仍 pending，且没有候选 Release workflow 结果。因此本地版本检查和 PR CI 不能满足 release workflow 的 tag 祖先与发布门禁。当前 `update-notes.json` 的 CHANGELOG URL 指向 `main`；候选合入后才会显示本地 0.0.9 条目。

**A4 判定：失败（候选未满足 main 祖先门禁，也没有候选 workflow 结果）。**

### A5：镜像身份

上表验证了现存 `v0.0.8` 两镜像的 registry index、双平台 manifest 与 OCI labels；GHCR 没有 `v0.0.9` tag。5.21 执行者报告连接器实跑所用 API 容器的 OCI revision 精确匹配候选 SHA，但未提供 registry index/platform digest；CLI 首装使用的是 `v0.0.8` API/updater 固定摘要。当前候选的 GHCR Web 清单、API/updater 双平台摘要与 updater 镜像身份仍未核验。旧快照本地 image ID 和单个 OCI revision 都不能替代 registry digest。

**A5 判定：需外部动作。**

### A6：真实 updater 与状态对账

本轮隔离执行者报告：真实 Chromium 登录 `v0.0.8` 旧稳定镜像并打开 `/settings/about` 后，页面识别当前版本 `0.0.8`；点击检查更新后 `POST /v1/version/image-update-checks` 返回 `202`（本地记录 `/tmp/ling-009-image-check-confirmed.log`），随后 updater `status.json` 出现新 `check_id`、`checked_at=2026-11-16T22:48:59Z`、`state=ok`、`current.version=0.0.8`，digest 与稳定镜像记录相符。`202` 只表示请求已接受；终态来自之后读取的 status 文件，本次只检查版本，没有执行镜像升级。

同一隔离镜像环境的执行者报告 `ling doctor` 只读检查为 `ok=27, warn=1, fail=0`；唯一 warn 是测试用户未配置备份 cron（预期），GHCR 固定摘要可达，并有两份完整恢复点。运行时曾临时通过 Compose 环境覆盖应用项目名，因此该结果**不能证明安装器原始 `.env` 项目名正确**；这正是本轮 CLI 回归补测的边界。另有上一节记录的 `_drill` PostgreSQL sentinel 恢复证据。上述 doctor 与恢复点摘要来自隔离执行者报告，未在本机重连该环境复查。

候选 CLI `981a815` 另在 5.21 新隔离项目 `ling-image-accept-009-cli-20261117` 实跑镜像模式首装：使用固定 `v0.0.8` API/updater digest，执行 `ling install --with-updater --enable-updates` 返回 0；未传项目名环境覆盖。updater 容器内 `LING_COMPOSE_PROJECT` 等于自定义应用项目名，`status.json` 的 `release_check.current` 识别 `version=0.0.8` 与对应固定 digest。旧版基线项目保持完整，供后续网页升级使用。该实跑验证了候选 CLI 的自定义项目名传递和旧稳定 updater 启动；使用的是 `v0.0.8` API/updater 镜像，不证明候选 updater 镜像身份或网页升级。

同一 CLI 隔离项目的执行者补充了数据恢复证据：`.env` SHA 短指纹 `71394e4f...ac25a`、`updater/.env` 短指纹 `a8b20780...2c252`；固定假口令对应的哈希指纹单独保存且不在此回显。`ling backup` 与 `ling restore-drill` 均退出 0，隔离 `_drill` PostgreSQL 读回哨兵 `image-cli-upgrade-009=persist`，媒体 SHA 短指纹为 `21ed498f...106cf`。这些摘要来自 5.21 执行者报告，短指纹按交接文本记录；该项目适合作为之后单跳 `v0.0.8 → v0.0.9` 网页升级的基线，但尚未实际执行该升级。

以上补充了候选 API 的连接器真实路径，以及候选 CLI 配合旧稳定镜像的首装、检查与恢复基线。本轮没有运行当前候选 updater 镜像，也未验证候选 updater 摘要、真实更新任务阶段与耗时、并发/断线、updater 忙闲重启、中断对账、迁移故障、maintenance gate，或一次真实网页升级前后的 Cookie/配置/PostgreSQL/媒体状态；这些实跑不计作候选 updater 更新链路通过。

**A6 判定：需外部动作。**

### A7：稳定版网页更新

GHCR 只读查询没有 `v0.0.9` 稳定 tag 或对应 digest。未创建 tag/Release，也未在隔离镜像部署中从 `v0.0.8` 经 GHCR 直连及可达加速线路升级。旧镜像上的检查请求不是版本升级。5.21 CLI 隔离项目已保留作升级基线。若正式流程只产生一个新的稳定版本，本轮最多能验一跳 `v0.0.8 → v0.0.9`；没有第二个可区分的稳定摘要时，不得宣称两跳连续升级已验，也不伪造 tag 或放宽 updater 校验。

**A7 判定：需外部动作。**

### A8：文档与本地测试结果

README、CHANGELOG、DEPLOYMENT、UPDATES 与本记录现区分原始代码快照 `62e41c9`、实现提交 `981a815c67b689b1bb863a3a858ac5358e99ee6d` 和历史部署快照 `0cfc9be`；本记录与安装说明作为 PR #1 的单独文档提交。`deploy/update-notes.json` 的 0.0.9 文案与候选变更范围一致，版本号未改动。本记录不自引用其文档提交 SHA；最终候选 SHA 以该文档提交后的 PR head 为准。

原始快照 `62e41c9` 的验证基线（不能替代后续工作树的复验）：

| 检查 | 结果 / 证据 |
|---|---|
| 全量 Python pytest | 日志 `/tmp/ling-pytest-final-0.0.9-62e41c9-20261117.log` 退出码 0、进度 100%；独立 collect-only 按文件计 323 个文件、6610 个已选用例。原始日志没有最终 summary；6593 passed 是 `6610 - 14 skip - 3 xfail` 的推算，不是原始汇总。 |
| 文档/部署定向回归 | `tests/test_deploy_install.py`、`tests/test_deploy_update_wiring.py`、`tests/test_update_executor.py` 退出码 0、进度 100%；日志 `/tmp/ling-pytest-doc-targets-final-0.0.9-62e41c9-20261117.log`。 |
| Web 真浏览器全套 | 39 个文件通过，1039 passed、1 skipped；`/tmp/ling-web-all-tests-0.0.9-62e41c9.log`。浏览器真，但按各测试定义使用 API route mock。 |
| Web 类型检查与生产构建 | `pnpm build` 退出码 0（含 `tsc --noEmit`）；Vite 有一个非阻断的大 chunk 提示。日志 `/tmp/ling-web-build-0.0.9-62e41c9.log`。 |
| Flutter | `flutter analyze --no-pub` 无问题；3 个测试通过。日志 `/tmp/ling-mobile-0.0.9-62e41c9-20261117.log`。 |
| 静态及发布本地门禁 | Ruff 通过；MyPy 234 个源文件无问题；py_compile、部署脚本 bash -n、版本一致性和 release catalog 校验均退出码 0。日志位于 `/tmp/ling-{ruff,mypy,pycompile,bash-syntax,version-check,release-catalog}-0.0.9-62e41c9-20261117.log`。 |
| 文档完整性 | `git diff --check` 无输出；`src/`、`tests/`、`docs/` 无 U+FFFD；对本轮涉及的六份文档单独扫描规则指定的内网地址、密钥样式和真实账号模式，均无命中。仓库全量敏感词扫描有历史假密钥样例和 `AGENTS.md` 规则示例命中，均非本轮新增。 |

实现提交 `981a815c67b689b1bb863a3a858ac5358e99ee6d` 上的追加复验（文档提交不改源文件；最终 PR head 将另跑门禁）：

| 检查 | 结果 / 证据 |
|---|---|
| 自定义 updater 项目名 | `tests/test_cli_install.py` 中 4 条安装用例通过。旧实现先因 `.env` 缺 `LING_APP_COMPOSE_PROJECT` 失败；只补 `.env` 的半成品又因子进程继承 `wrong-host-project` 失败；完整修复后 `.env` 与启动子进程均为 `ling-verify`。日志：`/tmp/ling-updater-project-mutation.log`、`/tmp/ling-updater-project-env-only.log`、`/tmp/ling-updater-project-focused.log`。这些是 CLI/Docker stub 测试，不是真实 Docker 部署。 |
| 连接器后续浏览器回归 | scoped generation 相关用例变异时 1 failed、10 skipped，错误为等待 generation `1` 却仍读到 `0`；恢复后的三条连续保存/409 用例 3 passed、8 skipped。完整 Web 套件 39 文件、1042 passed、1 skipped；日志 `/tmp/ling-scoped-generation-mutation.log`、`/tmp/ling-scoped-generation-restored.log`、`/tmp/ling-web-connector-current.log`。`ConnectorsPage.tsx` 变异前后 SHA 均为 `92f83aa93029a0132f25b7c3a1bceba050b8dd1ad7ce5f54f747298662ea91ec`。 |
| Web 类型检查与构建 | 当前工作树 `pnpm build` 退出码 0，含 `tsc --noEmit`；Vite 仅提示一个超过 500 kB 的 chunk。日志 `/tmp/ling-web-build-current.log`。 |
| Python 局部验证 | 自定义项目四条相关安装用例 4 passed；完整 Python suite 未在后续工作树重跑。Ruff、`py_compile`、`git diff --check` 与文档完整性扫描通过。 |
| 5.21 候选 API 连接器实跑 | 执行者报告两轮非默认 OneBot 保存均按 `If-Match 1/2 → PUT 202 → PG 读回` 完成；API OCI revision 与候选 SHA 相同，`.env` SHA 未变。报告日志 `/tmp/ling-009-product-smoke-981a815.log`、`/tmp/ling-009-product-smoke-981a815-second.log`。此环境由执行者运行，本机未连接 5.21。 |
| 5.21 CLI Docker 首装 | 执行者报告候选 CLI 用固定 `v0.0.8` API/updater digest 在自定义隔离项目首装返回 0，updater `LING_COMPOSE_PROJECT` 与 `release_check.current` 均正确；未传项目名覆盖。此项验证 CLI 与旧稳定镜像，不是候选 updater 镜像更新。 |
| 5.21 CLI 备份恢复与数据 | 同一隔离项目内 `ling backup` 和 `ling restore-drill` 均退出 0；恢复后的 `_drill` PostgreSQL 读到 `image-cli-upgrade-009=persist`。`.env`、`updater/.env` 和媒体 SHA 仅记录短指纹；登录哈希为固定假口令且不回显。该项目作为之后单跳稳定版网页升级基线，升级本身尚未执行。 |
| PR CI | run `43977747265`：Web job passed；Python 3.12/3.13 检查时仍 pending。无候选 Release workflow 结果。 |

**A8 判定：通过（记录区分原始基线、最终代码候选、旧镜像报告与 mock 边界；不代表发布门禁通过）。**

### 最终判定

| ID | 结论 |
|---|---|
| A1 | 通过（快照和本机隔离范围） |
| A2 | 部分通过，整体未验（连接器真实两轮保存通过，其余范围未完成） |
| A3 | 需外部动作（源码模式首装/升级未在候选上实跑） |
| A4 | 失败（未合入 main、无候选 CI） |
| A5 | 需外部动作（无候选 GHCR 镜像） |
| A6 | 需外部动作（旧稳定 updater 启动已验；候选 updater 与网页更新链路未验） |
| A7 | 需外部动作（无稳定 tag，未跑真实网页升级） |
| A8 | 通过（候选/历史范围已区分，No-Go 明确） |

**整体 Go/No-Go：No-Go。** 版本字符串正确、旧稳定镜像身份可核验、本地隔离测试通过，都不能替代 main 祖先、当前候选镜像和真实部署更新门禁。

**剩余门禁：**用户已在 5.21 隔离项目验证候选 API 的连接器路径和候选 CLI 配合 `v0.0.8` 镜像的首装；当前候选 PR 的 Python 3.12/3.13 CI 尚 pending。No-Go 仍由候选未合入 `main`、源码模式首装/升级未验、候选 updater 镜像与稳定版网页更新未验导致。候选分支已推送并进入 PR；本轮没有合并 PR、创建稳定 tag/Release 或访问 NAS 生产端。
