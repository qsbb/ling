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
- 文档提交 `fb0d5fb92b5599e3f59b6022a4f4b4d0aabe712a` 后只读检查 PR #1 head 与本地 HEAD 一致；CI run `44025317841` 对该 head 的 Web、Python 3.12、Python 3.13 均成功。本轮接续时工作树干净，`origin/lingxi/release-0.0.8` 与该 SHA 一致；此后文案校准发生在该 CI run 之后，尚未推送，因此不声称这些后续文档字节已在 PR CI 验证。
- 该 head 的 SHA-only `workflow_dispatch` run `43989637070` 全部成功：verify、mobile、Python 3.12/3.13、Web、API image、updater-image；release job 按设计 skipped。标签只含 `sha-8fc335b8b6723b72f49d3f160ddf2bc7792a4bff`，没有创建稳定 tag/Release。
- `8fc335b` 的 SHA 镜像构建证据见 A5 历史记录。随后为实现/运行验收候选 `ab0ddf4` 另跑 PR CI 和 SHA-only workflow；其镜像与运行结果分列于后续条目及当前 A5/A6。
- 实现与安装验收绑定的后续候选 head `ab0ddf41b81245bce40701ea1fdfebdba79bd06e`（本次文档复验前的 PR head；基于同一 Web/CLI 实现再加 docs-only 提交）。PR CI run `44006742093` 最终三项均通过：Web、Python 3.13、Python 3.12。Python 3.12 首次 attempt 有一条 QQBot 投递集成用例失败；同一 job 单独重跑成功，本机隔离 PostgreSQL 对应单例也通过。失败原因未定位，因此保留为并行全量测试的间歇风险，不把首轮失败从记录中抹去。
- SHA-only release workflow run `44015600964` 对 `ab0ddf41b81245bce40701ea1fdfebdba79bd06e` 全部必要门禁成功：verify、Python 3.12/3.13、Web、mobile、API image、updater-image；`release` job 按设计 skipped。没有创建 `v0.0.9` tag 或 Release。API/updater index 与平台摘要、OCI 身份见 A5。
- GHCR 只读核验：`ling-api` 与 `ling-updater` 稳定 tag 清单均为 `v0.0.2`、`v0.0.3`、`v0.0.4`、`v0.0.5`、`v0.0.6`、`v0.0.8`，没有 `v0.0.9`。在 `v0.0.8` registry index 下直接读取 manifest，并以响应内容 SHA-256 复核平台 descriptor；OCI labels 的 version/revision/source 均为 `0.0.8` / `0d36dbadc4c8f688df2775e81f1e5e795006a271` / `https://github.com/qsbb/ling`：

- 2026-11-17 全量 pytest 结束后、补写本段前的工作树快照：`HEAD=8fc335b8b6723b72f49d3f160ddf2bc7792a4bff`、分支 `lingxi/release-0.0.8`；`git status --short` 仅列 `README.md`、`CHANGELOG.md`、`docs/DEPLOYMENT.md`、`docs/UPDATES.md`、本记录五份文档。该时点 SHA-256：README `42c8bb19082f716e86db44eec145031d45d9f6363e837dfcb1e40baa3e434c9c`；CHANGELOG `f50c00fba8aa36c2a6b65e6e49cc8ac1eea52498fe1352e9f0d894a352300c22`；DEPLOYMENT `99ac189547ae653fd2dd3dbaa1df989c3066032a81f0969d55439ed17838fae9`；UPDATES `cb2e95a54556ed88fd7cbc2260c8a4956a00ff6d2295b671383645619d8ed167`；本记录 `2b69f0c7894f67397c2f1e7286d379621482e71166845f36ca9e9ed61bc57061`。这些是测试完成后的观察值；本次运行开始前没有单独保存这五份文档的 SHA 清单，因此不将它们冒记为运行前哈希。代码文件复验后 SHA：`ConnectorsPage.tsx=92f83aa93029a0132f25b7c3a1bceba050b8dd1ad7ce5f54f747298662ea91ec`、`deploy/cli/ling.py=0e19469b63015ac4bb6ff83a98f9bed5814eaf560ffcf9602c798a49d3926f2f`。
- docs-only 更新推送前快照：`HEAD=ab0ddf41b81245bce40701ea1fdfebdba79bd06e`、分支 `lingxi/release-0.0.8`；`git status --short` 仅列 `README.md`、`CHANGELOG.md`、`docs/DEPLOYMENT.md`、`docs/UPDATES.md`、本记录五份文档。该时点 SHA-256：README `eea22e87afe5b07e2df5974648bb1b918212624890d2fb727881603a6ec12399`；CHANGELOG `a2567b624352123dfd5dee940280402af9720e6a309be69632529c79c5c9c11a`；DEPLOYMENT `4b4836d0552283686d131fa1382266a68cd305c468e02a71cc487806339f7e05`；UPDATES `a8eb8d3625d12340348f99d0a7dead9963491f1e854295884c9a440b49c063c6`；本记录（加入本条之前）`01f429d44d894916588d558084b073c20e5beb380f8bb5e466d51db4a48276f9`。代码文件仍为已验 SHA：`ConnectorsPage.tsx=92f83aa93029a0132f25b7c3a1bceba050b8dd1ad7ce5f54f747298662ea91ec`、`deploy/cli/ling.py=0e19469b63015ac4bb6ff83a98f9bed5814eaf560ffcf9602c798a49d3926f2f`。
- 本次 docs-only 提交前快照：`HEAD=ab0ddf41b81245bce40701ea1fdfebdba79bd06e`、分支 `lingxi/release-0.0.8`；`git status --short` 仅列 README、CHANGELOG、DEPLOYMENT、UPDATES 与本记录五份文档。该时点 SHA-256：README `eea22e87afe5b07e2df5974648bb1b918212624890d2fb727881603a6ec12399`；CHANGELOG `a2567b624352123dfd5dee940280402af9720e6a309be69632529c79c5c9c11a`；DEPLOYMENT `4b4836d0552283686d131fa1382266a68cd305c468e02a71cc487806339f7e05`；UPDATES `a8eb8d3625d12340348f99d0a7dead9963491f1e854295884c9a440b49c063c6`；本记录（加入本条之前）`35743d7f1d0b25ce60f0f0e9fb89d54841e392c174c6fadc2b4ca0e604bf9faa`。已验源文件仍相同：`ConnectorsPage.tsx=92f83aa93029a0132f25b7c3a1bceba050b8dd1ad7ce5f54f747298662ea91ec`、`deploy/cli/ling.py=0e19469b63015ac4bb6ff83a98f9bed5814eaf560ffcf9602c798a49d3926f2f`。
- 本轮接续文档复核起点（2026-11-17）：`HEAD=fb0d5fb92b5599e3f59b6022a4f4b4d0aabe712a`、分支 `lingxi/release-0.0.8`，`git status --short` 为空，`origin/lingxi/release-0.0.8` 与 HEAD 相同。起点 SHA-256：README `eea22e87afe5b07e2df5974648bb1b918212624890d2fb727881603a6ec12399`；CHANGELOG `a2567b624352123dfd5dee940280402af9720e6a309be69632529c79c5c9c11a`；DEPLOYMENT `4b4836d0552283686d131fa1382266a68cd305c468e02a71cc487806339f7e05`；UPDATES `a8eb8d3625d12340348f99d0a7dead9963491f1e854295884c9a440b49c063c6`；本记录 `52d92377a82035b86c38a259d73e981bd6598afc3a852e5e6d35d6dcadc0f7a8`。这些值绑定复核前快照，不是后续修改后的哈希。
- 实现/运行验收候选 `ab0ddf41b81245bce40701ea1fdfebdba79bd06e` 是 `8fc335b` 上的 docs-only 提交；相对于已通过运行的源代码，代码、测试和部署文件没有改变。候选实际容器安装前的工作树 `git status --short` 为空；`ConnectorsPage.tsx` SHA-256=`92f83aa93029a0132f25b7c3a1bceba050b8dd1ad7ce5f54f747298662ea91ec`，`deploy/cli/ling.py`=`0e19469b63015ac4bb6ff83a98f9bed5814eaf560ffcf9602c798a49d3926f2f`。docs-only 更新后新 PR head 将变化，须为新 head 重跑门禁和 SHA 镜像身份检查。

  | 镜像 | index digest | linux/amd64 | linux/arm64 |
  |---|---|---|---|
  | `ghcr.io/qsbb/ling-api:v0.0.8` | `sha256:77a20bb9cfde183e64eb1349b773666fc673c6f4a945abe9d889451c026207f1` | `sha256:3a27a6057756525808aa552dffb4c5dde43a0bb61da37d5512795f7a9d5a5216` | `sha256:406b1b9cc8696adc557d106a272d798fedf706617fa8518acbd5179d8c3bf834` |
  | `ghcr.io/qsbb/ling-updater:v0.0.8` | `sha256:281736db4dedcb29643b01e1be52638c6a019ce7b282bb0d83895a067281fd0f` | `sha256:9dad77f6253bc55dfe959ff8bb211973e5513337ab6a57a040de59ba540f93fd` | `sha256:a30d22af9d10d6a9587e030cf7ba30b4aceacf16f9c9b917252ecde5974d9e1b` |

- Python 测试显式设置 `LING_TEST_DATABASE_URL` 与 `LING_DATABASE_URL` 为 `postgresql+asyncpg://ling@127.0.0.1:55432/ling_test`，并持有 `/tmp/ling_test_lock.sh` 提供的原子锁。测试前后只读查询均为数据库 `ling_test`、角色 `ling`、地址 `127.0.0.1:55432`；共享库 `alembic_version` 保持 `0067_connector_instances`。仓库迁移 head 为 `0078_proactive_daily_quota`；集成 fixture 使用临时 schema，迁移验收在测试自建数据库运行，本轮没有升级共享测试库。
- 本机 PATH 没有 `docker`、`podman`、`buildah`。对 Linux 开发机（文档脱敏地址 `192.0.2.21`）的 SSH 用户不能直接访问 Docker socket，`sudo -n docker` 最初因需口令失败；随后取得授权，只对原 CLI 升级基线项目 `ling-image-accept-009-cli-20261117` 与其 `-updater` 子项目执行只读 `docker ps` / `inspect`。其 API、worker、PostgreSQL 均 healthy，固定使用 `v0.0.8` index digest `sha256:77a20bb9cfde183e64eb1349b773666fc673c6f4a945abe9d889451c026207f1`；卷为带该项目名前缀的 `app-data`、`db-data`。随后候选镜像只安装到全新 Compose project `ling-image-candidate-ab0ddf4-20261117` 和独立 updater 子项目，数据库与应用卷均新建；旧稳定基线未被重建、升级或删卷。本轮未连接 NAS/生产端。

**A1 判定：通过（候选、远端状态与本地数据库隔离均有快照；不代表发布门禁通过）。**

### A2：功能范围与真实路径

| 范围 | 本轮可核验证据 | 判定边界 |
|---|---|---|
| 连接器实例详情、保存目标与实例切换 | 原 S9 Playwright/Chromium 专项 9/9；把保存目标变异为 `default` 后按预期以 `default` 对 `main` 失败，恢复后通过。后续 generation 修复在 `connector-instances.browser.test.tsx` 加入三条 scoped GET / 连续保存 / 409 刷新场景；省略保存后 `refreshInstanceView` 的变异使同页连续保存用例以 generation `0` 对 `1` 失败，恢复文件 SHA 后定向三条 3/3 通过。本机 Web 全套 39 文件、1042 passed、1 skipped。5.21 隔离执行报告记载 API OCI revision `981a815c67b689b1bb863a3a858ac5358e99ee6d` 的真实 Chromium 两轮保存：`If-Match: 1`、`If-Match: 2` 均 PUT `202`，随后从 PostgreSQL 读回配置。追加的 `ab0ddf4` 固定摘要容器实测见下方运行记录。 | 本地 `/v1` 组件用例均由 `page.route` mock；mock 测试只证明客户端契约。PUT `202` 是请求受理，持久化结论另由 PG 读回支撑。密钥清空、所有者权限与部署失败回滚仍需各自证据。早期 5.21 日志：`/tmp/ling-009-product-smoke-981a815.log`、`/tmp/ling-009-product-smoke-981a815-second.log`。 |
| 关系与窄屏布局 | 5.21 smoke 的真实关系页 subjects API 返回 `200`，手机视口无横向溢出。 | 没有在候选真实 API 上验关系合并/拆分、权限拒绝或大量数据分页；本地 identity browser 用 route mock。 |
| 日程当前活动、原因、手动生成与失败重试 | 5.21 独立作业报告为 `POST 202 → GET 200 → degraded`；第一轮 smoke 的手动生成 `POST 202`。第二轮脚本重试得到 `POST 409`，记录归因为 `life_timeline.py` 预算/模型闸拒绝。 | `degraded` 不是成功生成；409 是独立预算/模型门禁，不是连接器错误。未覆盖全部状态、PG 持久化和失败重试。 |
| 更新进度、历史耗时与下载线路 | `imageUpdate.browser.test.tsx` 在真浏览器中验证界面交互；另有 `v0.0.8` 旧稳定镜像隔离环境的真 Chromium 检查路径，细节见 A6。 | 浏览器组件的 `/v1` 请求由 route mock；旧镜像检查不是候选升级，也没有切换镜像。 |
| 登录、刷新/新标签、对话与留存 | 5.21 smoke 的真实 API 登录进入总览通过。隔离镜像验收前，执行者报告已把测试用户口令改为固定假口令，凭据指纹不在此回显。历史 `0cfc9be` 浏览器使用 deterministic fake provider。 | 新标签与刷新后的登录恢复尚无本候选证据；fake provider 不证明真实模型能力。 |
| 参考插件功能取舍 | 当前 `docs/UPDATES.md` 记录借鉴 AstrBot 控制台更新状态可见性，并明确不是 `git pull`。 | 冻结判据没有给出更广的参考插件功能清单或逐项取舍来源；除该已记录的更新 UI 取舍外，其余范围未能核验。 |

**A2 判定：部分通过，整体未验。** 非默认 OneBot 两轮保存，以及 `ab0ddf4` 固定摘要容器上的真实 Chromium 登录、总览、关系页、连接器页和 375px 无横溢均已实测；新标签/刷新会话也维持 `/v1/auth/me` 与 `/v1/dashboard` 200。关系合并/拆分、权限与批量数据，以及完整日程/失败路径等冻结范围仍未全部完成真实浏览器/API/PG 验收。候选启用 `LING_TEST_MODE=1`，不证明真实模型能力。

### A3：源码首装与升级

当前 `deploy/install.sh` / `deploy/upgrade.sh` 只完成静态语法检查，未在本轮候选的干净 Docker 环境按 README 源码模式命令首装、升级或做失败恢复。历史隔离部署结果绑定 `0cfc9be`；脚本 hash 相同不足以替代完整候选运行记录。5.21 的 `ling install` 镜像模式首装记录见 A6，不等同于本项源码模式验收。

补充的旧镜像数据恢复证据来自隔离 `_drill` PostgreSQL：备份前写入的 `image-upgrade-009=persist` 在恢复后仍可读取；隔离执行者报告备份与恢复演练各自 0 错误。此证据支持旧镜像隔离环境的数据恢复行为，不是当前候选的源码一行首装/升级验收。初始验收者本机没有 Docker runtime，且当时的 SSH 身份不能直接调用远端 Docker；后续用户在 5.21 隔离项目执行的镜像模式 CLI 验收记在 A6，仍不替代源码模式首装/升级。

**A3 判定：未验。** 5.21 Docker 访问现已可用，但本轮只在候选上完成镜像模式首装；源码模式 README 一行首装、升级与失败恢复没有在 `ab0ddf4` 上执行。

### A4：版本、Release 与 CI

- `.venv/bin/python scripts/check_release.py --tag v0.0.9` 退出码 0，三处应用版本（`pyproject.toml`、`src/ling/__init__.py`、Web `package.json`）一致为 `0.0.9`。
- `.venv/bin/python deploy/release/catalog.py validate` 退出码 0；`deploy/update-notes.json` schema 有效并含 4 个版本条目。
- 当前已验证候选 head `ab0ddf41b81245bce40701ea1fdfebdba79bd06e` 的 PR CI run `44006742093` 最终通过；Python 3.12 首轮单例失败，单 job 重跑通过，原因未定位。SHA-only run `44015600964` 的 verify、CI、mobile、API image、updater-image 全成功，`release` 按设计 skipped。该 dispatch 只推 SHA 标签，不是正式 Release。候选仍不是远端 `main` 祖先，稳定 tag 祖先门禁不满足。当前 `update-notes.json` 的 CHANGELOG URL 指向 `main`；候选合入后才会显示本地 0.0.9 条目。

**A4 判定：失败（未满足 main 祖先门禁；当前 PR CI 与 SHA-only 必需门禁通过，Python 3.12 有一条未定位的首轮间歇失败）。**

### A5：镜像身份

此前 `8fc335b` 的 SHA 镜像结果保留作历史记录。本次 workflow_dispatch run `44015600964` 为候选 `ab0ddf41b81245bce40701ea1fdfebdba79bd06e` 成功构建并推送 SHA 标签，Release job 按设计 skipped。构建 job 核对 OCI revision/version/source、API 内 `LING_COMMIT` 与 Web manifest，以及 updater 内容、非 root 与只读根启动。随后用 GHCR pull token 读取原始 index 和平台 manifest bytes，分别重算摘要并与 registry descriptor 比较，均一致：

| 镜像 | index digest | linux/amd64 manifest | linux/arm64 manifest |
|---|---|---|---|
| `ghcr.io/qsbb/ling-api:sha-ab0ddf41b81245bce40701ea1fdfebdba79bd06e` | `sha256:5ab6f3bc7ad4f1161da783ceb717835e17383f0a362c89f1479b0387b68bf2ea` | `sha256:f9b36a405309a963b5ac15dc4309ca505b8de4768f266571b4c2a9728a624b39` | `sha256:d1abea68a0b066fa13e9deb9683e81c51c6f1c3e1ea351553dc87bbf3c3a735b` |
| `ghcr.io/qsbb/ling-updater:sha-ab0ddf41b81245bce40701ea1fdfebdba79bd06e` | `sha256:ff934276b045ffb32d4ee33943cfc616fd01f5733417414ddc3e2123c31ffcae` | `sha256:38150eb166a37ca31e6ad05f391a4ad1b9bfbdc8d0200ab7517603035966c6c8` | `sha256:95a26b0429060e3cd678307f5e094f22f5a788ec3d4b30f6278bfa131bf80f26` |

Successful jobs verified `version=0.0.9`, `revision=ab0ddf41b81245bce40701ea1fdfebdba79bd06e`, `source=https://github.com/qsbb/ling`; the API job also verified `LING_COMMIT` and the embedded Web manifest, and the updater job verified its runtime contents and non-root/read-only startup. This is a SHA-tagged candidate, not a stable tag. A later docs-only commit changes PR head and requires a new SHA image identity check; the existing 5.21 runtime evidence remains bound to this code-identical candidate.

**A5 判定：通过（`ab0ddf4` 双平台 SHA 镜像 index、平台摘要与 OCI 身份均核验通过；后续 docs-only head 待重建）。**

### A6：真实 updater 与状态对账

5.21 隔离执行报告：真实 Chromium 登录 `v0.0.8` 旧稳定镜像并打开 `/settings/about` 后识别版本 `0.0.8`；检查更新的 `POST /v1/version/image-update-checks` 返回 `202`（本地记录 `/tmp/ling-009-image-check-confirmed.log`），之后 updater `status.json` 有新 `check_id`、`checked_at=2026-11-16T22:48:59Z`、`state=ok`、`current.version=0.0.8`，digest 与稳定镜像一致。`202` 只表示受理，终态来自 status；该操作没有升级镜像。

同一隔离镜像环境的执行者报告 `ling doctor` 只读检查为 `ok=27, warn=1, fail=0`；唯一 warn 是测试用户未配置备份 cron（预期），GHCR 固定摘要可达，并有两份完整恢复点。运行时曾临时通过 Compose 环境覆盖应用项目名，因此该结果**不能证明安装器原始 `.env` 项目名正确**；这正是本轮 CLI 回归补测的边界。另有上一节记录的 `_drill` PostgreSQL sentinel 恢复证据。上述 doctor 与恢复点摘要来自隔离执行者报告，未在本机重连该环境复查。

候选 CLI `981a815` 在 5.21 隔离项目 `ling-image-accept-009-cli-20261117` 使用固定 `v0.0.8` API/updater digest 运行 `ling install --with-updater --enable-updates`，退出 0；未传项目名覆盖。updater 容器 `LING_COMPOSE_PROJECT` 与自定义应用项目名一致，`release_check.current` 识别 `0.0.8` 与固定 digest。该项目未被覆盖为其他安装，随后作为网页升级基线保留。此项验证 CLI 和旧稳定 updater 启动，不是候选 updater 实际更新验收。

同一 CLI 隔离项目的执行者补充了数据恢复证据：`.env` SHA 短指纹 `71394e4f...ac25a`、`updater/.env` 短指纹 `a8b20780...2c252`；固定假口令对应的哈希指纹单独保存且不在此回显。`ling backup` 与 `ling restore-drill` 均退出 0，隔离 `_drill` PostgreSQL 读回哨兵 `image-cli-upgrade-009=persist`，媒体 SHA 短指纹为 `21ed498f...106cf`。这些摘要来自 5.21 执行者报告，短指纹按交接文本记录；该项目适合作为之后单跳 `v0.0.8 → v0.0.9` 网页升级的基线，但尚未实际执行该升级。

候选 `ab0ddf4` 镜像已在新建的隔离 Compose 项目 `ling-image-candidate-ab0ddf4-20261117` 实际启动。使用的固定摘要为本节 A5 的 API/updater index digest；安装 CLI 来自候选 `deploy/cli/ling.py`，远端 SHA 与本地一致。`ling install --non-interactive --with-updater --test-mode` 指向独立安装目录 `/opt/ling-image-candidate-ab0ddf4-20261117`、端口 `20487` 和新建的项目专属卷。安装期间 GHCR Docker pull 首两次分别在不同 layer 报 `not found`；registry HEAD/Range 核对均可读，第三次对相同摘要执行 Docker 原生 pull 成功，随后 CLI 使用本地已核对摘要完成安装。没有改动保留的 `v0.0.8` 网页升级基线。

安装器核对镜像 `version=0.0.9`、`commit=ab0ddf41b81245bce40701ea1fdfebdba79bd06e`，单次迁移到 `0078_proactive_daily_quota`，`/readyz=200`；API、worker、PostgreSQL 均 healthy。候选 API/worker 运行镜像 ID 和 RepoDigest 与 A5 API index digest 一致，卷名为 `ling-image-candidate-ab0ddf4-20261117_app-data` 与 `_db-data`。updater 项目 `ling-image-candidate-ab0ddf4-20261117-updater` 已按 A5 updater digest 启动，`ling doctor` 确认容器运行、协议 1、摘要一致。自动网页更新保持 `LING_UPDATE_ENABLED=0`；候选运行启用了 `LING_TEST_MODE=1`，不会调用真实模型。

真实 Chromium 经 SSH 本地转发登录进入总览成功，`GET /v1/version` 返回 200、版本 `0.0.9`、提交 `ab0ddf4` 和 Web manifest 摘要；关系页与连接器页加载时 `/v1` 请求均为 200。新标签与刷新后 `/v1/auth/me`、`/v1/dashboard` 均为 200；两个页面在 375px 视口的 `documentElement.scrollWidth` 均为 375px。测试模式只验证会话、API、布局和页面数据读取，不证明真实模型能力。

该候选项目 `ling backup` 生成完整 dump，CLI 校验 `dump_sha256` 与 manifest 一致；`ling restore-drill` 恢复到项目专属 `_drill` 数据库，通过关键表、墓碑和媒体覆盖核对。新库行数为 `users=1, windows=0, turns=0, messages=0, memories=0`，因此本次没有非空业务数据哨兵可证明有记录被恢复。备份后 `ling doctor` 为 `ok=27, warn=1, fail=0`；唯一 warn 为隔离项目没有 root crontab 备份任务。上述步骤均未触碰生产库、NAS 或 `v0.0.8` 基线项目。

候选 updater 的镜像和运行容器身份已验，但因自动更新开关保持关闭且 GHCR 尚无 `v0.0.9` 稳定 tag，仍未验证候选 updater 的真实更新任务阶段/耗时、重复/并发、断线、忙闲重启、中断对账、迁移故障、maintenance gate，或稳定版网页升级前后的 Cookie/配置/PostgreSQL/媒体状态。因此候选镜像启动通过不代表更新链路通过。

**A6 判定：部分通过。** 候选 API/updater 固定摘要已在独立 5.21 项目实际启动，status/doctor、真实 Chromium 登录与页面/API 路径、首份备份和隔离 restore-drill 通过；真实网页更新及 updater 故障注入仍未验。

### A7：稳定版网页更新

GHCR 只读查询没有 `v0.0.9` 稳定 tag 或对应 digest。未创建 tag/Release，也未在隔离镜像部署中从 `v0.0.8` 经 GHCR 直连及可达加速线路升级。旧镜像上的检查请求不是版本升级。5.21 CLI 隔离项目已保留作升级基线。若正式流程只产生一个新的稳定版本，本轮最多能验一跳 `v0.0.8 → v0.0.9`；没有第二个可区分的稳定摘要时，不得宣称两跳连续升级已验，也不伪造 tag 或放宽 updater 校验。

**A7 判定：需外部动作。**

### A8：文档与本地测试结果

README、CHANGELOG、DEPLOYMENT、UPDATES 与本记录区分原始代码快照 `62e41c9`、实现提交 `1edc369`/`981a815`、实现与运行验收候选 `ab0ddf4` 和历史部署快照 `0cfc9be`。PR #1 文档 head `fb0d5fb` 的 Web、Python 3.12、Python 3.13 CI 均成功；本轮接续修订仅涉及文档，尚未推送或进入 CI。若推送该文档提交，仍须对新 PR head 重跑 PR CI；若要求镜像身份绑定完整新 SHA，也须运行 SHA-only 镜像工作流。`deploy/update-notes.json` 文案与候选变更范围一致，版本号未改动。本记录不自引用文档提交 SHA。

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
| 后续全量 Python | `ab0ddf4` 代码未改；此次持相同 PostgreSQL 锁重跑，全量 `pytest -q` 退出码 0、进度 100%，日志 `/tmp/ling-pytest-docs-final-0.0.9-20261117.log`，有 warnings summary、无最终用例计数，不推算通过数。 |
| PR CI | run `44006742093` 最终 Python 3.12/3.13 与 Web 均通过。Python 3.12 attempt 1 的 `test_worker_resumes_delivery_using_each_persisted_window_instance[None-qqbot]` 失败，attempt 2 单 job 重跑通过；本机对该单例的隔离 PostgreSQL 定向运行也通过。原因未定位，记录为一次间歇失败风险。 |
| 候选 SHA 镜像 | workflow_dispatch `44015600964` 对 `ab0ddf4` 的 verify、mobile、Python 3.12/3.13、Web、API image、updater-image 全成功；Release job skipped，无稳定 tag/Release。A5 列出 index 与平台摘要。 |
| 5.21 候选 API smoke | 执行者报告 revision 为实现提交 `981a815`；两轮非默认 OneBot 保存 `If-Match 1/2 → PUT 202 → 刷新后 PG 读回`。真实登录进入总览、关系 subjects API 200、窄屏布局通过。首轮手动日程生成 POST 202；第二轮 POST 409 是预算/模型门禁，不能算连接器失败。日志 `/tmp/ling-009-product-smoke-981a815.log`、`/tmp/ling-009-product-smoke-981a815-second.log`。 |
| 5.21 CLI、备份与恢复 | 自定义隔离项目以 `v0.0.8` 固定 API/updater digest 首装成功，updater 应用项目名与 `release_check.current` 正确；`ling backup` 和 `ling restore-drill` 均退出 0，恢复 `_drill` PG 哨兵可读，媒体/env SHA 记录短指纹。项目保留为未来网页升级基线；未执行升级。 |
| `ab0ddf4` 固定摘要容器安装 | 专用项目 `ling-image-candidate-ab0ddf4-20261117` 安装 API index `sha256:5ab6f3bc7ad4f1161da783ceb717835e17383f0a362c89f1479b0387b68bf2ea`，执行单次迁移后 `/readyz=200`；`ling status` 的 image ID、RepoDigest、版本/commit、卷与 schema 均匹配，API/worker/PostgreSQL healthy。更新器子项目以 updater index `sha256:ff934276b045ffb32d4ee33943cfc616fd01f5733417414ddc3e2123c31ffcae` 运行，`ling doctor` 读到 updater 摘要/协议匹配。自动更新开关保持 0。 |
| `ab0ddf4` 真 Chromium + API | SSH 隧道后的 Chromium 登录总览成功；版本接口返回 200、`0.0.9`、完整候选 commit 与 Web manifest。关系页、连接器页真实 API 请求 200；新标签与刷新后 auth/me 和 dashboard 均 200。两个页面在 375px 下 `scrollWidth=375`。测试模式启用，没有调用真实模型。 |
| `ab0ddf4` 备份恢复 | `ling backup` 退出 0，dump SHA 与 manifest 一致；`ling restore-drill` 退出 0，隔离库表计数、墓碑与媒体覆盖通过。基线只有 1 个 tester 用户、0 windows/turns/messages，因此没有非空业务哨兵可以证明数据行恢复。备份后的 `ling doctor` 为 `ok=27,warn=1,fail=0`，唯一 warn 为未配置 root crontab。 |

**A8 判定：通过（记录区分原始基线、最终代码候选、旧镜像报告与 mock 边界；不代表发布门禁通过）。**

### 最终判定

| ID | 结论 |
|---|---|
| A1 | 通过（快照和本机隔离范围） |
| A2 | 部分通过，整体未验（连接器两轮保存、登录进入总览、关系列表读取、新标签/刷新会话与窄屏通过；关系合并/拆分和完整日程未验） |
| A3 | 未验（源码模式首装/升级未在候选上实跑；镜像模式首装已在 A6 实测） |
| A4 | 失败（未满足 main 祖先门禁；`ab0ddf4` PR CI 与 SHA-only 必需门禁通过，Python 3.12 首轮一条失败后重跑通过；`fb0d5fb` 文档 head 的 PR CI 三项也通过） |
| A5 | 通过（`ab0ddf4` SHA 镜像 index、平台摘要与 OCI 身份已验证；当前未推送的文档修订没有对应的新 SHA 镜像身份工作流） |
| A6 | 部分通过（候选 API/updater 容器、浏览器/API 路径、备份与 restore-drill 已验；真实网页更新和 updater 故障注入未验） |
| A7 | 需外部动作（无稳定 tag，未跑真实网页升级） |
| A8 | 通过（候选/历史范围已区分，No-Go 明确） |

**整体 Go/No-Go：No-Go。** 候选不在 `main` 祖先链；候选 updater 的实际更新任务与稳定版网页升级仍未验，且当前没有 `v0.0.9` 稳定 tag 可供单跳升级。

**剩余门禁：**候选 `ab0ddf4` 的 PR CI、SHA-only 双镜像及 5.21 隔离容器/普通浏览器页面路径已验；PR 文档 head `fb0d5fb` 的 CI 三项也通过。当前工作区的进一步文案校准尚未推送或进入 CI。源码模式首装/升级、关系合并/拆分、稳定版单跳网页升级、updater 执行真实更新任务及直连/加速/断线/中断对账未完成。候选不在 `main` 祖先链。本轮没有合并 PR、创建稳定 tag/Release 或访问 NAS 生产端。
