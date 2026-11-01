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
  DialoguePolicy,
  LifePolicy,
  MemoryPolicy,
  ProfileDraftInput,
  ProviderDraftInput,
  SecretStatus,
  SessionRecord,
  SettingsConfig,
  ConsentsResponse,
  ConsentUpdate,
  StopResponse,
  TaxonomyResponse,
  ActivityResponse,
  GoalsResponse,
  ProactiveResponse,
  TimelineResponse,
  SubjectsResponse,
  MergeResult,
  UnmergeResult,
  UserInfo,
  DashboardResponse,
  VersionInfo,
  ReleaseCheck,
  UpdateJob,
  UpdateStatus,
  UpdateRequest,
  CoreMemoryInput,
  CoreMemoryListResponse,
  MemoryInclude,
  MemoryListResponse,
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

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
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
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
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
  saveDraft(payload: {
    providers?: ProviderDraftInput[];
    profiles?: ProfileDraftInput[];
    test_mode?: boolean;
    memory?: Partial<MemoryPolicy>;
    life?: Partial<LifePolicy>;
    dialogue?: Partial<DialoguePolicy>;
    note?: string;
  }): Promise<DraftResponse> {
    return request<DraftResponse>('/v1/settings/draft', jsonInit('PUT', payload));
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

  /* ------------------------------------------------ 设置分类法与授权 */
  /** 分类法是导航的唯一来源；前端不自己抄类名。 */
  taxonomy(): Promise<TaxonomyResponse> {
    return request<TaxonomyResponse>('/v1/settings/taxonomy');
  },
  consents(): Promise<ConsentsResponse> {
    return request<ConsentsResponse>('/v1/consents');
  },
  setConsent(body: ConsentUpdate): Promise<{ scope: string; granted: boolean; revision: number }> {
    return request('/v1/consents', jsonInit('PUT', body));
  },

  /* ------------------------------------------------------------ 多用户与账号 */
  subjects(): Promise<SubjectsResponse> {
    return request<SubjectsResponse>('/v1/accounts/subjects');
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

  /** 遗忘全部：软清除当前 subject，返回四类受影响行数之和。没有恢复接口。 */
  forgetAllMemories(): Promise<{ retracted: number }> {
    return request('/v1/memory/forget', { method: 'POST' });
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
};

export type { UserInfo };
