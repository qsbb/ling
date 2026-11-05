# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.0.2] - 2026-11-05

### 新增
- **私聊自动注册**（`LING_AUTO_REGISTER_PRIVATE`，默认关）：私聊里没绑定过的
  渠道账号会注册成**独立自然人**（`proof=pending`、`trust=known`），消息不再
  整条被拒。**只注册、不合并**——合并会共享主人的全部记忆，必须由管理员在
  `设置 → 多用户与账号` 里看过是谁之后手动做（已有界面）。
- 新增集成测试：开关关时未绑定私聊被拒、开时被注册但不等同主人

### 安全
- **移除文档里的明文凭据**：`docs/HANDOFF-2026-10-30.md` 曾包含部署机
  管理员口令与 SSH 口令、内网地址与主机名。已全部替换为占位符与文档专用
  网段（192.0.2.0/24）。**注意：这些值仍留在 git 历史中**，相关凭据必须轮换。

### 测试
- 审计与自动注册用例改用明显假的账号，不再使用真实 QQ 号作样例

## [Unreleased]

## [0.0.1] - 2026-11-03

首个正式发布。仓库此前从未打过 tag，所以下面「Unreleased」里的全部历史改动
都属于这一个版本；本节只列这一轮（第三方审查 → 逐条核验 → 修复）的内容。

### 安全
- **SPA 兜底路由路径穿越（P0）**：未认证请求 `GET /../../data/root.key` 可读出
  44 字节 AES 根密钥明文。修复用 `resolve()` + `Path.is_relative_to` 判包含
  （不用字符串前缀——`/app/web-evil` 会误通过），24 个回归用例（含 `%2e%2e`、
  `..%2f`、符号链接逃逸、公共前缀目录）
- OneBot HTTP 入站未配 secret 时改为 fail-closed（原来直接放行；路由无 auth
  依赖 + 端口默认对外 + 白名单里就是 QQ 号，三者叠加等于账号可被冒充）
- OneBot 反向 WS 无 token 时拒绝启动（原来只打 warning 后监听 0.0.0.0）
- 媒体 `file:` 引用加目录约束与大小上限；OneBot/微信媒体域名白名单改用
  `host == h or endswith("." + h)`（裸 endswith 会放过 `evilqpic.cn`）；不跟随重定向
- 5 个只读端点补鉴权（`/v1/settings/health` 泄露熔断详情、`/v1/onebot/health`
  每次真打 NapCat 且泄露 self_id、`/v1/qqbot/health` 泄露明文 app_id）。
  `/readyz` **不加 auth** 而是降级：未鉴权只回 `{"status":...}`，否则容器
  healthcheck 会永远 unhealthy
- 登录加固：用户不存在时也跑 dummy scrypt（原来短路跳过，约 16ms 差异可枚举
  账号）、`to_thread` 避免阻塞事件循环、per-IP + per-username 限速与退避

### 修复
- **「停止 → 重试」必被旧标记停**：停止信号从 `turn_id` 改为 `(turn_id, attempt)`
  作用域（迁移 0045 加列并改复合主键）。原来重试复用同一 turn_id，新一轮
  watcher 在 0.5s 首次轮询命中旧标记，重试多少次都停
- 群聊隐私闸门失效：`is_group` 恒为 False（唯一生产者从不写这个键），改为从
  已有的 `source_visibility == group_scope` 派生
- **到期承诺永久失效**：调度从不传 `window_ref`，handler 读空后标 unsupported
  → `mark_delivery` 改成 undeliverable → 只取 active 的查询再也看不到它。
  改为从承诺行取 `window_id` 并做 `Window.id → Window.title` 两跳解析，
  缺失时回落到最近同渠道私聊窗口
- 主动投递不读已发布策略：勿扰恒不成立、日限额恒 2。改为传
  `policy=effective.dialogue`
- 配置刷新标记只认已发布修订（原来含草稿：存一次草稿就让所有进程永久不再
  重载），且换/删密钥会 bump 修订（跨进程唯一信号就是修订号）
- 精排不再整批丢弃情节记忆（断言 ≥20 时情节一次都没送进重排器）
- 分段按渠道硬上限切分（原来溢出段无上限、被整条拒收 → 部分送达 + 尾段丢失）
- 发言租约队首可抢走仍有效的租约（两个窗口同时外发）；`extend` 失败后不再
  继续输出；`release`/`leave` 失败留 WARNING 并补回收
- 会话 TTL 从配置派生（原来库里硬编码 +14 天，配置只影响 cookie），并加过期清理
- `_sync_secret_flags` 写不进库（浅拷贝 + 无 MutableDict，SQLAlchemy 检测不到
  变化）→ `has_secret`/`secret_last4` 永远停在旧值
- 承诺的 `window_id` 加归属校验（A3 之后它成了真实收件人，不校验则知道别人
  `Window.id` 就能让机器人往别人私聊发消息）
- 媒体描述失败终态（迁移 0044）：引用本身不可用不再每 60 秒无限重试；模型/配置
  错误整轮中止、不标终态（否则密钥配错一次会把整批待描述证据永久标死）
- Flutter 客户端编译错误（`StreamedResponse` 传给要求 `Response` 的工厂），
  retry 路径编译不过导致整个 App 构建失败

### 新增
- **群聊回复真正投递**：原来 `decision == "replied"` 只 `replied += 1`，群里
  永远不会开口。接上「构造上下文 → 生成 → 逐段投递（拿真实回执）」，
  投递失败记 `reply_failed` 而非 `replied`
- 配置项 `DialoguePolicy.group_reply_delivery_enabled`（默认**关**）与设置页开关
- 迁移链冒烟测试（45 个版本首次被测试真正执行，顺带抓到一个会让生产
  `alembic upgrade` 崩溃的 `get_pk_constraint()` 漏参 bug）
- CI：Python 3.12/3.13 矩阵、全仓 `ruff check .`、Flutter analyze 独立 workflow

### 变更
- 领域边界闸门修正：`DOMAINS_ROOT` 原指向不存在的目录，`rglob` 扫 0 个文件
  导致测试永远通过（真实基线 7 处跨域 import，已列入显式白名单并逐条写明理由）
- 6 组模型/迁移漂移对齐（`ix_messages_unseen` 偏索引曾被完全漏声明，任何人
  跑 autogenerate 都会生成删除它的迁移 → 注意力轮询退化为窗口全扫）
- 死代码清理：`allow_camera`/`allow_location`（零消费方，真授权走 ConsentService）、
  `handoff.to_contract`（无调用方且方法体把 created_at 写成 expires_at）、
  `speech.asr`/`speech.tts`/`realtime.voice` 移进 `RESERVED_PROFILES`
  （原来设置页能绑、绑了零效果，比没有入口更糟）、mobile 三个死依赖

### 破坏性变更
- `LING_ONEBOT_SECRET` 在 HTTP 上报模式下**必填**，且必须等于 NapCat 的
  `httpClients[].token`（同一字段同时承担 API 鉴权与 HMAC 密钥）。不配则该
  入口全部 403——容器仍 healthy、`readyz` 仍 200，只是 QQ 完全不回话
- 用反向 WS 且部署在容器里时，需额外一份 compose override 发布 WS 端口
  （`deploy/compose.ws.example.yaml`）
- 从 edge 拓扑升级必须先按 `docs/DEPLOYMENT.md` §7 人工迁移
  （`down --remove-orphans` 清孤儿容器 + `.env` 里 `LING_EDGE_PORT` 换成 `LING_PORT`）

---


### 新增
- **Web 主动投递**：没有外部推送渠道时，主动消息会落进 Web 会话，
  她才能真正先开口（仍受勿扰时段/日限额/草稿开关约束）
- 重复提醒：她连续几次用同一句收尾时，把这条**事实**告诉她（换不换由她自己定）
- **她有自己的日程**：每天醒来后按人格自己排 1-3 件小事，到点由 `life.tick`
  推进成"正在做"的活动；没有计划时如实显示"没有登记中的活动"，绝不硬编
- 审计日志与安全日志：`ling.audit` / `ling.security` 独立命名空间，
  配置发布、记忆撤回、授权变更、改密都留痕；账号标识只写 HMAC 哈希
- 群聊轮询与主动联系的结构化事件：每个群窗口的结论（含"没回"）、
  主动投递被门控的原因，都能只看日志回答
- worker 启动时探测供应商可达性：连不上会明确报 `worker_provider_unreachable`
  并给出检查方向，不再只表现为"能力悄悄变差"
- 轮次生命周期日志：`turn_started` / `turn_completed` / `delivery_failed`，
  与决策事件串成一条链，「为什么没回」「为什么没送达」都能只看日志回答
- 决策可观测：`reply_decision_made` 事件 + 受控 `skip_code` 枚举，
  只看日志就能回答「她为什么没回这条消息」
- 结构化日志：一行一条 JSON、`trace_id` 贯穿请求与响应头、字段名级脱敏、
  连接器状态机事件名固定可检索
- **总览（仪表盘）**：登录后首屏，一屏看到她在做什么、情绪、待发候选、
  最近记住的事、关系与渠道/模型状态；每块独立标 `ok/empty/unavailable`
  与更新时间，单块故障不拖垮整页
- 首次安装体验：随机口令打印在启动日志、登录后强制改密（改密时吊销其它会话）
- QQ 官方机器人渠道：WebSocket 网关模式，不需要公网回调地址、不需要扫码
- 一键安装脚本 `deploy/install.sh`（含基础镜像回退、幂等升级）
- 主动联系：到点的念头能变成真实投递，受勿扰时段与频率门控
- 表达选择权交给模型：`[at:]` / `[quote:]` / `[poke:]` 标记，她自己决定用不用
- 跨平台身份合并/拆分界面

### 变更
- 许可改为 AGPL-3.0-or-later
- 草稿超长改为在句子边界截断，不再切半句

### ⚠️ 破坏性变更
- **`LING_ONEBOT_SECRET` 现在是 HTTP 上报模式的必填项。** 留空则
  `/v1/onebot/events` **拒绝所有上报（403）**：这条路由没有鉴权依赖且 6520 默认
  对外发布，未配密钥时任何能访问端口的人都能伪造 `X-Self-ID` + 白名单 QQ 号
  冒充用户（完整的账号接管）。升级后不配 secret 的部署会表现为
  **容器 healthy、`/readyz` 200，但 QQ 完全不回话**；启动日志里有一条
  ERROR 说明怎么配。
  - 它必须与 NapCat `onebot11_<QQ>.json` 的 **`network.httpClients[].token`**
    逐字符相同（NapCat 的 httpClients 没有独立 `secret` 字段，`token`
    同时是 HMAC 签名密钥）。配错值仍然全 403。
  - `LING_ONEBOT_ACCESS_TOKEN` 是**出站**调用凭据（对应
    `network.httpServers[].token`），和 SECRET 不是一回事。
  - 详见 `docs/DEPLOYMENT.md` §6.1 与 `deploy/env.example`。
- **OneBot 反向 WS 服务端在容器部署下需要额外一份 compose override。**
  `LING_ONEBOT_WS_PORT` 现在会传进容器（`x-app-env`），但基础
  `deploy/compose.yaml` **不发布**这个端口（不配 WS 的部署不该多出一个对外端口）。
  要用服务端模式必须追加 `deploy/compose.ws.example.yaml`，否则容器内监听了、
  宿主上的 NapCat 连不进去，且日志里没有任何异常。另需
  `LING_ONEBOT_WS_TOKEN`（缺了服务端拒绝启动）与 `LING_ONEBOT_WS_HOST=0.0.0.0`。
  不配 WS 的部署**行为完全不变**。

### 安全
- 入站 OneBot 上报：未配 `LING_ONEBOT_SECRET` 时一律拒绝（原实现按"未加密
  上报"放行，等于无凭据放行）
- OneBot 反向 WS 服务端：未配 token 时**拒绝启动**（原实现只打 warning 后放行）
- 媒体下载（OneBot / 微信）：域名白名单改用
  `host == h or host.endswith("." + h)`（裸 `endswith` 会放行 `evilqpic.cn`
  这类仿冒域），并新增端口约束（只接受 http=80 / https=443）、大小上限与
  流式计数、不跟随重定向
- Web 上传的媒体引用（`file:`）：要求解析后落在媒体目录内（`is_relative_to`），
  拒绝 `file://` URI 形式、相对路径与非普通文件，并有大小上限

### 修复
- 容器镜像漏打 `connectors` 包，导致所有渠道在容器里都无法连接
- 容器与开发两条启动路径的日志配置不一致，容器内一条应用日志都没有
- 群聊里非主人说话时回复发不出去（会话归属判定过严）
- 媒体去重键未按会话隔离，导致后续上传取到第一张图
- 媒体引用被拒（越界/超限/形式不支持）不再被描述层降级成"可重试"：
  这类是永久错误，原实现每 60 秒重试一次、永远重试不完
- `LING_ONEBOT_WS_URL`（反向 WS 客户端模式）补进 compose 的 `x-app-env`
  白名单，原实现设了也进不了容器
