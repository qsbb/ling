/** ling Web API v1 的类型定义（与 docs/API-WEB-V1.md 对齐）。 */

export type TurnState = 'pending' | 'streaming' | 'stopping' | 'completed' | 'stopped' | 'failed';

export interface SessionRecord {
  schema_version: string;
  session_id: string;
  actor_id: string;
  subject_id: string;
  window_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  archived: boolean;
}

export interface ChatMessage {
  schema_version: string;
  message_id: string;
  turn_id: string | null;
  role: 'user' | 'actor' | 'system';
  text: string;
  state: TurnState;
  created_at: string;
  sequence: number;
  origin: string;
  error_code: string | null;
  model_label: string | null;
  // 她怎么处置这条消息。replied / ignored / missed。
  // 沉默时 handled_reason 说明为什么——不能让消息看起来凭空消失。
  handled_as?: string | null;
  handled_reason?: string | null;
}

export interface TurnView {
  turn_id: string;
  state: TurnState;
  attempt: number;
  model_label: string | null;
  error_code: string | null;
  retriable: boolean;
}

export interface MessagesResponse {
  messages: ChatMessage[];
  active_turn: TurnView | null;
}

export interface UserInfo {
  id: string;
  username: string;
  role: string;
  subject_id: string;
  display_name: string;
  /** 首次安装的随机口令还没改过；为 true 时后端除 /v1/auth/* 外全部 403 */
  must_change_password?: boolean;
}

export interface AuthResponse {
  schema_version: string;
  user: UserInfo;
  config_revision: number;
  test_mode: boolean;
  csrf_token: string;
}

export interface ErrorPayload {
  code: string;
  message: string;
  retriable: boolean;
  detail?: string | null;
  trace_id?: string | null;
}

export interface StopResponse {
  turn_id: string;
  state: TurnState;
  was_running?: boolean;
}

/* ---------------------------------------------------------------- 设置 */

/**
 * 供应商目录里的一个模型。
 *
 * 它是**给人看的清单**，不是路由白名单：停用的模型不会出现在新的职责绑定
 * 下拉里，但已经绑给它的职责仍会照常调用——所以界面要把这种旧绑定标成
 * 「需要检查」，而不是假装它已经不可用。
 */
export interface ProviderModel {
  model_id: string;
  note: string;
  enabled: boolean;
}

export interface ProviderView {
  provider_id: string;
  kind: string;
  base_url: string | null;
  enabled: boolean;
  has_secret: boolean;
  secret_last4: string;
  secret_fingerprint: string;
  extra: Record<string, unknown>;
  models: ProviderModel[];
}

export interface ProfileView {
  profile: string;
  provider_id: string;
  model_id: string;
  fallback_provider_id: string | null;
  fallback_model_id: string | null;
  enabled: boolean;
  temperature: number;
  max_tokens: number | null;
  timeout_seconds: number;
  daily_cost_limit: number | null;
}

export interface RevisionView {
  revision: number;
  state: string;
  created_at: string | null;
  published_at: string | null;
  note: string | null;
  validation_errors: ValidationIssue[];
}

export interface ValidationIssue {
  field: string;
  message: string;
  /** error 阻断发布；warning 只提示（例如某个供应商还没存密钥）。 */
  severity?: 'error' | 'warning';
}

export interface MemoryPolicy {
  relative_score_floor: number;
  rerank_top_n: number;
  recency_half_life_days: number;
  max_retention_days: number;
}

/* ------------------------------------------------------------ 记忆内容页 */

/** 契约里 `kind` 是 episode / assertion / procedure 三选一（`contracts/v1`）。 */
export type MemoryKind = 'episode' | 'assertion' | 'procedure';

/** `GET /v1/memory` 的 include 取值；不传（null）= 只看现行记忆。 */
export type MemoryInclude = 'retracted' | 'archived' | 'all';

/**
 * 一条长期记忆（docs/API-WEB-V1.md §10.1 的完整形状）。
 *
 * **没有单条详情端点**：详情只能从列表里这一份数据渲染，别再造一个
 * `GET /v1/memory/{id}` 的假设。
 */
export interface MemoryRecord {
  memory_id: string;
  kind: string;
  text: string;
  origin: string;
  visibility: string;
  confidence: number;
  retracted: boolean;
  retract_reason: string | null;
  supersedes: string | null;
  superseded_by: string | null;
  source_event_ids: string[];
  valid_until: string | null;
  created_at: string | null;
  archived_at: string | null;
}

export interface MemoryListResponse {
  memories: MemoryRecord[];
  /** 单次返回上限（1..200，默认 50）。**没有分页**：没有 offset / cursor。 */
  limit: number;
  /** 当前 include 口径下的总行数（不受 limit 截断）。 */
  total: number;
  /** `total > memories.length`：limit 把结果截掉了。 */
  has_more: boolean;
  /** 回显请求里的原始值；默认不传时为 null。 */
  include: string | null;
  note?: string;
}

export interface CoreMemoryItem {
  core_id: string;
  label: string;
  text: string;
  priority: number;
  scope: string;
  origin_kind: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface CoreMemoryListResponse {
  items: CoreMemoryItem[];
  /** 每 subject 上限（后端回显 20）。 */
  limit: number;
  note?: string;
}

export interface CoreMemoryInput {
  label: string;
  text: string;
  priority: number;
  scope: 'private' | 'group' | 'global';
}

export interface LifePolicy {
  scene_ttl_minutes: number;
  energy_decay_per_hour: number;
  social_load_per_turn: number;
  social_load_decay_per_hour: number;
  max_live_plans: number;
}

export interface DialoguePolicy {
  group_turn_budget: number;
  proactive_daily_limit: number;
  quiet_start_minute: number;
  quiet_end_minute: number;
  private_may_stay_silent: number | boolean;
  group_mention_requires_reply: number | boolean;
  rest_may_skip: number | boolean;
  group_reply_delivery_enabled: number | boolean;
}

export interface SettingsConfig {
  schema_version: string;
  revision: number;
  test_mode: boolean;
  /**
   * 入站白名单拒绝是否逐条写日志。**只影响日志量，不影响拒绝本身**。
   * 它存在配置修订里（与 `test_mode` 同层），所以开关走草稿→发布流程。
   */
  log_access_denials: boolean;
  memory: MemoryPolicy;
  life: LifePolicy;
  dialogue: DialoguePolicy;
  providers: ProviderView[];
  profiles: ProfileView[];
  revisions: RevisionView[];
  known_kinds: string[];
  known_profiles: string[];
  reserved_profiles?: Record<string, string>;
  /** 未绑定职责 → 具体会失去什么。系统不报错，只会静默降级。 */
  unbound_degradations?: Record<string, string>;
}

export interface DraftResponse {
  revision: number;
  state: string;
  validation_errors: ValidationIssue[];
}

export interface SecretStatus {
  provider_id: string;
  has_secret: boolean;
  last4: string;
  fingerprint: string;
}

export interface ProbeResult {
  provider_id: string;
  ok: boolean;
  detail: string;
  latency_ms: number;
  models: string[];
  /** 探测是只读的；这个时间戳只用于界面标注"检查发生在什么时候"。 */
  checked_at?: string;
}

/**
 * 供应商/模型的最近调用健康度（`GET /v1/settings/health`）。
 *
 * `health` 的键是 `provider_id/model_id`。服务端刻意不返回 `last_error`
 * 原文（可能带上游地址、请求 id），设置页只该说"正常/失败/熔断"。
 */
export interface ModelHealthEntry {
  consecutive_failures: number;
  open: boolean;
  last_latency_ms: number | null;
}

export interface ModelHealthResponse {
  revision: number;
  test_mode: boolean;
  health: Record<string, ModelHealthEntry>;
  profiles: Record<string, { provider_id: string; model_id: string; enabled: boolean }>;
}

export interface ProviderDraftInput {
  provider_id: string;
  kind: string;
  base_url: string | null;
  enabled: boolean;
  extra?: Record<string, unknown>;
  /** 不传 = 不改动服务端已有清单；新建供应商时传 []。 */
  models?: ProviderModel[];
}

export interface ProfileDraftInput {
  profile: string;
  provider_id: string;
  model_id: string;
  fallback_provider_id: string | null;
  fallback_model_id: string | null;
  enabled: boolean;
  temperature: number;
  max_tokens: number | null;
  timeout_seconds: number;
  daily_cost_limit: number | null;
}

/* ---------------------------------------------------------------- 角色 */

/**
 * 一个场景在人设下的样子。
 *
 * `text` 是生效值，`default_text` 是内置默认——两个都给是刻意的：界面上
 * 空着的输入框用默认值当占位提示，"恢复默认"也只需要清空，不用前端自己
 * 存一份默认（那份迟早和服务端走散）。
 */
export interface PersonaScenario {
  key: string;
  label: string;
  when: string;
  text: string;
  default_text: string;
  custom: boolean;
}

export interface Persona {
  persona_id: string;
  revision: number;
  name: string;
  display_name: string;
  system_prompt: string;
  traits: Record<string, unknown>;
  boundaries: string[];
  /** 只含覆盖，不含默认。 */
  scenario_prompts: Record<string, string>;
  scenarios: PersonaScenario[];
}

export interface PersonaUpdate {
  display_name?: string;
  system_prompt?: string;
  traits?: Record<string, unknown>;
  boundaries?: string[];
  /** 整组替换：空串 = 恢复默认，缺键 = 没有覆盖。 */
  scenario_prompts?: Record<string, string>;
}

export interface TimelineEvent {
  id: string;
  type: string;
  origin: string;
  visibility: string;
  revision: number;
  occurred_at: string | null;
  window_id: string | null;
  summary: string;
}

export interface TimelineResponse {
  events: TimelineEvent[];
}

/* ---------------------------------------------------------------- SSE */

export interface SseEvent {
  event: string;
  data: Record<string, unknown>;
}

export interface TurnStartedData {
  turn_id: string;
  state: TurnState;
  user_message?: ChatMessage;
  test_mode?: boolean;
  config_revision?: number;
  retried?: boolean;
  replayed?: boolean;
}

export interface DeltaData {
  text: string;
  turn_id?: string;
}

export interface TurnFinalData {
  turn_id: string;
  state: TurnState;
  text?: string;
  model_label?: string | null;
  message?: ChatMessage | null;
  error?: ErrorPayload;
  retried?: boolean;
  replayed?: boolean;
  /**
   * `silence` 表示她**选择**不回（不是失败、也不是没收到）。
   * 服务端此时给 `message: null`，前端不能就此画一个空气泡。
   */
  decision?: string;
  decision_reason?: string;
  decision_hard?: boolean;
  model_status?: string;
  silent_ack?: boolean;
}

/* ---------------------------------------------------------------- 设置分类法 */

export type StorageKind = 'config_revision' | 'env' | 'business_table' | 'secret_store';

export interface TaxonomyGroup {
  key: string;
  label: string;
  defaults_to: StorageKind;
}

export interface TaxonomyCategory {
  index: number;
  key: string;
  label: string;
  /** 一句话：改这一类是在改什么 */
  verb: string;
  /** 硬边界问题，末尾带问号 */
  boundary: string;
  available: boolean;
  /** GET 可达的入口；null 表示尚无设置面，前端不得渲染入口 */
  editable_route: string | null;
  status_route: string | null;
  groups: TaxonomyGroup[];
}

export interface TaxonomyResponse {
  categories: TaxonomyCategory[];
  summary: { total: number; editable: number; pending: number };
}

/* ---------------------------------------------------------------- 授权 */

export interface ConsentRecord {
  scope: string;
  granted: boolean;
  revision: number;
  valid_until: string | null;
  note: string | null;
}

export interface ConsentsResponse {
  consents: ConsentRecord[];
  known_scopes: string[];
  defaults: Record<string, boolean>;
}

export interface ConsentUpdate {
  scope: string;
  granted: boolean;
  valid_until?: string | null;
  note?: string | null;
}

/* ---------------------------------------------------------------- 生活 */

export interface ActivityRecord {
  activity_id: string;
  kind: string;
  state: string;
  origin: string;
  /**
   * 后端 `_activity_dict()` 目前**不返回** note（`ActivityView` 也没有这个字段），
   * 所以标成可选：前端一律按「可能没有」渲染，不能假定它存在。
   */
  note?: string | null;
  started_at: string | null;
  ended_at: string | null;
  completion_evidence: string | null;
}

export interface ActivityResponse {
  current: ActivityRecord | null;
  history: ActivityRecord[];
  note: string;
}

export interface GoalRecord {
  goal_id: string;
  title: string;
  horizon: string;
  state: string;
  progress: number;
  note: string | null;
}

export interface GoalsResponse {
  goals: GoalRecord[];
}

export interface ProactiveOpportunity {
  opportunity_id: string;
  kind: string;
  /** 内部推理：她为什么会想找你。不是要发出去的话。 */
  reason: string;
  /** 生成好的开场白。null = 还没生成。 */
  draft_text: string | null;
  expires_at: string;
}

export interface ProactiveResponse {
  pending: ProactiveOpportunity[];
  gate: boolean;
  gate_reasons: string[];
  /** 主动发送是否真的实现了。false 时候选只会过期，不会变成消息。 */
  delivery_implemented?: boolean;
  /** 开场白生成是否实现。 */
  draft_implemented?: boolean;
  /** 主动发送是否**开启**（与"是否实现"是两件事）。 */
  delivery_enabled?: boolean;
  note?: string;
}


export interface MediaUploadResponse {
  evidence_id: string;
  kind: string;
  mime: string;
  bytes: number;
  note: string;
}


/* ------------------------------------------------------------ 多用户与账号 */

export interface AccountRow {
  account_id: string;
  connector_instance: string;
  external_account_id: string;
  proof: string;
  revoked: boolean;
}

export interface SubjectRow {
  subject_id: string;
  display_name: string;
  trust_level: string;
  /** 非空表示这个 subject 已被合并到别人名下（值就是那个人）。 */
  merged_into: string | null;
  accounts: AccountRow[];
}

export interface SubjectsResponse {
  subjects: SubjectRow[];
}

export interface MergeResult {
  source_id: string;
  target_id: string;
  /** 恒为 0：合并只写指针，不搬数据。非零说明实现被改坏了。 */
  moved: number;
}

export interface UnmergeResult {
  source_id: string;
  was_merged_into?: string;
  restored_memories: number;
}

export type DashboardStatus = 'ok' | 'empty' | 'unavailable';

export interface DashboardSection {
  status: DashboardStatus;
  as_of: string;
  data?: Record<string, unknown> | null;
}

export interface DashboardResponse {
  schema_version: string;
  generated_at: string;
  timezone: string;
  sections: {
    activity: DashboardSection;
    affect: DashboardSection;
    presence: DashboardSection;
    proactive: DashboardSection;
    agenda: DashboardSection;
    memories: DashboardSection;
    timeline: DashboardSection;
    relationship: DashboardSection;
    connectivity: DashboardSection;
  };
}

/* ------------------------------------------------------------ 运行日志 */

/**
 * 日志级别（**过滤**下拉的取值）。
 *
 * 只列四档：后端 `LEVEL_ORDER` 就是这四档。条目自己的 `level` 字段是
 * `string`——CRITICAL 之类的记录也会进缓冲，按原样显示，不硬塞进这四档里
 * （把它悄悄显示成 "error" 会让人以为是 error 而不是更严重的那一级）。
 */
export type LogLevel = 'debug' | 'info' | 'warning' | 'error';

/** `GET /v1/logs` 的一条。 */
export interface LogEntry {
  /** ISO8601 UTC（带 `+00:00` 偏移），可直接 `new Date()`。 */
  timestamp: string;
  level: string;
  logger: string;
  /** 已在服务端脱敏；界面按纯文本渲染，不做任何还原。 */
  message: string;
  event_name?: string;
  attributes?: Record<string, unknown>;
  /**
   * 日志分类：`runtime`（运行）/ `behavior`（处理）/ `audit` / `security`。
   *
   * 由**后端**算好下发，前端不再从 `attributes.log_kind` 里猜一遍——
   * 两处各判一次早晚会不一致。
   */
  kind?: LogKind;
  trace_id?: string;
  turn_id?: string;
  session_id?: string;
}

/** 与后端 `LOG_KINDS` 同一份取值。 */
export type LogKind = 'runtime' | 'behavior' | 'audit' | 'security';

export interface LogsResponse {
  /** **时间正序**：最新的在最后。 */
  items: LogEntry[];
  /** 缓冲当前条数（过滤前），界面据此说明"你看到的是全部还是最近一段"。 */
  buffered: number;
  /** 缓冲容量（进程启动时的 `LING_LOG_BUFFER_SIZE`）。 */
  capacity: number;
  /**
   * **本进程**启动以来被白名单拒掉的入站条数。
   *
   * 与"是否逐条写日志"的开关无关：关掉逐条 WARNING 之后，这个数字仍然
   * 要能回答"最近有没有人在扫我的机器人"。多副本时各进程各算各的。
   */
  denied_total: number;
}

export interface LogQuery {
  limit?: number;
  level?: LogLevel | null;
  logger?: string | null;
  q?: string | null;
  /** 空值表示不筛选（不要传空串，那是语义不同的请求）。 */
  kind?: LogKind | null;
}

/* ------------------------------------------------------------ 关于与版本 */

export interface VersionInfo {
  schema_version: string;
  current: string;
  /** 构建时由 LING_COMMIT 注入；源码运行时为 null。 */
  commit: string | null;
  /** 部署清单的 SHA-256；开发模式没有清单时为 null。 */
  web_manifest_sha256: string | null;
}

export type ReleaseStatus = 'ok' | 'no_release' | 'unavailable';

export interface ReleaseCheck {
  schema_version: string;
  status: ReleaseStatus;
  current: string;
  latest: string | null;
  published_at: string | null;
  html_url: string | null;
  /** null 表示版本号无法按 semver 比较，不等于“已是最新”。 */
  is_newer: boolean | null;
  /**
   * 更新通道新增字段。旧服务端不返回时按“不可安装”处理，
   * 绝不用缺失字段拼出一个安装目标。
   */
  release_id?: number | null;
  target_commit?: string | null;
  update_eligible?: boolean;
  update_reason?: string | null;
}

/* ------------------------------------------------------------ 更新通道 */

export type UpdateReason =
  | 'disabled'
  | 'not_owner'
  | 'executor_offline'
  | 'channel_unavailable'
  | 'installed_commit_unknown'
  | 'version_drift'
  | 'update_in_progress'
  | 'release_unverifiable'
  | 'not_newer';

export type UpdateJobState = 'queued' | 'running' | 'succeeded' | 'failed' | 'needs_manual';

export type UpdateStage =
  | 'queued'
  | 'preflight'
  | 'backup'
  | 'fetch'
  | 'verify_source'
  | 'build'
  | 'deploy'
  | 'verify'
  | 'complete';

export interface UpdateJob {
  request_id: string;
  state: UpdateJobState;
  stage: UpdateStage;
  target_tag: string;
  target_commit: string;
  created_at: string;
  updated_at: string;
  error_code: string | null;
  message: string | null;
  /**
   * 备份告警（可选，协议仍是 v1）：执行器把 `verify-warnings.txt` 里的告警同时
   * 写进任务视图。旧执行器不写这个键，前端必须按“没有告警”处理。
   */
  backup_warnings?: string[];
}

export interface UpdateExecutor {
  online: boolean;
  last_seen_at: string | null;
}

/** GET /v1/version/update 的只读状态；不发起任何更新。 */
export interface UpdateStatus {
  schema_version: string;
  enabled: boolean;
  reason: string | null;
  executor: UpdateExecutor;
  current_job: UpdateJob | null;
}

/** POST /v1/version/update-requests 的乐观并发校验字段。 */
export interface UpdateExpected {
  release_id: number;
  tag: string;
  commit: string;
  current_version: string;
  current_commit: string;
}

export interface UpdateRequest {
  confirm_tag: string;
  expected: UpdateExpected;
}

/* ------------------------------------------------- 镜像更新通道（新协议 P5）
 *
 * 依据 `src/ling/api/routes/version.py` 的 `ImageUpdateChannel.view()`：响应是
 * **白名单投影**，更新器状态文件里的自由文本（日志、路径、口令）不会出现在这里。
 * 下面的 `reason` / `failure.code` / `stage` / `updater.state` / `task.state`
 * 都是后端码表值；前端只用映射函数翻译，**不**把它们当自由文本渲染。
 * 未知码（后端更新、前端未同步）一律落到兜底文案，不显示英文原文。
 */

/** `getImageUpdate` 的 `reason` 码表（后端 `IMAGE_REASON_MESSAGES` 的键）。 */
export type ImageUpdateReason =
  | 'image_mode_required'
  | 'update_disabled'
  | 'not_owner'
  | 'update_channel_unavailable'
  | 'updater_status_missing'
  | 'updater_status_invalid'
  | 'protocol_unsupported'
  | 'updater_offline'
  | 'manual_required'
  | 'update_in_progress'
  | 'backup_gate_missing'
  | 'current_image_unknown'
  | 'installed_image_changed'
  | 'installed_version_changed'
  | 'already_on_target'
  | 'duplicate_request_id'
  | 'recent_login_required'
  | 'rate_limited'
  /** 可信发布目录里没有适用于当前版本的新版本：没有目标就不能提交。 */
  | 'no_available_target'
  /** 提交的摘要不在目录的兼容目标里（不能绕过目录提交任意摘要）。 */
  | 'target_not_in_catalog';

/** 更新器自报状态；状态文件缺失/坏掉时后端给 `unknown`，**不会**伪装成 idle。 */
export type ImageUpdaterState = 'idle' | 'busy' | 'needs_manual' | 'unknown';

/** 任务终态与进行中（后端 `IMAGE_TASK_STATE_MESSAGES` 的键）。 */
export type ImageUpdateTaskState =
  | 'running'
  | 'succeeded'
  | 'failed_not_started'
  | 'needs_manual'
  | 'resolved';

/**
 * 真实阶段（后端 `IMAGE_STAGE_MESSAGES` 的键，与更新器 `STAGES` 一致）。
 * 不存在"下载百分比/剩余时间"这类字段——后端本来就不产生。
 */
export type ImageUpdateStage = 'prepare' | 'backup' | 'migrate' | 'switch' | 'verify';

/** 失败码（后端 `IMAGE_FAILURE_MESSAGES` 的键）；未知码后端只给兜底 message。 */
export type ImageFailureCode =
  | 'request_rejected'
  | 'already_on_target'
  | 'duplicate_request'
  | 'expected_current_mismatch'
  | 'preflight_failed'
  | 'maintenance_gate_failed'
  | 'pull_failed'
  | 'backup_failed'
  | 'migration_failed'
  | 'switch_failed'
  | 'old_service_unverified'
  | 'reconcile_needs_manual'
  | 'state_unreadable'
  | 'previous_task_needs_manual'
  | 'install_lock_busy'
  | 'env_write_failed'
  | 'internal_error';

export interface ImageUpdateCurrent {
  /** `LING_APP_IMAGE`（完整 digest）；后端无法确认时为 null。 */
  digest: string | null;
  /** 构建提交（`LING_COMMIT`）；未注入时为 null。 */
  commit: string | null;
}

export interface ImageUpdaterView {
  /** 状态文件新鲜、协议一致、state 已知才算在线（后端按 `updated_at` 判定）。 */
  online: boolean;
  state: ImageUpdaterState;
  protocol: number | null;
  version: string | null;
  updated_at: string | null;
  age_seconds: number | null;
}

export interface ImageBackupGate {
  /** 后端自己核实后的结论（不盲信状态文件里的布尔值）。 */
  restore_point: boolean;
  complete: boolean;
  backup_id: string | null;
  tombstones_state: string | null;
  media_state: string | null;
  keys_root: string | null;
  keys_env_set: string | null;
  /** 后端码表码；未认识时 null。 */
  reason: string | null;
  /** 后端码表文案（已脱敏）；null 表示没有可展示的理由。 */
  reason_message: string | null;
  checked_at: string | null;
  source: 'backup_gate' | 'task' | null;
}

export interface ImageUpdateFailure {
  code: ImageFailureCode | null;
  stage: string | null;
  /** 码表文案（已脱敏）；未知码时是后端固定兜底句。 */
  message: string;
}

export interface ImageUpdateTargetRef {
  digest: string | null;
  commit: string | null;
}

export interface ImageUpdateMigration {
  attempted: boolean;
  current_before: string | null;
  current_after: string | null;
  target_head: string | null;
}

export interface ImageUpdateBackupRef {
  id: string | null;
  complete: boolean;
  tombstones_state: string | null;
  media_state: string | null;
  keys_root: string | null;
  keys_env_set: string | null;
}

export interface ImageUpdateMaintenance {
  state: 'active' | 'released' | 'unknown' | null;
  verified: boolean;
}

export interface ImageUpdateRollback {
  attempted: boolean;
  verified: boolean;
}

export interface ImageUpdateTask {
  request_id: string | null;
  state: ImageUpdateTaskState;
  /** 码表文案。 */
  state_message: string;
  stage: ImageUpdateStage | null;
  /** 码表文案；stage 未知时 null。 */
  stage_message: string | null;
  started_at: string | null;
  updated_at: string | null;
  finished_at: string | null;
  target: ImageUpdateTargetRef;
  previous: ImageUpdateTargetRef;
  migration: ImageUpdateMigration;
  backup: ImageUpdateBackupRef;
  maintenance: ImageUpdateMaintenance;
  rollback: ImageUpdateRollback;
  /** null = 更新器没有实测旧服务，**不敢**断言（不能当成"仍在旧版本"）。 */
  still_on_old_version: boolean | null;
  failure: ImageUpdateFailure | null;
  warnings: string[];
  next_actions: string[];
}

export interface ImageUpdatePendingRequest {
  request_id: string;
  readable: boolean;
  target_digest: string | null;
  created_at: string | null;
}

/**
 * 可信发布目录里的一条目标（后端 `available_targets[i]` 的白名单投影）。
 *
 * 后端把码表码与文案都给了；前端只按字段渲染，**不**自己判断兼容性。
 */
export interface ImageUpdateAvailableTarget extends ImageUpdateViewTarget {
  /** 后端复核后的结论：版本必须严格大于当前版本，且当前版本在 min_from 里。 */
  compatible: boolean;
  /** 后端 `IMAGE_INCOMPATIBLE_MESSAGES` 的键；兼容时为 null。 */
  incompatible_reason: ImageUpdateIncompatibleReason | null;
  /** 后端码表文案（已脱敏）；兼容时为 null。 */
  incompatible_reason_message: string | null;
}

/** 不兼容原因码表（后端 `IMAGE_INCOMPATIBLE_MESSAGES` 的键）。 */
export type ImageUpdateIncompatibleReason =
  | 'not_in_min_from'
  | 'not_newer'
  | 'current_version_unknown'
  | 'version_uncomparable';

/** 可信发布目录的读取结论（后端 `release_catalog`）；原因码未知时三项都是 null。 */
export interface ImageUpdateCatalogView {
  state: 'ok' | 'missing' | 'invalid' | null;
  state_message: string | null;
  reason: string | null;
  /** 后端码表文案（已脱敏）：目录缺失/坏掉时说清"为什么没有目标"。 */
  reason_message: string | null;
  entries: number;
  /** 按当前镜像摘要反查到的安装版本；反查不到时 null。 */
  current_version: string | null;
}

/**
 * 目标版本（后端 `target`）：只给这四个字段。
 *
 * `has_migration` 来自发布目录，用来提示"这次更新会跑数据库迁移"。
 */
export interface ImageUpdateViewTarget {
  version: string;
  digest: string;
  commit: string;
  has_migration: boolean;
}

/** GET /v1/version/image-update 的响应体。 */
export interface ImageUpdateView {
  schema_version: string;
  image_mode: boolean;
  enabled: boolean;
  installable: boolean;
  reason: ImageUpdateReason | null;
  /** 后端码表文案（已脱敏）；可安装时为 null。 */
  message: string | null;
  /** 后端码表给出的下一步（已脱敏），原样展示。 */
  next_actions: string[];
  current: ImageUpdateCurrent;
  updater: ImageUpdaterView;
  backup_gate: ImageBackupGate;
  /** 可信发布目录的读取结论（缺失/坏掉时有明确原因）。 */
  catalog: ImageUpdateCatalogView;
  /** 推荐的目标（最新且兼容）；没有可用目标时为 null。 */
  target: ImageUpdateViewTarget | null;
  /** 目录里比当前版本新的目标（含不兼容的，带原因）。 */
  available_targets: ImageUpdateAvailableTarget[];
  task: ImageUpdateTask | null;
  request: ImageUpdatePendingRequest | null;
}

/** POST /v1/version/image-update-requests 的**固定 5 键**请求体。 */
export interface ImageUpdateSubmission {
  request_id: string;
  target_digest: string;
  expected_current_digest: string;
  expected_current_commit: string;
  operator_confirmed: true;
}

/** 提交回执（201）：请求已写入，等待更新器接手。 */
export interface ImageUpdateSubmissionAck {
  schema_version: string;
  request_id: string;
  state: string;
  state_message: string;
  target_digest: string;
  expected_current_digest: string;
  expected_current_commit: string;
  created_at: string;
}
