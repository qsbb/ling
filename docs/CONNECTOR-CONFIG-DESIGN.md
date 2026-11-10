# 接入器可配置设计（OneBot / QQBot）

> 设计稿，2026-11-09；只定义契约与验收，不表示代码已实现。现状依据本机诊断报告 `shots/diag-connector-config-2026-11-09.md`。
> 外部参考以本日读取的上游 `master` / `main` 源码为准；分支会变化，链接指向具体文件与行区间。

## §1 结论

- **存哪**：新增 PostgreSQL 接入器覆盖层；有效值按 DB 已生效覆盖 > 容器 env > 代码默认值解析，页面永不写 `.env`。
- **怎么生效**：保存时校验、生成候选修订、逐副本重建受影响的连接器；全部确认后发布，失败自动恢复上一份可用配置。
- **密钥怎么显示**：仅回显最多前 7 个 Unicode 字符加固定遮罩、`configured` 标志；短密钥至少遮住 1 字符，永不回传完整密钥。
- 本项目容器是非 root、只读根目录且未挂 `.env`（`deploy/compose.yaml:155-166,214-215`），所以“网页写 env 后重启”不可行。
- 本设计取 AstrBot 的平台局部重载与 MaiBot 的配置变更回调思路，但另加密文存储、候选修订和自动回退；这三项是 **ling 的设计选择**，不是对参考项目能力的断言。
- 生效对象是 OneBot HTTP/WS、QQBot 网关和入站白名单的**有效配置**，不是仅把页面值写进数据库。
- 使用者看到的“保存成功”必须以最终 active 状态为准；提交请求受理仅表示正在尝试。
- **§1 验收**：在只读根文件系统部署下，PUT 后 DB 出现候选及最终修订；宿主 `.env` 校验和不变；成功无需重启 API 容器。

## §2 AstrBot / MaiBot 事实对照

| 项目 | 配置存储（事实） | 生效方式（事实） | 密钥回显（事实与边界） |
| --- | --- | --- | --- |
| AstrBot | 主配置 `data/cmd_config.json`；`platform` 数组含 `enable`、OneBot `ws_reverse_token` 和 QQ 官方 `secret`。见 [路径定义](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/config/astrbot_config.py#L20-L23)、[模板](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/config/default.py#L343-L379)。 | Dashboard 的平台更新先保存配置，再调用 `platform_manager.reload(config)`；该方法终止旧平台实例、启用时加载新实例。见 [保存服务](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/dashboard/services/config_service.py#L1422-L1453)、[重载实现](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/platform/manager.py#L287-L319)。**平台保存即局部重载，不要求整进程重启。**插件配置保存后的热重载是另一条路径，不能混为一谈。 | 平台列表/详情返回配置字典副本；通用 `secret` 元数据令输入框使用 `password` 类型、可点眼睛显示。没有在这条平台读取链路看到服务端统一脱敏。见 [读取服务](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/dashboard/services/config_service.py#L1401-L1417)、[输入控件](https://github.com/AstrBotDevs/AstrBot/blob/master/dashboard/src/components/shared/ConfigItemRenderer.vue#L352-L357)。推断：有权限的 Dashboard 客户端可能收到原值；具体每个适配器字段是否标为 `secret`，**不确定**。 |
| MaiBot | 主配置为 `config/bot_config.toml`；WebUI 还有按路径读写独立适配器 TOML 的接口；内置 QQ 官方适配器用插件配置模型，`app_secret` 标为密码控件。见 [主配置路径](https://github.com/Mai-with-u/MaiBot/blob/main/src/config/config.py#L70-L75)、[适配器文件接口](https://github.com/Mai-with-u/MaiBot/blob/main/src/webui/routers/config.py#L1809-L1866)、[QQ 字段](https://github.com/Mai-with-u/MaiBot/blob/main/src/plugins/built_in/qq_official_adapter/settings.py#L15-L26)。 | 主配置写入 `bot_config.toml` 后由文件监视/配置管理器热重载并通知回调；`POST /config/bot` 本身仅写文件。内置 QQ 官方插件收到配置更新时 `_stop()` 再 `_start()`，开关决定是否启动。见 [保存路由](https://github.com/Mai-with-u/MaiBot/blob/main/src/webui/routers/config.py#L1503-L1525)、[监视与回调](https://github.com/Mai-with-u/MaiBot/blob/main/src/config/config.py#L533-L614)、[QQ 生命周期](https://github.com/Mai-with-u/MaiBot/blob/main/src/plugins/built_in/qq_official_adapter/plugin.py#L183-L214)。**独立 NapCat 适配器 TOML 保存后是否自动重启进程：不确定**，该接口只证明写文件。 | `GET /config/bot` 返回解析后的 TOML；`GET /adapter-config` 返回文件全文，故不能把前端密码控件当作服务端脱敏保证。见 [主配置读取](https://github.com/Mai-with-u/MaiBot/blob/main/src/webui/routers/config.py#L1325-L1337)、[适配器读取](https://github.com/Mai-with-u/MaiBot/blob/main/src/webui/routers/config.py#L1809-L1827)。QQ 控件的密码声明仅证明视觉遮挡，是否有其他服务端过滤：**不确定**。 |

- 事实与推断分界：两项目都有可写文件系统；其存储载体不能直接复制到 ling 的只读容器。两项目都展示“局部重载可行”，却都不构成 ling 失败自动回退的现成保证。
- AstrBot 的 `update_bot` 是“先持久化、再 reload”；其平台管理器的 reload 代码没有为新实例启动失败恢复旧实例的分支，因此不照搬其失败语义。
- MaiBot 的主配置更新路由仅返回“配置已保存”；文件监视器与回调是否完成需要另一层运行时状态判断，因此本设计也不以 HTTP 保存返回值替代生效确认。
- 关于 MaiBot 独立 NapCat 适配器的启动方式，WebUI 的配置文件接口不足以证明；本文不据此推导热重启按钮或命令。
- **§2 验收**：脚本提取表内官方 URL 并断言 HTTP 200，逐项匹配目标源码中的 `reload(config)`、`bot_config.toml`、`_stop()`/`_start()` 等证据词；文档断言不把插件重载当平台重载，也不宣称独立 NapCat 配置必定热生效。

## §3 数据模型、范围与优先级

- 新 Alembic 迁移 `0054_connector_config`（实际编号以合并时迁移头为准）；不改现有 `config_revisions`，因为其 payload 可读且仅含模型类配置（`src/ling/settings/service.py:978-1032`）。
- 选**统一 key-value 覆盖表**，不建 OneBot/QQBot 两套宽表：字段都有相同的来源、清除、加密与审计语义；用固定字段注册表约束 key，拒绝任意环境变量名。
- `connector_config_revisions`：`id bigint PK`、`scope`（`onebot|qqbot|access`）、`parent_id`、`state`（`pending|activating|active|failed|rolled_back`）、`created_by`、`created_at`、`activated_at`、`error_code`、`error_safe`。
- `connector_override_values`：复合键 `(revision_id, field_key)`；`value_kind`、`plain_value jsonb NULL`、`ciphertext text NULL`、`preview text NULL`、`audit_fingerprint text`；CHECK 保证密钥只存密文，非密钥只存规范化值。
- 每个候选修订保存该 scope **完整覆盖集合快照**；未出现的 key 表示回退到 env，不写空串哨兵。修订历史只供内部回退，不进普通 GET。
- `connector_config_heads`：每 scope 一行，含 `active_revision_id`、`candidate_revision_id`、`generation bigint`；无修订时有效覆盖集合为空。
- `connector_runtime_acks`：`(generation, replica_id)` 唯一，含 `phase`、`health`、`updated_at`、`error_safe`；供多副本确认、超时和异常重启恢复。
- `field_key` 必须在服务端固定注册表中，并带 `scope`、类型、默认、敏感级别、解析器和校验器；迁移不从请求动态生成 schema。
- DB 对 `(revision_id, field_key)` 建唯一约束，并对 `scope`、`state`、`created_at` 建查询索引；active head 引用的修订不得物理删除。
- `plain_value` 仅允许 JSON 字符串、布尔、整数或字符串数组；名单使用有序去重数组存储，读取时转成现有 `frozenset[str]`。
- 密文字段的 `ciphertext` 采用现有 SecretBox 格式；每个字段单独加密，不能把整渠道 JSON 加密后使非密钥难以校验与比较。
- `preview` 是 UI 元数据，不是解密依据；审计指纹也不参与认证或连接，连接器只读取 `ciphertext` 解密后的值。
- 优先级：逐字段 `active DB override` > **进程启动时取得的 env 快照** > 现有默认值；清除单项覆盖是 DELETE 该 key 在下一修订中的记录，立即回退到 env；env 本身改变仍需重启容器。
- “默认值”沿用现有 `Settings` 与 `onebot_config()` 的默认定义，不在迁移时复制成一套容易漂移的数据库默认。
- 一个 scope 的修订只改该 scope 的字段；白名单 scope 参与同一套 generation 机制，但无需重连 QQ 网关或 OneBot WS。
- DB 覆盖可显式设 `false`、`0`、空名单，与“无覆盖”不同；密钥的“清空”定义为**删除覆盖并回退 env**，不允许写空密钥覆盖。
- OneBot 全字段：`API_BASE`、`ACCESS_TOKEN`、`SELF_ID`、`SECRET`、`WS_PORT`、`WS_TOKEN`、`WS_URL`、`WS_HOST`、`INSTANCE_ID`、`SUPPORTS_GROUP`、`PROACTIVE`、`MEDIA_HOSTS`（均加 `LING_ONEBOT_`）。
- QQBot 全字段：`ENABLED`、`APP_ID`、`CLIENT_SECRET`、`BOT_SECRET`、`WEBHOOK_PUBLIC_KEY`、`INSTANCE_ID`、`SANDBOX`、`API_BASE`、`TOKEN_URL`、`ALLOW_UNSIGNED_VALIDATION`、`PROACTIVE`、`SUPPORTS_GROUP`（均加 `LING_QQBOT_`）。
- 白名单本轮仅 `LING_ACCESS_MODE` 与 `LING_ACCESS_LIST`；`LING_ACCESS_GROUPS` 继续沿用 env，页面说明群名单暂不能在此编辑，绝不把群号混进用户号名单。
- `ACCESS_LIST` 保留字符串形式及前导零；UI 可逐行录入，API 接受规范化数组，不能经数字转换。
- 即使关闭 OneBot WS 服务端，HTTP 事件端点仍注册；“关闭连接器”不能被误读为关闭所有入站路径。
- OneBot 目前 `ACCESS_TOKEN`/`SELF_ID` 根本不在视图（`src/ling/settings/connectors.py:147-180`）；本设计将它们纳入字段注册表与 GET。
- **§3 验收**：迁移后空覆盖表的 GET 与升级前等价；对 `ENABLED=false`、空白名单和删除 `ACCESS_TOKEN` 覆盖分别断言 `db/env/default` 来源准确。

## §4 生效编排与状态机

```text
active(N) --PUT/校验--> pending(N+1) --> activating(N+1)
                                         | 全部副本确认 --> active(N+1)
                                         | 任一失败/超时 --> rolled_back(N+1)，active(N)
```

- 每次 PUT 先在内存合并提交字段与旧覆盖快照，校验**完整有效配置**，通过后写候选修订；不要求用户额外点“发布”。
- 一个请求只更改一个 scope；若要同时改 OneBot 凭据和白名单，页面依次提交并明确展示两个操作结果，不暗示跨 scope 原子性。
- candidate 的创建与 head 的 `candidate_revision_id` 更新放在一个短数据库事务；长连接的 stop/start 绝不在数据库事务中执行。
- 活动中的 candidate 不允许另一个相同 scope 的写入覆盖；旧 generation 的客户端必须重读后再提交。
- 校验包含类型/长度、`WS_PORT` 为 1–65535、URL scheme、普通 URL 无嵌入凭据、`ACCESS_MODE` 枚举、名单按字符串逐项 trim 去重、密钥必填配对和监听地址安全条件。
- 新密钥拒绝空值、控制字符和超出字段上限的值；token 不凭臆测限制其合法字母表。由此产生的 422 属于输入校验，远端拒绝鉴权属于异步生效失败。
- 对 OneBot 的 `WS_URL` 仅允许 `ws/wss`，`API_BASE` 和 QQBot 的 API/TOKEN URL 仅允许 `http/https`；生产对外地址要求安全 scheme 的策略须与现有部署兼容。
- 配置校验不向 QQ/NapCat 发探测请求；远端鉴权只能在连接器启动后验证，因此可能走异步失败回退。
- 对 `WS_PORT`/`WS_HOST` 的容器端口发布限制只能静态检查已知 compose 配置；运行时端口占用由绑定失败触发回退；不能声称 WebUI 能修改 Docker 端口映射。
- 编排器以 PostgreSQL advisory lock 串行同 scope 写入，持锁期间比较 `If-Match` 的 generation；不同 scope 可并行，涉及同一入站策略时按固定 scope 顺序加锁。
- 每副本先准备新配置与新实例；若新旧不能并存（同一 WS 监听端口），按 `stop(old) → construct(new) → start(new)`，在切换点原子替换 `app.state` 引用与路由解析快照。
- 能并存的 QQBot 出站连接也应避免同一机器人身份双连；默认仍先停旧再启新，空窗由状态页真实呈现。
- 旧实例引用与旧有效快照保留到全部副本完成切换；成功后再释放；失败时用旧配置重建，不能指望旧对象 stop 后可无条件再次 start。
- OneBot HTTP 入站与白名单判断均须通过同一已生效快照；不能只重建 WS 而让 `onebot_config()` 或 `inbound_intake` 继续读旧 `Settings`/`os.environ`。
- 请求处理时捕获一次代际快照，完整处理该请求；不能在验签取旧 token、授权名单却读到新 revision。
- QQ 网关及出站 WS 的 `start()` 只可能启动后台任务；“启动成功”与“远端已连”分开确认：本地已启动可确认切换，远端握手在有界等待内失败则回退；远端不可达而旧连接也不可达时标明诊断，避免无限卡在“生效中”。
- 所有副本从 DB 候选 generation 感知更新：`LISTEN/NOTIFY` 作提示、短周期轮询兜底；每个副本写 ACK；只有所需副本全部 ACK 才将 head 指向新修订。
- 所需副本集合取切换开始时仍有心跳的 API 副本；新副本启动先加载 active、再补当前 candidate；失联副本不能无限阻塞激活，应在有界超时后触发回退。
- 多副本同时监听同一端口时，按部署形态各自绑定容器内端口；端口冲突或反向代理路由不一致均视为生效失败，不靠单副本成功掩盖。
- 若副本在生效期间退出、超时、拒绝版本或重启后发现未决候选，协调器标记失败并令已切换副本按旧 active 修订恢复；页面只在完成最终 ACK 后显示“已生效”。
- 失败保留候选的安全错误、失败时间与字段名（无值）；head 保持旧修订，已切换副本执行 `stop(new) → construct(old) → start(old)`；回退本身失败则标为 `degraded` 并报警，不虚报成功。
- 长连接切换存在**可测但不能保证为零**的收消息空窗；不承诺 500ms 内远端重新连上。上游若有重发/续传按协议利用；无重放能力时不造“缓冲零丢失”承诺。
- v1 不建内存消息缓冲队列：HTTP 入站切换时短暂返回可重试状态，WS 依赖对端重连/重发；没有协议重发保证的消息可能丢失，页面与指标须如实说明。
- 记录 `disconnect_started_at`、`new_listener_ready_at`、`remote_connected_at` 三个时间点，用于量化中断窗口而不把“任务已创建”当“已连上”。
- 生效状态为 `pending`/`activating`/`active`/`rolled_back`/`degraded`；刷新页面从 DB 与各副本 ACK 恢复，不靠前端定时器猜测。
- **§4 验收**：把 OneBot token A 改 B 后，单副本本地代际在目标 500ms 内切换；`/v1/onebot/health` 或设置状态显示新指纹、旧实例已停；远端连接另按实测握手时间判断。
- **§4 验收**：含控制字符或超长的 token 在校验阶段 422 且 `stop()` 零次；模拟 `start()` 抛错，旧 revision、旧连接、旧白名单恢复，状态为“失败已回滚”。
- **§4 验收**：两副本中一副本失败或失联，candidate 不成为 active；所有存活副本最终报告旧 generation；`degraded` 必须可观测。

## §5 密钥处理与安全边界

- 用现有 `SecretBox` AES-GCM 加密（`src/ling/infra/security.py:110-127`）；密钥只在写入时参与加密、在**构造或恢复连接器的受控路径**解密；普通 GET、审计、状态机与日志不解密。
- 启动时从 env 得来的原值只留在进程内的受控配置快照；API 序列化层接收脱敏视图对象，不接收可被误序列化的配置 dataclass。
- `LING_ONEBOT_ACCESS_TOKEN`、`SECRET`、`WS_TOKEN`、`WS_URL`（可能携 token）、`LING_QQBOT_CLIENT_SECRET`、`BOT_SECRET` 必按密钥处理；`QQBOT_APP_ID` 按现有诊断口径视为敏感身份并遮罩。
- `WEBHOOK_PUBLIC_KEY` 是公钥，按普通文本展示；`API_BASE`/`TOKEN_URL` 必须拒绝 URL userinfo 或敏感 query，防止明文凭据混入普通字段。
- 写入时生成 `preview`：按 Unicode 字符取前 `min(7, len(secret)-1)` 个，再接固定 `••••`；1 字符密钥仅显示 `••••`。此 preview 单独落库，GET 不解密。
- 密钥输入不做 NFC 归一化或自动 trim，按原字节加密并送上游；若某上游凭据禁止空白，由该字段校验器明确拒绝。
- `preview` 显示的是当前**有效**密钥来源：DB 覆盖时读 DB 元数据，env 回退时用进程内 env 派生，不显示已删除候选密钥的预览。
- 对 env 密钥在进程内从启动快照计算同样 preview，永不持久化原文到 DB；`configured=false` 的 preview 为 `null`，`configured=true` 的短密钥至少留一字符不显示。
- 用户例子 `sk-1234a` 实际是 **8 个字符**；若原文更长，严格“前 7 位”应显示 `sk-1234••••`。文案以长度规则为准，不把 8 位例子当测试期望。
- 固定遮罩不暴露剩余长度；预览是有意允许的**前缀泄露**，不能说“API 零字符明文”。完整值、密文、root key 均不能出 API、前端状态、日志或审计。
- 浏览器不提供“显示现有完整密钥”按钮；眼睛按钮仅能显示本次尚未提交的输入值，保存后立即清空该输入控件。
- 服务器禁止把请求体打入访问日志；异常链中的 URL、Authorization、AppSecret 先经过结构化脱敏，再进入错误码映射。
- 旧值/新值指纹用独立派生的 HMAC-SHA256（按 scope/key/value 作域分离）截断显示；不用现有无盐 SHA 指纹直接审计低熵字段；页面只给 owner 查看短指纹。
- 根密钥丢失会使旧覆盖不可解密：启动 fail-closed 并提示恢复密钥/清除覆盖；不得自动回落 env 后声称 DB 覆盖仍有效。
- **§5 验收**：7 字符密钥显示至多 6 字符加遮罩且 `configured=true`，未配置为 `configured=false`；全量扫描 JSON/日志/审计找不到完整测试密钥。

## §6 API 契约、权限与审计

- `GET /v1/settings/connectors` 保留现有诊断结构，新增 `generation`、`activation`、`fields[].{key,kind,source,configured,preview,value,editable}`；非密钥 `value` 可回显，密钥 `value=null`。
- 示例仅列一个字段；实际 `connectors` 包含 OneBot、QQBot，新增顶层 `access` 存白名单组：

```json
{
  "schema_version": "v2",
  "as_of": "2026-11-09T00:00:00Z",
  "connectors": [{
    "id": "onebot", "generation": 12,
    "activation": {"state": "active", "active_generation": 12},
    "fields": [{"key": "LING_ONEBOT_ACCESS_TOKEN", "kind": "secret",
      "source": "db", "configured": true, "preview": "sk-1234••••",
      "value": null, "editable": true}]
  }],
  "access": {"generation": 3, "fields": []}
}
```

- `activation` 至少含 `operation_id`、`state`、`active_generation`、`candidate_generation`、`started_at`、`activated_at`、`ack_count`、`expected_count`、`error_code`，不含原始异常。
- 字段 `source` 限 `db|env|default`；`configured` 表示有效值非空，不等于“DB 有覆盖”；`preview` 仅用于敏感字段，不能放进可提交的字段值。
- 读接口需要登录；非 owner 的字段可读性按既有登录门槛保留，但敏感 preview/指纹只给 owner，其他身份只得 `configured`；写入口统一 owner。
- `PUT /v1/settings/connectors/{scope}`，`scope=onebot|qqbot|access`；请求头 `If-Match: <generation>`、`Origin`、`Content-Type: application/json`、`X-Ling-Connector: 1`。
- 请求体示例：`{"set":{"LING_ONEBOT_API_BASE":"http://napcat:5700","LING_ONEBOT_ACCESS_TOKEN":"新值"},"clear":["LING_ONEBOT_SECRET"]}`；未出现=不改，`set` 的空密码串=不改，`clear`=删除覆盖回退 env；同 key 同时 set/clear 为 422。
- `set` 对非密钥文本字段的空串代表显式空值，仅字段允许时接受；前端不得把空密码框序列化成一次密钥删除。
- 有效值未变化的请求返回当前 active generation 与 `unchanged`，不创建候选、不重连、不制造虚假的审计改动。
- `PUT` 同步返回 `202 {operation_id,generation,status:"activating"}`；客户端轮询 `GET /v1/settings/connectors/operations/{id}` 到 `active|rolled_back|degraded`，不能将 202 解释成已生效。
- 操作查询只返回无敏感值的状态；operation 属于 owner 会话可见范围，历史记录按保留期清理，但 active 修订和回退所需密文不能随便删。
- `POST /v1/settings/connectors/reset` 以同样门禁和候选修订机制清空三个 scope 的全部覆盖；操作需能显示预计会回退到哪些 env/default 来源，密钥仍不回显。
- 全局 reset 使用一个 operation 跟踪三个 scope：全部通过才清空全部 active head；任一 scope 重建失败则三个 scope 都保留原 active，避免“部分恢复默认”却报成功。
- 权限照更新通道：owner（`src/ling/api/routes/version.py:773`）+ 严格同源（`:647`）；Bearer/API 自动化若无浏览器 Origin，需明确独立策略，默认 fail-closed。
- 错误：401 未登录；403 `not_owner|bad_origin`；409 `generation_conflict|activation_in_progress`；422 `invalid_field|invalid_config`；503 `activation_unavailable`；异步启动失败落 operation 的 `rolled_back`，不伪装为 PUT 422。
- 每个错误包含稳定 `code`、中文 `message`、可选字段 `field` 与 `operation_id`；不把 Pydantic 原始输入值或上游异常字符串直接传给浏览器。
- 每次通过校验的改动记一条审计 `connector_config_changed`：操作者 ID、scope、字段名、动作、旧/新 HMAC 指纹、候选修订和结果；重连成功/回退另记 `connector_activated|connector_rolled_back`，绝不写原文或 preview。
- 并发由 DB 锁 + generation CAS 控制；同代际两次保存只有一个进入 activating，另一方 409 并拿新 generation；操作幂等键允许网络重试但不重复生成修订。
- **§6 验收**：非 owner、异源分别得到 403；并发 PUT 一成功一 409；审计只含字段名/指纹，抓包无完整密钥回显。

## §7 前端交互

- `ConnectorsPage.tsx` 保留运行诊断，上方改为 OneBot、QQBot、白名单三组表单；设置中心入口与“本页只读”文案同步改为可配置说明。
- 每组有自己的脏标记、保存与放弃；切换分组时未提交更改保留在页面内，离开页面前给出未保存提示。
- OneBot：`API_BASE`、`SELF_ID`、`WS_HOST`、`INSTANCE_ID` 用文本框；`WS_PORT` 数字框；`ACCESS_TOKEN`、`SECRET`、`WS_TOKEN`、`WS_URL` 密码框；`SUPPORTS_GROUP`、`PROACTIVE` 开关；`MEDIA_HOSTS` 逗号/逐行名单框。
- QQBot：`ENABLED`、`SANDBOX`、`ALLOW_UNSIGNED_VALIDATION`、`PROACTIVE`、`SUPPORTS_GROUP` 开关；`APP_ID`、`CLIENT_SECRET`、`BOT_SECRET` 密码框；`WEBHOOK_PUBLIC_KEY`、`INSTANCE_ID`、`API_BASE`、`TOKEN_URL` 文本框。
- 白名单：`ACCESS_MODE` 单选/下拉；`ACCESS_LIST` 每行一个字符串 ID，保留前导零；按现有 `ACCESS_MODE` 枚举展示，不能自造“黑名单模式”。
- 两种列表输入都在失焦时展示规范化预览：删除空行、提示重复项，不自动把空列表改成“未覆盖”。
- 密码框永远初始为空，不将 preview 当表单值；旁边显示 `已设置（sk-1234••••，来源：env/覆盖）` 或 `未设置`，提供“替换”和“恢复 env 默认”两个明确动作。
- 非密钥显示有效值及来源徽标；“恢复默认”指删除 DB 覆盖、显示回退来源，不能误导为清空 env；“放弃”仅丢弃未提交草稿。
- “恢复默认”分单字段与全局两个入口；全局入口先列出将回退的字段名与来源变化，再调用 reset API，确认行为而非密钥内容。
- 保存按钮仅在有变更时可用；按字段提示校验错误；提交后状态依次“正在验证…→正在生效…→已生效”，失败显示“失败，已恢复上一配置”及可操作原因。
- 对端口未发布提示“需在部署文件映射端口”；对 WS 未连提示“检查对端地址/令牌，连接仍在重试”；不展示 Python/socket 异常原文。
- 页面显示 active generation、最后生效时间、各副本确认数；刷新或离开再回来仍能看到未完成操作；非 owner 只见诊断与不可编辑状态。
- 无障碍标签不能把 preview 写进密码框的 `value`/`aria-label`；屏幕阅读器朗读“已设置，来源 DB，前缀 …”，不朗读遮罩以外内容。
- **§7 验收**：每类字段控件类型与清单一致；短密钥/未设置视觉可区分；保存、放弃、恢复默认三条路径均有交互测试；失败提示不含异常栈或密钥。

## §8 迁移与兼容

- 老部署升级后覆盖表为空，连接器仍按 env + 默认值启动；不导入 env 明文进 DB，不修改现有 `.env` 或 `Settings` 配置格式。
- 迁移为只加表和索引，不做数据回填；旧环境变量仍是部署者控制的最低配置基础。
- 两份 `deploy/compose.yaml`、`deploy/compose.image.yaml` 都补 `LING_QQBOT_BOT_SECRET`、`WEBHOOK_PUBLIC_KEY`、`API_BASE`、`TOKEN_URL`、`ALLOW_UNSIGNED_VALIDATION` 的 env 白名单；这是独立兼容 bug，须先修。
- 现有 `Settings` frozen 快照可继续承载启动基础配置，但所有接入器构造、OneBot HTTP 懒构造、白名单入站判断、诊断页必须统一调用有效配置解析器。
- 首次升级与重启时读取 active head 构造连接器；未完成的 candidate 由协调器判超时并恢复旧 head；不让半完成修订在重启后自行成为 active。
- 若新版本数据库迁移尚未完成，进程按现有启动失败策略报错，不悄悄丢弃 DB 覆盖再回退 env。
- 通过页面“一键回到 .env”或 reset API 清除全部 DB 覆盖；清除也经历校验与重建，失败则保留旧 active 覆盖并解释原因。
- 现有只读测试与类型契约须同步更新，但“密钥原文不进完整 JSON”测试保留并补短密钥测试。
- **§8 验收**：env-only 数据库迁移前后有效字段/连接状态一致；五个 QQ 变量用 compose 配置解析可见；reset 后全部 `source` 变回 `env/default`。

## §9 实施顺序与程序化验收

1. 先建迁移、字段注册表、解析器、SecretBox 密文与安全 preview；用最小 DB 集成测试覆盖覆盖优先级、删除回退、短密钥与 root key 错误。
2. 修两份 compose 白名单，接入构造与入站读取点；启动 env-only 回归通过后，保证单进程同一代际不混用新旧配置。
3. 实现候选修订、单副本重建与自动回退，再扩展多副本 ACK/超时；故障注入覆盖端口占用、QQ 认证失败、WS 断线、进程中途退出。
4. 加 owner/同源/最近登录 API、指纹审计、前端表单与状态；更新 API 文档、设置分类、原只读测试断言。
5. **验收矩阵**：OneBot 12 字段、QQBot 12 字段、白名单 2 字段逐项验证 GET 来源、PUT、clear、重启恢复；自动生成矩阵，不靠人工勾选。
6. **具体例 A**：env 里 token 为 A，页面写入 B 并生效后 GET 为 `source=db` 且指纹指向 B；点清除后回到 A，DB 不存 A 的密文副本。
7. **具体例 B**：改 `WS_PORT` 为已占用端口，操作终态为 `rolled_back`，旧端口继续监听；改为 compose 未映射端口时页面提示外部无法连入。
8. **具体例 C**：QQBot 关闭 `ENABLED` 后网关任务停止、HTTP 配置视图仍可读取；再次启用时使用有效凭据重建，不复活过期任务。
9. **具体例 D**：白名单从 A 改 B 后，新请求按 B 判定；在途请求维持其进入时捕获的代际，不出现半次请求混用规则。
10. **切换时延**：本地 revision 和实例切换在单副本无外网阻塞条件下目标 ≤500ms；连接远端用独立超时和状态衡量，不把网络抖动算作本地配置未应用。
11. **安全回归**：静态扫描响应、审计与日志不含完整密钥；模拟两副本冲突与失败均不出现一副本永久留在未发布修订。
- **§9 验收**：阶段 1–4 各有可执行测试和 `git diff --check`；最终全量 pytest、前端测试与构建通过，故障注入矩阵全绿。

## §10 不做与风险

- 不让 WebUI 写宿主 `.env`、修改 Docker 端口映射或调用 Docker 重启；这些操作不属于容器现有权限。
- 不复用模型 `config_revisions.payload` 存渠道密钥；不把密钥全量值塞入浏览器、历史 JSON、错误、审计或 URL query。
- 不承诺 WS 重连零消息丢失、远端 500ms 必连上，也不承诺所有上游适配器都有消息重放；这取决于对端协议与网络。
- 端口变更、密钥轮换可能造成短暂中断；多副本逐个切换有瞬时混合代际，要求限时收敛并在失败时整体回退。
- 自动回退的保证范围是应用可控的 DB 修订与连接器实例；外部平台已经撤销的旧 token、上游不可达或 Docker 未发布端口，无法由本进程修好。
- 历史修订中的密文会延长旧凭据留存时间；需要保留期限、备份加密和 root key 轮换操作手册，不能因“已经回滚”就当旧密钥已消失。
- `LING_ACCESS_GROUPS` 本轮仍由 env 管理；若未来纳入，应单独审查群号和用户号的授权语义。
- 短密钥前缀策略与用户给的 8 字符示例存在字面冲突，本文明确以前 7 字符且至少遮 1 字符为验收规则；若产品最终决定完整显示 7 字符密钥，须另做风险决策。
- **§10 验收**：自动检查 API schema 不含 `.env` 写回或 Docker 重启端点；故障注入报告明确记录中断窗口、混合代际时长和回退结果，不输出零丢消息的虚假断言。
