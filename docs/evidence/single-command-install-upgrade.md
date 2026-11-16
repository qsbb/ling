# 单行安装与升级验收

日期：2026-11-16

## 被测版本快照

测试前记录如下，未提交工作区是本轮开发状态，不代表干净 checkout。

```text
$ git log -1 --oneline
5f55ab9 style(migrations): 排序模型导入

$ git status --short
 M .dockerignore
 M README.md
 M deploy/Dockerfile.api
 M deploy/compose.yaml
 M deploy/env.example
 M deploy/install.sh
 M deploy/web_manifest.py
 M docs/DEPLOYMENT.md
 M docs/UPDATES.md
 M tests/test_deploy_install.py
 M tests/test_deploy_update_wiring.py
?? deploy/configure_apt_mirror.py
?? deploy/upgrade.sh
?? docs/evidence/single-command-install-upgrade.md
?? tests/test_deploy_bootstrap.py

SHA-256
deploy/install.sh: d5a5517945b641ebdd9800efd4ddf4d9d6482aeb70fa978c30e6fe4f6b8ae935
deploy/upgrade.sh: 2567627ee95f92d18ed13a193d01e471fbe80a2086e262751dc10c4e6ee53623
tests/test_deploy_install.py: 895d16edc3c45f1598185c0aa40d8dad846d1969411b08404e6a345f718798d4
tests/test_deploy_update_wiring.py: 9cc07d201ec4a4cba1ae185ec8c7235cb480617ec3e79c6aaff8ec5eceded0f7
README.md: 23b2220d863514f6031b35db304bef9f14d68442c412936636c356a0e6f96916
```

## 真实隔离部署

在隔离 Linux Docker 主机执行了首次安装、一次快进升级和一次无新提交的重复升级。测试源码来自隔离 Git bare remote，升级提交 `5b0f99a` 是仅用于验收的空提交，没有推送到项目正式远端。测试主机地址按文档约定脱敏为 `192.0.2.21`。

- 首装按单条 shell 命令克隆并调用 `deploy/install.sh`，指定独立项目名与端口。`/readyz` 返回 200，api、worker、PostgreSQL 均健康。
- `deploy/upgrade.sh` 快进到 `5b0f99a` 后重新部署，`/readyz` 返回 200，容器健康检查通过。
- 重复运行升级脚本返回 0，服务保持健康。
- `deploy/.env` 升级前后 SHA-256 均为 `782fd115268749ade35b9a825fb0e4100648ff1d33c6dc2e5ab1c9aad69357b0`。
- PostgreSQL 卷始终为 `ling-val-one-line_db-data`。升级前写入的 `dbacceptance` 标记在一次和重复升级后均可读。

本次仅验证源码模式的首装、升级和配置/数据保留，没有执行备份恢复或回滚，也没有启用网页自动更新。

## 发现与修复

1. 本机首次测试时 PostgreSQL 服务未启动，全量测试无法连接 `ling_test`。启动仓库的 `scripts/dev_pg.sh` 后重跑通过，测试 URL 显式指向本机隔离库。
2. 5.21 测试主机没有 pnpm。默认安装器已改为从 Dockerfile 的 `web` 阶段构建并导出前端，临时容器与镜像会在成功和失败路径清理。
3. 干净检出没有前端 `dist`。Compose 将空宿主目录挂载到 `/app/web`，遮住镜像内的前端文件，API 因缺少 `/app/web/assets` 在启动时退出。安装器现在会在宿主目录缺少 `index.html` 或 `assets` 时，先发布已校验的前端产物，再启动 API。已有可用前端的升级仍等 API 就绪后再发布。
4. 初次空挂载问题修复后，首次生成 `deploy/.env` 的未加引号 heredoc 又把注释中的 `` `ports` `` 当作 shell 命令执行并报 `command not found`。注释改为普通文本，安装回归测试检查 stderr 不再出现该错误。
5. 默认 Debian 源的 FFmpeg 依赖安装很慢。提供可选 APT 镜像主机配置后，隔离构建用阿里云源下载约 133 MB 耗时 12 秒。先前同量下载耗时 45 分 34 秒，来源于此前测试记录。

## 回归结果

部署安装、bootstrap 和更新接线测试共 165 条通过，用时 87.11 秒。随后全量测试为 6590 passed、14 skipped、28 deselected、3 xfailed，耗时 2336.58 秒。测试使用本机隔离 PostgreSQL `ling_test`，同时设置了 `LING_TEST_DATABASE_URL` 和 `LING_DATABASE_URL`。

安装入口测试还做了两项变异验证。

- 暂时禁用首次安装的前端预发布逻辑后，测试因 `compose up` 时缺少静态入口失败；恢复后 `deploy/install.sh` 的 SHA-256 回到 `d5a5517945b641ebdd9800efd4ddf4d9d6482aeb70fa978c30e6fe4f6b8ae935`。
- 暂时把 heredoc 注释恢复成 `` `ports` `` 后，测试因 stderr 出现 `command not found` 失败；恢复后脚本 SHA-256 与同一基线一致。

两次变异后 `tests/test_deploy_install.py` 的 SHA-256 均为 `895d16edc3c45f1598185c0aa40d8dad846d1969411b08404e6a345f718798d4`，`git status --short` 与变异前列出的文件一致。

## 2026-11-16 5.21 容器与浏览器补测

测试实例使用独立 Compose 项目 `ling-e2e-20261116` 和端口 `20481`。首次安装前删除了临时检出里的 `.env`，从 `85fb0b4` 执行 `deploy/install.sh`，由安装器生成配置并经 Dockerfile 的 web 阶段构建前端；`/readyz` 返回 200。测试额外注入 `LING_TEST_MODE=1` 与临时管理员口令，回复由 fake provider 生成。

随后用相同项目、端口和数据卷从 `85fb0b4` 部署到 `d7e8e676`，并从前一提交建立测试分支执行 `deploy/upgrade.sh`，验证其从本地测试 bundle 快进到 `d7e8e676`。各次安装均完成 `/readyz` 检查。`.env` 权限为 `600`，升级前后 SHA-256 均为 `d491d51f9f07c06a6d0188a8dcf860ee3685cbd15ef708e84839b6c40ddebe52`；PostgreSQL 卷保持为 `ling-e2e-20261116_db-data`。首次浏览器实测后，用户 1、会话 2、对话轮次 4、消息 8；直接升级后这四项计数相同。

用 Playwright 1.63.0 和 Chromium 153 通过 SSH 本地转发运行 `tests/e2e/w0_browser.mjs`。首次安装后与直接升级后各通过 34/34。`deploy/upgrade.sh` 部署后有一次 28/34，其中总览登录与数据断言失败；同一时间段 API 日志观察到 `/v1/auth/login` 返回 200 和 `/v1/dashboard` 返回 401。单独浏览器复现时登录 cookie 存在且总览返回 200，完整复跑通过 34/34。此次 401 的原因未定位，记录为一次未稳定复现的观测，不能当作已修复。

这组浏览器测试覆盖登录、对话流式输出、停止、失败重试、历史恢复、设置、账号、总览和手机视口。测试模式使用 fake provider，不代表真实模型质量，也没有验证 STT/ASR。收尾已移除该 Compose 项目、容器、数据库和应用卷及生成的 `.env`；测试端口已关闭，原有容器仍在运行。

### 2026-11-16 登录会话竞态后续定位

后续候选提交 `c86c84d` 增加了 `tests/integration/test_login_session_commit_order.py`。在本地隔离 PostgreSQL 回归中，测试于登录响应刚发出、请求依赖尚未结束时立即携带响应 cookie 请求 `/v1/dashboard`；修复前得到 401，登录路由在返回响应前提交会话后得到 200。认证相关 22 项定向测试通过。

这证明当前应用事务时序可以复现该类竞态，并覆盖了修复行为；它不能证明 5.21 上一次浏览器失败必然由同一原因导致，也不代表 `c86c84d` 已在 Docker 容器或真实浏览器中复验。上文 34/34 的浏览器结果仍属于 `d7e8e676`，不能归到该候选提交。

### 2026-11-16 `c86c84d` 后续容器与浏览器补证

以下补测更新上段的“尚未在 Docker 容器或真实浏览器中复验”状态。结论按证据来源区分；历史的 34/34 和此前隔离安装/升级结果仍按其各自版本与范围解释。

| 验收项 | 结果 | 能证明 / 不能证明 |
|---|---|---|
| 单行源码首装、快进升级、重复升级 | 历史演练通过，非候选验收 | 本文前段所列源码快照在隔离 PostgreSQL 部署中保留 `.env` 与数据库卷；没有证据把这些运行绑定到 `c86c84d` 的安装脚本，不能计作候选验收，也不是已发布镜像的网页更新验收。 |
| 登录会话提交时序 | 通过；父提交变异失败 | 当前候选回归测试在 `c86c84d` 通过；同一测试复制到父提交 `e8cbab3` 后，登录为 200、紧随其后的总览为 401。该反例证明测试能区分修复前后，不证明旧浏览器故障必由此根因造成。 |
| 5.21 loopback 服务浏览器观测 | 通过，范围有限 | Chromium 对 loopback 转发的测试服务实测：`/readyz` 200，`/v1/version` 自报版本 `0.0.8`、提交 `c86c84d1a529d39416af1082e5139f9619f80e40`；登录后 61 ms 的总览请求为 200。两个标签页上 48 个 `/v1/auth/me` 与 `/v1/dashboard` 只读请求全部 200；刷新及第二标签页复用成功，前后 cookie 指纹一致。保留的日志没有请求发起时间或可重放的发起脚本，故不能证明这 48 个请求确实重叠；也不能证明服务重启后的 cookie 连续性，或把自报提交绑定到已检查的容器 ID / 镜像摘要。 |
| 候选容器身份与普通重启 | 操作者提供的快照，未独立复核 | 操作者提供的 `docker inspect` 显示 api/worker OCI revision 为 `c86c84d`；本地 image ID 分别为 `sha256:70ece4423f704629e3720a58cd92f916859468818d97a4dde5f2b7dcaa44d62d` 与 `sha256:2f7372873bfc339308e5ee21a8df189d3473881b7cf9041a11798cf7e87c167f`。操作者提供的 10:21:12 至 10:22:05 +0800 重启前后快照显示 api/worker 重启，容器与镜像 ID、卷名、`.env` SHA-256 `bfc33cfa3be0e7176bed514a50bb3ae8e518919b07989aa7cc9639c19491791c` 及 PostgreSQL 计数 `users/windows/turns/messages = 1/3/8/16` 未变，健康检查正常。原始 inspect/重启输出未落入本仓库；这些信息不能把浏览器观测绑定到该镜像，也没有测重启后的已登录 cookie、更新器中断恢复或升级期间的数据迁移。镜像 ID 不是 GHCR 多架构 index digest。 |
| 更新页 mock 浏览器套件 | 模拟套件通过，636/636 | 桌面与移动视口覆盖离线/恢复、POST 响应丢失、任务恢复/轮询及人工门禁；测试对全部 `/v1/**` 使用 mock，不是服务端或 Docker 更新端到端验收。 |
| updater stub 模拟 | 本地模拟通过，16 项 | 覆盖镜像预取身份、预取失败、重复请求和迁移前后重启对账；使用本地 stub，不访问真实 Docker/GHCR，不证明真实中断恢复。 |
| GHCR 稳定发布目标 | 未通过 / 当前无目标 | 2026-11-16 的 `RegistryClient.stable_tags()` 查询日志记录到 6 个稳定 tag：`v0.0.2`、`v0.0.3`、`v0.0.4`、`v0.0.5`、`v0.0.6`、`v0.0.8`；本次查询的最高值为 `v0.0.8`，与服务当前版本相同。更新器只接受 `vX.Y.Z` 稳定 tag，忽略 `sha-*`；因此目前没有已观察到的更高版本可供网页更新与两阶段镜像升级验收。未查询/记录对应 GHCR index digest。 |

#### 可复核证据

本轮核对时工作树 HEAD 为 `327101a2acbd8bd38dde5032c0b1c7dd9976226e`，候选 `c86c84d1a529d39416af1082e5139f9619f80e40` 是其祖先。回归、updater 与浏览器测试所涉源文件在候选和 HEAD 间无差异（`git diff --quiet c86c84d1 HEAD -- <files>` 退出码 0）；SHA-256：

```text
src/ling/api/routes/auth.py
  12405cad861bdbdf0963f6f815ee22fc02f8b0fcfd592f24e56a02c1eeeb8369
tests/integration/test_login_session_commit_order.py
  7439e6feb9dab143ea05b8464f150ebbf3e8820773e1e96acd18164b066b51de
deploy/updater/updater.py
  d08a6ec184b7968f47419f40dc7983e365fb1b89ff57ad45bad8325d03ed27d6
deploy/updater/registry.py
  ffe819268b8edbff823dd997177a284838d242496a20017067ecea1aa80b2172
tests/test_updater.py
  21d5bd70033c3c73cd9341c6b3457c4e85597a2a725ca57ceb78bda2f806f1ff
tests/integration/test_version_image_update.py
  2c8497b9242892c348d9861a17affc477e6ab55c283d8dba41a09cf621de8b35
tests/e2e/update_browser.mjs
  5ee0ac016073c8f90ac6c597de4037901b8d0f9578b488d6ae9bccd363b25d89
```

本机原始日志及 SHA-256：

```text
/tmp/ling-auth-regression-c86-projectvenv.log
  423b1d0e014eb1eab96f4420f7b344c2615be505dd574b756ac884826ca74f2d
/tmp/ling-auth-regression-prefx.log
  642f69d1a03c7a7cdce576907b2a11e62cbc4ada463570b30382ab8f53f1c03e
/tmp/ling-auth-interleaved-20261116-c86.log
  cc17b81f573082288ad0d853524920d5601d6eaf54d2db7b08408fc7391beb5a
/var/tmp/ling-auth-browser-c86.log
  54dee31169577b4af07f06b9fe602d422fa2c9e2edb7f45b91737ab9f4b08e00
/var/tmp/ling-auth-browser-after-upgrade-c86.log
  7fe115307418c01b6fefae40d3f56e6316f1286050170c235be1423f9bfb44f8
/tmp/ling-update-browser-20261116-c86.log
  6f0c89a75524f813c52c74f71aed732a9e2b911307963383b1f07852c6e653af
/tmp/ling-image-updater-sim-20261116-c86-pinned.log
  2a4b2ab6df93923c3fa1eba9505d1924cdf7851b747e40336cf5a1d4c1b6a56d
/tmp/ling-ghcr-tags-20261116.log
  1bad1248ac56013089d74e20b6da95a20ce319a451c4956cce56be1cde079514
```

`/var/tmp/ling-auth-browser-c86.log` 与 `...after-upgrade-c86.log` 各记录 34/34，但使用 fake provider；日志没有 HTTP 状态或镜像 digest，故只作辅助 UI 证据。`tests/e2e/update_browser.mjs` 的 `attachMocks()` 将所有 `/v1/**` 路由交由 mock 处理。

本轮只读 tag 查询由 `deploy/updater/registry.py` 的 `RegistryClient` 执行。`gh api /users/qsbb/packages/container/ling-api/versions` 因当前 token 缺少 `read:packages` 返回 403；该失败不影响前述匿名 registry 客户端结果，也不作为版本清单证据。

#### Go / No-Go

**当时结论（`c86c84d`）：No-Go。** 登录修复的本地回归与 loopback 服务浏览器观测符合预期，但浏览器结果没有绑定到 OCI digest；当时没有高于 `0.0.8` 的稳定 GHCR 目标，因而真实镜像升级路径不可执行。历史源码单行安装/升级不计作该候选验收；操作者提供的普通容器重启快照也不足以代替下列实测。

当时待定的版本决策已由用户指定为 `0.0.9`。版本选择本身不授权推送、打 tag 或发布；候选进入 updater 固定 GHCR 源后，真实镜像更新验收才能开始。下列门槛按本文后续补测结果逐项更新：

- 归档原始 Docker inspect/重启输出，并用容器 ID、OCI revision、GHCR index/platform digest 将浏览器所测 API/worker 服务绑定到确切镜像；另外记录 updater 容器 ID，并核对其运行镜像的 GHCR index/platform digest。
- 将候选提交固定在干净环境，按该候选 README 原文执行源码模式的一行命令首次安装与升级；记录 README commit/SHA、实际执行的完整命令、安装提交，以及升级起始提交和目标提交，并记录相关脚本哈希。以隔离 PostgreSQL 核验配置、数据库及媒体数据在成功与失败路径后的保留/恢复。此项与镜像模式验收分开签核。
- 在隔离 PostgreSQL 部署中用真实发布镜像完成首装及至少两次连续网页镜像升级，分别覆盖 GHCR 直连和至少一条加速线路，逐项核验目标摘要、实际运行摘要、版本、配置、PostgreSQL 与媒体数据。
- 记录各更新阶段耗时、成功/失败文案与诊断；验证并发/重复请求、浏览器断线重连、更新器重启，以及中断后真实状态对账。
- 在容器切换/重启前后复验登录 cookie 与会话；成功、失败和中断路径都核对 PostgreSQL、媒体数据、配置及恢复状态。

## 2026-11-16 `0.0.9` 候选隔离验收

> **历史快照说明**：本节记录的是当时的代码候选 `0cfc9be`，不是当前代码候选。
> 当前本地代码候选为 `62e41c9`；本轮结果与边界见
> [`release-0.0.9-acceptance.md`](release-0.0.9-acceptance.md)。本节原始部署、数据库、
> 容器和浏览器记录仍按当时观测保留，不将其改写为当前候选实测。
> 2026-11-17 对 GHCR 的只读复核确认稳定 tag 仍最高为 `v0.0.8`，但补充核验了
> API/updater 的 index 与双平台摘要；当前候选验收记录保存了该次核验值。

本节更新上文针对 `c86c84d` 的结论。用户指定下一版本为 `0.0.9` 后，版本提交
`6910890` 与严格权限修复提交 `0cfc9beef9a22ee2b1661a68c757da402b5aaf93` 形成
当时的候选。本机及 5.21 测试检出在验收取证时均无未提交改动；`deploy/install.sh`
SHA-256 为 `bdafce6f36a9f7485c72f266f34a3f9afcf1d381eca1c400b6b8f19f055cd661`。

5.21 专用 Compose 项目 `ling-accept-009-20261116` 使用隔离 PostgreSQL 与应用卷。
普通用户（临时 Docker 组）和 root 分别在全新卷、`umask 077` 下完成源码首装，
安装命令退出码均为 0，API、worker、PostgreSQL 均 healthy，前端 `dist` 与 `assets`
目录权限为 `0755`。修复前的 `6910890` 在同一严格权限条件下首装失败；
`0cfc9be` 的测试先观察到失败，再观察到修复后的成功。`c86c84d` 到 `6910890`、
`6910890` 到 `0cfc9be` 的源码升级，以及修复版的重复安装也均以退出码 0 完成。

root 路径重复安装前后的 `.env` SHA-256 均为
`e4b9d78b9644d15df659df83f7fcd7c0e41c53f1b20aa5c370a0f6a6edb2098f`；
PostgreSQL 哨兵 `root-077-retry` 可读，媒体标记文件 SHA-256 均为
`f6d6e4c8371068994d3f9b2719100f90e8cdad1e09860d815c7d81196103a8b5`。
数据库和应用卷身份保持为 `ling-accept-009-20261116_db-data` 与
`ling-accept-009-20261116_app-data`，迁移 head 为 `0078_proactive_daily_quota`。
重复安装后用户、会话、轮次、消息计数分别为 1、2、4、8。

最终 API 容器 ID 为 `4df843cad59b7ad4c8eeade8b955ce2c566fa7e5624d739051c3f1cfa62dced6`，
本地 image ID 为 `sha256:5dc9031ef3529c80da9288c71e37a2a4741f5a43aa31c9cd07b1d51239b77441`；
worker 容器 ID 为 `bcb45f76c6755658e61b11bef5df2ab04278cd4c63ab60d6269bfe5e95689f4b`，
本地 image ID 为 `sha256:c2b85276081136b60e82e52d87660f674c28f1ce826b9791595d242a865214b8`。
两镜像的 OCI revision、容器内版本与 commit 分别对应完整 `0cfc9be`、`0.0.9` 与
完整 `0cfc9be`。这些是本地构建的镜像身份，不是 GHCR index/platform digest。

真实 Chromium 对 root 首装验证了随机口令登录（200）、强制改密页、改密（204）
及改密后的鉴权与总览（均 200）。最终候选的主要页面和对话套件为 34/34；同一
浏览器 context 在刷新、新标签及 API/worker 普通重启后，`/v1/auth/me` 与
`/v1/dashboard` 均返回 200，脱敏 cookie 指纹一致。对话使用 fake provider，
不证明真实模型能力。原始安装日志保存在测试机 `/tmp/ling-009-*.log`，可能包含
随机初始口令，不公开原文；本机浏览器日志保存在 `/tmp/ling-009-*.log`。

最终候选的本机全量 pytest 在隔离 PostgreSQL、测试锁及两个数据库 URL 同时设置下
退出码为 0。进度标记统计为 6593 passed、14 skipped、3 xfailed，日志见
`/tmp/ling-pytest-009-0cfc9be.log`。Web 串行测试为 1039 passed、1 skipped，
39/39 个文件通过；类型检查、构建、Ruff 及版本一致性检查也通过。

**整体发版验收仍为 No-Go。** `0.0.9` 尚无正式 GHCR 稳定镜像，真实网页镜像更新
的直连与加速线路、两次连续升级、更新器中断及状态对账未绑定到本候选验收。
源码模式在线业务库实际恢复与跨 schema 回退尚未实测。本轮未推送、打 tag、创建
Release 或操作生产环境。

### 备份、恢复演练与人工切换补测

验收方先固定备份产物、实际记录、配置与媒体哈希、容器身份、失败阶段及重试的判据，
再在同一隔离项目执行。在线备份退出码为 0；manifest 中 PostgreSQL dump、墓碑
与媒体文件的 SHA-256 均与产物一致，墓碑状态为 `ok`，根密钥来源为 `env`，媒体
状态为 `present`。`restore_drill.sh` 退出码为 0，演练库
`ling_009_accept_drill` 的迁移 head 为 `0078_proactive_daily_quota`，用户、会话、
轮次、消息计数为 1、2、4、8，数据库哨兵仍为 `root-077-retry`；在线服务保持 200。
停止 API/worker 后的 oneshot 备份和恢复演练也均退出码为 0，manifest 记录
`app_data_read=oneshot`，随后服务重启成功。

不存在的 Compose override 使安装器在容器变更前以退出码 1 停止；容器 ID、`.env`
指纹、数据库和媒体哨兵不变。撤销该故障后用 `--prebuilt-web` 重试，退出码为 0，
API、worker、PostgreSQL 恢复健康。

历史 `6910890` 镜像已不在测试机，验收方从该提交重新构建本地镜像并核对 OCI
revision。`git diff` 显示它与 `0cfc9be` 的业务源码、迁移、Dockerfile、Compose
及前端无差异，数据库 schema head 同为 0078。在隔离项目人工切至旧本地镜像、
再切回 `0cfc9be` 本地镜像，两个操作均退出码为 0，`/readyz`、首页、服务健康、
数据库和媒体哨兵保持正常。临时隔离测试用户的同一 Cookie 在当前镜像登录 200，
切旧及切回后请求 `/v1/auth/me`、`/v1/dashboard` 均为 200，Cookie jar 哈希不变。
该临时用户、subject、会话及口令/Cookie 临时文件已清理，最终业务计数恢复到
1、2、4、8。跨镜像会话核验使用真实 HTTP 请求，没有重做 Chromium 双标签测试。

媒体归档哨兵位于 `/data/media/acceptance/sentinel.txt`，其哈希以 `f5be91d2` 开头，
归档内校验一致；上文 `f6d6e4c8` 哈希属于媒体目录外的
`/data/acceptance/marker.txt`，单独用于核验应用卷保留。备份目录位于测试机
受限路径，含私密配置快照和原始日志，未公开原文。

这些结果证明同 schema 下的备份、演练库恢复、部署前失败后重试及本地镜像人工
切换。在线业务库实际恢复、跨 schema 迁移回退及 GHCR 正式镜像网页升级仍未验收。
