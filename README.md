# ling

一个**独立运行的拟人化 AI 角色系统**。

她不是一个等待指令的聊天机器人。她有自己的账号、日程、情绪、关系、记忆和主动性——
会自己判断什么时候想说话、什么时候在忙、什么时候只是懒得回。你和她说话，是和一个
持续存在的人说话，而不是调用一个接口。

QQ、微信、Web 控制台、移动端都只是**连接到她的渠道**，她本身不依赖任何第三方机器人框架
运行时。

```
                    ┌─────────────┐
   QQ 官方机器人 ───┤             │
   OneBot/NapCat ───┤    ling     ├─── 人格 · 记忆 · 情绪 · 关系
   Web 控制台   ───┤   (她本人)   │    日程 · 主动性 · 模型路由
   移动端       ───┤             │
                    └─────────────┘
```

## 特性

**她是一个人，不是一个工具**

- **自主判断**：要不要接话、什么时候接、说几句，由她按当时的处境（在忙吗、累不累、
  和对方熟不熟）自己决定，不是关键词触发
- **主动联系**：到点想找你、想起昨天你提过的事，会自己开口；被勿扰时段和频率上限门控
- **情绪与关系**：情绪影响的是**回复意愿和简短程度**，怎么表达由人格决定，不写死在代码里
- **记忆**：短期/长期/核心记忆分层，会遗忘、会撤回、会修正，来源可追溯
- **日程与生活**：有自己要做的活动和精力状态，不是随时待命

**工程上的坚持**

- **不假装送达**：没有平台回执就不记成功；失败分四类（可重试/不确定/被拒/不可达），
  不确定的去对账而不是重发
- **不冒充事实**：模型推断出的内容会标注来源，不当作确定事实写进记忆
- **契约优先**：13 份 v1 schema 集中在 `contracts/`，改动先改契约
- **单用户可部署**：Docker Compose 一套起完，不需要 Kubernetes

## 快速开始

### Docker（推荐）

需要 Git、Docker Engine 和 Docker Compose v2。整行执行后会在当前目录创建 `ling`，
生成本机配置、构建镜像并启动服务；没有预装 pnpm 也可以安装。

```bash
git clone --depth 1 https://github.com/qsbb/ling.git ling && cd ling && bash deploy/install.sh
```

首次启动会生成密钥和管理员账号。安装成功后显示控制台地址与用户名，随机初始密码
只写入 API 启动日志，可用下面的日志命令查看；首次登录后需要修改密码。

```bash
bash deploy/upgrade.sh                # 拉取当前分支快进更新并部署
bash deploy/install.sh --status      # 查看状态
bash deploy/install.sh --logs api    # 查看 API 日志和首次登录密码
bash deploy/install.sh               # 仅重新部署当前检出，不拉取代码
bash deploy/install.sh --down        # 停止服务，保留数据
```

`deploy/upgrade.sh` 适用于源码安装，会获取当前分支远端并只接受快进更新，再调用标准安装
流程；工作区有未提交改动时会停止。已有 `deploy/.env` 和 PostgreSQL 数据卷会保留。
镜像模式不使用这个脚本。网页自动更新默认关闭（`LING_UPDATE_ENABLED=0`），首次启用自动
更新仍需按 [docs/UPDATES.md](docs/UPDATES.md) 完成宿主纳管。

需要回滚保障时，升级前另行备份数据库和 `deploy/.env`。默认管理员名为 `ling`，可在首装
时用 `--admin-user <名字>` 修改。

### 本地开发

前置：`uv`（≥0.5）、`pnpm`（≥9）、`python3.11`（`scripts/dev_pg.sh` 用它跑便携
PostgreSQL——`pgserver` 目前没有 3.13 的 wheel，所以 PostgreSQL 和运行 ling 的
Python 是两份不同的解释器）。

数据库地址需要显式配置。此前使用本地 SQLite 的用户应先备份旧文件及相关密钥，
确认数据迁移与回退方案后再切换。下面的 PostgreSQL 示例不会自动导入旧数据。
回退时保留旧版本、原数据库文件及其密钥，并恢复原有数据库配置。

```bash
uv venv --python 3.13 .venv && . .venv/bin/activate
uv pip install -e ".[dev]"

scripts/dev_pg.sh start                      # 便携 PostgreSQL，端口 55432（内部用 3.11）
cd clients/web-console && pnpm install && pnpm build && cd ../..

# 请先确认连接的是需要使用的数据库。
export LING_DATABASE_URL='postgresql+asyncpg://ling@127.0.0.1:55432/ling'
. scripts/dev_env.sh
python -m ling                               # → http://127.0.0.1:8080
```

连接地址支持 `postgresql://` 和 `postgresql+asyncpg://`。运行时使用 asyncpg 驱动，其他驱动格式会在启动前拒绝。

默认 `LING_TEST_MODE=1`：对话由确定性 fake provider 生成，界面会固定显示测试模式提示。
**测试模式不会调用真实模型**，用于开发与回归。

## 接入 QQ

两条路，按需要选：

| 方式 | 适合 | 特点 |
|---|---|---|
| **QQ 官方机器人** | 长期使用、合规 | 走开放平台，WebSocket 网关，**不需要公网地址、不需要扫码** |
| **OneBot（NapCat 等）** | 用自己的号 | 需要额外的协议实现端，能力更全但有封号风险 |

两者共用同一套入站受理（白名单、去重、身份、决策、投递），差别只在协议层。
详细步骤见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

## 配置

所有配置走环境变量，完整清单见 [`deploy/env.example`](deploy/env.example)。几个关键的：

| 变量 | 作用 |
|---|---|
| `LING_ACCESS_MODE` / `LING_ACCESS_LIST` | 谁能跟她说话。`off` / `whitelist` / `blacklist` |
| `LING_USER_TIMEZONE` | 勿扰时段、每日限额按这个时区算 |
| `LING_QQBOT_*` | QQ 官方机器人凭据与开关 |
| `LING_ONEBOT_*` | OneBot 渠道连接 |
| `LING_LOG_LEVEL` | 日志级别 |
| `LING_LOG_BUFFER_SIZE` | 界面「日志」页能看的最近条数（内存环形缓冲），默认 500 |
| `LING_LOG_ACCESS_DENIALS` | 白名单拒绝是否逐条写日志，默认开启。关掉只少日志，拒绝与计数照旧（设置页也可改，发布后即生效） |
| `LING_UPDATE_ENABLED` | 自助更新开关，默认 `0`（关闭）。打开前必须先由宿主人工纳管执行器，见 [UPDATES](docs/UPDATES.md) |

> 渠道的发送者标识是 **openid**（官方机器人）或 **QQ 号**（OneBot），两者不通用。
> 加白名单时注意用对的那种。详见 [身份设计](docs/IDENTITY-MULTIUSER-DESIGN.md)。

## 界面

登录后先看**总览**：她此刻在做什么、情绪如何、有没有想主动找你（以及为什么
没发出去）、最近记住了什么、渠道与模型通不通。每块数据都标了更新时间和状态
（正常 / 确实为空 / 读不到），不会把「她没在做什么」和「读不到她在做什么」
画成同一个样子。

聊天、生活、人格、记忆、身份、设置按功能分组在顶栏与设置中心。

## 架构

```
src/ling/
  api/           HTTP 接口与鉴权
  domains/       20 个领域：会话/记忆/情绪/关系/日程/主动性/身份/人格/…
  kernel/        回合编排、上下文构造、入站受理
  capabilities/  投递、表达、渠道能力、模型路由
  contracts/     v1 契约（Pydantic 单一真源）
  ports/         外部依赖接口
connectors/      onebot / qqbot / qq / wechat / sdk
clients/         web-console（React 19）、mobile（Flutter）
contracts/       导出的 JSON Schema
```

设计文档与决策记录：

- [功能清单](docs/FEATURES.md)
- [API 契约](docs/API-WEB-V1.md)
- [设置分类](docs/SETTINGS-TAXONOMY.md)
- [架构决策记录 (ADR)](docs/adr/)
- [升级与自动更新](docs/UPDATES.md)（默认关闭，未在真实 Docker 上演练）
- [注意力设计](docs/ATTENTION-DESIGN.md) · [自主性设计](docs/AUTONOMY-DESIGN.md) · [群聊轮询](docs/GROUP-POLLING-DESIGN.md)

## 开发

```bash
pytest tests                      # 全量
ruff check src tests connectors   # 静态检查
mypy src connectors               # 类型检查
cd clients/web-console && pnpm typecheck && pnpm test && pnpm build
```

提交请遵循 [Conventional Commits](https://www.conventionalcommits.org/)，
首行不超过 50 字符。详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

推送说明：本项目在部分网络环境下 `git push` 到 github.com 不通，
可用 `python scripts/gh_push.py` 走 GitHub Git Data API 推送。

## 状态

功能已实现并有测试覆盖，但**仍在演进中**。数据库 schema、配置项和部分 API
可能变动。生产使用前请先读 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) 的注意事项一节。

发布与升级：正式 Release `v0.0.8` 于 2026-11-13 发布。当前工作树版本字段仍为
`0.0.8`，但候选 `c86c84d` 比该 Release 提交多 37 个提交，并非已发布镜像的源码快照。
源码模式的一行首装、升级、重复升级及配置/PostgreSQL 卷保留已在隔离 Docker 项目验证，
测试提交截至 `d7e8e676`；当前候选后续的认证时序修复只完成了本地 PostgreSQL 回归，尚未
在容器中复验。源码模式备份恢复/回滚和当前候选的镜像网页更新验收仍未完成；自助更新默认
关闭。详细边界见 [docs/UPDATES.md](docs/UPDATES.md)。

## 安全与隐私

这是个会长期记录你说话方式、生活细节和情绪状态的系统，**部署在你自己的机器上**，
数据不出你的服务器。发现安全问题请走 [SECURITY.md](SECURITY.md) 的私下渠道，
不要开 public issue。

## 许可

[GNU AGPL-3.0-or-later](LICENSE)。

你可以自由使用、修改、部署，但如果把它作为网络服务提供给别人，
**必须公开你的修改**。想闭源商用需要另行取得授权——见 [许可说明](docs/LICENSING.md)。
