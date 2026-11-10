# 连接器「同协议多实例」改造面勘察

> 状态：**勘察报告（只读）**。本文只列改造面与事实，不含实现。
>
> 勘察基线：仓库 `/Users/lingxi/project/ling`，commit `5818d2f`。
> 本地 dev PG（`127.0.0.1:55432`，`.data/pg`）停在 `0063_deferred_replies`；本地 `.data/ling.db`（SQLite）停在 `0007_cross_instance`，**不含**连接器四表。
>
> **未连接 NAS / 生产库**。本文中「每个 scope 现在有几条数据」的部分，本地两个开发库的答案都是 **0 行**（四表全空），因此下文只给**代码推导的语义**，不拿本地数据冒充生产事实。
>
> 标注约定：
> - **【事实】** = 已读到代码/DDL/schema 原文，给出 `文件:行号`；
> - **【推断】** = 由事实推导、未经运行验证的结论（含风险判断与迁移策略）。
>
> 本文写作时工作区已有他人未提交改动（`clients/web-console/*`、`connectors/qq/ingress.py`、`docs/API-WEB-V1.md`、`docs/CONNECTOR-CONFIG-DESIGN.md` 等）。下文行号以**工作区当前内容**为准。

---

## 0. 先纠正初步核实里的三处偏差（其它条目属实）

| 初步核实说法 | 实际情况 | 依据 |
|---|---|---|
| 运行时 `_instances: dict[str, dict[str, Any]]`（scope → 实例名 → 对象）「看起来天然支持一个 scope 多实例」 | **形状像，语义不支持**。它是一个**"单实例快照"**：`apply()` 每次用 `new_instances` **整体替换** `_instances[scope]`，`_effective[scope]` 也只是一个参数 dict。实例名是硬编码组件名（`"onebot_ws_server"` / `"onebot_ws"` / `"onebot_connector"`），不是用户命名空间。 | `src/ling/kernel/connector_runtime.py:70,72,107,146,158-159`；`src/ling/app.py:352-353,370-371,380,410` |
| `ConnectorOverrideValue` 的 `UniqueConstraint("revision_id","field_key")` —— 无实例维度 | 属实，但**它本来就不需要实例维度**：覆盖值是"某个**修订**的完整覆盖集合快照"，修订本身才该带实例维度。真正缺实例维度的是**修订表**。 | `src/ling/infra/models.py:526-527` vs `:495-500` |
| HEAD 表「每个 scope 一行」 | 属实，且**是主键级**约束：`scope` 就是主键，不是普通唯一索引。 | `src/ling/infra/models.py:569`；迁移 `migrations/versions/0054_connector_config.py:112` |
| 「`_instances` 的 key 是实例名」 | `_instances` 外层 key 是 **scope**，内层 key 是 **组件名**（见上）。而代码里另有一个**对外**的"实例标识" `instance_id`（如 `onebot-default`），它只进入 `ActorAccount.connector_instance` 做账号命名空间隔离，**不参与任何运行时决策**。两套东西同名不同物，是这次改造最容易混的一处。 | `connectors/onebot/settings.py:74`；`src/ling/kernel/inbound_intake.py:215` |

---

## 1. 数据层改造面

### 1.1 四张表的完整字段 / 约束 / 索引

#### A. `connector_config_revisions`（`src/ling/infra/models.py:476-509`）

模型声明（`__table_args__` 在 `:490-493`）：

| 字段 | 类型 / 可空 | 模型行号 |
|---|---|---|
| `id` | String(64) **PK** | `:495` |
| `scope` | String(16)，NOT NULL，**`index=True`** | `:497` |
| `parent_id` | String(64)，nullable | `:499` |
| `state` | String(24)，default `pending`，**`index=True`** | `:500` |
| `created_by` | String(64)，nullable | `:501` |
| `created_at` | DateTime(tz)，NOT NULL（`default=utcnow`） | `:502` |
| `activated_at` | DateTime(tz)，nullable | `:503-505` |
| `error_code` | String(48)，nullable | `:507` |
| `error_safe` | Text，nullable | `:509` |

索引：`ix_connector_config_revisions_scope_state(scope,state)` `:491`、`ix_connector_config_revisions_created(created_at)` `:492`、`ix_connector_config_revisions_scope(scope)`（由 `index=True` 生成）、`ix_connector_config_revisions_state(state)`（由 `index=True` 生成）。
迁移侧建表与四个索引：`migrations/versions/0054_connector_config.py:56-80`。
**唯一约束：无**（除了 PK）。`state` 机：pending→activating→active / rolled_back / degraded（`:484-486`）。

#### B. `connector_override_values`（`src/ling/infra/models.py:512-556`）

| 字段 | 类型 / 可空 | 模型行号 | 表 DDL |
|---|---|---|---|
| `id` | String(64) **PK** | `:540` | `0054:84` |
| `revision_id` | String(64)，NOT NULL，**`index=True`** | `:541` | `0054:85` |
| `field_key` | String(64)，NOT NULL | `:543` | `0054:86` |
| `value_kind` | String(16)，NOT NULL | `:544` | `0054:87` |
| `plain_value` | JSON，nullable | `:549` | `0054:88` |
| `ciphertext` | Text，nullable | `:551` | `0054:89` |
| `preview` | String(32)，nullable | `:553` | `0054:90` |
| `audit_fingerprint` | String(64)，nullable | `:555` | `0054:91` |
| `created_at` | DateTime(tz)，NOT NULL | `:556` | `0054:92` |

约束（`:526-538`，DDL `0054:93-104`）：
- `UNIQUE(revision_id, field_key)` name=`uq_connector_override_key`（`:527`）——**注意 `column_has_default` 语义：迁移里是 `sa.UniqueConstraint(...)`，不是一个单独的 unique index**；
- `CHECK value_kind IN ('secret','text','switch','list')` name=`ck_connector_override_kind`（`:528-531`）；
- `CHECK (value_kind='secret' AND ciphertext IS NOT NULL AND plain_value IS NULL) OR (value_kind<>'secret' AND plain_value IS NOT NULL AND ciphertext IS NULL)` name=`ck_connector_override_secret_shape`（`:532-536`）；
- 索引 `ix_connector_override_revision(revision_id)`（`:537`）。

我在本地 dev PG 用 `\d connector_override_values` 核对了实际 DDL，与模型一致（`plain_value` 落成 PostgreSQL `json`，非 `jsonb`）。

#### C. `connector_config_heads`（`src/ling/infra/models.py:559-576`）

| 字段 | 类型 / 可空 | 模型行号 |
|---|---|---|
| `scope` | String(16)，**PRIMARY KEY** | `:569` |
| `active_revision_id` | String(64)，nullable | `:570` |
| `candidate_revision_id` | String(64)，nullable | `:571` |
| `generation` | Integer，NOT NULL，default `0` | `:573` |
| `updated_at` | DateTime(tz)，NOT NULL（`onupdate=utcnow`） | `:574-576` |

**没有** `__table_args__`、没有外键、没有额外索引。DDL：`0054:111-117`（本地 PG 核对：`connector_config_heads_pkey PRIMARY KEY btree (scope)`，仅此一个索引）。

#### D. `connector_runtime_acks`（`src/ling/infra/models.py:579-605`）

| 字段 | 类型 / 可空 | 模型行号 |
|---|---|---|
| `id` | String(64) **PK** | `:594` |
| `scope` | String(16)，NOT NULL，**`index=True`** | `:595` |
| `generation` | Integer，NOT NULL | `:596` |
| `replica_id` | String(64)，NOT NULL | `:597` |
| `phase` | String(16)，default `prepared` | `:599` |
| `health` | String(16)，default `unknown` | `:601` |
| `error_safe` | Text，nullable | `:602` |
| `updated_at` | DateTime(tz)，NOT NULL | `:603-605` |

约束：`UNIQUE(scope, generation, replica_id)` name=`uq_connector_ack_generation`（`:590`）；索引 `ix_connector_ack_generation(generation)`（`:591`）、`ix_connector_ack_scope(scope)`（`index=True`）。

**这张表当前是"死"的**：【事实】`ConnectorConfigService` 的 `replica_id` 默认值是字面量 `"local"`（`src/ling/settings/connector_service.py:313`），而调用方构造 service 时不传它（`src/ling/api/routes/settings.py:818-826` 只传 `settings` / `box` / `runtime`）。全库除 `connector_service.py` 与 `models.py` 外，`grep replica_id` 零命中。也就是说 `expected_count` 恒为 1、`ack_count` 恒为 `1 if active else 0`（`:804-805`），ACK 表里每代最多一行。多副本 ACK 是设计意图，**当前未实现**。

### 1.2 每个 scope 实际几条数据 / HEAD 行数语义

**【事实】本地两个开发库四张表全部 0 行**（SQLite `ling.db` 甚至没有这几张表；PG `ling` 库 `alembic_version=0063_deferred_replies`，四表 `select count(*)` 均为 0）。所以**本地无法提供"生产里每个 scope 几条"的证据**。

**【事实（代码语义推导）】**
- 修订：每 scope 0..N 行，**N 无上限**（每次有效改动 +1，`connector_service.py:458-465`）；`unchanged` 不建修订（`:435-449`）。
- 覆盖值：属于**修订**，每修订 ≤ 该 scope 的字段数（onebot 12 / qqbot 12 / access 2，`src/ling/settings/connector_fields.py:374-378`）。
- HEAD：**每 scope 恰好 0 或 1 行**（主键 = scope，`models.py:569`）。首次写入时才 `INSERT`（`connector_service.py:657-661`）。所以"从没改过配置"的部署，HEAD **一行都没有**——读侧全部走 `getattr(head, 'generation', 0) or 0` 兜底（`connector_overrides.py:143,349-350`；`connector_service.py:372,858-859`）。
- ACK：**每 (scope, generation, "local") 一行**，每成功激活 +1 行。

### 1.3 要支持多实例，每张表各需要改什么

核心原则（**这是本次改造的支点**）：**实例身份应该落在「修订」上，而不是落在「HEAD」上**。理由：HEAD 现在的语义是"这一 scope 现在生效的是哪一版"，它的正确多实例形态不是"多行"而是"多组指针"；而每个实例的配置差异本来就该由各自的修订承载。

#### A. `connector_config_revisions`：**必须加列** `instance_id`

- `ADD COLUMN instance_id varchar(16) NOT NULL DEFAULT 'default'`（长度 16 已够；与 `scope` 同宽）。
- 索引改为/补齐 `(scope, instance_id, state)` 与 `(scope, instance_id, created_at DESC)`；现有 `ix_..._scope_state` 可保留（对"某 scope 全部实例"的查询仍有用）。
- **不要**加 `UNIQUE(scope, instance_id, state='active')` 这类部分唯一索引来表达"每实例只有一个 active"——**权威指针在 HEAD**（`models.py:560-564` 的注释已经写明这个理由），加部分唯一索引会造出第二个真相源，回退时容易两边不一致。【推断】

#### B. `connector_override_values`：**不需要改**

- 它由 `revision_id` 唯一确定归属，而 `revision_id` 已隐含 `(scope, instance_id)`。`UNIQUE(revision_id, field_key)`（`models.py:527`）在多实例下依然正确。
- **不要**给它加 `instance_id`：那会让"同修订内同字段两行"变成合法，破坏 `ck_connector_override_secret_shape`（`:532-536`）所保护的"一字段一值"不变量。

#### C. `connector_config_heads`：**必须改主键**，`scope` → `(scope, instance_id)`

- Alembic 无 `ALTER PRIMARY KEY`，实际做法：`DROP CONSTRAINT connector_config_heads_pkey` → `ADD COLUMN instance_id varchar(16) NOT NULL DEFAULT 'default'` → `ADD CONSTRAINT connector_config_heads_pkey PRIMARY KEY (scope, instance_id)`。
- 这是**唯一能保证"generation 单调"与"If-Match 只有一个赢家"的形态**：`generation` 是**每实例**的（`models.py:572-573`、`connector_service.py:518` 的 `generation + 1`）。
- **替代方案（不推荐，但要列出来）**：保留 `scope` 单主键，把 `scope` 列改存复合键 `"onebot:default"`。这样零 DDL 改主键，但 `scope` 列会同时承载"协议名"和"实例名"两种含义，而**代码里到处是 `scope == cf.SCOPE_ONEBOT` 的直接比较**（`connector_overrides.py:293,297,562-563`；`connector_service.py:250,259`；`app.py:310-316,330`），任何一处漏改都会静默走错分支。**明确不建议**。

#### D. `connector_runtime_acks`：**建议改唯一键**，`(scope, generation, replica_id)` → `(scope, instance_id, generation, replica_id)`

- 不改也能跑（generation 是每实例递增的，理论上不同实例可能撞同一个 `(scope, generation, replica_id)`，见 §7 风险 R6），但会**串号**：实例 A 的第 3 代会挡住实例 B 的第 3 代，而 ACK 写的是 `phase='activated'`，表现为"切换成功但没 ACK"，进而影响 `ack_count`。
- 同时 `ADD COLUMN instance_id`，`ix_connector_ack_scope` 可保留。

#### E. 新增一张"实例目录"表（**这是本次改造真正的缺口**）

现状**没有任何地方存储"这个 scope 下有哪些实例"**：
- 实例名的唯一来源是 `LING_ONEBOT_INSTANCE_ID` / `LING_QQBOT_INSTANCE_ID` 两个**字段**（`connector_fields.py:290-296,317-323`），它们会被写进覆盖值，但**只是普通字符串字段**，不带生命周期、不能被枚举、不能表达"已删除"；
- HEAD 只在**首次保存**时才建行（`connector_service.py:657-661`），所以"创建的实例但还没保存过配置"在库里根本不存在；
- 用户要的"AstrBot 式左侧实例列表（名称 + 状态点 + 协议标识）"需要"名称"和"创建时间"两个属主，现在无处可放。

【推断】建议新增 `connector_instances`：
```
id            varchar(16) PK            -- 与 instance_id 同一命名空间，不可变
scope         varchar(16) NOT NULL       -- onebot | qqbot | access
display_name  varchar(64) NOT NULL       -- 左侧列表显示名
enabled       boolean NOT NULL DEFAULT true
created_at    timestamptz NOT NULL
created_by    varchar(64) NULL
deleted_at    timestamptz NULL           -- 软删：保留修订历史与审计
UNIQUE(scope, display_name)
CHECK (scope IN ('onebot','qqbot','access'))
```
不建触发器、不用 FK 级联：【事实】这四张表**现在一条外键都没有**（`0054:56-133` 全文无 `ForeignKey`），`parent_id` / `revision_id` / `active_revision_id` 都是"应用层保证的软引用"。加 FK 会让迁移在"历史孤儿行"上失败，而这类行在生产上无法确认不存在（我无法访问生产库）。

同时建议引入一个**可空**的 `scope_configs(scope PK)`，把 `access` 的语义显式化：`access` 是**每 scope 一份、全实例共用**的出入许可，不该被当成"可多开"的实例（见 §7 R8）。

### 1.4 老数据映射到 `instance_id='default'` 会不会撞唯一约束

逐表回答（均基于 `0054` 的 DDL 与模型）：

| 表 | 老数据形态 | 默认值 `'default'` 后是否冲突 | 理由 |
|---|---|---|---|
| `connector_config_revisions` | PK=`id`，无唯一约束 | **不会** | 加列不参与任何唯一键；`id` 仍唯一 |
| `connector_override_values` | `UNIQUE(revision_id,field_key)` | **不会**（且本表不加列） | 唯一键不含 scope/instance |
| `connector_config_heads` | **每 scope 0 或 1 行**（PK=scope） | **不会** | 改 PK 为 `(scope,instance_id)` 后，一个 scope 最多 1 行老数据 → `(scope,'default')` 最多 1 行 → 自然满足新 PK |
| `connector_runtime_acks` | `UNIQUE(scope,generation,replica_id)` | **不会** | 加列后老行全取 `'default'`，新唯一键 `(scope,instance_id,generation,replica_id)` 在老数据上是原唯一键的**超集**，冲突集合不会变大；而老数据内部本来就满足原唯一键 |

唯一的真实风险不在约束而在**语义**：【推断】如果生产在本次迁移前已经存在 `LING_ONEBOT_INSTANCE_ID` 被用户改成非 `onebot-default` 的部署，那么"老数据 → `instance_id='default'`"与"字段里的 `LING_ONEBOT_INSTANCE_ID=onebot-x`"会**分叉**：库里的 `instance_id` 是 `default`，而 actor 命名空间是 `onebot-x`。过渡期必须定义二者关系（建议：`instance_id` 成为唯一权威，`LING_*_INSTANCE_ID` 字段降级为"该实例的 actor 命名空间"，迁移时把 `instance_id` 初始化为字段值，而不是 `'default'`）。这条必须在迁移前**查一次**：
```sql
-- 只读，用于确认生产是否存在自定义 INSTANCE_ID 的覆盖
SELECT s.scope, o.plain_value
FROM connector_override_values o JOIN connector_config_revisions r ON r.id=o.revision_id
JOIN connector_config_heads h ON h.active_revision_id=r.id
JOIN (VALUES ('onebot','LING_ONEBOT_INSTANCE_ID'),('qqbot','LING_QQBOT_INSTANCE_ID')) s(scope,k) ON s.scope=r.scope
WHERE o.field_key = s.k AND o.plain_value <> '""';
```

---

## 2. 服务层改造面

### 2.1 每一个接受 `scope` 参数的函数（全量，含签名与行号）

#### `src/ling/settings/connector_overrides.py`

| 行号 | 函数 / 方法 | 当前签名 | 需要 `instance_id`？ |
|---|---|---|---|
| `:135` | `_head_row` | `async def _head_row(session: AsyncSession, scope: str) -> Any` | **必须**（`session.get(ConnectorConfigHead, scope)` 在复合 PK 下会直接报错，`models.py:569`） |
| `:141` | `active_generation` | `async def active_generation(session, scope: str) -> int` | **必须**（透传给 `_head_row`） |
| `:146` | `read_override_values` | `async def read_override_values(session, *, scope: str, box=None, include_secrets=False, revision_id: str | None = None) -> dict[str, Any]` | **必须**（`revision_id=None` 时 `session.get(Head, scope)`，`:162`） |
| `:323` | `resolve_field_values` | `async def resolve_field_values(session, *, scope: str, settings=None, onebot=None, qqbot=None, box=None, include_secrets=False, use_overrides=True) -> ResolvedConnectorConfig` | **必须**（`read_override_values` + `_head_row` 双重依赖，`:340-347`） |
| `:408` | `defaults_only` | `def defaults_only(scope: str) -> dict[str, Any]` | **不需要**（纯 `fields_for(scope)`，无 DB/实例概念；`fields_for` 只按协议取字段表） |
| `:436` | `value_fingerprint` | `async def value_fingerprint(box_key: bytes|None, *, scope: str, key: str, value: Any) -> str` | **不需要**（HMAC 域分离，`:447-451` 只把 scope 当命名空间标签；改它会**让所有历史指纹失效**，见 §7 R5） |
| `:558` | `ConnectorRuntimeSettings.with_values` | `def with_values(self, scope: str, values: dict) -> ConnectorRuntimeSettings` | **建议改**（见下） |
| `:601` | `current_access_snapshot` | `async def current_access_snapshot(session, *, settings=None) -> AccessSnapshot` | **不需要**（写死 `SCOPE_ACCESS`，`access` 不作多实例） |

另有两个**不接 `scope` 但按 scope 强绑字段**的构造函数（`:478-510` / `:513-540`）：`build_onebot_config_from(values)`、`build_qqbot_config_from(values)`。它们从 `values` 取 `LING_*_INSTANCE_ID` 作为命名空间（`:505`、`:531`），多实例下依然成立——**只要 `values` 来自正确的实例**。但 `:505` 的 `or "onebot-default"` 兜底意味着"实例没填 actor 名"时所有实例会共用同一个 actor 命名空间（见 §7 R4）。

#### `src/ling/settings/connector_service.py`

| 行号 | 函数 / 方法 | 当前签名 | 需要 `instance_id`？ |
|---|---|---|---|
| `:112` | `RuntimeSwap.apply`（Protocol） | `async def apply(self, scope: str, *, values: dict, operation_id: str) -> SwapOutcome` | **必须**（这是 service→runtime 的唯一入口） |
| `:161` | `ChangeRequest.build` | `@classmethod def build(cls, scope: str, body: dict) -> ChangeRequest` | **不需要**（只做静态校验：`spec.scope != scope` 的归属检查，`:178,190`；实例无关） |
| `:228` | `validate_full` | `def validate_full(scope: str, values: dict) -> None` | **不需要**（纯字段校验 + 跨字段组合，`:250-266`）；**但多实例会需要**它多一条"端口不得与其他实例冲突"的跨实例检查（见 §7 R1） |
| `:272` | `_current_effective` | `async def _current_effective(session, *, scope, settings, onebot, qqbot, box)` | **必须**（调 `resolve_field_values`） |
| `:326` | `ConnectorConfigService.resolved` | `async def resolved(self, session, *, scope: str, include_secrets: bool=False)` | **必须** |
| `:345` | `ConnectorConfigService.apply` | `async def apply(self, session, *, scope: str, change, expected_generation, actor_id)` | **必须**（锁 key、HEAD、ACK 全按它，`:365-371,657-661,690,698-701`） |
| `:545` | `_fingerprints` | `async def _fingerprints(self, scope, current, effective, change)` | **不需要**（仅转发给 `value_fingerprint`） |
| `:569` | `_same_effective` | `def _same_effective(self, scope, current, effective, change) -> bool` | **不需要** |
| `:599` | `_write_candidate` | `async def _write_candidate(self, session, *, scope, revision_id, parent_id, merged, actor_id)` | **必须**（写 `ConnectorConfigRevision(scope=...)`，`:617-624`） |
| `:666` | `_mark_active` | `async def _mark_active(self, session, *, scope, revision_id, generation, operation_id, health)` | **必须**（HEAD 写 `:690`、ACK 写 `:698-701,706-716`） |
| `:730` | `_mark_failed` | `async def _mark_failed(self, session, *, scope, revision_id, outcome, state)` | **必须**（HEAD 写 `:750`） |
| `:767` | `activation_payload` | `def activation_payload(*, scope, generation, active_revision_id, candidate_revision_id, last=None)` | **不需要**（纯 dict 组装；**输出契约**需要多一个 `instance_id` 字段，见 §4） |
| `:813` | `latest_revision` | `async def latest_revision(session, scope: str)` | **必须**（`where(scope==)` 会跨实例串味，`:822`） |
| `:829` | `pending_revision` | `async def pending_revision(session, scope: str)` | **必须**（同上，`:837-839`） |
| `:847` | `operation_view` | `async def operation_view(session, operation_id: str)` | **不需要入参**，但**内部必须**用 `revision.instance_id` 去取 HEAD（`:858`），否则会显示错的 generation |
| `:883` | `_revision_generation` | `async def _revision_generation(session, revision)` | **不需要入参**，但内部 `session.get(Head, revision.scope)`（`:887`）**必须**改成复合键 |

#### `src/ling/settings/connector_fields.py`

| 行号 | 函数 | 当前签名 | 需要 `instance_id`？ |
|---|---|---|---|
| `:389` | `fields_for` | `def fields_for(scope: str) -> tuple[FieldSpec, ...]` | **不需要**（字段表按**协议**定义，实例只是数据行） |
| `:396` | `is_known_scope` | `def is_known_scope(scope: str) -> bool` | **不需要** |
| `:381` | `spec_for` | `def spec_for(key: str) -> FieldSpec` | **不需要** |
| `:214/:221/:242/:254` | `_secret/_text/_switch/_list` | 私有构造器，`scope` 只用于给 `FieldSpec.scope` 打标（`:177`） | **不需要** |

**结论**：`connector_fields.py` **一行都不用改**。字段注册表是"协议 → 字段集合"的映射，本来就与实例数无关；这也正是"多实例"能低成本落地的原因——实例之间只是**同表不同行**。

#### 其它层

| 行号 | 位置 | 说明 |
|---|---|---|
| `src/ling/app.py:281,290,299` | 启动时三次 `resolve_field_values` / `current_access_snapshot` | **必须改**：一次启动要解析 N 份（N = 实例数），逐实例起组件 |
| `src/ling/app.py:318-410` | `_start_onebot` / `_start_qqbot` / `_start_access` | **必须改**（见 §3） |
| `src/ling/app.py:449-475,478-501` | `_plan_*` 工厂与 `register` 调用 | **必须改**（见 §3） |
| `src/ling/api/routes/settings.py:594-646` | `_connector_field_views` | **必须改**：现在按 `cf.SCOPES` 三路输出，要变成按实例 |
| `src/ling/api/routes/settings.py:649-664` | `_activation_views` | **必须改** |
| `src/ling/api/routes/settings.py:1019-1062` | `reset_connector_config` 的 `for scope in cf.SCOPES` | **建议改**：reset 语义在多实例下要重新定义（全实例？还是当前实例？），否则"重置"会跨实例清空所有人的凭据 |

### 2.2 `resolve_field_values` 的调用方（全量）

【事实】`.py` 源码内共 6 处（不含定义与 `__all__`）：

| 调用点 | 用途 | 多实例后的形态 |
|---|---|---|
| `src/ling/app.py:281-289` | 启动期解析 onebot 有效值（`include_secrets=True`） | **每实例一次** |
| `src/ling/app.py:290-298` | 启动期解析 qqbot 有效值 | **每实例一次** |
| `src/ling/settings/connector_service.py:282-290` | `_current_effective`：保存前取当前有效值 | 加 `instance_id` 透传 |
| `src/ling/settings/connector_service.py:333-341` | `ConnectorConfigService.resolved`（`apply` 的基线、reset 的 `to_clear` 计算） | 加 `instance_id` |
| `src/ling/settings/connector_service.py:413-422` | `apply` 里**不带覆盖的 env 基线**（`use_overrides=False`） | 加 `instance_id`（虽然 `use_overrides=False` 时不读覆盖，**但仍读 HEAD**，`:347`） |
| `src/ling/api/routes/settings.py:615-628` | `_connector_field_views` 里**两次** `use_overrides=False` 的 env 基线 | 实例无关的部分可保留；但每实例的字段视图仍需一次**带覆盖**的解析（`:634-641`） |

调用方的**间接**依赖：`read_override_values`（`connector_overrides.py:146`）被 `resolve_field_values:341` 与 `connector_service.py:400-402` 直接调用，两处都要加 `instance_id`。

### 2.3 哪些函数可以保持不动（**这条很重要**）

以下函数在多实例下**不应该**加 `instance_id`，加了反而是设计错误：

1. `value_fingerprint`（`connector_overrides.py:436`）——加实例维度会改变 HMAC 输入，让**所有历史审计指纹无法比对**（用户会看到"每个字段都变过"）。实例身份已经由 `revision_id` 隐含。
2. `ChangeRequest.build` / `validate_full`（`connector_service.py:161,228`）——纯字段级校验，与实例无关。**新增的跨实例校验应作为独立函数**（例如 `validate_instance_uniqueness(session, scope, instance_id, values)`），不要塞进 `validate_full` 破坏它"不碰数据库、不碰连接"的契约（`:162-165` 明文承诺 422 阶段 `stop()` 为 0 次）。
3. `connector_fields.py` 全体——见上。
4. `defaults_only`（`:408`）——纯默认值表。

---

## 3. 运行时改造面

### 3.1 `ConnectorRuntime` 四个方法的签名与实现要点

文件：`src/ling/kernel/connector_runtime.py`

| 方法 | 行号 | 签名 | 实现要点 |
|---|---|---|---|
| `register` | `:76-88` | `def register(self, scope: str, factory: Callable[[dict], Awaitable[SwapPlan]], *, instances: dict[str, Any] | None = None, values: dict[str, Any] | None = None) -> None` | `self._plans[scope]=factory`（`:84`，**覆盖式**，同名 scope 第二次注册会静默替换工厂）；`_instances[scope]=instances`（`:86`）；`_effective[scope]=values`（`:88`） |
| `snapshot` | `:90-92` | `def snapshot(self, scope: str) -> dict[str, Any]` | 返回 `dict(self._effective.get(scope) or {})` 的**浅拷贝**，`scope` 维度单键 |
| `apply` | `:96-174` | `async def apply(self, scope: str, *, values: dict, operation_id: str) -> SwapOutcome` | ①`factory is None` → 只记快照、`stop_calls=0`（`:109-112`，access 走这条）；②对 `old_instances` 逐个 `stop()`，**支持 sync/async**（`:118-126`）；③`plan = await factory(dict(values))` 再 `await plan.start()`（`:128-129`）；④异常 → `_rollback`（`:130-155`）；⑤成功 → **整体替换** `_instances[scope]` 与 `_effective[scope]`（`:158-159`）；⑥远端探活有界 3s，超时只影响 `health`（`:162-171`，常量 `:43,46`） |
| `_rollback` | `:176-191` | `async def _rollback(self, scope: str, old_values: dict) -> tuple[bool, dict[str, Any]]` | 用**旧值**重新走 `factory` → `plan.start()`；失败返回 `(False, {})` 且**不改** `_instances` |

`SwapPlan`（`:49-58`）：`stops: tuple[str, ...]`、`start: Callable[[], Awaitable[dict[str, Any]]]`、`connected: Callable[[dict], bool] | None`。

**一个必须知道的事实**：`plan.stops` 在本仓库**从未被使用**。`_plan_onebot` 传了 `stops=("onebot_ws_server","onebot_ws")`（`app.py:453`）、`_plan_qqbot` 传了 `stops=("qqbot_gateway",)`（`app.py:456`），但 `apply()` 的停止循环遍历的是 `old_instances`（`:118-119`），**根本不读 `plan.stops`**。所以 `SwapPlan.stops` 是一个纯文档字段。

### 3.2 工厂函数在哪注册、现在按 scope 生成什么形状

【事实】注册点全在 `src/ling/app.py`：

| 行号 | 调用 | 工厂 |
|---|---|---|
| `:458-462` | `register(SCOPE_ONEBOT, _plan_onebot, values=...)` | `_plan_onebot`（`:452-453`） |
| `:463-467` | `register(SCOPE_QQBOT, _plan_qqbot, values=...)` | `_plan_qqbot`（`:455-456`） |
| `:468-475` | `register(SCOPE_ACCESS, _plan_access, values={...})` | `_plan_access`（`:449-450`），`start` → `_start_access`（`:412-447`） |
| `:482-487` | `register(SCOPE_ONEBOT, _plan_onebot, instances=_onebot_instances, values=...)` | 启动期二次注册，把真实实例塞进 `_instances` |
| `:496-501` | `register(SCOPE_QQBOT, _plan_qqbot, instances=_qqbot_instances, values=...)` | 同上 |

注意 `:458` 与 `:482` 是**同一 scope 注册两次**（覆盖式，`:84`）——第二次才带上 `instances`。多实例化后这个"先注册再补实例"的双段式写法会直接变成 bug 来源（见 §7 R7）。

**当前形状**：`factory(values) -> SwapPlan`，`start()` 返回 `dict[组件名 → 对象]`，组件名硬编码：
- onebot：`"onebot_ws_server"`（`:352`）、`"onebot_ws"`（`:370`）、`"onebot_connector"`（`:353,371,380`）；
- qqbot：`"qqbot_gateway"`、`"qqbot_connector"`（`:410`）；
- access：`{}`（`:447`）。

而且 `_start_onebot` / `_start_qqbot` 在构造前就**直接写 `app.state.*`**（`:331,354-355,358,367-368,374,379,389,393,402-403`），这违反了同一文件里 `apply()` 注释所声称的"成功后才把引用替换成新的"（`connector_runtime.py:11,157`）——引用其实是**构造过程中**就被改掉的。

**多实例后应变成的形状**【推断】：
```
factory(values) -> SwapPlan
  start() -> dict[instance_key -> Runnable]     # 或用 dataclass InstanceBundle
instance_key = f"{scope}:{instance_id}"          # 全局唯一，取代硬编码组件名
app.state.connector_instances: dict[instance_key, Runnable]
```
`stop()` 只需对"本次要停下/替换的那一个 instance_key"生效，**不能**再整体遍历 `old_instances`（否则改 A 实例会把 B 实例一起停掉）。这是本次改造里**运行时层唯一的硬改动**：`apply()` 的停止集合必须从"scope 的全部实例"收窄为"该 instance 的组件"。

### 3.3 `_instances[scope]` 的 key 到底是什么 / 实例名从哪来

- **`_instances` 外层 key = scope**（`"onebot"` / `"qqbot"` / `"access"`，来自 `cf.SCOPES`，`connector_fields.py:59`）。
- **内层 key = 代码硬编码的组件名**，不是用户可配的实例名（见 §3.2 行号）。
- 用户可见的"实例名"实际上有**两个互不相干**的来源：
  1. `LING_ONEBOT_INSTANCE_ID` / `LING_QQBOT_INSTANCE_ID` 字段（`connector_fields.py:290-296,317-323`），默认 `onebot-default` / `qqbot-default`，进入 `OneBotConfig.instance_id`（`connectors/onebot/settings.py:74`）→ `ActorAccount.connector_instance`（`inbound_intake.py:215`）与 `window_ref` 前缀（`connectors/onebot/message.py:228` 的 `f"{instance_id}:{kind}:{target_id}"`）;
  2. 环境变量同名项（`src/ling/config.py:253-255`、`src/ling/capabilities/channels.py:39`）。
- **关闭路径**用的是 `_instances`（`app.py:508-517`），所以多实例后关闭逻辑要改成遍历全部 `instance_key`。

### 3.4 重点判断：运行时层是否真的支持多实例？

**结论：不支持。形状像，语义不支持。** 四条代码事实：

1. **状态是"单值"而非"映射"级**：`_effective: dict[str, dict]`（`:72`）与 `_instances: dict[str, dict]`（`:70`）的**外层**都只有一个 scope 键。第二层在 `_instances` 里是组件名，在 `_effective` 里是**字段名**——两者维度不同，也就是说"多实例"在 `_effective` 里**连放的位置都没有**（一个 scope 只有一个参数 dict）。
2. **`apply()` 是整 scope 替换**：`:158-159` `self._instances[scope] = new_instances`、`self._effective[scope] = dict(values)`。哪怕 `new_instances` 里有 5 个名字，它表达的也是"**这一 scope 的当前实例群**"，没有任何"实例维度"的概念。
3. **停止语义是"全停"**：`:118-126` 遍历 `old_instances` 全部停下。两个 onebot 实例共存时，改实例 B 的 token 会把实例 A 的连接**一起停掉**，然后 `start()` 只重建 B —— 实例 A 直接消失，且 `Outcome` 报 `ok=True`。
4. **`register()` 的 `instances=` 是覆盖式**（`:86`），而 `app.py:458` 与 `:482` 对同一 scope 注册两次，正是依赖这个覆盖语义。

所以"看起来天然支持"这个判断需要**推翻**。运行时层需要改的不是"支持多实例的能力"，而是**"把实例维度加进 key 与停止集合"**这一件事——改动量不大，但不改就是静默的跨实例互相杀死。

---

## 4. API 改造面

### 4.1 现有连接器相关路由（全量）

文件：`src/ling/api/routes/settings.py`（`router` 前缀见文件头；前端调用在 `clients/web-console/src/api.ts:500-560`）

| # | 方法 + 路径 | 行号 | 当前行为 |
|---|---|---|---|
| 1 | `GET /v1/settings/connectors` | `:667-755` | 返回 `ConnectorsOut`（`schema_version="v2"`）：`connectors[]` 只含 **onebot / qqbot 两个渠道视图**（`src/ling/settings/connectors.py:452-462` 的 `connector_views` 固定返回 2 个），`access` 单独一段（`:749-754`）。每项带 `generation` + `activation` + `fields`（三 scope，`:700`）。**owner 之外不给 `preview`**（`:692,698`） |
| 2 | `PUT /v1/settings/connectors/{scope}` | `:829-970` | owner + 同源（`:855`）+ `is_known_scope`（`:856`，否则 404）+ `If-Match` 解析（`:860-871`，接受裸整数或带引号）+ `ChangeRequest.build`（`:874`）+ `service.apply`（`:886-892`）+ 三条审计事件（`:908-946`）+ `200`（unchanged）/`202`（其余）（`:950`） |
| 3 | `GET /v1/settings/connectors/operations/{operation_id}` | `:973-991` | owner only（`:984-987`），404 或 `operation_view` 结果 |
| 4 | `POST /v1/settings/connectors/reset` | `:994-1077` | owner + 同源（`:1015`），**串行遍历 `cf.SCOPES` 三个 scope**（`:1019`），逐 scope 算 `to_clear` 再 `apply(expected_generation=None)`（`:1037`）；任一失败 `ok=false` 但其它照做（`:1052`）。**注意 `expected_generation=None` 意味着 reset 绕过了 CAS** |

相关但不属"连接器路由"：`_connector_field_views`（`:594-646`）、`_activation_views`（`:649-664`）、`_require_connector_write_guards`（`:788-815`）、`_connector_service`（`:818-826`）、`ConnectorChangeBody`（`:776-785`）。

### 4.2 需要新增/修改的路由

【推断】建议的最小 API 面：

| 方法 + 路径 | 用途 | 备注 |
|---|---|---|
| `GET /v1/settings/connectors` **(改)** | 左侧列表 + 右侧详情 | 契约要加 `instances[]`（见下） |
| `POST /v1/settings/connectors/{scope}/instances` | 创建实例 | body: `{display_name}`；返回 `{instance_id, generation:0}`；**必须** owner+同源+`Content-Type: application/json` |
| `DELETE /v1/settings/connectors/{scope}/instances/{instance_id}` | 删除实例 | **软删 + 先停运行时实例**；必须带 `If-Match`（删除也是一次状态变更，见 §7 R3） |
| `PUT /v1/settings/connectors/{scope}/instances/{instance_id}` | **改现有 PUT 的形状** | 与旧 `/connectors/{scope}` 并存一段时间 |
| `POST /v1/settings/connectors/{scope}/instances/{instance_id}/activate` | 显式激活 | 现有 `PUT` 已经是"保存即尝试生效"（`:838-844`）；**如果保留这个语义，就不需要单独的 activate 路由**。AstrBot 的"创建 → 选中 → 展开配置"流程里"切换选中"只是前端状态，不是后端动作 |
| `POST /v1/settings/connectors/reset` **(改)** | 语义改为"当前实例"或"全部实例"二选一 | 现状 `expected_generation=None` 会绕过 CAS（`:1037`）；多实例下这是"一键清空所有实例凭据"的按钮，风险放大 |

保证向后兼容的路径：保留 `PUT /v1/settings/connectors/{scope}` 作为 `instance_id='default'` 的别名，**不要**先删再补。

**契约改动**（`src/ling/contracts/v1.py`）：
- `ConnectorView`（`:1535-1552`）加 `instance_id` / `display_name`；
- `ConnectorActivationOut`（`:1508-1533`）加 `instance_id`；
- `ConnectorAccessGroupOut`（`:1554-1562`）**不需要**加（access 不实例化）；
- `ConnectorChangeAccepted`（`:1585-1604`）、`ConnectorOperationOut`（`:1606-1619`）加 `instance_id`；
- `ConnectorsOut`（`:1564-1572`）建议加 `instances: list[ConnectorInstanceOut]` 而**不要**把现有 `connectors[]` 从 2 个变成 N 个——后者会让所有按 `id` 索引的前端代码（`settings.py:738-740`、`clients/web-console/src/types.ts:490-491`）一次性失效。

### 4.3 现有校验在多实例下是否要逐个改

| 校验 | 位置 | 多实例后 |
|---|---|---|
| **owner 检查** | `:799-805`（`_require_connector_write_guards`）、`:984-987`（operation GET） | **不用改**。owner 是全局角色，与实例无关 |
| **同源 / `Origin` 检查** | `:806-815`，复用 `src/ling/api/routes/version.py` 的 `_origin_problem` / `_require_json_content_type` | **不用改**，但**新路由必须逐个调用**——这是最容易漏的一类：新加的 `POST /instances` 若忘了调 `_require_connector_write_guards`，就等于开了一个只有 owner-cookie 即能触发的 CSRF 入口（`:791-792` 的注释解释了为什么 owner 不够） |
| **`If-Match`** | `:859-871`；前端 `clients/web-console/src/api.ts:525-532` | **必须改**：generation 已变成"每实例"，`If-Match` 必须与**路径里的 instance_id** 配对校验。若仍按 scope 读 HEAD，两个实例的 generation 会串（§7 R3） |
| **generation CAS** | `connector_service.py:381-386` | **必须改**：读 HEAD 要带 `instance_id` |
| **advisory lock** | `:365-368`（`pg_advisory_xact_lock(hashtext('connector:{scope}'))`） | **必须改**：锁粒度现在是**整 scope 串行**。多实例后若保持 scope 粒度，改实例 A 会阻塞改实例 B（可用但不必要地慢，且 A 的慢重连会拖住 B）；若改成 `connector:{scope}:{instance_id}`，则必须确认"跨实例互斥"没有被任何逻辑依赖——**目前没有**，所以可以收窄。【推断】 |
| **`is_known_scope` 404** | `:856-857` | **不用改**，但要新增 "instance 不存在 → 404" |
| **reset 的 `expected_generation=None`** | `:1037` | **必须改**：多实例下"无条件覆盖别的并发写"的窗口从 1 个 scope 放大到 N 个实例 |

---

## 5. 迁移改造面

### 5.1 当前最新迁移版本号

【事实】`migrations/versions/` 共 **63** 个 `.py`。最新是 **`0063_deferred_replies`**，`down_revision = "0062_memory_consolidate"`（链脚见 `migrations/versions/0063_deferred_replies.py`）。本地 dev PG 的 `alembic_version` 也是 `0063_deferred_replies`，与仓库一致。
连接器四表由 **`0054_connector_config`** 创建（`down_revision="0053_prompt_snapshots"`，`migrations/versions/0054_connector_config.py:48-49`），**此后没有任何迁移再碰过这四张表**（grep 四张表名于 `migrations/versions/`，除 `0054` 自身外零命中）。

### 5.2 新迁移要做什么（逐条 SQL 级）

拟编号 `0064_connector_instances`，`down_revision = "0063_deferred_replies"`。

```sql
-- 1) 实例目录（新建）
CREATE TABLE connector_instances (
    id            varchar(16)  NOT NULL,
    scope         varchar(16)  NOT NULL,
    display_name  varchar(64)  NOT NULL,
    enabled       boolean      NOT NULL DEFAULT true,
    created_at    timestamptz  NOT NULL,
    created_by    varchar(64),
    deleted_at    timestamptz,
    CONSTRAINT connector_instances_pkey PRIMARY KEY (id),
    CONSTRAINT uq_connector_instance_name UNIQUE (scope, display_name),
    CONSTRAINT ck_connector_instance_scope CHECK (scope IN ('onebot','qqbot','access'))
);

-- 2) 回填实例目录：每个"曾经有过 HEAD 或修订"的 scope 造一个 'default'
INSERT INTO connector_instances (id, scope, display_name, enabled, created_at)
SELECT 'default', s.scope, s.scope, true, now()
FROM (
    SELECT DISTINCT scope FROM connector_config_heads
    UNION
    SELECT DISTINCT scope FROM connector_config_revisions
) s
ON CONFLICT (id) DO NOTHING;
-- ⚠ 注意：上面这条在"onebot 和 qqbot 都有数据"时会给两个 scope 插同一个 id='default'，
--   撞 PK。实际必须用 (scope, instance_id) 或让 id = scope || ':default'。这是
--   本次迁移**最容易写错的一条 SQL**，见 5.4 的 id 命名空间讨论。

-- 3) 修订表加实例维度
ALTER TABLE connector_config_revisions
    ADD COLUMN instance_id varchar(16) NOT NULL DEFAULT 'default';
CREATE INDEX ix_connector_config_revisions_scope_instance_state
    ON connector_config_revisions (scope, instance_id, state);
-- 可选：父链仍按 id 走，无需改 parent_id

-- 4) HEAD 表改复合主键
ALTER TABLE connector_config_heads
    ADD COLUMN instance_id varchar(16) NOT NULL DEFAULT 'default';
ALTER TABLE connector_config_heads DROP CONSTRAINT connector_config_heads_pkey;
ALTER TABLE connector_config_heads
    ADD CONSTRAINT connector_config_heads_pkey PRIMARY KEY (scope, instance_id);

-- 5) ACK 表加实例维度并改唯一键
ALTER TABLE connector_runtime_acks
    ADD COLUMN instance_id varchar(16) NOT NULL DEFAULT 'default';
ALTER TABLE connector_runtime_acks DROP CONSTRAINT uq_connector_ack_generation;
ALTER TABLE connector_runtime_acks
    ADD CONSTRAINT uq_connector_ack_generation
    UNIQUE (scope, instance_id, generation, replica_id);

-- 6) （可选，推荐）覆盖值表不动
```

**不需要做的事**（列出以避免过度迁移）：
- **不要**回填 env 到数据库。`0054:36-40` 已明确写了理由（密钥多一份副本 + 改 env 静默失效），多实例**不改变这个理由**；
- **不要**给 `connector_override_values` 加列；
- **不要**给任何一张表加外键（现状零 FK，见 §1.3E）。

### 5.3 `downgrade()` 的可行性

`downgrade()` 把多实例折叠回单实例时，**必然丢失**以下信息（逐项）：

| 丢失项 | 是否可恢复 | 说明 |
|---|---|---|
| 除 `default` 外每个实例的**当前生效配置** | ❌ 不可恢复 | 折叠 = 删掉其它实例的修订行，或把它们标成非 active。值本身还在行里，但 `HEAD` 只有一个地址，**"哪个实例现在生效"这个事实没有地方表达** |
| 其它实例的**全部历史修订链** | ❌ | 若迁移选择 `DELETE` 非 default 修订，历史与审计就没了 |
| 其它实例的 **ACK 记录** | ❌（但价值低） | ACK 现在是"每 (scope,generation,'local') 一行"，本身已是弱证据 |
| 实例目录的 `display_name` / `created_by` / `deleted_at` | ⚠️ 若只降级 schema 不删数据则可保 | 取决于 downgrade 是否 `DROP TABLE connector_instances` |
| 多实例的 **actor 命名空间**（`instance_id` 字段值） | ✅ | 它在覆盖值里，属于修订数据，折叠时若保留 `default` 那一条则不受影响 |

**结论与建议**：

1. **明确拒绝而不是丢弃**。理由不是洁癖：【事实】`0054:36-40` 的迁移注释已经确立了本仓库的立场——"不要静默改变部署行为"。多实例折叠会**静默改变哪一路消息还能进来**（比如两个 NapCat，折叠后只有 default 那路的端口会重新监听），这是"用户以为回滚了、实际机器人半残"的典型。

2. 具体实现方式二选一：
   - **A（推荐）**：`downgrade()` 在检测到"任一 scope 存在非 `default` 实例（`instance_id <> 'default'` 或 `connector_instances` 有多行）"时，`raise RuntimeError("多实例配置不能无损回退：请先在设置页删除多余实例")`。只在单实例数据上放行。
   - **B**：`downgrade()` 先做一次**导出**（把非 default 实例的修订 + 覆盖值写成 JSON 落到 `op.get_bind()` 之外的文件或返回给调用方），再折叠。复杂、且容器内文件系统只读（`0054:8-13` 明确写了 `read_only`），基本不可行。

3. **必须在 downgrade 里做的一件事**：`instance_id` 列的 `DEFAULT 'default'` 被 `DROP COLUMN` 之后，**残留的 `connector_instances` 行**如果没删，会在下次 upgrade 时与新的 `INSERT` 冲突。所以 `downgrade()` 要按 **先 DROP 新表、再 DROP 列、最后恢复旧 PK/UNIQUE** 的逆序执行，且每一步都验证过约束名（`connector_config_heads_pkey`、`uq_connector_ack_generation` 是**具名**的，可以直接 `DROP CONSTRAINT`）。

### 5.4 迁移时的锁风险

【事实】四张表都**没有外键引用任何业务大表**，只有 `connector_head` 的 PK 和无 FK 的 VARCHAR 列。行数规模（基于代码推导，**非生产实测**）：HEAD ≤ 3 行；ACK = 成功激活次数 × 1；修订 = 有效改动次数；覆盖值 = 修订数 × (≤12)。

【推断】因此：

| 操作 | 锁 | 风险 |
|---|---|---|
| `ADD COLUMN ... NOT NULL DEFAULT 'default'`（PG 11+） | `ACCESS EXCLUSIVE`，**但表重写不发生**（常量默认值走 metadata-only） | **低**。仅瞬时阻塞读写 |
| `CREATE INDEX`（非并发） | `SHARE`，阻塞写 | **低**（表小）；仍建议 `CREATE INDEX CONCURRENTLY` 以彻底避免与 `apply()` 期间的长事务相撞。**注意 `CONCURRENTLY` 不能跑在 Alembic 默认的事务里**，需要 `op.execute("COMMIT")` 或 `autocommit_block()` |
| `DROP CONSTRAINT` + `ADD PRIMARY KEY` | `ACCESS EXCLUSIVE`，**重扫索引** | **低**（≤3 行）；`ADD CONSTRAINT ... PRIMARY KEY` 会建新索引，表小则瞬间完成 |
| `ALTER TABLE ... DROP CONSTRAINT uq_connector_ack_generation` + 新 UNIQUE | `ACCESS EXCLUSIVE` + 重建唯一索引 | **低**（行数 = 激活次数） |
| `CREATE TABLE connector_instances` | 无 | **无** |
| 回填 `INSERT ... SELECT` | 行级 | **无** |

**真正的锁风险不在表大小，而在与在线写入的交叉**【推断】：
- `apply()` 会持有 `pg_advisory_xact_lock(hashtext('connector:{scope}'))` 并在**同一事务里**做 `commit`（`connector_service.py:365-368,466`），而插入候选修订与覆盖值也在这个事务里（`:458-465`）。如果迁移的 `ALTER TABLE` 撞上某个正在进行的长事务，`ALTER` 会排队等在 `ACCESS EXCLUSIVE`，**并且它后面所有对这张表的访问都会排在它之后**——也就是一次部署会把设置页写入（以及读 GET 的字段视图）全部堵住直到 ALTER 拿到锁。表小时窗口是毫秒级，但**排队效应**不是。
- 结论：**迁移应在低流量窗口执行，并且不要把多个 `ALTER TABLE` 拆成多次事务**（每个 `ALTER` 各自申请一次 `ACCESS EXCLUSIVE`，之间会被写入插队，反复失败）。

### 5.5 id 命名空间问题（必须在迁移里就定死）

【事实】`connector_config_heads.scope varchar(16)`（`models.py:569`）与 `connector_runtime_acks.scope varchar(16)`（`:595`）的宽度只有 16。若打算用 `"onebot:default"` 这类复合串塞进 `scope` 列（§1.3C 的替代方案），`"onebot:default"` 恰好 14 字符能塞下，但 `"onebot:my-long-name"` 会溢出。**所以强烈建议走"独立列"而不是"复合串"**，并在迁移里把 `instance_id` 定义为 `varchar(16)` 且加 `CHECK` 只允许 `[a-z0-9_-]`。

【推断】`instance_id='default'` 作为**每个 scope 各自**的默认实例是安全的，**前提是主键是 `(scope, instance_id)` 而不是 `instance_id` 单列**。`connector_instances.id` 若用单列 PK，就必须变成 `(scope, id)` 或让 id 带 scope 前缀——这正是 §5.2 第 2 条 SQL 里我标⚠的地方。

---

## 6. 配置与环境变量

### 6.1 连接器相关环境变量（全量）

#### (a) `Settings` 里显式解析的（`src/ling/config.py`）

| 环境变量 | Settings 属性 | 行号 | 默认 |
|---|---|---|---|
| `LING_ONEBOT_WS_PORT` | `onebot_ws_port: int|None` | `:109-115` | `None` |
| `LING_ONEBOT_WS_TOKEN` | `onebot_ws_token` | `:116-118` | `None` |
| `LING_ONEBOT_WS_URL` | `onebot_ws_url` | `:122-124` | `None` |
| `LING_ACCESS_MODE` | `access_mode` | `:129-131` | `"off"` |
| `LING_ACCESS_LIST` | `access_list: frozenset[str]` | `:133-139` | 空 |
| `LING_ACCESS_GROUPS` | `access_groups` | `:145-151` | 空 |
| `LING_LOG_ACCESS_DENIALS` | `log_access_denials` | `:169-171` | `True` |
| `LING_QQBOT_ENABLED` | `qqbot_enabled` | `:235` | `False` |
| `LING_QQBOT_APP_ID` | `qqbot_app_id` | `:236-238` | `None` |
| `LING_QQBOT_CLIENT_SECRET` | `qqbot_client_secret` | `:239-241` | `None` |
| `LING_QQBOT_BOT_SECRET` | `qqbot_bot_secret` | `:244-246` | `None` |
| `LING_QQBOT_WEBHOOK_PUBLIC_KEY` | `qqbot_webhook_public_key` | `:248-250` | `None` |
| `LING_QQBOT_INSTANCE_ID` | `qqbot_instance_id` | `:253-255` | `"qqbot-default"` |
| `LING_QQBOT_SANDBOX` | `qqbot_sandbox` | `:258` | `True` |
| `LING_QQBOT_API_BASE` | `qqbot_api_base` | `:260-262` | `None` |
| `LING_QQBOT_TOKEN_URL` | `qqbot_token_url` | `:263-265` | `None` |
| `LING_QQBOT_PROACTIVE` | `qqbot_proactive` | `:269-271` | `False` |
| `LING_QQBOT_SUPPORTS_GROUP` | `qqbot_supports_group` | `:272-274` | `True` |
| `LING_QQBOT_ALLOW_UNSIGNED_VALIDATION` | `qqbot_allow_unsigned_validation` | `:277-279` | `False` |

#### (b) 只在 `onebot_config()` 里直接读 `os.environ` 的（**不进 `Settings`**，`src/ling/capabilities/channels.py:25-46`）

| 环境变量 | 用途 | 行号 | 默认 |
|---|---|---|---|
| `LING_ONEBOT_MEDIA_HOSTS` | 媒体域名白名单（逗号分隔） | `:30-33` | `DEFAULT_MEDIA_HOSTS` |
| `LING_ONEBOT_API_BASE` | OneBot HTTP API 基址 | `:35` | `http://127.0.0.1:5700` |
| `LING_ONEBOT_ACCESS_TOKEN` | 调 OneBot API 的令牌 | `:36` | `None` |
| `LING_ONEBOT_SECRET` | HTTP 上报 HMAC 密钥 | `:37` | `None` |
| `LING_ONEBOT_SELF_ID` | 自己的 QQ 号 | `:38` | `None` |
| `LING_ONEBOT_INSTANCE_ID` | **actor 命名空间** | `:39` | `onebot-default` |
| `LING_ONEBOT_SUPPORTS_GROUP` | 能力位 | `:40` | `"1"` |
| `LING_ONEBOT_PROACTIVE` | 能力位 | `:41` | `"0"` |
| `LING_ONEBOT_WS_HOST` | WS 服务端监听地址 | `:45` | `127.0.0.1` |

#### (c) 用户感知但不在注册表里的相关开关

`LING_QQ_ENABLED`（`config.py:228`）、`LING_WECHAT_ENABLED`（`:229`）——它们不参与 OneBot 有效配置解析，但影响"连接器是否启用"的判断（`channels.py:152` 的文案）。

### 6.2 多实例后这些变量怎么解释

【推断】建议规则，按优先级：

1. **DB 有实例 → env 是该实例的基线**。`resolve_field_values` 的语义已经就是"DB 覆盖 > env 快照 > 默认"（`connector_fields.py:11-17`），多实例只是把这次解析**执行 N 遍**，每遍带不同的 `instance_id`。**`_env_baseline` 本身（`connector_overrides.py:275-317`）完全不需要改**——它产出的基线对每个实例都相同，这正是想要的（"我新加的实例默认继承我原来的 env"）。
2. **无前缀 env = 属于 `instance_id='default'`**。这是唯一能保证"老部署行为完全不变"的映射：老部署不创建任何实例 → 只有 `default` 存在 → 它读到的 env 和升级前**逐字节相同**。
3. **新增实例默认不继承 env**【推断，两可】：如果新实例也继承 env，两个 onebot 实例会抢同一个 `LING_ONEBOT_WS_PORT`，第二个必然 bind 失败（见 §7 R1）。建议"非 default 实例默认只继承**非独占型**字段（`LING_ONEBOT_API_BASE` / `SUPPORTS_GROUP` / `PROACTIVE` / `MEDIA_HOSTS`），不继承端口/令牌/URL"。这需要给 `FieldSpec` 加一个 `shared_with_instances: bool` 标记——**这是 `connector_fields.py` 唯一可能要多改的地方**（与 §2.1 的"一行不用改"冲突时，以"不继承独占字段"为准，因为端口冲突是硬失败）。

### 6.3 老部署（只有 env、没有 DB 覆盖）行为必须完全不变

**逐条论证【事实】**：

- 老部署：四张表全空（`SELECT count(*)` 都为 0，HEAD 无行）。
- 启动期：`resolve_field_values` 里 `read_override_values` 因为 `head.active_revision_id is None` → 返回 `{}`（`connector_overrides.py:161-165`）；`_head_row` 返回 `None`；`generation=0`、`active_revision_id=None`（`:347-351`）。**与加实例维度前完全等价**——只要新代码把 `default` 实例的 `head` 行查不到当作同样情况。
- 运行时：`_start_onebot` 的判定是 `if settings.onebot_ws_port or settings.onebot_ws_url`（`app.py:478`），仍是 env 直读；`_start_qqbot` 的 `if not runtime_settings.qqbot_enabled or not bot_config.configured`（`app.py:391`）读的是 env 派生值。**只要不把 env 回填进 DB、不改这两处判定条件，行为不变**。
- 迁移的 `ADD COLUMN ... DEFAULT 'default'` 在空表上是 no-op。
- **唯一会变的可观测面**：`GET /v1/settings/connectors` 的返回值形状（`ConnectorView` 多一个 `instance_id`、`ConnectorsOut` 多一个 `instances[]`）。**这必须保持向后兼容**：老前端按 `connectors[].id`（`"onebot"`/`"qqbot"`）索引（`settings.py:738`、`types.ts:490-491`），若把 id 改成复合键会直接 404/空白页。建议 id 保持 `onebot`/`qqbot`，另起 `instances[]`。

### 6.4 有没有"实例名 → 环境变量前缀"的约定空间

**目前没有，且不建议新增。** 依据：

- 现有 env 命名是**扁平的**（`LING_ONEBOT_*`，`LING_QQBOT_*`），没有任何前缀维度；
- 容器**根文件系统只读、未挂载 `.env`**（`migrations/versions/0054_connector_config.py:8-13` 明文记录），所以"改 env"在容器里根本不可能，env 只是**启动期快照**（`connector_overrides.py:282-286` 的注释也强调是快照而非实时读）；
- 若引入 `LING_ONEBOT__{NAME}__PORT` 这类双下划线前缀，会立刻制造两套真相（env 前缀 vs DB `instance_id`），而且 `connector_fields.py:8-9` 明确要求"只有注册表里的键能被写入"。

【推断】正确做法：**实例名只存在于 DB**（`connector_instances.id` + `display_name`），env 仅作为 `default` 实例的基线（§6.2）。如果确实需要为"第二个 NapCat"提供 env，用**显式索引**而非名字：`LING_ONEBOT_1_WS_PORT`、`LING_ONEBOT_2_WS_PORT`，并在启动时映射为 `instance_id = "onebot-1"`。但这是"为了不用界面而配"，与本次"页面上点 + 创建机器人"的方向相反，**建议不做**。

---

## 7. 风险清单（按严重度）

### R1【致命】两个实例抢同一个 bind 端口 → 有界 3 秒后报 `degraded`，而旧实例已被停掉

【事实】`_start_onebot` 用 `runtime_settings.onebot_ws_port`（`app.py:335`）做 `ws_server.start()`（`:348`）；`OneBotWsServer.start()` 在没有 token 时直接抛（`connectors/onebot/ws_server.py:181-186`），端口被占用时由底层 bind 抛异常，异常类型名字里通常不含 `bind`/`port`（uvicorn/starlette 的路径各异），于是 `_error_code_for`（`connector_runtime.py:194-201`）会落到 `connector_start_failed` 而不是 `bind_failed`。
【推断】多实例下，两个 onebot 实例若都继承 env 的 `LING_ONEBOT_WS_PORT=3001`（`app.py:478-481` 的环境），**第二个实例必然 bind 失败**；而 `apply()` 在此之前已经把**旧实例全部 stop 掉**（`connector_runtime.py:118-126`），回滚又要再起一次（`:184`）。结果是"改 B 实例 → A 实例的端口被释放 → 回滚重起 A 时若 B 还占着 → A 也起不来"。
**具体防护**：`validate_full`（`connector_service.py:228`）里加**跨实例端口唯一性 + 令牌唯一性检查**，并且在**停旧之前**校验，保证"422 阶段 stop 次数为 0"的既有承诺（该承诺在 `:162-165` 与 `:126-127` 都有明文）。

### R2【致命】`apply()` 的停止集合是"整个 scope"，改一个实例会静默杀死其它实例

【事实】`:118-126` 遍历 `old_instances`（`_instances[scope]` 的全集），`:158` 整体替换；`Outcome` 照样 `ok=True`（`:172-174`）。
【推断】这是本次改造**最容易漏改且最难排查**的一处：两个 NapCat 都在线，改其中一个的 token，另一个的 WS 服务端被 `stop()`，但页面显示"保存成功"，用户只会看到"另一个机器人怎么不回消息了"，日志里只有一条 `接入器配置已生效 scope=%s`（`connector_service.py:722-728`），**不含实例信息**。这条日志本身也要加 `instance_id`。

### R3【高】`If-Match` / generation 从"每 scope"变成"每实例"，但契约没变 → 两个标签页互相覆盖

【事实】`If-Match` 只解析成整数（`settings.py:860-871`），generation 来自 path 里的 scope 对应的 HEAD（`connector_service.py:371-372`）。前端把卡片上的 `generation` 当 `If-Match`（`clients/web-console/src/api.ts:513-532`）。
【推断】加实例维度后，如果路由路径变成 `/connectors/{scope}/instances/{instance_id}` 而 `If-Match` 解析与 CAS 还按 scope，就会出现：实例 A 保存成功把 generation 从 3→4，实例 B 的页面仍拿着 3，保存时 **A 被覆盖**或 **B 误报 409**。更糟的是 `reset` 直接传 `expected_generation=None`（`settings.py:1037`），**完全绕过 CAS**——多实例下这个"重置"是唯一一个能无条件覆盖所有实例的入口。
**具体防护**：CAS 校验必须 `(scope, instance_id, generation)` 三元组；`reset` 必须要求 `If-Match` 或明确标注"危险操作"并要求二次确认体（例如 body 里带 `confirm: "reset-all"`）。

### R4【高】actor 命名空间共用 → 两个 NapCat 下同一个 QQ 号被当成同一个人 / 同一账号

【事实】`instance_id` 默认 `onebot-default`（`connectors/onebot/settings.py:74`、`connector_overrides.py:505`），进入 `ActorAccount.connector_instance`（`inbound_intake.py:215`）；`window_ref = f"{instance_id}:{kind}:{target_id}"`（`connectors/onebot/message.py:228`）；事件去重键是 `scope=f"connector:{connector_instance}"`（`inbound_intake.py:216-220`）。
【推断】如果两个 onebot 实例**都没改 `LING_ONEBOT_INSTANCE_ID`**（很可能，因为它是"实例标识"字段、默认有值且界面不显眼），则：
- 两路消息的 `window_ref` 完全相同 → **同一个群在两个 NapCat 下被并进同一个会话窗口**；
- 去重键 `connector:onebot-default` 相同 → 如果两个 NapCat 上报了同一个 `external_event_id`（同一个 QQ 号登录两处时确实会），**第二条被当成重复丢弃**；
- 账号绑定（`ActorAccount`）共用命名空间 → 身份混淆，而代码注释明确说这是要避免的（`connectors/onebot/settings.py:52-54`、`connectors/qqbot/settings.py:39`）。
**具体防护**：创建实例时**强制**要求一个非默认的 actor 命名空间（可以在 `POST /instances` 时自动派生，如 `onebot-{short_id}`），并把它作为**不可编辑或警告性编辑**的字段。这是"多实例"能否真正可用（而非只是能配两个端口）的**分水岭**。

### R5【中】给 `value_fingerprint` 加 `instance_id` → 全部历史审计指纹失效

【事实】`value_fingerprint` 的 HMAC 输入是 `json.dumps({"scope","field","value"}, sort_keys=True)`（`connector_overrides.py:447-451`）。审计里存的是 `旧指纹>新指纹`（`connector_service.py:566`）。
【推断】一旦把 `instance_id` 放进 HMAC，历史行里的旧指纹与新算出的新指纹**永远不相等**，用户会看到"每个字段都被改过"，而且审计本身无法区分"真改了"和"算法变了"。**不要改它**（与 §2.3 一致）。

### R6【中】ACK 唯一键不含实例 → 不同实例的第 N 代互相串号

【事实】`UNIQUE(scope, generation, replica_id)`（`models.py:590`），写入时 `replica_id` 恒为 `"local"`（`connector_service.py:313`，且 `settings.py:822-826` 不传）。
【推断】两个实例都从 0 开始计数，实例 A 激活到第 1 代后，实例 B 激活第 1 代时 `_mark_active` 的"查已有再更新/插入"（`:696-720`）会**命中 A 那一行**并把它的 `health` 改成 B 的。当前 `ack_count` 是硬编码的（`:804-805`）所以**暂时没有可见故障**——但这是一种"数据已经错了、只是没人读"的状态，一旦将来真做多副本 ACK，会变成难查的问题。迁移里一并修（§5.2 第 5 条）。

### R7【中】`app.py` 的"同一 scope 注册两次"会变成 bug

【事实】`app.py:458-462` 先 `register` 一次（无 `instances`），`:482-487` 再 `register` 一次（带 `instances`），靠 `_plans[scope]=factory`（`connector_runtime.py:84`）的覆盖语义工作。
【推断】多实例时若对每个实例各 `register` 一次，**最后一次注册会覆盖前面的工厂**，而这个工厂通常是闭包里捕获了某个 `instance_id` 的 lambda——表现为"所有实例都按最后一个实例的 factory 切换"。必须把 `_plans` 的 key 也改成 `instance_key`，或在工厂内部按 `values` 里的 `instance_id` 分发。**这是静默错误**，不会抛异常。

### R8【中】`access` 被当成"可多开实例" → 出入许可出现两套真相

【事实】`access` 的"切换"不重建连接，只是 `dataclasses.replace` 换掉 `app.state.settings`（`app.py:412-447`）；授权判断读的是 `app.state.settings.access_mode/access_list`（`inbound_intake.py:171-176`），**只有一个全局快照**；`allow_groups` 本轮仍走 env（`connector_overrides.py:606,612`）。
【推断】"一个 scope 下可以有多个 access 实例"在语义上不成立：两份白名单同时生效没有定义（并集？交集？按 bot？）。用户需求里把 `access` 与 `onebot`/`qqbot` 并列是自然的（都叫 scope），但**实现上必须把 access 设计成"单例 + 不可创建实例"**，否则会造出"改了一个 access 实例、另一个仍然生效"的诡异状态。建议在 API 层直接对 `access` 拒绝 `POST /instances`（400/422），而不是留一个能点但语义未定义的按钮。

### R9【低-中】`ws_server` 的路径没有实例命名空间 → 两个 NapCat 连同一个端点无法区分

【事实】`OneBotWsServer._handler` 从 `ws.request.path` 取路径（`connectors/onebot/ws_server.py:148`），只用于取 `access_token`（`:97-101`），**不按路径区分实例**；`clients` 计数是单值（`:70,155,166`）。
【推断】两个 WS 服务端各自 bind 不同端口时没问题（端口即区分）；但如果将来想"一个端口、多个实例按路径区分"（`/ws/onebot-a`），现在的实现做不到，且 `_authorized` 只比令牌、不比实例。**本期不要尝试单端口多实例**，明确"一实例一端口"。

### R10【低-中】recall / 出站只认"渠道"不认"实例"

【事实】`_connector_for_window`（`src/ling/api/routes/recall.py:60-76`）按 `window_title` 前缀 `onebot`/`qqbot`/`qq` 选 `app.state.{channel}_connector`，**完全忽略实例名**（`:71-76`）；`app.py:367-371` 与 `:402-403` 把 `onebot_connector` / `qqbot_connector` 覆盖成"最后一个构造的那个"。
【推断】多实例后，撤回 bot A 的消息可能拿到 bot B 的连接器 → 去 B 的 NapCat 调 `delete_msg`，拿到"消息不存在"。而 `delivery` 记录的 `instance` 是**正确的**（`capabilities/delivery.py:198`），**记录是对的、执行用错了**——这类错最难查。同理，`channels.py` 的 `build_onebot_connector()`（`:49-52`）**只读 env**，完全不看 DB 覆盖，所以"只走 HTTP 上报"的部署（`app.py:489-494`）在多实例下会永远发不出去第二条线。

### R11【低】迁移里 `INSERT` 回填实例目录的 PK 设计错误

见 §5.2 第 2 条的 ⚠：`id='default'` 单列 PK 在两个 scope 都有数据时撞主键。**这是纯 SQL 层面的、迁移一跑就失败的错误**（好在是 fail-fast）。

### R12【低】前端把 `id` 硬编码为 `"onebot"`/`"qqbot"`

【事实】`settings.py:723-743` 用 `view["id"]` 当 `ConnectorsOut.connectors[]` 的键与 `activations` 的下标；`clients/web-console/src/types.ts:490-491` 的 `ConnectorView.id` 承载同样语义。
【推断】若把"实例"塞进这个数组（让 N 个 onebot 各占一项），所有按 `id` 索引的代码与所有 SSR fixture（`clients/web-console/src/connectors.fixtures.test-helpers.tsx:263` 注释写明"三个 scope"）都会需要改。建议**新增 `instances[]` 数组**而不是复用 `connectors[]`（与 §4.2 一致）。

---

## 附：本次勘察未能确认的事项（需在生产侧只读核实）

1. 生产四表行数与各 scope 的 HEAD 行数——本地库全空，**无法代替**；
2. 生产是否存在 `LING_ONEBOT_INSTANCE_ID` / `LING_QQBOT_INSTANCE_ID` 被改成非默认值的覆盖（决定 §1.4 的映射策略）；
3. 生产 `connector_runtime_acks` 是否真的只有 `replica_id='local'`（代码推导是，但生产若有手工数据则不成立）；
4. 生产是否已存在"同一 scope 多套端口"的意图（例如 compose 里映射了多个端口）——这决定 R1 的紧迫度。

以上任一项需要在生产机**只读**执行 SQL 才能回答，本次按要求未连接任何生产库。
