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

```bash
git clone https://github.com/qsbb/ling.git
cd ling
bash deploy/install.sh
```

脚本会自动生成密钥、构建镜像、起服务，最后打印控制台地址与账号密码。

```bash
bash deploy/install.sh --status      # 状态
bash deploy/install.sh --logs api    # 日志
bash deploy/install.sh               # 升级（在**当前源码**上重新构建并重启）
bash deploy/install.sh --down        # 停止（数据保留）
```

> **升级前必读**
> `deploy/install.sh` 只会**构建并部署当前这份源码**，不会 `git pull`、也不会去
> GitHub 下载新版本。仓库目前**还没有发布正式 Release**，自动更新默认关闭
> （`LING_UPDATE_ENABLED=0`）。想升级要先在宿主机上人工接管：取到新源码后重新运行
> `install.sh`（它会保留 `deploy/.env` 与数据卷）。
> 更新执行器（`deploy/update/`，默认不启用）只负责在**已经人工纳管**的机器上按
> tag/commit 执行同一套安装流程；第一次纳管必须人工完成，见
> [docs/UPDATES.md](docs/UPDATES.md)。
> 升级前先备份数据库与 `deploy/.env`。API 容器是只读根文件系统、非 root，**没有
> Docker socket**，也没有挂宿主源码树；真实 Docker 上的升级/回滚演练**尚未完成**，
> 不要把自动更新当成已验证能力。

> 首次启动会**生成随机密码并打印在启动日志里**（安装脚本也会把它取出来显示）。
> 只有 `deploy/install.sh --admin-user <名字>` 能改用户名；默认 `ling`。
> 登录后会**强制要求修改密码**——随机口令只当一次性登船票。

### 本地开发

前置：`uv`（≥0.5）、`pnpm`（≥9）、`python3.11`（`scripts/dev_pg.sh` 用它跑便携
PostgreSQL——`pgserver` 目前没有 3.13 的 wheel，所以 PostgreSQL 和运行 ling 的
Python 是两份不同的解释器）。

```bash
uv venv --python 3.13 .venv && . .venv/bin/activate
uv pip install -e ".[dev]"

scripts/dev_pg.sh start                      # 便携 PostgreSQL，端口 55432（内部用 3.11）
cd clients/web-console && pnpm install && pnpm build && cd ../..

. scripts/dev_env.sh
python -m ling                               # → http://127.0.0.1:8080
```

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

发布与升级：仓库目前**没有发布正式 Release**，也没有经过真实 Docker 的升级/回滚
演练。`install.sh` 只部署当前源码；自助更新默认关闭，第一次升级必须人工纳管，
见 [docs/UPDATES.md](docs/UPDATES.md)。

## 安全与隐私

这是个会长期记录你说话方式、生活细节和情绪状态的系统，**部署在你自己的机器上**，
数据不出你的服务器。发现安全问题请走 [SECURITY.md](SECURITY.md) 的私下渠道，
不要开 public issue。

## 许可

[GNU AGPL-3.0-or-later](LICENSE)。

你可以自由使用、修改、部署，但如果把它作为网络服务提供给别人，
**必须公开你的修改**。想闭源商用需要另行取得授权——见 [许可说明](docs/LICENSING.md)。
