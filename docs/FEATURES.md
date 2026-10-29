# ling 功能文档与使用说明

> 依据：`pytest --collect-only`、`app.py` 实际接线、`migrations/versions/`、路由装饰器统计
> **状态口径**：`✅ 实测` 有自动化测试且跑通；`⚠️ 仅端口` 有类型与端点但无实际链路；`⬜ 空壳` 只有占位

---

## 第一部分：功能文档

### 1. 代码与规模

| 项 | 数量 |
| --- | --- |
| 后端代码 | 约 2.7 万行（`src/ling`） |
| 连接器 | 约 3.9 千行（`connectors`） |
| Web 控制台 | 约 5.7 千行（React 19 + Vite） |
| 测试 | 1100+ 个测试函数，本地全量通过 |
| 数据库表 | 42 张业务表；迁移 42 个 |
| HTTP 端点 | 85 个（路由装饰器计） |
| 浏览器 E2E | 手工执行，未纳入 CI |

> 上面是量级，不是承诺。要复核请跑：
>
> ```bash
> find src -name '*.py' | xargs wc -l | tail -1
> ls migrations/versions/*.py | wc -l
> grep -rhoE '@router\.(get|post|put|patch|delete)' src/ling/api/routes/*.py | wc -l
> pytest tests                      # 真实通过数以此为准
> ```

### 2. 会话与对话

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| 多会话窗口 | ✅ | 每个窗口独立上下文；会话可归档 |
| 流式回复（SSE） | ✅ | POST + fetch 流式；`proxy_buffering off` 已实测不被缓冲 |
| 轮次状态机 | ✅ | `pending → streaming → completed/failed/stopped`，服务端裁决，浏览器不参与 |
| 幂等提交 | ✅ | `client_turn_id` 唯一约束；重复提交不产生两条 |
| 停止 / 重试 | ✅ | 停止走 `TurnControlStore`；重试复用同一 turn |
| 打断与发言权 | ✅ | `SpeakerArbiter` + 跨实例租约（`SELECT ... FOR UPDATE`） |
| 历史与分页 | ✅ | 消息按 `sequence` 排序 |
| 滚动摘要 | ✅ | 超出 token 预算的消息压成摘要，**保留头 45% + 尾 55%** |

**滚动摘要的两个关键约束**（都踩过坑）：
- 条数上限不能截断未摘要的消息——否则它们既进不了上下文也永远压不进摘要
- 摘要不能只留尾部——最早的约定往往就在开头

### 3. 记忆系统

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| 断言（Assertion） | ✅ | 模型提炼 + 代码闸门，13 类槽位 |
| 情节（Episode） | ✅ | 每轮互动摘要，用户/角色各留 500 字 |
| 核心记忆（Core） | ✅ | 每轮常驻不走检索；上限 20；`scope=private` 不进群 |
| 多路融合检索 | ✅ | BM25 / 向量 / 实体 / 新近度 / 重要性 → RRF(k=60) |
| 语义检索 | ✅ | BGE-M3（1024 维）+ BGE-reranker 精排 top-20 |
| MMR 去重 | ✅ | 2-gram Jaccard ≥ 0.7 判近似 |
| 相关性下限 | ✅ | 与第一名同量级才保留 |
| 实体链接 | ✅ | 别名归并（母亲/老妈/妈），沿关系召回 |
| 双时间轴 | ✅ | 单值槽位改口关闭旧值；问「以前」时带历史 |
| 压缩级联 | ✅ | 同槽位低价值 ≥3 条 → 高层摘要，原行归档保留链 |
| 只强化实际注入的 | ✅ | 被预算截掉的不计分（防马太效应） |
| 表达策略 | ✅ | `mention` / `tone` / `uncertain` 三层 |
| 纠错与遗忘 | ✅ | 撤回、supersede、`forget_subject`（含核心记忆） |
| 保留期与归档 | ✅ | 默认 365 天；被有效断言引用的情节不归档 |

**证据等级**（`origin`）：`user_statement` < `external_observation` < `verified_source`；
`model_inference` 永远只能进「隐约觉得」组，哪怕置信度 0.99。

**隐私边界**（均有回归测试）：
- 私聊内容不进群聊上下文
- 压缩**不跨用户、不跨可见范围、不跨来源窗口**
- 实体关联不绕过权限过滤
- `sealed` 模式连核心记忆也不注入

### 4. 人格与情绪

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| 人格设定 | ✅ | 稳定人设、叙事边界、版本化 |
| 短时情绪 | ✅ | 强度随事件变化并按时间衰减；权限不放宽 |
| 关系投影 | ✅ | 亲密度/信任度由**有归因**的互动事件驱动 |

### 5. 隔离与跨会话意识

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| 三种隔离模式 | ✅ | `sealed` / `per_window`（默认）/ `continuous` |
| 跨会话事实 | ✅ | 只传事实与事件，**不传对话状态** |
| 可见性单一权威 | ✅ | `kernel/audience.py` 是唯一裁决点（结构性测试锁死） |

### 6. 主动性

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| 主动机会 | ✅ | 生活/天气/目标/记忆只产生**有时效的机会**，不直接发消息 |
| 勿扰时段 | ✅ | 按**用户时区**判定（UTC 兜底，绝不用服务器本地时区） |
| 每日限额 | ✅ | 按用户时区计日 |
| 承诺履约 | ✅ | 持久任务，直到完成/取消/失败；取消是终态且幂等 |

### 7. 目标、知识与生活模拟

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| 目标 | ✅ | 状态机 + 承诺派发 |
| 知识库 | ✅ | 来源必须可核验（URL 或明确外部观察）；模型记忆不算来源 |
| 活动/世界 | ✅ | 模拟活动状态机、地点与物品 |
| 自我时间线 | ⬜ | 角色无自主行为，暂缓 |

### 8. 渠道接入

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| OneBot V11 | ✅ | 上报验签（HMAC-SHA1）、CQ 码/段数组解析、发送、回执查询 |
| QQ 官方 Bot | ⚠️ | 端点按条件挂载，但 `settings.qq` 不存在，签名校验永远为 None |
| 微信 | ⚠️ | `connectors/wechat` 有实现，`app.py` 未挂路由 |
| Web | ✅ | 浏览器端到端 20/20 |
| 第三方 SDK | ✅ | `connectors/sdk/client.py`，7 项端到端测试 |

**OneBot 错误语义**（有负例测试）：
- 失败一律**抛出** `ChannelError`，只有成功返回回执
- `retcode` 是权威字段：`retcode=0 且 status != failed` 才算成功
- `status=async` → `uncertain` 且 **不可重试**（避免发出两条）

### 9. 模型接入

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| OpenAI 兼容 provider | ✅ | 流式、多 provider、按职责绑定 |
| 错误分类 | ✅ | `unauthorized` / `unreachable` / `rate_limited` / `missing_secret` / `content_filtered` / `bad_request` |
| 密钥诊断 | ✅ | 区分「没存过」与「存过但解不开（根密钥变了）」 |
| 跨副本配置传播 | ✅ | 按配置修订号懒刷新 |
| 熔断与退避 | ✅ | 连续失败暂时熔断 |

### 10. 运维

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| 健康检查 | ✅ | `/healthz`、`/readyz`、`/v1/health/model` |
| 运维状态 | ✅ | 5 项可执行告警：outbox 积压/死信、卡住轮次、备份新鲜度、磁盘余量 |
| 告警推送 | ✅ | 通用 webhook，30 分钟去重，恢复后清零 |
| 备份 | ✅ | 直接模式 + compose 模式（`docker compose exec`，不暴露端口） |
| 恢复演练 | ✅ | 目标库名必须 `_drill` 结尾；真实部署跑通 |
| TLS | ✅ | `nginx-tls.conf`；TLS 下 SSE 实测不逐段缓冲问题 |
| 多副本 | ✅ | 幂等 outbox、租约仲裁、配置传播 |

### 11. 明确未实现（不要误以为有）

| 模块 | 真实状态 |
| --- | --- |
| `domains/presence/` | 只有一行 `__init__` 的空包 |
| `capabilities/embodiment.py` | 321 行，只被 `manage.py` 当状态字符串读，**无功能接线** |
| `ports/speech.py` | 只有端口与状态端点，**无 TTS/STT 实际链路** |
| 视觉/相机/位置 | `media_refs` 已提取但**不进模型输入** |
| 工具调用 | `/v1/tools` 可单独调，但 `ModelRequest` **没有 tools 字段**，不进对话环 |
| 公开发布 | 默认关闭，仅审批流骨架 |
| 共处会话 | 仅端口定义 |
| 风格/黑话学习 | 无 |
| 表情包 | 无 |
| 多平台（Discord/Telegram） | 无 |

---

## 第二部分：使用说明书

### 1. 本地开发启动

```bash
cd /opt/ling
. .venv/bin/activate
scripts/dev_pg.sh start          # 便携 PostgreSQL（127.0.0.1:55432）
. scripts/dev_env.sh             # 注意：默认 LING_TEST_MODE=1（假模型）
python -m ling                    # API
python -m ling.worker             # 后台任务（记忆提炼/关系/承诺）
```

用真模型要显式关掉测试模式：

```bash
export LING_TEST_MODE=0
```

浏览器打开 `http://127.0.0.1:8080`。

### 2. 首次配置（空库）

```bash
# 1) 迁移（42 张业务表，合计 43）
LING_DATABASE_URL="postgresql+asyncpg://ling@127.0.0.1:55432/ling" python -m alembic upgrade head

# 2) 启动后登录
curl -c cookies.txt -X POST http://127.0.0.1:8080/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"ling","password":"<LING_BOOTSTRAP_ADMIN_PASSWORD>"}'
```

此时发消息会明确告诉你缺什么：

```
职责 dialogue.fast 尚未绑定模型，请在设置里配置后再试
真实供应商还没有保存密钥
```

按提示在「设置」页配置 provider 与密钥即可。

### 3. 语义检索（可选）

```bash
export LING_EMBEDDING_API_BASE=https://api.cloudflare.com/client/v4/accounts/<ID>/ai/v1
export LING_EMBEDDING_ACCOUNT_ID=<ID>
export LING_EMBEDDING_API_KEY=<KEY>
export LING_EMBEDDING_MODEL=@cf/baai/bge-m3
export LING_RERANK_API_BASE=https://api.siliconflow.cn/v1
export LING_RERANK_API_KEY=<KEY>
export LING_RERANK_MODEL=BAAI/bge-reranker-v2-m3
```

不配也能用，检索退回 BM25+RRF 字面路径。

### 4. 接 QQ（OneBot V11）

```bash
export LING_ONEBOT_API_BASE=http://<napcat-host>:3000
export LING_ONEBOT_ACCESS_TOKEN=<token>
export LING_ONEBOT_SECRET=<上报签名密钥>
export LING_ONEBOT_SELF_ID=<机器人QQ号>
export LING_ONEBOT_INSTANCE_ID=napcat-main
```

NapCat/SL 的上报地址填 `http://<ling-host>:8080/v1/onebot/events`。

**必须先绑定账号**才能收到回复：未证明的绑定会被拒绝（这是有意的，防止误绑陌生人）。

### 5. 运维

```bash
# 状态总览
curl -b cookies.txt http://127.0.0.1:8080/v1/health/ops

# 定时备份
deploy/backup/install-cron.sh /mnt/backup/ling 04:30

# 恢复演练（目标库名必须 _drill 结尾）
deploy/backup/restore_drill.sh <dump> ling_drill

# 告警推送
export LING_ALERT_WEBHOOK_URL=https://example.com/hook
```

### 6. 记忆相关的用户可见行为

| 你说 | 它会怎么做 |
| --- | --- |
| 「我不吃香菜」 | 记成 `dislikes=香菜`，之后能换措辞问回来 |
| 「我住杭州」→「我搬苏州了」 | 旧值关闭、新值生效；问「我住哪」答苏州 |
| 「我以前住哪」 | 带出历史值，并明确说是「以前」 |
| 「忘记我」 | 撤回普通记忆 + 归档情节 + 停用核心记忆 |
| 「我朋友的猫叫咪咪」 | **不记成你的猫**（他人信息） |
| 「我打算搬去苏州」 | **不记成已搬**（未兑现计划） |

---

## 第三部分：当前完成度自评

| 维度 | 完成度 | 说明 |
| --- | --- | --- |
| 对话主链 | 高 | 流式、停止重试、幂等、发言权都实测 |
| 记忆 | 高 | 参照系里的记忆能力基本齐了，缺风格学习 |
| 隔离与隐私 | 高 | 单一权威 + 多处负例测试 |
| 主动性 | 中高 | 硬门完整，实际触发频率未在真实流量下调 |
| 渠道 | 中 | OneBot 模拟端到端过；真实 QQ 未联调 |
| 多模态 | 低 | 语音/视觉/具身都是空壳 |
| 扩展性 | 低 | 无插件体系，工具不进对话环 |
| 运维 | 高 | 备份、恢复、告警、TLS、多副本 |
