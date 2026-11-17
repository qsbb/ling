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

- 首轮验收代码快照完整 SHA：`62e41c96e96754cfe4a6057ec3c732008b9ce64d`；分支：`lingxi/release-0.0.8`。该快照在当时的 `origin/main` 之上 47 个提交，`git merge-base --is-ancestor HEAD origin/main` 返回 1。
- 开始验收时，工作树仅有五份文档已修改、此记录未跟踪；源代码、测试和部署实现没有未提交差异。后续只修改文档，测试期间冻结。源文件 SHA-256：`clients/web-console/src/connector-instance-acceptance.browser.test.tsx` = `0ed05d809bcf044adbfee3a7d19ab6310fc9a4e723b00c1da2ba0b1dd2b4c502`；`clients/web-console/src/components/ConnectorsPage.tsx` = `72f5848d240b8dead0d55938ef2abd952d189f74ab31f3f394e0fda21c8b9e6d`；`deploy/install.sh` = `bdafce6f36a9f7485c72f266f34a3f9afcf1d381eca1c400b6b8f19f055cd661`；`deploy/upgrade.sh` = `2567627ee95f92d18ed13a193d01e471fbe80a2086e262751dc10c4e6ee53623`；`deploy/compose.yaml` = `b8ad9834592dbec57eea272b7c73d1a7eb955fea64d2e41db05de9949b25fb10`。
- 后续复验快照（2026-11-17）：测试开始时的基线提交为 `68c25dc08bb389a20f30745b3e2536b341555676`，其上的五份源文件差异按文件 SHA 绑定；这些内容随后分别提交为 Web `1edc369ab874f589bf6a6d949948cbba058832f8` 与 CLI `981a815c67b689b1bb863a3a858ac5358e99ee6d`，并推送到 `lingxi/release-0.0.8`。当前 [PR #1](https://github.com/qsbb/ling/pull/1) 包含这两个实现提交和本验收记录；本记录提交后，以 PR head 作为本地候选 SHA。测试时五份源文件 SHA-256：`clients/web-console/src/components/ConnectorInstances.tsx` `209c860e947402a9e1f8481f8a663e74b8a6805f95316a84bc812240a2e3c3a5`；`clients/web-console/src/components/ConnectorsPage.tsx` `92f83aa93029a0132f25b7c3a1bceba050b8dd1ad7ce5f54f747298662ea91ec`；`clients/web-console/src/connector-instances.browser.test.tsx` `a5179d63d5d60051e6fe63fecfde79056b7c4430624fb17ecf701bc13c9ee975`；`deploy/cli/ling.py` `0e19469b63015ac4bb6ff83a98f9bed5814eaf560ffcf9602c798a49d3926f2f`；`tests/test_cli_install.py` `5458e249b57f02444f8bbd93391c1832942ce4af59906653413ff21f3587e243`。本地 `origin/main=0d36dbadc4c8f688df2775e81f1e5e795006a271`；实现提交时 `origin/main..HEAD` 为 56，候选不是 `origin/main` 祖先。
- GitHub API 于 2026-11-17 只读核验：`main=0d36dbadc4c8f688df2775e81f1e5e795006a271`；最新正式 Release 为 `v0.0.8`，`published_at=2026-11-13T16:48:41Z`（上海时间 2026-11-14 00:48:41），tag 解引用到同一 main SHA。最初快照 `62e41c9` 的 `v0.0.9` tag 查询为 404、对应 Actions run 数为 0。`v0.0.8` Release workflow run `43458504082` 的 verify、Python 3.12/3.13 CI、mobile、web、image、updater-image 和 release jobs 均成功；这是旧 Release 的结果，不是候选结果。
- 实现提交 `981a815c67b689b1bb863a3a858ac5358e99ee6d` 的 PR CI run `43977747265` 三项均通过。文档修订前最终候选 head `8fc335b8b6723b72f49d3f160ddf2bc7792a4bff` 的 PR CI run `43983345542` 三项也均通过。
- 文档修订前最终候选 head 为 `8fc335b8b6723b72f49d3f160ddf2bc7792a4bff`。其 PR CI run `43983345542` 的 Python 3.12、Python 3.13、Web 全部成功；持锁本地全量 pytest 退出码 0、进度 100%，日志 `/tmp/ling-pytest-final-0.0.9-8fc335b.log`，该 `-q` 运行无最终数量汇总。Ruff、版本检查、release catalog、敏感信息/U+FFFD 扫描及 `git diff --check` 均通过。
- 该 head 的 SHA-only `workflow_dispatch` run `43989637070` 全部成功：verify、mobile、Python 3.12/3.13、Web、API image、updater-image；release job 按设计 skipped。标签只含 `sha-8fc335b8b6723b72f49d3f160ddf2bc7792a4bff`，没有创建稳定 tag/Release。
- 两个 SHA 镜像的 index 与平台 manifest digest、OCI revision/version/source 及验证结果见 A5；该构建绑定 `8fc335b`。本记录的 docs-only 提交会产生新 head，之后仍需为新 head 再跑 SHA-only workflow。
- GHCR 只读核验：`ling-api` 与 `ling-updater` 稳定 tag 清单均为 `v0.0.2`、`v0.0.3`、`v0.0.4`、`v0.0.5`、`v0.0.6`、`v0.0.8`，没有 `v0.0.9`。在 `v0.0.8` registry index 下直接读取 manifest，并以响应内容 SHA-256 复核平台 descriptor；OCI labels 的 version/revision/source 均为 `0.0.8` / `0d36dbadc4c8f688df2775e81f1e5e795006a271` / `https://github.com/qsbb/ling`：

- 2026-11-17 全量 pytest 结束后、补写本段前的工作树快照：`HEAD=8fc335b8b6723b72f49d3f160ddf2bc7792a4bff`、分支 `lingxi/release-0.0.8`；`git status --short` 仅列 `README.md`、`CHANGELOG.md`、`docs/DEPLOYMENT.md`、`docs/UPDATES.md`、本记录五份文档。该时点 SHA-256：README `42c8bb19082f716e86db44eec145031d45d9f6363e837dfcb1e40baa3e434c9c`；CHANGELOG `f50c00fba8aa36c2a6b65e6e49cc8ac1eea52498fe1352e9f0d894a352300c22`；DEPLOYMENT `99ac189547ae653fd2dd3dbaa1df989c3066032a81f0969d55439ed17838fae9`；UPDATES `cb2e95a54556ed88fd7cbc2260c8a4956a00ff6d2295b671383645619d8ed167`；本记录 `2b69f0c7894f67397c2f1e7286d379621482e71166845f36ca9e9ed61bc57061`。这些是测试完成后的观察值；本次运行开始前没有单独保存这五份文档的 SHA 清单，因此不将它们冒记为运行前哈希。代码文件复验后 SHA：`ConnectorsPage.tsx=92f83aa93029a0132f25b7c3a1bceba050b8dd1ad7ce5f54f747298662ea91ec`、`deploy/cli/ling.py=0e19469b63015ac4bb6ff83a98f9bed5814eaf560ffcf9602c798a49d3926f2f`。

  | 镜像 | index digest | linux/amd64 | linux/arm64 |
  |---|---|---|---|
  | `ghcr.io/qsbb/ling-api:v0.0.8` | `sha256:77a20bb9cfde183e64eb1349b773666fc673c6f4a945abe9d889451c026207f1` | `sha256:3a27a6057756525808aa552dffb4c5dde43a0bb61da37d5512795f7a9d5a5216` | `sha256:406b1b9cc8696adc557d106a272d798fedf706617fa8518acbd5179d8c3bf834` |
  | `ghcr.io/qsbb/ling-updater:v0.0.8` | `sha256:281736db4dedcb29643b01e1be52638c6a019ce7b282bb0d83895a067281fd0f` | `sha256:9dad77f6253bc55dfe959ff8bb211973e5513337ab6a57a040de59ba540f93fd` | `sha256:a30d22af9d10d6a9587e030cf7ba30b4aceacf16f9c9b917252ecde5974d9e1b` |

- Python 测试显式设置 `LING_TEST_DATABASE_URL` 与 `LING_DATABASE_URL` 为 `postgresql+asyncpg://ling@127.0.0.1:55432/ling_test`，并持有 `/tmp/ling_test_lock.sh` 提供的原子锁。测试前后只读查询均为数据库 `ling_test`、角色 `ling`、地址 `127.0.0.1:55432`；共享库 `alembic_version` 保持 `0067_connector_instances`。仓库迁移 head 为 `0078_proactive_daily_quota`；集成 fixture 使用临时 schema，迁移验收在测试自建数据库运行，本轮没有升级共享测试库。
- 本机 PATH 没有 `docker`、`podman`、`buildah`。对 Linux 开发机（文档脱敏地址 `192.0.2.21`）的 SSH 用户不能直接访问 Docker socket，`sudo -n docker` 最初因需口令失败；随后取得授权，只对 Compose project label `ling-image-accept-009-cli-20261117` 与其 `-updater` 子项目执行只读 `docker ps` / `inspect`。原应用项目的 API、worker、PostgreSQL 均 healthy，API/worker 固定使用 `v0.0.8` index digest `sha256:77a20bb9cfde183e64eb1349b773666fc673c6f4a945abe9d889451c026207f1`；数据卷为带该项目名前缀的 `app-data`、`db-data`。updater 容器为运行状态。没有改变容器、卷或数据库，也未连接 NAS/生产端。

**A1 判定：通过（候选、远端状态与本地数据库隔离均有快照；不代表发布门禁通过）。**

### A2：功能范围与真实路径

| 范围 | 本轮可核验证据 | 判定边界 |
|---|---|---|
| 连接器实例详情、保存目标与实例切换 | 原 S9 Playwright/Chromium 专项 9/9；把保存目标变异为 `default` 后按预期以 `default` 对 `main` 失败，恢复后通过。后续 generation 修复在 `connector-instances.browser.test.tsx` 加入三条 scoped GET / 连续保存 / 409 刷新场景；省略保存后 `refreshInstanceView` 的变异使同页连续保存用例以 generation `0` 对 `1` 失败，恢复文件 SHA 后定向三条 3/3 通过。本机 Web 全套 39 文件、1042 passed、1 skipped。5.21 隔离执行报告记载候选实现 OCI revision `981a815c67b689b1bb863a3a858ac5358e99ee6d` 的真实 Chromium 两轮保存：`If-Match: 1`、`If-Match: 2` 均 PUT `202`，随后从 PostgreSQL 读回配置；`.env` SHA 未变。 | 本地 `/v1` 组件用例均由 `page.route` mock；mock 测试只证明客户端契约。5.21 两轮真实 API/PG 读回证明该连接器保存路径生效。PUT `202` 是请求受理，持久化结论另由 PG 读回支撑。密钥清空、所有者权限与部署失败回滚仍需各自证据。日志：`/tmp/ling-009-product-smoke-981a815.log`、`/tmp/ling-009-product-smoke-981a815-second.log`。 |
| 关系与窄屏布局 | 5.21 smoke 的真实关系页 subjects API 返回 `200`，手机视口无横向溢出。 | 没有在候选真实 API 上验关系合并/拆分、权限拒绝或大量数据分页；本地 identity browser 用 route mock。 |
| 日程当前活动、原因、手动生成与失败重试 | 5.21 独立作业报告为 `POST 202 → GET 200 → degraded`；第一轮 smoke 的手动生成 `POST 202`。第二轮脚本重试得到 `POST 409`，记录归因为 `life_timeline.py` 预算/模型闸拒绝。 | `degraded` 不是成功生成；409 是独立预算/模型门禁，不是连接器错误。未覆盖全部状态、PG 持久化和失败重试。 |
| 更新进度、历史耗时与下载线路 | `imageUpdate.browser.test.tsx` 在真浏览器中验证界面交互；另有 `v0.0.8` 旧稳定镜像隔离环境的真 Chromium 检查路径，细节见 A6。 | 浏览器组件的 `/v1` 请求由 route mock；旧镜像检查不是候选升级，也没有切换镜像。 |
| 登录、刷新/新标签、对话与留存 | 5.21 smoke 的真实 API 登录进入总览通过。隔离镜像验收前，执行者报告已把测试用户口令改为固定假口令，凭据指纹不在此回显。历史 `0cfc9be` 浏览器使用 deterministic fake provider。 | 新标签与刷新后的登录恢复尚无本候选证据；fake provider 不证明真实模型能力。 |
| 参考插件功能取舍 | 当前 `docs/UPDATES.md` 记录借鉴 AstrBot 控制台更新状态可见性，并明确不是 `git pull`。 | 冻结判据没有给出更广的参考插件功能清单或逐项取舍来源；除该已记录的更新 UI 取舍外，其余范围未能核验。 |

**A2 判定：部分通过，整体未验。** 非默认 OneBot 两轮保存、登录进入总览、关系列表读取及窄屏无横溢已在 5.21 隔离项目实测；关系合并/拆分、登录新标签/刷新、权限与批量数据，以及完整日程/失败路径等冻结范围仍未全部完成真实浏览器/API/PG 验收。

### A3：源码首装与升级

当前 `deploy/install.sh` / `deploy/upgrade.sh` 只完成静态语法检查，未在本轮候选的干净 Docker 环境按 README 源码模式命令首装、升级或做失败恢复。历史隔离部署结果绑定 `0cfc9be`；脚本 hash 相同不足以替代完整候选运行记录。5.21 的 `ling install` 镜像模式首装记录见 A6，不等同于本项源码模式验收。

补充的旧镜像数据恢复证据来自隔离 `_drill` PostgreSQL：备份前写入的 `image-upgrade-009=persist` 在恢复后仍可读取；隔离执行者报告备份与恢复演练各自 0 错误。此证据支持旧镜像隔离环境的数据恢复行为，不是当前候选的源码一行首装/升级验收。初始验收者本机没有 Docker runtime，且当时的 SSH 身份不能直接调用远端 Docker；后续用户在 5.21 隔离项目执行的镜像模式 CLI 验收记在 A6，仍不替代源码模式首装/升级。

**A3 判定：需外部动作。**

### A4：版本、Release 与 CI

- `.venv/bin/python scripts/check_release.py --tag v0.0.9` 退出码 0，三处应用版本（`pyproject.toml`、`src/ling/__init__.py`、Web `package.json`）一致为 `0.0.9`。
- `.venv/bin/python deploy/release/catalog.py validate` 退出码 0；`deploy/update-notes.json` schema 有效并含 4 个版本条目。
- 文档修订前候选 `8fc335b8b6723b72f49d3f160ddf2bc7792a4bff` 的 PR CI 与 SHA-only workflow run `43989637070` 中 verify、CI、mobile、API/updater 镜像作业均通过；release job 按设计 skipped。该 dispatch 只推 SHA 标签，不是正式 Release。候选不是远端 `main` 祖先，稳定 tag 祖先门禁仍不满足；本次文档修订形成的新 head 尚未运行 PR CI 或 SHA-only workflow。当前 `update-notes.json` 的 CHANGELOG URL 指向 `main`；候选合入后才会显示本地 0.0.9 条目。

**A4 判定：失败（未满足 main 祖先门禁；旧 head 的 PR CI 与 SHA-only workflow 通过，新文档 head 待复验）。**

### A5：镜像身份

workflow_dispatch run `43989637070` 为 head `8fc335b8b6723b72f49d3f160ddf2bc7792a4bff` 成功构建并推送 SHA 标签，Release job 按设计 skipped。buildx 返回 digest、CI 从 registry 原始 index bytes 重算一致，并确认 index 平台为 amd64 + arm64；随后 job 拉镜像核对 OCI revision/version/source、API 内 `LING_COMMIT` 与 Web manifest，以及 updater 内容、非 root 与只读根启动。用 GHCR pull token 只读取回 index，再独立计算响应体 SHA-256，header digest 与 workflow digest 相同：

| 镜像 | index digest | linux/amd64 manifest | linux/arm64 manifest |
|---|---|---|---|
| `ghcr.io/qsbb/ling-api:sha-8fc335b8b6723b72f49d3f160ddf2bc7792a4bff` | `sha256:5e9ad176bf647ea688bdb5a4482913319e69a2b70ec7dc8025f28253131116ea` | `sha256:2ef288dd2525e895764981780edae60ab602b77e6b25f19ef872019c34fd635b` | `sha256:8af19ca6e6a06ffc2b85ff60cab4b480190b2cbe9241a78388c24aaa1af05513` |
| `ghcr.io/qsbb/ling-updater:sha-8fc335b8b6723b72f49d3f160ddf2bc7792a4bff` | `sha256:52c368e6d50404486f15b28e87a5307e25e7bd8210d2961cd47e61ec069706ad` | `sha256:cf3f2f6deb27ed4958db21f4040a970500a4726741faeb7cffc98ec758cdda94` | `sha256:1de49fad60dc14af109cf97363aee7fe000f661855a5672974b607a6e361370d` |

Labels checked by successful job: `version=0.0.9`, `revision=8fc335b8b6723b72f49d3f160ddf2bc7792a4bff`, `source=https://github.com/qsbb/ling`. This is a SHA-tagged candidate, not a stable tag, and was not run in the 5.21 environment. This docs-only commit changes the PR head, so these digests remain bound to `8fc335b`; the new head needs its own SHA workflow before its image identity is verified.

**A5 判定：部分通过（head `8fc335b` 的 SHA 镜像身份已验证；新文档 head 待重新构建）。**

### A6：真实 updater 与状态对账

5.21 隔离执行报告：真实 Chromium 登录 `v0.0.8` 旧稳定镜像并打开 `/settings/about` 后识别版本 `0.0.8`；检查更新的 `POST /v1/version/image-update-checks` 返回 `202`（本地记录 `/tmp/ling-009-image-check-confirmed.log`），之后 updater `status.json` 有新 `check_id`、`checked_at=2026-11-16T22:48:59Z`、`state=ok`、`current.version=0.0.8`，digest 与稳定镜像一致。`202` 只表示受理，终态来自 status；该操作没有升级镜像。

同一隔离镜像环境的执行者报告 `ling doctor` 只读检查为 `ok=27, warn=1, fail=0`；唯一 warn 是测试用户未配置备份 cron（预期），GHCR 固定摘要可达，并有两份完整恢复点。运行时曾临时通过 Compose 环境覆盖应用项目名，因此该结果**不能证明安装器原始 `.env` 项目名正确**；这正是本轮 CLI 回归补测的边界。另有上一节记录的 `_drill` PostgreSQL sentinel 恢复证据。上述 doctor 与恢复点摘要来自隔离执行者报告，未在本机重连该环境复查。

候选 CLI `981a815` 在 5.21 隔离项目 `ling-image-accept-009-cli-20261117` 使用固定 `v0.0.8` API/updater digest 运行 `ling install --with-updater --enable-updates`，退出 0；未传项目名覆盖。updater 容器 `LING_COMPOSE_PROJECT` 与自定义应用项目名一致，`release_check.current` 识别 `0.0.8` 与固定 digest。该项目未被覆盖为其他安装，随后作为网页升级基线保留。此项验证 CLI 和旧稳定 updater 启动，不是候选 updater 实际更新验收。

同一 CLI 隔离项目的执行者补充了数据恢复证据：`.env` SHA 短指纹 `71394e4f...ac25a`、`updater/.env` 短指纹 `a8b20780...2c252`；固定假口令对应的哈希指纹单独保存且不在此回显。`ling backup` 与 `ling restore-drill` 均退出 0，隔离 `_drill` PostgreSQL 读回哨兵 `image-cli-upgrade-009=persist`，媒体 SHA 短指纹为 `21ed498f...106cf`。这些摘要来自 5.21 执行者报告，短指纹按交接文本记录；该项目适合作为之后单跳 `v0.0.8 → v0.0.9` 网页升级的基线，但尚未实际执行该升级。

虽然候选 API/updater SHA 镜像已构建并检查，5.21 容器端仍未运行这些候选镜像；本轮只读核对了保留作 `v0.0.8 → v0.0.9` 基线的专用 Compose 项目，未改动其容器、卷或数据库。尚未验证候选 updater 的真实任务进度/耗时、并发/断线、忙闲重启、中断对账、迁移故障、maintenance gate，或真实网页升级前后的 Cookie/配置/PostgreSQL/媒体状态。因此镜像身份门禁通过，不代表候选 updater 更新链路通过。

**A6 判定：部分通过，候选运行链路未验。** 旧稳定镜像检查、候选 CLI 旧镜像首装/备份恢复及候选 SHA 镜像 CI 构建已完成；Docker 只读权限已验证，但为保留单跳升级基线，本轮没有在该项目运行候选镜像，也没有建立数据克隆项目做候选容器验收。

### A7：稳定版网页更新

GHCR 只读查询没有 `v0.0.9` 稳定 tag 或对应 digest。未创建 tag/Release，也未在隔离镜像部署中从 `v0.0.8` 经 GHCR 直连及可达加速线路升级。旧镜像上的检查请求不是版本升级。5.21 CLI 隔离项目已保留作升级基线。若正式流程只产生一个新的稳定版本，本轮最多能验一跳 `v0.0.8 → v0.0.9`；没有第二个可区分的稳定摘要时，不得宣称两跳连续升级已验，也不伪造 tag 或放宽 updater 校验。

**A7 判定：需外部动作。**

### A8：文档与本地测试结果

README、CHANGELOG、DEPLOYMENT、UPDATES 与本记录区分原始代码快照 `62e41c9`、实现提交 `981a815`、文档修订前代码 head `8fc335b` 和历史部署快照 `0cfc9be`。本次文档提交是 docs-only，会产生新的 PR head；之后必须按新 head 重跑 PR CI 与 SHA-only 镜像工作流。`deploy/update-notes.json` 文案与候选变更范围一致，版本号未改动。本记录不自引用文档提交 SHA。

原始快照 `62e41c9` 的验证基线（不能替代后续工作树的复验）：

| 检查 | 结果 / 证据 |
|---|---|
| 全量 Python pytest | 日志 `/tmp/ling-pytest-final-0.0.9-62e41c9-20261117.log` 退出码 0、进度 100%；独立 collect-only 按文件计 323 个文件、6610 个已选用例。原始日志没有最终 summary；6593 passed 是 `6610 - 14 skip - 3 xfail` 的推算，不是原始汇总。 |
| 文档/部署定向回归 | `tests/test_deploy_install.py`、`tests/test_deploy_update_wiring.py`、`tests/test_update_executor.py` 退出码 0、进度 100%；日志 `/tmp/ling-pytest-doc-targets-final-0.0.9-62e41c9-20261117.log`。 |
| Web 真浏览器全套 | 39 个文件通过，1039 passed、1 skipped；`/tmp/ling-web-all-tests-0.0.9-62e41c9.log`。浏览器真，但按各测试定义使用 API route mock。 |
| Web 类型检查与生产构建 | `pnpm build` 退出码 0（含 `tsc --noEmit`）；Vite 有一个非阻断的大 chunk 提示。日志 `/tmp/ling-web-build-0.0.9-62e41c9.log`。 |
| Flutter | `flutter analyze --no-pub` 无问题；3 个测试通过。日志 `/tmp/ling-mobile-0.0.9-62e41c9-20261117.log`。 |
| 静态及发布本地门禁 | Ruff 通过；MyPy 234 个源文件无问题；py_compile、部署脚本 bash -n、版本一致性和 release catalog 校验均退出码 0。日志位于 `/tmp/ling-{ruff,mypy,pycompile,bash-syntax,version-check,release-catalog}-0.0.9-62e41c9-20261117.log`。 |
| 文档完整性 | docs-only 提交前 `git diff --check` 无输出；本轮五份文档无 U+FFFD，凭据/token/真实私网地址/账号模式扫描无命中。 |

实现提交 `981a815c67b689b1bb863a3a858ac5358e99ee6d` 上的追加复验（文档提交不改源文件；最终 PR head 将另跑门禁）：

| 检查 | 结果 / 证据 |
|---|---|
| 自定义 updater 项目名 | `tests/test_cli_install.py` 中 4 条安装用例通过。旧实现先因 `.env` 缺 `LING_APP_COMPOSE_PROJECT` 失败；只补 `.env` 的半成品又因子进程继承 `wrong-host-project` 失败；完整修复后 `.env` 与启动子进程均为 `ling-verify`。日志：`/tmp/ling-updater-project-mutation.log`、`/tmp/ling-updater-project-env-only.log`、`/tmp/ling-updater-project-focused.log`。这些是 CLI/Docker stub 测试，不是真实 Docker 部署。 |
| 连接器后续浏览器回归 | scoped generation 相关用例变异时 1 failed、10 skipped，错误为等待 generation `1` 却仍读到 `0`；恢复后的三条连续保存/409 用例 3 passed、8 skipped。完整 Web 套件 39 文件、1042 passed、1 skipped；日志 `/tmp/ling-scoped-generation-mutation.log`、`/tmp/ling-scoped-generation-restored.log`、`/tmp/ling-web-connector-current.log`。`ConnectorsPage.tsx` 变异前后 SHA 均为 `92f83aa93029a0132f25b7c3a1bceba050b8dd1ad7ce5f54f747298662ea91ec`。 |
| Web 类型检查与构建 | 当前工作树 `pnpm build` 退出码 0，含 `tsc --noEmit`；Vite 仅提示一个超过 500 kB 的 chunk。日志 `/tmp/ling-web-build-current.log`。 |
| 最终候选全量 Python | `8fc335b8b6723b72f49d3f160ddf2bc7792a4bff` 上持 PostgreSQL 测试锁，全量 `pytest -q` 退出码 0、进度 100%；日志 `/tmp/ling-pytest-final-0.0.9-8fc335b.log`。该 `-q` 没有统计汇总行，不报告推算通过数。收集日志 `/tmp/ling-pytest-collect-0.0.9-8fc335b.log`。 |
| 文档复验后的全量 Python | 在同一代码 HEAD `8fc335b8b6723b72f49d3f160ddf2bc7792a4bff` 上再次持锁运行，显式设置两个数据库 URL 指向隔离 `ling_test`；退出码 0、进度 100%，日志 `/tmp/ling-pytest-docs-final-0.0.9-20261117.log`。`-q` 输出 warnings summary 但未给最终用例计数，不推算通过数。运行后工作树仅有上述五份文档差异；源文件 SHA 见 A1。 |
| PR CI | run `43983345542` 的 Python 3.12、Python 3.13、Web 全通过。 |
| 最终候选 SHA 镜像 | workflow_dispatch `43989637070` 全通过，A5 列出 API/updater index 与平台 digest；release job skipped，只有 SHA 标签，无稳定 tag/Release。 |
| 5.21 候选 API smoke | 执行者报告 revision 为实现提交 `981a815`；两轮非默认 OneBot 保存 `If-Match 1/2 → PUT 202 → 刷新后 PG 读回`。真实登录进入总览、关系 subjects API 200、窄屏布局通过。首轮手动日程生成 POST 202；第二轮 POST 409 是预算/模型门禁，不能算连接器失败。日志 `/tmp/ling-009-product-smoke-981a815.log`、`/tmp/ling-009-product-smoke-981a815-second.log`。 |
| 5.21 CLI、备份与恢复 | 自定义隔离项目以 `v0.0.8` 固定 API/updater digest 首装成功，updater 应用项目名与 `release_check.current` 正确；`ling backup` 和 `ling restore-drill` 均退出 0，恢复 `_drill` PG 哨兵可读，媒体/env SHA 记录短指纹。项目保留为未来网页升级基线；未执行升级。 |
| 5.21 Docker 只读核验 | 经授权后按 Compose project label 读取隔离基线容器和卷身份：API/worker/PostgreSQL healthy，API/worker 仍固定在 `v0.0.8` digest，卷名使用专用项目名前缀；updater 容器运行中。未操作容器/卷或数据库，未连接 NAS/生产端。 |

**A8 判定：通过（记录区分原始基线、最终代码候选、旧镜像报告与 mock 边界；不代表发布门禁通过）。**

### 最终判定

| ID | 结论 |
|---|---|
| A1 | 通过（快照和本机隔离范围） |
| A2 | 部分通过，整体未验（连接器两轮保存、登录进入总览、关系列表读取和窄屏通过；关系合并/拆分、新标签刷新和完整日程未验） |
| A3 | 需外部动作（源码模式首装/升级未在候选上实跑） |
| A4 | 失败（未满足 main 祖先门禁；旧 head 的 PR CI 与 SHA-only workflow 通过，新文档 head 待复验） |
| A5 | 部分通过（`8fc335b` SHA 镜像 index、平台摘要与 OCI 身份已验证；新文档 head 待重建） |
| A6 | 部分通过（已授权只读核验旧稳定基线；候选容器/updater 和真实网页更新未验） |
| A7 | 需外部动作（无稳定 tag，未跑真实网页升级） |
| A8 | 通过（候选/历史范围已区分，No-Go 明确） |

**整体 Go/No-Go：No-Go。** 候选不在 `main` 祖先链；候选容器/updater 的真实运行链路及稳定版网页升级仍未验，且当前没有 `v0.0.9` 稳定 tag 可供单跳升级。

**剩余门禁：**文档修订前候选 `8fc335b` 的 PR CI 与 SHA-only API/updater 镜像构建全部通过；本地文档提交推送后，新 PR head 仍需重跑 PR CI 与镜像工作流。源码模式首装/升级、关系合并/拆分、登录新标签/刷新、候选镜像容器运行、稳定版单跳网页升级及直连/加速/断线/中断对账未完成。本轮没有合并 PR、创建稳定 tag/Release 或访问 NAS 生产端。
