# WP-02 ORM 与迁移一致性

## 观测对象

2026-11-21 00:02:08（北京时间），工作树干净，`git log -1` 为
`1142bb92cd94639487445945bd883b981315af67 refactor(config): 清除停止生效的群观察配置`。
在同一个隔离 PostgreSQL 上用两个空库分别执行 `alembic upgrade head` 和
`create_all_for_dev()`；以下业务结构计数不含 `alembic_version`。没有读取生产实例。

| 阶段 | 表 | 列 | 约束 | 索引 |
| --- | ---: | ---: | ---: | ---: |
| 迁移至 `0079_manual_names_and_order` | 91 | 1,008 | 215 | 300 |
| 原 `create_all_for_dev()` 冷启动 | 87 | 959 | 197 | 279 |
| 先显式导入全部 ORM，再 `create_all_for_dev()` | 91 | 1,008 | 203 | 300 |

## 处理决定

以已发布的迁移为准。修正 ORM 的建表定义并统一注册路径；不修改已发布迁移，也不需要新增迁移。
保留现有 Python 插入默认值，不把应用层选择变成数据库的隐式策略。

| 差异 | 来源 | 处理 |
| --- | --- | --- |
| 未注册 `media_assets`、`media_asset_grants`、`media_asset_leases`、`media_play_events` | 迁移 0065、0066、0068 | 一处登记六个 ORM 模块；迁移检查、开发建表、预检和测试不再依赖偶然的导入顺序 |
| 同列重复索引 `ix_connector_override_values_revision_id` | ORM `85784b5`，0054 已有同列索引 `ix_connector_override_revision` | ORM 保留 0054 的显式索引 |
| 接入器 ACK 的 scope 索引同形异名 | ORM `85784b5`、迁移 0054 | ORM 使用已部署的 `ix_connector_ack_scope` |
| 冗余的单列 `ix_deferred_replies_state` | ORM `310d632`，0063 已有以 `state` 开头的两个索引 | ORM 保留 0063 的索引 |
| `memory_purge_tombstones` 的 `(subject_id, requested_at)` 与 `outbox_events` 的 `(subject_id, state)` 索引缺失 | 0060 | 补入 ORM，保留主体检索与清理路径的索引 |
| `conversation_attempt_snapshots` 的实例复合外键缺失 | 0075 | ORM 恢复 `(instance_scope, instance_id)` 外键及 `ON DELETE RESTRICT` |
| 11 个 `CHECK` 未进入 ORM | `prompt_snapshots`（0053，3）、`media_read_jobs`（0058，1）、`memory_purge_tombstones`（0060，2）、`media_slots`（0061，1）、`memory_consolidations`（0062，1）、`deferred_replies`（0063，3） | 补齐 ORM 约束，未放宽生产约束 |
| 153 个列默认值只在迁移侧存在 | 下表为逐版运行迁移时最后写入该默认值的修订；列类型与可空性一致 | 补齐 ORM 的 `server_default`，独立对照 PostgreSQL 实际默认表达式 |

### 列默认值来源

| 迁移 | 涉及列 |
| --- | --- |
| 0003 | `window_contexts`: `summary_message_count`, `summary_through_sequence` |
| 0004 | `awareness_items`: `origin`, `policy_revision`, `source_visibility`；`session_policies`: `mode`, `revision` |
| 0005 | `awareness_items`: `consent_revision`, `source_event_ids`；`memories`、`memory_episodes`: `source_visibility` |
| 0006、0012 | `affect_states`: `scope`（0006）、`window_id`（0012） |
| 0007 | `speak_leases.queue` |
| 0016 | `knowledge_items.visibility` |
| 0017 | `core_memories`: `active`, `origin_kind`, `priority`, `scope` |
| 0022 | `entities`: `aliases`, `kind`；`memory_entities.role` |
| 0023 | `ops_alert_state.count` |
| 0025 | `presence_states`: `energy`, `revision`, `social_load`；`scene_commits`: `origin`, `source_event_ids`, `state` |
| 0026 | `activities`: `origin`, `source_event_ids`, `visibility`；`actor_routines`: `rest_minute`, `revision`, `source`, `timezone`, `wake_minute`；`agenda_plans`: `commitment_level`, `revision`, `source_event_ids`, `state`, `timezone` |
| 0027 | `proactive_opportunities`: `reply_expected`, `visibility` |
| 0028 | `self_experiences`: `kind`, `origin`, `source_event_ids`, `visibility` |
| 0029 | `media_evidence`: `consent_revision`, `kind`, `origin`, `visibility` |
| 0031 | `intents`: `confidence`, `kind`, `origin`, `source_kind`, `state`, `visibility` |
| 0032 | `interaction_load.local_load` |
| 0035 | `attention_cursor.last_sequence` |
| 0036 | `attention_poll_log`: `baseline_calls`, `llm_calls`, `unseen_count` |
| 0037 | `together_sessions`: `progress`, `state` |
| 0039、0045 | `subjects.trust_level`（0039）、`turn_controls.attempt`（0045） |
| 0046 | `phone_states`: `created_at`, `state`, `updated_at` |
| 0047 | `personas.scenario_prompts` |
| 0048 | `provider_token_usage`: `estimated`, `tokens`, `updated_at` |
| 0049 | `user_preferences.revision` |
| 0051 | `affect_signals`: `intensity`, `scope`, `window_id`；`behavior_model_calls`: `attempt`, `reserved_input_tokens`, `reserved_output_tokens`, `state`, `window_id`；`day_plans`: `generation_source`, `revision`, `routine_revision`, `state`, `timezone_revision`, `timezone_source`；`dream_reports`: `origin`, `status`, `visibility`；`schedule_change_requests.base_revision`；`schedule_segments`: `anchor_kind`, `commitment_level`, `is_leaf`, `parent_key`, `revision_created`, `source`, `status`；`sleep_settlements.source_routine_revision` |
| 0052 | `schedule_intentions`: `horizon_rev`, `kind`, `priority`, `revision`, `state`, `target_timezone`, `visibility`；`schedule_recurrence_rules`: `revision`, `state`, `timezone`, `visibility` |
| 0053 | `prompt_snapshots`: `schema_version`, `size_bytes`, `truncated` |
| 0054 | `connector_config_heads.generation`；`connector_config_revisions.state`；`connector_runtime_acks`: `health`, `phase` |
| 0058 | `media_read_jobs`: `feedback_mode`, `lease_generation`, `mode`, `requested_at`, `state` |
| 0061 | `media_slots`: `actor_id`, `generation`, `kind`, `phase`, `subject_id`, `window_id` |
| 0063 | `deferred_replies`: `applied_delay_ms`, `claim_generation`, `clamped`, `fallback_used`, `input_sequence`, `requested_delay_ms`, `state` |
| 0065 | `media_assets`: `created_at`, `format`, `kind`, `mime_type`, `size_bytes`, `updated_at` |
| 0066 | `media_play_events`: `completed`, `created_at`, `first_played_at`, `last_played_at`, `play_count`, `updated_at` |
| 0067 | `connector_instances.enabled` |
| 0068 | `media_asset_grants`: `created_at`, `purpose`, `updated_at` |

## 验收边界

[迁移测试](../../tests/integration/test_migrations.py)在隔离 PostgreSQL 上执行严格的
`alembic check`，对比业务表、列类型、可空性、数据库默认值、索引、约束的实际定义，并检查
`0079` 的合成样本在两次 `upgrade head` 后保持不变。两条建表路径使用不同空库的 `public`
schema，避免改写历史迁移对 `public` 的引用。

- 0061 的媒体槽位、0075 的会话配置代次行只在迁移建库时预置，验收单独检查两边的行内容。
- 0051 的排除约束与配套索引只在迁移库可用 `btree_gist` 时建立；测试在安装了扩展时单独检查，
  不把 contrib 扩展变成开发建表的依赖。此处的本机实例没有该扩展。
- 0075 的配置代次触发器、0076 的主动草稿保护触发器仍由各自的迁移测试验证，不算 ORM 声明。
- 两个原有提示词快照测试曾只回拨过期时间，触发生产已有的 `expires_at > created_at` 约束。
  现同时回拨两个时间保留原始保留期，不改变业务约束。

不包含生产库预检、部署升级、备份或发布验收。
