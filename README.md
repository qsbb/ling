# ling

**一个可自托管的 AI 角色系统。** 角色有持续的人格、记忆、情绪、关系和日程，会根据当下的情境决定是否回复，也能在策略允许时主动联系你。

角色独立于聊天平台运行，不依赖第三方机器人框架。你可以直接在 Web 控制台里跟她聊天，也可以接入 QQ 官方机器人或 OneBot（如 NapCat）。

## 快速开始

在 Linux 宿主机准备 Git、Bash、Python 3、curl、Docker Engine 和 Docker Compose v2。以下命令会在当前目录新建 `ling`，生成配置、构建前端和 API 镜像，并启动服务。没有 pnpm 时，前端在 Docker 中构建。

```bash
git clone --depth 1 https://github.com/qsbb/ling.git ling && cd ling && bash deploy/install.sh
```

1. 打开安装结束时显示的控制台地址。全新源码安装的宿主端口默认是 `18080`。
2. 用默认账号 `ling` 登录。通过 `bash deploy/install.sh --logs api` 查看随机初始密码；首次登录后须改密。不要分享启动日志里的口令。
3. 在「设置 → 供应商」添加模型服务并保存密钥，为 `dialogue.fast` 绑定模型，最后发布设置。在 Web 聊天页发一条消息确认配置，之后再按需[接入 QQ](docs/DEPLOYMENT.md#6-接入-qqonebot-v11)。

想部署已经构建的镜像、不在宿主构建源码？走[镜像模式](docs/DEPLOYMENT.md#11-镜像模式部署推荐)。两种模式的命令不能混用。

## 升级与运维

**源码模式**：先[备份数据与配置](docs/UPDATES.md#7-源码模式怎么升级人工路径)，再从原安装目录执行：

```bash
bash deploy/upgrade.sh
```

升级脚本只接受干净工作区和 Git 快进更新。沿用原 Compose 项目名和部署参数时，它会继续使用已有的 `deploy/.env` 与数据卷。仅重建当前源码用 `bash deploy/install.sh`，它不拉取代码。旧版 `v0.0.8` 没有 `upgrade.sh`，请按[旧检出升级步骤](docs/UPDATES.md#从-v008-源码检出升级)操作。

| 要做什么 | 源码模式命令 |
| --- | --- |
| 查看状态和实际端口 | `bash deploy/install.sh --status` |
| 查看 API 日志 | `bash deploy/install.sh --logs api` |
| 停止服务并保留数据 | `bash deploy/install.sh --down` |

**镜像模式**：使用[按摘要发布流程](docs/DEPLOYMENT.md#113-镜像模式的升级)。网页自动更新默认关闭；隔离环境已记录部分成功路径，但中断恢复、下载线路和故障场景尚未完成整体验收。见[更新指南](docs/UPDATES.md)及[发布验收记录](docs/evidence/release-0.0.9-acceptance.md)。

## 能做什么

- **对话**：独立会话、流式回复、停止与重试；管理员可在 Web 控制台查看运行状态。
- **记忆**：区分用户陈述、外部观察与模型推断；支持更正和撤回，私聊记忆不会自动进入群聊。
- **角色状态**：人格、关系和日程会影响回复意愿；主动联系受勿扰时段、频率及渠道能力限制。
- **渠道**：QQ 官方机器人使用开放平台；OneBot 需要单独运行兼容端。账号标识与权限配置见[部署手册](docs/DEPLOYMENT.md#6-接入-qqonebot-v11)。

## 开发与文档

| 想了解什么 | 从这里开始 |
| --- | --- |
| 本地开发、测试和贡献 | [贡献指南](CONTRIBUTING.md) |
| 功能与使用边界 | [功能说明](docs/FEATURES.md) |
| 部署、备份、恢复和排障 | [部署手册](docs/DEPLOYMENT.md) |
| 配置、API、架构和验收记录 | [文档索引](docs/README.md) |

后端位于 `src/ling/`：`kernel/` 编排对话，`domains/` 管理业务状态，`api/` 提供 HTTP 接口；`connectors/` 负责渠道接入，`clients/` 包含 React Web 控制台与 Flutter 客户端。

## 隐私与许可

业务数据存放在自管部署中，但使用外部模型、语音服务或聊天渠道时，相应请求内容仍会发给所配置的服务。不要把初始口令、`.env`、根密钥或原始备份公开。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。

项目使用 [AGPL-3.0-or-later](LICENSE)。其他授权选项见[许可说明](docs/LICENSING.md)。
