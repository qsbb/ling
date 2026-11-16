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
- GitHub API 于 2026-11-17 只读核验：`main=0d36dbadc4c8f688df2775e81f1e5e795006a271`；最新正式 Release 为 `v0.0.8`，`published_at=2026-11-13T16:48:41Z`（上海时间 2026-11-14 00:48:41），tag 解引用到同一 main SHA。`v0.0.9` tag 查询为 404；候选 SHA 对应 Actions run 数为 0。`v0.0.8` Release workflow run `43458504082` 的 verify、Python 3.12/3.13 CI、mobile、web、image、updater-image 和 release jobs 均成功；这是旧 Release 的结果，不是候选结果。
- GHCR 只读核验：`ling-api` 与 `ling-updater` 稳定 tag 清单均为 `v0.0.2`、`v0.0.3`、`v0.0.4`、`v0.0.5`、`v0.0.6`、`v0.0.8`，没有 `v0.0.9`。在 `v0.0.8` registry index 下直接读取 manifest，并以响应内容 SHA-256 复核平台 descriptor；OCI labels 的 version/revision/source 均为 `0.0.8` / `0d36dbadc4c8f688df2775e81f1e5e795006a271` / `https://github.com/qsbb/ling`：

  | 镜像 | index digest | linux/amd64 | linux/arm64 |
  |---|---|---|---|
  | `ghcr.io/qsbb/ling-api:v0.0.8` | `sha256:77a20bb9cfde183e64eb1349b773666fc673c6f4a945abe9d889451c026207f1` | `sha256:3a27a6057756525808aa552dffb4c5dde43a0bb61da37d5512795f7a9d5a5216` | `sha256:406b1b9cc8696adc557d106a272d798fedf706617fa8518acbd5179d8c3bf834` |
  | `ghcr.io/qsbb/ling-updater:v0.0.8` | `sha256:281736db4dedcb29643b01e1be52638c6a019ce7b282bb0d83895a067281fd0f` | `sha256:9dad77f6253bc55dfe959ff8bb211973e5513337ab6a57a040de59ba540f93fd` | `sha256:a30d22af9d10d6a9587e030cf7ba30b4aceacf16f9c9b917252ecde5974d9e1b` |

- Python 测试显式设置 `LING_TEST_DATABASE_URL` 与 `LING_DATABASE_URL` 为 `postgresql+asyncpg://ling@127.0.0.1:55432/ling_test`，并持有 `/tmp/ling_test_lock.sh` 提供的原子锁。测试前后只读查询均为数据库 `ling_test`、角色 `ling`、地址 `127.0.0.1:55432`；共享库 `alembic_version` 保持 `0067_connector_instances`。仓库迁移 head 为 `0078_proactive_daily_quota`；集成 fixture 使用临时 schema，迁移验收在测试自建数据库运行，本轮没有升级共享测试库。
- `docker`、`podman`、`buildah` 均不在本机 PATH。本轮没有 SSH 到测试机或连接 NAS/生产端；不以既有生产状态作为证据。

**A1 判定：通过（候选、远端状态与本地数据库隔离均有快照；不代表发布门禁通过）。**

### A2：功能范围与真实路径

| 范围 | 本轮可核验证据 | 判定边界 |
|---|---|---|
| 连接器实例详情、保存目标与实例切换 | Playwright/Chromium 专项 9/9；完整 Web 套件包含该文件。S9 将保存目标临时变异为 `default` 后，测试按预期以 `default` 对 `main` 失败；恢复后通过，测试文件 SHA 与候选 blob 一致。 | 该测试源码明确将 `/v1/**` 交给 `page.route` mock；不能证明真实 API/PG 写入、密钥清空或部署后的失败回滚。变异只证明 S9 对该错误有区分力。日志：`/tmp/ling-s9-wrong-instance-mutation.log`、`/tmp/ling-s9-restored.log`、`/tmp/ling-web-connector-acceptance.log`。 |
| 关系列表、详情、权限与大量数据/布局 | `identity.browser.test.tsx` 使用真 Chromium，造 2000 条关系，检查详情、分页、合并/拆开及 375/1440 视口。 | `/v1/**` 由 `page.route` 模拟；没有与真实 API、权限存储和 PostgreSQL 组成的端到端路径。 |
| 日程当前活动、原因、手动生成与失败重试 | 当前 Web 测试覆盖展示状态、原因文案、生成和幂等重试行为。 | UI 测试与 PostgreSQL 后端集成测试分开运行；没有候选绑定的真实浏览器/API/PG 日程验收。 |
| 更新进度、历史耗时与下载线路 | `imageUpdate.browser.test.tsx` 在真浏览器中验证界面交互；后端更新 API/状态测试另行运行。 | 浏览器 API 请求被路由模拟；没有当前候选 updater 容器与实际镜像更新链路。 |
| 登录、对话、设置及数据留存 | 本机全量 pytest、Web 与 Flutter 测试通过；历史 `0cfc9be` 隔离 Chromium 34/34 使用 fake provider。 | 历史容器、Cookie 和数据留存证据绑定 `0cfc9be`，不能替代 `62e41c9` 的部署验收；fake provider 不证明真实模型能力。 |
| 参考插件功能取舍 | 当前 `docs/UPDATES.md` 记录借鉴 AstrBot 控制台更新状态可见性，并明确不是 `git pull`。 | 冻结判据没有给出更广的参考插件功能清单或逐项取舍来源；除该已记录的更新 UI 取舍外，其余范围未能核验。 |

**A2 判定：未验。** 浏览器组件级覆盖存在，但冻结条件要求真实浏览器、真实 API 与隔离 PostgreSQL 同路径验证；本轮没有该组合运行。

### A3：源码首装与升级

当前 `deploy/install.sh` / `deploy/upgrade.sh` 只完成静态语法检查，未在 `62e41c9` 的干净 Docker 环境按候选 README 命令首装、升级或做失败恢复。历史 5.21 隔离部署结果绑定 `0cfc9be`；脚本 hash 相同不足以替代完整候选运行记录。本机没有 Docker/Compose runtime，本轮不接触远端主机。

**A3 判定：需外部动作。**

### A4：版本、Release 与 CI

- `.venv/bin/python scripts/check_release.py --tag v0.0.9` 退出码 0，三处应用版本（`pyproject.toml`、`src/ling/__init__.py`、Web `package.json`）一致为 `0.0.9`。
- `.venv/bin/python deploy/release/catalog.py validate` 退出码 0；`deploy/update-notes.json` schema 有效并含 4 个版本条目。
- 远端 `main` 没有候选提交；候选不是 `main` 祖先，候选 Actions run 为 0。因此仅通过本地版本检查和本地 CI 等价命令，不能满足 release workflow 的 tag 祖先与候选 CI 门禁。当前 `update-notes.json` 的 CHANGELOG URL 指向 `main`；候选合入后才会显示本地 0.0.9 条目。

**A4 判定：失败（候选未满足 main 祖先门禁，也没有候选 workflow 结果）。**

### A5：镜像身份

上表验证了现存 `v0.0.8` 两镜像的 registry index、双平台 manifest 与 OCI labels；GHCR 没有 `v0.0.9` tag。本候选未构建/发布到 registry，未能核验 candidate image 的 Web 清单、revision、两架构摘要或运行容器身份。旧快照本地 image ID 不替代 registry digest。

**A5 判定：需外部动作。**

### A6：真实 updater 与状态对账

候选本地 Python/Web 更新测试通过，但本机无 Docker。未运行真实 API/updater 镜像，也未验证候选绑定的摘要、任务阶段/耗时、并发与断线、升级器忙/闲重启、中断对账、迁移前后故障、maintenance gate、Cookie/配置/PostgreSQL/媒体数据前后状态。stub 和 `0cfc9be` 历史演练均不计作本项通过。

**A6 判定：需外部动作。**

### A7：稳定版网页更新

GHCR 只读查询没有 `v0.0.9` 稳定 tag 或对应 digest。未创建 tag/Release，也未在隔离镜像部署中从 `v0.0.8` 经 GHCR 直连及可达加速线路升级；没有伪造版本或放宽 updater 校验。

**A7 判定：需外部动作。**

### A8：文档与本地测试结果

README、CHANGELOG、DEPLOYMENT、UPDATES、历史隔离部署说明与本记录均区分代码候选 `62e41c9` 和部署历史快照 `0cfc9be`；`deploy/update-notes.json` 的 0.0.9 文案与候选变更范围一致。版本号未改动。文档提交后以 Git commit/blob 为准；本记录不尝试自引用自身 SHA。

本地验证命令与结果（均在不修改源代码的候选 `62e41c9` 上）：

| 检查 | 结果 / 证据 |
|---|---|
| 全量 Python pytest | 最终运行退出码 0，进度 100%；日志 `/tmp/ling-pytest-final-0.0.9-62e41c9-20261117.log`。独立 collect-only 输出 `/tmp/ling-pytest-collect-0.0.9-62e41c9-20261117.log` 按文件计 323 个文件、6610 个已选用例；pytest 进度含 14 个 skip、3 个 xfail 标记。由于 `-q` 输出未打印最终统计行，6593 passed 是 `6610 - 14 - 3` 的推算，不是原始 summary。 |
| 文档/部署定向回归 | `tests/test_deploy_install.py`、`tests/test_deploy_update_wiring.py`、`tests/test_update_executor.py` 退出码 0、进度 100%；日志 `/tmp/ling-pytest-doc-targets-final-0.0.9-62e41c9-20261117.log`。 |
| Web 真浏览器全套 | 39 个文件通过，1039 passed、1 skipped；`/tmp/ling-web-all-tests-0.0.9-62e41c9.log`。运行前后测试文件 SHA 为 `0ed05d809bcf044adbfee3a7d19ab6310fc9a4e723b00c1da2ba0b1dd2b4c502`。浏览器真，但按各测试定义使用 API route mock。 |
| Web 类型检查与生产构建 | `pnpm build` 退出码 0（含 `tsc --noEmit`）；Vite 有一个非阻断的大 chunk 提示。日志 `/tmp/ling-web-build-0.0.9-62e41c9.log`。 |
| Flutter | `flutter analyze --no-pub` 无问题；3 个测试通过。日志 `/tmp/ling-mobile-0.0.9-62e41c9-20261117.log`。 |
| 静态及发布本地门禁 | Ruff 通过；MyPy 234 个源文件无问题；py_compile、部署脚本 bash -n、版本一致性和 release catalog 校验均退出码 0。日志位于 `/tmp/ling-{ruff,mypy,pycompile,bash-syntax,version-check,release-catalog}-0.0.9-62e41c9-20261117.log`。 |
| 文档完整性 | `git diff --check` 无输出；`src/`、`tests/`、`docs/` 无 U+FFFD；对本轮涉及的六份文档单独扫描规则指定的内网地址、密钥样式和真实账号模式，均无命中。仓库全量敏感词扫描有历史假密钥样例和 `AGENTS.md` 规则示例命中，均非本轮新增。 |

**A8 判定：通过（文档与本地证据范围一致；不代表发布门禁通过）。**

### 最终判定

| ID | 结论 |
|---|---|
| A1 | 通过（快照和本机隔离范围） |
| A2 | 未验（缺真实浏览器/API/PG 集成路径） |
| A3 | 需外部动作（无本机 Docker runtime） |
| A4 | 失败（未合入 main、无候选 CI） |
| A5 | 需外部动作（无候选 GHCR 镜像） |
| A6 | 需外部动作（未运行候选真实 updater 容器） |
| A7 | 需外部动作（无稳定 tag，未跑真实网页升级） |
| A8 | 通过（候选/历史范围已区分，No-Go 明确） |

**整体 Go/No-Go：No-Go。** 版本字符串正确、旧稳定镜像身份可核验、本地隔离测试通过，都不能替代 main 祖先、当前候选镜像和真实部署更新门禁。

**唯一所需外部动作：**若要解除 No-Go，由有权限的维护者明确授权一次非生产发布验收窗口：候选经 PR 合入 `main` 并运行 release workflow，同时提供隔离 Linux Docker/PostgreSQL 环境完成候选源码安装、真实镜像/updater 与 GHCR 稳定版网页升级验收。当前明确禁止推送、打 tag、创建 Release；本轮没有执行这些动作，也没有访问 NAS 生产端。
