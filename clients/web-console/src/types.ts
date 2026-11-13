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
  /**
   * 群名 / 对方昵称。**读不到就是 null**：渠道窗口的 `title` 是
   * `onebot-default:group:100200400` 这类机器键，拿它当标题等于什么都没说。
   * 界面按 `channel` + `peer_id` 回退成「群 100200400」「私聊 100200301」，
   * 不编名字。旧服务端没有这个字段。
   */
  display_name?: string | null;
  /**
   * 渠道名：`web` 或连接器 id（如 `onebot-default`）。
   * 只在 `title` 解析不出窗口键时用于判断这条会话是群聊还是私聊。
   */
  channel?: string;
  /** 群号 / QQ 号。没有具体对端（网页会话）时为 null。 */
  peer_id?: string | null;
  /**
   * 是否置顶。后端早在 `contracts/v1.py` 返回这个字段，前端一直没消费——
   * 于是"能按好感度排"（后端已修）但"不能置顶"。这里补上。
   *
   * 旧服务端没有这个字段，所以可选：缺失按"未置顶"处理，不报错。
   */
  pinned?: boolean;
}

export interface ChatMessage {
  schema_version: string;
  message_id: string;
  turn_id: string | null;
  role: 'user' | 'actor' | 'system';
  text: string;
  output_modality?: 'text' | 'voice';
  speech_state?: 'none' | 'pending' | 'synthesizing' | 'ready' | 'failed' | 'cancelled' | 'expired';
  speech_delivery_state?: string | null;
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
  /**
   * 发言人显示名：群名片优先、其次昵称。
   * 缺失时**不要编名字**——界面只把 `sender_id` 当标签（`成员 100200300`）。
   */
  sender_name?: string | null;
  /** 发言人的平台号码（QQ 号）或 subject id；和名字分两行显示，不要挤一起。 */
  sender_id?: string | null;
  /**
   * 这条被**对方**撤回了（入站撤回，迁移 0055）。非空 = 撤回时刻。
   * 与下面的 `recalled_at` 分开：那个是"她自己收回了"。
   */
  retracted_at?: string | null;
  /**
   * **她自己**把这条收回来了（出站撤回，迁移 0057）。非空 = 撤回时刻。
   * 两者都非空时以它为准（她自己撤的优先级更高，说明这条是她的发言）。
   */
  recalled_at?: string | null;
  /**
   * 最后一次撤回尝试的结果：`recalled` / `uncertain` / `expired` /
   * `not_found` / `unsupported`。**只有 `recalled` 等于"撤回了"**——
   * `uncertain` 是"没拿到回执"，界面不能说成功。
   */
  recall_state?: string | null;
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

/* ---------------------------------------------------------------- 提示词快照 */

/**
 * 「她这次看到了什么」快照的三态。
 *
 * `expired` / `unavailable` **不是**空内容：它们各自有一句人话，界面必须把
 * 那句话显示出来。把三态都渲染成"没有条目"会让人以为提示词真的是空的。
 */
export type PromptSnapshotState = 'available' | 'expired' | 'unavailable';

/** 快照里的一段注入内容。`key` 是机器键（system/memory/…），`label` 是人话标题。 */
export interface PromptSection {
  key: string;
  label: string;
  role?: string | null;
  tokens?: number | null;
  /** 后端建议默认折叠（例如很长的历史）。前端仍会自己按长度兜底。 */
  collapsed?: boolean;
  items: PromptItem[];
}

/** 一条注入项。有 `sender` 时界面显示发言人，否则回退到 `role`。 */
export interface PromptItem {
  role?: string | null;
  sender?: string | null;
  content: string;
  at?: string | null;
}

/** 这一轮真正使用的模型与职责（拿不到时整块为 null）。 */
export interface PromptModelInfo {
  provider_id: string | null;
  model_id: string | null;
  profile: string | null;
}

/**
 * `GET /v1/messages/{message_id}/prompt` 的响应。
 *
 * 契约里 `items` 与 `model` 是内联结构；这里给它们起了名字（字段名一字未改），
 * 只是为了让组件与测试不必重复写匿名类型。
 */
export interface PromptSnapshotOut {
  state: PromptSnapshotState;
  message_id: string;
  turn_id: string | null;
  created_at: string | null;
  expires_at: string | null;
  model: PromptModelInfo | null;
  token_total: number | null;
  truncated: boolean;
  sections: PromptSection[];
  tool_transcript?: Array<{
    step: number;
    provider_call_id: string;
    tool_id: string;
    outcome: string;
    content: string;
    tokens: number;
  }>;
  tool_transcript_truncated?: boolean;
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
  /**
   * 这家**能服务哪些能力**（chat / embed / rerank …）。
   * 默认槽位只收 chat：把嵌入供应商填进「快速」，发布能成功、调用必失败。
   */
  capabilities?: string[];
  base_url: string | null;
  enabled: boolean;
  has_secret: boolean;
  secret_last4: string;
  secret_fingerprint: string;
  extra: Record<string, unknown>;
  models: ProviderModel[];
  /**
   * 传输与额度属于**供应商**：同一家下面所有职责共用一份。
   * `-1`（或 null）= 不限制。
   */
  timeout_seconds: number;
  max_output_tokens: number | null;
  daily_token_limit: number | null;
}

/**
 * 一个职责绑定：只回答"交给哪家供应商的哪个模型"。
 *
 * 超时 / 输出上限 / 日额度**不在这里**——它们在 `ProviderView` 上。
 * 原来这四项挂在这里，同一家供应商下的五个职责各有各的一份，
 * 改一次要改五处，而且默认值会悄悄盖掉供应商的设置。
 */
export interface ProfileView {
  profile: string;
  provider_id: string;
  model_id: string;
  fallback_provider_id: string | null;
  fallback_model_id: string | null;
  enabled: boolean;
  /**
   * `explicit` = 用户单独指定了这个模型；`slot` = 跟随默认槽位
   * （此时 provider_id / model_id 一定是空的）。
   */
  mode: BindingMode;
}

export type BindingMode = 'explicit' | 'slot';

/** 生效模型是怎么来的。**只有这三种**，界面按它分三种样式。 */
export type ResolutionSource = 'explicit' | 'slot' | 'legacy_fallback' | 'unavailable';

/**
 * 一个职责**最后会跑哪个模型**——由服务端算好，前端只显示。
 *
 * 前端**不要**自己根据 `profiles` / `slots` 再判一次：回退规则实现两遍，
 * 两边迟早不一致，症状是"界面写着 A、实际跑 B"，用户永远查不出来。
 */
export interface ProfileResolution {
  profile: string;
  /** 该职责所属的槽位（嵌入/重排没有槽位 → null）。 */
  slot: string | null;
  source: ResolutionSource;
  effective: { provider_id: string; model_id: string } | null;
  /** 用户单独指定过什么。只有"指定了但没用上"时才非空（界面标「已回退」）。 */
  requested: { provider_id: string; model_id: string } | null;
  fell_back: boolean;
  enabled: boolean;
  /** `unavailable` 的具体原因，中文，可直接显示。 */
  reason: string;
}

/** 一个默认槽位（快速 / 创作 / 识图 / 视频 / 日常）。 */
export interface SlotView {
  slot: string;
  label: string;
  hint: string;
  provider_id: string;
  model_id: string;
  configured: boolean;
  /** 供应商 enabled **且** 模型 enabled。没配 ≠ 坏了，所以两个字段分开。 */
  usable: boolean;
  /** 哪些职责跟着它（服务端算的，别在前端抄一份映射表）。 */
  roles: string[];
}

export interface SlotDraftInput {
  slot: string;
  provider_id: string;
  model_id: string;
}

/** 快速配置二次确认里的一行。 */
export interface QuickConfigRow {
  profile: string;
  slot: string;
  slot_label: string;
  provider_id: string | null;
  model_id: string | null;
  change: string;
  change_kind: 'override' | 'same' | 'repair' | 'unavailable';
}

export interface QuickConfigPreview {
  schema_version: string;
  rows: QuickConfigRow[];
  /** 点确认后应当写入的那份草稿，原样发回 `saveDraft` 即可。 */
  profiles: ProfileDraftInput[];
  summary: {
    total: number;
    overrides: number;
    unchanged: number;
    repairs: number;
    still_unavailable: number;
  };
}

export interface ResolveResponse {
  schema_version: string;
  slots: SlotView[];
  profile_resolutions: ProfileResolution[];
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
  tools_enabled?: boolean;
  /**
   * 她**愿不愿意开口说话**的倾向档位。设置页「⑤ 对话与主动性」。
   * 对应后端 `ports/config.py::VoiceWillingness`（§17.4）。
   *
   * **这是倾向，不是概率**：三档之间没有 0.3/0.7 的映射，不抽随机数。
   * 每档只转成一段提示词倾向，由她自己决定这一条说还是打。
   * 服务端默认 `natural`；非法值也退回 `natural`。
   */
  voice_willingness?: VoiceWillingness;
  /**
   * 她要用的**音色**。对应后端 `ports/config.py::VoiceSetting`。
   *
   * 缺省（`undefined`）与 `{kind: null}` 都表示"还没配"——那时运行时降级发文字。
   * 页面**不得**把它显示成"可用"。
   */
  voice?: VoiceSetting;
}

/**
 * 可以配音色的三家。取值与后端 `VoiceKind` / `ports/model.ProviderKind` 逐字一致。
 *
 * **没有统一的 `voice_key`**（§17.2 撤销了跨厂商映射表）：同一个"温柔"在三家
 * 是三个完全不同的值。所以 `kind` 只回答"这套参数是给谁的"，不承担翻译。
 */
export type VoiceKind = 'minimax_tts' | 'fishaudio_tts' | 'mimo_tts';

/**
 * 一套音色参数。与后端 `ports/config.py::VoiceSetting` 一一对应。
 *
 * ## 字段按家归属，**不是并集都能填**
 *
 * - `minimax_tts`：`voice_id` + speed / vol / pitch / emotion
 * - `fishaudio_tts`：**只有** `voice_id`（那边叫 reference_id）——没有 speed/vol/pitch
 * - `mimo_tts`：**只有** `reference_asset_id`（走 voiceclone，由上传接口拿到）
 *
 * 后端对"多填了别家的"不报错（它只判"缺该有的"），所以**清理残留字段是界面的
 * 责任**：`speed` 留在 mimo 配置里不会生效，但会让人以为它生效。
 */
export interface VoiceSetting {
  kind: VoiceKind | null;
  /** MiniMax 的 `voice_id`，**也是** FishAudio 的 `reference_id`（由 kind 决定是哪种）。 */
  voice_id: string | null;
  /** MiMo 的参考素材 id（`media_assets.digest`，64 位十六进制）。**不是字节**。 */
  reference_asset_id: string | null;
  speed: number;
  vol: number;
  pitch: number;
  /** 默认 `null` = 不发。猜一个 emotion 会让语音听起来莫名其妙。 */
  emotion: string | null;
}

/**
 * 参考音频上传的成功返回（`POST /v1/settings/speech/voice-reference`）。
 *
 * `asset_id` 直接填进 `voice.reference_asset_id`；`format` / `size_bytes` 用于
 * 在页面上说清"传进来的是什么"，免得用户以为扩展名对了就行（后端按字节头判定）。
 */
export interface VoiceReferenceUpload {
  schema_version: string;
  asset_id: string;
  format: string;
  mime_type: string;
  size_bytes: number;
  duration_ms: number | null;
  retention_days: number;
}

/**
 * 语音意愿档位。与后端 `ports/config.py::VoiceWillingness` 一一对应（§17.4）。
 *
 * - `reserved` 少用语音
 * - `natural` 自然选择（默认）
 * - `expressive` 多用语音
 *
 * 任何档位都允许她选择任一种表达；对方明确的「请文字／不方便听」优先。
 */
export type VoiceWillingness = 'reserved' | 'natural' | 'expressive';

/**
 * 出站附件的类型。**必须显式给出**，不由引用内容猜。
 *
 * 为什么重要：一个 `asset:abc` 既可能是图片也可能是语音。猜错的后果是
 * "语音被当图片发出去"——用户只会看到一张图，不会有任何报错。
 */
export type AttachmentKind = 'image' | 'audio' | 'video' | 'file';

/**
 * 一个**类型化**的出站附件。与后端 `contracts/v1.py::OutboundAttachment` 对应。
 *
 * 语音消息的**平台侧**形态是：`text=""` + 一条 `kind: 'audio'` 附件，
 * **无 caption、无正文**（§17.5）。后台能看到的「语音标签 + 文字」是
 * 另一个**投影**，由后台接口从 Message.text 取，不是从附件或 wire 消息取。
 * 正文绝不复制进附件——复制一份必然出现"两份不一致该信哪个"。
 */
export interface OutboundAttachment {
  kind: AttachmentKind;
  /** 平台侧引用：URL、不透明 id，或 `asset:<id>`。 */
  ref: string;
  mime_type?: string | null;
  caption?: string | null;
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
  /** 默认槽位（快速 / 创作 / 识图 / 视频 / 日常）。 */
  slots: SlotView[];
  /**
   * 每个职责最后会跑哪个模型。**服务端算的**，界面「实际生效」那一列直接用它。
   * 改草稿时用 `POST /settings/resolve` 拿同一份结论，不要在浏览器里重算。
   */
  profile_resolutions: ProfileResolution[];
  /**
   * 每个职责**需要哪种能力**（chat / embed / rerank …）。
   * 绑定下拉据此过滤：`profile_capabilities[role]` 必须出现在供应商的
   * `capabilities` 里。**服务端是唯一真源**，前端不自己维护 kind → 能力表。
   */
  profile_capabilities?: Record<string, string | null>;
  /**
   * 嵌入/重排当前的能力来源：`binding`（网页绑定）/ `env`（环境变量）/
   * `null`（都没有 = 该能力实际关闭）。
   *
   * 为什么不能只看"有没有绑定"：老部署用的是 `LING_EMBEDDING_*` /
   * `LING_RERANK_*`，把 env 配好的部署报成"没配"，用户会去乱改。
   */
  memory_sources?: Record<string, MemorySource>;
  reserved_profiles?: Record<string, string>;
  /** 未绑定职责 → 具体会失去什么。系统不报错，只会静默降级。 */
  unbound_degradations?: Record<string, string>;
}

/** 记忆类能力（嵌入/重排）当前由什么提供。`null` = 两处都没配。 */
export type MemorySource = 'binding' | 'env' | null;

export interface DraftResponse {
  revision: number;
  state: string;
  validation_errors: ValidationIssue[];
}

/**
 * 接入器（`GET /v1/settings/connectors`）的视图。
 *
 * 四态而不是三态：`disabled` 只出现在 QQ 官方机器人上——凭据配好了但渠道开关
 * 是关的。把它说成"未连接"是错的（连接从没被尝试过），去查连接只会白费时间。
 *
 * 契约见 `src/ling/contracts/v1.py` 的 `ConnectorsOut`；字段注册表见
 * `src/ling/settings/connector_fields.py`。
 */
export type ConnectorStatus =
  | 'not_configured'
  | 'disabled'
  | 'configured_disconnected'
  | 'connected';

/**
 * 字段四档（`ling/settings/connector_fields.py:23-29`）。
 *
 * `list` 是名单：GET 回字符串数组，PUT 接受数组或逗号拼接的字符串，
 * 后端 `normalize_list` 会 trim/去空/去重，**但绝不转成数字**
 * （QQ 号可能有前导零）。
 */
export type ConnectorFieldKind = 'secret' | 'text' | 'switch' | 'list';

/** 有效值从哪来：DB 覆盖 > 进程启动时的 env 快照 > 代码默认值。 */
export type ConnectorFieldSource = 'db' | 'env' | 'default';

/**
 * 一项配置。
 *
 * **密钥只有 `configured` / `set` / `preview`**（`kind==='secret'` 时 `value`
 * 恒为 null）：后端连值都不放进视图对象，页面不存在"显示/隐藏密钥"这条路径。
 * `preview` 是 owner 才拿到的前 7 字符 + `••••`，**只在要点开编辑时才显示**，
 * 不作为输入框的回显值（那等于把一个可用的凭据明文画在屏幕上）。
 */
export interface ConnectorField {
  /** 注册表里的固定键名，同时也是 PUT 的 `set` / `clear` 里的键 */
  key: string;
  /** 与 `key` 相同的兼容别名（旧后端/旧前端读它） */
  name: string;
  scope: string;
  kind: ConnectorFieldKind;
  label: string;
  /** 控件提示：text / password / switch / list / select（纯展示，不是校验依据） */
  control: string;
  /** 这一项是否允许在网页上改（注册表里的 `editable`） */
  editable: boolean;
  source: ConnectorFieldSource | string;
  /** 有效值非空（**不等于"DB 里有覆盖"**） */
  configured: boolean;
  /** `configured` 的兼容别名 */
  set: boolean;
  /** 非密钥的规范化值；密钥恒 null。switch 给布尔，list 给数组 */
  value: string | boolean | string[] | null;
  /** 密钥预览（前 7 字符 + ••••）；非密钥或非 owner 为 null */
  preview: string | null;
}

export interface ConnectorRuntime {
  /** 当前连接数；null = 这条路径没有可观察的连接对象（例如只配了 HTTP 上报） */
  connections: number | null;
  /** 已收帧/事件数；null 同上。已连接但恒为 0 本身是有用的结论。 */
  received: number | null;
}

/**
 * 一个 scope 的**生效**状态（`ConnectorActivationOut`）。
 *
 * `activating` 只是"候选落库、正在切换"，只有 `active` 才算生效；
 * `rolled_back` 是失败且已恢复旧配置；`degraded` 要么本地生效但远端没连上，
 * 要么回退本身也失败——两者都**不是成功**。
 */
export interface ConnectorActivation {
  scope: string;
  state: string;
  generation: number;
  active_generation: number;
  candidate_generation: number | null;
  active_revision_id: string | null;
  candidate_revision_id: string | null;
  last_revision_id: string | null;
  last_state: string | null;
  started_at: string | null;
  activated_at: string | null;
  error_code: string | null;
  /** 可以展示给人看的一句话（不含异常原文） */
  error_safe: string | null;
  ack_count: number;
  expected_count: number;
}

export interface ConnectorView {
  id: string;
  label: string;
  /** 凭据齐不齐（与总览 connectivity 同源，不另判一套） */
  configured: boolean;
  enabled: boolean;
  status: ConnectorStatus | string;
  status_reason: string;
  status_message: string;
  /** 一句"怎么改"，点名真实环境变量 */
  guidance: string;
  /** owner 为 true；非 owner 只读（PUT 会 403） */
  editable: boolean;
  /** 值落在 DB 覆盖层（容器根目录只读，写不了 .env） */
  storage: string;
  /** 并发代际：PUT 的 `If-Match` 用它 */
  generation: number;
  activation: ConnectorActivation;
  fields: ConnectorField[];
  runtime: ConnectorRuntime;
}

/**
 * 出入许可（白名单）组：参与同一套 generation，但不重建长连接。
 * 它**不是**一个"渠道"，所以单独一段而不是混进 `connectors`。
 */
export interface ConnectorAccessGroup {
  id: string;
  generation: number;
  activation: ConnectorActivation;
  editable: boolean;
  fields: ConnectorField[];
}

export interface ConnectorsResponse {
  schema_version: string;
  as_of: string;
  /** 当前登录者是不是 owner（非 owner 页面不给编辑入口） */
  can_edit: boolean;
  connectors: ConnectorView[];
  access: ConnectorAccessGroup;
  /**
   * 这次读的是哪个实例——**只在带 `?instance_id=` 读取时才有**。
   *
   * 有它，客户端才能自证"这份字段视图属于这个实例"；页面级读（不带实例）
   * 没有它，那份视图只代表默认实例。可选而非必有，正是因为老服务端/默认读
   * 不会给：客户端必须按**实际收到的响应**判断，不能假设它一定在。
   */
  instance_id?: string;
}

/**
 * 一个接入器实例（同属一个 scope 的"一条线"）。
 *
 * 契约：`src/ling/settings/connector_instances.py` 的 `instance_view()`，
 * 端点在 `/v1/settings/connectors/{scope}/instances`。
 *
 * 身份是 `(scope, instance_id)`：`instance_id` **建后不可变**，改名只改
 * `display_name`。配置保存（`PUT /v1/settings/connectors/{scope}?instance_id=`）
 * 认的是 `instance_id`，不是显示名——两者在列表里看起来很像，别混用。
 */
export interface ConnectorInstance {
  scope: string;
  /** 身份。派生自显示名，派生不出时是 `i-<8 位 hex>`。最长 16 字符。 */
  instance_id: string;
  display_name: string;
  /** 停用**不动**已保存的配置与修订链，只表示"这个实例现在不参与运行"。 */
  enabled: boolean;
  /**
   * 默认实例（`instance_id === 'default'`）为 false。
   *
   * **后端算好给的**，不是前端按字面量猜的：默认实例是存量配置的属主，
   * 删掉它会让读取路径静默读到"已删除"的状态。前端据此把删除按钮置灰，
   * 而不是让用户点了才收 409 `default_instance_protected`。
   */
  deletable: boolean;
  created_at: string | null;
  created_by: string | null;
  deleted_at: string | null;
}

/** `GET /v1/settings/connectors/{scope}/instances` 的返回。 */
export interface ConnectorInstancesResponse {
  schema_version: string;
  scope: string;
  /** 后端已按「默认实例最前、其余按创建时间」排好序，前端不再重排。 */
  items: ConnectorInstance[];
  include_deleted: boolean;
}

/** `POST` / `PUT` / `DELETE .../instances/...` 的返回：单个实例的最新状态。 */
export interface ConnectorInstanceResponse {
  schema_version: string;
  item: ConnectorInstance;
}

/**
 * `PUT /v1/settings/connectors/{scope}` 的请求体（`ConnectorChangeBody`）。
 *
 * - `set` 里**没出现的键 = 不改**；
 * - 密钥给空串 = **不改**（密码框总是空的，不能因此变成一次删除，
 *   见 `ling/settings/connector_service.py:198-205`）；
 * - `clear` = 删掉该键的覆盖、回退到 env/默认（**不是写空串**）；
 * - 同一个键同时出现在两边 → 422。
 */
export interface ConnectorChangeRequest {
  set: Record<string, string | boolean | string[]>;
  clear: string[];
}

/**
 * `PUT` 的返回。
 *
 * **202 不等于"已生效"**：终态看 `state`（`active` / `unchanged` /
 * `rolled_back` / `degraded`）。成功生效时 `generation +1`；回滚时
 * generation **不增**（旧配置仍在）。
 */
export interface ConnectorChangeResult {
  schema_version: string;
  operation_id: string;
  scope: string;
  generation: number;
  status: string;
  state: string;
  changed: boolean;
  revision_id: string | null;
  active_revision_id: string | null;
  ack_count: number;
  expected_count: number;
  started_at: string;
  finished_at: string;
  error_code: string | null;
  error_safe: string | null;
  timings: Record<string, number>;
}

/** `POST /v1/settings/connectors/reset` 里一个 scope 的结果。 */
export interface ConnectorResetResult {
  scope: string;
  /** unchanged / active / rolled_back / degraded / failed */
  state: string;
  /** 被清掉的键（回退到 env / 默认） */
  cleared: string[];
  operation_id?: string;
  generation?: number;
  error_code?: string;
  error_safe?: string;
}

/**
 * `POST /v1/settings/connectors/reset` 的返回：三个 scope 一个操作，
 * **全部成功才算成功**（`ok`）。任一 scope 重建失败会保留原 active，
 * 绝不把"部分恢复默认"报成成功。
 */
export interface ConnectorResetResponse {
  schema_version: string;
  ok: boolean;
  results: ConnectorResetResult[];
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
  /**
   * 这家**能服务哪些能力**。只读，来自 `/settings/config`。
   *
   * 草稿里改了 kind 时会丢掉（服务端上次说的能力对新类型不成立），
   * 保存后重新读回。`undefined` = 还不确定，界面按"失败关闭"处理：
   * 新供应商要保存一次才会出现在职责/槽位的下拉里。
   */
  capabilities?: string[];
  /** 不传 = 不改动服务端已有清单；新建供应商时传 []。 */
  models?: ProviderModel[];
  timeout_seconds: number;
  /** `-1` = 不限制（输入框留空时提交 -1）。 */
  max_output_tokens: number | null;
  /** `-1` = 不限制。 */
  daily_token_limit: number | null;
}

export interface ProfileDraftInput {
  profile: string;
  provider_id: string;
  model_id: string;
  fallback_provider_id: string | null;
  fallback_model_id: string | null;
  enabled: boolean;
  /** `slot` = 跟随该职责的默认槽位；此时上面两项会被服务端清成空串。 */
  mode: BindingMode;
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
  name?: string;
  display_name?: string;
  system_prompt?: string;
  traits?: Record<string, unknown>;
  boundaries?: string[];
  /** 整组替换：空串 = 恢复默认，缺键 = 没有覆盖。 */
  scenario_prompts?: Record<string, string>;
}

/**
 * 多人设列表里的一项（`GET /v1/personas`，契约见 `PersonaInput` / `PersonaOut`）。
 *
 * 与 `Persona`（当前激活的那一套，`GET /v1/persona`）是**同一份数据**，这里
 * 只是给列表端点一个明确的形状，并把两个"能缺就缺"的字段标成可选：
 *
 * - `id` 可能不单独给（旧实现只有 `persona_id`），读取一律走
 *   {@link personaId}，组件里不直接碰这两个键名；
 * - `created_at` 可能在旧后端里没有——卡片上的创建时间就会整行不出现，
 *   而不是画一个 `1970/01/01`。
 *
 * `is_active` 是"这一套正在生效"。后端若不在每个对象上带它，前端就退回
 * `GET /v1/persona` 的 `persona_id` 来判——两条路都给，界面上"当前使用"
 * 就不会因为后端少一个字段而消失。
 */
export type PersonaSummary = Persona & {
  /** 新契约的 id。缺失时回退 `persona_id`。 */
  id?: string;
  /** 列表端点上的激活标记；缺失时按 `active_persona_id` / `GET /v1/persona` 判。 */
  is_active?: boolean;
  /** ISO8601；**后端目前不给**（`PersonaListItem` 没有这一列）→ 卡片不显示时间行。 */
  created_at?: string | null;
  updated_at?: string | null;
  /**
   * `system_prompt` 的字数（`PersonaListItem.system_prompt_chars`）。
   *
   * 列表接口**刻意不带正文**（上限 20 万字符，带上会变成几百 KB），所以卡片上
   * 没有摘要时靠它说明"写了 N 字，点开看"。为 0 = 真的没写。
   */
  system_prompt_chars?: number;
  /** `PersonaListItem.has_content`：与注入侧 `persona_is_empty()` 同一个判据。 */
  has_content?: boolean;
  trait_count?: number;
  boundary_count?: number;
  scenario_count?: number;
  /** "4 个技能"徽章。缺失时不出徽章，不编数字。 */
  skill_count?: number | null;
  /** "使用 N 个工具"徽章。 */
  tool_count?: number | null;
  /** 发布态；缺失时按"未发布"处理。 */
  published?: boolean;
};

/**
 * 新建人格的请求体（`POST /v1/personas`，后端 `routes/persona.py::PersonaCreate`）。
 *
 * **没有 `name`**：库里那一列由服务端从 `display_name` 生成（`persona_<slug>-<6 位随机>`），
 * id 也由服务端生成。所以表单里只需要"她叫什么"。
 *
 * **空值不发送**：`system_prompt: ''` 和"没有这个键"在后端是两种意思
 * （前者可能被当成显式清空）。新建时两件事本来就等价，所以统一不发，
 * 让"空就是空"只由后端一处定义。
 */
export interface PersonaCreate {
  display_name?: string;
  system_prompt?: string;
  traits?: Record<string, unknown>;
  boundaries?: string[];
  scenario_prompts?: Record<string, string>;
}

/**
 * 人格列表的一次响应（`GET /v1/personas`）。
 *
 * 契约写的是"直接回 `Persona[]`"，但 REST 列表端点回包一层 `{personas: [...]}`
 * 也很常见。**两种形状都接受**（见 `api.listPersonas`）：读错形状的代价是
 * 整页"暂无人设"——一个非常容易被误当成"用户真的还没建人设"的失败。
 */
export interface PersonaListResponse {
  /** 后端实际字段名是 `items`（`PersonaListOut`）；`personas` 是接受别的写法。 */
  items?: PersonaSummary[];
  personas?: PersonaSummary[];
  /** 当前激活的人格 id（`PersonaListOut.active_persona_id`）。 */
  active_persona_id?: string | null;
  active_id?: string | null;
  total?: number;
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

/**
 * `turn.deferred`：她**选择**过一会儿再回（不是失败、也不是沉默）。
 *
 * 服务端在原轮 `_finalize` **之前**发这个事件，所以它一定先于 `turn.completed`。
 * 前端靠这个顺序决定"显示她说等会儿"还是"显示一条空回复"——后者是 bug 的样子。
 *
 * ## 三个时长字段必须一起看
 *
 * `requested_seconds` 是她**真正要求**的，`applied_seconds` 是代码实际用的
 * （≤300），`clamped` 说明两者不等。只显示 `applied_seconds` 等于把系统的
 * 上限说成她的选择——用户会以为她只想等 5 分钟，而她想等 16 分钟。
 * 钳过时必须让用户看得出来。
 */
export interface TurnDeferredData {
  turn_id: string;
  delay_id?: string;
  requested_seconds?: number;
  applied_seconds?: number;
  clamped?: boolean;
  due_at?: string;
  hard_deadline_at?: string;
  note?: string;
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
  /**
   * 她这一轮**提议撤回**的待确认项（后端 `kernel/turn.py` 的
   * `_collect_recall_proposals`）。
   *
   * 只带 id 与人话说明，**不带被撤那条的正文**——撤回往往正是因为那段内容
   * 不该再被复制一份（见 `_record_recall_proposal` 的 payload 注释）。
   * 字段可选：旧服务端不会下发，前端按"这一轮没有提议"处理。
   */
  recall_proposals?: RecallProposalRef[];
}

/* ---------------------------------------------------------------- 撤回提议 */

/** 提议状态。`expired` 后端目前没有人写入，但契约里有，前端照实渲染。 */
export type RecallProposalState = 'proposed' | 'confirmed' | 'declined' | 'expired';

/**
 * 流里下发的一条撤回提议（`turn.completed` 的 `recall_proposals[]`）。
 *
 * 与 `RecallProposalOut`（列表接口）的区别：流里没有 `created_at`/`window_id`
 * ——那是落库之后才有的。所以前端合并时以列表接口的记录为准，流里这条只用来
 * "立刻把卡片挂上去"，不参与过期判断。
 */
export interface RecallProposalRef {
  proposal_id: string;
  message_id: string;
  note?: string;
}

/** `GET /v1/sessions/{cid}/recall-proposals` 的一项（`RecallProposalOut`）。 */
export interface RecallProposal {
  schema_version: string;
  proposal_id: string;
  /** 她想撤的那条本地消息 id。 */
  message_id: string;
  window_id: string;
  /** 人话："她要撤回这条，确认吗？"。 */
  note: string;
  created_at: string;
}

/**
 * 撤回**执行结果**（`RecallMessageOut`）。
 *
 * `state` 是平台的结论，`note` 是给人看的那句话；`reason_code` 只给日志与
 * 测试，**界面不许显示它**（本项目的通例：人话给用户，原因码给排查）。
 */
export interface RecallResult {
  schema_version: string;
  message_id: string;
  state: 'recalled' | 'not_found' | 'expired' | 'unsupported' | 'uncertain';
  note: string;
  recalled_at?: string | null;
  receipt_id?: string | null;
  attempts?: number;
  reason_code?: string | null;
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

/* ---------------------------------------------------------------- 用户偏好 */

/** 时区从哪一层来：你自己设的 / 环境变量 / 默认值。 */
export type TimezoneSource = 'preference' | 'env' | 'default';

/**
 * `GET|PUT /v1/settings/preferences` 的响应。
 *
 * `timezone` 是**生效值**，`stored` 是库里存的原值（null = 没设过）。
 * 两个都要：只给生效值，用户会把默认值当成自己设过的；只给 stored，
 * 界面无法回答"那现在到底按哪个时区算"。
 */
export interface UserPreferencesResponse {
  schema_version: string;
  timezone: string;
  source: TimezoneSource;
  stored: string | null;
  /** `LING_USER_TIMEZONE` 的值（null = 没配）。 */
  env: string | null;
  default: string;
  /** 常用时区的建议列表；**不是白名单**，任何合法 IANA 名都能填。 */
  common: string[];
  /** 视频读取模式（生效值）：`frames` 抽帧 / `native` 原生视频输入。 */
  video_mode: VideoMode;
  /** 可选值（顺序即展示顺序）。 */
  video_modes: VideoMode[];
  video_mode_default: VideoMode;
}

/**
 * 视频读取模式。
 *
 * - `frames`：抽帧——任何供应商都能用，按固定间隔抽几帧交给视频模型；
 * - `native`：原生视频输入——把视频原样交给模型，只有部分供应商支持，
 *   不支持时后端自动退回抽帧并在日志里记原因。
 *
 * 两种模式都只在**她主动想看**（回复里写了标记）时才读。
 */
export type VideoMode = 'frames' | 'native';

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

/* ------------------------------------------------------ 生活时间轴（只读） */

/**
 * 只读生活时间轴的对外状态（后端 `TimelineState`，六个值**必须互相区分**）。
 *
 * - `empty`：这一天没有计划行——真的没有，不是故障；
 * - `pending`：行已建、大纲还没生成；
 * - `ready`：模型或规则大纲已提交；
 * - `degraded`：有行，但内容是规则兜底（模型不可用/输出被拒收）；
 * - `failed`：最近一次生成失败，原因在 `last_error_code`；
 * - `unavailable`：这一次读取出错，投影拿不到（**不是**"没安排"）。
 */
export type TimelineState =
  | 'ready'
  | 'degraded'
  | 'failed'
  | 'pending'
  | 'empty'
  | 'unavailable';

/** 仪表盘一条今日摘要与"此刻"的关系（后端 `OutlineRelation`）。 */
export type OutlineRelation = 'now' | 'next' | 'anchor';

/** 时间轴上的一个计划段（`layer` 的闭集；未知值原样显示，不猜）。 */
export type TimelineLayer = 'block' | 'hour' | 'minute';

/**
 * `GET /v1/life/timeline` 的 `segments[]`：**计划层**。
 *
 * `start_at` / `end_at` 是原始区间；`display_*` 是裁剪到展示日窗口后的区间。
 * 跨午夜的段在两天各自被裁剪，靠 `continues_*` 说明还有另一半——**不要**把
 * 裁剪后的值当成段的真实起止，也不要用 `start_at` 反推归属日（用 `plan_day`）。
 */
export interface LifeTimelineSegment {
  schema_version: string;
  segment_id: string;
  plan_id: string;
  /** 这个段属于哪一天的**角色计划日**（可能不等于顶层 `plan_day`）。 */
  plan_day: string;
  parent_id: string | null;
  layer: string;
  is_leaf: boolean;
  /** current / superseded / cancelled（已替代段不会出现在响应里）。 */
  status: string;
  revision: number;
  /** model / rule / baseline / user。 */
  source: string;
  /** '' 可变段；sleep / commitment 是不可移动锚点。 */
  anchor_kind: string;
  /** tentative / confirmed。 */
  commitment_level: string;
  place_id: string | null;
  title: string;
  start_at: string;
  end_at: string;
  display_start_at: string;
  display_end_at: string;
  continues_from_previous_day: boolean;
  continues_next_day: boolean;
}

/**
 * `facts[]`：**事实层**（已发生的活动进程），与计划段分开。
 *
 * `evidence_kind=clock_only` 只证明"时间到了"，不能当成"她做过"；
 * `observed` / `user_confirmed` 才是事实。`scene_valid_until=null` 表示
 * 此刻情景状态 unknown（过期或未提交），不要读成"她还在做"。
 */
export interface LifeTimelineFact {
  schema_version: string;
  activity_id: string;
  /** running / paused / completed / failed。 */
  state: string;
  origin: string;
  evidence_kind: string;
  scene_valid_until: string | null;
  started_at: string | null;
  ended_at: string | null;
  place_id: string | null;
}

/** 未来 7 天展望里的一条长程意向（**不是**那一天的日计划）。 */
export interface DayIntentSummary {
  schema_version: string;
  intention_id: string;
  target_start_day: string;
  target_end_day: string;
  /** proposed / planned / confirmed（confirmed 也不等于"某个时段约好"）。 */
  state: string;
  /** model / user / actor / recurrence。 */
  source_kind: string;
  visibility: string;
  priority: number;
}

/**
 * 一晚的梦（模拟叙事）。**不是事实**，且受众受限。
 *
 * `dream=null` = 没有记录；`restricted=true` = 有记录但你看不到正文。
 * 两者不能合并。
 */
export interface DreamCard {
  schema_version: string;
  /** ready / failed / missed / no_dream。 */
  status: string;
  origin: string;
  visibility: string;
  title: string | null;
  narrative: string | null;
  restricted: boolean;
}

/** `GET /v1/life/timeline` 的完整响应（字段名严格对齐后端契约）。 */
export interface LifeTimelineOut {
  schema_version: string;
  state: TimelineState;
  /** 库里 `day_plans.state` 原值：pending / ready / degraded；无行为 null。 */
  plan_state: string | null;
  revision: number;
  /** 最近一次失败/降级/待复核的**原因码**（如 `degraded_late`），不是文案。 */
  last_error_code: string | null;
  display_date: string;
  display_timezone: string;
  display_timezone_source: TimezoneSource;
  display_window_start_at: string;
  display_window_end_at: string;
  actor_timezone: string;
  actor_timezone_source: TimezoneSource;
  plan_day: string;
  plan_window_start_at: string;
  plan_window_end_at: string;
  /** 生成到期时刻（D−1 角色本地 21:00），与 `generated_at` 不是一回事。 */
  outline_due_at: string;
  timezone_source: string;
  /** 当前版本**实际提交**时刻；无计划行时为 null。 */
  generated_at: string | null;
  segments: LifeTimelineSegment[];
  facts: LifeTimelineFact[];
  intents: DayIntentSummary[];
  /** 未来日尚未生成大纲：`intents` 是展望，不是 D+1…D+7 的日计划。 */
  horizon_unplanned: boolean;
  dream: DreamCard | null;
  superseded_segment_count: number;
  hidden_segment_count: number;
  hidden_fact_count: number;
  hidden_intent_count: number;
  /** 有内容因受众裁决被隐藏（空数组 ≠ 没有内容）。 */
  has_private_content: boolean;
  note: string | null;
}

/**
 * `POST /v1/life/outline-jobs` 与 `GET /v1/life/outline-jobs/{job_id}` 的响应。
 *
 * `state` 是**作业**状态：`queued` / `running` / `ready` / `degraded` /
 * `failed` / `unchanged`（这次点击什么都没做）/ `blocked`（入队被预算拦下）。
 * 202 只表示**已提交**，不表示生成成功——还要按 `job_id` 轮询到终态。
 *
 * `note` 是**人话**，直接进界面；`reason_code` 只给日志与测试，
 * 界面上一个字符都不许出现（设计 §0：界面不出现原因码）。
 */
export interface OutlineJobOut {
  schema_version: string;
  /** `unchanged` 且没有对应的作业事件时可以是 null。 */
  job_id: string | null;
  /** 目标日 = **角色计划日**（不是浏览器时区的今天）。 */
  plan_day: string;
  state: string;
  note: string;
  revision: number;
  plan_state: string | null;
  reason_code: string | null;
  /** 这次调用是否真的新入队（false = 复用了已有作业，或什么都没做）。 */
  enqueued: boolean;
}

/** `GET /v1/dashboard` 的 `sections.today_outline.data` 里的一条。 */
export interface TodayOutlineItem {
  schema_version: string;
  segment_id: string | null;
  relation: OutlineRelation;
  title: string;
  start_at: string;
  end_at: string;
}

/** `GET /v1/dashboard` 的 `sections.today_outline.data`。 */
export interface TodayOutlineOut {
  schema_version: string;
  state: TimelineState;
  display_date: string;
  display_timezone: string;
  plan_day: string;
  actor_timezone: string;
  items: TodayOutlineItem[];
  note: string | null;
}

/** `GET /v1/dashboard` 的 `sections.intent_teaser.data` 里的一条。 */
export interface IntentTeaserItem {
  schema_version: string;
  intention_id: string;
  title: string;
  target_start_day: string;
  target_end_day: string;
  state: string;
  source_kind: string;
}

/** `GET /v1/dashboard` 的 `sections.intent_teaser.data`。 */
export interface IntentTeaserOut {
  schema_version: string;
  /** 恒为 true：未来日**尚未生成大纲**，条目是意向而不是安排。 */
  horizon_unplanned: boolean;
  items: IntentTeaserItem[];
  note: string | null;
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

/**
 * `GET /v1/accounts/subjects` 的一页（`docs/API-WEB-V1.md` §13，契约 `SubjectsPage`）。
 *
 * **分页是契约的一部分**：`subjects` 默认只有 50 条，看到 `has_more=true` 就必须
 * 继续 `offset`，不能假设"一次就是全部"——那正是关系页卡死的成因（2000 条 =
 * 561 KB，前端再按 n² 渲染候选）。
 *
 * `total` 是**筛选后**的总行数；`limit` / `offset` / `q` 是服务端回显的实际生效值，
 * 前端不自己猜默认值。
 */
export interface SubjectsResponse {
  subjects: SubjectRow[];
  limit: number;
  offset: number;
  total: number;
  has_more: boolean;
  q: string | null;
}

/** 取关系列表的参数。全部可选：不带参数时服务端给第一页（50 条）。 */
export interface SubjectsQuery {
  limit?: number;
  offset?: number;
  /** 服务端筛选词（昵称 / subject_id / 账号外部 id），空串等于不筛选。 */
  q?: string;
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

/**
 * 回退标记里「上游当时是什么状态」。取值与后端
 * `contracts/v1.py::FallbackState` 完全一致——**不是** `TimelineState`
 * （这里多一个 `unknown`），也不是 `DashboardStatus`（三态 vs 七态）。
 */
export type FallbackState =
  | 'ready'
  | 'degraded'
  | 'empty'
  | 'pending'
  | 'failed'
  | 'unavailable'
  | 'unknown';

/** 这个值是上游给的，还是本领域自己的默认参数。**只有这两种**。 */
export type FallbackSource = 'upstream' | 'domain_default';

/**
 * 一个消费点上"这次的值从哪来"的紧凑标记（后端 `contracts/v1.py::FallbackNotice`）。
 *
 * **它不是事实**：`used=true` 只说明"上游没给出可用的值，我们用了自己的
 * 默认参数"，不代表上游坏了、更不代表她做了什么。界面上必须把这件事说出来，
 * 否则兜底值会被读成她的真实状态（本项目明令禁止的那类伪造）。
 *
 * 没有回退时后端也会给这个标记（`used=false` / `source=upstream`），所以
 * 「没有字段」与「没用回退」是两件事，不能混成一种。
 */
export interface FallbackNotice {
  /** 上游是谁，如 `schedule`（日程）/ `scene`（场景）。 */
  upstream: string;
  /** 这次是否用了领域默认参数。 */
  used: boolean;
  /** 上游当时的状态。 */
  state: FallbackState;
  /** 值的来源。 */
  source: FallbackSource;
  /** 用默认值的人话原因（后端直接写成中文，界面原样显示）；没回退时为 null。 */
  reason: string | null;
}

export interface DashboardSection {
  status: DashboardStatus;
  as_of: string;
  /** 只读，不做原地修改：`sections` 里的分段可能是可选的（见 `today_outline`）。 */
  data?: Readonly<Record<string, unknown>> | null;
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
    /**
     * 她此刻跟手机的关系。
     *
     * 单列一块而不是塞进「此刻」：活动是"她在做什么事"，看手机是"她正低头
     * 看屏幕"——后者是更短、更频繁的状态切换，两者时间尺度不一样。
     */
    phone: DashboardSection;
    /**
     * 「今天她可能做什么」：时间轴上的 2–3 条摘要（当前段 / 接下来 / 已约定）。
     *
     * 与旧的 `agenda` **并列、互不改义**：`agenda` 是"最多几件小事"，这块回答
     * 的是"她这一天的时间轴现在到哪儿了"。运行期按可选处理——旧后端不返回它，
     * 那时整块退化成 `unavailable`，而不是让整页炸掉。
     */
    today_outline?: DashboardSection;
    /**
     * 未来意向预告（1–2 条）。`horizon_unplanned=true` 是契约的一部分：
     * 未来日**没有**生成大纲，这些是意向而不是那一天的安排。
     */
    intent_teaser?: DashboardSection;
    /**
     * 她此刻是不是正在看/刷媒体（全局唯一槽位）。
     *
     * 与 `phone` **分开**：看视频 ≠ 看手机，合成一句会把具体事实说糊。
     * 没有有效占用时是 `empty`，界面不渲染这一块。
     *
     * 受众：`in_my_session === false` 表示观看发生在别人的会话里——那时
     * 后端只给 `kind` 与 `started_at`，不给 `activity_id`，界面也只能说
     * "她正忙着"。
     */
    media?: DashboardSection;
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

/* ---------------------------------------- 镜像更新卡片（四态契约，docs/UPDATE-UX-DESIGN.md）
 *
 * `GET /v1/version/image-update` 的白名单投影 + `POST /v1/version/image-update-checks`
 * 的回执。前端只按字段渲染：路径、日志、原始异常、密钥都进不来；原因码只出现在
 * 默认折叠的「详情/诊断」里，并且必须配一句人话。
 */

/** 卡片结论只有四类（判定优先级由后端算，前端不加自己的版本比较）。 */
export type ImageUpdateStateV2 = 'latest' | 'available' | 'newer_unavailable' | 'unknown';

/** 当前运行版本、构建提交与镜像摘要；读不到就是 null，不用 `pyproject` 猜。 */
export interface ImageUpdateCurrentV2 {
  version: string | null;
  commit: string | null;
  digest: string | null;
}

/** 目录里比当前版本高的每一条（`compatible=false` 的也保留，带不可用原因）。 */
export interface ImageUpdateNewerReleaseV2 {
  version: string;
  /**
   * 目标版本的构建提交。更新器改成直接查 GHCR 之后，registry 的 tag 元数据里
   * 通常拿不到它，所以是 null；只用于展示/核对，不参与「能不能装」的判断。
   */
  commit: string | null;
  /**
   * **已废弃**：发布目录（`deploy/releases.json`）已删除，`has_migration` 失去事实源，
   * 当前后端恒给 null。不要据此推断「包含迁移」或「不需要迁移」——迁移是否需要
   * 由迁移器在实际执行时判断。
   */
  has_migration: boolean | null;
  /**
   * 发布方写的原话；前端不改写、不总结。空数组 = 这一版没有提供更新说明。
   *
   * 注意：当前后端把 `update-notes.json` 的 `summary` 与 `items` **摊平**进这个
   * 数组（summary 在前）；契约正文里没有单独的 `summary` 字段，所以前端不猜
   * 「第一行是不是摘要」——有 `summary` 才渲染摘要行，否则按原样逐条显示。
   */
  notes: string[];
  notes_url: string | null;
  compatible: boolean;
  /** 不可用原因码（`digest_unresolved` 等）；只进详情，且必须配一句人话。 */
  incompatible_reason: string | null;
  /** 后端码表给的人话（已脱敏）；有就用它，缺省用前端码表兜底。 */
  incompatible_reason_message?: string | null;
  digest: string | null;
  /**
   * 契约正文只给了 `notes` / `notes_url`；设计文档 §附 的 `update-notes.json` 里
   * `summary` 是独立字段。后端加法投影时用它；缺失时就**不显示摘要**——不拿第一条
   * `notes` 冒充摘要，也不自己编一句。
   */
  summary?: string | null;
}

/** 唯一可提交的目标：只能来自「兼容 + 摘要已解析」的目录条目。 */
export interface ImageUpdateTargetV2 {
  version: string;
  digest: string;
  /**
   * 目标版本的构建提交。registry 发现拿不到时为 null（这是常态，不是错误）；
   * 只用于展示，不参与安装校验。
   */
  commit: string | null;
}

/**
 * 最近一次安装任务（只给卡片与详情用；不参与发现结论的判定）。
 *
 * 后端把旧的完整任务视图**原样保留**（一个版本）并加 `version` / `failed_code`：
 * 首屏只用 state/stage/version，详情用 request_id、失败码、目标摘要与迁移 head
 * 做三项核验。这里全部按可选处理——缺字段时只显示「未知/未提供」，不当成事实。
 */
export interface ImageUpdateTaskV2 {
  /** 任务状态码：`running` / `succeeded` / `failed_not_started` / `needs_manual` / `resolved`。 */
  state: string;
  /** 阶段码：`prepare` / `backup` / `migrate` / `switch` / `verify`；未知/缺失为 null。 */
  stage: string | null;
  version: string | null;
  failed_code: string | null;
  /** 当前请求 ID（设计文档详情区要求）。 */
  request_id?: string | null;
  /** 后端码表文案（中文）；缺失时前端按 `state` 翻译。 */
  state_message?: string | null;
  stage_message?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  target?: { digest: string | null; commit: string | null } | null;
  migration?: {
    attempted?: boolean;
    current_before?: string | null;
    current_after?: string | null;
    target_head?: string | null;
  } | null;
  failure?: { code?: string | null; stage?: string | null; message?: string | null } | null;
  still_on_old_version?: boolean | null;
}

/** 排队中的安装请求（旧字段保留一个版本；四态契约正文没有它，但排队要能显示）。 */
export interface ImageUpdatePendingRequestV2 {
  request_id: string;
  created_at?: string | null;
  target_digest?: string | null;
}

/** 按需检查的终态元数据（更新器写 `catalog_check`，API 白名单投影）。 */
export interface ImageUpdateCatalogCheckDetailsV2 {
  check_id?: string | null;
  /** running / succeeded / degraded / failed */
  state?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  /** remote / local / none */
  source?: string | null;
  reason?: string | null;
}

/** 目录诊断（设计文档详情区：来源、检查时间、状态与原因）。 */
export interface ImageUpdateCatalogDetailsV2 {
  state?: string | null;
  state_message?: string | null;
  /** 固定码（`catalog_missing` 等），只进详情。 */
  reason?: string | null;
  reason_message?: string | null;
  /** `remote` = 远程发布目录，`local` = 本机回退目录，`none` = 都没有读到。 */
  source?: string | null;
  checked_at?: string | null;
  entries?: number | null;
  /** 本次/上次按需检查的元数据；旧更新器没有这个键。 */
  check?: ImageUpdateCatalogCheckDetailsV2 | null;
}

/** 更新器诊断（在线/状态/协议）。 */
export interface ImageUpdateUpdaterDetailsV2 {
  online?: boolean | null;
  state?: string | null;
  protocol?: number | null;
  version?: string | null;
}

/** 恢复点诊断。 */
export interface ImageUpdateBackupGateDetailsV2 {
  restore_point?: boolean | null;
  complete?: boolean | null;
  reason?: string | null;
  reason_message?: string | null;
  checked_at?: string | null;
}

export interface ImageUpdateDetailsV2 {
  catalog: ImageUpdateCatalogDetailsV2;
  updater: ImageUpdateUpdaterDetailsV2;
  backup_gate: ImageUpdateBackupGateDetailsV2 | null;
}

/** `GET /v1/version/image-update` 的响应体（四态卡片）。 */
export interface ImageUpdateV2 {
  schema_version: 'v1';
  state: ImageUpdateStateV2;
  /** 服务端算好的一句话结论，前端**直接显示**，不自己拼。 */
  message: string;
  /** ISO；null = 从来没有成功检查过。 */
  checked_at: string | null;
  current: ImageUpdateCurrentV2;
  newer: ImageUpdateNewerReleaseV2[];
  target: ImageUpdateTargetV2 | null;
  task: ImageUpdateTaskV2 | null;
  /** 排队中的请求；非空 = 已提交、还没开始。旧字段，后端保留一个版本。 */
  request?: ImageUpdatePendingRequestV2 | null;
  details: ImageUpdateDetailsV2;
  /**
   * 契约正文未列：`AboutPage` 靠它二选一（镜像卡片 / 源码模式页）。缺省按镜像
   * 模式处理；只有显式 `false` 才走源码模式。
   */
  image_mode?: boolean;
}

/** `POST /v1/version/image-update-checks` 的固定请求体（多余字段会被拒绝）。 */
export interface ImageUpdateCheckRequest {
  request_id: string;
}

/** 检查请求回执（200 复用同一 ID / 201 / 202 排队）：只表示已写入，不表示已完成。 */
export interface ImageUpdateCheckAck {
  schema_version?: string;
  request_id: string;
  state?: string;
  state_message?: string;
  created_at?: string;
}

/** `POST /v1/version/image-update-requests` 的**固定 5 键**请求体。 */
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
