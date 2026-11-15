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
