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

export interface ProviderView {
  provider_id: string;
  kind: string;
  base_url: string | null;
  enabled: boolean;
  has_secret: boolean;
  secret_last4: string;
  secret_fingerprint: string;
  extra: Record<string, unknown>;
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
}

export interface SettingsConfig {
  schema_version: string;
  revision: number;
  test_mode: boolean;
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
}

export interface ProviderDraftInput {
  provider_id: string;
  kind: string;
  base_url: string | null;
  enabled: boolean;
  extra?: Record<string, unknown>;
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

export interface Persona {
  persona_id: string;
  revision: number;
  name: string;
  display_name: string;
  system_prompt: string;
  traits: Record<string, unknown>;
  boundaries: string[];
}

export interface PersonaUpdate {
  display_name?: string;
  system_prompt?: string;
  traits?: Record<string, unknown>;
  boundaries?: string[];
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
  note: string | null;
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
