/**
 * ling API v1 客户端。
 *
 * 只调用 docs/API-WEB-V1.md 冻结的端点；不缓存权威历史。
 * 401 统一交给应用层处理（显示登录表单并记住当前会话）。
 */
import type {
  AuthResponse,
  MediaUploadResponse,
  DraftResponse,
  MessagesResponse,
  Persona,
  PersonaUpdate,
  ProbeResult,
  PromptSnapshotOut,
  DialoguePolicy,
  LifePolicy,
  MemoryPolicy,
  ProfileDraftInput,
  ProviderDraftInput,
  QuickConfigPreview,
  ResolveResponse,
  SlotDraftInput,
  SecretStatus,
  SessionRecord,
  SettingsConfig,
  ModelHealthResponse,
  ConsentsResponse,
  ConsentUpdate,
  ConnectorsResponse,
  StopResponse,
  TaxonomyResponse,
  UserPreferencesResponse,
  VideoMode,
  ActivityResponse,
  GoalsResponse,
  ProactiveResponse,
  TimelineResponse,
  LifeTimelineOut,
  OutlineJobOut,
  SubjectsResponse,
  SubjectsQuery,
  MergeResult,
  UnmergeResult,
  UserInfo,
  DashboardResponse,
  VersionInfo,
  ReleaseCheck,
  ImageUpdateV2,
  ImageUpdateCheckAck,
  ImageUpdateSubmission,
  ImageUpdateSubmissionAck,
  UpdateJob,
  UpdateStatus,
  UpdateRequest,
  CoreMemoryInput,
  CoreMemoryListResponse,
  MemoryInclude,
  MemoryListResponse,
  LogQuery,
  LogsResponse,
  RecallProposal,
  RecallResult,
} from './types';

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retriable: boolean;
  readonly detail: string | null;
  readonly traceId: string | null;

  constructor(
    status: number,
    code: string,
    message: string,
    retriable = false,
    detail: string | null = null,
    traceId: string | null = null,
  ) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.retriable = retriable;
    this.detail = detail;
    this.traceId = traceId;
  }
}

/** `POST /v1/memory/forget` 的两段式返回（请求阶段）。 */
export interface ForgetAcceptedResponse {
  tombstone_id: string;
  generation: number;
  events_cancelled: number;
  state: string;
}

/**
 * 旧后端（两段式之前）的返回：只做了软清除，`retracted` 是四类受影响行数之和。
 * 保留这个分支不是为了兼容，而是为了让页面能**如实指出**"这个后端还没真删"。
 */
export interface ForgetLegacyResponse {
  retracted: number;
}

export type ForgetResponse = ForgetAcceptedResponse | ForgetLegacyResponse;

/** `GET /v1/memory/purge/status` 的返回：最近的清理记录。 */
export interface PurgeStatusItem {
  tombstone_id: string;
  state: string;
  requested_at: string | null;
  completed_at: string | null;
  memories_deleted: number;
  episodes_deleted: number;
  core_deleted: number;
  awareness_deleted: number;
  embeddings_deleted: number;
  entities_deleted: number;
  /** 后端墓碑表里有这一列；老后端不返回时按 0 处理。 */
  retrieval_logs_deleted?: number | null;
  events_cancelled: number;
  events_scrubbed?: number | null;
  error_code: string | null;
}

export interface PurgeStatusResponse {
  items: PurgeStatusItem[];
}

let unauthorizedHandler: (() => void) | null = null;

/** 应用层注册：任何请求 401 时回到登录表单（保留当前路由）。 */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

export async function parseErrorResponse(response: Response): Promise<ApiError> {
  let text = '';
  try {
    text = await response.text();
  } catch {
    text = '';
  }
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }
  const error = (payload as { error?: Record<string, unknown> } | null)?.error;
  if (error && typeof error === 'object') {
    const code = typeof error.code === 'string' ? error.code : 'internal';
    const message = typeof error.message === 'string' ? error.message : response.statusText;
    return new ApiError(
      response.status,
      code,
      message || `请求失败（HTTP ${response.status}）`,
      error.retriable === true,
      typeof error.detail === 'string' ? error.detail : null,
      typeof error.trace_id === 'string' ? error.trace_id : null,
    );
  }
  return new ApiError(
    response.status,
    response.status === 401 ? 'unauthorized' : 'internal',
    text.slice(0, 300) || response.statusText || `请求失败（HTTP ${response.status}）`,
    response.status >= 500,
  );
}

async function requestResponse(path: string, init: RequestInit = {}): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(path, { credentials: 'same-origin', ...init });
  } catch (cause) {
    // 主动取消（unmount/轮询换挡）不是网络故障，交给调用方识别并忽略。
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
    throw new ApiError(
      0,
      'network_error',
      '无法连接服务端，请确认后端已启动或网络可用',
      true,
      cause instanceof Error ? cause.message : null,
    );
  }
  if (response.status === 401) {
    const error = await parseErrorResponse(response);
    unauthorizedHandler?.();
    throw error;
  }
  if (!response.ok) {
    throw await parseErrorResponse(response);
  }
  return response;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await requestResponse(path, init);
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

/**
 * 响应里的服务端时刻（HTTP `Date` 头，毫秒时间戳）。
 *
 * 为什么需要它：撤回窗口是 120 秒，而**浏览器时钟不可信**（时区设错、机器
 * 休眠后漂移、手动改过时间）。拿 `Date.now()` 减服务端给的 `created_at`，
 * 时钟一歪就会把"还来得及"判成"来不及"，或者反过来让用户点一个注定失败的
 * 按钮。`Date` 头是服务端自己盖的时间，与 `created_at` 同源，差值才是真的。
 *
 * 拿不到（代理剥掉、CORS 未暴露）时返回 null，调用方退回保守策略。
 */
export function serverNowMsFromResponse(response: Response): number | null {
  const header = response.headers.get('date');
  if (!header) return null;
  const parsed = Date.parse(header);
  return Number.isNaN(parsed) ? null : parsed;
}

function jsonInit(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
}

/** 发送/重试返回原始 SSE 响应，由 sse.ts 解析。 */
async function postStream(path: string, body: unknown | null, signal: AbortSignal): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...(body === null ? { method: 'POST' } : jsonInit('POST', body)),
      credentials: 'same-origin',
      headers: { Accept: 'text/event-stream', ...(body === null ? {} : { 'Content-Type': 'application/json' }) },
      signal,
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') {
      throw cause;
    }
    throw new ApiError(
      0,
      'network_error',
      '无法建立流式连接，请确认服务端可用',
      true,
      cause instanceof Error ? cause.message : null,
    );
  }
  if (response.status === 401) {
    const error = await parseErrorResponse(response);
    unauthorizedHandler?.();
    throw error;
  }
  if (!response.ok) {
    throw await parseErrorResponse(response);
  }
  return response;
}

export const api = {
  /* ------------------------------------------------------------ 认证 */
  me(): Promise<AuthResponse> {
    return request<AuthResponse>('/v1/auth/me');
  },
  dashboard(): Promise<DashboardResponse> {
    return request<DashboardResponse>('/v1/dashboard');
  },
  changeAccount(current: string, username: string): Promise<{ username: string }> {
    return request<{ username: string }>(
      '/v1/auth/account',
      jsonInit('PATCH', { current, username }),
    );
  },

  changePassword(current: string, next: string): Promise<void> {
    return request<void>('/v1/auth/password', jsonInit('POST', { current, new: next }));
  },
  login(username: string, password: string): Promise<AuthResponse> {
    return request<AuthResponse>('/v1/auth/login', jsonInit('POST', { username, password }));
  },
  logout(): Promise<{ ok: boolean }> {
    return request<{ ok: boolean }>('/v1/auth/logout', { method: 'POST' });
  },

  /* ------------------------------------------------------------ 会话 */
  async listSessions(): Promise<SessionRecord[]> {
    const data = await request<{ sessions: SessionRecord[] }>('/v1/sessions');
    return data.sessions;
  },
  createSession(title?: string): Promise<SessionRecord> {
    return request<SessionRecord>('/v1/sessions', jsonInit('POST', title ? { title } : {}));
  },
  renameSession(sessionId: string, title: string): Promise<SessionRecord> {
    return request<SessionRecord>(`/v1/sessions/${encodeURIComponent(sessionId)}`, {
      ...jsonInit('PATCH', { title }),
    });
  },
  messages(sessionId: string, limit = 200): Promise<MessagesResponse> {
    const query = new URLSearchParams({ limit: String(limit) });
    return request<MessagesResponse>(
      `/v1/sessions/${encodeURIComponent(sessionId)}/messages?${query.toString()}`,
    );
  },
  /**
   * 她这次回复时**完整看到的东西**（系统提示词、记忆、历史、用户输入…）。
   *
   * 只有消息所有者能读；不是自己的消息、消息不存在、快照已过保留期都由
   * 服务端决定，前端不按本地字段猜。404 在界面上的落点是「这次没有记录」，
   * 不是空白内容——见 `PromptDrawer.tsx` 的三态映射。
   */
  messagePrompt(messageId: string): Promise<PromptSnapshotOut> {
    return request<PromptSnapshotOut>(
      `/v1/messages/${encodeURIComponent(messageId)}/prompt`,
    );
  },

  /* ------------------------------------------------------- 她提议的撤回 */
  /**
   * 她提议撤回、还没被处理的那些（后端只回 `state='proposed'`）。
   *
   * 用途是"刷新页面后把没处理完的提议找回来"：流式事件只在一轮结束时给一次，
   * 关掉标签页就再也看不到了，而提议本身还挂在库里等着人确认。
   *
   * 失败**不该影响聊天**：调用方自己 catch 成"这块暂时读不到"。
   */
  async recallProposals(sessionId: string): Promise<{
    items: RecallProposal[];
    /** 服务端"现在"（HTTP Date 头）；null = 拿不到，调用方自己决定保守程度。 */
    serverNowMs: number | null;
  }> {
    const response = await requestResponse(
      `/v1/sessions/${encodeURIComponent(sessionId)}/recall-proposals`,
    );
    const items = (await response.json()) as RecallProposal[];
    return { items, serverNowMs: serverNowMsFromResponse(response) };
  },
  /**
   * 用户点「撤回」→ 这时才真的执行（后端用 `confirmed=True` 调同一个工具）。
   *
   * 返回的是**执行结果**而不是"提议状态"：`state` 可能是 `expired`
   * （超过平台窗口）、`unsupported`（这个渠道不支持）、`uncertain`
   * （没拿到回执）——三者的界面文案不同，不能一律说"已撤回"。
   */
  confirmRecall(sessionId: string, proposalId: string): Promise<RecallResult> {
    return request<RecallResult>(
      `/v1/sessions/${encodeURIComponent(sessionId)}/recall-proposals/${encodeURIComponent(proposalId)}/confirm`,
      { method: 'POST' },
    );
  },
  /** 用户点「算了」：只把提议标成 declined，不发任何平台动作（204）。 */
  declineRecall(sessionId: string, proposalId: string): Promise<void> {
    return request<void>(
      `/v1/sessions/${encodeURIComponent(sessionId)}/recall-proposals/${encodeURIComponent(proposalId)}/decline`,
      { method: 'POST' },
    );
  },

  /* ------------------------------------------------------------ 轮次 */
  sendTurn(sessionId: string, clientTurnId: string, text: string, signal: AbortSignal) {
    return postStream(
      `/v1/sessions/${encodeURIComponent(sessionId)}/turns`,
      { client_turn_id: clientTurnId, text },
      signal,
    );
  },
  retryTurn(turnId: string, signal: AbortSignal) {
    return postStream(`/v1/turns/${encodeURIComponent(turnId)}/retry`, null, signal);
  },
  stopTurn(turnId: string): Promise<StopResponse> {
    return request<StopResponse>(`/v1/turns/${encodeURIComponent(turnId)}/stop`, {
      method: 'POST',
    });
  },

  /* ------------------------------------------------------------ 设置 */
  settingsConfig(): Promise<SettingsConfig> {
    return request<SettingsConfig>('/v1/settings/config');
  },
  /**
   * 接入器（渠道）的只读诊断。
   *
   * 不带任何参数：值在宿主 `.env` 里，这条端点只报告"设了没有、通没通"，
   * 密钥值连后端视图对象都不进（见 `ling/settings/connectors.py`）。
   */
  connectors(): Promise<ConnectorsResponse> {
    return request<ConnectorsResponse>('/v1/settings/connectors');
  },
  saveDraft(payload: {
    providers?: ProviderDraftInput[];
    profiles?: ProfileDraftInput[];
    slots?: SlotDraftInput[];
    test_mode?: boolean;
    log_access_denials?: boolean;
    memory?: Partial<MemoryPolicy>;
    life?: Partial<LifePolicy>;
    dialogue?: Partial<DialoguePolicy>;
    note?: string;
  }): Promise<DraftResponse> {
    return request<DraftResponse>('/v1/settings/draft', jsonInit('PUT', payload));
  },
  /**
   * 按**草稿**算每个职责的生效模型。**只算不写**。
   *
   * 界面上的「实际生效」列靠它：改了下拉还没保存，那一列要立刻跟着变。
   * 但回退规则**不在浏览器里实现**——服务端用与保存完全相同的合并 + 解析
   * 跑一遍，前端只显示结论。各算一次迟早不一致，而那种不一致查不出来。
   */
  settingsResolve(payload: {
    providers?: ProviderDraftInput[];
    profiles?: ProfileDraftInput[];
    slots?: SlotDraftInput[];
    test_mode?: boolean;
  }): Promise<ResolveResponse> {
    return request<ResolveResponse>('/v1/settings/resolve', jsonInit('POST', payload));
  },
  /**
   * 「快速配置」的二次确认：把每个职责指回它自己的槽位会发生什么。
   *
   * 返回的 `profiles` 就是点确认后要写入的那份草稿，**不要在浏览器里再拼一次**。
   */
  quickConfigPreview(payload: {
    providers?: ProviderDraftInput[];
    profiles?: ProfileDraftInput[];
    slots?: SlotDraftInput[];
    test_mode?: boolean;
  }): Promise<QuickConfigPreview> {
    return request<QuickConfigPreview>(
      '/v1/settings/quick-config/preview',
      jsonInit('POST', payload),
    );
  },
  publish(revision: number): Promise<{ published_revision: number }> {
    return request<{ published_revision: number }>(
      '/v1/settings/publish',
      jsonInit('POST', { revision }),
    );
  },
  rollback(toRevision: number): Promise<{ published_revision: number }> {
    return request<{ published_revision: number }>(
      '/v1/settings/rollback',
      jsonInit('POST', { to_revision: toRevision }),
    );
  },
  setSecret(providerId: string, secret: string): Promise<SecretStatus> {
    return request<SecretStatus>(
      `/v1/settings/providers/${encodeURIComponent(providerId)}/secret`,
      jsonInit('PUT', { secret }),
    );
  },
  deleteSecret(providerId: string): Promise<SecretStatus> {
    return request<SecretStatus>(
      `/v1/settings/providers/${encodeURIComponent(providerId)}/secret`,
      { method: 'DELETE' },
    );
  },
  probeProvider(providerId: string): Promise<ProbeResult> {
    return request<ProbeResult>(
      `/v1/settings/providers/${encodeURIComponent(providerId)}/probe`,
      { method: 'POST' },
    );
  },
  /** 今日各供应商已用 token。取不到时页面照常工作，只是不显示用量。 */
  settingsUsage(): Promise<{
    day: string | null;
    usage: Record<string, { day: string; used_tokens: number; estimated: boolean }>;
    limits: Record<string, number | null>;
  }> {
    return request('/v1/settings/usage');
  },
  /** 最近调用健康度：键是 `provider_id/model_id`，不含错误原文。 */
  settingsHealth(): Promise<ModelHealthResponse> {
    return request<ModelHealthResponse>('/v1/settings/health');
  },

  /* ------------------------------------------------------------ 媒体 */
  /** 上传一张图片。返回的 evidence_id 挂在当前会话上，她下一轮就能看到。 */
  uploadMedia(sessionId: string, file: File): Promise<MediaUploadResponse> {
    const form = new FormData();
    form.append('upload', file);
    // **不要**手写 Content-Type：multipart 的 boundary 由浏览器生成，
    // 自己设会把 boundary 弄丢，服务端解析失败。
    return request<MediaUploadResponse>(
      `/v1/sessions/${encodeURIComponent(sessionId)}/media`,
      { method: 'POST', body: form },
    );
  },

  /* ------------------------------------------------------------ 生活 */
  activity(): Promise<ActivityResponse> {
    return request<ActivityResponse>('/v1/activity');
  },
  goals(): Promise<GoalsResponse> {
    return request<GoalsResponse>('/v1/goals');
  },
  proactive(): Promise<ProactiveResponse> {
    return request<ProactiveResponse>('/v1/proactive');
  },
  /**
   * 一天的**只读**生活时间轴（`GET /v1/life/timeline`）。
   *
   * 不传 `date` = 服务端按**登录者时区**的今天。前端不自己算"今天"：
   * 同一个请求在两台机器上会落到两个日期（见 docs/API-WEB-V1.md §11.1）。
   * 这个端点不生成、不重排、不调模型，读到的就是库里的投影。
   */
  lifeTimeline(date?: string): Promise<LifeTimelineOut> {
    const query = date ? `?date=${encodeURIComponent(date)}` : '';
    return request<LifeTimelineOut>(`/v1/life/timeline${query}`);
  },
  /**
   * 手动生成某一天的日程（owner only，`POST /v1/life/outline-jobs`）。
   *
   * **`plan_day` 必须来自时间轴的 `plan_day`**：那是角色计划日。按浏览器
   * 时区算"今天"在跨日附近会落到另一个日期，后端只会回 422。
   *
   * 202 = 已入队（不是已生成）：拿 `job_id` 去轮询；200 = 已经生成过、
   * 这次什么都没做（`state=unchanged`）。失败码由调用方翻译成人话。
   */
  createOutlineJob(body: {
    plan_day: string;
    force: boolean;
    request_id: string;
  }): Promise<OutlineJobOut> {
    return request<OutlineJobOut>('/v1/life/outline-jobs', jsonInit('POST', body));
  },
  /** 按作业 id 读生成进度（终态后调用方重读时间轴）。 */
  outlineJob(jobId: string): Promise<OutlineJobOut> {
    return request<OutlineJobOut>(`/v1/life/outline-jobs/${encodeURIComponent(jobId)}`);
  },

  /* ------------------------------------------------ 设置分类法与授权 */
  /** 分类法是导航的唯一来源；前端不自己抄类名。 */
  taxonomy(): Promise<TaxonomyResponse> {
    return request<TaxonomyResponse>('/v1/settings/taxonomy');
  },
  /**
   * 用户自己的偏好（当前只有时区）。
   *
   * **不进 `/settings/draft`**：时区是用户的事实，不是配置修订；跟发布/回滚
   * 绑定会让"改个时区"变成一次部署动作。
   */
  userPreferences(): Promise<UserPreferencesResponse> {
    return request<UserPreferencesResponse>('/v1/settings/preferences');
  },
  /**
   * 写时区。传空串 = 清掉偏好，回退到环境变量/默认值。
   *
   * 非法 IANA 名由服务端拒绝（400 `invalid_request`），前端**不自己维护
   * 时区清单**：那份清单一定会过期，而服务端有 tzdata。
   */
  setTimezone(timezone: string): Promise<UserPreferencesResponse> {
    return request<UserPreferencesResponse>(
      '/v1/settings/preferences',
      jsonInit('PUT', { timezone }),
    );
  },
  /**
   * 改"怎么看视频"（用户偏好，**不在模型页**）。
   *
   * 与 {@link setTimezone} 共用一条 PUT：两者都是 `user_preferences` 的一列，
   * 分开两个端点只会让"保存一半"变得可能。
   */
  setVideoMode(videoMode: VideoMode): Promise<UserPreferencesResponse> {
    return request<UserPreferencesResponse>(
      '/v1/settings/preferences',
      jsonInit('PUT', { video_mode: videoMode }),
    );
  },
  consents(): Promise<ConsentsResponse> {
    return request<ConsentsResponse>('/v1/consents');
  },
  setConsent(body: ConsentUpdate): Promise<{ scope: string; granted: boolean; revision: number }> {
    return request('/v1/consents', jsonInit('PUT', body));
  },

  /* ------------------------------------------------------------ 多用户与账号 */
  /**
   * 一页关系（默认 50 条）。**必须看 `has_more`**：这个列表天然无界
   * （群里见到的每个人都有一行），一次全取会让页面卡死。
   *
   * `q` 由**服务端**筛选（昵称 / subject_id / 账号外部 id）；前端不做本地过滤，
   * 否则"只搜当前页"会骗人。
   */
  subjects(query: SubjectsQuery = {}): Promise<SubjectsResponse> {
    const params = new URLSearchParams();
    if (query.limit !== undefined) params.set('limit', String(query.limit));
    if (query.offset !== undefined) params.set('offset', String(query.offset));
    const q = query.q?.trim();
    if (q) params.set('q', q);
    const suffix = params.toString();
    return request<SubjectsResponse>(`/v1/accounts/subjects${suffix ? `?${suffix}` : ''}`);
  },
  mergeSubjects(body: { source_id: string; target_id: string }): Promise<MergeResult> {
    return request<MergeResult>('/v1/accounts/subjects/merge', jsonInit('POST', body));
  },
  unmergeSubject(sourceId: string): Promise<UnmergeResult> {
    return request<UnmergeResult>(
      `/v1/accounts/subjects/${encodeURIComponent(sourceId)}/unmerge`,
      jsonInit('POST', {}),
    );
  },

  /* ------------------------------------------------------------ 关于与版本 */
  version(): Promise<VersionInfo> {
    return request<VersionInfo>('/v1/version');
  },
  /**
   * 镜像更新卡片（四态）：只读快照，`no-store`。
   *
   * 后端做白名单投影；前端不解析目录、不拼摘要、不比较版本。
   */
  imageUpdate(signal?: AbortSignal): Promise<ImageUpdateV2> {
    return request<ImageUpdateV2>('/v1/version/image-update', {
      cache: 'no-store',
      ...(signal ? { signal } : {}),
    });
  },
  /**
   * 触发一次**真正的按需检查**：后端只把请求原子写进 requests 目录，更新器认领后
   * 重新取目录、解析摘要，结果落回 `status.json`。回执只表示已写入（200/201/202），
   * 是否检查完还要重读 {@link imageUpdate} 看 `checked_at`。
   */
  imageUpdateCheck(requestId: string): Promise<ImageUpdateCheckAck> {
    return request<ImageUpdateCheckAck>(
      '/v1/version/image-update-checks',
      jsonInit('POST', { request_id: requestId }),
    );
  },
  /**
   * 提交一次镜像更新请求。自定义头 X-Ling-Update 是后端的防误触标记；
   * 浏览器会自动带上 Origin，服务端据此做同源校验。
   */
  requestImageUpdate(body: ImageUpdateSubmission): Promise<ImageUpdateSubmissionAck> {
    return request<ImageUpdateSubmissionAck>('/v1/version/image-update-requests', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Ling-Update': '1' },
      body: JSON.stringify(body),
    });
  },
  /** 手动触发的只读检查；不下载、不应用更新。 */
  checkRelease(): Promise<ReleaseCheck> {
    return request<ReleaseCheck>('/v1/version/check-release');
  },
  /** 只读的更新通道状态；不创建任务。 */
  updateStatus(signal?: AbortSignal): Promise<UpdateStatus> {
    return request<UpdateStatus>('/v1/version/update', {
      cache: 'no-store',
      ...(signal ? { signal } : {}),
    });
  },
  /**
   * 创建一次更新请求。自定义头 X-Ling-Update 是后端的防误触标记；
   * 浏览器会自动带上 Origin，服务端据此做同源校验。
   */
  requestUpdate(body: UpdateRequest): Promise<UpdateJob> {
    return request<UpdateJob>('/v1/version/update-requests', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Ling-Update': '1' },
      body: JSON.stringify(body),
    });
  },

  /* ------------------------------------------------------------ 记忆
   *
   * 只调用 docs/API-WEB-V1.md §10 冻结的 8 个端点。刻意**没有**：
   * 单条详情（GET /v1/memory/{id} 不存在）、分页（没有 offset/cursor）、
   * memory_episodes / 实体 / 向量 / 检索日志的读取端点。
   */

  /** 记忆列表。`include` 不传 = 只看现行记忆（未撤回且未归档）。 */
  listMemories(options: { limit?: number; include?: MemoryInclude | null } = {}): Promise<MemoryListResponse> {
    const query = new URLSearchParams({ limit: String(options.limit ?? 50) });
    if (options.include) query.set('include', options.include);
    return request<MemoryListResponse>(`/v1/memory?${query.toString()}`);
  },

  /** 撤回一条记忆：必填理由；重复撤回幂等。 */
  retractMemory(memoryId: string, reason: string): Promise<{ memory_id: string; retracted: boolean }> {
    return request(`/v1/memory/${encodeURIComponent(memoryId)}/retract`, jsonInit('POST', { reason }));
  },

  /** 纠正（supersede）：旧行被置撤回并指向新行，新行 `supersedes` 旧 id。 */
  supersedeMemory(
    memoryId: string,
    text: string,
    reason = '用户纠正',
  ): Promise<{ memory_id: string; supersedes: string }> {
    return request(
      `/v1/memory/${encodeURIComponent(memoryId)}/supersede`,
      jsonInit('POST', { text, reason }),
    );
  },

  /**
   * 遗忘全部（两段式的**请求阶段**）：真删当前 subject 的记忆与派生数据。
   *
   * 这个调用返回时物理删除**还没有做**——后台清理需要几秒到几分钟，进度走
   * `purgeStatus()`。返回里唯一能显示的是 `events_cancelled`（在途事件数），
   * 它**不是**"删掉了多少条记忆"，页面不许把它当计数用。
   *
   * 旧后端（两段式之前）只回 `{retracted}`（软清除的受影响行数）：这里如实
   * 透传，由页面识别并说明"这个后端还在软清除"，不假装已经真删。
   */
  forgetAllMemories(): Promise<ForgetResponse> {
    return request('/v1/memory/forget', { method: 'POST' });
  },

  /** 最近的清理记录：`{items: [...]}`，按时间倒序；没配信号 = 不取消。 */
  purgeStatus(signal?: AbortSignal): Promise<PurgeStatusResponse> {
    return request('/v1/memory/purge/status', {
      cache: 'no-store',
      ...(signal ? { signal } : {}),
    });
  },

  listCoreMemory(): Promise<CoreMemoryListResponse> {
    return request<CoreMemoryListResponse>('/v1/core-memory');
  },

  /** 同 label 覆盖（并重新激活），不是新增第二条。 */
  upsertCoreMemory(body: CoreMemoryInput): Promise<{ core_id: string; label: string; text: string }> {
    return request('/v1/core-memory', jsonInit('PUT', body));
  },

  /** 软删除：active=false，数据仍在库里。 */
  deleteCoreMemory(coreId: string): Promise<{ core_id: string; active: boolean }> {
    return request(`/v1/core-memory/${encodeURIComponent(coreId)}`, { method: 'DELETE' });
  },

  /* ------------------------------------------------------------ 角色 */
  persona(): Promise<Persona> {
    return request<Persona>('/v1/persona');
  },
  updatePersona(patch: PersonaUpdate): Promise<Persona> {
    return request<Persona>('/v1/persona', jsonInit('PUT', patch));
  },
  timeline(limit = 50): Promise<TimelineResponse> {
    return request<TimelineResponse>(`/v1/actor/timeline?limit=${limit}`);
  },

  /* ------------------------------------------------------------ 运行日志
   *
   * 只读**本进程**的环形缓冲（`GET /v1/logs`）：没有分页、没有游标、
   * 没有持久化，缓冲被覆盖的旧日志再也取不回来。要更早的历史得看容器日志
   * 采集，此处不假装能翻页。
   *
   * 只有 owner/admin 能调；普通成员收到 403，不是空列表。
   */

  /**
   * 取最近日志。空参数**不出现在 query 里**：传 `level=` 会让后端收到
   * 空串而不是"不筛选"，那是一个语义不同的请求。
   *
   * `signal` 用于轮询换挡/卸载时真正取消在途请求——否则暂停后仍会有一条
   * 迟到的响应把状态写回去，界面看起来"暂停了还在动"。
   */
  getLogs(params: LogQuery = {}, signal?: AbortSignal): Promise<LogsResponse> {
    const query = new URLSearchParams();
    if (params.limit !== undefined) query.set('limit', String(params.limit));
    if (params.level) query.set('level', params.level);
    if (params.logger) query.set('logger', params.logger);
    if (params.q) query.set('q', params.q);
    if (params.kind) query.set('kind', params.kind);
    const suffix = query.toString();
    return request<LogsResponse>(`/v1/logs${suffix ? `?${suffix}` : ''}`, {
      cache: 'no-store',
      ...(signal ? { signal } : {}),
    });
  },
};

export type { UserInfo };
