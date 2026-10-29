# ling 部署与运维手册

## 1. 本地开发（无 Docker）

本机没有 Docker 时，用便携 PostgreSQL + Python 虚拟环境。

```bash
cd /opt/ling

# 1) 依赖
uv venv --python 3.13 .venv
. .venv/bin/activate
uv pip install -e ".[dev]"

# 2) 数据库（便携 PostgreSQL 16，端口 55432）
scripts/dev_pg.sh start

# 3) 前端构建（可选，首次需要）
cd clients/web-console && pnpm install && pnpm build && cd ../..

# 4) 启动
. scripts/dev_env.sh
export LING_WEB_DIST=$PWD/clients/web-console/dist
python -m ling
# → http://127.0.0.1:8080
```

开发默认 `LING_TEST_MODE=1`，对话由确定性 fake provider 生成，界面显著标注
“测试模式：回复由确定性 fake provider 生成，非真实模型能力”。

## 2. Docker Compose（Linux）

```bash
cd /opt/ling
cp deploy/env.example deploy/.env
# 编辑 deploy/.env：至少设置 POSTGRES_PASSWORD、LING_BOOTSTRAP_ADMIN_PASSWORD、
# LING_ROOT_KEY（固定值，用于加密供应商密钥），并把 LING_TEST_MODE 设为 0

cd clients/web-console && pnpm install && pnpm build && cd ../..
docker compose -f deploy/compose.yaml --project-name ling-dev up -d --build
curl http://127.0.0.1:8080/readyz
```

服务：`db`（不暴露端口）、`api`（8080）、`worker`（outbox/恢复）、`web`（构建产物挂载到 api）。

启动顺序：`db` 健康 → `api` 等库 → 跑迁移 → 起服务。迁移失败时 api 不启动。

### 非 root 与只读根

容器以 uid 10001 运行，根文件系统只读 + `/tmp` tmpfs，`no-new-privileges`。
数据卷只有 `/data`（本地密钥回退用）。

### 健康检查语义

| 端点 | 含义 |
| --- | --- |
| `/healthz` | 进程活着 |
| `/readyz` | 数据库可达 + 迁移完成 + 生效配置修订 |
| `/v1/health/model` | 各职责 profile 的熔断状态摘要 |

## 3. 设置模型（唯一入口）

1. 登录 → 设置 → 供应商：新增 `openai_compatible`，填 `base_url`（如
   `https://api.openai.com/v1`），保存密钥（只显示后四位）。
2. 连接检查（`/models` 探测），确认鉴权通过。
3. 职责绑定：至少给 `dialogue.fast` 绑定 provider + model_id。
4. 保存草稿 → 发布。发布后的修订对**下一轮**生效。
5. 出问题时回滚到上一个已发布修订。

**没有凭据时**：保持 `LING_TEST_MODE=1`，界面会一直标注测试模式。

## 4. 备份与恢复

```bash
# 备份（数据库 + 媒体清单）
scripts/backup.sh /path/to/backup

# 恢复演练（隔离库，不碰生产）
# 恢复演练（**两个参数**：dump 文件 + 演练目标库）
# 目标库名必须以 _drill 结尾，脚本拒绝覆盖生产库
deploy/backup/restore_drill.sh \
    /path/to/ling-YYYYmmddTHHMMSSZ.dump \
    "postgresql://ling:PASSWORD@127.0.0.1:5432/ling_drill"
```

恢复前必须核对撤回墓碑：旧备份可能让已删除记忆重新可见。见
本节列出的未验证项。

## 5. 故障排查

| 现象 | 检查 |
| --- | --- |
| 登录后立刻 401 | `LING_COOKIE_SECURE=1` 但走 HTTP；或数据库不可达导致会话查不到 |
| 对话一直失败 `provider_unavailable` | 设置里 provider 未启用 / 熔断中（`/v1/health/model`） |
| `secret_missing` | 该供应商没保存密钥 |
| `config_invalid` 发布被拒 | 草稿校验错误，响应里 `detail` 列出字段 |
| 界面显示“服务端仍在生成” | 上一进程崩溃；重启后 `_recover_dangling_turns` 会标记 failed，可点重试 |
| `/readyz` 503 | 数据库不可达或迁移未跑 |

## 6. 验证状态（哪些验过、哪些没验）

**已验证**（后文 §7–§9 是这些验证的记录）：

- Docker Compose 单机部署：`api` / `worker` / `db` / `edge` 全部起得来，`/readyz` 通过
- 多副本 + edge 反代：滚动更新期间不断服
- 备份 → 恢复 → 校验整条链在真实部署上跑通
- 真实模型对话：配好供应商与密钥后能正常回复

**未验证**：

- 微信 / 手机端 / 具身设备（这些渠道的适配代码在仓库里，但没有可用的对照端）
- 公网部署所需的域名、TLS、反代加固
- 长时间（数周）无人值守运行的稳定性

> 判断某项是否真的能用，看它有没有对应的测试或本节的验证记录；
> 只写了代码没有记录的地方，按未验证对待。

## 7. 多副本部署（已验证）

```bash
# deploy/.env
LING_API_REPLICAS=2          # api 副本数
LING_EDGE_PORT=8080          # 对外只暴露 edge
LING_NGINX_IMAGE=nginx:alpine

docker compose -f deploy/compose.yaml --project-name ling up -d --build
```

拓扑：`edge(nginx):80 → api×N:8080`，`worker`、`db` 不对外。

### 为什么 api 不发布 host 端口

多副本发布同一 host 端口会冲突。对外只经 edge，内部用 Compose DNS
（服务名 `api` 解析到所有副本，nginx upstream 轮询即负载均衡）。

### 多副本的前提：状态不能在进程内存

已在 PostgreSQL 实现（迁移 0007）：
- `turn_controls`：停止信号跨实例传递（A 点停止、B 跑轮次）
- `speak_leases`：发言租约，同一角色同时只在一个窗口说话

**仍限单实例**（有意降级，非缺陷）：
- 流式片段续传 → 多实例下 resume 退化为权威快照（前端仍拿到完整文本）
- 群发言频率 → 实际限额约为单实例 × 副本数（宽松方向）

### nginx 必须关闭 buffering

`deploy/nginx.conf` 里 `/v1/` 段设了 `proxy_buffering off` 与 300 秒超时。
不关的话 SSE 会被 nginx 攒成一整段，流式就失效了。
实测：delta 每 50ms 到达一次，26 个事件总耗时 1.3 秒。

### worker 保持单副本

outbox handler 里有非幂等副作用（记忆提取会写 Episode/Assertion）。
`SELECT ... FOR UPDATE SKIP LOCKED` 能防重复领取，但处理中途失败重试
仍可能重复写入。需要扩容时先让每个 handler 幂等。

### 部署踩过的坑（都已修）

| 现象 | 原因 | 修复 |
| --- | --- | --- |
| 启动后 500 `column "attempt" does not exist` | 镜像里没有 `alembic.ini`，迁移器检测不到修订，退化成 `create_all`（不会给已有表加列） | Dockerfile 补 `COPY alembic.ini`；设 `LING_PROJECT_ROOT=/app` |
| 迁移器找不到 `migrations/` | 镜像里包进 site-packages 后 `__file__.parents[2]` 指向 Python 安装目录 | 按 `LING_PROJECT_ROOT` → cwd → 包位置依次探测 |
| `SyntaxError: source code string cannot contain null bytes` | macOS tar 打包带上 `._xxx.py`（AppleDouble），Alembic 当迁移脚本加载 | `.dockerignore` 排除 + 迁移器跳过 `._` 与空文件 + 打包用 `COPYFILE_DISABLE=1` |

## 6. 接入 QQ（OneBot V11）

OneBot V11 是 NapCat / Lagrange / go-cqhttp 共同实现的协议。ling 提供
`POST /v1/onebot/events` 作为**事件上报地址**。

### 6.1 配置

```bash
# deploy/.env
LING_ONEBOT_API_BASE=http://127.0.0.1:5700   # OneBot 实现的 HTTP API
LING_ONEBOT_ACCESS_TOKEN=<与 NapCat 一致>
LING_ONEBOT_SECRET=<与 NapCat 一致，用于校验上报签名>
LING_ONEBOT_SELF_ID=<机器人 QQ 号>
LING_ONEBOT_INSTANCE_ID=onebot-main
```

NapCat 侧要开：
- **HTTP 服务器**：端口 5700（对应 `LING_ONEBOT_API_BASE`）
- **HTTP 上报（反向 HTTP）**：地址填 `http://<ling 地址>:8080/v1/onebot/events`
- 上报的 **secret / token** 与上面两项保持一致

### 6.1.1 谁能跟她说话（出入许可）

陌生 QQ 号原来会走完整个流程——建窗口、写消息、跑决策、调模型——
才可能被发现不该理。现在在解析完信封、**任何写库之前**就判定。

```bash
LING_ACCESS_MODE=whitelist        # off | whitelist | blacklist，默认 off
LING_ACCESS_LIST=10001       # 逗号分隔的 QQ 号
```

- 默认 `off`：与改动前行为一致，不会悄悄改变既有部署的表现
- 名单按**字符串**比较——QQ 号可能有前导零，转 int 会丢，于是"加了白名单却对不上"
- 配置写错（如 `white-list`）**按拒绝处理**：宁可漏接，不要因为一个拼写错误
  把所有陌生人放进来
- 被拒时返回 200 + `accepted:false`：拒绝不是服务出错，非 200 会让 OneBot
  反复重投同一条事件
- 名单外的人**不建窗口、不留消息**，只在日志里留一行便于核对

### 6.1.2 其它运行时开关

```bash
LING_USER_TIMEZONE=Asia/Shanghai  # 勿扰时段、每日限额按当地作息算
LING_CONTEXT_BUDGET_SCALE=1.0     # 整体缩放上下文预算（0.5 = 25k token）
LING_ONEBOT_PROACTIVE=0           # 主动推送能力位（平台权限探测结果，不是偏好）
```

上下文预算默认合计约 **50k token**（车道 38k + 最近原文层 20k）。
模型窗口装不下就调 `LING_CONTEXT_BUDGET_SCALE`，不必改代码。

### 6.1.3 NapCat 侧：必须配「HTTP 上报」（实测跑通）

ling 的 OneBot 连接器是 **HTTP-only**（`connectors/onebot/` 里没有 WebSocket 实现），
所以 NapCat 侧建议用 **HTTP 上报（httpClients）**，或在 ling 支持的服务端模式下反向连入。

在 NapCat 的 `onebot11_<机器人QQ>.json` 里：

```json
{
  "network": {
    "httpServers": [{ "name": "http-local", "enable": true, "port": 3000, "token": "" }],
    "httpClients": [
      {
        "name": "ling",
        "enable": true,
        "url": "http://<ling 所在机器>:8080/v1/onebot/events",
        "messagePostFormat": "array",
        "reportSelfMessage": false,
        "token": "",
        "heartInterval": 30000
      }
    ]
  }
}
```

三个容易踩的点：

1. **`LING_HOST` 必须是 `0.0.0.0`**。只绑 `127.0.0.1` 时 NapCat 连不上
   （实测：同一局域网内 `readyz` 直接 000）。
2. **`LING_ONEBOT_INSTANCE_ID` 要与绑定账号时用的实例名一致**。
   账号绑定按 instance 存，写错会「事件进来了但解析不到身份」。
3. **`X-Self-ID` 必须匹配**。配了 `LING_ONEBOT_SELF_ID` 之后，
   上报不带或带错这个头会被直接拒绝（防止别的实例误配到同一入口）。

连通后应该看到这样的记录（数值为示例）：

```
NapCat 日志：HTTP上报服务: http://<ling-host>:8080/v1/onebot/events, : 已启动
ling 响应：  {"accepted":true,"decision":"reply","delivered":true,
             "parts_total":2,"parts_sent":2}
NapCat 日志：<bot> | 发送 -> 私聊 (10001) 第一条测试消息
             <bot> | 发送 -> 私聊 (10001) 第二条测试消息
回执：        state=delivered platform_msg_id=1000000001 / 1000000002
```

**注意**：NapCat 的 httpClient 只在有**消息事件**时上报，不推心跳。
所以「配好之后 ling 一直没动静」不等于配错了——要有人给机器人发一条消息才算通。

### 6.2 绑定账号（**前置条件**）

OneBot 事件要先解析成自然人。**未绑定会被直接拒绝**（不猜身份）：

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8080/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"ling","password":"<你的密码>"}' -c /tmp/c.txt >/dev/null && echo ok)

# 绑定（proof=pending：先建立关系，不能用于身份解析）
curl -b /tmp/c.txt -X POST http://127.0.0.1:8080/v1/accounts/bind \
  -H 'Content-Type: application/json' \
  -d '{"connector_instance":"onebot-main","external_account_id":"<QQ号>"}'

# 确认账号确实属于本人后，升级为已证明（此后才能解析身份）
curl -b /tmp/c.txt -X POST http://127.0.0.1:8080/v1/accounts/<account_id>/prove
```

`proof=pending` 只能建关系、不能解析身份——否则"自称某账号"就能拿到那个人的记忆。

### 6.3 验证

```bash
# OneBot 连通性（调 get_login_info）
curl http://127.0.0.1:8080/v1/onebot/health

# 在 QQ 里给机器人发一条私聊，应收到回复；
# 服务器侧可核对：
curl -b /tmp/c.txt http://127.0.0.1:8080/v1/accounts   # 绑定状态
```

事件上报**业务失败也返回 200**（否则 OneBot 会反复重投），
验签失败返回 403。重复事件按 `(实例, message_id)` 去重，只处理一次。

### 6.4 QQ 官方机器人（QQ 开放平台 / QQ Bot）

这是与 OneBot **平行的第二条接入路径**：直接用 QQ 开放平台的官方接口，
不需要 NapCat。两者共用同一套入站受理（白名单、去重、身份、决策、投递），
差别只在协议层。

**先读这条**：官方接口和 NapCat 不是一回事，下面三件事在协议层就受限，
不是 ling 没实现——

| 限制 | 实际含义 |
| --- | --- |
| **只能被动回复** | 回复必须带用户那条消息的 `msg_id`。官方数值（已核实）：**单聊 60 分钟内 4 次，群聊 5 分钟内 5 次**；`msg_id + msg_seq` 相同会重复失败。窗口过期或用尽后再发平台会拒绝，ling 会如实报失败，不会假装送达 |
| **主动推送要单独申请** | 想让她主动找你（早安、提醒），必须在开放平台申请"主动消息"权限。没有权限时 `supports_proactive=false`，主动投递会被门控**拒绝并说明原因** |
| **openid ≠ QQ 号** | 事件里只有 `author.member_openid` / `author.user_openid`。同一个人在不同机器人下 openid 不同，也**拿不到** QQ 号。因此白名单、账号绑定都要用 openid |

> 本仓库没有任何真实 app_id/secret，**未与真实平台联调**。协议细节按官方文档实现，
> 标注为「未核实」的几处在下面 6.4.4 列明。

#### 6.4.0 收事件用 **WebSocket 网关**，不要用 Webhook（默认）

官方有两种收事件的方式，**这里默认并推荐 WebSocket**：

| | Webhook | WebSocket 网关 |
| --- | --- | --- |
| 要求 | 腾讯要能访问你的**公网 HTTPS** 回调地址 | ling 主动连出去 |
| 家里/NAS 内网 | ❌ 走不通（没公网 IP/域名/证书） | ✅ 直接可用 |
| 发消息 | 官方文档要求"机器人需连到 WebSocket 保持在线"，纯 Webhook 发不出去 | ✅ |

网关模式**不需要公网、不需要备案域名、也不需要任何扫码**——只需要
`AppID` + `AppSecret` 两个票据。

协议按官方文档 `api-v2 → 事件订阅与通知 → WebSocket 方式` 实现：

```
GET  {base}/gateway                    → {"url": "wss://api.bot.qq.com/websocket/"}
连接后 ← op=10 Hello {heartbeat_interval}
发   → op=2 Identify {token: "QQBot <access_token>", intents: 1<<25, shard:[0,1]}
收   ← op=0 t=READY {session_id}       此后每帧的 s 是序号
定时 → op=1 心跳 {d: 最近一次 s}
重连 → op=6 Resume {session_id, seq}   平台补发漏掉的事件
```

`intents = 1 << 25`（`GROUP_AND_C2C_EVENT`）覆盖 `C2C_MESSAGE_CREATE` 与
`GROUP_AT_MESSAGE_CREATE`——也就是单聊与群里 @ 她。

断线按关闭码处理：`4007/4006` 与 `4900~4913` 重新 Identify，`4008/4009`
走 Resume，`4914/4915`（只允许沙箱 / 已封禁）**停止重连**并报错。

#### 6.4.1 平台侧要做什么

1. 在 [QQ 开放平台](https://q.qq.com/) 创建机器人应用，进入
   **开发设置**，记下 `AppID` 与 `AppSecret`（= ling 的 `LING_QQBOT_CLIENT_SECRET`）
   以及 `BotSecret`（Webhook 验签用）。
2. **添加沙箱/正式环境的测试用户**：沙箱环境下只有名单里的人能跟机器人对话，
   不加人就会"发了没反应"。
3. **开通能力**：私聊（C2C）默认有；群聊需要在"功能配置"里开通
   （未开通时群事件根本不会推过来）。
4. **事件订阅**：在"开发设置 → 回调地址"填
   `https://<你的域名>/v1/qqbot/events`，并勾选消息事件：
   - `C2C_MESSAGE_CREATE`（私聊）
   - `GROUP_AT_MESSAGE_CREATE`（群里 @ 机器人）
   保存时平台会立刻回调一次 `op=13` 做**回调地址校验**，ling 必须在线，
   否则配置保存不成功。
5. 回调地址必须是 **HTTPS 且公网可达**（官方要求）。本机开发用内网穿透，
   并把穿透域名填进去。

> 频道（guild）产品线的事件（`AT_MESSAGE_CREATE` / `DIRECT_MESSAGE_CREATE`）
> **暂不支持**：它的窗口与发送语义和 QQ 群/私聊不同，当前会明确返回
> `unsupported`，不会静默丢弃。

#### 6.4.2 ling 侧配什么

```bash
# deploy/.env
LING_QQBOT_ENABLED=1
LING_QQBOT_APP_ID=<开放平台的 AppID>
LING_QQBOT_CLIENT_SECRET=<AppSecret>
LING_QQBOT_BOT_SECRET=<BotSecret，Webhook 验签密钥>
LING_QQBOT_INSTANCE_ID=qqbot-main        # 账号绑定按它隔离，别和 onebot 混用
LING_QQBOT_SANDBOX=1                     # 沙箱=1；上正式环境并过审后改 0
LING_QQBOT_SUPPORTS_GROUP=1              # 平台侧开通了群聊才设 1
LING_QQBOT_PROACTIVE=0                   # 平台权限探测结果，不是偏好开关
```

可选：

```bash
LING_QQBOT_API_BASE=            # 显式覆盖 API 基址（自建代理/平台改域名）
LING_QQBOT_TOKEN_URL=           # 覆盖换 token 的端点
LING_QQBOT_WEBHOOK_PUBLIC_KEY=  # 直接给 Ed25519 公钥（hex），跳过由 BotSecret 派生
LING_QQBOT_ALLOW_UNSIGNED_VALIDATION=0  # 平台不给 op=13 签名时才开
```

**没有 `LING_QQBOT_BOT_SECRET` 就收不到任何消息**：没有密钥无法确认来源，
验签一律失败并返回 403（不会"宽松通过"）。

账号绑定与 OneBot 相同，只是 `external_account_id` 填 **openid**，
`connector_instance` 填 `LING_QQBOT_INSTANCE_ID`：

```bash
curl -b /tmp/c.txt -X POST http://127.0.0.1:8080/v1/accounts/bind \
  -H 'Content-Type: application/json' \
  -d '{"connector_instance":"qqbot-main","external_account_id":"<openid>"}'
```

那么 openid 从哪来？**只能从事件里抄**：给机器人发一条消息，在
ling 日志里找 `拒绝入站：sender=<openid>`（白名单模式下）或直接看
`account_ref`。拿不到 openid 时不要猜 QQ 号——那会让身份绑到错误的行。

#### 6.4.3 如何验证接通（实测步骤）

本仓库只验证了协议层（用 `httpx.MockTransport` 模拟平台）。对上真实平台时按下面顺序走，
**每一步都有可核对的结果**，不要跳步：

1. **凭据能不能换到 token**（不涉及消息权限）：

   ```bash
   curl -s http://127.0.0.1:8080/v1/qqbot/health | python -m json.tool
   ```

   期望 `ok: true`，`detail` 里写着 `鉴权通过 ... 消息收发权限未验证`。
   `ok: false` 时 `detail` 会给出确切原因（缺凭据 / 401 / 不可达），按它排查。

2. **回调地址校验能不能过**：在开放平台点保存回调地址。
   - 成功：平台提示配置成功
   - 失败：ling 日志里会有一条 `QQ 机器人回调验签失败（op=13）` 或
     `无法完成回调地址校验：未配置 bot_secret`。前者说明 BotSecret 与平台不一致，
     后者说明根本没配。

3. **私聊一句，看是否真的发出去**：在 QQ 里给机器人发一条私聊，然后核对
   ling 侧回执（这一条最关键——只有拿到平台返回的消息 id 才算成功）：

   ```bash
   psql "$LING_DATABASE_URL" -c \
     "select channel, state, platform_message_id, detail from delivery_receipts
      where channel = 'qqbot' order by observed_at desc limit 5;"
   ```

   期望 `state = sent` 且 `platform_message_id` 非空。
   若 `state = uncertain`：平台超时/5xx，**不要重发**，按对账流程处理。
   若 `state = failed` + `detail` 含 `40034`/`msg_id`：被动回复窗口过期或用尽，
   属于平台限制，不是配置错。

4. **群里 @ 一次**：只有 `GROUP_AT_MESSAGE_CREATE` 会推过来（不 @ 的群消息
   官方不推）。收到即说明群聊能力已开通。

5. **多段回复要注意**：一条长回复会被拆成多条发送，每条都要各自带 `msg_id`
   与递增的 `msg_seq`。如果平台返回"重复回复"类错误，说明 `msg_seq` 没递增——
   这是连接器的 bug，不是配置问题。

#### 6.4.4 已知限制与「未核实」项

诚实清单（写在这里，避免以后有人把"没验证过"当成"验证过"):

- **未与真实平台联调**：本仓库没有真实凭据，全部按官方文档实现 + MockTransport 验证。
- **单条文本长度上限未核实**：官方未给稳定数字，默认按 1500 字符分片
  （`connectors/qqbot/settings.py: max_text_length`），部署时按实测调整。
- **被动回复的次数与时效已核实**（官方「消息收发概述 → 频率与时效规则」）：
  单聊 **60 分钟 / 4 次**、群聊 **5 分钟 / 5 次**，代码按场景分别计
  （`passive_quota()`）。窗口过期或用尽后按主动推送处理（默认拒绝）。
- **BotSecret → Ed25519 私钥的补齐规则未核实**：实现为"循环补足到 32 字节后截断"。
  若平台验签一直失败（现象：所有回调都 403），改用 `LING_QQBOT_WEBHOOK_PUBLIC_KEY`
  直接给公钥绕过派生。
- **换 token 的域名已核实**（官方 api-v2「获取访问凭证」）：
  `POST https://api.bot.qq.com/app/getAppAccessToken`，body 为
  `{"appId": ..., "clientSecret": ...}`；⚠️ 失败时 HTTP 仍是 200，必须按
  响应体的 `code` 判定（100007/100016/10004 是凭据或机器人状态问题）。
  平台改域名时用 `LING_QQBOT_TOKEN_URL` 覆盖。
- **被动窗口缓存在进程内存里**：多副本部署时，若回复由**另一个副本**发出，
  那个副本不知道 msg_id，会把这条当成主动推送并拒绝（`unsupported` + 原因），
  **不会**静默发丢。要么让 API 保持单副本，要么等后续把窗口落库。
- **富媒体（图片/语音）发送未实现**：`supports_media=false`，带媒体的投递会被
  明确拒绝而不是"发文字、丢图片、照样记成功"。入站附件下载已实现（https）。
- **频道（guild）产品线未接入**。
- **对账能力有限**：官方没有按消息 id 查投递状态的公开接口，`query_receipt`
  只能返回 `uncertain` 并说明原因，**不要**据此重发。

## 8. 无人值守：定时备份与运维告警

个人服务器长期没人看着，"有日志、有 healthcheck" 不足以保证你会第一时间
知道故障。这一节是让"有事能知道"成立的最小配置。

### 8.1 定时备份

```bash
# 安装每日 04:30 备份（幂等，可重复执行）
deploy/backup/install-cron.sh /mnt/backup/ling 04:30
crontab -l | grep backup.sh    # 确认已装
```

**关键**：宿主机 cron 里**没有 DSN**，数据库也只在本 compose 内网
（`compose.yaml` 有意不发布 db 端口）。直接跑 `pg_dump` 必然失败——
"已安装每日备份"其实一份都备不出来。

所以备份脚本有 compose 模式，通过 `docker compose exec` 进 db 容器导出：

```bash
# 手动验证一次（install-cron 已把这三个变量写进任务）
LING_BACKUP_VIA_COMPOSE=1 \
LING_COMPOSE_PROJECT=ling-verify \
LING_COMPOSE_FILE=$(pwd)/deploy/compose.yaml \
deploy/backup/backup.sh /mnt/backup/ling
```

`LING_COMPOSE_PROJECT` 不能省：`docker compose -f 路径` 会按**文件所在目录名**
推导项目名，从 cron 里跑时目录名往往和项目名不同（实测：项目叫 ling-verify、
目录叫 deploy），于是 `exec` 找不到正在运行的容器。

产物三件：`*.dump`、`manifest-*.txt`、`tombstones-*.txt`（无撤回记录时为空）。

**备份目录必须与数据库所在磁盘分开**（异机 / 网盘 / 外置盘）。
放同一块盘上，磁盘坏了备份和原文一起丢。

`manifest` 里的 `keys/root.key` 也要单独保管：数据库里的 provider 凭据
是加密的，根密钥丢了就全部解不开——表现为"机器人突然连不上模型"，
很难第一时间想到是密钥问题。

### 8.2 运维状态端点

```bash
curl -b cookies.txt http://127.0.0.1:8080/v1/health/ops
```

返回每个检查项的 `severity` 与 `action`（只有出问题时才给建议）：

| 检查项 | 什么情况下报警 |
| --- | --- |
| `outbox_pending` | 积压 ≥50 条，或最老一条超过 30 分钟（worker 可能停了） |
| `outbox_dead` | 有事件连续失败转入 dead（派生记忆会一直缺席） |
| `stuck_turns` | 有轮次超过 15 分钟没推进 |
| `backup_age_hours` | 超过 48 小时没新备份，或未配置 `LING_BACKUP_DIR` |
| `disk_free_gb` | 可用空间低于 2GB（写满后数据库直接不可写） |

`status` 为 `ok` 或 `attention`。配 `LING_BACKUP_DIR` 后备份新鲜度才会被判为正常。

实测：未配备份时 `status=attention`（1 项告警），
配上并跑一次备份后 `status=ok`（0 项告警）。

### 8.3 恢复演练

```bash
LING_BACKUP_VIA_COMPOSE=1 \
LING_COMPOSE_PROJECT=ling-verify \
LING_COMPOSE_FILE=$(pwd)/deploy/compose.yaml \
deploy/backup/restore_drill.sh /mnt/backup/ling/ling-XXXX.dump ling_drill
```

目标库名必须以 `_drill` 结尾，脚本会拒绝覆盖生产库。

**实测**（远端 compose：2 个 API 副本 + worker + db + edge）：

```
[drill] 重建隔离库 ling_drill（容器 db）
[drill] 恢复 dump
[drill] 校验
windows=7
turns=18
messages=32
memories=0
[drill] 完成（隔离库 ling_drill 未触碰生产库）
```

备份 → 恢复 → 校验整条链在真实部署上跑通，不是只在本地单机验证过。

### 8.4 多副本下的配置变更

默认部署是 2 个 API 副本 + 1 个 worker。密钥缓存是**进程内存**，
所以配置变更需要每个进程各自刷新，否则会出现"有的请求成功、有的报
`secret_missing`，worker 的记忆提炼静默退回正则"。

实现方式是**按配置修订号懒刷新**：每个进程记住自己加载到哪一版，
处理请求前比对一次（一个很小的查询），变了就重载。worker 在每次取任务前比对。

**实测验证**（本地双实例共享同一库）：

| 步骤 | 结果 |
| --- | --- |
| 在 A 上新建 provider 并写入（假）密钥、发布 | A/B 都返回 `unauthorized` |
| 同样的 provider 但**不写密钥** | 返回 `secret_missing` |

区别本身就是证据：`unauthorized` 说明密钥被解密并真的拿去请求了，
`secret_missing` 说明根本没有密钥。B 从未参与发布，却拿到了运行期
新增 provider 的密钥——传播生效。

### 8.5 告警推送

`/v1/health/ops` 解决"能查"，但不解决"会知道"——没人天天盯仪表盘。
配一个 webhook 就能主动推：

```bash
export LING_ALERT_WEBHOOK_URL=https://example.com/hook
```

内容是通用的 POST JSON，任意能收 HTTP 的服务都能用
（Telegram Bot API、Discord、钉钉/飞书机器人、ntfy、自建脚本）：

```json
{
  "text": "ling 需要处理：\n· backup_age_hours = None — 超过 48 小时没有新备份…",
  "status": "attention",
  "warnings": [{"name": "backup_age_hours", "severity": "warn", "action": "…"}],
  "source": "ling-worker"
}
```

worker 每 5 分钟检查一次（比小时级的维护任务密得多——"它不回话了"要尽快知道）。

**三条设计约束**：

| 约束 | 为什么 |
| --- | --- |
| 只推 `severity != ok` 的项 | 心跳推送会让人开始忽略通知，真出事那条就淹在噪声里 |
| 同一告警 30 分钟内只推一次 | 否则 worker 挂一小时推几百条，渠道直接废掉 |
| 恢复后**删除**去重状态 | 不然下次再出问题推不出来 |

手动验证一次（不用等 worker）：

```bash
curl -b cookies.txt -X POST http://127.0.0.1:8080/v1/health/ops/notify
```

**实测完整闭环**：未配备份 → worker 推送成功并记录 `count=1`、
再次触发被正确去重 → 配上备份后 `status=ok` 且去重状态清零
（下次出事能立刻再推）。

### 8.6 dead 事件的处理

连续失败 5 次的事件转入 `dead` 而不是静默丢弃。修好原因后重放：

```python
from ling.worker import replay_dead_events
await replay_dead_events()   # 把 dead 重新入队
```

**不做自动重放**：连续失败通常意味着规则或数据有问题，
自动重放只会反复失败刷日志。

## 9. 访问边界：什么时候必须上 TLS

先把场景说清楚，避免两个方向的错：

| 你怎么访问 | 需要 TLS 吗 |
| --- | --- |
| 只在同一台机器 / 可信 VPN / SSH 隧道内 | **不必须**（默认 `nginx.conf` 明文即可） |
| 手机在外网直连、或暴露到公网 | **必须** |

判断标准不是"是不是自己的服务器"，而是**登录凭证与私聊内容会不会经过不可信网络**。

### 9.1 启用 TLS

```bash
# 1) 生成自签证书（内部使用足够；浏览器会提示不受信任）
mkdir -p deploy/certs
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout deploy/certs/privkey.pem -out deploy/certs/fullchain.pem \
  -subj "/CN=ling.local"

# 2) 切到 TLS 配置并打开 cookie Secure
export LING_EDGE_CONF=./nginx-tls.conf
export LING_EDGE_CERTS=./certs
export LING_COOKIE_SECURE=1        # 少了这一条，凭证仍可能走明文
docker compose -f deploy/compose.yaml up -d
```

`LING_COOKIE_SECURE=1` 不是可选项：上了 TLS 但 cookie 不带 Secure，
浏览器仍可能在明文请求里带上会话 cookie。`scripts/check_compose.py`
会在检测到 443 端口时强制要求 `env.example` 里有这一项。

### 9.2 证书用 certbot 签发

`nginx-tls.conf` 已放行 `/.well-known/acme-challenge/`，
把 webroot 指到 `/var/www/certbot` 即可用 certbot 的 webroot 模式签发。

### 9.3 TLS 下的 SSE

`proxy_buffering off` 在 TLS 配置里同样必须存在，否则流式回复会被攒成整段。

**实测**（远端 Docker，自签证书，上游每 100ms 发一条）：

```
 0.006s data: tick-0
 0.107s data: tick-1
 0.207s data: tick-2
 0.308s data: tick-3
 0.408s data: tick-4
 0.509s data: tick-5
```

到达节奏与上游一致，说明 TLS 层没有缓冲。同时确认：HTTP/2 协商成功、
HSTS 头存在、TLS 1.1 握手被拒。
