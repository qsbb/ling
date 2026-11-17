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
- 文档提交 `fb0d5fb92b5599e3f59b6022a4f4b4d0aabe712a` 后只读检查 PR #1 head 与本地 HEAD 一致；CI run `44025317841` 对该 head 的 Web、Python 3.12、Python 3.13 均成功。本轮接续时工作树干净，`origin/lingxi/release-0.0.8` 与该 SHA 一致。之后的文案校准分别提交于 `96abcd3` 和 `915644d`，只改文档；run `44025317841` 的证据不覆盖这两个提交。
- 后续 PR head `3a7a1f8b27006e6375c8138ee9bb17f23b7a572a` 的 PR CI run `44087255580` 于 2026-11-17 完成；Web、Python 3.12、Python 3.13 均成功。此后仅本验收文档再追加说明；该 CI 不覆盖本条新内容。代码与部署实现未变。
- 该 head 的 SHA-only `workflow_dispatch` run `43989637070` 全部成功：verify、mobile、Python 3.12/3.13、Web、API image、updater-image；release job 按设计 skipped。标签只含 `sha-8fc335b8b6723b72f49d3f160ddf2bc7792a4bff`，没有创建稳定 tag/Release。
- `8fc335b` 的 SHA 镜像构建证据见 A5 历史记录。随后为实现/运行验收候选 `ab0ddf4` 另跑 PR CI 和 SHA-only workflow；其镜像与运行结果分列于后续条目及当前 A5/A6。
- 实现与安装验收绑定的后续候选 head `ab0ddf41b81245bce40701ea1fdfebdba79bd06e`（本次文档复验前的 PR head；基于同一 Web/CLI 实现再加 docs-only 提交）。PR CI run `44006742093` 最终三项均通过：Web、Python 3.13、Python 3.12。Python 3.12 首次 attempt 有一条 QQBot 投递集成用例失败；同一 job 单独重跑成功，本机隔离 PostgreSQL 对应单例也通过。失败原因未定位，因此保留为并行全量测试的间歇风险，不把首轮失败从记录中抹去。
- SHA-only release workflow run `44015600964` 对 `ab0ddf41b81245bce40701ea1fdfebdba79bd06e` 全部必要门禁成功：verify、Python 3.12/3.13、Web、mobile、API image、updater-image；`release` job 按设计 skipped。没有创建 `v0.0.9` tag 或 Release。API/updater index 与平台摘要、OCI 身份见 A5。
- GHCR 只读核验：`ling-api` 与 `ling-updater` 稳定 tag 清单均为 `v0.0.2`、`v0.0.3`、`v0.0.4`、`v0.0.5`、`v0.0.6`、`v0.0.8`，没有 `v0.0.9`。在 `v0.0.8` registry index 下直接读取 manifest，并以响应内容 SHA-256 复核平台 descriptor；OCI labels 的 version/revision/source 均为 `0.0.8` / `0d36dbadc4c8f688df2775e81f1e5e795006a271` / `https://github.com/qsbb/ling`：

- 2026-11-17 全量 pytest 结束后、补写本段前的工作树快照：`HEAD=8fc335b8b6723b72f49d3f160ddf2bc7792a4bff`、分支 `lingxi/release-0.0.8`；`git status --short` 仅列 `README.md`、`CHANGELOG.md`、`docs/DEPLOYMENT.md`、`docs/UPDATES.md`、本记录五份文档。该时点 SHA-256：README `42c8bb19082f716e86db44eec145031d45d9f6363e837dfcb1e40baa3e434c9c`；CHANGELOG `f50c00fba8aa36c2a6b65e6e49cc8ac1eea52498fe1352e9f0d894a352300c22`；DEPLOYMENT `99ac189547ae653fd2dd3dbaa1df989c3066032a81f0969d55439ed17838fae9`；UPDATES `cb2e95a54556ed88fd7cbc2260c8a4956a00ff6d2295b671383645619d8ed167`；本记录 `2b69f0c7894f67397c2f1e7286d379621482e71166845f36ca9e9ed61bc57061`。这些是测试完成后的观察值；本次运行开始前没有单独保存这五份文档的 SHA 清单，因此不将它们冒记为运行前哈希。代码文件复验后 SHA：`ConnectorsPage.tsx=92f83aa93029a0132f25b7c3a1bceba050b8dd1ad7ce5f54f747298662ea91ec`、`deploy/cli/ling.py=0e19469b63015ac4bb6ff83a98f9bed5814eaf560ffcf9602c798a49d3926f2f`。
- docs-only 更新推送前快照：`HEAD=ab0ddf41b81245bce40701ea1fdfebdba79bd06e`、分支 `lingxi/release-0.0.8`；`git status --short` 仅列 `README.md`、`CHANGELOG.md`、`docs/DEPLOYMENT.md`、`docs/UPDATES.md`、本记录五份文档。该时点 SHA-256：README `eea22e87afe5b07e2df5974648bb1b918212624890d2fb727881603a6ec12399`；CHANGELOG `a2567b624352123dfd5dee940280402af9720e6a309be69632529c79c5c9c11a`；DEPLOYMENT `4b4836d0552283686d131fa1382266a68cd305c468e02a71cc487806339f7e05`；UPDATES `a8eb8d3625d12340348f99d0a7dead9963491f1e854295884c9a440b49c063c6`；本记录（加入本条之前）`01f429d44d894916588d558084b073c20e5beb380f8bb5e466d51db4a48276f9`。代码文件仍为已验 SHA：`ConnectorsPage.tsx=92f83aa93029a0132f25b7c3a1bceba050b8dd1ad7ce5f54f747298662ea91ec`、`deploy/cli/ling.py=0e19469b63015ac4bb6ff83a98f9bed5814eaf560ffcf9602c798a49d3926f2f`。
- 本次 docs-only 提交前快照：`HEAD=ab0ddf41b81245bce40701ea1fdfebdba79bd06e`、分支 `lingxi/release-0.0.8`；`git status --short` 仅列 README、CHANGELOG、DEPLOYMENT、UPDATES 与本记录五份文档。该时点 SHA-256：README `eea22e87afe5b07e2df5974648bb1b918212624890d2fb727881603a6ec12399`；CHANGELOG `a2567b624352123dfd5dee940280402af9720e6a309be69632529c79c5c9c11a`；DEPLOYMENT `4b4836d0552283686d131fa1382266a68cd305c468e02a71cc487806339f7e05`；UPDATES `a8eb8d3625d12340348f99d0a7dead9963491f1e854295884c9a440b49c063c6`；本记录（加入本条之前）`35743d7f1d0b25ce60f0f0e9fb89d54841e392c174c6fadc2b4ca0e604bf9faa`。已验源文件仍相同：`ConnectorsPage.tsx=92f83aa93029a0132f25b7c3a1bceba050b8dd1ad7ce5f54f747298662ea91ec`、`deploy/cli/ling.py=0e19469b63015ac4bb6ff83a98f9bed5814eaf560ffcf9602c798a49d3926f2f`。
- 本轮接续文档复核起点（2026-11-17）：`HEAD=fb0d5fb92b5599e3f59b6022a4f4b4d0aabe712a`、分支 `lingxi/release-0.0.8`，`git status --short` 为空，`origin/lingxi/release-0.0.8` 与 HEAD 相同。起点 SHA-256：README `eea22e87afe5b07e2df5974648bb1b918212624890d2fb727881603a6ec12399`；CHANGELOG `a2567b624352123dfd5dee940280402af9720e6a309be69632529c79c5c9c11a`；DEPLOYMENT `4b4836d0552283686d131fa1382266a68cd305c468e02a71cc487806339f7e05`；UPDATES `a8eb8d3625d12340348f99d0a7dead9963491f1e854295884c9a440b49c063c6`；本记录 `52d92377a82035b86c38a259d73e981bd6598afc3a852e5e6d35d6dcadc0f7a8`。这些值绑定复核前快照，不是后续修改后的哈希。
- 2026-11-17 14:31（Asia/Shanghai）在 5.21 测试主机通过 sudo 对隔离项目 `ling-image-candidate-ab0ddf4-20261117` 执行只读 `docker ps -a` 名称过滤。该项下 API、worker、PostgreSQL 均显示 healthy，updater 显示 Up；本次没有执行容器命令、数据库查询或资源变更。此观测确认目标隔离项目仍在运行，不替代前述镜像摘要、卷身份与数据恢复记录。
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

截至 2026-11-17，候选 updater 的镜像和运行容器身份已验，但自动更新开关保持关闭且 GHCR 尚无 `v0.0.9` 稳定 tag；当时尚未验证候选 updater 的真实更新任务阶段/耗时、重复/并发、断线、忙闲重启、中断对账、迁移故障、maintenance gate，或稳定版网页升级前后的 Cookie/配置/PostgreSQL/媒体状态。因此候选镜像启动通过不代表更新链路通过。

**A6 判定：部分通过。** 候选 API/updater 固定摘要已在独立 5.21 项目实际启动，status/doctor、真实 Chromium 登录与页面/API 路径、首份备份和隔离 restore-drill 通过；真实网页更新及 updater 故障注入仍未验。

### A7：稳定版网页更新（截至 2026-11-17）

GHCR 只读查询没有 `v0.0.9` 稳定 tag 或对应 digest。未创建 tag/Release，也未在隔离镜像部署中从 `v0.0.8` 经 GHCR 直连及可达加速线路升级。旧镜像上的检查请求不是版本升级。5.21 CLI 隔离项目已保留作升级基线。若正式流程只产生一个新的稳定版本，本轮最多能验一跳 `v0.0.8 → v0.0.9`；没有第二个可区分的稳定摘要时，不得宣称两跳连续升级已验，也不伪造 tag 或放宽 updater 校验。

**A7 判定：需外部动作。**

### A8：文档与本地测试结果

README、CHANGELOG、DEPLOYMENT、UPDATES 与本记录区分原始代码快照 `62e41c9`、实现提交 `1edc369`/`981a815`、实现与运行验收候选 `ab0ddf4` 和历史部署快照 `0cfc9be`。PR #1 head `3a7a1f8` 的 Web、Python 3.12、Python 3.13 CI run `44087255580` 均成功。本轮新增的文档说明发生在该 run 之后，仅改验收记录；推送后仍需为新的 PR head 重跑 CI。镜像身份仍绑定 `ab0ddf4`；合并后应先对 main 合并提交运行 SHA-only workflow，再打稳定 tag。`deploy/update-notes.json` 文案与候选变更范围一致，版本号未改动。本记录不自引用文档提交 SHA。

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

### 最终判定（截至 2026-11-17）

| ID | 结论 |
|---|---|
| A1 | 通过（候选快照、隔离测试范围及 5.21 命名隔离项目的只读容器核对） |
| A2 | 部分通过，整体未验（连接器两轮保存、登录进入总览、关系列表读取、新标签/刷新会话与窄屏通过；关系合并/拆分和完整日程未验） |
| A3 | 未验（源码模式首装/升级未在候选上实跑；镜像模式首装已在 A6 实测） |
| A4 | 失败（此记录时 PR 尚未合并，main 祖先门禁未满足；`ab0ddf4` PR CI 与 SHA-only 必需门禁通过，Python 3.12 首轮一条失败后重跑通过；`fb0d5fb` 与 `3a7a1f8` 两个 PR 文档 head 的 CI 三项均通过） |
| A5 | 通过（`ab0ddf4` SHA 镜像 index、平台摘要与 OCI 身份已验证；摘要绑定 `ab0ddf4`，新文档 head `3a7a1f8` 没有单独的 SHA 镜像身份工作流） |
| A6 | 部分通过（候选 API/updater 容器、浏览器/API 路径、备份与 restore-drill 已验；真实网页更新和 updater 故障注入未验） |
| A7 | 需外部动作（无稳定 tag，未跑真实网页升级） |
| A8 | 通过（候选/历史范围已区分，No-Go 明确） |

**整体 Go/No-Go：No-Go。** 候选不在 `main` 祖先链；候选 updater 的实际更新任务与稳定版网页升级仍未验，且当前没有 `v0.0.9` 稳定 tag 可供单跳升级。


**剩余门禁（该历史快照）：**`ab0ddf4` 的 PR CI、SHA-only 双镜像及 5.21 隔离容器/普通浏览器页面路径已验；PR head `3a7a1f8` 的 CI 三项也通过。本条文档说明是在该 run 后新增，待新 head CI。合并后还须对 main 合并提交运行 SHA-only image workflow，再创建稳定 tag。源码模式首装/升级、关系合并/拆分、稳定版单跳网页升级、updater 执行真实更新任务及直连/加速/断线/中断对账未完成。此快照时 PR 尚未合并，本轮没有创建稳定 tag/Release 或访问 NAS 生产端。

### 2026-11-18 发布后复验

本节更新当前发布和实际更新状态；上文各项仍保留其观测日期与历史边界。目标范围限于命名隔离项目与本地工作树，没有连接 NAS 或生产部署。

**A1：当前快照与远端身份**

- 发布代码、`origin/main` 与 `v0.0.9` tag 解引用均为 `f381a8c76df43a24b60944c91e22be46501bbd8c`；验收分支为 `lingxi/release-0.0.9-final-acceptance`，本轮开始时的文档提交为 `d43f3af4042e7488711d240450319498cdbf66c3`。本节复验只涉及文档和 5.21 上的隔离测试资源，没有改版本、源代码、测试或部署实现。
- GitHub Release [`v0.0.9`](https://github.com/qsbb/ling/releases/tag/v0.0.9) 的发布时间为 `2026-11-17T16:01:11Z`（上海时间 `2026-11-18 00:01:11`）。release workflow [run `44112900865`](https://github.com/qsbb/ling/actions/runs/44112900865) 的 verify、Python 3.12/3.13、Web、mobile、API image、updater-image 与 release jobs 全部成功。
- 同一 SHA 的较早 push CI run `44102373375` 中，Python 3.12 的 `test_slow_media_does_not_block_ordinary_events` 失败（“慢媒体把普通事件也堵住了”）；release workflow 对该 SHA 重跑全绿。失败原因未定位，保留为间歇风险。
- GHCR `v0.0.9` index 与平台 manifest：

  | 镜像 | index digest | linux/amd64 manifest | linux/arm64 manifest |
  |---|---|---|---|
  | `ghcr.io/qsbb/ling-api:v0.0.9` | `sha256:477146f2e2989e8df50a36b6ccc4b696c1b8072e66206d07849695e709724756` | `sha256:0cca394aa99aef2be8c0d71f8e2a44650ae8407b542a06f011dbb196522a6b20` | `sha256:68e60fc2162fd288b337c5847d2be17597a73d3c0325d5304d30464afc5cb321` |
  | `ghcr.io/qsbb/ling-updater:v0.0.9` | `sha256:324f211911a2f41a6538104c6c29b9a4c9eb913e4de15969fbc1b0be704c7429` | `sha256:6a77a0e8a9dd85915f59108167365fafece0a2c5c5637d06afc3dd4af25565ee` | `sha256:c82cce538de2b290f073b9cdafdc2df3510fe3f328a392d65e82e710a8c853ad` |

- Registry manifest/config reads confirmed both indexes contain amd64 and arm64, and all platform image labels report `version=0.0.9`, `revision=f381a8c76df43a24b60944c91e22be46501bbd8c`, source `https://github.com/qsbb/ling`.

**A2：当前候选真实浏览器路径**

- 独立源码项目 `ling-src-one-line-20261118` 的 checkout 为完整 SHA `f381a8c76df43a24b60944c91e22be46501bbd8c`；API、worker、PostgreSQL healthy，迁移 head `0078_proactive_daily_quota`，端口 `20488`，应用卷为 `ling-src-one-line-20261118_app-data`。首次安装前账户有强制改密标志。真实 Chromium 完成登录、强制改密（接口 204）、改密后 `/v1/auth/me` 与 dashboard 200；之后 `LING_TEST_MODE=1` 的 W0 套件通过 34/34，包含桌面/手机聊天、刷新、停止/重试、设置、账号及总览路径。日志 `/tmp/ling-w0-candidate-0.0.9-20261118.log`。
- 测试期间以 Compose 环境覆盖临时启用 `LING_TEST_MODE=1`，测试后用原 `.env` 重建 API/worker，确认 `LING_TEST_MODE=0` 且 `/readyz=200`。临时口令通过恢复表保存原哈希后使用；测试结束原哈希、更新时间和 `must_change_password=true` 均核对恢复，7 个测试会话吊销，恢复表删除。浏览器套件新增的对话数据留在此命名隔离测试库。
- 旧版源码项目 `ling-src-upgrade-20261118`（`v0.0.8` commit `0d36dbadc4c8f688df2775e81f1e5e795006a271`）W0 套件通过 34/34，日志 `/tmp/ling-w0-source-acceptance-20261118-r1-redacted.log`。第一次使用错误测试账号时登录失败，没有通过鉴权或执行写操作；随后用临时账户口令重跑。原密码哈希与账户标记已恢复，17 个测试会话吊销。
- 发布后新增的固定源码检查中，`ling-src-bootstrap-release009-20261118` 的 `v0.0.8` checkout W0 套件通过 34/34，日志 `/tmp/ling-w0-bootstrap-old-source-v0.0.8-20261118.log`；`v0.0.8` 镜像项目 `ling-image-current-updater-20261118` 的 W0 套件为 32/33，唯一失败是“桌面：失败后可见重试按钮”，日志 `/tmp/ling-w0-image-current-updater-v0.0.8-20261118.log`。截图中该错误场景实际收到正常 fake 回复；失败原因未定位，不能把这组镜像基线浏览器套件记为全通过。
- 旧源码项目 `ling-src-release009-20261118` 升到候选后，候选 W0 套件通过 34/34，日志 `/tmp/ling-w0-source-v0.0.9-after-upgrade-20261118.log`。这些浏览器结果使用 `LING_TEST_MODE=1` 和 fake provider；它们不证明真实模型能力。关系合并/拆分、权限拒绝、大量数据分页和完整日程失败恢复仍未覆盖。

**A3：源码模式安装与升级**

- 固定 v0.0.8 checkout 的首次命令 `bash deploy/install.sh --project ling-src-release009-20261118 --port 20490` 在容器、卷和 `.env` 创建前以“缺 pnpm”退出 1；当时 5.21 没有 `pnpm`。在 Mac 对同一源码运行 `bash deploy/install.sh --build-web-only`、把匹配的 `dist` 复制到隔离 checkout 后，`--prebuilt-web` 首装成功。构建产物清单与源码匹配；安装创建项目专属卷，`.env` 权限为 0600。首装过程中出现一条非致命 `ports: command not found`，安装仍健康完成。
- 源码实例 `ling-src-release009-20261118` 在 v0.0.8 上完成 W0 与媒体上传后，数据库 head 为 `0070_voice_reply_projection`，`users/windows/turns/messages/memories/media_evidence = 1/3/4/8/0/1`；媒体 SHA 分别为 `2c8772be6e1d925eded3b93729fda19d0f1cf31a3b9c8537a2a81d34921d8944` 和 `a3fc2de06e10af91f6360dc21db363f5c3abad53e51e15883a8e0b1b89461915`，`.env` SHA 为 `777f9b5898f8a6bc21c1e058b7aa62f3ca49882dab6dff270b7f9dbd6e55ad86`。项目专属 app/db 卷为 `ling-src-release009-20261118_app-data` / `ling-src-release009-20261118_db-data`。
- 从 v0.0.8 checkout 原样执行 README 升级命令 `bash deploy/upgrade.sh --project ling-src-bootstrap-release009-20261118 --port 20493`，shell 在任何变更前以退出码 127 报 `deploy/upgrade.sh: No such file or directory`；该 tag 不包含此文件。因此 v0.0.8 部署不能按 v0.0.9 README 中的升级命令启动快进升级。
- 另一个 v0.0.8 源码项目中，先人工 `git pull --ff-only` 到 `f381a8c`，再运行候选 `bash deploy/upgrade.sh --project ling-src-release009-20261118 --prebuilt-web`，源码部署成功。这证明候选安装脚本可在保留卷上升级旧 schema；但它不是从干净 v0.0.8 checkout 执行 README 单行升级命令，不能填补上面的 bootstrap 缺口。升级后 `/readyz=200`、head 为 `0078_proactive_daily_quota`、业务表计数保持 `1/3/4/8/0/1`，`.env` SHA、两个媒体 SHA 与 app/db 卷名不变；升级前浏览器 Cookie 的 `/v1/auth/me` 与 dashboard 在升级后均为 200。升级后 W0 34/34。
- 单独的 `ling-src-bootstrap-release009-20261118` 在 v0.0.8 首装并通过 W0 34/34；README 升级脚本失败后，`docker compose down` 未删除数据卷。以同一 Compose/env 重新启动后，`/readyz=200`、数据库仍为 `0070|1|2|4|8|0`，媒体哨兵 SHA `e757e934e3e7fe56b2dca74b51be3d2e8e871f7b11f44bc5e4e2eac361fe6870`、`.env` SHA `573cc191a2a7ad86825885e0381c5a3d87e5d5789884d2216eeb598dce4a6e4b` 均保持。
- 操作偏差与清理：一次候选 `install.sh --prebuilt-web` 忘记传 `--project`，误用默认项目名 `ling`；启动在端口 `20490` 已被目标隔离实例占用时失败。操作前已确认默认项目没有容器/卷；本次新建资源标签和创建时间均核对后，用该隔离 checkout 执行 `docker compose down --volumes --remove-orphans` 清理，确认没有 `ling` 项目容器/卷残留。目标 `ling-src-release009-20261118` 服务和卷未受影响；没有改动其他 Compose 项目。

**A3 判定：部分通过，整体项未通过。** 候选源码部署在显式准备匹配前端并快进代码后能保留 PostgreSQL、配置、媒体和卷；但固定 v0.0.8 checkout 没有 README 要求的 `deploy/upgrade.sh`，而 5.21 直接源码首装缺少 `pnpm`。无法证明固定基线能按文档中的一行命令完成首装与升级。

**A6/A7：隔离镜像更新实测**

- 基线项目 `ling-image-accept-009-cli-20261117` 的 API/updater 原摘要分别为 `sha256:77a20bb9cfde183e64eb1349b773666fc673c6f4a945abe9d889451c026207f1` 与 `sha256:281736db4dedcb29643b01e1be52638c6a019ce7b282bb0d83895a067281fd0f`，两镜像 revision 均为 `0d36dbadc4c8f688df2775e81f1e5e795006a271`。项目 `LING_UPDATE_ENABLED=1`、`LING_TEST_MODE=1`，数据库迁移 head 为 `0070_voice_reply_projection`，API/worker/PG 健康。
- 更新前真实 Chromium W0 套件通过 34/34；隔离 PostgreSQL 为 `users/windows/turns/messages/memories = 1/2/4/8/0`。日志 `/tmp/ling-w0-image-v0.0.8-20261118.log`。此步骤使用测试 fake provider。
- `ling backup` 退出 0；dump `ling-20261118-025321.dump` 的 SHA-256 为 `a51841de7a2b437ee5303f10fda2cfc6c2884074519c3b4266e7cbf287f7692f`，manifest/墓碑与媒体状态校验通过。`ling restore-drill --db ling_codex_a7_20261118_drill` 退出 0，在项目专属 `_drill` 数据库恢复 1 个用户并核对 dump、墓碑、媒体覆盖。Updater 重启后读取到完整恢复点 `backup-0df756761bf8`，`backup_gate=ok`。
- 门禁诊断过程：旧状态的 `no_manifest` 时间早于既有备份；updater 重启后发现旧 root-owned `0600` manifest 但报告 `manifest_unreadable`。通过正式 `ling backup` 新建 manifest 后，updater 再次启动读取到完整恢复点并放行。没有手工改权限或绕过门禁；更新前已记录 `backup_gate=ok`。
- 真实浏览器在 `/settings/about` 确认目标后选择默认 GHCR 直连、勾选迁移风险确认并提交；API 返回 `201`，request ID `0df756761bf887f9a4673b60b0ef76a3`。旧 `v0.0.8` updater 任务于 `2026-11-17T19:09:10Z` 开始、`19:10:02Z` 结束，状态 `succeeded`、最后阶段 `verify`，无失败码。迁移从 `0070_voice_reply_projection` 到 `0078_proactive_daily_quota`，maintenance gate `released/verified=true`；更新器备份完整，墓碑 `ok`，媒体 `absent` 与空媒体引用一致。
- 更新后 API 与 worker 的 `RepoDigest` 均为 `ghcr.io/qsbb/ling-api@sha256:477146f2e2989e8df50a36b6ccc4b696c1b8072e66206d07849695e709724756`，OCI labels 为 `0.0.9` / `f381a8c76df43a24b60944c91e22be46501bbd8c`，健康检查通过。PostgreSQL 仍为 `users/windows/turns/messages/memories = 1/2/4/8/0`；应用卷名保持 `ling-image-accept-009-cli-20261117_app-data`，端口与测试开关不变。主 `.env` SHA-256 从 `71394e4ff5eb81a22826d366ba26141ec6bd8af8f28cd924d39732973b0ac25a` 变为 `28ea37882b6e8a852bd862b3a1b0fc04aafafb1f7bec5da40928a738bc3d7231`，其中 `LING_APP_IMAGE` 更新到目标 digest；`updater/.env` SHA-256 保持 `a8b2078095c2a5b80ccba72741f717e249a893231fc12e569992503d4de2c252`。更新后原浏览器会话 `/v1/auth/me=200`、dashboard=200，cookie 指纹一致。
- 该请求由旧 `v0.0.8` updater 镜像执行；`v0.0.9` updater 镜像已独立核验，但本次没有替换/验证它执行更新。任务有一条非阻断 warning：更新器容器看不到宿主 Docker 根目录，因此磁盘余量未核对。直连一跳成功不等于加速线路、并发/断线/中断故障路径或连续两跳验收。
- 更新后已恢复 `ling` 测试账户原密码哈希/标志，吊销 9 个测试会话并删除临时恢复表；确认只留下该测试项目数据。没有触碰其他 Compose 项目、NAS 或生产端。

**当前 v0.0.9 updater：v0.0.8 稳定镜像一跳复验**

- 独立项目 `ling-image-current-updater-20261118` 用 v0.0.8 API index `sha256:77a20bb9cfde183e64eb1349b773666fc673c6f4a945abe9d889451c026207f1`、源提交 `0d36dbadc4c8f688df2775e81f1e5e795006a271`，以及当前 updater index `sha256:324f211911a2f41a6538104c6c29b9a4c9eb913e4de15969fbc1b0be704c7429` 首装，项目与目录均独立，端口 `20492`。CLI 首装迁移到 `0070_voice_reply_projection` 后，updater 初次启动因宿主 Docker address pool 无空闲子网失败；只停止了本轮已完成的源码测试项目网络并保留卷，随后同一 updater 容器启动成功。此为测试宿主网络容量限制。
- 更新前隔离 PostgreSQL 为 `0070_voice_reply_projection|1|3|4|8|0|1`；媒体上传文件和哨兵 SHA 分别为 `2c8772be6e1d925eded3b93729fda19d0f1cf31a3b9c8537a2a81d34921d8944` 与 `64eb1a1b0fd872f1e2da8ec690fceabd2bf36e847250eb25ab20d21656b523ef`。真实 Chromium W0 为 32/33，唯一失败项及边界见 A2。
- 更新前 `ling backup` 成功：dump SHA `434250f41cd198991c872be52c6d5e9fa8196bda41bada65a48d55b87e0c568e`，媒体归档 SHA `1f78ee7942bc927d992f64fda4bd857d68ed56c1e573cc8ac42453dc8e4f958a`，`media_state=present`、`tombstones_state=ok`。`restore-drill` 在隔离库 `ling_image_current_updater_20261118_drill` 成功恢复 `1/3/4/8/0` 条 users/windows/turns/messages/memories，并核对媒体覆盖 `1/1`，未改业务库。
- v0.0.8 实际运行的更新页没有下载线路选择控件，目标检查显示 v0.0.9 后默认经 GHCR 直连。浏览器勾选迁移风险并提交，API 返回 201，request ID `223384061f9ea72d907fa2bec221bf4f`，目标 API digest `sha256:477146f2e2989e8df50a36b6ccc4b696c1b8072e66206d07849695e709724756`。收到 201 后关闭浏览器；任务仍运行时没有该浏览器连接，随后重连查询结果。第一个轮询脚本因在未导航页面上使用相对 URL 失败；修正为先导航再轮询后，同一 request ID 显示 `succeeded`，升级后 `/v1/auth/me=200`、dashboard=200。
- 当前 updater 任务于 `2026-11-17T21:33:45Z` 开始、`21:34:19Z` 结束，用时 34 秒；日志依次记录备份、迁移、维护门禁关闭、目标 API 内网验证、恢复宿主端口及最终 digest/head 核验。最后阶段 `verify`，无 failure、warnings 或 next actions，`maintenance=released/verified=true`。进度页显示五阶段全完成、用时 34 秒、来源“直连 GHCR”；updater 回到 online/idle，`backup_gate=ok`。
- 更新后 API 与 worker 实际 RepoDigest 均为 v0.0.9 index，OCI revision 均为 `f381a8c76df43a24b60944c91e22be46501bbd8c`；updater 容器实际 digest 为 `sha256:324f211911a2f41a6538104c6c29b9a4c9eb913e4de15969fbc1b0be704c7429`，revision 同为 `f381a8c...`。PG head 为 `0078_proactive_daily_quota`，计数保持 `1/3/4/8/0/1`，媒体文件与哨兵 SHA 不变，app/db 卷名不变。env 权限均为 0600；更新后 API `.env` SHA 为 `47576eab844c1843bb51ffbb23054c0bec62274e280eae7450ac4009a979e1ee`，updater `.env` SHA 为 `46a33b234f2f72f6f05329d22527b36815e080399c353b6142e4512cc4aff51f`。
- 更新成功后，`ling status` 仍以安装时的 v0.0.8 `install-state.json` 对照实时部署，报告 8 项记录/实况差异，包括 digest、commit/version、迁移 head 和最近备份；状态文件没有自动同步。本次没有手工改写记录来消除差异。
- 上述 34 秒成功任务本身没有执行 GH-Proxy/EdgeOne/custom mirror 下载，也没有注入 busy restart、任务中断、migration/verify 故障或真实失败回滚。浏览器断开后该任务继续并成功只证明断线不会取消任务；后续独立中断实验见下节。v0.0.8 页面没有实际线路选择器。

**2026-11-18：updater 中断与旧 API 线路字段复测**

- 在独立项目 `ling-a6-interruption-20261118`（loopback `20521`）使用发布代码 checkout `f381a8c76df43a24b60944c91e22be46501bbd8c` 的安装器，固定 API/worker 到 v0.0.8 index `sha256:77a20bb9cfde183e64eb1349b773666fc673c6f4a945abe9d889451c026207f1`，更新器固定为 v0.0.9 index `sha256:324f211911a2f41a6538104c6c29b9a4c9eb913e4de15969fbc1b0be704c7429`。安装前该 Compose 项目、目录和端口均为空闲；初始迁移 head 为 `0070_voice_reply_projection`，API `/readyz=200`，更新页报告 v0.0.8 当前摘要及 v0.0.9 目标摘要，更新器 `online/idle`、protocol 1、version 1.0.0。
- API 容器 ID `b1b0672343bb4510bad049ec8e85b6a7dc2daa0462d52dd3f1349b79ac999337`、worker `1b03ef7aab7e3e8d276df2f22833c7f431d186241ee7012d549b043c34762eb2`、PostgreSQL `23d2367753501051d497d1274f10cdb3e7d62f4efdab4ddde4fee25f3a402265`；API/worker 的 `Config.Image` 与 image ID 均为指定 v0.8 摘要，状态 healthy。中断后的独立核对仍得到 API 版本/提交 `0.0.8` / `0d36dbadc4c8f688df2775e81f1e5e795006a271`、PG head `0070_voice_reply_projection`、计数 `users/windows/turns/messages/media_evidence = 1/0/0/0/0`。`.env` mode `0600`，SHA-256 `a10502163e790ccb0e0b3d87baf8ea513fb59677b8e5e2e4b86279e28b30fb09`。
- `ling backup` 退出 0，dump `ling-20261118-072216.dump` SHA-256 `ead9152c3cc72c134afce521eac3eec70a4cc96d748c7bee7710aefdb2b3c104`；manifest SHA-256 `a2bc920f127ace8547adce5af7f09474c291ee346c8781305837011d180c883f`，`tombstones_state=ok`、`media_state=absent`、`keys_env_set=true`。`ling restore-drill --db ling_a6_interruption_20261118_drill` 退出 0，恢复计数 `1/0/0/0/0` 并确认库内无媒体引用。安装、backup、drill 日志 SHA-256 分别为 `205c23622bc3773a97d40732088a35182eaca638e7ffa489d44c820d7f50b12f`、`e0763c1f4922c9ed30bec1a48c54f0c7f27e865ac9b89261041bc0647c1fe864`、`b4f98a94fe3c273fef6c98e1c5a36fcb7f3d6bdb473d53d131a012a9b4f9575a`；日志权限已收紧为 `0600`。
- 真实 HTTP 更新请求 `18ba0848f932493383755abae0d7e0df` 返回 201，目标为 v0.0.9 API digest。观察到 updater task `running/prepare` 且 `migration.attempted=false` 后，对唯一目标 updater 容器执行 `docker stop --time 0`，退出 0。显式启动 updater 后，任务进入 `needs_manual`，failure `old_service_unverified`，`still_on_old_version=null`，maintenance `unknown`；任务没有尝试迁移，旧 API/worker 和 PG 仍可用。task 记录中断时 `previous.services={}`、`previous.head=null`。
- 使用 updater 支持的 `--acknowledge-manual 18ba0848f932493383755abae0d7e0df`，退出 0；task 变为 `resolved`、updater 回到 `idle`。但已认领的 `claimed-18ba0848f932493383755abae0d7e0df.json` 仍留在请求目录（SHA-256 `954a70b1ccbcf5cf2490cb3a787e0a17756aac869e4388aeaf60ec6656d52ca6`）。随后以新 request ID `efa0ecbcacbb444b9e32a22d2fcdc336` 重试，API 返回 409 `update_in_progress`，请求目录未增加文件。task/status 文件 SHA-256 为 `6bec67c93ba558d33fb9c2705f9fd1ab4498b4599b343e65cfa740f10e603a85` / `0817c9aded7bbb329cdc8c5ae1200e716c227e02e34accc7830d0adf05f3d9f0`。代码路径上，`acknowledge_manual()` 只更新 task/status；API 的 `_image_pending_request_ids()` 仍将 `claimed-*` 视为进行中，因此支持的确认后无法提交新任务。
- 在同一 v0.8 API 上发送带 `download_source=gh_proxy` 的新请求 `9830cd494bf24bedacc72b51454a6d0d`，返回 422 `invalid_request_body`，请求目录未变化。v0.8 页面没有线路选择控件，旧 API 也不接受线路字段；未伪造稳定目标或手改请求文件。
- 清理时只对本轮两个命名 Compose 项目执行 `down --remove-orphans`，未传 `--volumes`；容器和网络均已移除，app/db volumes `ling-a6-interruption-20261118_app-data`、`ling-a6-interruption-20261118_db-data` 及受限配置、备份和请求现场保留。未操作其他 Compose 项目、`5.88` 或 NAS 生产。

**发布后状态表**

| ID | 当前结论 |
|---|---|
| A1 | 通过；tag/main、镜像摘要与隔离边界已核验 |
| A2 | 部分通过；多组三十四项浏览器套件通过，冻结范围尚未完整覆盖 |
| A3 | 部分通过但整体项未通过；源码部署与数据保留验证成功，v0.0.8 README 升级命令因该 tag 缺 `deploy/upgrade.sh` 退出 127，首装需额外准备前端产物 |
| A4 | 通过；release workflow 全部成功，保留同 SHA 较早一次间歇失败 |
| A5 | 通过；`v0.0.9` 双平台摘要、OCI revision 与运行 API RepoDigest 匹配 |
| A6 | 部分通过且恢复门禁未通过；直连更新、进度/日志/浏览器断线后重连已验；prepare 阶段中断后进入 `needs_manual`，支持确认后 stale `claimed` 请求仍使新任务 409，重试恢复失败；其他故障矩阵未验，`ling status` 安装记录未同步 |
| A7 | 部分通过；v0.8 稳定 API 经 GHCR 直连升 v0.9 成功；v0.8 UI 无线路选择，v0.8 API 对 `download_source` 返回 422，加速线路未实测 |
| A8 | 通过；文档与发布后状态对齐，完整测试/扫描结果见记录末尾 |

**当前发布验收与更新通道签核：No-Go。** `v0.0.9` 已发布，GHCR 直连一跳成功；但固定 v0.0.8 源码 checkout 缺少 README 要求的 `deploy/upgrade.sh`，在 5.21 上直接首装又缺 `pnpm`，所以“一行首装/升级”判据未通过。更新线路被 v0.8 API 拒绝；中断恢复在 `prepare` 阶段进入人工门禁，支持确认后仍因遗留 claimed 请求无法重试。加速线路、其他忙时/故障矩阵及关系/日程冻结范围仍未完整覆盖。此判定不否认 Release、镜像身份或成功的一跳直连实测。

**A8：文档与本地验证**

- 本地全量 Python pytest 以发布代码 SHA `f381a8c76df43a24b60944c91e22be46501bbd8c` 运行，持有 `/tmp/ling_test_lock.sh` 原子锁，`LING_TEST_DATABASE_URL` 与 `LING_DATABASE_URL` 均指向隔离 `ling_test`；退出码 0、进度 100%。`-q` 日志没有最终用例计数，不推算 passed/skipped/xfail 总数。日志：`/tmp/ling-pytest-release-docs-20261118.log`。仅出现 Starlette/httpx、Alembic `path_separator` 与 multiprocessing fork 的 deprecation warnings。
- 文档修改前固定的关键源文件 SHA-256：`clients/web-console/src/connector-instances.browser.test.tsx=a5179d63d5d60051e6fe63fecfde79056b7c4430624fb17ecf701bc13c9ee975`；`clients/web-console/src/components/ConnectorsPage.tsx=92f83aa93029a0132f25b7c3a1bceba050b8dd1ad7ce5f54f747298662ea91ec`；`deploy/cli/ling.py=0e19469b63015ac4bb6ff83a98f9bed5814eaf560ffcf9602c798a49d3926f2f`；`tests/test_cli_install.py=5458e249b57f02444f8bbd93391c1832942ce4af59906653413ff21f3587e243`；`deploy/install.sh=bdafce6f36a9f7485c72f266f34a3f9afcf1d381eca1c400b6b8f19f055cd661`；`deploy/upgrade.sh=2567627ee95f92d18ed13a193d01e471fbe80a2086e262751dc10c4e6ee53623`；`deploy/compose.yaml=b8ad9834592dbec57eea272b7c73d1a7eb955fea64d2e41db05de9949b25fb10`。全量运行前后工作区均只涉及文档，源代码与测试未改。
- `git diff --check` 通过；修改文档的编码完整性和敏感内容扫描均通过。
- 本轮涉及的 W0 日志 SHA-256：v0.8 源码 `60ddb75f353581ed0487a697ff2c7916f405ead842f71df4318136b7f14d8a69`；候选升级后源码 `098dbd69619bbe96e8d3dd1d7e5c5ac4b320b6ca916c4b73c21c115a661b77c3`；v0.8 bootstrap `683b115a5378b2d863bd02abb055c75a852b51ff084f68dce6292f95c96b3bc6`；v0.8 镜像 `a2b4c2e36353bc0a11e7f5494d2bd2c72cbf629a11aeaa1b7d4127ab2223a88d`。当前 updater 进度截图 SHA-256：`c819cf65a54766314513444f4c64dfa22896fc4505300c9dbbb35b2052af5874`。
- 本轮及交接指定的 `ling-src-release009-20261118`、`ling-src-bootstrap-release009-20261118`、`ling-src-one-line-20261118`、`ling-src-upgrade-20261118`、`ling-image-current-updater-20261118` 与 `ling-image-accept-009-cli-20261117` 项目容器/updater 均已停止；这些项目的 app/db volumes 保留。旧安装记录、源码 checkout 和备份产物仍在隔离目录供复核。更早的 `ling-image-candidate-ab0ddf4-20261117`、`ling-accept-*`、`ling-auth-accept-*` 项目在本轮开始前已运行，本轮未改动。验收 SSH 隧道已关闭；未连接 `5.88` 或 NAS 生产。
